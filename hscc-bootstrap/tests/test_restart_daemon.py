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
#   normal    : writes a fresh pid (last+1)          -> restart turns over
#   same-pid  : writes back the SAME pid             -> "restart" is a no-op
#   no-return : never writes a pid file              -> daemon does not come back
STUB = """#!/usr/bin/env python3
import os, sys
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
    if mode == "same-pid":
        with open(pid_file, "w") as f:
            f.write("42")
    elif mode == "no-return":
        pass
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
