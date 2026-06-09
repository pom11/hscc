"""CLI commands and entry point for the HSCC daemon."""

import json
import os
import signal
import subprocess
import sys
import threading
import time


def cmd_start():
    """Start the daemon in the background."""
    from .daemon_ops import get_pid, save_pid, write_stopped
    from . import log
    from .state import ensure_state_dir
    from .daemon_ops import run_daemon_loop

    existing_pid = get_pid()
    if existing_pid:
        print(f"Daemon already running (PID {existing_pid})")
        try:
            os.kill(existing_pid, 0)
            return
        except OSError:
            write_stopped()

    print("Starting hscc-daemon...")
    log("Daemon starting")

    # Fork into background
    pid = os.fork()
    if pid > 0:
        try:
            save_pid()
            print(f"hscc-daemon started (PID {pid})")
        except Exception:
            print(f"hscc-daemon started (child PID {pid})")
        return

    # Child — become daemon
    os.setsid()
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    os.chdir(os.path.expanduser("~"))

    # Re-fork so no controlling terminal
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — write PID and run
    save_pid()

    try:
        run_daemon_loop()
    except Exception as e:
        log(f"Daemon crashed: {e}", "ERROR")
        write_stopped()
        os._exit(1)


def _sigterm_handler(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    from .daemon_ops import write_stopped
    from . import log
    log(f"Received signal {signum}, shutting down...")
    write_stopped()
    os._exit(0)


def _run_event_driven_daemon(stop_event):
    """Event-driven daemon loop (placeholder — needs event_driven.py)."""
    pass


def cmd_stop():
    """Stop the daemon."""
    from .daemon_ops import get_pid, write_stopped
    from . import log

    pid = get_pid()
    if not pid:
        print("Daemon is not running")
        write_stopped()
        return

    print(f"Stopping hscc-daemon (PID {pid})...")
    log("Daemon stop requested")

    try:
        os.kill(pid, signal.SIGTERM)
        for i in range(10):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print(f"hscc-daemon stopped (PID {pid})")
                return
        os.kill(pid, signal.SIGKILL)
        print(f"hscc-daemon force-killed (PID {pid})")
    except ProcessLookupError:
        print("hscc-daemon already stopped")
    except Exception as e:
        print(f"Error stopping daemon: {e}")
    finally:
        write_stopped()


def cmd_status():
    """Show daemon status and last check results."""
    from .daemon_ops import get_pid
    from .state import read_all_states

    pid = get_pid()

    print("=" * 60)
    print("  HSCC Daemon Status")
    print("=" * 60)

    if pid:
        print(f"  Status:    RUNNING (PID {pid})")
        try:
            os.kill(pid, 0)
            print(f"  Process:   alive")
        except OSError:
            print(f"  Process:   stale PID file")
            pid = None
    else:
        print(f"  Status:    STOPPED")

    print()

    states = read_all_states()

    if not states:
        print("  No state data yet (no checks have run)")
        return

    print("  ── Check Streams ──────────────────────")
    print(f"  {'Stream':<12s} {'Status':<8s} {'Last Check':<22s} {'OK'}")
    print(f"  {'─'*12} {'─'*8} {'─'*22} {'─'*8}")

    for stream_name in ["dgx", "gateway", "local", "heartbeat", "nas", "watchdog", "triggers"]:
        state = states.get(stream_name)
        if not state:
            print(f"  {stream_name:<12s} {'—':<8s} {'never':<22s} —")
            continue

        ok = state.get("ok", state.get("blocked", "?"))
        ts = state.get("timestamp", "?")[:19]
        status_str = "BLOCKED" if state.get("blocked") else ("OK" if ok is True else "FAIL" if ok is False else str(ok))
        ok_str = "✓" if ok is True else ("🚨" if ok is False else "—")
        print(f"  {stream_name:<12s} {status_str:<8s} {ts:<22s} {ok_str}")

    print()

    wd_state = states.get("watchdog")
    if wd_state:
        print("  ── PipelineWatchdog ───────────────────")
        print(f"  Blocked:   {wd_state.get('blocked', False)}")
        if wd_state.get("blocked"):
            print(f"  Reason:    {wd_state.get('reason', '')}")
        print(f"  Restarts:  {wd_state.get('auto_restart_count', 0)}")
        print()

    tr_state = states.get("triggers")
    if tr_state:
        print("  ── Trigger Engine ─────────────────────")
        print(f"  Rules:     {tr_state.get('rules_evaluated', 0)}")
        print(f"  Actions:   {tr_state.get('actions_fired', 0)}")
        print()

    print("=" * 60)


def cmd_check(stream=None):
    """Run a single check cycle."""
    from .health import check_dgx, check_gateway, check_local, check_heartbeat, check_nas, check_idle_monitor, check_workers
    from .lifecycle import pipeline_watchdog
    from .trigger import trigger_engine
    from .state import read_state

    check_map = {
        "dgx": check_dgx, "gateway": check_gateway, "local": check_local,
        "heartbeat": check_heartbeat, "nas": check_nas,
        "watchdog": pipeline_watchdog, "triggers": trigger_engine,
        "idle": check_idle_monitor, "workers": check_workers,
    }

    if stream and stream == "all":
        results = {}
        for name, fn in check_map.items():
            print(f"Running {name}...")
            try:
                ok = fn()
                results[name] = ok
            except Exception as e:
                print(f"  Error: {e}")
                results[name] = False
        print()
        print("Results:")
        for name, ok in results.items():
            status = "OK" if ok else "FAIL"
            print(f"  {name:<12s} {status}")
        return

    if stream and stream in check_map:
        fn = check_map[stream]
        print(f"Running {stream} check...")
        try:
            ok = fn()
            state = read_state(stream)
            print(f"  Result: {'OK' if ok else 'FAIL'}")
            if state:
                msg = state.get("message", "")
                if msg:
                    print(f"  Detail: {msg}")
        except Exception as e:
            print(f"  Error: {e}")
        return

    print("Running DGX check...")
    try:
        ok = check_dgx()
        print(f"  Result: {'OK' if ok else 'FAIL'}")
    except Exception as e:
        print(f"  Error: {e}")


def cmd_watch(stream=None):
    """Tail check results in real-time."""
    from .daemon_ops import stream_watcher
    stream_watcher(stream)


def cmd_triggers():
    """Show trigger engine status."""
    from .trigger import load_triggers, load_cooldowns
    from .state import read_state

    rules = load_triggers()
    cooldowns = load_cooldowns()
    last_check = read_state("triggers")

    print("Trigger Engine Status")
    print(f"  Rules configured: {len(rules)}")
    print(f"  Cooldowns: {len(cooldowns)} active")
    print()

    if last_check:
        print(f"  Last run:  {last_check.get('timestamp', '?')[:19]}")
        print(f"  Rules eval: {last_check.get('rules_evaluated', 0)}")
        print(f"  Actions:   {last_check.get('actions_fired', 0)}")
    else:
        print("  No check results yet")
    print()

    if rules:
        print("  Rules:")
        for r in rules:
            rid = r.get("id", "?")
            enabled = "✓" if r.get("enabled", True) else "✗"
            cooldown = r.get("cooldown_seconds", 0)
            last = cooldowns.get(rid, "never")
            import datetime
            if isinstance(last, (int, float)):
                last = datetime.datetime.fromtimestamp(last).isoformat()[:19]
            else:
                last = str(last)[:19]
            print(f"    {enabled} {rid:<20s} cooldown={cooldown:>4s}s  last_fired={last}")
    else:
        print("  No rules configured.")


def cmd_notify(msg):
    """Send a manual notification."""
    from .desktop import send_macos_notification
    from .state import now_iso

    ts = now_iso()
    title = f"HSCC Manual: {ts[:19]}"
    print(f"Sending notification: {title}")
    ok = send_macos_notification(title, msg, priority="normal")
    print(f"  {'Sent' if ok else 'Failed'}")


def cmd_log():
    """Show daemon log output."""
    from .daemon_ops import get_daemon_log_tail
    lines = get_daemon_log_tail(50)
    if not lines:
        print("No daemon log entries.")
        return
    for line in lines:
        print(line.rstrip())


def cmd_start_daemon():
    """Internal entry point: run the daemon loop directly (used by launchd/systemd)."""
    from .daemon_ops import get_pid, save_pid, write_stopped, run_daemon_loop
    from . import log
    from .state import ensure_state_dir

    log("start-daemon invoked (service-supervised mode)")
    write_stopped()
    ensure_state_dir()
    save_pid()
    try:
        run_daemon_loop()
    except Exception as e:
        log(f"start-daemon crashed: {e}", "ERROR")
        write_stopped()
        raise


# Event-driven commands (placeholder)
def cmd_ed_status():
    """Show event-driven mode status."""
    print("Event-driven mode: not available (event_driven.py not found)")
    print("  Daemon will use polling fallback.")


def cmd_ed_install():
    """Install event-driven launchd jobs."""
    print("Event-driven mode: not available (event_driven.py not found)")


def cmd_ed_uninstall():
    """Remove event-driven launchd jobs."""
    print("Event-driven mode: not available (event_driven.py not found)")
