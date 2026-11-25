"""A packed leg the harness could not run must not look like a leg whose tests failed.

WHY THIS FILE EXISTS — measured, not inferred. In the pack, a leg whose wrapper died
before it could write its ``.rc`` record and whose log holds nothing is reported
*identically* to a leg whose tests failed::

    parallel: done leg=.github/scripts elapsed=0s rc=1 pack=0s
      ✗ .github/scripts (pytest exit 1, 0s)

That is the shape t_692814f5 shipped with: the leg's own ``> "$logf"`` failed, the
subshell died before pytest was ever spawned, no record was written, and the reaper's
``wait`` fallback produced an ordinary-looking red. Two readers then made it worse
rather than better:

* ``merge_queue._failure_lines`` buckets by line-start regex only, so an *extra
  diagnostic line* would be dropped from every gate handback — the distinction has to
  live ON the ``✗`` row to exist for the queue at all.
* ``.github/scripts/leg_verdict.sh`` hard-reds ``pytest exit`` with any code but 1 as
  "the harness, not a test, is broken", and reports ``pytest exit 1`` as an ordinary
  TEST failure — ``::warning::`` and a GREEN check while ``HSCC_TESTS_ENFORCE`` is
  unset. Which of those two a victim leg lands on depends on *how* its record was lost,
  and that is an accident of the mechanism, not a property of the class: a wrapper
  SIGKILLed in flight leaves ``wait`` reporting 137 (hard-red, by luck), while the
  redirect failure t_692814f5 actually shipped leaves ``wait`` reporting **1** —
  advisory green. So the instance that hid for a whole release was not merely
  ambiguous, it was green.

The verdict this file pins is therefore a distinct one, on the anchored row, carrying
the phrase ``no leg output`` — see run_tests.sh's reap block for the reasoning:

      ✗ .github/scripts (no leg output — harness failure, pytest exit 137, 0s)

WHAT MAKES THE DISCRIMINATOR HONEST. Not "the record is missing" alone, but "the record
is missing **and** the log is empty". A leg that got as far as printing
``FAILED path::test`` and only then lost its wrapper has real evidence about a test, and
must stay the ordinary red row — that control is pinned below, because a verdict that
fires on every missing record would mislabel a genuine test failure as the harness and
teach people to distrust the phrase.

WHY THE INSTANCES BELOW, AND WHY BOTH. ``LEG_TMP`` has no env seam (it is
``mktemp -d /tmp/hscc-legs-XXXXXX``, run_tests.sh:688) and the runner is ``set -u``
without ``set -e``, so "point LEG_TMP at a non-writable dir" is not reachable from a
test. The shipped cases use the other real instance of the same two facts — the leg's
wrapper is SIGKILLed before it can record, which is what an OOM-kill or a ``kill -9``
of a running leg actually does to a pack. The redirect-failure instance is confirmed
separately by probe (it needs the pre-flatten ``logf=`` concat), and is what the
verdict is written to catch.

The stub seams are ``HSCC_TEST_PY`` / ``STUB_PLAN`` as in
``test_run_tests_leg_log_path.py``: the thing under test is the runner's *reporting*,
not pytest. ``test_run_tests_leg_parallelism.py`` is deliberately not touched (t_34c5a3ed
flake hotspot).
"""

import os
import re
import subprocess
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RUNNER = SCRIPTS / "run_tests.sh"

#: Two ordinary slash-free roster legs: one plays the victim, one must survive it and
#: keep reporting normally in the same pack.
LEG_VICTIM = "hscc-commands"
LEG_OTHER = "sparkrun-hermes"
TWO = f"{LEG_VICTIM} {LEG_OTHER}"

#: The phrase that IS the verdict. Kept ASCII and anchored on the opening paren of the
#: summary row so it cannot be matched by prose, and so `leg_verdict.sh` can grep for
#: it without depending on the em dash's bytes.
HARNESS_PHRASE = "(no leg output"

#: The ordinary red row this class must NOT be reported as.
ORDINARY_ROW = re.compile(r"^  ✗ (?P<leg>\S+) \(pytest exit (?P<code>\d+), \d+s\)$", re.M)
HARNESS_ROW = re.compile(
    r"^  ✗ (?P<leg>\S+) \(no leg output — harness failure, "
    r"pytest exit (?P<code>\d+), \d+s\)$", re.M)

