"""Tests for restart_daemon.sh — bootstrap's post-install daemon restart.

restart_daemon.sh is the bash helper bootstrap.sh calls right after the daemon
setup step. It has two restart paths and the tests cover both:

  * a supervisor owns the service (the operator host, and every Linux node) ->
    restart THROUGH the supervisor: `launchctl kickstart -k
    gui/$UID/com.hermes.hscc_daemon` on macOS, `systemctl --user restart
    <unit>` on Linux, verified against the pid the supervisor itself reports.
    This is the ONLY sanctioned restart while the service is loaded: the
    2026-10-29 P0 showed that the `hscc` console script is brew python with no
    macOS Local Network grant (every stream then reads "No route to host" and
    the daemon declares the fleet dead) and that `hscc start` escapes launchd
    (PPID 1), leaving the job label supervised with no pid.
  * no service loaded (fresh machine, CI) -> the daemon's own CLI (`hscc stop`
    / `hscc start`) + pid-file turnover.

Both paths first run the supervisor-level twin check: a live daemon process the
supervisor does not own is reported by pid and a second loop is never started
beside it (rc 4).

The tests drive the helper with STUBS for `launchctl`, `systemctl`, `pgrep` and
the daemon CLI in a fresh tmp dir, so they exercise the real bash logic (which
command runs with which arguments, pid comparison, exit codes) without ever
touching a real daemon, the real launchd job, a real systemd unit, or ~/.hscc.
The stubs are wired in by `_setup` for EVERY case — a test that forgot them
would talk to the real service on a macOS runner, which is exactly why the
helper honours HSCC_LAUNCHCTL / HSCC_SYSTEMCTL / HSCC_PGREP at all.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

BOOT = Path(__file__).resolve().parent.parent
RESTART = BOOT / "restart_daemon.sh"

# The launchd job label under test. A test-visible constant so the assertion on
# the executed kickstart command and the helper's default cannot drift apart
# silently (test_launchd_label_constant_matches_sibling_emitters pins it to the
# other places the real label is written).
LAUNCHD_LABEL = "com.hermes.hscc_daemon"

# A stub daemon CLI (Python, matching the real hscc.py which the helper runs
# via $HSCC_PYBIN $HSCC_CMD) that simulates stop/start on a pid file, recording
# every command it is asked to run. Mode controls how `start` behaves:
#   normal      : writes a fresh pid (last+1)          -> restart turns over
#   same-pid    : writes back the SAME pid             -> "restart" is a no-op
#   no-return   : never writes a pid file              -> daemon does not come back
#   start-error : writes back the SAME pid AND emits an error to stderr, so the
#                 helper must surface that text in its outcome line (rc 2)
#   daemonize   : double-forks a live grandchild (see below)
# It also appends its own argv line to $STUB_INVOKED so a test can prove the CLI
# was (or was NOT) invoked at all — the "no second start" half of the twin check.
STUB = """#!/usr/bin/env python3
import os, sys, time
mode = os.environ.get("STUB_MODE", "normal")
calls = os.environ["STUB_CALLS"]
pid_file = os.environ["STUB_PID_FILE"]
last_pid = os.environ["STUB_LAST_PID"]
cmd = sys.argv[1]
with open(calls, "a") as f:
    f.write(cmd + "\\n")
invoked = os.environ.get("STUB_INVOKED")
if invoked:
    with open(invoked, "a") as f:
        f.write("cli " + cmd + "\\n")
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

