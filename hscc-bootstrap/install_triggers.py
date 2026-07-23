"""Idempotent installer for HSCC default trigger rules.

Reads default rules from triggers.default.json (shipped in the bootstrap
directory) and merges them into ~/.hscc/triggers.json, only adding rules
whose id is not already present. Existing operator rules and any operator
edits to default-rule ids are fully preserved.

Best-effort: never raises. Creates ~/.hscc if missing. Swallows I/O and
JSON parse errors and returns what was actually done.
"""
import json
import os
import shutil
import sys


def _this_dir():
    """Return the directory this script lives in."""
    return os.path.dirname(os.path.abspath(__file__))


def install_triggers(
    triggers_path=None,
    defaults_path=None,
):
    """Install default trigger rules into *triggers_path*.

    Args:
        triggers_path: target path (default: ``~/.hscc/triggers.json``).
        defaults_path: path to default rules (default: triggers.default.json
            in this package's directory).

    Returns:
        dict with ``added`` (list of rule ids that were newly inserted) and
        ``total`` (final number of rules in the file).
    """
    if triggers_path is None:
        triggers_path = os.path.expanduser("~/.hscc/triggers.json")
    if defaults_path is None:
        defaults_path = os.path.join(_this_dir(), "triggers.default.json")

    result = {"added": [], "total": 0}

    # --- Load defaults ---
    try:
        with open(defaults_path) as f:
            defaults = json.load(f)
        default_rules = defaults.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return result

    # --- Load existing ---
    existing_rules = []
    try:
        with open(triggers_path) as f:
            data = json.load(f)
        existing_rules = data.get("rules", [])
    except FileNotFoundError:
        pass  # will create from scratch
    except (json.JSONDecodeError, OSError):
        existing_rules = []  # best-effort: treat corrupt as empty

    # --- Build id set of existing rules ---
    existing_ids = {r.get("id") for r in existing_rules if isinstance(r, dict)}

    # --- Merge: append missing default rules ---
    merged = list(existing_rules)
    for rule in default_rules:
        rid = rule.get("id")
        if rid and rid not in existing_ids:
            merged.append(rule)
            existing_ids.add(rid)
            result["added"].append(rid)

    result["total"] = len(merged)

    # --- Write back ---
    try:
        out_dir = os.path.dirname(triggers_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        # Backup existing file (if present)
        if os.path.exists(triggers_path):
            shutil.copy2(triggers_path, triggers_path + ".bak")

        tmp = triggers_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"rules": merged}, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, triggers_path)
    except OSError:
        pass  # best-effort

    return result


if __name__ == "__main__":
    print(json.dumps(install_triggers(), indent=2, ensure_ascii=False))