#: ``leg:secs:rc`` where rc is a number, ``kill`` (SIGKILL the leg's own wrapper with
#: nothing written to stdout) or ``killf`` (same, after flushing one line to stdout —
#: the control that proves the empty log is load-bearing).
STUB = """#!/usr/bin/env bash
set -u
tests_arg=""
for a in "$@"; do case "$a" in */tests) tests_arg="$a" ;; esac; done
leg="$(basename "$(dirname "$tests_arg")")"
leg="${leg%/tests}"
printf '%s\\t%s\\n' "$leg" "$*" >> "$STUB_ARGV"

secs=0.2; rc=0
for entry in $STUB_PLAN; do
  name="${entry%%:*}"; rest="${entry#*:}"
  if [ "$name" = "$leg" ]; then secs="${rest%%:*}"; rc="${rest#*:}"; break; fi
done

echo "start $leg $$" >> "$STUB_TRACE"
case "$rc" in
  kill|killf)
    # The stub IS the wrapper's direct child (`env` execs into this script inside the
    # `( … ) > "$logf" 2>&1 &` subshell), so $PPID is the wrapper that owns the leg's
    # `.rc` record. Killing it is what an OOM-kill of a leg does to the pack: the
    # record is never written and `wait` reports the signal.
    sleep "$secs"
    [ "${rc}" = "killf" ] && echo "F" && echo "FAILED $leg/tests/test_stub.py::test_breaks - boom"
    kill -9 "$PPID" 2>/dev/null
    sleep 0.2
    exit 137
    ;;
esac
sleep "$secs"
echo "end $leg $$" >> "$STUB_TRACE"
if [ "$rc" -ne 0 ]; then
  echo "F"
  echo "FAILED $leg/tests/test_stub.py::test_breaks - assert 1 == 2"
  echo "1 failed in ${secs}s"
else
  echo "."
  echo "STUBBODY for $leg: 7 passed in ${secs}s"
fi
exit "$rc"
"""


def _plan(spec):
    return " ".join(f"{leg}:{secs}:{rc}" for leg, secs, rc in spec)


def _env_for(tmp_path, plan, legs):
    stub = tmp_path / "stub_py.sh"
    stub.write_text(STUB)
    stub.chmod(0o755)
    for name in ("trace.txt", "argv.tsv"):
        (tmp_path / name).write_text("")
    env = dict(os.environ)
    env.update({
        "HSCC_TEST_PY": str(stub),
        "STUB_TRACE": str(tmp_path / "trace.txt"),
        "STUB_ARGV": str(tmp_path / "argv.tsv"),
        "STUB_PLAN": plan,
        "STUB_ROOT": str(REPO),
        "HSCC_LEG_HEARTBEAT_SECONDS": "0",
        "HSCC_TEST_LEGS": legs,
    })
    return env, tmp_path / "trace.txt", tmp_path / "argv.tsv"


def _run(tmp_path, *, plan, parallel=None, legs=TWO):
    """Execute the shipped runner against the stub, output REDIRECTED to a file.

    Redirected, never captured — a pipe plus a watcher that outlives a leg is a harness
    that hangs (see test_run_tests_leg_log_path.py for the same reasoning).
    """
    env, trace, argv = _env_for(tmp_path, plan, legs)
    log = tmp_path / "run.log"
    if parallel is None:
        env.pop("HSCC_TEST_PARALLEL", None)
    else:
        env["HSCC_TEST_PARALLEL"] = str(parallel)
    t0 = time.time()
    with open(log, "wb") as fh:
        proc = subprocess.run(["bash", str(RUNNER)], stdout=fh,
                              stderr=subprocess.STDOUT, env=env,
                              timeout=180, cwd=str(REPO))
    return {
        "rc": proc.returncode,
        "log": log.read_text(),
        "trace": [l for l in trace.read_text().splitlines() if l.strip()],
        "argv": [l.split("\t", 1) for l in argv.read_text().splitlines() if l.strip()],
        "wall": time.time() - t0,
    }


def _summary(log):
    return log.split("━━━ Summary ━━━")[1]


def _rows(log, rx):
    return {m.group("leg"): m.groupdict() for m in rx.finditer(_summary(log))}


# ── the premise: the class really is reachable in a pack ────────────────────

def test_a_leg_whose_wrapper_died_left_no_record_and_no_output(tmp_path):
    """Anti-vacuity for this file: the two facts really are both true here.

    Every case below reads as "the harness verdict fired" — which is only worth
    anything if the run really did lose the record AND really did capture nothing.
    Asserting the facts directly means a future harness change that quietly makes the
    victim leg produce output (so the verdict correctly stops firing) turns THIS case
    red instead of making the file silently vacuous.
    """
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.4, "kill"),
                                   (LEG_OTHER, 0.4, 0)]), parallel=2)
    # The other leg ran and recorded normally, so the pack itself was healthy.
    assert f"STUBBODY for {LEG_OTHER}" in r["log"], r["log"]
    assert f"STUBBODY for {LEG_VICTIM}" not in r["log"], r["log"]
    # The runner advertises the log dir and deletes it on exit, so the emptiness is
    # asserted through what the pack printed: the victim's block is present and holds
    # nothing, and the reaper had no record to read (its live line carries `wait`'s
    # status, not the wrapper's own).
    block = r["log"].split(f"━━━ {LEG_VICTIM} ━━━")[1].split("━━━")[0]
    assert block.strip() == "", f"the victim leg produced output: {block!r}"
    done = re.search(rf"^parallel: done leg={re.escape(LEG_VICTIM)} elapsed=(\d+)s rc=(\d+)",
                     r["log"], re.M)
    assert done, r["log"]
    assert done.group(2) != "0", "a leg whose wrapper died cannot report success"


