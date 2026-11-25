"""The leg heartbeat (scripts/leg_heartbeat.sh) — t_ecb84764.

What is being tested is a claim about a REDIRECTED file: an outside reader of a
leg's log must be able to tell "slow" from "stopped". So every assertion reads a
file, never a tty, exactly as a dispatcher does.

Two things the harness has to get right, both learned the hard way here:

  * the fake runner reproduces run_tests.sh's SHAPE, not a simplified one — the
    heartbeat is started BY the runner (so it is a child of the watched pid,
    which is what exercises the exclude-self guard: a naive
    `pgrep -P | head -1` picks the watcher, which has the lower pid), and the
    "leg" is itself a script with a child, so the `first_child=` field has
    something real to name;
  * output goes to a FILE, never capture_output. A watcher that outlives its leg
    holds the log's write end open, and `subprocess.run(capture_output=...)`
    then blocks until it closes — a test-shaped version of the very "leg that
    cannot finish" failure mode this file is about.
"""
import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
HEARTBEAT = SCRIPTS / "leg_heartbeat.sh"
RUNNER = SCRIPTS / "run_tests.sh"

# The "leg": a script with a child, i.e. what pytest looks like to a watcher.
LEG_SH = "#!/usr/bin/env bash\nsleep {leg}\n"

# ── the live leg is OBSERVER-driven, not timer-driven (t_49827b5d) ──────────
# The `live` fixture used to be `sleep 3` at a 1 s cadence and the two
# `>= 2 heartbeat lines` assertions read the log after that fixed 3 s. Under
# the queue gate's background QoS that shape broke twice (#734 reported
# "expected >=2 heartbeat lines in 3s at 1s cadence: got [1]"): a heartbeat
# tick is not one sleep, it is `sleep 1` PLUS the `date`/`ps`/`pgrep` forks it
# runs to build the line, and those forks are CPU work — exactly what a
# throttled scheduler stretches. A fixed wall-clock window therefore measures
# the scheduler, not the watcher, and a leg made longer to compensate is a
# slower suite for everyone.
#
# So the leg now runs long (LEG_LIVE_CAP) and the TEST decides when it is done:
# poll the redirected log, and as soon as it holds MIN_LIVE_LINES heartbeat
# lines, terminate the leg's process group. Same on a fast box (it stops after
# ~2 s instead of 3), bounded under throttle (it waits as long as the
# scheduler needs, up to OBSERVE_DEADLINE_SECONDS), and the assertion is then
# about the watcher's OUTPUT, not about winning a race with the CPU allocator.
# Killing the whole group is also what keeps a throttled run from leaking a
# `sleep 40` past the test.
MIN_LIVE_LINES = 2
OBSERVE_DEADLINE_SECONDS = 90.0
LEG_LIVE_CAP_SECONDS = 40
# The runner: heartbeat as its own child + a foreground leg + reap. Mirrors the
# loop body of scripts/run_tests.sh.
RUNNER_SH = (
    "#!/usr/bin/env bash\n"
    "bash {hb} demo $$ \"$(date +%s)\" {iv} & hb=$!\n"
    "bash {leg}\n"
    "kill $hb 2>/dev/null; wait $hb 2>/dev/null; true\n"
)
# A runner that leaves its heartbeat orphaned and is itself never reaped: the
# watcher sees a ZOMBIE, and kill -0 on a zombie succeeds.
ORPHAN_SH = (
    "#!/usr/bin/env bash\n"
    "bash {hb} demo $$ \"$(date +%s)\" {iv} &\n"
    "sleep 1\n"
    "exit 0\n"
)

LINE = re.compile(
    r"^\[leg-heartbeat\] leg=(?P<leg>\S+) elapsed=(?P<elapsed>\d+)s "
    r"pytest_pid=(?P<pid>\d+) cpu=(?P<cpu>\S+) children=(?P<kids>\d+) "
    r"first_child=(?P<kid>.+)$"
)


def _runner_script(tmp_path, interval, leg_seconds, name="runner"):
    leg = tmp_path / f"{name}_leg.sh"
    leg.write_text(LEG_SH.format(leg=leg_seconds))
    runner = tmp_path / f"{name}.sh"
    runner.write_text(RUNNER_SH.format(hb=HEARTBEAT, leg=leg, iv=interval))
    return runner


def _run(tmp_path, interval, leg_seconds, name="runner"):
    """Run a fake leg with its log REDIRECTED to a file, and return the log."""
    log = tmp_path / f"{name}.log"
    with open(log, "wb") as fh:
        subprocess.run(["bash", str(_runner_script(tmp_path, interval,
                                                   leg_seconds, name))],
                       stdout=fh, stderr=subprocess.STDOUT, timeout=60)
    return log.read_text()


