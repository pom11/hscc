"""Guard: a load test must not be able to leave its burners behind (t_71ed3b1c).

The incident this pins: on 2026-11-23 a load test spawned twelve
``while :; do :; done`` burners and its ``kill $BURNERS`` never reached them.
They ran as orphans at ~90% CPU each and held the box at load 24 for 3h40m.

The behavioural arms below spawn the real ``scripts/load_burners.sh`` harness,
wait until its burners exist (non-vacuity: the pgrep must find them first), then
kill the HARNESS and assert pgrep finds zero survivors. KILL is deliberately
not one of the signals: no process can trap SIGKILL, so the contract is the one
the card names — EXIT/INT/TERM — plus HUP. The timeout arm proves the watchdog
fires even when the command under test is a ``sleep`` that would happily run
for a minute.

The last arm is a static audit: an inline burner loop in a test file is the
copy-paste that re-creates the incident, so it must use the harness instead.
It is prospective (there are no such loops in the tree today) and has its own
non-vacuity probe.
"""

import contextlib
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "scripts" / "load_burners.sh"


def _survivors(tag):
    """PIDs whose argv still carries ``tag`` — i.e. live burners."""
    proc = subprocess.run(
        ["pgrep", "-f", tag], capture_output=True, text=True)
    if proc.returncode == 1:                     # no match: the green state
        return []
    assert proc.returncode == 0, (
        "pgrep could not answer (rc=%d): %r" % (proc.returncode, proc.stderr))
    return [int(pid) for pid in proc.stdout.split()]


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@contextlib.contextmanager
def _running(burners, timeout, command):
    """Start the harness, and make a failed assertion leave no burners either."""
    tag = "hscc-load-test-%s" % uuid.uuid4().hex
    env = {**os.environ, "HSCC_LOAD_BURNER_TAG": tag}
    proc = subprocess.Popen(
        ["/bin/sh", str(HARNESS), str(burners), str(timeout), "--", *command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env, start_new_session=True, preexec_fn=_born_able_to_trap)
    try:
        yield proc, tag
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        # Belt and braces for a red assertion: kill the groups this test saw.
        for pid in _survivors(tag):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _burners_of(tag, count):
    return _wait_for(lambda: len(_survivors(tag)) == count)


# The harness traps INT, TERM and HUP — and a POSIX shell is NOT ALLOWED to trap
# a signal it was born ignoring (POSIX shell specification; bash 3.2 obeys).
# Inherited ignores are sticky: `execve` preserves them, CPython does not
# install its own SIGINT handler over one, and every child of the suite
# inherits them again.
#
# Which makes the arm below depend on how the GATE happened to start the suite.
# A command run asynchronously by a non-interactive shell -- `( pytest ... ) &`,
# which is exactly how scripts/run_tests.sh starts every leg in parallel mode,
# and how every reproduction of this stall was launched -- is required to start
# with SIGINT (and SIGQUIT) ignored. Measured on this host, in that shape:
#
#   async  sh -c '... &'  -> python: <Handlers.SIG_IGN: 1>  -> harness has no
#                                                             INT handler, and
#                                                             the kernel
#                                                             DISCARDS the INT
#   foreground sh -c     -> python: <built-in function default_int_handler>
#                                                             -> trap works
#
# That is the whole flake: the same file passes foreground and fails 5/5
# backgrounded, at load 3-4 on 14 cpus, with no timing term anywhere (the
# stalled harness has no internal end -- it dies only at its own 60 s watchdog).
# The harness is not stuck in `wait`, it never HAD a handler to run: card
# t_44a09412 read a stalled harness's dispositions from outside and got
# `ignored INT=1 / caught INT=0` with `caught TERM=1 HUP=1` (the TERM rescue trap
# did run), i.e. the kernel discarded the SIGINT. See
# docs/audits/load-burners-sigint-trace-stage_t_5f9fab03.md.
#
# So the test resets the dispositions it is about to depend on, in the child
# between fork and exec, at the last link it owns. The harness is then born able
# to trap what the test delivers, whatever shape the suite was started in -- no
# retry, no skip, no widened timeout, and no change to the harness itself.
# test_the_reset_is_what_the_int_arm_rests_on() below is what pins it.
def _born_able_to_trap():
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)


