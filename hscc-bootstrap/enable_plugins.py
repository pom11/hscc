"""Idempotently enable HSCC plugins in ~/.hermes/config.yaml.

Hermes only loads a plugin if its name is in ``plugins.enabled``. Bootstrap
calls this so the cluster toolset + operator slash commands are live after a
fresh install. Safe to re-run: only appends missing names, preserves the rest
of the config, backs up before writing.
"""
import os
import sys

HSCC_PLUGINS = ["hscc-cluster", "hscc-commands", "sparkrun-hermes"]


def enable(config_path, plugins=HSCC_PLUGINS):
    """Ensure each plugin name is in plugins.enabled. Returns the list added."""
    import yaml

    if not os.path.exists(config_path):
        return []  # no config yet — nothing to do (gateway not configured)
    with open(config_path) as fh:
        cfg = yaml.safe_load(fh) or {}

    plug = cfg.setdefault("plugins", {})
    if not isinstance(plug, dict):
        return []  # unexpected shape — don't clobber
    enabled = plug.setdefault("enabled", [])
    if not isinstance(enabled, list):
        return []

    added = [p for p in plugins if p not in enabled]
    if not added:
        return []

    # Backup before mutating.
    import shutil
    import time
    shutil.copy(config_path, f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")

    enabled.extend(added)
    with open(config_path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)
    return added


if __name__ == "__main__":
    path = os.path.expanduser("~/.hermes/config.yaml")
    added = enable(path)
    print("enabled: " + (", ".join(added) if added else "none (already enabled)"))
    sys.exit(0)