def _run_until_lines(tmp_path, interval, min_lines, name="hb_live"):
    """Run a long fake leg and STOP it once its log has `min_lines` heartbeats.

    The leg's length is a ceiling, not a schedule: the observer ends it. That
    is what makes the cadence assertions scheduler-independent — a background
    QoS run just takes longer to reach the same two lines, it does not take
    fewer of them. Returns the log as the dispatcher would read it.
    """
    log = tmp_path / f"{name}.log"
    with open(log, "wb") as fh:
        proc = subprocess.Popen(
            ["bash", str(_runner_script(tmp_path, interval,
                                        LEG_LIVE_CAP_SECONDS, name))],
            stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + OBSERVE_DEADLINE_SECONDS
        seen = 0
        while time.time() < deadline:
            seen = len(_lines(log.read_text()))
            if seen >= min_lines:
                break
            time.sleep(0.1)
        # Terminate the whole group (runner + leg + heartbeat) so a leg that
        # was never reaped cannot outlive the test holding the log's write end.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:                 # pragma: no cover
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=30)
    out = log.read_text()
    assert len(_lines(out)) >= min_lines or seen >= min_lines, (
        f"the watcher printed {seen} line(s) in {OBSERVE_DEADLINE_SECONDS}s at "
        f"a {interval}s cadence — it is not ticking at all:\n{out!r}")
    return out


def _lines(out):
    return [m for m in (LINE.match(l) for l in out.splitlines()) if m]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """One real leg, asserted on by several tests.

    The leg runs until this fixture has SEEN `MIN_LIVE_LINES` heartbeat lines
    (see the OBSERVE block above) — so the cost is ~2 s on a normal box and
    however long a throttled scheduler needs, never a fixed wall-clock gamble.
    """
    d = tmp_path_factory.mktemp("hb_live")
    return _run_until_lines(d, interval=1, min_lines=MIN_LIVE_LINES)


def test_streams_elapsed_and_cpu_to_a_redirected_file(live):
    m = _lines(live)
    assert len(m) >= 2, f"expected >=2 heartbeat lines in 3s at 1s cadence:\n{live!r}"
    m = m[0]
    assert m["leg"] == "demo"
    # The two numbers that together separate slow from stopped.
    assert int(m["elapsed"]) >= 1
    # `ps -o time` is platform-shaped: macOS prints MM:SS.ss, Linux prints
    # HH:MM:SS (and DD-HH:MM:SS past a day). All are valid cpu-time output.
    assert re.fullmatch(r"(?:\d+-)?(?:\d+:)?\d+:\d\d(?:\.\d+)?", m["cpu"]), \
        f"cpu not `ps -o time` form: {m['cpu']!r}"


def test_names_the_child_the_leg_is_waiting_on(live):
    m = _lines(live)[0]
    assert m["kids"] == "1"
    assert "sleep" in m["kid"], live


def test_the_watcher_never_reports_itself(live):
    """It is a child of the watched pid and has the LOWER pid — a naive
    `pgrep -P | head -1` would watch itself instead of the leg."""
    for m in _lines(live):
        assert "leg_heartbeat" not in m["kid"], live
        assert m["kid"].strip().startswith("sleep"), live


def test_elapsed_is_monotonic(live):
    elapsed = [int(m["elapsed"]) for m in _lines(live)]
    assert elapsed == sorted(elapsed) and len(elapsed) >= 2, elapsed


@pytest.mark.parametrize("interval", ["0", "", "abc", "-1"])
def test_a_disabled_cadence_prints_nothing(tmp_path, interval):
    out = _run(tmp_path, interval=interval, leg_seconds=1)
    assert "[leg-heartbeat]" not in out, out


def test_an_orphaned_watcher_dies_with_its_runner(tmp_path):
    """The watcher must not outlive the leg.

    The runner here exits and is deliberately NOT reaped, so the watcher sees a
    zombie. `kill -0` on a zombie SUCCEEDS, which is why the gate also checks
    for state Z: without that, this is a watcher printing a dead pid forever and
    holding the log's fd open — manufacturing a "leg that cannot finish".
    """
    d = tmp_path
    log = d / "orphan.log"
    script = d / "orphan.sh"
    script.write_text(ORPHAN_SH.format(hb=HEARTBEAT, iv=1))
    fh = open(log, "wb")
    p = subprocess.Popen(["bash", str(script)], stdout=fh, stderr=subprocess.STDOUT,
                         start_new_session=True)
    deadline = time.time() + 8
    left = []
    while time.time() < deadline:
        time.sleep(0.3)
        left = subprocess.run(["pgrep", "-f", f"{HEARTBEAT} demo"],
                              capture_output=True, text=True).stdout.split()
        if not left:
            break
    p.wait(timeout=30)
    fh.close()
    assert not left, f"heartbeat outlived its runner: {left}\nlog={log.read_text()!r}"


