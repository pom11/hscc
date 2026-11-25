"""Bounded leg concurrency in scripts/run_tests.sh (t_6c2123f9).

What is being guarded is a claim about a STREAM, not about a speed: a pack of
concurrent legs must hand the queue the SAME stream a serial run hands it,
because two readers anchor on that stream rather than reading it —

  * ``merge_queue._TEST_FAILURE_RE`` / ``_LEG_FAILURE_RES`` build the handback
    digest from ``FAILED path::test`` and ``✗ <leg>`` at LINE START. Interleaved
    output from N legs breaks both, and the digest is what a red gate's author
    is handed;
  * the SIGTERM trap reports ``during suite: $CURRENT_SUITE`` (t_6bb29d46), and
    with a pack "the suite" is a set.

So every test here runs the SHIPPED runner end-to-end against a stub interpreter
(``HSCC_TEST_PY``), which is the seam the runner already offers, and asserts on
the redirected file — the same file a dispatcher reads. Nothing here needs a
real leg: the thing under test is scheduling and stream shape, not pytest.

The stub's plan format is ``<leg>:<sleep_secs>:<rc>`` per selected leg, so a test
can make the LAST leg in roster order the FASTEST one and still expect the blocks
in roster order. That inversion is the whole point of per-leg files.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RUNNER = SCRIPTS / "run_tests.sh"

# Real leg names: the runner rejects an unknown leg with exit 64 by design, so a
# stub leg name would test the rejection path instead of the scheduler.
LEG_A, LEG_B, LEG_C = "hscc-commands", "sparkrun-hermes", "memori_byodb"
THREE = f"{LEG_A} {LEG_B} {LEG_C}"

#: Written to a tmp dir and pointed at by HSCC_TEST_PY. It receives exactly what
#: the runner gives python (`-m pytest -q -rs <dir>/tests -p no:cacheprovider
#: -m not loadstress ...`), so it can report the argv it was handed and derive
#: its own leg name from the `<dir>/tests` argument.
STUB = """#!/usr/bin/env bash
set -u
tests_arg=""
for a in "$@"; do case "$a" in */tests) tests_arg="$a" ;; esac; done
leg="(none)"
[ -n "$tests_arg" ] && leg="$(basename "$(dirname "$tests_arg")")"
printf '%s\\t%s\\n' "$leg" "$*" >> "$STUB_ARGV"

secs=0.2; rc=0
for entry in $STUB_PLAN; do
  name="${entry%%:*}"; rest="${entry#*:}"
  if [ "$name" = "$leg" ]; then secs="${rest%%:*}"; rc="${rest#*:}"; break; fi
done

echo "start $leg $$" >> "$STUB_TRACE"
sleep "$secs"
echo "end $leg $$" >> "$STUB_TRACE"
if [ "$rc" -ne 0 ]; then
  echo "F"
  echo "FAILED $leg/tests/test_stub.py::test_${leg//-/_}_breaks - assert 1 == 2"
  echo "1 failed in ${secs}s"
else
  echo "."
  echo "7 passed in ${secs}s"