# Stub `launchctl`. Records every invocation (one argv per line) to
# $STUB_LAUNCHD_CALLS and reads its behaviour from env:
#   STUB_LAUNCHD_LOADED      1 -> job is bootstrapped: `print` succeeds
#                              (a loaded job with no pid still succeeds — that
#                              is the incident's shape)            [default 0]
#   STUB_LAUNCHD_PID_BEFORE  pid `print` reports while no kickstart has run
#   STUB_LAUNCHD_PID_AFTER   comma-separated pids `print` reports on successive
#                            calls AFTER a kickstart ("" -> job stays pid-less).
#                            Later calls repeat the last entry, so a 1-entry
#                            list is "the new pid" and ",77001" is "no pid yet,
#                            then 77001" — which is how launchd actually behaves
#                            (it reaps, waits ThrottleInterval, re-spawns).
#   STUB_LAUNCHD_KICK_RC     exit code of `kickstart`                [default 0]
#   STUB_LAUNCHD_KICK_ERR    text `kickstart` writes to stderr
# Kicked-state lives in $STUB_LAUNCHD_STATE so it survives across the helper's
# polling loop (each launchctl call is a fresh process).
LAUNCHCTL_STUB = """#!/usr/bin/env python3
import os, sys

calls_file = os.environ["STUB_LAUNCHD_CALLS"]
state_file = os.environ["STUB_LAUNCHD_STATE"]
loaded = os.environ.get("STUB_LAUNCHD_LOADED", "0") == "1"
kick_rc = int(os.environ.get("STUB_LAUNCHD_KICK_RC", "0"))
kick_err = os.environ.get("STUB_LAUNCHD_KICK_ERR", "")
pid_before = os.environ.get("STUB_LAUNCHD_PID_BEFORE", "").strip()
after = [p.strip() for p in os.environ.get("STUB_LAUNCHD_PID_AFTER", "").split(",")]

argv = sys.argv[1:]
with open(calls_file, "a") as f:
    f.write(" ".join(argv) + "\\n")

def state():
    if not os.path.exists(state_file):
        return {"kicked": False, "polls": 0}
    d = {"kicked": False, "polls": 0}
    for line in open(state_file):
        k, _, v = line.strip().partition("=")
        if k == "kicked":
            d["kicked"] = v == "1"
        elif k == "polls":
            d["polls"] = int(v or "0")
    return d

def save(d):
    with open(state_file, "w") as f:
        f.write("kicked=%d\\npolls=%d\\n" % (1 if d["kicked"] else 0, d["polls"]))

sub = argv[0] if argv else ""
if sub == "print":
    if not loaded:
        sys.stderr.write("Could not find service in domain\\n")
        sys.exit(113)
    st = state()
    if st["kicked"]:
        i = st["polls"]
        pid = after[i] if i < len(after) else (after[-1] if after else "")
        st["polls"] = i + 1
        save(st)
    else:
        pid = pid_before
    print("gui/501/com.hermes.hscc_daemon = {")
    print("\\tactive count = 1")
    print("\\tstate = running")
    if pid:
        print("\\tpid = %s" % pid)
    else:
        print("\\t(no pid reported)")
    print("}")
    sys.exit(0)
if sub == "kickstart":
    if kick_rc == 0:
        st = state(); st["kicked"] = True; save(st)
    if kick_rc:
        sys.stderr.write(kick_err or "Bootout failed\\n")
    sys.exit(kick_rc)
if sub == "list":
    sys.exit(0 if loaded else 1)
sys.exit(0)
"""

# Stub `pgrep -f <pattern>`: emits one pid per line from $STUB_ORPHAN_PIDS
# (comma-separated). Deliberately does NOT interpret the pattern — the point is
# to feed the helper a "live daemon processes" answer and assert what it does
# with it, not to re-implement pgrep.
PGREP_STUB = """#!/usr/bin/env python3
import os, sys
pids = [p.strip() for p in os.environ.get("STUB_ORPHAN_PIDS", "").split(",") if p.strip()]
for p in pids:
    print(p)
sys.exit(0 if pids else 1)
"""

# Stub `systemctl`. The daemon has a second supervisor (systemd --user, written
# by systemd-setup.sh) and the helper must treat it exactly like launchd:
# restart THROUGH it and trust only ITS pid. Behaviour from env:
#   STUB_SYSTEMD_ACTIVE     comma-separated unit names that are "active"
#                           (empty -> systemd has no hscc unit -> console path)
#   STUB_SYSTEMD_PID_BEFORE MainPID reported before `restart`
#   STUB_SYSTEMD_PID_AFTER  MainPID(s) reported after `restart` (comma list,
#                           last entry repeats) — "" keeps reporting none
#   STUB_SYSTEMD_RESTART_RC exit code of `restart`                     [default 0]
#   STUB_SYSTEMD_RESTART_ERR text `restart` writes to stderr
# Every invocation is appended to $STUB_SYSTEMD_CALLS; the restarted-state file
# lets the answer survive across the helper's polling loop.
SYSTEMCTL_STUB = """#!/usr/bin/env python3
import os, sys

calls_file = os.environ["STUB_SYSTEMD_CALLS"]
state_file = os.environ["STUB_SYSTEMD_STATE"]
active = [u.strip() for u in os.environ.get("STUB_SYSTEMD_ACTIVE", "").split(",") if u.strip()]
restart_rc = int(os.environ.get("STUB_SYSTEMD_RESTART_RC", "0"))
restart_err = os.environ.get("STUB_SYSTEMD_RESTART_ERR", "")
pid_before = os.environ.get("STUB_SYSTEMD_PID_BEFORE", "").strip()
after = [p.strip() for p in os.environ.get("STUB_SYSTEMD_PID_AFTER", "").split(",")]

argv = sys.argv[1:]
with open(calls_file, "a") as f:
    f.write(" ".join(argv) + "\\n")

def state():
    d = {"restarted": False, "polls": 0}
    if os.path.exists(state_file):
        for line in open(state_file):
            k, _, v = line.strip().partition("=")
            if k == "restarted":
                d["restarted"] = v == "1"
            elif k == "polls":
                d["polls"] = int(v or "0")
    return d

def save(d):
    with open(state_file, "w") as f:
        f.write("restarted=%d\\npolls=%d\\n" % (1 if d["restarted"] else 0, d["polls"]))

# argv looks like: --user is-active --quiet <unit>
args = [a for a in argv if not a.startswith("--")]
verb = args[0] if args else ""
unit = args[1] if len(args) > 1 else ""

if verb == "is-active":
    sys.exit(0 if unit in active else 3)
if verb == "show":
    st = state()
    if st["restarted"]:
        i = st["polls"]
        pid = after[i] if i < len(after) else (after[-1] if after else "")
        st["polls"] = i + 1
        save(st)
    else:
        pid = pid_before
    print(pid or "0")
    sys.exit(0)
if verb == "restart":
    if restart_rc == 0:
        st = state(); st["restarted"] = True; save(st)
    else:
        sys.stderr.write(restart_err or "Job for hscc_daemon.service failed\\n")
    sys.exit(restart_rc)
if verb == "status":
    sys.exit(0 if unit in active else 3)
sys.exit(0)
"""


