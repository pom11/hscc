"""Daemon entry paths must clear a BLOCKED stop signal inherited from the spawner.

WHY THIS FILE EXISTS (card t_81e00872, finding from t_7c2eb604). ``signal.signal``
installs a signal's DISPOSITION; it does not clear the calling thread's signal
MASK, and a mask survives fork/exec (``subprocess`` restores ignored
dispositions, never the mask). The API path already clears the inherited mask
(t_7c2eb604), but the daemon entry paths did not: `cli.cmd_start`'s forked
child, `EventDrivenDaemon.start`, and `daemon_ops.run_daemon_loop` (the
service-supervised `start-daemon` entry) all installed SIGTERM/SIGINT handlers
over whatever mask the spawner left behind. A daemon started by a process that
runs with SIGINT blocked therefore received the stop, held it pending forever,
and could only be killed — exactly the hazard the API fix measured.

The cases below reproduce the environment rather than trusting a call count:

  * a real child blocks the signal BEFORE the product installs its handler, and
    the case sends the signal and waits for the handler to run;
  * the two entry paths are driven for real: `cli.cmd_start` (double-fork, with
    only the unrelated daemon loop stubbed) and `EventDrivenDaemon.start` (with
    only the unrelated watcher/poller seams stubbed);
  * wiring cases pin that the clear happens AFTER both handlers are installed
    at each install site, so a future edit cannot move it above them.

Hermetic by construction: every child gets a throwaway HOME (so the daemon pid,
log, state and single-instance lock resolve inside the sandbox), `cmd_start`'s
`setsid` is fenced so the double-forked grandchild stays in the harness process
group for teardown, and the event-driven child stubs the kqueue/poller/launchd
work that is not the subject here. No live daemon, state or launchd job is
touched.
"""

import os
import signal
import subprocess
import sys
import time
import textwrap
from pathlib import Path

import pytest

import hscc_daemon.util as util

REPO = Path(__file__).resolve().parents[2]

#: Budgets are load budgets, not behaviour budgets: a blocked signal never
#: dispatches, so the failure path is the timeout either way. 30 s is >5x the
#: slowest idle spawn observed on this suite's child tests.
READY_BUDGET = 30.0
DISPATCH_BUDGET = 15.0


def _env(home: Path, **extra) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _wait_for(path: Path, timeout=READY_BUDGET):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text().strip()
        time.sleep(0.02)
    return None


def _wait_pid_gone(pid, timeout=DISPATCH_BUDGET):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return True
        time.sleep(0.02)
    return False


def _reap(proc):
    """Kill the harness's process group and wait for the direct child."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# --------------------------------------------------------------------------- #
# the helper: a real child that blocks the signal, then asks for the clear
# --------------------------------------------------------------------------- #

_HELPER_CHILD = textwrap.dedent('''
    import os, signal, sys, time
    sig = getattr(signal, sys.argv[1])
    signal.pthread_sigmask(signal.SIG_BLOCK, {sig})
    assert sig in signal.pthread_sigmask(signal.SIG_BLOCK, set()), "not blocked"
    from hscc_daemon.util import unblock_stop_signals
    unblock_stop_signals()
    assert sig not in signal.pthread_sigmask(signal.SIG_BLOCK, set()), (
        "unblock_stop_signals left the signal blocked")
    signal.signal(sig, lambda s, f: sys.exit(0))
    print("READY", flush=True)
    while True:
        time.sleep(1)
''')


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_unblock_stop_signals_makes_an_inherited_block_dispatch(tmp_path, sig):
    """The helper must turn a blocked handler into a dispatched one."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HELPER_CHILD, sig.name],
        env=_env(tmp_path / "home"), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        assert proc.stdout.readline().strip() == "READY", (
            "the child never reached READY; it could not block or unblock "
            f"{sig.name}")
        os.kill(proc.pid, sig)
        try:
            rc = proc.wait(timeout=DISPATCH_BUDGET)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"{sig.name} stayed BLOCKED: the installed handler was never "
                "dispatched, which is the t_81e00872 defect")
        assert rc == 0, f"{sig.name} was dispatched but the child exited {rc}"
    finally:
        _reap(proc)