fi
exit "$rc"
"""


def _plan(spec):
    return " ".join(f"{leg}:{secs}:{rc}" for leg, secs, rc in spec)


def _run(tmp_path, *, plan, parallel=None, heartbeat=0, extra_argv=(), legs=THREE):
    """Execute the shipped runner against the stub, output REDIRECTED to a file.

    Redirected, never captured: a watcher that outlives a leg holds the file's
    write end open and a captured pipe then blocks — the harness would hang in
    exactly the way a real leg hangs (see test_leg_heartbeat.py for the same
    choice).
    """
    stub = tmp_path / "stub_py.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    trace = tmp_path / "trace.txt"
    argv = tmp_path / "argv.tsv"
    log = tmp_path / "run.log"
    trace.write_text("")
    argv.write_text("")
    env = dict(os.environ)
    env.update({
        "HSCC_TEST_PY": str(stub),
        "STUB_TRACE": str(trace),
        "STUB_ARGV": str(argv),
        "STUB_PLAN": plan,
        "HSCC_LEG_HEARTBEAT_SECONDS": str(heartbeat),
    })
    if parallel is None:
        env.pop("HSCC_TEST_PARALLEL", None)
    else:
        env["HSCC_TEST_PARALLEL"] = str(parallel)
    env["HSCC_TEST_LEGS"] = legs
    t0 = time.time()
    with open(log, "wb") as fh:
        proc = subprocess.run(["bash", str(RUNNER), *extra_argv],
                              stdout=fh, stderr=subprocess.STDOUT,
                              env=env, timeout=180, cwd=str(REPO))
    return {
        "rc": proc.returncode,
        "log": log.read_text(),
        "trace": [l for l in trace.read_text().splitlines() if l.strip()],
        "argv": [l.split("\t") for l in argv.read_text().splitlines() if l.strip()],
        "wall": time.time() - t0,
    }


def _in_flight(trace):
    """Max legs the trace shows live at once, plus the per-leg span."""
    live = peak = 0
    for line in trace:
        if line.startswith("start"):
            live += 1
            peak = max(peak, live)
        else:
            live -= 1
    return peak, live


# ── the bound ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("parallel,expected_peak", [(1, 1), (2, 2), (3, 3)])
def test_the_pack_never_exceeds_the_bound(tmp_path, parallel, expected_peak):
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.6, 0), (LEG_B, 0.6, 0), (LEG_C, 0.6, 0)]),
             parallel=parallel)
    peak, leaked = _in_flight(r["trace"])
    assert leaked == 0, f"a leg was started and never finished: {r['trace']}"
    assert peak <= parallel, f"{peak} legs in flight with HSCC_TEST_PARALLEL={parallel}"
    assert peak == expected_peak, (
        f"expected the bound to be REACHED (that is the speedup): peak={peak} "
        f"bound={parallel}\nlog={r['log']!r}")


def test_the_default_pack_is_two(tmp_path):
    """Unset must mean TWO, and must SAY so.

    Two because this host also runs the fleet control plane; the number has to
    be visible in the log or a reader cannot tell a 2-deep run from a 1-deep one
    after the fact.
    """
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.6, 0), (LEG_B, 0.6, 0), (LEG_C, 0.6, 0)]))
    assert re.search(r"^parallel: 2 legs at a time", r["log"], re.M), r["log"]
    peak, _ = _in_flight(r["trace"])
    assert peak == 2, r["trace"]


def test_a_pack_is_faster_than_serial_for_the_same_plan(tmp_path):
    """The point of the card, measured rather than asserted."""
    plan = _plan([(LEG_A, 1.0, 0), (LEG_B, 1.0, 0), (LEG_C, 1.0, 0)])
    serial = _run(tmp_path / _mk(tmp_path, "s"), plan=plan, parallel=1)
    pack = _run(tmp_path / _mk(tmp_path, "p"), plan=plan, parallel=3)
    assert serial["rc"] == 0 and pack["rc"] == 0
    assert pack["wall"] < serial["wall"] * 0.75, (
        f"3-deep {pack['wall']:.1f}s vs serial {serial['wall']:.1f}s")


def _mk(tmp_path, tag):
    d = tmp_path / f"run_{tag}"
    d.mkdir()
    return d


@pytest.mark.parametrize("value,note", [
    ("1", "running SERIALLY"),
    ("0", "running SERIALLY"),
    ("abc", "is not a non-negative integer"),
    ("-2", "is not a non-negative integer"),
])
def test_a_value_that_is_not_a_pack_falls_back_to_serial(tmp_path, value, note):
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.3, 0), (LEG_B, 0.3, 0), (LEG_C, 0.3, 0)]),
             parallel=value)
    assert note in r["log"], r["log"]
    peak, _ = _in_flight(r["trace"])
    assert peak == 1, f"{value!r} must be serial, saw {peak} in flight: {r['trace']}"


def test_the_stress_leg_is_never_packed(tmp_path):
    """load_burners.sh applies BOUNDED pressure; a neighbour is unbounded noise."""
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.3, 0)]), parallel=3,
             legs=f"{LEG_A} loadstress")
    assert "loadstress leg needs the box to itself" in r["log"], r["log"]
    peak, _ = _in_flight(r["trace"])
    assert peak == 1, r["trace"]


# ── the stream the queue parses ─────────────────────────────────────────────


def test_blocks_concatenate_in_roster_order_not_completion_order(tmp_path):
    """The last leg in roster order is the FASTEST one, and must still print last.

    Completion order would put the fast leg first and interleave the dot bands,
    which is precisely what breaks an anchored read.
    """
    r = _run(tmp_path, plan=_plan([(LEG_A, 1.2, 0), (LEG_B, 0.8, 0), (LEG_C, 0.2, 0)]),
             parallel=3)
    blocks = re.findall(r"^━━━ (\S+) ━━━$", r["log"], re.M)
    assert blocks[:3] == [LEG_A, LEG_B, LEG_C], f"blocks came out {blocks}: {r['log']}"
    # The last block is the one that finished first, and it must sit after the
    # one that finished last.
    assert r["log"].index(f"━━━ {LEG_C} ━━━") > r["log"].index(f"━━━ {LEG_A} ━━━")


def test_every_legs_output_arrives_exactly_once(tmp_path):
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.4, 0), (LEG_B, 0.2, 0), (LEG_C, 0.6, 0)]),
             parallel=3)
    assert r["log"].count("passed in") == 3, r["log"]
    for leg in (LEG_A, LEG_B, LEG_C):
        assert r["log"].count(f"━━━ {leg} ━━━") == 1, f"{leg} block printed twice"


def test_red_leg_digest_matches_the_serial_shape(tmp_path):
    """THE pin. The queue's digest of a packed run must name the same failures a
    serial run names — asserted against merge_queue's real regexes, not a copy.

    A pack that let another leg's dots land inside a short test summary would
    still print the FAILED text somewhere, so substring assertions would pass.
    These match at LINE START, which is how the queue reads them.
    """
    from hscc_daemon import merge_queue as mq

    plan = _plan([(LEG_A, 0.4, 0), (LEG_B, 0.9, 1), (LEG_C, 0.2, 0)])
    pack = _run(tmp_path / _mk(tmp_path, "pack"), plan=plan, parallel=3)
    serial = _run(tmp_path / _mk(tmp_path, "ser"), plan=plan, parallel=1)

    assert pack["rc"] == 1 and serial["rc"] == 1, "the red leg must fail the run"
    assert "FAILURES ABOVE" in pack["log"] and "ALL GREEN" not in pack["log"]

    row = f"✗ {LEG_B} (pytest exit 1)"
    assert any(l.strip() == row for l in pack["log"].splitlines()), pack["log"]

    packed = mq._failure_lines(pack["log"])
    unrolled = mq._failure_lines(serial["log"])
    assert packed == unrolled, (
        "the digest a packed gate hands back differs from the serial one:\n"
        f"  packed: {packed}\n  serial: {unrolled}")
    assert any(l.startswith("✗") for l in packed), f"no leg row in {packed}"
    assert any(l.startswith("FAILED ") for l in packed), f"no test line in {packed}"
    assert any(f"{LEG_B}/tests/test_stub.py::" in l for l in packed), packed


def test_summary_rows_keep_the_serial_wording(tmp_path):
    """`  ✓ <leg>` / `  ✗ <leg> (pytest exit N)` verbatim — the queue anchors `^✗`."""
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 1), (LEG_C, 0.2, 0)]),
             parallel=3)
    summary = r["log"].split("━━━ Summary ━━━")[1]
    assert f"  ✓ {LEG_A}" in summary
    assert f"  ✗ {LEG_B} (pytest exit 1)" in summary
    assert f"  ✓ {LEG_C}" in summary


def test_pack_and_serial_hand_pytest_the_same_argv(tmp_path):
    """Flag drift between the two branches is the silent kind: one posture would
    quietly stop deselecting `loadstress`, or stop forwarding extra argv."""
    plan = _plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0)])
    pack = _run(tmp_path / _mk(tmp_path, "p"), plan=plan, parallel=3,
                extra_argv=("-k", "not slow"))
    serial = _run(tmp_path / _mk(tmp_path, "s"), plan=plan, parallel=1,
                  extra_argv=("-k", "not slow"))
    by_leg = lambda r: {leg: a for leg, a in r["argv"]}
    assert by_leg(pack) == by_leg(serial), (
        f"argv drift:\n  pack:   {by_leg(pack)}\n  serial: {by_leg(serial)}")
    for leg, a in by_leg(pack).items():
        assert "-m not loadstress" in a.replace('"', ""), f"{leg}: {a}"
        assert "-k not slow" in a.replace('"', ""), f"operator argv lost: {a}"


# ── liveness: the reason the heartbeat exists ───────────────────────────────

HEARTBEAT_LINE = re.compile(
    r"^\[leg-heartbeat\] leg=(?P<leg>\S+) elapsed=(?P<elapsed>\d+)s "
    r"pytest_pid=(?P<pid>\d+) cpu=(?P<cpu>\S+) children=(?P<kids>\d+) "
    r"first_child=(?P<kid>.+)$")


def test_heartbeat_names_every_live_leg_separately(tmp_path):
    """Ambiguous discovery, resolved by NAMING, not by dropping the signal.

    With three legs in flight, `pgrep -P <runner>` has three answers; the naive
    generalisation reports one of them as THE leg, which sends a reader to the
    wrong (possibly healthy) process. Each live leg must get its own line, with
    its own pytest pid and its own elapsed clock.
    """
    r = _run(tmp_path, plan=_plan([(LEG_A, 3.0, 0), (LEG_B, 3.0, 0), (LEG_C, 3.0, 0)]),
             parallel=3, heartbeat=1)
    ms = [m for m in (HEARTBEAT_LINE.match(l) for l in r["log"].splitlines()) if m]
    assert ms, f"no heartbeat lines at all:\n{r['log']}"
    by_leg = {}
    for m in ms:
        by_leg.setdefault(m["leg"], set()).add(m["pid"])
    for leg in (LEG_A, LEG_B, LEG_C):
        assert leg in by_leg, f"{leg} never reported; lines={[(m['leg'], m['pid']) for m in ms]}"
    names = {m["leg"] for m in ms}
    assert "roster" not in names, "the watcher must name real legs, not the pack"
    for leg, pids in by_leg.items():
        assert len(pids) == 1, f"{leg} reported two pytest pids: {pids}"


def test_heartbeat_stops_when_the_pack_ends(tmp_path):
    """A watcher that outlives the run holds the log open — the original
    'leg that cannot finish' failure mode, now multiplied by N."""
    r = _run(tmp_path, plan=_plan([(LEG_A, 1.0, 0), (LEG_B, 1.0, 0)]),
             parallel=2, heartbeat=1)
    tail = r["log"].split("━━━ Summary ━━━")[-1]
    assert "[leg-heartbeat]" not in tail, tail


def test_sigterm_names_the_live_legs_and_leaves_none_behind(tmp_path):
    """t_6bb29d46 still holds: rc=143 must be explainable from the log alone, and
    a pack must not survive its runner as N orphan pytests."""
    stub = tmp_path / "stub_py.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    trace, argv, log = tmp_path / "t", tmp_path / "a", tmp_path / "run.log"
    trace.write_text("")
    argv.write_text("")
    env = dict(os.environ)
    env.update({
        "HSCC_TEST_PY": str(stub), "STUB_TRACE": str(trace), "STUB_ARGV": str(argv),
        "STUB_PLAN": _plan([(LEG_A, 30, 0), (LEG_B, 30, 0), (LEG_C, 30, 0)]),
        "HSCC_TEST_LEGS": THREE, "HSCC_TEST_PARALLEL": "2",
        "HSCC_LEG_HEARTBEAT_SECONDS": "0",
    })
    with open(log, "wb") as fh:
        p = subprocess.Popen(["bash", str(RUNNER)], stdout=fh, stderr=subprocess.STDOUT,
                             env=env, cwd=str(REPO), start_new_session=True)
        deadline = time.time() + 30
        while time.time() < deadline and len(trace.read_text().splitlines()) < 2:
            time.sleep(0.1)
        stub_pids = [l.split()[-1] for l in trace.read_text().splitlines()
                     if l.startswith("start")]
        p.terminate()
        try:
            rc = p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait(timeout=30)
    out = log.read_text()
    assert rc == 143, f"rc={rc}\n{out}"
    assert "RECEIVED SIGTERM" in out, out
    m = re.search(r"during suite: (.*)$", out, re.M)
    assert m, out
    named = set(m.group(1).split())
    assert named == {LEG_A, LEG_B}, f"SIGTERM named {named}, expected the two live legs"
    left = [pid for pid in stub_pids
            if subprocess.run(["ps", "-o", "stat=", "-p", pid],
                              capture_output=True, text=True).stdout.strip()
            not in ("", "Z")]
    assert not left, f"legs survived the kill: {left}"


# ── hygiene ─────────────────────────────────────────────────────────────────


def test_the_per_leg_logs_do_not_pile_up_in_tmp(tmp_path):
    """A gate runs on a shared host; N files per run must not be left behind."""
    before = set(Path("/tmp").glob("hscc-legs-*"))
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0)]), parallel=2)
    m = re.search(r"leg logs in (/tmp/hscc-legs-\S+)", r["log"])
    assert m, r["log"]
    assert not Path(m.group(1)).exists(), f"left behind: {m.group(1)}"
    assert set(Path("/tmp").glob("hscc-legs-*")) == before, "tmp litter"


def test_heartbeat_seconds_zero_silences_the_watcher_in_a_pack(tmp_path):
    r = _run(tmp_path, plan=_plan([(LEG_A, 1.5, 0), (LEG_B, 1.5, 0)]),
             parallel=2, heartbeat=0)
    assert "[leg-heartbeat]" not in r["log"]
    assert "parallel: done leg=" in r["log"], "the live completion line is the other signal"


def test_completion_lines_are_prefixed_so_they_cannot_join_the_digest(tmp_path):
    """`parallel: done leg=… rc=1` says a leg failed. It must NOT be swept into
    the handback digest as if it were a leg row — that is what `✗ <leg>` is for.
    """
    from hscc_daemon import merge_queue as mq

    r = _run(tmp_path, plan=_plan([(LEG_A, 0.2, 1), (LEG_B, 0.2, 0), (LEG_C, 0.2, 0)]),
             parallel=3)
    assert any(l.startswith("parallel: done leg=") for l in r["log"].splitlines())
    assert not [l for l in mq._failure_lines(r["log"]) if "parallel:" in l], r["log"]
