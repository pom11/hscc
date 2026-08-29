"""HSCC HTTP API — Profile Library endpoints (install a bot from a git URL).

Surfaces Hermes' OWN profile-distribution mechanism (``hermes profile
export / import / install <git-url>``) over the HSCC API, so the operator's
phone can browse installed profiles, install a reusable "bot" profile from a
git URL, and export a profile to share. This is the most naturally sellable
part of the product: reusable "Flutter engineer" / "BC-AL engineer" bots.

Backing (surfaces the real ``hermes`` CLI — install/export have no Python
library, so we shell out; the READ list is rebuilt structurally from the
profiles directory, never by parsing the human ``hermes profile list`` table):

  * ``GET  /v1/profile/list``          -> read ~/.hermes/profiles/* for name,
                                           model (config.yaml), description,
                                           distribution source
  * ``POST /v1/profile/install``       -> ``hermes profile install <src>``
  * ``POST /v1/profile/export``        -> ``hermes profile export <name>`` into a
                                           controlled export dir
  * ``GET  /v1/profile/export/{file}`` -> download an exported archive (path-safe)

Contract conventions (identical to routes_ops / routes_actions):
  * READ backings DEGRADE to a 200-with-honest-speak on failure (never a
    crash, never fabricated data).
  * MUTATING endpoints require ``confirm: true`` in the body (409
    ``confirm_required`` otherwise); a failed mutation surfaces as a non-2xx
    (never claim success for a change that didn't land).
  * Every READ carries a top-level ``speak`` (design §B).

Export dir: ``<hscc_dir>/profile-exports/``. Exports land there and the
download route serves ONLY files from that controlled dir by a path-safe
filename (rejects ``/``, ``..``, empty) — no arbitrary path reads.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from api_server import ApiError, ROUTES

# --------------------------------------------------------------------------- #
# Hermes CLI resolution
# --------------------------------------------------------------------------- #

# Known installation path first (App-Store-style absolute path if present on
# PATH is not), then a bare `hermes` on PATH. Only used by the real backing
# calls; tests stub the _backing_* functions and never reach here.
_HERMES_CLI_PATHS = (
    os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"),
    "hermes",
)

# Default hermes home where profiles live (mirrors `hermes profile`); may be
# overridden via env HERMES_HOME, resolved at call time.
def _hermes_profiles_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(home) / "profiles"


def _hermes_cli() -> str:
    """Return a runnable hermes CLI path or raise ApiError 500 if not found."""
    for exe in _HERMES_CLI_PATHS:
        resolved = shutil.which(exe) if os.sep not in exe else exe
        if resolved and os.access(resolved, os.X_OK):
            return resolved
    raise ApiError(
        500, "internal_error",
        "hermes CLI not found on this host — profile library unavailable",
        "The Hermes CLI is not available here.",
    )


def _export_dir(ctx) -> Path:
    """The controlled directory exports land in (under the api's hscc dir)."""
    d = Path(getattr(ctx, "hscc_dir", os.path.expanduser("~/.hscc"))) / "profile-exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_list() -> list[dict]:
    """Build a structured list of installed profiles.

    Reads each ``<hermes_home>/profiles/<name>/`` directory directly rather
    than parsing the human `hermes profile list` table (the API never parses
    CLI text). For each profile:
      * ``name``            — directory name
      * ``model``           — ``config.yaml`` ``model.model`` (best-effort)
      * ``description``     — ``description.txt`` content (best-effort)
      * ``is_distribution`` — True when ``distribution.yaml`` is present
      * ``source``          — recorded distribution source (when present)
    """
    profiles_dir = _hermes_profiles_dir()
    profiles = []
    if not profiles_dir.is_dir():
        return profiles
    try:
        entries = sorted(profiles_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return profiles
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        profiles.append(_read_profile(entry, name))
    return profiles


def _read_profile(entry: Path, name: str) -> dict:
    """Read one profile dir into a JSON-safe dict (never raises)."""
    rec: dict = {"name": name}

    # model <- config.yaml model.model
    cfg = entry / "config.yaml"
    if cfg.is_file():
        rec["model"] = _read_yaml_model(cfg)
    else:
        rec["model"] = None

    # description <- description.txt (the kanban orchestrator's mirror
    # of `hermes profile describe` stores it here).
    desc_file = entry / "description.txt"
    if desc_file.is_file():
        try:
            text = desc_file.read_text(errors="replace").strip()
        except OSError:
            text = ""
        rec["description"] = text or None
    else:
        rec["description"] = None

    # distribution metadata <- distribution.yaml (a "bot" profile with a
    # recorded source can be updated / re-shared).
    dist = entry / "distribution.yaml"
    if dist.is_file():
        d = _read_yaml_flat(dist)
        rec["is_distribution"] = True
        rec["source"] = d.get("source")
        rec["version"] = d.get("version")
    else:
        rec["is_distribution"] = False
        rec["source"] = None
        rec["version"] = None

    return rec


def _read_yaml_model(path: Path):
    """Best-effort read of ``model.model`` from a profile config.yaml.

    Returns the scalar string value (or None). Never raises: a profile whose
    config is unreadable / malformed simply reports no model. We parse ONLY
    the single ``model:`` mapping key we need (matching the profile store's
    layout) without pulling in a YAML dependency.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    in_model_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("model:"):
            rest = stripped[len("model:"):].strip()
            # Inline scalar: `model: orchestrator-model`
            if rest:
                return rest.strip("\"'")
            in_model_block = True
            continue
        if in_model_block:
            # A nested key under model:, e.g. `    model: worker-model`
            if re.match(r"^\s{2,}[A-Za-z_]+:", line):
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip("\"'")
                if key == "model" and val:
                    return val
                continue
            # Dedent past the model block -> stop.
            break
    return None


def _read_yaml_flat(path: Path) -> dict:
    """Best-effort flat YAML read for distribution.yaml scalar keys.

    Handles ``key: value`` lines (and quoted values). Returns only scalar
    string values for the keys we care about. Never raises.
    """
    out: dict = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        if stripped.startswith("- "):
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if not key or not val:
            continue
        out[key] = val.strip("\"'")
    return out


def _run_hermes(args, timeout=120) -> subprocess.CompletedProcess:
    """Run a ``hermes`` subcommand; raise ApiError 500 if the CLI is absent."""
    cli = _hermes_cli()
    try:
        return subprocess.run(
            [cli, *args], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ApiError(
            502, "profile_op_timeout",
            f"hermes profile {' '.join(args[:2])} timed out after {timeout}s",
            "The profile operation timed out.",
        )
    except OSError as exc:
        raise ApiError(
            502, "profile_op_failed",
            f"could not run hermes: {exc}",
            "The profile operation could not run.",
        )


def _backing_install(source: str, name: str | None = None) -> dict:
    """Install a profile distribution from a git URL (or local dir).

    Runs ``hermes profile install <source> [--name NAME] --yes`` (``--yes``
    skips the manifest-preview confirmation — the operator already confirmed
    in the app's confirm gate). Returns ``{installed, name, source}``.
    """
    args = ["profile", "install", source, "--yes"]
    if name:
        args += ["--name", name]
    proc = _run_hermes(args)
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip()
        raise ApiError(
            502, "install_failed",
            (reason or f"profile install of {source!r} failed"),
            f"Profile install failed.",
        )
    # The installed profile name = override if given, else the manifest's.
    installed = name or _installed_name_from_output(proc.stdout, source)
    return {"installed": True, "name": installed, "source": source}


def _installed_name_from_output(stdout: str, source: str) -> str:
    """Best-effort extraction of the installed profile name from CLI output."""
    # hermes prints an install line like "Installed profile '<name>'".
    for line in stdout.splitlines():
        m = re.search(r"(?:Installed|installed).{0,40}?['\"]?([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})['\"]?", line)
        if m:
            return m.group(1)
    # Fall back to a repo-name heuristic on the source (github.com/user/repo).
    base = source.rstrip("/").split("/")[-1]
    base = re.sub(r"\.git$", "", base)
    return base or "installed"


def _valid_profile_name(name: str) -> bool:
    """True when ``name`` is a safe bare profile name (no path separators)."""
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$", name))


def _backing_export(profile: str, ctx) -> dict:
    """Export a profile to a tarball in the controlled export dir.

    Runs ``hermes profile export <profile> -o <exportdir>/<profile>.tar.gz``
    and returns ``{profile, filename, path, size}``.
    """
    if not _valid_profile_name(profile):
        raise ApiError(
            400, "bad_request",
            f"invalid profile name {profile!r}",
            "That is not a valid profile name.",
        )
    export_dir = _export_dir(ctx)
    filename = profile + ".tar.gz"
    dest = export_dir / filename
    args = ["profile", "export", profile, "-o", str(dest)]
    proc = _run_hermes(args)
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip()
        raise ApiError(
            502, "export_failed",
            (reason or f"profile export of {profile!r} failed"),
            f"Profile export failed.",
        )
    size = dest.stat().st_size if dest.is_file() else None
    return {
        "profile": profile,
        "filename": filename,
        "path": str(dest),
        "size": size,
    }


# --------------------------------------------------------------------------- #
# speak helpers (design §B)
# --------------------------------------------------------------------------- #

def _speak_list(profiles: list) -> str:
    """§B: \"{n} profile(s) installed.\" (+ a distributable count)."""
    n = len(profiles)
    bots = sum(1 for p in profiles if p.get("is_distribution"))
    base = f"{n} profile{'s' if n != 1 else ''} installed"
    if bots:
        base += f", {bots} from a distribution"
    return base + "."


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def handle_profile_list(server, ctx, query, body):
    """GET /v1/profile/list — browse installed profiles (read-only)."""
    try:
        profiles = _backing_list()
    except Exception:
        # Read backing degrades to an honest empty/broken speak, never a crash.
        return 200, {"profiles": [], "count": 0,
                     "speak": "Profile library unavailable."}
    if not isinstance(profiles, list):
        return 200, {"profiles": [], "count": 0,
                     "speak": "Profile library unavailable."}
    return 200, {"profiles": profiles, "count": len(profiles),
                 "speak": _speak_list(profiles)}


def handle_profile_install(server, ctx, query, body):
    """POST /v1/profile/install — install a bot profile from a git URL.

    Confirm-gated (mutates the host: writes a new profile directory). Body:
    ``{ source, name?, confirm: true }``. ``source`` is the git URL or local
    directory passed to ``hermes profile install``.
    """
    data = _parse_body(body)
    _require_confirm(data, "install a profile")
    source = data.get("source")
    if not source or not str(source).strip():
        raise ApiError(400, "bad_request", "missing required field 'source'",
                       "A profile source is required.")
    name = data.get("name")
    if name is not None and not str(name).strip():
        name = None
    result = _backing_install(str(source), str(name) if name else None)
    payload = dict(result)
    payload["message"] = f"installed profile {result['name']}"
    payload["speak"] = f"Installed profile {result['name']}."
    return 200, payload


def handle_profile_export(server, ctx, query, body):
    """POST /v1/profile/export — export a profile to a shareable tarball.

    Confirm-gated (creates a file on the host). Body:
    ``{ profile, confirm: true }``. Creates ``<hscc_dir>/profile-exports/
    <profile>.tar.gz`` and returns its path + size so the app can show the
    operator where it went and offer to download it.
    """
    data = _parse_body(body)
    _require_confirm(data, "export a profile")
    profile = data.get("profile")
    if not profile or not str(profile).strip():
        raise ApiError(400, "bad_request", "missing required field 'profile'",
                       "A profile name is required.")
    result = _backing_export(str(profile), ctx)
    payload = dict(result)
    payload["message"] = f"exported profile {profile}"
    payload["speak"] = f"Exported profile {profile}."
    return 200, payload


def handle_profile_export_download(server, ctx, query, body):
    """GET /v1/profile/export/{file} — download an exported archive.

    Serves the raw tarball bytes for a ``filename`` previously produced by
    POST /v1/profile/export. Path-safe: only serves files directly inside the
    controlled export dir, and ``{file}`` must be a bare filename (no ``/``,
    no ``..``). Never reads outside that dir.
    """
    import json as _json
    filename = query.get("file")
    if not filename or not str(filename).strip():
        raise ApiError(400, "bad_request", "missing export filename")
    filename = str(filename)
    if "/" in filename or filename in ("", ".", "..") or "\\" in filename:
        raise ApiError(400, "bad_request", "invalid export filename")
    export_dir = _export_dir(ctx)
    path = export_dir / filename
    resolved = path.resolve()
    # Confine the read to the export dir (belt-and-braces against traversal).
    try:
        resolved.relative_to(export_dir.resolve())
    except ValueError:
        raise ApiError(400, "bad_request", "invalid export filename")
    if not path.is_file():
        raise ApiError(404, "not_found", f"no export named {filename!r}")
    data = path.read_bytes()
    # Raw binary escape hatch (see api_server._send_json): the bytes are
    # delivered verbatim, not wrapped in a JSON envelope.
    return 200, {"__raw_bytes__": data,
                 "__raw_content_type__": "application/gzip"}


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_ops / routes_actions)
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
        f"this action installs or exports on the shared host and requires "
        f"\"confirm\": true in the request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/profile/list$"), handle_profile_list))
ROUTES.append(("POST", re.compile(r"^/v1/profile/install$"), handle_profile_install))
ROUTES.append(("POST", re.compile(r"^/v1/profile/export$"), handle_profile_export))
ROUTES.append(
    ("GET", re.compile(r"^/v1/profile/export/(?P<file>[^/]+)$"),
     handle_profile_export_download)
)