def _make_exec(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _setup(tmp_path, mode="normal", preexisting_pid=None, *, launchd=False,
           launchd_pid_before="", launchd_pid_after="",
           launchd_kick_rc=0, launchd_kick_err="",
           systemd_unit=None, systemd_pid_before="", systemd_pid_after="",
           systemd_restart_rc=0, systemd_restart_err="",
           orphan_pids=None):
    """Create stub daemon CLI + launchctl + systemctl + pgrep + pid files in a
    fresh tmp dir; return (env, calls_path).

    EVERY case gets stubbed launchctl, systemctl and pgrep (see module
    docstring): on a macOS runner an unstubbed `launchctl print` would find the
    operator's real job and the helper would restart the real daemon. Defaults:
    no supervisor at all, no daemon processes anywhere — i.e. the classic
    console-script world. `systemd_unit="hscc_daemon.service"` makes systemd the
    supervisor instead of launchd.
    """
    stub = _make_exec(tmp_path / "hscc.py", STUB)
    launchctl = _make_exec(tmp_path / "launchctl", LAUNCHCTL_STUB)
    systemctl = _make_exec(tmp_path / "systemctl", SYSTEMCTL_STUB)
    pgrep = _make_exec(tmp_path / "pgrep", PGREP_STUB)

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
        "STUB_INVOKED": str(tmp_path / "invoked.txt"),
        "POLL_ATTEMPTS": "3",
        "POLL_INTERVAL": "1",
        # ── service-layer stubs (never the real ones) ──
        "HSCC_LAUNCHCTL": str(launchctl),
        "HSCC_LAUNCHD_LABEL": LAUNCHD_LABEL,
        "HSCC_SYSTEMCTL": str(systemctl),
        "HSCC_PGREP": str(pgrep),
        "STUB_LAUNCHD_CALLS": str(tmp_path / "launchd-calls.txt"),
        "STUB_LAUNCHD_STATE": str(tmp_path / "launchd-state.txt"),
        "STUB_LAUNCHD_LOADED": "1" if launchd else "0",
        "STUB_LAUNCHD_PID_BEFORE": launchd_pid_before,
        "STUB_LAUNCHD_PID_AFTER": launchd_pid_after,
        "STUB_LAUNCHD_KICK_RC": str(launchd_kick_rc),
        "STUB_LAUNCHD_KICK_ERR": launchd_kick_err,
        "STUB_SYSTEMD_CALLS": str(tmp_path / "systemd-calls.txt"),
        "STUB_SYSTEMD_STATE": str(tmp_path / "systemd-state.txt"),
        "STUB_SYSTEMD_ACTIVE": systemd_unit or "",
        "STUB_SYSTEMD_PID_BEFORE": systemd_pid_before,
        "STUB_SYSTEMD_PID_AFTER": systemd_pid_after,
        "STUB_SYSTEMD_RESTART_RC": str(systemd_restart_rc),
        "STUB_SYSTEMD_RESTART_ERR": systemd_restart_err,
        "STUB_ORPHAN_PIDS": "" if orphan_pids is None else ",".join(str(p) for p in orphan_pids),
        "LAUNCHD_POLL_ATTEMPTS": "3",
        "LAUNCHD_POLL_INTERVAL": "1",
    })
    if systemd_unit:
        # name the unit explicitly so a candidate-list change cannot silently
        # redirect a systemd case at launchd
        env["HSCC_SYSTEMD_UNIT"] = systemd_unit
    return env, calls


def _run(env, **kw):
    return subprocess.run(
        ["bash", str(RESTART)],
        env=env, capture_output=True, text=True, **kw,
    )


def _launchd_calls(env) -> list:
    p = Path(env["STUB_LAUNCHD_CALLS"])
    return p.read_text().splitlines() if p.exists() else []


def _systemd_calls(env) -> list:
    p = Path(env["STUB_SYSTEMD_CALLS"])
    return p.read_text().splitlines() if p.exists() else []


