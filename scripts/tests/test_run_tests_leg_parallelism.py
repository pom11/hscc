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

#: The one roster leg whose NAME is a nested path, and therefore the only leg
#: that can prove a leg log is a FILE and not a directory component. Every other
#: leg name is one flat component, so a log path built by splicing the name in
#: raw works for eleven legs and fails silently for this one: `…/1-.github/scripts.log`
#: names a file inside a directory that was never created, the redirect fails,
#: the leg never runs, and it reads red with no FAILED line to capture. That is
#: how `.github/scripts` was red on every packed run of t_6c2123f9's roster.
LEG_SLASHY = ".github/scripts"

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


#: The three tokens a packed stream legitimately carries that differ per run and
#: say nothing about its SHAPE: the two mktemp dirs the banners name, and the two
#: counters on a completion line. Folded narrowly — each pattern is anchored to the
#: line that owns it, so the fold cannot swallow a real difference (pytest's own
#: `7 passed in 0.5s`, the leg banners, the summary rows and every FAILED line all
#: survive untouched, and a dropped or duplicated block is still caught).
_NONCES = (
    (re.compile(r"/tmp/hscc-legs-\w+"), "<LEG-TMP>"),
    (re.compile(r"/tmp/hscc-suite-home-\w+"), "<SEAL-TMP>"),
    (re.compile(r"^parallel: done leg=(\S+) elapsed=\d+s rc=(\d+) pack=\d+s$", re.M),
     r"parallel: done leg=\1 rc=\2 <TIMING>"),
)


def _fold_stream(log):
    """The stream a READER sees, with the per-run nonces and counters folded.

    Byte-identity pins below compare this. "Identical" is the claim being pinned;
    including a mktemp nonce or a duration in it would only make the pin flaky —
    and both folds are anchored to their own line, so neither can hide a block that
    arrived twice, a block that never arrived, or a FAILED line that lost its
    line-start.
    """
    for pat, repl in _NONCES:
        log = pat.sub(repl, log)
    return log


def test_the_default_is_a_pack_of_two_and_is_the_explicit_two_stream_exactly(tmp_path):
    """The default moved 1 -> 2 (t_97013930). Two things are pinned: the width, and
    that the default introduces no THIRD stream.

    This is the re-argument of `test_the_default_is_the_serial_stream_and_prints_
    nothing_new`, whose value was that two readers were measured against the stream
    an UNSET run produces: the SIGTERM forensics' `during suite:` line (t_6bb29d46)
    and merge_queue's `^✗ <leg>` / `^FAILED <path>::<test>` digest anchors. That
    claim cannot survive the flip as written, and deleting it would drop the only
    guard on what the flip is allowed to change — so it becomes the claim that still
    protects those readers: an unset run, an empty-valued run and an explicit
    `HSCC_TEST_PARALLEL=2` run of the same plan emit the SAME STREAM. Everything that
    was measured against a packed run — this file's digest, heartbeat and roster-
    order pins, and t_4fc35c1f's green full-roster run — therefore sees exactly what
    it has already been shown, and the only thing the flip changes is which knob a
    reader turns to get serial (the next test).
    """
    plan = _plan([(LEG_A, 0.5, 0), (LEG_B, 0.5, 0), (LEG_C, 0.5, 0)])
    unset = _run(tmp_path / _mk(tmp_path, "unset"), plan=plan)
    empty = _run(tmp_path / _mk(tmp_path, "empty"), plan=plan, parallel="")
    two = _run(tmp_path / _mk(tmp_path, "two"), plan=plan, parallel=2)
    assert unset["rc"] == empty["rc"] == two["rc"] == 0, (
        f"{unset['rc']} / {empty['rc']} / {two['rc']}")
    for name, r in (("unset", unset), ("empty", empty)):
        assert _fold_stream(r["log"]) == _fold_stream(two["log"]), (
            f"{name} is its own stream shape — the readers were measured against "
            f"the packed one, not a third:\n  {name}: {r['log']!r}\n  two:   "
            f"{two['log']!r}")
    peak, _ = _in_flight(unset["trace"])
    assert peak == 2, f"the default must PACK (want peak 2): {unset['trace']}"
    assert re.search(r"^parallel: 2 legs at a time", unset["log"], re.M), unset["log"]
    # The block shape the digest anchors need, at the default, unasked for.
    assert re.findall(r"^━━━ (\S+) ━━━$", unset["log"], re.M)[:3] == [LEG_A, LEG_B, LEG_C]