# ── the verdict ─────────────────────────────────────────────────────────────

def test_a_leg_with_no_record_and_no_output_gets_the_harness_verdict(tmp_path):
    """The card's question, answered as a distinct verdict rather than a note.

    Pre-fix this row is `  ✗ hscc-commands (pytest exit 137, 0s)` — indistinguishable
    in kind from a leg whose tests failed, and (via leg_verdict.sh) in class too.
    """
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.4, "kill"),
                                   (LEG_OTHER, 0.4, 0)]), parallel=2)
    harness = _rows(r["log"], HARNESS_ROW)
    assert LEG_VICTIM in harness, (
        f"no harness-failure row for the leg that never ran:\n{r['log']}")
    # …and it is NOT also/instead the ordinary row.
    assert LEG_VICTIM not in _rows(r["log"], ORDINARY_ROW), r["log"]
    # The other leg keeps its normal green row: the verdict is per leg, not a stamp
    # on the whole pack.
    assert LEG_OTHER in _rows(r["log"], re.compile(r"^  ✓ (?P<leg>\S+) \(\d+s\)$", re.M))


def test_the_harness_verdict_is_red_and_the_run_still_exits_nonzero(tmp_path):
    """Fails RED, never silent — the card's hard constraint.

    Two ways this could be quietly green and both are pinned shut here: the verdict
    being rendered as `✓` because the fallback rc happened to be 0, and the verdict
    being printed while `rc` stays 0.
    """
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.4, "kill"),
                                   (LEG_OTHER, 0.4, 0)]), parallel=2)
    assert r["rc"] != 0, f"a leg the harness could not run must fail the run:\n{r['log']}"
    assert "FAILURES ABOVE" in r["log"] and "ALL GREEN" not in r["log"], r["log"]
    assert f"✓ {LEG_VICTIM}" not in r["log"], \
        f"a leg with no record read as GREEN:\n{r['log']}"


def test_the_queue_digest_still_names_the_leg_after_the_row_widened(tmp_path):
    """THE contract, asserted against merge_queue's REAL regexes rather than a copy.

    The digest keeps a line only if it matches `^(FAILED|ERROR)\\s` or `^✗\\s` /
    `^\\d+ failed\\b` at line start. This is the assertion that turns "add a diagnostic
    line" into a provably empty answer: an unanchored extra line never reaches here.
    """
    from hscc_daemon import merge_queue as mq

    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.4, "kill"),
                                   (LEG_OTHER, 0.4, 0)]), parallel=2)
    lines = mq._failure_lines(r["log"])
    mine = [l for l in lines if LEG_VICTIM in l]
    assert mine, f"the gate handback lost the leg entirely: {lines}\n{r['log']}"
    assert HARNESS_PHRASE in mine[0], mine[0]
    # Still counted as a LEG row by the leg bucket, not only by substring luck.
    assert any(l.startswith("✗") for l in lines), lines
    # And short enough that the cap cannot truncate the verdict away.
    assert len(mine[0]) < mq.MAX_FAILURE_LINE_CHARS, mine[0]


def test_the_live_reap_line_says_the_record_is_missing(tmp_path):
    """The same fact while the pack is still running, for a reader of the stream.

    The `parallel:` prefix is what keeps this line OUT of the digest (it must never be
    mistaken for an anchored row), so it is the only place the live stream can say it.
    """
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.4, "kill"),
                                   (LEG_OTHER, 0.4, 0)]), parallel=2)
    line = [l for l in r["log"].splitlines() if l.startswith(f"parallel: done leg={LEG_VICTIM} ")]
    assert line, r["log"]
    assert "record=missing" in line[0], line[0]
    # …and the healthy leg's live line does not claim it.
    other = [l for l in r["log"].splitlines() if l.startswith(f"parallel: done leg={LEG_OTHER} ")]
    assert other and "record=missing" not in other[0], other


# ── controls: the discriminator has to be able to say "no" ─────────────────

