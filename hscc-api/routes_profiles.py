"""HSCC HTTP API — profile lifecycle + config endpoints (t_740d9489).

Exposes ``hermes profile create/delete/rename/describe/show`` and each
profile's config (``model``, ``toolsets``, ``skills.preload``, ``compression``)
over the API so the app can manage bots.

Follows the established API contract (see routes_kanban.py / routes_ops.py):

  * handlers are ``(server, ctx, query, body) -> (status, dict)``;
  * every READ carries a top-level ``speak`` (design §B);
  * every MUTATING endpoint (create/delete/rename/describe-set) requires
    ``confirm: true`` in the body (409 ``confirm_required`` otherwise — the
    same gate as ``routes_actions.py``). DELETE and RENAME are destructive and
    get a distinct ``confirm_required`` wording; CREATE and DESCRIBE-SET are
    confirm-gated for symmetry but the message reflects that they are additive;
  * read backing errors DEGRADE to a 200-with-honest-speak (never a crash,
    never fabricated data); mutating backing failures surface as a non-2xx
    (never claim success for a change that didn't land).

Backing (the ``hermes_cli.profiles`` library, NEVER CLI text-parsing —
``hermes_cli`` is resolved lazily inside each ``_backing_*`` so tests inject
a fake via ``sys.modules``):

  * ``GET  /v1/profiles/list``               -> ``hermes_cli.profiles.list_profiles()``
  * ``GET  /v1/profiles/{name}``             -> ``show``: profile summary + config whitelist
  * ``GET  /v1/profiles/{name}/describe``    -> ``read_profile_meta().description``
  * ``POST /v1/profiles/create``             -> ``create_profile(...)``
  * ``POST /v1/profiles/{name}/delete``      -> ``delete_profile(name, yes=True)``
  * ``POST /v1/profiles/{name}/rename``      -> ``rename_profile(old, new)``
  * ``POST /v1/profiles/{name}/describe``    -> ``write_profile_meta(dir, description=text)``

SECURITY: the config surface is a STRICT WHITELIST. We expose only
``model.{default,provider}``, ``toolsets``, ``skills.preload`` and the
``compression`` block — NEVER ``model.api_key``, ``model.base_url``, the
``auxiliary`` subtree (which carries ``api_key``/``base_url``), or anything
from ``.env``. Secrets never leave the host.
"""

from __future__ import annotations

import re

from api_server import ApiError, ROUTES

# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests). ``hermes_cli`` is only
# resolvable inside the hermes runtime (or via sys.modules) — deferring the
# import to call time is what lets tests inject a fake ``hermes_cli``.
# --------------------------------------------------------------------------- #

def _profiles_module():
    """Lazily import ``hermes_cli.profiles`` (deferred: resolvable only at
    call time under the hermes runtime, or via a test-injected fake)."""
    from hermes_cli import profiles as profiles_mod
    return profiles_mod


def _backing_list_profiles():
    """Return a list of ``ProfileInfo``-shaped dicts (no secrets)."""
    mod = _profiles_module()
    infos = mod.list_profiles()
    return [_profile_info_to_dict(i, mod) for i in infos]


def _backing_show_profile(name):
    """Return the profile summary + config whitelist for ONE profile, or None
    when the profile does not exist."""
    mod = _profiles_module()
    canon = mod.normalize_profile_name(name)
    mod.validate_profile_name(canon)
    if not mod.profile_exists(canon):
        return None
    profile_dir = mod.get_profile_dir(canon)
    # Find the matching ProfileInfo entry so we reuse the same reads the list
    # uses (gateway_running, skill_count, description, model/provider…).
    info = None
    for i in mod.list_profiles():
        if i.name == canon:
            info = i
            break
    return {
        "summary": _profile_info_to_dict(info, mod) if info is not None
                   else _summary_fallback(canon, profile_dir, mod),
        "config": _extract_config(profile_dir),
    }


def _backing_describe_get(name):
    """Return the profile's description string ('' when unset). Returns None
    when the profile does not exist."""
    mod = _profiles_module()
    canon = mod.normalize_profile_name(name)
    mod.validate_profile_name(canon)
    if not mod.profile_exists(canon):
        return None
    meta = mod.read_profile_meta(mod.get_profile_dir(canon))
    return {
        "description": meta.get("description", ""),
        "description_auto": bool(meta.get("description_auto", False)),
    }


def _backing_create(name, *, clone_from=None, clone_all=False, no_alias=False,
                    no_skills=False, description=None):
    """Create a profile. Raises ValueError/FileExistsError on invalid input."""
    mod = _profiles_module()
    path = mod.create_profile(
        name,
        clone_from=clone_from,
        clone_all=clone_all,
        no_alias=no_alias,
        no_skills=no_skills,
        description=description,
    )
    return str(path)