@pytest.mark.parametrize("serial_value", ["1", "0"])
def test_the_serial_posture_is_one_line_away_and_adds_nothing_else(tmp_path, serial_value):
    """The half of the old default pin that keeps all its teeth after the flip.

    `HSCC_TEST_PARALLEL=1` (and 0) is the stream the SIGTERM forensics and the
    queue's digest anchors were MEASURED against, so it has to stay the old loop:
    one leg in flight, each leg's banner interleaved with its own output rather
    than printed above a captured block, no tmp dir, and exactly one line saying
    which posture ran. The argv each leg is handed is identical across postures —
    `test_pack_and_serial_hand_pytest_the_same_argv` holds that half.
    """
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.3, 0), (LEG_B, 0.3, 0), (LEG_C, 0.3, 0)]),
             parallel=serial_value)
    assert r["log"].count("parallel:") == 1, (
        f"the serial posture prints exactly one self-reporting line:\n{r['log']}")
    assert "running SERIALLY (the pre-concurrency path)" in r["log"], r["log"]
    peak, _ = _in_flight(r["trace"])
    assert peak == 1, r["trace"]
    # Interleaved banner+output: in a pack the banner sits before the whole block.
    assert re.search(rf"━━━ {LEG_A} ━━━\n\.", r["log"]), r["log"]
    assert "leg logs in" not in r["log"], f"no tmp dir on the serial path:\n{r['log']}"


def test_a_single_leg_selection_stays_serial_and_silent_at_the_default(tmp_path):
    """CI's shape, unpinned until the default moved.

    `.github/workflows/ci.yml` runs ONE leg per job, and the pack clamps to the
    roster size before it can print anything — so a one-leg job keeps the serial
    stream AND the silent one. That is why flipping the default needs no change in
    CI: the workflows never set HSCC_TEST_PARALLEL, and this pins that they never
    have to. A regression here would pack CI's legs and change what CI measures,
    which no one asked for.
    """
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.3, 0)]), legs=LEG_A)
    assert "parallel:" not in r["log"], f"a one-leg run must stay silent:\n{r['log']}"
    assert "leg logs in" not in r["log"], r["log"]
    peak, _ = _in_flight(r["trace"])
    assert peak == 1, r["trace"]


def test_an_explicit_pack_width_is_honoured_and_announced(tmp_path):
    """A width asked for is a width got, and the run says so (and says where the
    leg logs went — the tmp dir is the one thing a reader needs to read a packed
    run while it is still running)."""
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.6, 0), (LEG_B, 0.6, 0), (LEG_C, 0.6, 0)]),
             parallel=2)
    assert re.search(r"^parallel: 2 legs at a time", r["log"], re.M), r["log"]
    peak, _ = _in_flight(r["trace"])
    assert peak == 2, r["trace"]


def test_a_free_slot_is_recycled_and_reports_its_own_duration(tmp_path):
    """The head-of-line bug this card found by RUNNING it, pinned.

    The first implementation waited for the OLDEST leg in flight, so a leg that
    finished early still occupied its slot until the slowest neighbour ended, and
    its reported duration was the wait, not the work. Measured on the real roster
    at p=2: seven legs, 627 s of work in 615 s of wall — 1.0x, and a 3 s leg
    reported 320 s. Both halves are pinned here: a slot must come back
    immediately, and `elapsed=` must be the leg's own time.
    """
    slow, fast_a, fast_b = LEG_A, LEG_B, LEG_C
    r = _run(tmp_path,
             plan=_plan([(slow, 3.0, 0), (fast_a, 1.0, 0), (fast_b, 1.0, 0)]),
             parallel=2)
    assert r["rc"] == 0, r["log"]
    # Serial would be 5.0 s + overhead; a pool that recycles finishes in ~3.
    assert r["wall"] < 4.3, f"the pack did not recycle the slot: {r['wall']:.1f}s wall"
    done = dict(re.findall(r"^parallel: done leg=(\S+) elapsed=(\d+)s", r["log"], re.M))
    assert set(done) == {slow, fast_a, fast_b}, r["log"]
    assert int(done[slow]) >= 3, done
    for leg in (fast_a, fast_b):
        assert int(done[leg]) <= 2, (
            f"{leg} reported {done[leg]}s for 1s of work — the duration is being "
            f"measured to the REAP, not to the leg's own exit: {r['log']}")


def test_a_leg_killed_by_a_signal_reads_red(tmp_path):
    """A leg that dies without printing a summary must still fail the run.

    The rc comes from the wrapper's own record when it can write one (this stub
    dies by signal, so the record is 137/143 rather than a pytest code), and from
    `wait`'s status when it cannot — both of which are non-zero. What this pins is
    that no path through the new code reads a leg that never completed as green.
    """
    stub = tmp_path / "stub_py.sh"
    # The stub kills itself before pytest could print anything. The wrapper still
    # records the signal exit; what matters is that the runner propagates it.
    stub.write_text(STUB.replace('echo "start $leg $$" >> "$STUB_TRACE"',
                                'echo "start $leg $$" >> "$STUB_TRACE"\n'
                                'kill -9 "$$"'))
    stub.chmod(0o755)
    trace, argv, log = tmp_path / "t", tmp_path / "a", tmp_path / "run.log"
    trace.write_text("")
    argv.write_text("")
    env = dict(os.environ)
    env.update({"HSCC_TEST_PY": str(stub), "STUB_TRACE": str(trace),
                "STUB_ARGV": str(argv),
                "STUB_PLAN": _plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0)]),
                "HSCC_TEST_LEGS": f"{LEG_A} {LEG_B}", "HSCC_TEST_PARALLEL": "2",
                "HSCC_LEG_HEARTBEAT_SECONDS": "0"})
    with open(log, "wb") as fh:
        proc = subprocess.run(["bash", str(RUNNER)], stdout=fh,
                              stderr=subprocess.STDOUT, env=env, timeout=120,
                              cwd=str(REPO))
    out = log.read_text()
    assert proc.returncode == 1, f"a killed leg read as green:\nrc={proc.returncode}\n{out}"
    assert "FAILURES ABOVE" in out, out


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


