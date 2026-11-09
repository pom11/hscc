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
import re
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
HEARTBEAT = SCRIPTS / "leg_heartbeat.sh"
RUNNER = SCRIPTS / "run_tests.sh"

# The "leg": a script with a child, i.e. what pytest looks like to a watcher.
LEG_SH = "#!/usr/bin/env bash\nsleep {leg}\n"
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


def _lines(out):
    return [m for m in (LINE.match(l) for l in out.splitlines()) if m]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """One real leg, asserted on by several tests (each run costs leg_seconds)."""
    d = tmp_path_factory.mktemp("hb_live")
    return _run(d, interval=1, leg_seconds=3)


def test_streams_elapsed_and_cpu_to_a_redirected_file(live):
    m = _lines(live)
    assert len(m) >= 2, f"expected >=2 heartbeat lines in 3s at 1s cadence:\n{live!r}"
    m = m[0]
    assert m["leg"] == "demo"
    # The two numbers that together separate slow from stopped.
    assert int(m["elapsed"]) >= 1
    assert re.fullmatch(r"\d+:\d\d(\.\d+)?", m["cpu"]), f"cpu not `ps -o time` form: {m['cpu']!r}"


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
    assert '-p no:cacheprovider "$@"\n  code=$?' in src