def _all_service_calls(env) -> list:
    return _launchd_calls(env) + _systemd_calls(env)


def _code_lines_only() -> str:
    """restart_daemon.sh with whole-line comments dropped. The behavioural pins
    below read the helper's SOURCE for which supervisor verbs it may invoke;
    prose is allowed to *name* forbidden commands (the WARN line hands the
    operator the bootout+bootstrap remedy), so only code lines count."""
    return "\n".join(
        l for l in RESTART.read_text().splitlines() if not l.lstrip().startswith("#")
    )


def _invoked(env) -> list:
    p = Path(env["STUB_INVOKED"])
    return p.read_text().splitlines() if p.exists() else []


# ── the operator rule: launchd-loaded -> kickstart, never the console script ──


def test_launchd_loaded_restarts_via_kickstart_only(tmp_path):
    """With the job loaded the ONLY command issued is `launchctl kickstart -k
    gui/$UID/com.hermes.hscc_daemon`, and the daemon CLI is never invoked. This
    is the pinned operator rule: the console script is a different (TCC-blind)
    interpreter and `hscc start` escapes launchd entirely."""
    env, calls = _setup(tmp_path, launchd=True,
                        launchd_pid_before="61218", launchd_pid_after="77001")
    res = _run(env)

    assert res.returncode == 0
    assert " ".join(_launchd_calls(env)).count(
        f"kickstart -k gui/{os.getuid()}/{LAUNCHD_LABEL}") == 1
    # no stop/start, no console script at all
    assert calls.exists() is False or calls.read_text() == ""
    assert _invoked(env) == []
    assert "daemon restarted via launchd kickstart (pid 61218 -> 77001)" in res.stdout


def test_launchd_loaded_new_pid_comes_from_launchctl_not_pid_file(tmp_path):
    """Verification must read the pid launchd reports. The pid FILE is a shared
    label that an orphan fork can (and did) rewrite, so a restart verified
    against it can bless a process launchd does not supervise. Here the two
    disagree: the pid file still holds the OLD pid, launchd reports a new one —
    the helper must take launchd's and turn it over."""
    env, calls = _setup(tmp_path, launchd=True,
                        launchd_pid_before="61218", launchd_pid_after="77001",
                        preexisting_pid=61218)
    # Make the pid file permanently unhelpful: it keeps the OLD pid.
    Path(env["STUB_PID_FILE"]).write_text("61218")
    res = _run(env)

    assert res.returncode == 0
    assert "61218 -> 77001" in res.stdout
    # the stale pid file was never rewritten to the new pid, and that did not
    # stop the helper from verifying the real (launchd-owned) turnover
    assert Path(env["STUB_PID_FILE"]).read_text() == "61218"


def test_launchd_loaded_polls_until_launchd_reports_a_pid(tmp_path):
    """launchd reaps and re-spawns (ThrottleInterval spaces them out), so the
    first `print` may report no pid. The helper must poll rather than declare
    the daemon dead."""
    env, _ = _setup(tmp_path, launchd=True, launchd_pid_after=",77001")
    res = _run(env)

    assert res.returncode == 0
    assert "pid  -> 77001" not in res.stdout  # never a blank old->new line
    assert "77001" in res.stdout
    assert sum(1 for c in _launchd_calls(env) if c.startswith("print")) >= 2


def test_launchd_loaded_same_pid_reports_unchanged_rc2(tmp_path):
    """A kickstart whose job still reports the SAME pid did not turn the daemon
    over — the exact class of silent no-op this helper exists to catch (rc 2,
    never success)."""
    env, _ = _setup(tmp_path, launchd=True, launchd_pid_before="61218",
                    launchd_pid_after="61218")
    res = _run(env)

    assert res.returncode == 2
    assert "PID unchanged after restart" in res.stdout
    assert "launchd kickstart" in res.stdout


def test_launchd_loaded_no_pid_ever_rc3(tmp_path):
    """If launchd never reports a pid again the daemon did not come back: rc 3
    and a message that says so, so bootstrap warns instead of stamping a green
    install."""
    env, _ = _setup(tmp_path, launchd=True, launchd_pid_before="",
                    launchd_pid_after="")
    res = _run(env)

    assert res.returncode == 3
    assert "did not come back" in res.stdout
    assert "no pid" in res.stdout


def test_launchd_loaded_kickstart_failure_rc3_surfaces_reason(tmp_path):
    """A failing kickstart must not be swallowed: exit code and the tool's own
    stderr belong in the outcome line (the silent /dev/null hid the original
    ModuleNotFoundError)."""
    env, _ = _setup(tmp_path, launchd=True,
                    launchd_pid_before="61218", launchd_pid_after="77001",
                    launchd_kick_rc=1,
                    launchd_kick_err="Bootout unsuccessful: Resource busy")
    res = _run(env)

    assert res.returncode == 3
    assert "launchctl kickstart -k gui" in res.stdout
    assert "Bootout unsuccessful: Resource busy" in res.stdout