def _backing_delete(name):
    """Delete a profile (``yes=True`` — the API owns the confirm gate).
    Raises ValueError/FileNotFoundError on invalid input."""
    mod = _profiles_module()
    path = mod.delete_profile(name, yes=True)
    return str(path)


def _backing_rename(old_name, new_name):
    """Rename a profile. Raises ValueError/FileNotFoundError/FileExistsError."""
    mod = _profiles_module()
    path = mod.rename_profile(old_name, new_name)
    return str(path)


def _backing_describe_set(name, text):
    """Set a profile's description. Raises ValueError/FileNotFoundError."""
    mod = _profiles_module()
    canon = mod.normalize_profile_name(name)
    mod.validate_profile_name(canon)
    if not mod.profile_exists(canon):
        raise FileNotFoundError(f"Profile '{canon}' does not exist.")
    mod.write_profile_meta(mod.get_profile_dir(canon), description=text)
    return canon


# --------------------------------------------------------------------------- #
# Pure serializers — never include secrets
# --------------------------------------------------------------------------- #

def _profile_info_to_dict(info, mod) -> dict:
    """Convert a ``ProfileInfo`` to a safe JSON-able dict (whitelist only)."""
    return {
        "name": info.name,
        "is_default": bool(getattr(info, "is_default", False)),
        "gateway_running": bool(getattr(info, "gateway_running", False)),
        "model": getattr(info, "model", None),
        "provider": getattr(info, "provider", None),
        "skill_count": int(getattr(info, "skill_count", 0) or 0),
        "description": getattr(info, "description", ""),
        "description_auto": bool(getattr(info, "description_auto", False)),
        "distribution_name": getattr(info, "distribution_name", None),
        "distribution_version": getattr(info, "distribution_version", None),
    }


def _summary_fallback(canon, profile_dir, mod):
    """Minimal summary used only when ``list_profiles()`` did not surface the
    profile (e.g. a just-created one raced by a read). No secrets."""
    model, provider = None, None
    try:
        model_fn = getattr(mod, "_read_config_model", None)
        if model_fn is not None:
            model, provider = model_fn(profile_dir)
    except Exception:
        pass
    meta = {}
    try:
        meta = mod.read_profile_meta(profile_dir)
    except Exception:
        pass
    return {
        "name": canon,
        "is_default": canon == "default",
        "gateway_running": False,
        "model": model,
        "provider": provider,
        "skill_count": 0,
        "description": meta.get("description", ""),
        "description_auto": bool(meta.get("description_auto", False)),
        "distribution_name": None,
        "distribution_version": None,
    }


# Whitelisted keys inside the ``compression`` block. The block is operator
# tuning (thresholds, ratios, message counting) and holds NO credentials.
_COMPRESSION_SAFE_KEYS = (
    "enabled",
    "threshold",
    "threshold_tokens",
    "target_ratio",
    "protect_last_n",
    "protect_first_n",
)