def test_a_leg_that_produced_output_before_dying_stays_an_ordinary_red(tmp_path):
    """The empty log is load-bearing, and this is the case that proves it.

    Here the wrapper also dies without writing its record, but the leg flushed output
    first. Its tests may well have broken — the `FAILED` line is real evidence — so it
    must keep the ordinary row and must NOT borrow the harness verdict. A verdict that
    fired on a missing record alone would pass the cases above and fail this one.
    """
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.4, "killf"),
                                   (LEG_OTHER, 0.4, 0)]), parallel=2)
    assert LEG_VICTIM not in _rows(r["log"], HARNESS_ROW), (
        f"the harness verdict fired on a leg that produced output:\n{r['log']}")
    assert f"FAILED {LEG_VICTIM}/tests/test_stub.py::test_breaks" in r["log"], r["log"]
    assert r["rc"] != 0, r["log"]


def test_an_ordinary_test_failure_keeps_the_ordinary_row(tmp_path):
    """Nothing about a real red moves: same row, same code, no harness phrase."""
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.3, 1), (LEG_OTHER, 0.3, 0)]),
             parallel=2)
    rows = _rows(r["log"], ORDINARY_ROW)
    assert LEG_VICTIM in rows and rows[LEG_VICTIM]["code"] == "1", r["log"]
    assert HARNESS_PHRASE not in r["log"], r["log"]
    assert r["rc"] == 1, r["log"]


def test_a_green_pack_never_mentions_the_harness_verdict(tmp_path):
    """Anti-vacuity in the other direction: a healthy pack must not grow the row."""
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.3, 0), (LEG_OTHER, 0.3, 0)]),
             parallel=2)
    assert r["rc"] == 0, r["log"]
    assert "ALL GREEN" in r["log"], r["log"]
    assert HARNESS_PHRASE not in r["log"], r["log"]
    assert "record=missing" not in r["log"], r["log"]


def test_the_serial_summary_row_is_unchanged(tmp_path):
    """The class is pack-only and the serial digest shape must not move a byte.

    The artefact that goes missing is the per-leg `.rc` record, which only the pack
    has; a serial leg writes to the runner's own stdout and its code is captured in the
    same shell that prints the row, so there is no record to lose. A serial leg that
    exits 137 therefore has to keep reporting exactly `✗ <leg> (pytest exit 137, Ns)` —
    that is what keeps `test_red_leg_digest_matches_the_serial_shape`'s parity claim and
    `test_run_tests_leg_duration.py`'s strict `RED_ROW` regex true.
    """
    r = _run(tmp_path, plan=_plan([(LEG_VICTIM, 0.2, 137), (LEG_OTHER, 0.2, 0)]),
             parallel=None)
    assert "parallel:" not in r["log"], r["log"]
    rows = _rows(r["log"], ORDINARY_ROW)
    assert LEG_VICTIM in rows and rows[LEG_VICTIM]["code"] == "137", r["log"]
    assert HARNESS_PHRASE not in r["log"], r["log"]
    assert r["rc"] == 1, r["log"]


# ── the seams a future edit must keep single ────────────────────────────────

def test_the_verdict_is_derived_from_the_two_reap_facts_at_one_site():
    """The classification must stay a function of the record and the log.

    A `rc == 1` heuristic would look equivalent and be wrong: it re-creates the very
    ambiguity this card exists to remove, and it would fire on every ordinary test red.
    Pinned as counts over the CODE lines (comments excluded — a pin that reads prose
    cannot tell "the bug is back" from "the bug is documented", and the block above the
    reap site explains the old shape in its own words).
    """
    text = RUNNER.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert code.count(HARNESS_PHRASE) == 1, (
        "the harness verdict must be rendered exactly once — a second rendering site "
        f"is a second shape the digest readers were never checked against (found {code.count(HARNESS_PHRASE)})")
    # One record writer, and one classification site reading it. A second writer would
    # fork the fallback logic the card depends on; a second classifier would drift.
    assert code.count('> "$LEG_TMP/$P_NEXT.rc"') == 1, code.count('> "$LEG_TMP/$P_NEXT.rc"')
    assert code.count('P_ORPHAN[$_k]=1') == 1, code.count('P_ORPHAN[$_k]=1')
    # The discriminator is "no record AND no output", and it must key on an EMPTY log:
    # `-f` is true for the zero-byte file a failed redirect leaves behind, and the
    # record alone would mislabel a leg that printed `FAILED …` before it died.
    assert re.search(r'\[ ! -f "\$LEG_TMP/\$_k\.rc" \] && \[ ! -s "\$\{P_LOG\[\$_k\]\}" \]',
                     code), (
        "the verdict must key on BOTH facts in one condition — no `.rc` record AND an "
        "empty log (`[ ! -s … ]`, not `-f`)")
    # Orphan-first ordering: an orphan whose fallback code happened to be 0 must not
    # render as `✓`.
    summary = code.split('echo "━━━ Summary ━━━"')[0]
    assert summary.rindex("-n \"${P_ORPHAN[$P_I]}\"") < summary.rindex("-eq 0 ]"), (
        "the harness verdict must be tested BEFORE the exit-code-0 branch")