def test_launchd_loaded_job_with_no_pid_still_uses_kickstart(tmp_path):
    """The incident's shape: the job label is registered but has NO pid (the
    real loop escaped launchd via `hscc start`). That is still a loaded job, so
    kickstart is the answer — and the orphan is named."""
    env, calls = _setup(tmp_path, launchd=True, launchd_pid_after=",77001",
                        orphan_pids=[27723], preexisting_pid=27723)
    res = _run(env)

    assert f"kickstart -k gui/{os.getuid()}/{LAUNCHD_LABEL}" in _launchd_calls(env)
    assert calls.exists() is False or calls.read_text() == ""
    # supervised daemon came up, but the unsupervised twin is still live -> warn
    assert res.returncode == 4
    assert "27723" in res.stdout
    assert "launchd" in res.stdout


# ── the same rule for the OTHER supervisor: systemd --user (Linux) ──────────


def test_systemd_unit_active_restarts_through_systemctl_not_the_cli(tmp_path):
    """Linux is not "no supervisor": systemd-setup.sh runs the daemon as a
    --user unit, so the restart must go through `systemctl --user restart` and
    be verified from MainPID. Falling off to the console path here would (a)
    skip the reload this script exists for and (b) leave `hscc stop`/`start`
    fighting a unit systemd owns.
    """
    env, calls = _setup(tmp_path, systemd_unit="hscc_daemon.service",
                        systemd_pid_before="5001", systemd_pid_after="5002")
    res = _run(env)

    assert res.returncode == 0
    assert "restart hscc_daemon.service" in " ".join(_systemd_calls(env))
    assert calls.exists() is False or calls.read_text() == ""
    assert _invoked(env) == []
    assert "daemon restarted via systemd restart (pid 5001 -> 5002)" in res.stdout
    # and launchd must not have been *restarted* (probing it is fine, kicking a
    # non-existent job is not)
    assert not any(c.startswith("kickstart") for c in _launchd_calls(env))


def test_systemd_alternative_unit_name_is_recognised(tmp_path):
    """The two installers emit different unit names (systemd-setup.sh writes
    hscc_daemon.service, install.py writes com.hermes.hscc_daemon.service). If
    the helper recognised only one, a daemon installed by the other would look
    unsupervised — and the orphan guard would then REFUSE the restart, silently
    skipping the reload. Both names must be probed."""
    env, calls = _setup(tmp_path,
                        systemd_unit="com.hermes.hscc_daemon.service",
                        systemd_pid_before="6001", systemd_pid_after="6002")
    del env["HSCC_SYSTEMD_UNIT"]  # exercise the candidate-list probe, not the override
    res = _run(env)

    assert res.returncode == 0
    assert "restart com.hermes.hscc_daemon.service" in " ".join(_systemd_calls(env))
    assert calls.exists() is False or calls.read_text() == ""
    assert "pid 6001 -> 6002" in res.stdout


def test_systemd_supervised_pid_is_not_an_orphan(tmp_path):
    """The systemd-owned loop IS visible to pgrep (`... hscc.py start-daemon`),
    so excluding only launchd's pid would flag every Linux node's healthy daemon
    as an orphan and refuse its restart — the bug class this guard was written
    to prevent, turned against itself."""
    env, calls = _setup(tmp_path, systemd_unit="hscc_daemon.service",
                        systemd_pid_before="5001", systemd_pid_after="5002",
                        orphan_pids=[5001])
    res = _run(env)

    assert res.returncode == 0
    assert "WARN" not in res.stdout
    assert "daemon restarted via systemd restart (pid 5001 -> 5002)" in res.stdout
    assert calls.exists() is False or calls.read_text() == ""


def test_systemd_restart_failure_rc3_surfaces_reason(tmp_path):
    """A failed `systemctl --user restart` must surface rc + systemd's own
    text, exactly like the kickstart case (rc 3, never success)."""
    env, _ = _setup(tmp_path, systemd_unit="hscc_daemon.service",
                    systemd_pid_before="5001", systemd_pid_after="5002",
                    systemd_restart_rc=1,
                    systemd_restart_err="Job for hscc_daemon.service failed")
    res = _run(env)

    assert res.returncode == 3
    assert "systemctl --user restart hscc_daemon.service exited 1" in res.stdout
    assert "Job for hscc_daemon.service failed" in res.stdout


def test_systemd_same_mainpid_reports_unchanged_rc2(tmp_path):
    """If MainPID does not move, the unit was not restarted (rc 2)."""
    env, _ = _setup(tmp_path, systemd_unit="hscc_daemon.service",
                    systemd_pid_before="5001", systemd_pid_after="5001")
    res = _run(env)

    assert res.returncode == 2
    assert "PID unchanged after restart" in res.stdout
    assert "systemd restart" in res.stdout


