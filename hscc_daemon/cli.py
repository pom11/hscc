"""CLI commands and entry point for the HSCC daemon.

Command OUTPUT (a result the user asked for) is rendered through the shared
Rich theme module (``hscc_daemon.cli_theme``) so it matches the Hermes default
palette. Lines a background daemon writes to stdout during its live loop (the
``stream_watcher`` tail inside ``cmd_watch``, the ``log()`` calls inside
``cmd_start_daemon``) are NOT restyled — see the per-command notes.

``--json`` machine output is a hard invariant: any command whose output is
parsed by the daemon / scripts / ios console stays byte-identical. Rich only
decorates the human path, and Rich's Console auto-degrades to plain text (no
ANSI) when stdout is not a tty, which the no-ANSI regression test pins.
"""

import os
import signal
import time

from .cli_theme import make_console, make_panel, make_status_panel, make_table


def _console(**kwargs):
    """A themed Console; theme auto-detects (dark/light) at render time.

    Pass ``file=`` from tests to capture output; a non-tty ``file`` makes Rich
    degrade to plain text (no ANSI escapes), which the regression tests pin.
    """
    return make_console(**kwargs)


def cmd_start():
    """Start the daemon in the background."""
    from .daemon_ops import get_pid, save_pid, write_stopped
    from . import log
    from .state import ensure_state_dir
    from .daemon_ops import run_daemon_loop

    console = _console()

    existing_pid = get_pid()
    if existing_pid:
        try:
            os.kill(existing_pid, 0)
            console.print(
                make_status_panel(
                    f"Daemon already running (PID {existing_pid})",
                    status="warn", title="start",
                )
            )
            return
        except OSError:
            write_stopped()

    console.print("Starting hscc_daemon...")
    log("Daemon starting")

    # Fork into background
    pid = os.fork()
    if pid > 0:
        try:
            save_pid()
            console.print(
                make_status_panel(
                    f"hscc_daemon started (PID {pid})",
                    status="ok", title="start",
                )
            )
        except Exception:
            console.print(
                make_status_panel(
                    f"hscc_daemon started (child PID {pid})",
                    status="ok", title="start",
                )
            )
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

    console = _console()

    pid = get_pid()
    if not pid:
        console.print(
            make_status_panel(
                "Daemon is not running", status="warn", title="stop"
            )
        )
        write_stopped()
        return

    console.print(f"Stopping hscc_daemon (PID {pid})...")
    log("Daemon stop requested")

    try:
        os.kill(pid, signal.SIGTERM)
        for i in range(10):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                console.print(
                    make_status_panel(
                        f"hscc_daemon stopped (PID {pid})",
                        status="ok", title="stop",
                    )
                )
                return
        os.kill(pid, signal.SIGKILL)
        console.print(f"hscc_daemon force-killed (PID {pid})")
    except ProcessLookupError:
        console.print("hscc_daemon already stopped")
    except Exception as e:
        console.print(f"Error stopping daemon: {e}")
    finally:
        write_stopped()


# Streams shown in `hscc status`, in a stable order. Only streams with state on
# disk (or the ones the daemon knows about) are listed; "heartbeat"/"nas" etc.
# fall through to a "never" row when the daemon hasn't written them yet.
_STATUS_STREAMS = ["dgx", "gateway", "local", "heartbeat", "nas", "watchdog",
                   "triggers", "engine_wedge", "dispatcher"]


def _status_for(state):
    """Derive a human status string + ok sign from a stream state row."""
    ok = state.get("ok", state.get("blocked", "?"))
    if state.get("blocked"):
        return "BLOCKED", "▌"
    if ok is True:
        return "OK", "✓"
    if ok is False:
        return "FAIL", "✗"
    return str(ok), "—"


