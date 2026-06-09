"""State management for the HSCC daemon."""

import os
import json
import threading


STATE_DIR = os.path.expanduser("~/.hscc/state")


def now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def write_state(stream_name, data):
    """Write check result to ~/.hscc/state/<stream>.json."""
    ensure_state_dir()
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
    entry = {
        "timestamp": now_iso(),
        "stream": stream_name,
        **data,
    }
    # Unique tmp per writer: a fixed name races when the same stream check
    # overlaps (two writers, one renames first, the other's tmp vanishes).
    tmp = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(entry, f, indent=2, default=str)
        os.replace(tmp, filepath)
    except (OSError, IOError) as e:
        _log_error(f"write_state({stream_name}) error: {e}")
        # Fallback: write directly
        try:
            with open(filepath, "w") as f:
                json.dump(entry, f, indent=2, default=str)
        except (OSError, IOError):
            pass
    return entry


def read_state(stream_name):
    """Read the last result for a stream, or None."""
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_all_states():
    """Read all state files."""
    ensure_state_dir()
    states = {}
    for fn in os.listdir(STATE_DIR):
        if fn.endswith(".json"):
            stream = fn[:-5]  # strip .json
            filepath = os.path.join(STATE_DIR, fn)
            try:
                with open(filepath) as f:
                    states[stream] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    return states


# log function is imported at runtime from the parent module
def _log_error(msg):
    try:
        from . import log as _log
        _log(msg, "ERROR")
    except ImportError:
        pass