@pytest.mark.parametrize("packed_parallel", [3, None],
                         ids=["asked-for-3", "the-default"])
def test_red_leg_digest_matches_the_serial_shape(tmp_path, packed_parallel):
    """THE pin. The queue's digest of a packed run must name the same failures a
    serial run names — asserted against merge_queue's real regexes, not a copy.

    A pack that let another leg's dots land inside a short test summary would
    still print the FAILED text somewhere, so substring assertions would pass.
    These match at LINE START, which is how the queue reads them.

    Run twice on purpose since t_97013930: once for a width someone asked for and
    once for the SHIPPED DEFAULT (unset), because the default is what the queue's
    gate actually runs — `merge_queue._selected_leg_gate` builds
    `HSCC_TEST_LEGS=… bash scripts/run_tests.sh` and never sets a width, so this
    is the only thing standing between a default flip and a handback digest that
    silently names fewer failures than the run produced.
    """
    from hscc_daemon import merge_queue as mq

    plan = _plan([(LEG_A, 0.4, 0), (LEG_B, 0.9, 1), (LEG_C, 0.2, 0)])
    pack = _run(tmp_path / _mk(tmp_path, "pack"), plan=plan, parallel=packed_parallel)
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


@pytest.mark.parametrize("parallel", ["2", None], ids=["asked-for-2", "the-default"])
def test_sigterm_names_the_live_legs_and_leaves_none_behind(tmp_path, parallel):
    """t_6bb29d46 still holds: rc=143 must be explainable from the log alone, and
    a pack must not survive its runner as N orphan pytests.

    The other reader in the pair, and the one the flip actually changes: with a
    pack `during suite:` names the SET of legs in flight, not one suite. Nothing
    in the repo parses that line (measured: `grep -rn "during suite"` lands the
    writer, this pin, `scripts/audits/rc143_guard_selftest.sh`'s own echo and the
    prose recipe in docs/audits/rc143-suite-reaper-t_6bb29d46.md — no reader), so
    the shape change is safe; what is NOT safe is the default silently dropping
    back to naming one arbitrary suite while two are burning. Hence both postures
    here, and the assertion being a SET rather than a string.
    """
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
        "HSCC_TEST_LEGS": THREE,
        "HSCC_LEG_HEARTBEAT_SECONDS": "0",
    })
    if parallel is None:
        env.pop("HSCC_TEST_PARALLEL", None)
    else:
        env["HSCC_TEST_PARALLEL"] = parallel
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


def test_a_leg_whose_name_is_a_path_still_runs(tmp_path):
    """A leg log must be a FILE for every leg name the roster can contain.

    Eleven of the twelve names are one flat path component, so a log path built
    by splicing the leg name into a filename raw passes eleven tests and silently
    loses the twelfth: `.github/scripts` turned `$LEG_TMP/1-.github/scripts.log`
    into a file in a directory that does not exist. The redirect then fails in
    the child before `env` is execed, so pytest never ran, the wrapper recorded
    nothing, and the leg read red off `wait`'s status — a red with no `FAILED`
    line, which is exactly the un-capturable third red of t_6c2123f9's p=2
    roster (measured: `line 939: …/1-.github/scripts.log: No such file or
    directory`, `elapsed=0s rc=1`).

    Asserting on the block being NON-EMPTY is the whole test: the runner prints
    the `━━━ <leg> ━━━` header whether or not the log opened, so a header with
    nothing under it is this defect, and a green run of the stub cannot produce
    one.
    """
    r = _run(tmp_path, plan=_plan([(LEG_A, 0.2, 0), (LEG_SLASHY, 0.2, 0)]),
             parallel=2, legs=f"{LEG_A} {LEG_SLASHY}")
    assert r["rc"] == 0, (
        f"a leg named with a '/' read red (rc={r['rc']}):\n{r['log']}")
    assert f"  ✓ {LEG_SLASHY}" in r["log"], r["log"]
    head = f"━━━ {LEG_SLASHY} ━━━"
    assert head in r["log"], r["log"]
    block = r["log"].split(head, 1)[1].split("━━━")[0]
    assert "7 passed" in block, (
        f"the {LEG_SLASHY} block printed no pytest output at all — the leg's log "
        f"could not be opened, so pytest was never execed:\n{r['log']}")


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