def cmd_status():
    """Show daemon status and last check results.

    RUNNING is reported ONLY when the durable ``daemon_liveness()`` signal
    confirms the daemon is genuinely alive: the pid file exists AND that pid
    is a live process AND the heartbeat is fresh. If the pid is gone OR the
    heartbeat is stale, we report NOT-RUNNING — never "RUNNING alive" — and
    surface the unexpected-exit so the operator can see a dead daemon at a
    glance rather than being told it is alive.
    """
    from .daemon_ops import daemon_liveness
    from .state import read_all_states

    console = _console()

    liv = daemon_liveness()
    liv_state = liv["state"]

    # Keep the panel line concise: panels clip a single long line at the
    # terminal width, and the unexpected-exit/pid detail must NOT be lost to
    # that clipping. We print the short status in the panel, then surface any
    # dead/stale detail as its own distinct wrapped line below it.
    detail = None
    if liv_state == "running-fresh":
        # Genuinely alive: pid present + alive + fresh heartbeat.
        status, note = "RUNNING", f"alive (PID {liv['pid']})"
    elif liv_state == "running-stale-heartbeat":
        # Process present but the durable heartbeat stopped advancing — the
        # operator's dead-daemon case. Surface it loudly, never "RUNNING".
        status, note = "STOPPED", "stale heartbeat"
        detail = (f"possible unexpected exit / stale heartbeat — Heartbeat "
                  f"last advanced {liv['last_heartbeat']} (older than "
                  f"HEARTBEAT_STALE_AFTER); PID {liv['pid']} still present but "
                  "not supervised, treat as dead daemon.")
    elif liv_state == "running-no-heartbeat":
        # Pid alive but no heartbeat written yet — the pid file alone cannot
        # durably confirm the daemon, so report NOT-RUNNING (errs safe).
        status = "STOPPED"
        note = f"pid alive but no durable heartbeat (PID {liv['pid']})"
        detail = "The pid file exists but no heartbeat has ever been written — " \
                 "cannot confirm the daemon is alive; treat as not running."
    else:  # pid-gone
        # pid file absent ⇒ clean stop; pid present but not alive ⇒ stale PID file.
        if liv["pid_file_present"]:
            status, note = "STOPPED", "stale PID file"
            detail = (f"PID file names {liv['pid']} but that process is not "
                      "alive — possible unexpected exit; pid file cleaned up.")
        else:
            status, note = "STOPPED", ""

    state_role = "ok" if status == "RUNNING" else "warn"
    console.print(
        make_status_panel(
            f"HSCC Daemon Status — {status} {note}".strip(),
            status=state_role, title="status",
        )
    )
    if detail:
        # The operator's glance-line for a dead daemon: a distinct wrapped
        # line that cannot be clipped like a single-line panel body.
        console.print(f"[warn]  ! {detail}[/warn]")
        console.print()

    states = read_all_states()

    if not states:
        console.print("  No state data yet (no checks have run)")
        return

    stream_any = any(name in states for name in _STATUS_STREAMS)
    if stream_any:
        table = make_table("Check Streams")
        table.box = None  # borderless: row lines lead with the stream name
        table.add_column("Stream", justify="left")
        table.add_column("Status", justify="left")
        table.add_column("Last Check", justify="left")
        table.add_column(" ", justify="left")
        for stream_name in _STATUS_STREAMS:
            state = states.get(stream_name)
            if not state:
                table.add_row(stream_name, "—", "never", "—")
                continue
            ts = state.get("timestamp", "?")[:19]
            status_str, ok_char = _status_for(state)
            if status_str == "FAIL":
                row_status = f"[error]{status_str}[/error]"
            elif status_str == "BLOCKED":
                row_status = f"[warn]{status_str}[/warn]"
            else:
                row_status = status_str
            table.add_row(stream_name, row_status, ts, ok_char)
        console.print(table)
        console.print()

    wd_state = states.get("watchdog")
    if wd_state:
        console.print(make_panel(
            "watchdog",
            f"Blocked:  {wd_state.get('blocked', False)}\n"
            f"Reason:   {wd_state.get('reason', '')}\n"
            f"Restarts: {wd_state.get('auto_restart_count', 0)}",
        ))
        console.print()

    tr_state = states.get("triggers")
    if tr_state:
        console.print(make_panel(
            "Trigger Engine",
            f"Rules:   {tr_state.get('rules_evaluated', 0)}\n"
            f"Actions: {tr_state.get('actions_fired', 0)}",
        ))


def cmd_check(stream=None):
    """Run a single check cycle.

    Ad-hoc (from the terminal): prints the result only and NEVER writes the
    shared stream-state files the daemon owns. If a CLI-side check fails where
    the daemon succeeds (TCC, off-LAN laptop, a transient blip), `hscc status`
    must keep reporting what the daemon observes — a manual failure must not
    masquerade as a fleet failure.
    """
    from .state import persist_disabled

    with persist_disabled():
        return _cmd_check_impl(stream)


def _cmd_check_impl(stream=None):
    """The actual check cycle; runs under persist_disabled()."""
    from .health import check_dgx, check_gateway, check_local, check_heartbeat, check_nas, check_idle_monitor, check_workers, check_engine_wedge
    from .dispatcher_wedge import check_dispatcher_wedge
    from .lifecycle import pipeline_watchdog
    from .trigger import trigger_engine
    from .state import read_state

    console = _console()

    check_map = {
        "dgx": check_dgx, "gateway": check_gateway, "local": check_local,
        "heartbeat": check_heartbeat, "nas": check_nas,
        "watchdog": pipeline_watchdog, "triggers": trigger_engine,
        "idle": check_idle_monitor, "workers": check_workers,
        "engine_wedge": check_engine_wedge,
        "dispatcher": check_dispatcher_wedge,
    }

    if stream and stream == "all":
        results = {}
        for name, fn in check_map.items():
            console.print(f"Running {name}...")
            try:
                ok = fn()
                results[name] = ok
            except Exception as e:
                console.print(f"  Error: {e}")
                results[name] = False
        console.print()
        table = make_table("Results")
        table.add_column("Check", justify="left")
        table.add_column("Status", justify="left")
        for name, ok in results.items():
            status = ("[ok]OK[/ok]" if ok
                      else "[error]FAIL[/error]")
            table.add_row(name, status)
        console.print(table)
        return

    if stream and stream in check_map:
        fn = check_map[stream]
        console.print(f"Running {stream} check...")
        try:
            ok = fn()
            state = read_state(stream)
            console.print(f"  Result: {'[ok]OK[/ok]' if ok else '[error]FAIL[/error]'}")
            if state:
                msg = state.get("message", "")
                if msg:
                    console.print(f"  Detail: {msg}")
        except Exception as e:
            console.print(f"  Error: {e}")
        return

    console.print("Running DGX check...")
    try:
        ok = check_dgx()
        console.print(f"  Result: {'[ok]OK[/ok]' if ok else '[error]FAIL[/error]'}")
    except Exception as e:
        console.print(f"  Error: {e}")