def test_burners_run_in_their_own_process_groups():
    """The group is the fix: a burner must lead its own PGID, not share ours."""
    with _running(2, 60, ["sleep", "60"]) as (proc, tag):
        assert _burners_of(tag, 2), "harness never started two tagged burners"
        pids = _survivors(tag)
        ours = os.getpgid(proc.pid)
        for pid in pids:
            pgid = os.getpgid(pid)
            assert pgid == pid, (
                "burner %d shares PGID %d instead of leading its own — "
                "`kill -- -PGID` would miss it" % (pid, pgid))
            assert pgid != ours


@pytest.mark.parametrize("sig,status", [
    (signal.SIGTERM, 128 + signal.SIGTERM),
    (signal.SIGINT, 128 + signal.SIGINT),
    (signal.SIGHUP, 128 + signal.SIGHUP),
])
def test_signal_to_the_harness_takes_every_burner_with_it(sig, status):
    with _running(2, 60, ["sleep", "60"]) as (proc, tag):
        assert _burners_of(tag, 2), "no burners to prove the trap against"
        proc.send_signal(sig)
        assert proc.wait(timeout=10) == status
        assert _wait_for(lambda: not _survivors(tag)), (
            "burners %r survived %s of the harness — the exact orphan shape "
            "t_71ed3b1c exists to prevent" % (_survivors(tag), sig.name))


def test_the_reset_is_what_the_int_arm_rests_on():
    """Non-vacuity for the launcher reset: force the bad birth, then trap anyway.

    The arm above only proves the harness traps INT when the suite happened to be
    born able to deliver it — which is why the same file passed 60/60 launched in
    the foreground and failed 5/5 launched as `( … ) &`. This case removes the
    confound: it puts the *suite process itself* in the state an asynchronous
    command of a non-interactive shell is born in (SIGINT ignored), launches the
    harness from there, and still requires INT to be trapped.

    Without `_born_able_to_trap` the ignore is inherited across `execve`, bash 3.2
    cannot `trap` it, the kernel discards the SIGINT, and THIS case goes red in
    every launch shape — so the reset cannot be quietly dropped later. The
    disposition is restored in a finally; nothing else in the suite sees it.
    """
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with _running(2, 60, ["sleep", "60"]) as (proc, tag):
            assert _burners_of(tag, 2), "no burners to prove the trap against"
            proc.send_signal(signal.SIGINT)
            assert proc.wait(timeout=10) == 128 + signal.SIGINT, (
                "the harness was born with SIGINT ignored and the launcher reset "
                "did not undo it: the INT was discarded, not delivered")
            assert _wait_for(lambda: not _survivors(tag)), (
                "burners %r survived the reset INT" % (_survivors(tag),))
    finally:
        signal.signal(signal.SIGINT, previous)


def test_hard_timeout_fires_and_kills_the_burners():
    started = time.monotonic()
    with _running(2, 1, ["sleep", "60"]) as (proc, tag):
        assert proc.wait(timeout=10) == 124, "timeout must report 124, not hang"
        elapsed = time.monotonic() - started
        assert elapsed < 10, "the hard timeout did not bound the run: %.1fs" % elapsed
        assert _wait_for(lambda: not _survivors(tag)), (
            "burners %r survived the hard timeout" % _survivors(tag))


def test_command_status_is_preserved():
    with _running(1, 10, ["sh", "-c", "exit 7"]) as (proc, tag):
        assert proc.wait(timeout=10) == 7
        assert _wait_for(lambda: not _survivors(tag))


# ── static audit: the copy-paste that re-created the incident ───────────────
#
# A future load test that inlines its own spinner bypasses the harness and its
# traps. The pattern is split so this file does not match itself.
_BURNER_LOOP = re.compile(
    r"while\s+(?::|true)\s*;\s*do" r"\s+:\s*;\s*done")


def _test_sources():
    skip = {".git", ".worktrees", "_archive", ".venv", "node_modules",
            "__pycache__"}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        if "tests" not in Path(root).parts:
            continue
        for name in files:
            if name.endswith((".py", ".sh")):
                yield Path(root) / name


def test_no_test_inlines_its_own_burner_loop():
    """Non-vacuity first: the detector must bite on the incident's shape."""
    assert _BURNER_LOOP.search("while " + ":; do :; done")
    assert _BURNER_LOOP.search("while true; do :; done")

    me = Path(__file__).resolve()
    offenders = [str(path) for path in _test_sources()
                 if path.resolve() != me
                 and _BURNER_LOOP.search(path.read_text(errors="replace"))]
    assert not offenders, (
        "test source(s) inline a CPU burner loop instead of using "
        "scripts/load_burners.sh (own process group + trap + hard timeout): %s"
        % ", ".join(offenders))
