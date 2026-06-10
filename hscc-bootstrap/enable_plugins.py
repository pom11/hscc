"""Idempotently wire HSCC into ~/.hermes/config.yaml.

Two things must be true for the HSCC tools to actually work:
  1. each plugin name is in ``plugins.enabled`` (Hermes only loads enabled plugins);
  2. each plugin's toolset is in the top-level ``toolsets`` (a tool is gated by its
     toolset — without it the tool registers but no agent can call it).

Bootstrap calls this so a fresh install is usable, not half-wired. Safe to
re-run: only appends missing names, preserves the rest of the config, backs up
once before writing. Only the orchestrator (active profile = ``default``, which
reads top-level config) gets these — role profiles do not, preserving the
orchestrator-only cluster boundary.
"""
import os
import sys

HSCC_PLUGINS = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]
# Toolsets the orchestrator needs. hscc-commands registers slash COMMANDS (not a
# toolset), so it is intentionally absent here.
HSCC_TOOLSETS = ["hscc-cluster", "sparkrun"]


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


def enable(config_path, plugins=HSCC_PLUGINS, toolsets=HSCC_TOOLSETS):
    """Ensure HSCC plugins + toolsets are wired in config_path.

    Returns {"plugins": [...added], "toolsets": [...added]}. Writes (with one
    backup) only if something changed. No-op + no backup if already wired or if
    the config is missing/malformed.
    """
    import yaml

    if not os.path.exists(config_path):
        return {"plugins": [], "toolsets": []}
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        return {"plugins": [], "toolsets": []}

    added_plugins = _ensure_plugins_enabled(cfg, plugins)
    added_toolsets = _ensure_toolsets(cfg, toolsets)

    if added_plugins or added_toolsets:
        import shutil
        import time
        shutil.copy(config_path,
                    f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        with open(config_path, "w") as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)

    return {"plugins": added_plugins, "toolsets": added_toolsets}


if __name__ == "__main__":
    path = os.path.expanduser("~/.hermes/config.yaml")
    res = enable(path)
    parts = []
    if res["plugins"]:
        parts.append("plugins: " + ", ".join(res["plugins"]))
    if res["toolsets"]:
        parts.append("toolsets: " + ", ".join(res["toolsets"]))
    print(" | ".join(parts) if parts else "already wired (no changes)")
    sys.exit(0)