def test_systemd_no_mainpid_ever_rc3(tmp_path):
    """Unit restarted but never came up: no MainPID => rc 3 and a line saying
    which supervisor stayed silent."""
    env, _ = _setup(tmp_path, systemd_unit="hscc_daemon.service",
                    systemd_pid_before="5001", systemd_pid_after="")
    res = _run(env)

    assert res.returncode == 3
    assert "did not come back" in res.stdout
    assert "systemd" in res.stdout


def test_systemd_path_ignores_launchd_stub_entirely(tmp_path):
    """When systemd owns the service the helper must not kick launchd even if a
    launchd job exists on that machine — two supervisors for one daemon is a
    misconfiguration, and the one that owns the running pid is the one we restart
    (launchd wins over systemd when both claim it; here only systemd does)."""
    env, _ = _setup(tmp_path, systemd_unit="hscc_daemon.service",
                    systemd_pid_before="5001", systemd_pid_after="5002")
    res = _run(env)

    assert res.returncode == 0
    assert "kickstart" not in " ".join(_launchd_calls(env))


def test_launchd_wins_when_both_supervisors_claim_the_service(tmp_path):
    """A machine where both a loaded launchd job AND an active systemd unit
    claim the daemon is a misconfiguration; the helper must pick ONE and say
    which it used rather than restarting twice (two restarts in one bootstrap
    step = two loops, i.e. the twin class again). The documented choice is
    launchd, whose `kickstart -k` is the stronger verb."""
    env, _ = _setup(tmp_path, launchd=True, launchd_pid_before="61218",
                    launchd_pid_after="77001",
                    systemd_unit="hscc_daemon.service",
                    systemd_pid_before="5001", systemd_pid_after="5002")
    res = _run(env)

    assert res.returncode == 0
    assert "launchd kickstart" in res.stdout
    assert "systemd" not in res.stdout
    assert not any(c.startswith("restart") for c in _systemd_calls(env))


# ── the twin check: never start a second loop beside an unsupervised daemon ──


def test_console_path_orphan_warns_and_does_not_start_a_second_loop(tmp_path):
    """No launchd job + a live daemon process nobody supervises == the 2026-10-29
    orphan class. The helper must refuse to start another one (no CLI invoked at
    all), name the pid, and hand over the supervised command."""
    env, calls = _setup(tmp_path, preexisting_pid=27723, orphan_pids=[27723])
    res = _run(env)

    assert res.returncode == 4
    assert calls.exists() is False or calls.read_text() == ""
    assert _invoked(env) == []
    assert "27723" in res.stdout
    assert "WARN" in res.stdout
    # the remedy it prints must be the operator rule, verbatim enough to paste
    assert f"launchctl kickstart -k gui/{os.getuid()}/{LAUNCHD_LABEL}" in res.stdout


def test_console_path_multiple_orphans_lists_every_pid(tmp_path):
    """Two orphans is the incident's actual state (27723 plus the litellm-era
    leftovers); the operator needs every pid, not the first one."""
    env, calls = _setup(tmp_path, orphan_pids=[32582, 40984])
    res = _run(env)

    assert res.returncode == 4
    assert "32582" in res.stdout and "40984" in res.stdout
    assert _invoked(env) == []


def test_console_path_no_orphan_proceeds_to_restart(tmp_path):
    """Counterpart to the guard: with no unsupervised daemon the fallback path
    must still do its job (stop + start), or the guard would be a blanket
    refusal."""
    env, calls = _setup(tmp_path, preexisting_pid=7, orphan_pids=[])
    res = _run(env)

    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "daemon restarted (pid 7 -> 8)" in res.stdout


def test_launchd_owned_pid_is_not_reported_as_orphan(tmp_path):
    """The pid launchd owns is the supervised daemon — by far the common case
    (pgrep always sees it) — so it must be excluded from the twin list, and the
    restart must read as clean."""
    env, _ = _setup(tmp_path, launchd=True,
                    launchd_pid_before="61218", launchd_pid_after="77001",
                    orphan_pids=[61218])
    res = _run(env)

    assert res.returncode == 0
    assert "WARN" not in res.stdout
    assert "daemon restarted via launchd kickstart (pid 61218 -> 77001)" in res.stdout


def test_twin_check_fails_open_when_pgrep_is_unavailable(tmp_path):
    """No pgrep (CI container, exotic OS) must NOT block an install: the guard
    finds nothing and the restart proceeds. A guard that refused to install
    where it cannot look is worse than the twin it hunts."""
    env, calls = _setup(tmp_path, preexisting_pid=7, orphan_pids=[27723])
    env["HSCC_PGREP"] = str(tmp_path / "definitely-not-pgrep")
    res = _run(env)

    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]


