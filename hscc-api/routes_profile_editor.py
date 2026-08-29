"""HSCC HTTP API — Per-project profile editor endpoints.

Edits a Hermes profile's worker-editable fields directly on disk, so the
operator can tune a project's bot from the app. A profile's editable surface
is the five fields the kanban orchestrator routes against (the per-project
<project>-orch profile's description is what the decomposer matches tasks
against):

  * ``model``           -> config.yaml ``model.default``
  * ``provider``        -> config.yaml ``model.provider``
  * ``toolsets``        -> config.yaml ``toolsets`` (ordered list)
  * ``preload_skills``  -> config.yaml ``skills.preload`` (ordered list)
  * ``description``     -> profile.yaml ``description`` (routing description)
  * ``compression``     -> config.yaml ``compression`` block (threshold,
                           threshold_tokens)

Contract conventions (identical to routes_profile / routes_ops):

  * READ backings DEGRADE to a 200-with-honest-speak on failure (never a
    crash, never fabricated data).
  * MUTATING endpoints require ``confirm: true`` in the body (409
    ``confirm_required`` otherwise); a failed mutation surfaces as a non-2xx
    (never claim success for a change that didn't land).
  * Every READ carries a top-level ``speak`` (design §B).

The write path is a SAFE merge, not a clobber: only the fields present in the
request body are touched; every other key in config.yaml / profile.yaml is
preserved verbatim. This keeps operator values (api keys, base_urls, auxiliary
compaction routing, alias) intact when the operator tweaks just the model or
the description.

The description lives in ``profile.yaml`` (written by ``write_profile_meta`` /
``hermes profile meta``), NOT ``description.txt`` — the profile-list route's
best-effort mirror. We read and write the authoritative ``profile.yaml`` key.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from api_server import ApiError, ROUTES

# --------------------------------------------------------------------------- #
# Path helpers (same resolution as routes_profile._hermes_profiles_dir)
# --------------------------------------------------------------------------- #


def _hermes_profiles_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "profiles"


# Known toolset catalog for pickers. This mirrors the Hermes toolset
# enumeration (hermes-agent tool reference); the app renders these as
# checkbox options. A toolset the operator enables/disables shows up in the
# profile's config.yaml ``toolsets`` list.
_TOOLSET_CATALOG = [
    "web", "search", "browser", "terminal", "file", "code_execution",
    "coding", "computer_use", "vision", "image_gen", "video", "video_gen",
    "x_search", "tts", "skills", "memory", "session_search",
    "context_engine", "project", "delegation", "cronjob", "clarify",
    "todo", "kanban", "debugging", "safe", "spotify", "homeassistant",
    "discord", "discord_admin", "feishu_doc", "feishu_drive", "yuanbao",
]


def _installed_skill_names(profile_dir: Path) -> list:
    """Enumerate installed skill names (the ``skills.preload`` candidates).

    Scans the GLOBAL skills dir plus the profile-local skills dir for
    ``SKILL.md`` files, returning bare names (last two path components trimmed
    of ``SKILL.md``), sorted. Never raises — a missing dir yields an empty
    list.
    """
    names: set = set()
    for root_dir in (
        Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        / "skills",
        profile_dir / "skills",
    ):
        if not root_dir.is_dir():
            continue
        try:
            for f in root_dir.rglob("SKILL.md"):
                # Preload entries in config.yaml are bare skill names (the leaf
                # directory holding SKILL.md), regardless of category nesting,
                # so surface exactly that so picker selections map 1:1 onto
                # skills.preload. e.g. skills/auto/hermes-agent/SKILL.md ->
                # "hermes-agent".
                names.add(f.parent.name)
        except OSError:
            continue
    return sorted(names)


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #


def _backing_read(profile: str) -> dict:
    """Read one profile's editable fields + available options.

    Returns a dict with ``profile``, ``model``, ``provider``, ``toolsets``,
    ``preload_skills``, ``description``, ``compression``, ``toolsets_all``,
    ``skills_all``. Raises ApiError 404 when the profile does not exist.
    """
    pdir = _resolve_profile_dir(profile)
    cfg, _ = _load_yaml(pdir / "config.yaml")
    meta, _ = _load_yaml(pdir / "profile.yaml")

    model_block = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    comp_block = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
    skills = (cfg.get("skills") or {}) if isinstance(cfg.get("skills"), dict) else {}
    preload = skills.get("preload")
    if not isinstance(preload, list):
        preload = []

    toolsets = cfg.get("toolsets")
    if not isinstance(toolsets, list):
        toolsets = []

    description = None
    if isinstance(meta, dict):
        d = meta.get("description")
        if isinstance(d, str) and d.strip():
            description = d.strip()
        elif d is not None:
            description = d

    return {
        "profile": profile,
        "model": model_block.get("default"),
        "provider": model_block.get("provider"),
        "toolsets": list(toolsets),
        "preload_skills": [str(s) for s in preload],
        "description": description,
        "compression": {
            "threshold": comp_block.get("threshold"),
            "threshold_tokens": comp_block.get("threshold_tokens"),
        },
        "toolsets_all": list(_TOOLSET_CATALOG),
        "skills_all": _installed_skill_names(pdir),
    }


def _backing_update(profile: str, data: dict) -> dict:
    """Merge the supplied fields into the profile's config.yaml + profile.yaml.

    Only the keys present in ``data`` are written; everything else in each
    YAML file is preserved verbatim (safe merge, never a clobber). Reads back
    the resulting editable state.

    Supported top-level data keys: ``model``, ``provider``, ``toolsets``,
    ``preload_skills``, ``description``, ``compression`` (sub-keys
    ``threshold`` / ``threshold_tokens``).
    """
    pdir = _resolve_profile_dir(profile)
    cfg_path = pdir / "config.yaml"
    meta_path = pdir / "profile.yaml"

    changed_config = False

    # --- config.yaml (model / toolsets / skills / compression) ---
    cfg, had_cfg = _load_yaml(cfg_path)
    if "model" in data or "provider" in data:
        model_block = cfg.setdefault("model", {})
        if not isinstance(model_block, dict):
            model_block = {}
            cfg["model"] = model_block
        if "model" in data and data["model"] is not None:
            model_block["default"] = str(data["model"])
        if "provider" in data and data["provider"] is not None:
            model_block["provider"] = str(data["provider"])
        changed_config = True
    if "toolsets" in data:
        if not isinstance(data["toolsets"], list):
            raise ApiError(400, "bad_request", "'toolsets' must be a list of strings")
        cfg["toolsets"] = [str(t) for t in data["toolsets"]]
        changed_config = True
    if "preload_skills" in data:
        if not isinstance(data["preload_skills"], list):
            raise ApiError(400, "bad_request", "'preload_skills' must be a list of strings")
        skills = cfg.get("skills")
        if not isinstance(skills, dict):
            skills = {}
            cfg["skills"] = skills
        skills["preload"] = [str(s) for s in data["preload_skills"]]
        changed_config = True
    if "compression" in data:
        comp_in = data["compression"]
        if not isinstance(comp_in, dict):
            raise ApiError(400, "bad_request", "'compression' must be an object")
        comp = cfg.get("compression")
        if not isinstance(comp, dict):
            comp = {}
            cfg["compression"] = comp
        if "threshold" in comp_in and comp_in["threshold"] is not None:
            comp["threshold"] = comp_in["threshold"]
        if "threshold_tokens" in comp_in and comp_in["threshold_tokens"] is not None:
            comp["threshold_tokens"] = comp_in["threshold_tokens"]
        changed_config = True

    if changed_config or not had_cfg:
        _atomic_yaml_write(cfg_path, cfg)

    # --- profile.yaml (description) ---
    if "description" in data:
        meta, _meta_had = _load_yaml(meta_path)
        if data["description"] is None:
            meta.pop("description", None)
        else:
            meta["description"] = str(data["description"])
        meta["description_auto"] = False
        _atomic_yaml_write(meta_path, meta)

    return _backing_read(profile)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _resolve_profile_dir(profile: str) -> Path:
    """Resolve a profile name to its directory, raising ApiError 404 if absent.

    Enforces a strict name so a path traversal can't escape the profiles dir
    (only letters, digits, ``-``, ``_``, ``.``).
    """
    name = str(profile or "").strip()
    if not name:
        raise ApiError(400, "bad_request", "missing profile name")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ApiError(400, "bad_request",
                       f"invalid profile name {name!r}")
    pdir = (_hermes_profiles_dir() / name).resolve()
    profiles_root = _hermes_profiles_dir().resolve()
    try:
        pdir.relative_to(profiles_root)
    except ValueError:
        raise ApiError(400, "bad_request", f"invalid profile name {name!r}")
    if not pdir.is_dir():
        raise ApiError(404, "not_found", f"no profile named {name!r}")
    return pdir


def _load_yaml(path: Path) -> "tuple[dict, bool]":
    """Best-effort YAML load -> (dict, existed_bool). Never raises.

    A missing file yields ``({}, False)``; a malformed file yields ``({}, True)``
    so the writer can attempt a clean rewrite without losing the file handle.
    """
    if not path.is_file():
        return {}, False
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            data = {}
        return data, True
    except Exception:  # noqa: BLE001 — malformed config degrades to empty
        return {}, True


def _atomic_yaml_write(path: Path, data: dict) -> None:
    """Write a YAML dict atomically (tmpfile + os.replace)."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".prof-editor-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# speak helpers (design §B)
