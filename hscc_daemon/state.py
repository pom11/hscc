"""State management for the HSCC daemon."""

import os
import json
import threading


STATE_DIR = os.path.expanduser("~/.hscc/state")

# CLI-side suppression of stream-state persistence. `hscc check` run ad-hoc
# from the terminal must NOT be indistinguishable from the daemon's own
# monitoring result — otherwise a CLI-side failure (TCC, off-LAN laptop, a
# transient blip) clobbers the daemon's shared state file and makes
# `hscc status` report the FLEET as failing when the fleet is fine. So the CLI
# prints only; it never writes the stream files the daemon and `hscc status`
# read. `persist_disabled()` scopes that no-op, and the daemon path (which
# calls the check_* functions directly, never through cmd_check) never touches
# these flags, so its writes are unaffected.
_persist_disabled = threading.local()


class _PersistDisabledCtx:
    """Context manager that suspends real stream-state writes.

    While active, write_state() keeps building/returning its entry (so callers
    that inspect it behave unchanged) but does not touch the state files. The
    would-be entry is stashed in a per-process in-memory overlay so read_state()
    still returns it — this is what lets `hscc check <stream>` print its
    Details line without leaking anything to the daemon's on-disk state.
    """

    def __init__(self):
        self._overlay = {}

    def __enter__(self):
        setattr(_persist_disabled, "ctx", self)
        return self

    def __exit__(self, exc_type, exc, tb):
        setattr(_persist_disabled, "ctx", None)
        return False


def persist_disabled():
    """Context manager: suppress stream-state persistence until exit.

    Use around ad-hoc CLI check execution (hscc_daemon.cli.cmd_check) so a
    manual check never overwrites the daemon's monitoring state. Reads inside
    the block see the suppressed writes via an in-memory overlay, so CLI output
    (e.g. the Details line) still reflects the check just run.
    """
    return _PersistDisabledCtx()


def _current_ctx():
    return getattr(_persist_disabled, "ctx", None)


def now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def write_state(stream_name, data):
    """Write check result to ~/.hscc/state/<stream>.json.

    When a persist_disabled() block is active (ad-hoc CLI check), the entry is
    returned but no file is written — the on-disk state stays exactly as the
    daemon left it, so `hscc status` keeps reporting what the daemon observes.
    """
    entry = {
        "timestamp": now_iso(),
        "stream": stream_name,
        **data,
    }
    ctx = _current_ctx()
    if ctx is not None:
        # CLI-only run: keep the entry in memory for read_state() within the
        # block, but do not touch the shared files. A CLI failure must never
        # flip what `hscc status` reports for the daemon.
        ctx._overlay[stream_name] = entry
        return entry
    ensure_state_dir()
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
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
    """Read the last result for a stream, or None.

    Inside a persist_disabled() block (ad-hoc CLI check), an in-memory overlay
    is consulted first so the caller sees the check it just ran; the on-disk
    file — the daemon's state — is left untouched.
    """
    ctx = _current_ctx()
    if ctx is not None and stream_name in ctx._overlay:
        return ctx._overlay[stream_name]
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