def test_orphan_guard_pattern_is_narrow():
    """The pgrep pattern must match only a daemon loop (`hscc start` /
    `hscc.py start-daemon`, `hscc` at a path/word boundary) and never `hscc api
    start`, a flightdeck `--start-daemon` flag, or an unrelated pytest/editor
    command line — a pattern that matched those would refuse every restart on a
    busy host, or conversely miss the real orphan.

    Checked against GNU/BSD `grep -E` (same POSIX ERE engine class pgrep -f
    uses) rather than Python `re`: Python has no POSIX classes, so `[[:space:]]`
    compiled by `re` would test a DIFFERENT pattern than the helper runs."""
    src = RESTART.read_text()
    line = [l for l in src.splitlines() if "PGREP_BIN" in l and "-f" in l]
    assert line, "helper must call the pgrep seam (HSCC_PGREP), not pgrep inline"
    import re as _re
    m = _re.search(r"-f\s+'([^']+)'", line[0])
    assert m, f"could not read the pattern out of: {line[0]!r}"
    pat = m.group(1)

    MATCH = [
        # launchd-supervised loop (the plist's own argv)
        "/Users/op/.hermes/hermes-agent/venv/bin/python "
        "/Users/op/.hermes/plugins/hscc_daemon/hscc.py start-daemon",
        # the incident: brew console script, orphaned, PPID 1
        "/opt/homebrew/Cellar/python/3.13/bin/python3.13 "
        "/opt/homebrew/bin/hscc start",
        # console script with an absolute path and no extension
        "/opt/homebrew/bin/hscc start",
    ]
    NO_MATCH = [
        "/opt/homebrew/bin/hscc api start",
        "/opt/homebrew/bin/hscc stop",
        "/opt/homebrew/bin/hscc start-daemon-something-else",
        "/usr/bin/python3 -m pytest hscc-bootstrap/tests -q",
        "bash /Users/op/.hermes/plugins/hscc-bootstrap/restart_daemon.sh",
        "vim /Users/op/.hermes/plugins/hscc_daemon/hscc.py",
        "hscc project chat my-project",
        "/bin/zsh -c flightdeck daemon --start-daemon",
    ]
    for argv, want in [(a, 1) for a in MATCH] + [(a, 0) for a in NO_MATCH]:
        # one grep -E per candidate: rc 0 == the pattern matches that argv
        rc = subprocess.run(
            ["grep", "-E", "-q", pat],
            input=argv + "\n", capture_output=True, text=True,
        ).returncode
        assert (rc == 0) == bool(want), (
            f"pattern {'should' if want else 'must NOT'} match: {argv}"
        )


# ── fallback path: the daemon's own CLI, unchanged semantics ────────────────


def test_issues_stop_then_start_and_verifies_pid_changed(tmp_path):
    """With no launchd job the restart goes through the daemon's own lifecycle:
    `hscc stop` then `hscc start`, and the PID must turn over (a same-PID
    "restart" is exactly the failure this helper was written for)."""
    env, calls = _setup(tmp_path, preexisting_pid=7)
    res = _run(env)

    assert res.returncode == 0
    assert calls.read_text().splitlines() == ["stop", "start"]
    assert "daemon restarted (pid 7 -> 8)" in res.stdout
    # warning-style output must NOT appear on success
    assert "warn" not in res.stdout and "did not come back" not in res.stdout
    # the console path may PROBE launchd (it has to, to learn the job is
    # absent) but must never kickstart from it — kickstart is only for a
    # loaded job, and this machine has none.
    assert not any(c.startswith("kickstart") for c in _launchd_calls(env))