# --------------------------------------------------------------------------- #


def _speak_read(state: dict) -> str:
    model = state.get("model") or "(root config)"
    n_tools = len(state.get("toolsets") or [])
    n_skills = len(state.get("preload_skills") or [])
    return (f"{state['profile']} — model {model}, {n_tools} toolsets, "
            f"{n_skills} preloaded skills.")


def _speak_updated(state: dict, fields: list) -> str:
    what = ", ".join(fields) or "nothing"
    return f"Updated {state['profile']}: {what}."


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def handle_profile_editor_get(server, ctx, query, body):
    """GET /v1/profile/editor/{profile} — read one profile's editable fields."""
    profile = query.get("profile")
    try:
        state = _backing_read(profile)
    except ApiError:
        raise
    except Exception:
        # Read backing degrades to an honest broken speak, never a crash.
        return 200, {
            "profile": profile,
            "error": "unavailable",
            "speak": f"Profile {profile} unavailable.",
        }
    state["speak"] = _speak_read(state)
    return 200, state


def handle_profile_editor_put(server, ctx, query, body):
    """POST /v1/profile/editor/{profile} — update a profile's editable fields.

    Confirm-gated (writes config.yaml + profile.yaml on the shared host).
    Body: ``{ model?, provider?, toolsets?, preload_skills?, description?,
    compression?, confirm: true }``. Only supplied fields are written; the
    rest of each YAML file is preserved verbatim.
    """
    data = _parse_body(body)
    _require_confirm(data, "edit a profile")
    profile = query.get("profile")

    supported = ("model", "provider", "toolsets", "preload_skills",
                 "description", "compression")
    fields = [k for k in supported if k in data]
    if not fields:
        raise ApiError(400, "bad_request",
                       "nothing to update — supply at least one supported field")

    state = _backing_update(profile, data)
    state["speak"] = _speak_updated(state, fields)
    state["updated"] = fields
    return 200, state


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_profile / routes_ops)
# --------------------------------------------------------------------------- #


def _parse_body(body: bytes) -> dict:
    import json
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "request body must be JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_request",
                       "request body must be a JSON object")
    return data


def _require_confirm(data: dict, what: str) -> None:
    if data.get("confirm") is True:
        return
    raise ApiError(
        409, "confirm_required",
        f"this action writes a profile on the shared host and requires "
        f"\"confirm\": true in the request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(
    ("GET", re.compile(r"^/v1/profile/editor/(?P<profile>[^/]+)$"),
     handle_profile_editor_get)
)
ROUTES.append(
    ("POST", re.compile(r"^/v1/profile/editor/(?P<profile>[^/]+)$"),
     handle_profile_editor_put)
)