# --------------------------------------------------------------------------- #
# hscc_daemon.cli.cmd_start: the forked child installs handlers, then clears
# --------------------------------------------------------------------------- #

_CMD_START_CHILD = textwrap.dedent('''
    import os, signal, sys, time
    sig = getattr(signal, sys.argv[1])
    signal.pthread_sigmask(signal.SIG_BLOCK, {sig})
    assert sig in signal.pthread_sigmask(signal.SIG_BLOCK, set()), "not blocked"

    import hscc_daemon.cli as cli
    import hscc_daemon.daemon_ops as daemon_ops
    import hscc_daemon.daemon_singleton as singleton

    # Keep the fork chain in THIS process group so the harness can reap it.
    # Production detaches; that detach is not what this case measures.
    cli.os.setsid = lambda: None
    # The single-instance claim reads the process table; it is unrelated to the
    # signal mask and stays out of this sandbox case.
    singleton.hold_daemon_loop_lock_fail_open = lambda **kw: (True, None)
    singleton.release_daemon_lock = lambda: None

    loop_pid = []

    def fake_loop():
        # The real loop is tens of checks and fleet touches; the stop handler
        # installed by cmd_start is what this case is about. Announce the pid
        # that actually serves, then stay alive until the signal arrives.
        loop_pid.append(os.getpid())
        with open(os.environ["MASK_READY"], "w") as fh:
            fh.write(str(os.getpid()))
        while True:
            time.sleep(0.05)

    daemon_ops.run_daemon_loop = fake_loop
    cli.cmd_start()
    print("PARENT_RETURNED", flush=True)
    while True:
        time.sleep(1)
''')


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_cmd_start_daemon_honours_a_signal_its_spawner_blocked(tmp_path, sig):
    """A daemon double-forked out of a blocking spawner must still stop."""
    home = tmp_path / "home"
    (home / ".hscc").mkdir(parents=True)
    ready = tmp_path / "daemon.pid.seen"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CMD_START_CHILD, sig.name],
        env=_env(home, MASK_READY=ready), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        pid_text = _wait_for(ready)
        assert pid_text, (
            "the forked daemon never reached its loop; cmd_start likely "
            f"failed before the signal path.\n{proc.stdout.readline()}")
        daemon_pid = int(pid_text)
        os.kill(daemon_pid, sig)
        assert _wait_pid_gone(daemon_pid), (
            f"{sig.name} inherited as BLOCKED was never dispatched: the "
            "double-forked daemon is still alive and can only be killed")
    finally:
        _reap(proc)


def test_cmd_start_clears_the_mask_after_installing_handlers(
        tmp_hfcc_dir, monkeypatch):
    """Pin the ordering at the install site: both handlers, THEN the clear."""
    from hscc_daemon import cli, daemon_ops
    from hscc_daemon import daemon_singleton
    from hscc_daemon import state as state_mod

    events = []
    monkeypatch.setattr(state_mod, "STATE_DIR", str(tmp_hfcc_dir / "state"))
    monkeypatch.setattr(state_mod, "ensure_state_dir", lambda: None)
    monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
    monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
    monkeypatch.setattr(daemon_ops, "get_pid", lambda: None)
    monkeypatch.setattr(daemon_ops, "save_pid", lambda: events.append("save"))
    monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
    monkeypatch.setattr(daemon_ops, "run_daemon_loop", lambda: None)
    monkeypatch.setattr(daemon_singleton, "hold_daemon_loop_lock_fail_open",
                        lambda **kw: (True, None))
    monkeypatch.setattr(cli, "_detach_stdio", lambda: None)
    monkeypatch.setattr(cli.os, "setsid", lambda: events.append("setsid"))
    monkeypatch.setattr(cli.os, "chdir", lambda p: events.append("chdir"))
    monkeypatch.setattr(cli.os, "_exit", lambda code: None)
    monkeypatch.setattr(cli.signal, "signal",
                        lambda signum, handler: events.append(
                            f"handler:{signum.name}"))
    monkeypatch.setattr(cli, "unblock_stop_signals",
                        lambda: events.append("unblock"))

    forks = iter([0, 0])
    monkeypatch.setattr(cli.os, "fork", lambda: next(forks))
    cli.cmd_start()

    assert "handler:SIGTERM" in events and "handler:SIGINT" in events, (
        f"cmd_start no longer installs both stop handlers: {events}")
    assert "unblock" in events, (
        "cmd_start no longer clears the inherited signal mask, so a daemon "
        f"spawned with SIGINT blocked can never stop: {events}")
    assert events.index("unblock") > events.index("handler:SIGINT"), (
        f"the mask clear must follow both handler installs: {events}")


