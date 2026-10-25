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

import json
import os
import signal
import sys
import time

from .cli_theme import (
    esc,
    make_console,
    make_panel,
    make_status_panel,
    make_table,
)


def _console(**kwargs):
    """A themed Console; theme auto-detects (dark/light) at render time.

    Pass ``file=`` from tests to capture output; a non-tty ``file`` makes Rich
    degrade to plain text (no ANSI escapes), which the regression tests pin.
    """
    return make_console(**kwargs)


def _detach_stdio():
    """Point stdin/stdout/stderr at /dev/null in the daemonised child (F6).

    WHY: ``cmd_start`` double-forks but never redirected the standard streams,
    so the daemon inherited the caller's terminal. Two consequences, both real:

      1. the daemon (and any sparkrun/docker child it spawns) could write into
         the operator's terminal whenever it liked;
      2. the daemon HELD the caller's stdout open. A caller that waits for its
         stdout to close — ``hscc start | cat``, a tool capturing output, an
         editor's shell integration — blocked forever even though ``hscc start``
         had already printed its panel and returned.

    Redirecting to DEVNULL rather than ``os.close(1)``: closing fd 1 leaves the
    descriptor free, and the next ``open()`` anywhere in the daemon silently
    takes fd 1 — after which its file content is written into the caller's pipe,
    which is strictly worse than today. A device keeps fd 1 valid and inert.

    Best-effort by design: this runs after fork and before the loop, and a
    failure here must not kill a daemon that is otherwise fine, so every step is
    guarded and any error is swallowed (logged if logging still works).
    """
    try:
        fd = os.open(os.devnull, os.O_RDWR)
    except OSError:
        try:
            f = open(os.devnull, "r+")
        except OSError:
            return
        try:
            for std in (0, 1, 2):
                os.dup2(f.fileno(), std)
        except OSError:
            pass
        finally:
            try:
                f.close()
            except OSError:
                pass
        return
    try:
        for std in (0, 1, 2):
            os.dup2(fd, std)
    except OSError:
        pass
    finally:
        if fd > 2:
            try:
                os.close(fd)
            except OSError:
                pass


# Upper bound on how long the `hscc start` parent waits for the grandchild's
# single-instance verdict (t_52bd498e). Normal path: verdict arrives in ms.
# The bound only matters when the grandchild dies before writing one — and
# it must EXCEED the contender's own wait (daemon_singleton.
# CONTEND_WAIT_SECONDS=30) plus slack, so a refusing contender still gets
# its verdict reported instead of the parent timing out into the classic
# "started" panel. conftest pins it to 0 for fork-emulating tests.
_START_VERDICT_TIMEOUT = 40.0


