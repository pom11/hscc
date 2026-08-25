"""Tests for restart_daemon.sh — bootstrap's post-install daemon restart.

restart_daemon.sh is the bash helper bootstrap.sh calls right after the daemon
setup step. It stops the daemon via HSCC's own lifecycle (`hscc stop`), starts
it again (`hscc start`), and verifies the running process actually turned over
by comparing the daemon pid before vs after (a same-PID "restart" is exactly the
failure being fixed).

The tests drive the helper with a STUB `hscc` command + a stub pid file, so they
exercise the real bash logic (command sequence, pid comparison, exit codes)
without ever touching a real daemon / launchd / systemd / ~/.hscc.
"""

import os
import stat
import subprocess
from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent
RESTART = BOOT / "restart_daemon.sh"

# A stub daemon CLI (Python, matching the real hscc.py which the helper runs
# via $HSCC_PYBIN $HSCC_CMD) that simulates stop/start on a pid file, recording
# every command it is asked to run. Mode controls how `start` behaves:
#   normal      : writes a fresh pid (last+1)          -> restart turns over
#   same-pid    : writes back the SAME pid             -> "restart" is a no-op
#   no-return   : never writes a pid file              -> daemon does not come back
#   start-error : writes back the SAME pid AND emits an error to stderr, so the
#                 helper must surface that text in its outcome line (rc 2)
STUB = """#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get("STUB_MODE", "normal")
calls = os.environ["STUB_CALLS"]
pid_file = os.environ["STUB_PID_FILE"]
last_pid = os.environ["STUB_LAST_PID"]
cmd = sys.argv[1]
with open(calls, "a") as f:
    f.write(cmd + "\\n")
if cmd == "stop":
    if os.path.exists(pid_file):
        os.remove(pid_file)
elif cmd == "start":
    if mode in ("same-pid", "start-error"):
        if mode == "start-error":
            sys.stderr.write("hscc_daemon: failed to (re)start (simulated)\\n")
        with open(pid_file, "w") as f:
            f.write("42")
    elif mode == "no-return":
        pass
    elif mode == "daemonize":
        # Simulate the REAL `hscc start` (cli.cmd_start): double-fork a
        # grandchild that keeps running (and keeps fd 1/2 open) AFTER the
        # parent `hscc start` process exits. This is exactly the scenario that
        # used to hang restart_daemon.sh's `$(...)` capture of start output.
        pid = os.fork()
        if pid > 0:
            # parent (the `hscc start` CLI) returns immediately, like cmd_start
            pass
        else:
            pid2 = os.fork()
            if pid2 > 0:
                os._exit(0)
            # grandchild: record pid, then stay alive like the daemon
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
            with open(last_pid, "w") as f:
                f.write(str(os.getpid()))
            while True:
                time.sleep(5)
    else:
        prev = 0
        if os.path.exists(last_pid):
            prev = int(open(last_pid).read().strip())
        nxt = str(prev + 1)
        with open(last_pid, "w") as f:
            f.write(nxt)
        with open(pid_file, "w") as f:
            f.write(nxt)
"""

# A stub CLI that imports its OWN package (like the real hscc_daemon/hscc.py,
# which does `from hscc_daemon.serving import ...`). The CLI lives in a
# `hscc_daemon/` subdir and imports a package (`stublib`) that sits at the
# parent level — so run by bare path (script dir on sys.path) it raises
# ModuleNotFoundError, exactly the failure this fix addresses. With the helper's
# PYTHONPATH fallback (path A, package root = parent of hscc_daemon/) it must
# import and run fine.
IMPORT_STUB = {
    "hscc_daemon/hscc.py": """#!/usr/bin/env python3
from stublib.cli import main
import sys
sys.exit(main())
""",
    "stublib/__init__.py": "",
    "stublib/cli.py": """import os, sys
def main():
    mode = os.environ.get("STUB_MODE", "normal")
    calls = os.environ["STUB_CALLS"]
    pid_file = os.environ["STUB_PID_FILE"]
    last_pid = os.environ["STUB_LAST_PID"]
    cmd = sys.argv[1]
    with open(calls, "a") as f:
        f.write(cmd + "\\n")
    if cmd == "stop":
        if os.path.exists(pid_file):
            os.remove(pid_file)
    elif cmd == "start":
        prev = 0
        if os.path.exists(last_pid):
            prev = int(open(last_pid).read().strip())
        nxt = str(prev + 1)
        with open(last_pid, "w") as f:
            f.write(nxt)
        with open(pid_file, "w") as f:
            f.write(nxt)
    return 0
""",
}


