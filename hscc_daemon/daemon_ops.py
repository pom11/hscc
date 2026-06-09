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


def get_pid():
    """Read PID from file, return None if not running."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None
    except (FileNotFoundError, ValueError):
        return None


def save_pid():
    """Write current PID to file."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def write_stopped():
    """Remove PID file."""
    try:
        os.remove(PID_FILE)
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


def log(msg, level="INFO"):
    """Write a timestamped log line to the daemon log file."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[{ts}] [{level:>5s}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except IOError:
        pass
    # Also print if daemon is running in foreground mode
    if not os.path.exists(PID_FILE):
        print(line)


def run_daemon_loop():
    """Main daemon event loop (polling mode fallback)."""
    from .health import check_dgx, check_gateway, check_local, check_heartbeat, check_nas, check_idle_monitor, check_workers
    from .trigger import trigger_engine
    from .lifecycle import pipeline_watchdog
    from .state import now_iso, write_state

    ensure_state_dir()
    log("Daemon loop started")

    stop_event = threading.Event()

    def stop_handler(signum, frame):
        log(f"Received signal {signum}, stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    STREAMS = {
        "dgx": 60, "gateway": 60, "local": 60, "heartbeat": 300,
        "nas": 900, "idle": 300, "workers": 60,
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
                trigger_engine()
            except Exception as e:
                log(f"Trigger engine error: {e}", "ERROR")
            stop_event.wait(15)

    threads = []
    for stream_name, interval in STREAMS.items():
        check_fn = globals().get(f"check_{stream_name}")
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