def _read_start_verdict(rfd, timeout=None):
    """Read the grandchild's single-instance verdict from the pipe, at most
    ``timeout`` seconds. Returns the verdict string or None (timeout / EOF /
    any error ⇒ None, and the caller falls back to the classic panel).

    Bounded by contract: `hscc start` must never block on a daemon it already
    detached from (the F6 lesson), so this is a select() + single read, not a
    blocking readline. The verdict is tiny (< 4 KB) so one read suffices.
    ``_START_VERDICT_TIMEOUT`` is a module constant precisely so the test
    suite can pin it to 0 (conftest): fork-emulating tests must never sit in
    a real select(), while production waits the full window so a contending
    grandchild (which itself waits up to CONTEND_WAIT_SECONDS) still gets
    its verdict reported.
    """
    import select
    if timeout is None:
        timeout = _START_VERDICT_TIMEOUT
    try:
        ready, _, _ = select.select([rfd], [], [], timeout)
        if not ready:
            return None
        data = os.read(rfd, 4096)
        return data.decode("utf-8", "replace") or None
    except (OSError, ValueError):
        return None


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

    # Verdict pipe (t_52bd498e): the grandchild now claims the single-instance
    # lock BEFORE serving, and its claim verdict ("ok" / "refused\t<why>") is
    # written here so the parent can report the TRUTH instead of always
    # printing "started". The parent reads with a bounded select so a
    # verdict-less daemon (old crash window, pipe write failure) still gets
    # the classic success panel — behaviour only gets more honest, never
    # slower or blocking. Created BEFORE the first fork so both children
    # inherit both ends; every branch closes what it does not use.
    try:
        _rfd, _wfd = os.pipe()
    except OSError:
        _rfd = _wfd = None

    # Fork into background
    pid = os.fork()
    if pid > 0:
        # Parent: the children hold their own inherited write end, so ours is
        # closed immediately — if the grandchild dies before writing, the
        # parent sees EOF (→ None → classic panel) instead of waiting out the
        # full bound, and no fd leaks per start invocation.
        if _wfd is not None:
            try:
                os.close(_wfd)
            except OSError:
                pass
        verdict = _read_start_verdict(_rfd) if _rfd is not None else None
        if _rfd is not None:
            try:
                os.close(_rfd)
            except OSError:
                pass
        if verdict and verdict.startswith("refused"):
            reason = verdict.split("\t", 1)[1] if "\t" in verdict else "unknown"
            # Rich markup: reason is our own text but can carry paths — esc().
            console.print(
                make_status_panel(
                    f"start refused — {esc(reason)}",
                    status="error", title="start",
                )
            )
            return
        started_pid = pid
        if verdict and verdict.startswith("ok"):
            _rest = verdict.split(None, 1)
            if len(_rest) > 1 and _rest[1].strip().isdigit():
                started_pid = int(_rest[1].strip())  # the real daemon pid
        try:
            console.print(
                make_status_panel(
                    f"hscc_daemon started (PID {started_pid})",
                    status="ok", title="start",
                )
            )
        except Exception:
            console.print(
                make_status_panel(
                    f"hscc_daemon started (child PID {started_pid})",
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
        if _rfd is not None:
            try:
                os.close(_rfd)
            except OSError:
                pass
        if _wfd is not None:
            try:
                os.close(_wfd)
            except OSError:
                pass
        os._exit(0)

    # Grandchild. Detach the standard streams BEFORE anything else runs: this
    # is the point after which nobody owns the caller's terminal any more, and
    # leaving fd 0/1/2 attached is what made `hscc start` hold the caller's
    # pipe open (F6) and let the daemon write into the operator's terminal.
    # The verdict pipe fds (>=3) are deliberately NOT detached fds and stay
    # valid across the dup2s.
    _detach_stdio()
    if _rfd is not None:
        try:
            os.close(_rfd)
        except OSError:
            pass

    # SINGLE-INSTANCE CLAIM (t_52bd498e) BEFORE any pid-file bookkeeping.
    # This grandchild is the process that would actually serve, so it is the
    # process that must own the claim — and it must own it BEFORE save_pid():
    # the 2026-10 twin incident began exactly here (an orphaned fork whose
    # save_pid was later clobbered by launchd's write_stopped+save_pid, while
    # the orphan kept serving). If the claim is refused a live loop already
    # holds supervision: exit 3 loudly WITHOUT touching the pid file (the
    # holder's label stays intact and `hscc status` keeps telling the truth).
    # run_daemon_loop re-asserts the same idempotent claim as its unbypassable
    # backstop, so no future caller can skip this by invoking the loop
    # directly. Claiming only AFTER the second fork is deliberate: a claim
    # taken by the parent/first child would record a pid that is microseconds
    # from death and open a break-the-lock race against a concurrent start.
    from . import daemon_singleton as _singleton
    ok, why = _singleton.hold_daemon_loop_lock_fail_open()
    _verdict = (f"ok {os.getpid()}" if ok else f"refused\t{why}")
    if _wfd is not None:
        try:
            os.write(_wfd, _verdict.encode("utf-8", "replace"))
        except OSError:
            pass
        finally:
            try:
                os.close(_wfd)
            except OSError:
                pass
    if not ok:
        log(f"hscc start refused: {why}", "ERROR")
        os._exit(3)
    if why:
        log(f"hscc start single-instance WARN: {why}", "WARN")

    # Grandchild — write the PID exactly ONCE here, after the final fork, so
    # ~/.hscc/daemon.pid reflects the ONE process that actually serves (the
    # grandchild). The parent branch above does NOT save_pid(): writing it
    # there too makes the file a race between the short-lived first child and
    # the long-lived grandchild, and whichever wins may point at the wrong
    # (already-exited) process — the stale-pid class of bug that made
    # `hscc status` / `hscc stop` trust a dead pid. The grandchild is the
    # single owner, matching cmd_start_daemon (the launchd path), so start,
    # stop, status and launchd agree on one ownership model.
    save_pid()

    try:
        run_daemon_loop()
    except SystemExit as e:
        # Control flow (a refusal from the loop's own backstop claim, or a
        # deliberate exit) — not a crash we should paper over. We never
        # became the holder in the refusal case, and release_daemon_lock is
        # ownership-gated anyway.
        try:
            from . import daemon_singleton as _s
            _s.release_daemon_lock()
        except Exception:
            pass
        os._exit(e.code if isinstance(e.code, int) else 1)
    except Exception as e:
        log(f"Daemon crashed: {e}", "ERROR")
        write_stopped()
        try:
            from . import daemon_singleton as _s
            _s.release_daemon_lock()
        except Exception:
            pass
        os._exit(1)


def _sigterm_handler(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    from .daemon_ops import write_stopped
    from . import log
    log(f"Received signal {signum}, shutting down...")
    write_stopped()
    # The claim (t_52bd498e) is ownership-gated and this handler can fire
    # BEFORE the grandchild took the claim (signal between forks) — release
    # is a no-op then, and correct when we already hold it.
    try:
        from . import daemon_singleton as _s
        _s.release_daemon_lock()
    except Exception:
        pass
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
        # Exception text is DATA (an OSError message can carry a path with
        # arbitrary brackets) — escape before it reaches Rich markup.
        console.print(f"Error stopping daemon: {esc(e)}")
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
        status, note = "RUNNING", f"alive (PID {esc(liv['pid'])})"
    elif liv_state == "running-stale-heartbeat":
        # Process present but the durable heartbeat stopped advancing — the
        # operator's dead-daemon case. Surface it loudly, never "RUNNING".
        status, note = "STOPPED", "stale heartbeat"
        detail = (f"possible unexpected exit / stale heartbeat — Heartbeat "
                  f"last advanced {esc(liv['last_heartbeat'])} (older than "
                  f"HEARTBEAT_STALE_AFTER); PID {esc(liv['pid'])} still present but "
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

    # TWIN-LOOP SURFACE (t_52bd498e): the 2026-10 incident was TWO full
    # daemon loops serving side-by-side for 2+ days while status cheerfully
    # reported the pid-file owner. The single-instance lock now refuses a
    # twin at startup, but status still reports one (an old build's fork, a
    # manual loop bypassing the CLI) whenever the process scan finds >1
    # loop-entry-point process. This line is a REPORT, never an action:
    # status must not be able to kill anything.
    try:
        from . import daemon_singleton
        inst = daemon_singleton.daemon_instance_report()
        if inst.get("twin"):
            console.print(
                f"[error]  ! TWIN LOOP DETECTED: {esc(inst['loop_processes'])} "
                "daemon-loop processes are running (expected 1). The lock "
                f"holder is '{esc(inst['holder'] or 'unknown')}'. Inspect the "
                "pids (`ps -eo pid,ppid,lstart,command | grep hscc`), verify "
                "which owns the pid file, then SIGKILL the orphan (NOT the "
                "pid-file/launchd owner — a TERM'd twin deletes the holder's "
                "pid file via write_stopped).[/error]")
            console.print()
        elif inst.get("lock_stale_claim"):
            console.print(
                "[dim]  note: a stale daemon single-instance lock was found "
                "(holder dead); the next daemon start breaks it "
                "automatically.[/dim]")
    except Exception:
        # Same silence contract as _print_update_notice: a broken diagnostic
        # must never take the status surface down.
        pass

    # Update notice BEFORE the stream table (and before the "no state data"
    # early return below): a fresh machine whose daemon has written no state
    # yet should still see "a newer release is out", and the notice is the
    # one line on this surface that does not depend on stream state at all.
    _print_update_notice(console)

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
            # stream_name / timestamp come from files on disk — DATA.
            table.add_row(esc(stream_name), row_status, esc(ts), esc(ok_char))
        console.print(table)
        console.print()

    wd_state = states.get("watchdog")
    if wd_state:
        # ``reason`` is the watchdog's own text — it can quote a log line, a
        # path, or tool output, so it is DATA and must never be markup-parsed.
        console.print(make_panel(
            "watchdog",
            f"Blocked:  {esc(wd_state.get('blocked', False))}\n"
            f"Reason:   {esc(wd_state.get('reason', ''))}\n"
            f"Restarts: {esc(wd_state.get('auto_restart_count', 0))}",
        ))
        console.print()

    tr_state = states.get("triggers")
    if tr_state:
        console.print(make_panel(
            "Trigger Engine",
            f"Rules:   {tr_state.get('rules_evaluated', 0)}\n"
            f"Actions: {tr_state.get('actions_fired', 0)}",
        ))


def _print_update_notice(console):
    """One dim INFO line when a newer release is cached (never anything else).

    Ambient surface (card t_a4055572): reads ``~/.hscc/update-check.json``
    ONLY — status must never block on the network or make status fail — and
    prints only when the cache says newer AND is inside the 24h window
    (spec: "status with stale cache (>24h) prints nothing"). Everything this
    could get wrong (no cache, corrupt cache, old cache, bad version strings,
    an unexpected exception in the module) lands in the same place: silence.
    Status' exit code and every pre-existing line are untouched (pinned by
    the byte-comparison tests in test_update_check.py).
    """
    try:
        from . import update_check
        notice = update_check.check_from_cache()
        if notice:
            console.print(f"[dim]update:[/dim] HSCC "
                          f"{esc(notice['latest_tag'])} available "
                          f"(installed {esc(notice['installed'])}); "
                          f"upgrade: {esc(notice['upgrade_cmd'])}")
    except Exception:
        # Quiet by contract. A broken update notice must never take the
        # status surface down — the daemon health table is what operators
        # read here; the update line is decoration at most.
        pass


def _extract_repo_flag(argv):
    """Pull ``--repo <path>`` / ``--repo=<path>`` and ``--json`` out of argv.

    Returns ``(repo_or_None, has_repo_flag, json_mode, rest)``. ``--repo`` with
    no value is an error the caller surfaces — silently defaulting to cwd would
    hide a typo in the one command whose whole point is the posture HERE.
    """
    repo = None
    has_repo = False
    json_mode = False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            json_mode = True
            i += 1
            continue
        if a == "--repo":
            has_repo = True
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                repo = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--repo="):
            has_repo = True
            repo = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return repo, has_repo, json_mode, rest


def cmd_check(stream=None, *rest):
    """Run a single check cycle — or, with ``--repo``, the address-guard posture.

    Ad-hoc (from the terminal): prints the result only and NEVER writes the
    shared stream-state files the daemon owns. If a CLI-side check fails where
    the daemon succeeds (TCC, off-LAN laptop, a transient blip), `hscc status`
    must keep reporting what the daemon observes — a manual failure must not
    masquerade as a fleet failure.

    ``hscc check --repo <path>`` (t_5abdb13d): reports the commit-time address
    guard's TRUE posture for that checkout by calling ``posture()`` from the
    shipped ``hscc-bootstrap/install_hooks.py`` (via :mod:`hscc_daemon.guard_posture`
    — no logic duplicated here). Default path is the cwd's checkout. Exit code
    is 0 only for ``armed``; ``unarmed``, ``armed-but-absent`` (the fail-open
    line the operator asked to see: config advertises protection, this checkout
    resolves no runnable hook) and ``not-a-repo`` are all non-zero so
    scripts/cron can key off it. ``--json`` carries the posture dict verbatim.
    """
    from .state import persist_disabled

    argv = ([stream] if stream is not None else []) + [a for a in rest]
    repo, has_repo, json_mode, leftover = _extract_repo_flag(argv)
    if has_repo:
        with persist_disabled():
            return _cmd_check_repo(repo, json_mode, leftover)

    with persist_disabled():
        return _cmd_check_impl(stream)


def _cmd_check_repo(repo, json_mode, leftover):
    """Render the guard posture for one checkout; exits per the state contract.

    Kept inside persist_disabled() by the caller: this path must not touch the
    daemon's stream state any more than the other ad-hoc check paths do.
    """
    from . import guard_posture

    console = _console()
    if leftover:
        # A stream name alongside --repo is ambiguous — refusing beats
        # quietly dropping one of the two intents.
        console.print(f"[error]check --repo does not take a stream "
                      f"(got: {esc(' '.join(leftover))})[/error]")
        sys.exit(2)
    try:
        p = guard_posture.posture(repo)
    except guard_posture.GuardSourceError as exc:
        # Fail closed: a status line that cannot SEE the guard must not
        # advertise that it can. Non-zero, loudly.
        if json_mode:
            print(json.dumps({"state": "error", "detail": str(exc),
                              "repo": repo or os.getcwd()}))
        else:
            # exc text embeds candidate paths (operator-controlled); escape
            # the DATA, keep the [error] style the renderer chose.
            console.print(f"[error]guard posture UNVERIFIED — {esc(exc)}[/error]")
            console.print("[dim]next step: run hscc-bootstrap/bootstrap.sh "
                          "(installs hscc-bootstrap/) so the single-implementation "
                          "posture() is available[/dim]")
        sys.exit(1)

    if json_mode:
        # Machine contract: the dict EXACTLY as posture() produced it —
        # plain print, never Rich (same rule as `hscc verify --json`).
        print(json.dumps(p))
    else:
        role = guard_posture.status_role_for(p.get("state"))
        # The table title is the checkout path — user input. make_table escapes
        # titles; cell VALUES are not escaped by Rich, so every value below is.
        table = make_table(f"address-guard posture — {p.get('toplevel') or repo or os.getcwd()}")
        table.add_column("field", no_wrap=True)
        table.add_column("value")
        table.add_row("state", f"[{role}]{esc(p.get('state'))}[/]")
        table.add_row("core.hooksPath", esc(p.get("core_hooks_path")))
        table.add_row("hook present", "yes" if p.get("hook_present") else "no")
        table.add_row("hook executable", "yes" if p.get("hook_executable") else "no")
        table.add_row("detector present", "yes" if p.get("guard_present") else "no")
        table.add_row("detail", esc(p.get("detail")))
        console.print(table)
        hint = guard_posture.next_step_for(p)
        if hint:
            # Keep the exact "next step: <hint>" substring (automation greps it).
            console.print(f"[dim]next step: {esc(hint)}[/dim]")
    sys.exit(guard_posture.exit_code_for(p))


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
                # A check's exception text can quote a path, a host, or tool
                # output — DATA, never markup.
                console.print(f"  Error: {esc(e)}")
                results[name] = False
        console.print()
        table = make_table("Results")
        table.add_column("Check", justify="left")
        table.add_column("Status", justify="left")
        for name, ok in results.items():
            status = ("[ok]OK[/ok]" if ok
                      else "[error]FAIL[/error]")
            table.add_row(esc(name), status)
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
                    # The message is written by the check itself and commonly
                    # quotes command output — escape it.
                    console.print(f"  Detail: {esc(msg)}")
        except Exception as e:
            console.print(f"  Error: {esc(e)}")
        return

    console.print("Running DGX check...")
    try:
        ok = check_dgx()
        console.print(f"  Result: {'[ok]OK[/ok]' if ok else '[error]FAIL[/error]'}")
    except Exception as e:
        console.print(f"  Error: {esc(e)}")


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

    console.print(make_panel("Trigger Engine Status", esc(body)))
    console.print()

    if rules:
        table = make_table("Configured Rules")
        table.add_column("", justify="left")  # enabled marker
        table.add_column("ID", justify="left")
        table.add_column("Cooldown", justify="right")
        table.add_column("Last fired", justify="left")
        for r in rules:
            # Rule ids + cooldown strings come from triggers.json — edited by
            # hand and by the trigger API, so both are DATA.
            rid = esc(r.get("id", "?"))
            enabled = "[ok]✓[/ok]" if r.get("enabled", True) else "✗"
            cooldown = r.get("cooldown_seconds", 0)
            last = cooldowns.get(r.get("id", "?"), "never")
            if isinstance(last, (int, float)):
                last = datetime.datetime.fromtimestamp(last).isoformat()[:19]
            else:
                last = str(last)[:19]
            table.add_row(enabled, rid, esc(f"{cooldown}s"), esc(last))
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


def cmd_heal(node=None, reason="", status_only=False):
    """Inspect or clear the per-node heal circuit breaker (F5).

    The breaker's alert tells the operator to run this after investigating, so
    it must exist — an alert pointing at a command that does not exist is worse
    than no alert. Default (no --status) CLEARS the node's strikes.
    """
    from . import heal_breaker
    console = _console()

    if status_only:
        rows = []
        for k, v in sorted(heal_breaker._load().items()):
            if k.startswith("_") or not isinstance(v, list):
                continue
            remaining = heal_breaker.suspended(k)
            rows.append(f"  {k:<16} {len(v)} action(s) in window  "
                        + (f"SUSPENDED {int(remaining)}s more" if remaining > 0
                           else "healing allowed"))
        console.print(make_status_panel(
            "\n".join(rows) if rows
            else "No heal strikes recorded — all nodes healing normally.",
            status="ok", title="heal breaker"))
        return

    # Second line of defence (B4, card t_aeb982bc) — same shape as the
    # engine-side cmd_stop guard (B6, t_875c5019): the unified CLI intercepts
    # --help before dispatch, but a DIRECT caller (library import) used to be
    # able to land here with "--help" as the node name and CLEAR it (audit
    # EXEC_ECHO: "No strikes recorded for --help"). A help token can never
    # name a node, so it prints usage and touches no state.
    if node in ("-h", "--help"):
        console.print("Usage: hscc heal-clear <node> [reason...]   "
                      "(or: hscc heal-status)")
        return

    if not node:
        console.print("Usage: hscc heal-clear <node> [reason...]   "
                      "(or: hscc heal-status)")
        return
    cleared = heal_breaker.clear(node, reason or "cleared by operator")
    # A clear must un-silence the node too, so a later trip can alert again.
    heal_breaker.reset_alert(node)
    console.print(make_status_panel(
        (f"Cleared heal strikes for {node} — healing resumes now."
         if cleared else
         f"No strikes recorded for {node} (nothing to clear)."),
        status="ok" if cleared else "warn", title="heal breaker"))


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
    # Log lines are the daemon's own text — they quote tool output, paths and
    # subprocess errors, i.e. arbitrary brackets. Escape EVERY line before it
    # goes into the panel body (make_panel markup-parses its renderable).
    console.print(make_panel(
        "Daemon Log (last 50 lines)",
        "\n".join(esc(line.rstrip()) for line in lines),
    ))


def cmd_start_daemon():
    """Internal entry point: run the daemon loop directly (used by launchd/systemd).

    This is the supervised daemon entry point. Its only output is the
    daemon's own ``log()`` background-level lines — NOT command output the
    user asked for — so it is intentionally NOT restyled.

    SINGLE-INSTANCE CLAIM (t_52bd498e) FIRST, before ``write_stopped()``: the
    twin incident happened because this path re-labelled supervision as its
    own (write_stopped + save_pid) while a pre-existing loop kept serving —
    the pid file is a label, so overwriting it reaped nothing. A contender
    that finds a LIVE loop holding the claim exits 3 WITHOUT touching the pid
    file (the holder's label and log stay authoritative), and launchd
    KeepAlive (SuccessfulExit=false) retries until the holder releases. The
    refusal is logged loudly on every retry; run_daemon_loop re-asserts the
    same idempotent claim as the unbypassable backstop.
    """
    from .daemon_ops import get_pid, save_pid, write_stopped, run_daemon_loop
    from . import daemon_singleton, log
    from .state import ensure_state_dir

    log("start-daemon invoked (service-supervised mode)")
    ok, why = daemon_singleton.hold_daemon_loop_lock_fail_open()
    if not ok:
        log(f"start-daemon refused: {why}", "ERROR")
        raise SystemExit(3)
    if why:
        log(f"start-daemon single-instance WARN: {why}", "WARN")
    write_stopped()
    ensure_state_dir()
    save_pid()
    try:
        run_daemon_loop()
    except SystemExit:
        # A refusal raised from inside the loop (the backstop claim) or a
        # deliberate loop exit is control flow, not a crash: propagate as-is
        # (do not write_stopped a holder we never became).
        raise
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