def cmd_watch(stream=None):
    """Tail check results in real-time.

    This delegates to ``daemon_ops.stream_watcher``, a live infinite-loop
    streaming tail whose output (including its own opening banner at
    daemon_ops.py:85) is background streaming — NOT restyled, per the shared
    design rule. Restyling here would duplicate the banner stream_watcher
    already prints. Left as a thin passthrough.
    """
    from .daemon_ops import stream_watcher
    stream_watcher(stream)


def cmd_triggers():
    """Show trigger engine status."""
    from .trigger import load_triggers, load_cooldowns
    from .state import read_state
    import datetime

    console = _console()

    rules = load_triggers()
    cooldowns = load_cooldowns()
    last_check = read_state("triggers")

    body = (
        f"Rules configured: {len(rules)}\n"
        f"Cooldowns: {len(cooldowns)} active"
    )
    if last_check:
        body += (
            f"\nLast run:   {last_check.get('timestamp', '?')[:19]}\n"
            f"Rules eval: {last_check.get('rules_evaluated', 0)}\n"
            f"Actions:    {last_check.get('actions_fired', 0)}"
        )
    else:
        body += "\nLast run:   no check results yet"

    console.print(make_panel("Trigger Engine Status", body))
    console.print()

    if rules:
        table = make_table("Configured Rules")
        table.add_column("", justify="left")  # enabled marker
        table.add_column("ID", justify="left")
        table.add_column("Cooldown", justify="right")
        table.add_column("Last fired", justify="left")
        for r in rules:
            rid = r.get("id", "?")
            enabled = "[ok]✓[/ok]" if r.get("enabled", True) else "✗"
            cooldown = r.get("cooldown_seconds", 0)
            last = cooldowns.get(rid, "never")
            if isinstance(last, (int, float)):
                last = datetime.datetime.fromtimestamp(last).isoformat()[:19]
            else:
                last = str(last)[:19]
            table.add_row(enabled, rid, f"{cooldown}s", last)
        console.print(table)
    else:
        console.print(make_status_panel(
            "No rules configured.", status="warn", title="triggers"
        ))


def cmd_notify(msg):
    """Send a manual notification."""
    from .desktop import send_macos_notification
    from .state import now_iso

    console = _console()

    ts = now_iso()
    title = f"HSCC Manual: {ts[:19]}"
    console.print(f"Sending notification: {title}")
    ok = send_macos_notification(title, msg, priority="normal")
    console.print(make_status_panel(
        f"Notification {'sent' if ok else 'failed'}",
        status="ok" if ok else "error", title="notify",
    ))


def cmd_log():
    """Show daemon log output."""
    from .daemon_ops import get_daemon_log_tail

    console = _console()

    lines = get_daemon_log_tail(50)
    if not lines:
        console.print(
            make_status_panel(
                "No daemon log entries.", status="warn", title="log"
            )
        )
        return
    console.print(make_panel(
        "Daemon Log (last 50 lines)",
        "\n".join(line.rstrip() for line in lines),
    ))


def cmd_start_daemon():
    """Internal entry point: run the daemon loop directly (used by launchd/systemd).

    This is the supervised daemon entry point. Its only output is the
    daemon's own ``log()`` background-level lines — NOT command output the
    user asked for — so it is intentionally NOT restyled.
    """
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
    """Show event-driven mode status.

    Placeholder — event_driven.py is not present, so there is no real command
    surface to render. Left un-styled on purpose (see card decision).
    """
    print("Event-driven mode: not available (event_driven.py not found)")
    print("  Daemon will use polling fallback.")


def cmd_ed_install():
    """Install event-driven launchd jobs.

    Placeholder — event_driven.py is not present. Left un-styled on purpose.
    """
    print("Event-driven mode: not available (event_driven.py not found)")


def cmd_ed_uninstall():
    """Remove event-driven launchd jobs.

    Placeholder — event_driven.py is not present. Left un-styled on purpose.
    """
    print("Event-driven mode: not available (event_driven.py not found)")
