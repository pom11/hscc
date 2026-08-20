"""Daemon lifecycle management (PID, log, stream watcher, loop)."""

import json
import os
import signal
import subprocess
import threading
import time
import datetime


PID_FILE = os.path.expanduser("~/.hscc/daemon.pid")
LOG_FILE = os.path.expanduser("~/.hscc/daemon.log")
STATE_DIR = os.path.expanduser("~/.hscc/state")


def get_pid(pid_file=None):
    """Read PID from file, return None if not running.

    ``pid_file`` defaults to the daemon's PID_FILE. Pass an explicit path to
    reuse the same read/verify logic for a different service (e.g. the API
    server's ``~/.hscc/api.pid``) — one mechanism, not a parallel one.
    """
    pid_file = pid_file or PID_FILE
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None
    except (FileNotFoundError, ValueError):
        return None


def save_pid(pid_file=None):
    """Write current PID to file."""
    with open(pid_file or PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def write_stopped(pid_file=None):
    """Remove PID file."""
    try:
        os.remove(pid_file or PID_FILE)
    except FileNotFoundError:
        pass


def get_daemon_log_tail(lines=50):
    """Read last N lines from daemon log."""
    try:
        with open(LOG_FILE) as f:
            all_lines = f.readlines()
        return all_lines[-lines:]
    except FileNotFoundError:
        return []


def stream_watcher(stream=None, interval=2):
    """Tail the state directory for updates in real-time."""
    from .state import read_all_states
    
    print(f"Watching {stream or 'all'} streams (Ctrl+C to stop)...\n")
    last_state = {}

    try:
        for fn in os.listdir(STATE_DIR):
            if fn.endswith(".json"):
                stream_name = fn[:-5]
                fp = os.path.join(STATE_DIR, fn)
                try:
                    with open(fp) as f:
                        last_state[stream_name] = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
    except FileNotFoundError:
        pass

    try:
        while True:
            stream_names = [stream] if stream and stream != "all" else list(last_state.keys())

            for sn in stream_names:
                fp = os.path.join(STATE_DIR, f"{sn}.json")
                try:
                    with open(fp) as f:
                        current = json.load(f)
                    if current != last_state.get(sn):
                        ts = current.get("timestamp", "?")[:19]
                        ok = current.get("ok", current.get("blocked", "n/a"))
                        msg = current.get("message", current.get("ok", ""))
                        print(f"\n[{ts}] {sn:12s} ok={str(ok):5s} | {msg}")
                        last_state[sn] = current
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")


def ensure_state_dir():
    """Ensure the state directory exists."""
    os.makedirs(STATE_DIR, exist_ok=True)


HSCC_DIR = os.path.expanduser("~/.hscc")
_BAK_KEEP = 5  # newest N of each <file>.bak.* kept


def prune_dead_files(hscc_dir=None):
    """Remove dead daemon cruft on startup: .corrupt-* snapshots + .stale flags
    (both ignored by the daemon), and cap any <file>.bak.* group at _BAK_KEEP.

    Best-effort + idempotent — never raises. Returns a summary dict of counts.
    """
    import glob
    d = hscc_dir or HSCC_DIR
    removed_dead, pruned_bak = 0, 0
    # 1. dead files
    for pat in ("*.corrupt-*", "*.stale"):
        for f in glob.glob(os.path.join(d, pat)):
            try:
                os.remove(f)
                removed_dead += 1
            except OSError:
                pass
    # 2. cap each <stem>.bak.* group (e.g. serving.json.bak.*, models.json.bak.*)
    groups = {}
    for f in glob.glob(os.path.join(d, "*.bak.*")):
        stem = f.rsplit(".bak.", 1)[0]
        groups.setdefault(stem, []).append(f)
    for files in groups.values():
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[_BAK_KEEP:]:
            try:
                os.remove(old)
                pruned_bak += 1
            except OSError:
                pass
    return {"removed_dead": removed_dead, "pruned_bak": pruned_bak}


def log(msg, level="INFO", log_file=None, pid_file=None):
    """Write a timestamped log line to the daemon log file.

    ``log_file`` / ``pid_file`` default to the daemon's LOG_FILE / PID_FILE.
    Pass explicit paths to reuse the same timestamped format for a different
    service's log (~/.hscc/api.log) — one log convention, not a parallel one.
    """
    log_file = log_file or LOG_FILE
    pid_file = pid_file or PID_FILE
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[{ts}] [{level:>5s}] {msg}"
    try:
        with open(log_file, "a") as f:
            f.write(line + "\n")
    except IOError:
        pass
    # Also print if daemon is running in foreground mode
    if not os.path.exists(pid_file):
        print(line)


def run_daemon_loop():
    """Main daemon event loop (polling mode fallback)."""
    from .health import check_dgx, check_gateway, check_local, check_heartbeat, check_nas, check_idle_monitor, check_workers, check_proxy
    from .trigger import trigger_engine
    from .lifecycle import pipeline_watchdog, restart_vllm, load_watchdog_block
    from .state import now_iso, write_state

    ensure_state_dir()
    log("Daemon loop started")
    # Self-clean dead cruft on startup: .corrupt-*/.stale + uncapped .bak.* groups.
    try:
        pruned = prune_dead_files()
        if pruned["removed_dead"] or pruned["pruned_bak"]:
            log(f"Startup cleanup: removed {pruned['removed_dead']} dead, "
                f"{pruned['pruned_bak']} excess backups")
    except Exception as e:
        log(f"Startup cleanup skipped: {e}", "WARN")

    stop_event = threading.Event()

    def stop_handler(signum, frame):
        log(f"Received signal {signum}, stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    STREAMS = {
        "dgx": 60, "gateway": 60, "local": 60, "heartbeat": 300,
        "nas": 900, "idle": 300, "workers": 60, "proxy": 60,
    }

    # The check_* fns are imported as locals above (not module globals), so map
    # them explicitly — globals().get("check_<name>") would return None and no
    # check thread would ever start.
    CHECKS = {
        "dgx": check_dgx, "gateway": check_gateway, "local": check_local,
        "heartbeat": check_heartbeat, "nas": check_nas,
        "idle": check_idle_monitor, "workers": check_workers,
        "proxy": check_proxy,
    }

    def run_periodic(check_fn, interval, stream_name):
        while not stop_event.is_set():
            try:
                check_fn()
            except Exception as e:
                log(f"Check {stream_name} error: {e}", "ERROR")
            stop_event.wait(interval)

    def run_watchdog_loop():
        while not stop_event.is_set():
            try:
                pipeline_watchdog()
            except Exception as e:
                log(f"Watchdog error: {e}", "ERROR")
            stop_event.wait(30)

    def run_trigger_loop():
        while not stop_event.is_set():
            try:
                trigger_engine(
                    check_dgx_fn=check_dgx,
                    check_gateway_fn=check_gateway,
                    pipeline_watchdog_fn=pipeline_watchdog,
                    watchdog_block_fn=load_watchdog_block,
                    restart_vllm_fn=restart_vllm,
                )
            except Exception as e:
                log(f"Trigger engine error: {e}", "ERROR")
            stop_event.wait(15)

    threads = []
    for stream_name, interval in STREAMS.items():
        check_fn = CHECKS.get(stream_name)
        if check_fn:
            t = threading.Thread(
                target=run_periodic,
                args=(check_fn, interval, stream_name),
                daemon=True,
            )
            t.start()
            threads.append(t)
            log(f"Started {stream_name} check thread (interval={interval}s)")

    wd = threading.Thread(target=run_watchdog_loop, daemon=True)
    wd.start()
    threads.append(wd)
    log("Started watchdog thread (interval=30s)")

    te = threading.Thread(target=run_trigger_loop, daemon=True)
    te.start()
    threads.append(te)
    log("Started trigger engine thread (interval=15s)")

    log("All threads started, daemon loop running (polling mode)")

    while not stop_event.is_set():
        stop_event.wait(1)

    log("Daemon loop stopped")
    write_stopped()