def test_skip_daemon_does_not_attempt_restart(tmp_path):
    """bootstrap passes --skip when running with --skip-daemon; then the helper
    must not invoke stop/start at all and must succeed."""
    env, calls = _setup(tmp_path, launchd=True, launchd_pid_before="61218")
    res = subprocess.run(
        ["bash", str(RESTART), "--skip"],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert calls.exists() is False or calls.read_text() == ""
    assert _launchd_calls(env) == []
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
        res = _run(env, timeout=20)
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


# ── wiring / drift pins ────────────────────────────────────────────────────


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


def test_no_call_site_restarts_the_daemon_inline():
    """Every restart must route through restart_daemon.sh (or launchd). A caller
    that inlines `hscc stop`/`hscc start` re-imports the incident: brew python,
    no TCC grant, PPID 1. So no shipped shell script may start the daemon
    outside this helper."""
    offenders = []
    for sh in sorted(BOOT.glob("*.sh")):
        if sh.name == "restart_daemon.sh":
            continue
        for i, line in enumerate(sh.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for frag in ("hscc stop", "hscc start", "hscc.py start"):
                if frag in stripped:
                    offenders.append(f"{sh.name}:{i}: {stripped}")
    assert offenders == [], "daemon restart must go through restart_daemon.sh:\n" + "\n".join(offenders)


def test_launchd_label_constant_matches_sibling_emitters():
    """The label the helper kicks is the label launchd-setup.sh installs. If one
    moves and the other does not, kickstart targets a job that does not exist
    and the helper silently falls back to the console script — the incident,
    re-imported by a rename. Pin the three sites to one literal."""
    helper = RESTART.read_text()
    setup = (BOOT.parent / "hscc_daemon" / "launchd-setup.sh").read_text()
    install = (BOOT.parent / "hscc_daemon" / "install.py").read_text()
    assert f'HSCC_LAUNCHD_LABEL:-{LAUNCHD_LABEL}' in helper
    assert f'LABEL="{LAUNCHD_LABEL}"' in setup
    assert f'PLIST_FILE = os.path.join(PLIST_DIR, "{LAUNCHD_LABEL}.plist")' in install


def test_systemd_unit_candidates_match_sibling_emitters():
    """The helper probes a fixed candidate list of unit names, and the two
    installers emit DIFFERENT ones (pre-existing drift): systemd-setup.sh writes
    `hscc_daemon.service`, install.py writes `com.hermes.hscc_daemon.service`.
    If either changes and the candidate list is not updated, the helper stops
    seeing the service as supervised on that box — and then the orphan guard
    refuses the restart (rc 4) and freshly-installed code silently never loads.
    Pin every emitted unit name to appear in the probe list, so the drift can
    only ever be caught here, never in production."""
    helper = RESTART.read_text()
    m = re.search(r'SYSTEMD_UNIT_CANDIDATES="([^"]+)"', helper)
    assert m, "helper must keep its unit-name probe list declarative"
    candidates = m.group(1).split()

    setup = (BOOT.parent / "hscc_daemon" / "systemd-setup.sh").read_text()
    install = (BOOT.parent / "hscc_daemon" / "install.py").read_text()
    emitted = set()
    for pat, src in ((r'UNIT_NAME="([^"]+)"', setup),
                     (r'SYSTEMD_UNIT_NAME = "([^"]+)"', install)):
        found = re.search(pat, src)
        assert found, f"could not read the unit name out of {pat!r}"
        emitted.add(found.group(1))
    assert emitted == {"hscc_daemon.service", "com.hermes.hscc_daemon.service"}, (
        f"unit-name drift between the two installers: {sorted(emitted)}"
    )
    missing = emitted - set(candidates)
    assert not missing, (
        f"helper would treat a supervised daemon as an orphan; add to "
        f"SYSTEMD_UNIT_CANDIDATES: {sorted(missing)}"
    )


def test_helper_only_reads_and_kicks_launchd_never_reinstalls():
    """The plist and the job's bootstrap state are operator-owned runtime state
    (launchd-setup.sh / install.py write them). This helper may only LOOK at the
    job (`print`) and RESTART it (`kickstart`) — it may never bootout, bootstrap,
    load, unload or write a plist, because a bootout during an install leaves the
    job gone and the next step falls back to the console script (the incident).

    Asserted against the code lines' actual `$LAUNCHCTL <verb>` invocations, not
    prose: comments are allowed to *name* the forbidden commands (the WARN
    message hands the operator the bootout+bootstrap remedy)."""
    code = _code_lines_only()
    verbs = set(re.findall(r'\$LAUNCHCTL"?\s+([a-z][a-z-]*)', code))
    assert verbs == {"print", "kickstart"}, (
        f"helper may only read + kickstart the job, saw verbs: {sorted(verbs)}"
    )


def test_helper_never_installs_or_enables_the_systemd_unit():
    """The systemd counterpart of the pin above: the unit file belongs to
    systemd-setup.sh / install.py. The helper may ask whether the unit is active
    (`is-active`), read its pid (`show -p MainPID`) and restart it — never
    `enable`, `daemon-reload`, `stop`, `kill` or anything that writes the unit,
    since a mid-install enable/daemon-reload can change what systemd starts and
    would take the restart away from the operator's own definition."""
    code = _code_lines_only()
    verbs = set(re.findall(r'\$SYSTEMCTL"?\s+(?:--user\s+)?([a-z][a-z-]*)', code))
    assert verbs == {"is-active", "show", "restart"}, (
        f"helper may only query + restart the unit, saw verbs: {sorted(verbs)}"
    )
    # Every CALL (not the `command -v` presence probes) must go through the
    # --user manager, not just "somewhere in the file": a system-wide
    # `systemctl restart` of a --user unit targets a different (root-owned)
    # service or nothing at all, so a dropped `--user` on ONE call would
    # silently restart the wrong thing and still report a clean restart.
    calls = []
    for line in code.splitlines():
        if "command -v" in line:
            continue  # presence probe, not an invocation
        calls += re.findall(r'"\$SYSTEMCTL"\s+([^\n]*)', line)
    assert calls, "helper must invoke systemctl through the seam"
    bad = [c for c in calls if not c.startswith("--user")]
    assert not bad, f"every systemctl call must use the --user manager, found: {bad}"