def _extract_config(profile_dir) -> dict:
    """Return the profile's config WHITELIST: ``model``, ``toolsets``,
    ``skills.preload``, ``compression``.

    Never raises and never exposes secrets — ONLY the documented safe fields
    are returned. ``model`` is reduced to ``{default, provider}``; the
    ``auxiliary`` subtree (which embeds ``api_key``/``base_url``) and any
    ``model`` credential fields are dropped entirely.
    """
    import os
    import yaml
    cfg_path = os.path.join(str(profile_dir), "config.yaml")
    if not os.path.isfile(cfg_path):
        return _empty_config()
    try:
        with open(cfg_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return _empty_config()
    if not isinstance(raw, dict):
        return _empty_config()

    # model — WHITELIST to default/provider only (strip base_url/api_key).
    model_block = {}
    raw_model = raw.get("model")
    if isinstance(raw_model, str):
        model_block = {"default": raw_model, "provider": None}
    elif isinstance(raw_model, dict):
        for key in ("default", "model", "provider"):
            if key in raw_model:
                model_block.setdefault(
                    "default" if key == "model" else key, raw_model[key]
                )
        model_block = {k: model_block[k] for k in ("default", "provider")
                       if k in model_block}

    # toolsets — a list (ignore malformed values defensively).
    raw_toolsets = raw.get("toolsets")
    toolsets = raw_toolsets if isinstance(raw_toolsets, list) else []

    # skills.preload — a list.
    preload = []
    raw_skills = raw.get("skills")
    if isinstance(raw_skills, dict):
        raw_preload = raw_skills.get("preload")
        if isinstance(raw_preload, list):
            preload = raw_preload

    # compression — whitelist the safe operator-tuning keys only.
    compression = {}
    raw_comp = raw.get("compression")
    if isinstance(raw_comp, dict):
        for key in _COMPRESSION_SAFE_KEYS:
            if key in raw_comp:
                compression[key] = raw_comp[key]

    return {
        "model": model_block,
        "toolsets": toolsets,
        "skills": {"preload": preload},
        "compression": compression,
    }


def _empty_config() -> dict:
    return {
        "model": {},
        "toolsets": [],
        "skills": {"preload": []},
        "compression": {},
    }


# --------------------------------------------------------------------------- #
# Body helpers (confirm gate mirrors routes_kanban / routes_ops)
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


def _require_confirm(data: dict, what: str, destructive: bool = False) -> None:
    if data.get("confirm") is True:
        return
    basis = ("irreversibly deletes the profile and its data" if destructive
             else f"mutate the shared profile set — {what}")
    raise ApiError(
        409, "confirm_required",
        f"this action would {basis}; pass \\\"confirm\\\": true in the "
        f"request body to {what}",
        f"Confirmation required to {what}.",
    )


# --------------------------------------------------------------------------- #
# speak helpers (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_list(profiles: list) -> str:
    """§B: '{n} profile(s) total.'"""
    return (f"{len(profiles)} profile{'s' if len(profiles) != 1 else ''} total.")


def _speak_show(data: dict) -> str:
    """§B: 'Profile {name}.' (with model when set)."""
    name = data.get("summary", {}).get("name", "?")
    model = data.get("summary", {}).get("model")
    if model:
        return f"Profile {name} ({model})."
    return f"Profile {name}."


def _speak_describe_get(data: dict) -> str:
    """§B: 'Profile {name} description set/empty.'"""
    name = data.get("name", "?")
    if data.get("description"):
        return f"Profile {name} has a description."
    return f"Profile {name} has no description."


def _speak_create(payload: dict) -> str:
    """§B: 'Created profile {name}.'"""
    return f"Created profile {payload.get('name')}."


def _speak_delete(payload: dict) -> str:
    """§B: 'Deleted profile {name}.'"""
    return f"Deleted profile {payload.get('name')}."


def _speak_rename(payload: dict) -> str:
    """§B: 'Renamed {old} to {new}.'"""
    return f"Renamed {payload.get('old_name')} to {payload.get('name')}."


def _speak_describe_set(payload: dict) -> str:
    """§B: 'Description for {name} updated.'"""
    return f"Description for {payload.get('name')} updated."


# --------------------------------------------------------------------------- #
# Handlers (reads)
# --------------------------------------------------------------------------- #

def handle_list(server, ctx, query, body):
    """GET /v1/profiles/list — every profile + config summary (no secrets)."""
    try:
        profiles = _backing_list_profiles()
    except Exception:
        return 200, {"profiles": [], "count": 0,
                     "speak": "Profile list unavailable."}
    if not isinstance(profiles, list):
        return 200, {"profiles": [], "count": 0,
                     "speak": "Profile list unavailable."}
    return 200, {"profiles": profiles, "count": len(profiles),
                 "speak": _speak_list(profiles)}


def handle_show(server, ctx, query, body):
    """GET /v1/profiles/{name} — profile summary + config whitelist."""
    name = query.get("name")
    if not name or not str(name).strip():
        raise ApiError(400, "bad_request", "missing profile name")
    try:
        data = _backing_show_profile(name)
    except ApiError:
        raise
    except Exception:
        return 200, {"speak": "Profile unavailable."}
    if data is None:
        raise ApiError(404, "not_found", f"profile {name!r} does not exist",
                       f"Profile {name} not found.")
    payload = {"summary": data.get("summary", {}),
               "config": data.get("config", _empty_config())}
    payload["speak"] = _speak_show(payload)
    return 200, payload


def handle_describe_get(server, ctx, query, body):
    """GET /v1/profiles/{name}/describe — the profile's description."""
    name = query.get("name")
    if not name or not str(name).strip():
        raise ApiError(400, "bad_request", "missing profile name")
    try:
        data = _backing_describe_get(name)
    except Exception:
        return 200, {"description": "", "description_auto": False,
                     "name": name, "speak": "Profile description unavailable."}
    if data is None:
        raise ApiError(404, "not_found", f"profile {name!r} does not exist",
                       f"Profile {name} not found.")
    payload = {"name": data.get("name", name),
               "description": data.get("description", ""),
               "description_auto": bool(data.get("description_auto", False))}
    payload["speak"] = _speak_describe_get(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Handlers (mutations — confirm-gated)
# --------------------------------------------------------------------------- #

def handle_create(server, ctx, query, body):
    """POST /v1/profiles/create — create a profile (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "create this profile")
    name = data.get("name")
    if not name or not str(name).strip():
        raise ApiError(400, "bad_request", "missing profile name")
    clone_from = data.get("clone_from")
    clone_all = bool(data.get("clone_all"))
    no_alias = bool(data.get("no_alias"))
    no_skills = bool(data.get("no_skills"))
    description = data.get("description")
    try:
        path = _backing_create(name, clone_from=clone_from, clone_all=clone_all,
                               no_alias=no_alias, no_skills=no_skills,
                               description=description)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(400, "create_failed", str(exc),
                       "Profile could not be created.") from None
    payload = {"name": name, "path": path, "message": f"created profile {name}"}
    payload["speak"] = _speak_create(payload)
    return 200, payload


def handle_delete(server, ctx, query, body):
    """POST /v1/profiles/{name}/delete — DELETE a profile (confirm-gated,
    destructive)."""
    data = _parse_body(body)
    _require_confirm(data, "delete this profile", destructive=True)
    name = query.get("name")
    if not name or not str(name).strip():
        raise ApiError(400, "bad_request", "missing profile name")
    try:
        path = _backing_delete(name)
    except ApiError:
        raise
    except Exception as exc:
        # Map the library's ValueError (default/reserved) and FileNotFoundError
        # (missing) to the right HTTP error rather than a blanket 4xx.
        if "does not exist" in str(exc):
            raise ApiError(404, "not_found", str(exc),
                           f"Profile {name} not found.") from None
        raise ApiError(400, "delete_failed", str(exc),
                       "Profile could not be deleted.") from None
    payload = {"name": name, "path": path, "message": f"deleted profile {name}"}
    payload["speak"] = _speak_delete(payload)
    return 200, payload


def handle_rename(server, ctx, query, body):
    """POST /v1/profiles/{name}/rename — RENAME a profile (confirm-gated,
    destructive)."""
    data = _parse_body(body)
    _require_confirm(data, "rename this profile", destructive=True)
    name = query.get("name")
    if not name or not str(name).strip():
        raise ApiError(400, "bad_request", "missing profile name")
    new_name = data.get("new_name")
    if not new_name or not str(new_name).strip():
        raise ApiError(400, "bad_request", "missing new_name in request body")
    try:
        path = _backing_rename(name, new_name)
    except ApiError:
        raise
    except Exception as exc:
        msg = str(exc)
        if "does not exist" in msg:
            raise ApiError(404, "not_found", msg,
                           f"Profile {name} not found.") from None
        if "already exists" in msg:
            raise ApiError(409, "name_conflict", msg,
                           f"A profile named {new_name} already exists.") from None
        raise ApiError(400, "rename_failed", msg,
                       "Profile could not be renamed.") from None
    payload = {"name": new_name, "old_name": name, "path": path,
               "message": f"renamed profile {name} to {new_name}"}
    payload["speak"] = _speak_rename(payload)
    return 200, payload


def handle_describe_set(server, ctx, query, body):
    """POST /v1/profiles/{name}/describe — set the profile's description
    (confirm-gated)."""
    data = _parse_body(body)
    _require_confirm(data, "update this profile's description")
    name = query.get("name")
    if not name or not str(name).strip():
        raise ApiError(400, "bad_request", "missing profile name")
    text = data.get("description")
    if text is None:
        raise ApiError(400, "bad_request",
                       "missing description in request body")
    if not isinstance(text, str):
        raise ApiError(400, "bad_request", "description must be a string")
    try:
        canon = _backing_describe_set(name, text)
    except ApiError:
        raise
    except Exception as exc:
        if "does not exist" in str(exc):
            raise ApiError(404, "not_found", str(exc),
                           f"Profile {name} not found.") from None
        raise ApiError(400, "describe_failed", str(exc),
                       "Profile description could not be updated.") from None
    payload = {"name": canon, "message": f"updated description for {canon}"}
    payload["speak"] = _speak_describe_set(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #
# NOTE: ``/v1/profiles/list`` is registered BEFORE the ``{name}`` wildcard so
# the exact match wins; the ``{name}`` routes are anchored with ``$`` so they
# never swallow the ``/describe`` subpaths.

ROUTES.append(("GET", re.compile(r"^/v1/profiles/list$"), handle_list))
ROUTES.append(("GET", re.compile(r"^/v1/profiles/(?P<name>[^/]+)$"),
               handle_show))
ROUTES.append(
    ("GET", re.compile(r"^/v1/profiles/(?P<name>[^/]+)/describe$"),
     handle_describe_get)
)
ROUTES.append(("POST", re.compile(r"^/v1/profiles/create$"), handle_create))
ROUTES.append(
    ("POST", re.compile(r"^/v1/profiles/(?P<name>[^/]+)/delete$"),
     handle_delete)
)
ROUTES.append(
    ("POST", re.compile(r"^/v1/profiles/(?P<name>[^/]+)/rename$"),
     handle_rename)
)
ROUTES.append(
    ("POST", re.compile(r"^/v1/profiles/(?P<name>[^/]+)/describe$"),
     handle_describe_set)
)
