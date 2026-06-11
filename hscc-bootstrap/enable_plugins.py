"""Idempotently wire HSCC into ~/.hermes/config.yaml.

Four things must be true for the HSCC fleet to actually work:
  1. each plugin name is in ``plugins.enabled`` (Hermes only loads enabled plugins);
  2. each plugin's toolset is in the top-level ``toolsets`` (a tool is gated by its
     toolset — without it the tool registers but no agent can call it);
  3. kanban routing sends catch-all work to a WORKER, not the orchestrator
     (``kanban.default_assignee`` + concurrency caps);
  4. orchestrator subagents run on the WORKER pool, not the gateway GPU
     (``delegation.base_url`` → the load-balanced worker proxy).

Bootstrap calls this so a fresh install is fully wired, not half-wired and
silently dumping all work onto the orchestrator. Safe to re-run: only fills
missing/default values, never clobbers a value the operator deliberately set,
preserves the rest of the config, backs up once before writing.
"""
import os
import sys

HSCC_PLUGINS = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]
# Toolsets the orchestrator needs:
#   hscc-cluster — cluster ops (orchestrator-only)
#   sparkrun     — sparkrun_exec passthrough
#   delegation   — the delegate_task tool, so the orchestrator can actually spawn
#                  subagents (they run on the worker pool via delegation.base_url).
#                  Without this the orchestrator has no way to delegate and does
#                  everything inline.
# hscc-commands registers slash COMMANDS (not a toolset), so it is absent here.
HSCC_TOOLSETS = ["hscc-cluster", "sparkrun", "delegation"]

# Fleet-routing defaults. The worker proxy (sparkrun LiteLLM LB) load-balances
# every worker GPU behind one URL; env-overridable to match the generator.
WORKER_PROXY_URL = os.environ.get("HSCC_WORKER_PROXY_URL", "http://localhost:4000/v1")
WORKER_MODEL = os.environ.get("HSCC_WORKER_MODEL", "Qwen/Qwen3.6-27B-FP8")
WORKER_PROXY_KEY = os.environ.get("HSCC_WORKER_PROXY_KEY", "sk-sparkrun")
DEFAULT_ASSIGNEE = os.environ.get("HSCC_DEFAULT_ASSIGNEE", "worker")
# Concurrency: board-wide cap + per-profile cap. Defaults sized so the single
# `worker` catch-all role can run many tasks in parallel (the proxy spreads them).
MAX_IN_PROGRESS = int(os.environ.get("HSCC_MAX_IN_PROGRESS", "30"))
MAX_IN_PROGRESS_PER_PROFILE = int(os.environ.get("HSCC_MAX_IN_PROGRESS_PER_PROFILE", "10"))


def _ensure_plugins_enabled(cfg, plugins):
    """Append missing plugin names to plugins.enabled. Returns names added.

    Returns [] without mutating on a bad/unexpected config shape.
    """
    plug = cfg.setdefault("plugins", {})
    if not isinstance(plug, dict):
        return []
    enabled = plug.setdefault("enabled", [])
    if not isinstance(enabled, list):
        return []
    added = [p for p in plugins if p not in enabled]
    enabled.extend(added)
    return added


def _ensure_toolsets(cfg, toolsets):
    """Append missing toolset names to the top-level toolsets. Returns added.

    ``toolsets`` in config may be a JSON-string or a YAML list (Hermes'
    _normalize_toolsets accepts both). We normalize to a list and write it back
    as a list. Returns [] without mutating on a bad shape.
    """
    import json

    raw = cfg.get("toolsets")
    if isinstance(raw, str):
        try:
            current = json.loads(raw)
        except (ValueError, TypeError):
            return []
    elif isinstance(raw, list):
        current = raw
    elif raw is None:
        current = ["hermes-cli"]  # Hermes' own default
    else:
        return []
    if not isinstance(current, list):
        return []

    added = [t for t in toolsets if t not in current]
    current.extend(added)
    cfg["toolsets"] = current  # normalize to a list regardless
    return added


def _ensure_kanban_routing(cfg):
    """Route catch-all kanban work to a worker + size concurrency. Returns the
    keys changed.

    Only fills an EMPTY/absent ``default_assignee`` (so an operator who set a
    specific assignee keeps it). Caps are only RAISED toward the HSCC defaults —
    never lowered, so a deliberately larger cap is preserved.
    """
    k = cfg.setdefault("kanban", {})
    if not isinstance(k, dict):
        return []
    changed = []
    if not (k.get("default_assignee") or "").strip():
        k["default_assignee"] = DEFAULT_ASSIGNEE
        changed.append("default_assignee")
    for key, want in (("max_in_progress", MAX_IN_PROGRESS),
                      ("max_in_progress_per_profile", MAX_IN_PROGRESS_PER_PROFILE)):
        cur = k.get(key)
        if not isinstance(cur, int) or cur < want:
            k[key] = want
            changed.append(key)
    return changed


def _ensure_delegation(cfg):
    """Point orchestrator subagents at the worker proxy. Returns keys changed.

    Only fills EMPTY fields, so an operator-chosen delegation endpoint is kept.
    """
    d = cfg.setdefault("delegation", {})
    if not isinstance(d, dict):
        return []
    changed = []
    for key, want in (("base_url", WORKER_PROXY_URL), ("model", WORKER_MODEL),
                      ("provider", "custom"), ("api_key", WORKER_PROXY_KEY)):
        if not (d.get(key) or "").strip():
            d[key] = want
            changed.append(key)
    return changed


def enable(config_path, plugins=HSCC_PLUGINS, toolsets=HSCC_TOOLSETS):
    """Ensure HSCC plugins + toolsets + fleet routing are wired in config_path.

    Returns {"plugins": [...], "toolsets": [...], "kanban": [...], "delegation":
    [...]} of what changed. Writes (with one backup) only if something changed.
    No-op + no backup if already wired or if the config is missing/malformed.
    """
    import yaml

    empty = {"plugins": [], "toolsets": [], "kanban": [], "delegation": []}
    if not os.path.exists(config_path):
        return empty
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        return empty

    added_plugins = _ensure_plugins_enabled(cfg, plugins)
    added_toolsets = _ensure_toolsets(cfg, toolsets)
    changed_kanban = _ensure_kanban_routing(cfg)
    changed_delegation = _ensure_delegation(cfg)

    if added_plugins or added_toolsets or changed_kanban or changed_delegation:
        import shutil
        import time
        shutil.copy(config_path,
                    f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        with open(config_path, "w") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)

    return {"plugins": added_plugins, "toolsets": added_toolsets,
            "kanban": changed_kanban, "delegation": changed_delegation}


if __name__ == "__main__":
    path = os.path.expanduser("~/.hermes/config.yaml")
    res = enable(path)
    parts = [f"{k}: {', '.join(v)}" for k, v in res.items() if v]
    print(" | ".join(parts) if parts else "already wired (no changes)")
    sys.exit(0)