# --------------------------------------------------------------------------- #
# hscc_daemon.event_driven.EventDrivenDaemon.start
# --------------------------------------------------------------------------- #

_EVENT_DRIVEN_CHILD = textwrap.dedent('''
    import os, signal, sys, time
    sig = getattr(signal, sys.argv[1])
    signal.pthread_sigmask(signal.SIG_BLOCK, {sig})
    assert sig in signal.pthread_sigmask(signal.SIG_BLOCK, set()), "not blocked"

    from hscc_daemon import event_driven as ed
    ed.HSCC_DIR = os.environ["MASK_HSCC"]
    ed.STATE_DIR = os.path.join(os.environ["MASK_HSCC"], "state")
    os.makedirs(ed.STATE_DIR, exist_ok=True)

    daemon = ed.EventDrivenDaemon(
        check_map={}, watchdog_fn=lambda: True, trigger_fn=lambda: None,
        install_launchd=False)
    # The watcher/poller/launchd layers are not what this case measures; the
    # handler `start()` installs and the mask it clears are.
    daemon._init_kqueue = lambda *a, **k: False
    daemon._start_fallback = lambda: None
    daemon._cleanup_stale_launchd = lambda: None
    daemon.start()
    print("READY", flush=True)
    while daemon.is_running():
        time.sleep(0.05)
    print("STOPPED", flush=True)
''')


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_event_driven_daemon_honours_a_signal_its_spawner_blocked(
        tmp_path, sig):
    """A daemon started out of a blocking spawner must still stop."""
    hscc_dir = tmp_path / "hscc"
    hscc_dir.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", _EVENT_DRIVEN_CHILD, sig.name],
        env=_env(tmp_path / "home", MASK_HSCC=hscc_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True)
    try:
        assert proc.stdout.readline().strip() == "READY", (
            "the event-driven daemon never started")
        os.kill(proc.pid, sig)
        try:
            rc = proc.wait(timeout=DISPATCH_BUDGET)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"{sig.name} inherited as BLOCKED was never dispatched: the "
                "event-driven daemon is still running")
        assert rc == 0, f"{sig.name} stopped the daemon with rc={rc}"
    finally:
        _reap(proc)


def test_event_driven_start_clears_the_mask_after_installing_handlers(
        monkeypatch):
    """Pin the ordering at EventDrivenDaemon.start's install site."""
    from hscc_daemon import event_driven as ed

    events = []
    monkeypatch.setattr(ed, "_event_log", lambda *a, **k: None)
    monkeypatch.setattr(ed.signal, "signal",
                        lambda signum, handler: events.append(
                            f"handler:{signum.name}"))
    monkeypatch.setattr(util, "unblock_stop_signals",
                        lambda: events.append("unblock"))

    daemon = ed.EventDrivenDaemon(
        check_map={}, watchdog_fn=lambda: True, trigger_fn=lambda: None,
        install_launchd=False)
    monkeypatch.setattr(daemon, "_init_kqueue", lambda *a, **k: False)
    monkeypatch.setattr(daemon, "_start_fallback", lambda: None)
    monkeypatch.setattr(daemon, "_cleanup_stale_launchd", lambda: None)
    daemon.start()

    assert "handler:SIGTERM" in events and "handler:SIGINT" in events, (
        f"EventDrivenDaemon.start no longer installs both handlers: {events}")
    assert "unblock" in events, (
        "EventDrivenDaemon.start no longer clears the inherited signal mask, "
        f"so a blocked stop is never dispatched: {events}")
    assert events.index("unblock") > events.index("handler:SIGINT"), (
        f"the mask clear must follow both handler installs: {events}")