def test_runner_wires_the_heartbeat():
    """Pin the wiring, not the prose — without these the mechanism silently dies."""
    src = RUNNER.read_text()
    assert "scripts/leg_heartbeat.sh" in src
    assert "HSCC_LEG_HEARTBEAT_SECONDS:-30}" in src, "default cadence changed"
    assert "HB_PID=$!" in src
    assert 'kill "$HB_PID"' in src, "heartbeat must be reaped after each leg"
    # pytest must stay FOREGROUND: a backgrounded job in a non-interactive shell
    # gets SIG_IGN on SIGINT, so Ctrl-C would stop reaching the suite.
    # Re-pinned as a property of the invocation rather than one exact line:
    # t_b0bcc49c put the stress leg's command in an `if` beside this one and the
    # marker deselect wrapped onto a continuation line, so the old literal
    # `'-p no:cacheprovider "$@"\n  code=$?'` stopped matching while every
    # property it stood for still held. What it actually guarded is pinned
    # directly now — the status captured here, the caller's argv still last, and
    # nothing in the invocation backgrounded.
    assert "-p no:cacheprovider" in src
    invocation = src.split('"$PY" -m pytest', 1)[1].split("code=$?", 1)[0]
    assert '"$@"' in invocation, (
        "the runner's extra argv no longer reaches pytest — and `code=$?` on the "
        "other side of this block means a leg that ignores it could not report "
        "what it actually ran")
    assert "&\n" not in invocation and not invocation.rstrip().endswith("&"), (
        "something in the pytest invocation is backgrounded: Ctrl-C stops "
        "reaching the suite, which is what this pin exists for")


# ── the multi-leg watcher (t_6c2123f9) ──────────────────────────────────────
# A pack has N legs in flight, so "the leg's pytest" is no longer one answer to
# `pgrep -P <runner>`. The mode the runner now uses names every live leg; these
# two pins keep the seam from silently collapsing back to one.


def test_pack_wires_the_watcher_to_the_leg_file():
    """Pin BOTH sides of the fifth argument. A runner that packs legs but still
    calls the watcher with four gets one line about whichever child `pgrep`
    returns first — which reads as a healthy leg while the slow one is unnamed.
    """
    src = RUNNER.read_text()
    calls = re.findall(r'bash "\$ROOT/scripts/leg_heartbeat\.sh"[^\n]*', src)
    assert len(calls) == 2, f"expected one serial and one pack call, got {calls}"
    assert [c for c in calls if "LEG_PIDFILE" in c], f"the pack passes no leg file: {calls}"
    assert [c for c in calls if '"$d"' in c], f"the serial path lost its leg name: {calls}"
    hb = HEARTBEAT.read_text()
    assert 'LEGS_FILE="${5:-}"' in hb, "leg_heartbeat.sh lost its 5th argument"
    assert 'done < "$LEGS_FILE"' in hb, "the watcher no longer iterates the leg file"


def test_multi_leg_mode_reports_each_live_leg_once(tmp_path):
    """Two real concurrent legs, one watcher, output REDIRECTED to a file.

    Both leg names must appear, each with exactly one pytest pid, and no line may
    name the watcher — the ambiguity this mode exists to remove, asserted rather
    than described.

    Each leg is a SCRIPT WITH A CHILD, the way the real leg is a subshell whose
    child is pytest: the watcher names a leg by its CHILD, so a bare `sleep`
    would test the parsing and not the discovery.
    """
    log = tmp_path / "multi.log"
    legs_file = tmp_path / "legs.tsv"
    legs_file.write_text("")
    (tmp_path / "leg.sh").write_text("#!/usr/bin/env bash\nsleep 4\n")
    runner = tmp_path / "runner.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        f"bash {HEARTBEAT} roster $$ \"$(date +%s)\" 1 {legs_file} & hb=$!\n"
        "bash L1 & printf 'alpha\\t%s\\t%s\\n' \"$!\" \"$(date +%s)\" >> LOGS\n"
        "bash L2 & printf 'beta\\t%s\\t%s\\n' \"$!\" \"$(date +%s)\" >> LOGS\n"
        "sleep 3; kill $hb 2>/dev/null; true\n"
        .replace("L1", str(tmp_path / "leg.sh"))
        .replace("L2", str(tmp_path / "leg.sh"))
        .replace("LOGS", str(legs_file))
    )
    with open(log, "wb") as fh:
        subprocess.run(["bash", str(runner)], stdout=fh,
                       stderr=subprocess.STDOUT, timeout=60)
    out = log.read_text()
    ms = [m for m in (LINE.match(l) for l in out.splitlines()) if m]
    by_leg = {}
    for m in ms:
        by_leg.setdefault(m["leg"], set()).add(m["pid"])
    assert set(by_leg) == {"alpha", "beta"}, f"expected both legs, got {out!r}"
    for leg, pids in by_leg.items():
        assert len(pids) == 1, f"{leg} reported two pids: {pids}"
    assert all("leg_heartbeat" not in m["kid"] for m in ms), out
    # Each leg's elapsed clock is its OWN, measured from when THAT leg started.
    assert all(int(m["elapsed"]) >= 1 for m in ms), out