def _setup(tmp_path, mode="normal", preexisting_pid=None):
    """Create a stub hscc CLI + pid files in a fresh tmp dir; return env dict."""
    stub = tmp_path / "hscc.py"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    calls = tmp_path / "calls.txt"
    pid_file = tmp_path / "daemon.pid"
    last_pid = tmp_path / "last.pid"

    if preexisting_pid is not None:
        pid_file.write_text(str(preexisting_pid))
        last_pid.write_text(str(preexisting_pid))

    env = os.environ.copy()
    env.update({
        "HSCC_CMD": str(stub),
        "HSCC_PID_FILE": str(pid_file),
        "STUB_CALLS": str(calls),
        "STUB_PID_FILE": str(pid_file),
        "STUB_LAST_PID": str(last_pid),
        "STUB_MODE": mode,
        "POLL_ATTEMPTS": "3",
        "POLL_INTERVAL": "1",
    })
    return env, calls


def _run(env):
    return subprocess.run(
        ["bash", str(RESTART)],
        env=env, capture_output=True, text=True,
    )


def test_issues_stop_then_start_and_verifies_pid_changed(tmp_path):
    """The daemon step must restart: `hscc stop` then `hscc start`, and confirm
    the PID turned over (no same-PID "restart")."""
    env, calls = _setup(tmp_path, preexisting_pid=7)
    res = _run(env)

    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "daemon restarted (pid 7 -> 8)" in res.stdout
    # warning-style output must NOT appear on success
    assert "warn" not in res.stdout and "did not come back" not in res.stdout


def test_skip_daemon_does_not_attempt_restart(tmp_path):
    """bootstrap passes --skip when running with --skip-daemon; then the helper
    must not invoke stop/start at all and must succeed."""
    env, calls = _setup(tmp_path)
    res = subprocess.run(
        ["bash", str(RESTART), "--skip"],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert calls.exists() is False or calls.read_text() == ""
    assert "skipped" in res.stdout


def test_pid_unchanged_after_restart_warns(tmp_path):
    """If the pid file still holds the same pid after stop/start, the restart did
    NOT take (the exact failure being fixed): report a warning (rc 2), never
    success."""
    env, calls = _setup(tmp_path, mode="same-pid", preexisting_pid=42)
    res = _run(env)

    assert res.returncode == 2
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "PID unchanged after restart" in res.stdout


def test_daemon_fails_to_come_back_warns_and_does_not_raise(tmp_path):
    """If the daemon does not come back after restart, report a warning (rc 3)
    rather than success; bootstrap treats a non-zero return as a non-fatal warn,
    so the install still completes."""
    env, calls = _setup(tmp_path, mode="no-return", preexisting_pid=9)
    res = _run(env)

    assert res.returncode == 3
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "did not come back" in res.stdout
    # the helper must have returned (not died) so the caller can continue
    assert "Traceback" not in res.stderr


def test_fresh_install_no_existing_pid_starts_ok(tmp_path):
    """On a fresh install with no daemon yet running, stop/start leaves the
    daemon running with the new code — that's success, not a warning."""
    env, calls = _setup(tmp_path)  # no preexisting pid
    res = _run(env)

    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "daemon started (pid" in res.stdout


def test_import_package_stub_succeeds_with_pythonpath(tmp_path):
    """A CLI that imports its OWN package (like the real hscc.py) must work —
    this is the exact ModuleNotFoundError the fix addresses. On a bare path with
    no PYTHONPATH the import fails; the helper's fallback (path A) sets
    PYTHONPATH to the package root so it must run and turn the pid over."""
    for rel, content in IMPORT_STUB.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    cli = tmp_path / "hscc_daemon" / "hscc.py"
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)

    # sanity: bare path, no PYTHONPATH -> ModuleNotFoundError (reproduces bug)
    bare = subprocess.run(
        ["python3", str(cli), "start"],
        env={**os.environ, "STUB_CALLS": str(tmp_path / "calls.txt"),
             "STUB_PID_FILE": str(tmp_path / "daemon.pid"),
             "STUB_LAST_PID": str(tmp_path / "last.pid")},
        capture_output=True, text=True,
    )
    assert "ModuleNotFoundError" in bare.stderr

    env, calls = _setup(tmp_path, preexisting_pid=7)
    env["HSCC_CMD"] = str(cli)
    res = _run(env)

    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "daemon restarted (pid 7 -> 8)" in res.stdout


