"""Master autonomy flag for the fleet.

A single file ``~/.hscc/autonomy`` holds ``on``/``off``. When on, the
orchestrator runs the idea->spec->tasks pipeline hands-off (no human approval
gate at the spec step). Both the human and the orchestrator flip it via the
`hscc-roles autonomy` CLI. Default (absent file) is OFF — the conservative,
ask-first posture.
"""
import os

HSCC_DIR = os.path.expanduser("~/.hscc")
AUTONOMY_FILE = os.path.join(HSCC_DIR, "autonomy")
_TRUE = ("on", "1", "true", "yes")


def is_on():
    """True iff the autonomy flag file exists and holds a truthy value."""
    try:
        with open(AUTONOMY_FILE) as f:
            return f.read().strip().lower() in _TRUE
    except (FileNotFoundError, OSError):
        return False


def set_state(value):
    """Write the flag atomically. Any non-truthy string disables autonomy."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    tmp = AUTONOMY_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(value).strip() + "\n")
    os.replace(tmp, AUTONOMY_FILE)
