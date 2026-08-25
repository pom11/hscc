"""Preserve-or-seed ~/.hscc/autodown.json (operator state, not install payload).

~/.hscc/autodown.json is OPERATOR state: it records whether idle autodown is
armed (``enabled``) and after how many idle minutes it fires (``idle_minutes``).
It is the ONLY thing that decides whether the daemon may tear down the whole
serving fleet. It must therefore NEVER be clobbered by bootstrap.

- If the file EXISTS, leave every existing value exactly as-is (in particular
  ``enabled`` and ``idle_minutes``). Newly-introduced schema keys that are
  absent are added with their safe defaults (forward compatibility) — but an
  existing value is never flipped.
- If the file does NOT exist, seed it with the safe default: ``enabled: false``,
  ``idle_minutes: 10`` (new installs stay opt-in per C5).

Best-effort: never raises. Creates ~/.hscc if missing. Returns what was done so
bootstrap can report it (preserved vs seeded).
"""
import json
import os
import sys

# Mirror hscc_daemon/autodown.py::DEFAULT_CONFIG (the canonical §7 schema). The
# safe default seeds a DISABLED autodown so a fresh install never tears down.
DEFAULT_CONFIG = {
    "enabled": False,            # C5: OFF by default — opt-in
    "idle_minutes": 10,          # default 10; 0 = only via explicit wake
    "state": "up",
    "last_activity_iso": None,
    "down_since": None,
    "wake_source": None,
    "wake_at": None,
    "cancel_requested": False,
    "reason": "",
}

# Values bootstrap is allowed to touch ONLY when seeding a fresh file. When the
# file exists, these are preserved verbatim — they are the operator's decision.
_OPERATOR_KEYS = ("enabled", "idle_minutes")


def _this_dir():
    return os.path.dirname(os.path.abspath(__file__))


def ensure_autodown(autodown_path=None):
    """Preserve an existing autodown.json, or seed a disabled default.

    Args:
        autodown_path: target path (default: ``~/.hscc/autodown.json``).

    Returns:
        dict with ``action`` ("preserved" | "seeded"), ``enabled``,
        ``idle_minutes`` (the final on-disk values), and ``ok`` (True on
        success, False + ``error`` if an OSError occurred).
    """
    if autodown_path is None:
        autodown_path = os.path.expanduser("~/.hscc/autodown.json")

    # --- Branch on whether operator state already exists ---
    exists = False
    existing = {}
    try:
        with open(autodown_path) as f:
            existing = json.load(f)
        exists = isinstance(existing, dict) and bool(existing)
    except FileNotFoundError:
        exists = False  # fresh install — seed
    except (json.JSONDecodeError, OSError):
        # Present but corrupt: we cannot read the operator's `enabled`/
        # `idle_minutes`, so there is nothing to preserve verbatim. Fail
        # CLOSED and rewrite a DISABLED default (§8 "config corrupt"): a
        # corrupt config must never be treated as a reason to ENABLE autodown
        # or to leave it in an ambiguous re-arm-able state. We never flip it
        # ON — only ever OFF.
        exists = False

    if exists:
        # Keep every existing value untouched; add only missing schema keys.
        merged = dict(existing)
        for key, default in DEFAULT_CONFIG.items():
            merged.setdefault(key, default)
        action = "preserved"
    else:
        merged = dict(DEFAULT_CONFIG)
        action = "seeded"

    # --- Atomic write ---
    tmp = None
    try:
        out_dir = os.path.dirname(autodown_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        tmp = autodown_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(merged, f, indent=2)
            f.write("\n")
        os.replace(tmp, autodown_path)
        ok = True
        error = None
    except OSError as e:
        ok = False
        error = str(e)
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    return {
        "action": action,
        "enabled": merged.get("enabled"),
        "idle_minutes": merged.get("idle_minutes"),
        "ok": ok,
        "error": error,
    }


if __name__ == "__main__":
    result = ensure_autodown()
    print(json.dumps(result, indent=2))
    if not result.get("ok", True):
        sys.exit(1)