def test_restart_failure_surfaces_error_text(tmp_path):
    """When restart fails, the helper must surface the underlying error text in
    its outcome line instead of only "PID unchanged" — the silent
    `>/dev/null 2>&1` is what hid the original bug."""
    env, calls = _setup(tmp_path, mode="start-error", preexisting_pid=42)
    res = _run(env)

    assert res.returncode == 2
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "PID unchanged after restart" in res.stdout
    assert "failed to (re)start (simulated)" in res.stdout


def test_start_returns_promptly_and_daemon_survives_when_it_daemonizes(tmp_path):
    """The REAL `hscc start` double-forks a grandchild daemon that keeps running
    (and keeps fd 1/2 open) after the `hscc start` process exits. restart_
    daemon.sh must still return promptly — it must NOT block on a
    command-substitution pipe held open by that daemon — and the daemon must
    survive the script exiting. This is a regression test for the bootstrap
    hang: before the detach fix, this timed out (the start `$(...)` blocked)."""
    import signal as _sig
    env, calls = _setup(tmp_path, mode="daemonize", preexisting_pid=7)
    try:
        res = subprocess.run(
            ["bash", str(RESTART)],
            env=env, capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "restart_daemon.sh blocked on `start`: daemon inherited the pipe"
        )
    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]
    pid = int(open(env["HSCC_PID_FILE"]).read().strip())
    assert pid != 7  # it must be a NEW daemon pid (the grandchild's own pid)
    # ...and the daemon must still be alive AFTER restart_daemon.sh exited
    try:
        os.kill(pid, 0)
    except OSError:
        raise AssertionError("daemon died when restart_daemon.sh exited")
    assert "daemon restarted (pid 7 ->" in res.stdout
    # clean up the spawned daemon so it can't leak into other tests
    os.kill(pid, _sig.SIGKILL)


def test_bootstrap_wires_restart_inside_daemon_block_only(tmp_path):
    """bootstrap.sh must (a) invoke restart_daemon.sh inside the daemon install
    step and (b) place it inside the non-skip branch so --skip-daemon never
    triggers a restart."""
    src = (BOOT / "bootstrap.sh").read_text()
    # restart invocation is present and points at the helper
    assert "restart_daemon.sh" in src
    # it lives inside the `else` of `if $SKIP_DAEMON`, i.e. after the setup call
    daemon_blk = src.split("hdr \"Install: daemon\"", 1)[1].split("── Summary", 1)[0]
    assert "restart_daemon.sh" in daemon_blk
    assert daemon_blk.count('"$BOOT_DIR/restart_daemon.sh"') == 1
    # only reached when not skipping the daemon
    skip_idx = daemon_blk.find("if $SKIP_DAEMON")
    rst_idx = daemon_blk.find("restart_daemon.sh")
    assert skip_idx != -1 and rst_idx > skip_idx
    # and it is invoked AFTER the daemon setup step within that branch
    setup_idx = daemon_blk.find("DAEMON_SETUP")
    assert setup_idx != -1 and rst_idx > setup_idx
