"""daemon.py — the flightdeck daemon ENGINE (lifecycle, log, state, run loop).

This is the generic, flightdeck-agnostic engine, mirroring the SHAPE of HSCC's
``hscc_daemon`` (daemon_ops.py lifecycle + state.py persistence + a per-stream
``run_periodic`` loop). It holds no project-specific check logic — the check
streams live in ``flightdeck/commands/daemon.py`` (which owns the flightdeck
reads and imports the attribution it reuses). The engine is given a ``checks``
map of ``{stream_name: check_fn}`` where each ``check_fn(registry_path)``
returns a result dict with at least ``ok`` and ``message``.

The daemon's whole contract is **detect + report, human decides.** It never
merges, applies, archives, closes, or otherwise mutates any project's state.

Persisted state lives under the flightdeck home (``HERMES_HOME`` or
``~/.flightdeck``) in ``daemon/``.

SAFETY: this module has no code path that can write to a board. It only writes
its own PID file, its own log, and its own per-stream state JSON under
``daemon/``. No ``archive_task``, ``create_task``, ``merge``, ``--apply``, or
``git push`` anywhere.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import threading
import time
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# Paths — everything under the flightdeck home (HERMES_HOME or ~/.flightdeck)
# --------------------------------------------------------------------------- #

DEFAULT_HOME = "~/.flightdeck"
_HOME_ENV = "HERMES_HOME"


def daemon_home() -> str:
    """The resolved flightdeck home for daemon state, honouring ``HERMES_HOME``.

    Same seam as ``qa.qa_home()``: the test suite points ``HERMES_HOME`` at a
    temp sandbox so no default-constructed daemon read/write ever touches the
    operator's real ``~/.flightdeck``. Unset -> ``~/.flightdeck`` (production).
    """
    root = os.environ.get(_HOME_ENV) or DEFAULT_HOME
    return os.path.expanduser(root)


def daemon_dir() -> str:
    """The ``daemon/`` subdir under the flightdeck home (created on demand)."""
    return os.path.join(daemon_home(), "daemon")


PID_FILE = os.path.join(daemon_dir(), "daemon.pid")
LOG_FILE = os.path.join(daemon_dir(), "daemon.log")
STATE_DIR = os.path.join(daemon_dir(), "state")

# --------------------------------------------------------------------------- #
# Lifecycle (PID file) — mirrors hscc_daemon/daemon_ops.py
# --------------------------------------------------------------------------- #


def get_pid() -> Optional[int]:
    """PID from the PID file if that process is alive, else None.

    A stale PID file (file present but process gone) is treated as stopped and
    returns None — the caller is expected to clear it (``write_stopped``).
    """
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def save_pid() -> None:
    """Write the current process's PID to the PID file."""
    os.makedirs(daemon_dir(), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def write_stopped() -> None:
    """Remove the PID file (idempotent; a missing file is not an error)."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def is_running() -> bool:
    """True when the daemon process is currently alive (PID resolves)."""
    return get_pid() is not None


# --------------------------------------------------------------------------- #
# Logging — plain-text timestamped lines to <home>/daemon/daemon.log
# --------------------------------------------------------------------------- #


def log(msg: str, level: str = "INFO") -> None:
    """Append a timestamped line to the daemon log.

    Timestamped in UTC, ``[{iso}] [{LEVEL}] {msg}``. Failure to write is
    swallowed (a log write must never crash the daemon). Also echoes to stdout
    when the daemon is not running as a background process (foreground mode).
    """
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[{ts}] [{level:>5s}] {msg}"
    try:
        os.makedirs(daemon_dir(), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not is_running():
        print(line)


def get_daemon_log_tail(lines: int = 50) -> list[str]:
    """Last ``lines`` lines of the daemon log (empty when none yet)."""
    try:
        with open(LOG_FILE) as f:
            all_lines = f.readlines()
        return all_lines[-lines:]
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- #
# State — persisted per-stream last-check result (survives daemon restarts)
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_state(stream_name: str, data: dict) -> dict:
    """Persist a check result to ``<home>/daemon/state/<stream>.json``.

    Each entry carries ``timestamp``, ``stream`` and the check's own fields.
    A unique tmp file per writer avoids a rename race (same pattern as
    ``hscc_daemon/state.py``). ``os.replace`` is atomic; failure degrades to a
    direct write, then silently drops rather than raising — state persistence
    must never crash the daemon.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
    entry = {"timestamp": now_iso(), "stream": stream_name, **data}
    tmp = f"{filepath}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(entry, f, indent=2, default=str)
        os.replace(tmp, filepath)
    except OSError:
        try:
            with open(filepath, "w") as f:
                json.dump(entry, f, indent=2, default=str)
        except OSError:
            pass
    return entry


def read_state(stream_name: str) -> Optional[dict]:
    """The last persisted result for a stream, or None when none/nonexistent."""
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_all_states() -> dict[str, dict]:
    """Every persisted stream state, keyed by stream name."""
    states: dict[str, dict] = {}
    if not os.path.isdir(STATE_DIR):
        return states
    for fn in os.listdir(STATE_DIR):
        if not fn.endswith(".json"):
            continue
        stream = fn[:-5]
        try:
            with open(os.path.join(STATE_DIR, fn)) as f:
                states[stream] = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return states


# --------------------------------------------------------------------------- #
# Run loop — per-stream periodic scheduling (mirrors hscc_daemon/daemon_ops.py)
# --------------------------------------------------------------------------- #


def run_periodic(
    stream_name: str,
    registry_path: str,
    stop_event: threading.Event,
    *,
    check_fn: Callable,
    interval: int,
    _sleep: Optional[Callable] = None,
) -> None:
    """One check stream's loop: run, persist, log, then wait the interval.

    ``check_fn(registry_path)`` returns a dict with at least ``ok`` and
    ``message``. A failed check is logged as an ERROR/ATTN and the next tick
    still fires on schedule — a transient failure never kills the stream. Each
    run persists its state so ``status``/``check`` stay accurate across daemon
    restarts. ``_sleep`` is a testable wait that defaults to ``stop_event.wait``
    (so the loop wakes immediately on stop instead of blocking the interval).
    """
    while not stop_event.is_set():
        _run_one(stream_name, registry_path, check_fn)
        if _sleep is not None:
            _sleep(interval)
        else:
            stop_event.wait(interval)


def _run_one(stream_name: str, registry_path: str, check_fn: Callable) -> dict:
    """Run one stream, persist its state, and log an attention line when needed.

    Returns the result dict. A raised exception (should not normally happen —
    every check degrades gracefully) is caught, logged as ERROR, and persisted
    as an ``ok: False`` result so ``status`` reflects the broken stream.
    """
    try:
        result = check_fn(registry_path)
    except Exception as exc:  # noqa: BLE001 - a stream must never kill the loop
        log(f"check {stream_name} raised: {exc}", "ERROR")
        result = {"ok": False, "message": f"check raised: {exc}"}
    if not isinstance(result, dict):
        result = {"ok": False, "message": f"check returned non-dict: {result!r}"}
    write_state(stream_name, result)
    if result.get("ok") is False:
        log(f"[{stream_name}] {result.get('message')}", "ATTN")
    return result


def run_daemon_loop(
    registry_path: str,
    checks: dict[str, Callable],
    *,
    intervals: Optional[dict[str, int]] = None,
    stop_event: Optional[threading.Event] = None,
    _sleep: Optional[Callable] = None,
) -> None:
    """Main daemon loop: schedule every stream on its own thread.

    ``checks`` maps stream name -> ``check_fn(registry_path)`` (the command
    layer supplies the concrete flightdeck checks). Each stream runs in its own
    daemon thread via :func:`run_periodic`, so one slow/broken stream never
    delays another.

    ``intervals`` overrides the per-stream schedule (tests); ``stop_event`` is
    what the graceful-shutdown signal handlers set, shared across threads so a
    SIGTERM stops every stream cooperatively. When ``stop_event`` is None a
    fresh event is created and this blocks until ``request_stop`` sets it from
    another thread — the normal daemon process shape. ``_sleep`` is a testable
    wait.
    """
    own_event = stop_event is None
    stop = stop_event if stop_event is not None else threading.Event()
    effective = dict(DEFAULT_INTERVALS)
    if intervals:
        effective.update(intervals)

    log("Daemon loop started")
    threads: list[threading.Thread] = []
    for stream_name, check_fn in checks.items():
        t = threading.Thread(
            target=run_periodic,
            args=(stream_name, registry_path, stop),
            kwargs={
                "check_fn": check_fn,
                "interval": int(effective.get(stream_name, 60)),
            },
            daemon=True,
        )
        t.start()
        threads.append(t)
        log(f"Started {stream_name} check thread (interval={effective.get(stream_name)}s)")

    log(f"All {len(threads)} check threads started; daemon loop running")
    while not stop.is_set():
        if _sleep is not None:
            _sleep(1)
        else:
            stop.wait(1)
    for t in threads:
        t.join(timeout=5)
    log("Daemon loop stopped")
    if own_event:
        write_stopped()


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #

# Default check-stream schedule (seconds). Overridable via ``intervals``.
DEFAULT_INTERVALS: dict[str, int] = {
    "fleet": 60,
    "freshness": 300,
    "orphans": 300,
    "version": 3600,  # rate-limited version drift — once an hour is plenty
}


def install_signal_handlers(stop_event: threading.Event) -> None:
    """Wire SIGTERM/SIGINT to set ``stop_event`` (graceful cooperative stop)."""

    def _handler(signum, frame):  # noqa: ARG001 - frame is required by the signal API
        log(f"Received signal {signum}, shutting down…")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
