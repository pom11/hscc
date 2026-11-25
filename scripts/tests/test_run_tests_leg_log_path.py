"""The per-leg log path must survive a leg NAME that contains a slash (t_692814f5).

WHAT WOULD BREAK WITHOUT IT — measured, not inferred. The roster holds one leg whose
name contains a slash (``.github/scripts``), and the pack built the per-leg log path
by plain concatenation::

    logf="$LEG_TMP/$P_NEXT-$d.log"      # -> /tmp/hscc-legs-XXXX/1-.github/scripts.log

The redirect target's parent (``…/1-.github``) does not exist, so the LEG's own
``> "$logf"`` failed, the subshell died before pytest was ever spawned, no ``.rc``
was written, and the pack reported a leg that never ran::

    scripts/run_tests.sh: line 961: /tmp/hscc-legs-NAR3QF/1-.github/scripts.log: No such file or directory
    parallel: done leg=.github/scripts elapsed=0s rc=1 pack=0s
      ✗ .github/scripts (pytest exit 1, 0s)

Reproduced verbatim on two checkouts that do not contain each other's diff, while
the same leg run SERIALLY is ``233 passed`` — the leg is fine and the pack is what
broke it. The expensive part is the consequence: any pack that includes
``.github/scripts`` was red no matter what the code did, i.e. in the
bounded-concurrency posture a real roster leg silently stopped being gated. Every
pack test written before this one used slash-free stub names (``hscc-commands``,
``sparkrun-hermes``, ``memori_byodb``), which is exactly why they were all green
while the shipped roster could not be packed at all.

So this file packs the REAL leg name, through the same stub-interpreter seam the
other pack tests use (``HSCC_TEST_PY``) — the thing under test is the log path, not
pytest. Two of the cases observe the log dir WHILE the pack is in flight, because
the runner's EXIT trap deletes it on the way out.

The mapping under test is deliberately narrow: only ``/`` is flattened, and only
when a leg name becomes a file NAME. A slash-free name — every other leg in the
roster — must produce exactly the file name it produced before, because
``<index>-<leg>.log`` is what an engineer ``ls``-es when the runner tells them
``leg logs in /tmp/hscc-legs-…``.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RUNNER = SCRIPTS / "run_tests.sh"

#: The roster leg this whole file is about. Hard-pinned on purpose: the runner
#: rejects an unknown leg with exit 64, so a name that stopped existing would turn
#: every case here red instead of quietly making the file vacuous. Renaming the leg
#: is a decision that has to show up here too.
LEG_SLASH = ".github/scripts"
#: A normal, slash-free roster leg to pack alongside it (roster order puts this one
#: at index 0 and LEG_SLASH at index 1 — the shape in the failure quoted above).
LEG_PLAIN = "hscc-commands"
TWO = f"{LEG_PLAIN} {LEG_SLASH}"

#: What the fix must produce for LEG_SLASH at index 1, and the identity case for
#: LEG_PLAIN at index 0.
LOG_PLAIN = "0-hscc-commands.log"
LOG_SLASH = "1-.github-scripts.log"

#: Like scripts/tests/test_run_tests_leg_parallelism.py's stub, but it derives its
#: leg name from the WHOLE ``<root>/<leg>/tests`` argument rather than from the
#: immediate parent directory: for ``.github/scripts`` the immediate parent is
#: ``.github``, which is not the leg name, and a stub that mis-derives the name
#: silently falls back to its defaults — a harness that cannot see the leg it was
#: handed proves nothing about it.
STUB = """#!/usr/bin/env bash
set -u
tests_arg=""
for a in "$@"; do case "$a" in */tests) tests_arg="$a" ;; esac; done
leg="(none)"
case "$tests_arg" in
  "$STUB_ROOT"/*) leg="${tests_arg#"$STUB_ROOT"/}" ;;
  *) leg="$(basename "$(dirname "$tests_arg")")" ;;
esac
leg="${leg%/tests}"
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

    Redirected, never captured — the same reason the other pack tests redirect: a
    pipe plus a watcher that outlives a leg is a harness that hangs.
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


def _run_and_watch_leg_tmp(tmp_path, *, plan, parallel=2, legs=TWO, poll=0.05):
    """The same run, but the leg-log dir is SNAPSHOT WHILE THE PACK IS IN FLIGHT.

    The runner prints ``leg logs in <dir>`` and then deletes ``<dir>`` in its EXIT
    trap, so the file names it chose are only observable from outside, during the
    run. The plan must therefore keep the legs alive long enough to be caught (see
    the 1.5 s sleeps at the call sites); the loop just never stops looking until the
    runner is gone, so a name that appears for an instant is still recorded.

    Returns ``(result, entries)`` where ``entries`` is the set of
    ``(name, is_dir)`` seen at any point in the run.
    """
    env, trace, argv = _env_for(tmp_path, plan, legs)
    log = tmp_path / "run.log"
    env["HSCC_TEST_PARALLEL"] = str(parallel)
    entries = set()
    with open(log, "wb") as fh:
        proc = subprocess.Popen(["bash", str(RUNNER)], stdout=fh,
                                stderr=subprocess.STDOUT, env=env, cwd=str(REPO))
        deadline = time.time() + 180
        while True:
            alive = proc.poll() is None
            text = log.read_text(errors="replace")
            m = re.search(r"leg logs in (\S+)", text)
            if m:
                d = Path(m.group(1))
                try:
                    for p in d.iterdir():
                        entries.add((p.name, p.is_dir()))
                except OSError:
                    pass  # not created yet / already reaped
            if not alive or time.time() > deadline:
                break
            time.sleep(poll)
        proc.wait(timeout=180)
    return ({
        "rc": proc.returncode,
        "log": log.read_text(),
        "trace": [l for l in trace.read_text().splitlines() if l.strip()],
        "argv": [l.split("\t", 1) for l in argv.read_text().splitlines() if l.strip()],
    }, entries)


def _roster():
    """DIRS from the shipped runner — the legs the pack can actually be asked for."""
    text = RUNNER.read_text()
    m = re.search(r"^DIRS=\(([^)]*)\)", text, re.M)
    assert m, "the roster is no longer a DIRS=(…) literal in run_tests.sh"
    return m.group(1).split()


# ── the premise ─────────────────────────────────────────────────────────────

def test_the_roster_really_does_contain_a_leg_name_with_a_slash():
    """Anti-vacuity for this file.

    Every pack test written before this one used slash-free stub names, so the
    class was unreachable no matter how many of them passed. If the slash ever
    leaves the roster this file stops being load-bearing — and that has to be a
    red here, in the file that depends on it, not a silently green suite.
    """
    roster = _roster()
    assert LEG_SLASH in roster, roster
    assert "/" in LEG_SLASH
    # …and it is the ONLY one, so the index-prefix argument below covers the roster.
    assert [l for l in roster if "/" in l] == [LEG_SLASH], roster


# ── the red: a pack that cannot run the leg ─────────────────────────────────

def test_a_slash_leg_name_packs_and_actually_runs(tmp_path):
    """The card's repro, at the stub seam: rc 0, both legs really spawned.

    Pre-fix the slash leg dies at its own redirect: ``rc=1 elapsed=0s``, an empty
    log, and — the part a summary line hides — no pytest process at all. So this
    asserts on the three things that were missing, not just on the exit code.
    """
    r = _run(tmp_path, plan=_plan([(LEG_PLAIN, 0.4, 0), (LEG_SLASH, 0.4, 0)]),
             parallel=2)
    assert "No such file or directory" not in r["log"], (
        f"a leg log path that bash could not open — the leg never ran:\n{r['log']}")
    assert r["rc"] == 0, r["log"]
    done = {m.group(1): m.group(3)
            for m in re.finditer(
                r"^parallel: done leg=(\S+) elapsed=(\d+)s rc=(\d+)", r["log"], re.M)}
    assert set(done) == {LEG_PLAIN, LEG_SLASH}, r["log"]
    assert done[LEG_SLASH] == "0", f"the slash leg reported {done[LEG_SLASH]}: {r['log']}"
    # The leg was SPAWNED, with the leg dir in its argv — not merely "reported 0".
    spawned = {name for name, _ in r["argv"]}
    assert spawned == {LEG_PLAIN, LEG_SLASH}, r["argv"]
    assert any(LEG_SLASH in a for _, a in r["argv"]), r["argv"]
    # And it produced output INTO its log, which is what the digest concatenates.
    assert f"STUBBODY for {LEG_SLASH}" in r["log"], r["log"]


def test_the_pack_names_every_leg_log_a_direct_child_of_the_leg_tmp_dir(tmp_path):
    """The file names themselves, observed while the pack is in flight.

    Two claims, both red pre-fix: every leg has ONE log, and every log is a
    DIRECT child of the dir the runner advertised. The second half is what stops a
    'fix' that works by ``mkdir -p``-ing a per-leg subdirectory: a tree under the
    advertised dir is exactly the discoverability loss (``ls`` the dir a run told
    you about, get a mix of files and directories) that made this leg hard to read
    in the first place.
    """
    r, entries = _run_and_watch_leg_tmp(
        tmp_path, plan=_plan([(LEG_PLAIN, 1.5, 0), (LEG_SLASH, 1.5, 0)]), parallel=2)
    names = {n for n, _ in entries}
    assert r["rc"] == 0, r["log"]
    assert LOG_PLAIN in names, f"{LOG_PLAIN} never appeared: {sorted(names)}"
    assert LOG_SLASH in names, (
        f"the slash leg's log is not a direct child named {LOG_SLASH}: "
        f"{sorted(names)}\nlog={r['log']}")
    assert not [n for n, isdir in entries if isdir], (
        f"the pack created a SUBDIRECTORY under the log dir: "
        f"{sorted(n for n, d in entries if d)}")
    # Every entry the pack writes must be accountabl: N logs, N .rc records, one
    # legs.tsv. An unaccounted entry is a second naming scheme nobody pinned.
    assert {n for n, _ in entries} <= {LOG_PLAIN, LOG_SLASH, "0.rc", "1.rc",
                                       "legs.tsv"}, sorted(names)


def test_a_failing_slash_leg_is_still_reported_as_failing(tmp_path):
    """The fix must not be 'the leg stops being a problem'.

    A slash in the name may not make the leg skippable, digestless, or green: a
    red slash leg has to reach the summary as a red, with its real name, and drag
    the run's exit code with it. This is the case that a `mkdir -p`-style fix and a
    drop-from-roster 'fix' would both pass without noticing.
    """
    r = _run(tmp_path, plan=_plan([(LEG_PLAIN, 0.4, 0), (LEG_SLASH, 0.4, 1)]),
             parallel=2)
    assert r["rc"] == 1, f"a red slash leg must redden the run:\n{r['log']}"
    assert f"✗ {LEG_SLASH} (pytest exit 1, " in r["log"], r["log"]
    # …and the failure is readable, because the log it wrote is what gets shown.
    assert f"FAILED {LEG_SLASH}/tests/test_stub.py::test_breaks" in r["log"], r["log"]


def test_the_digest_keeps_the_real_leg_name_and_the_roster_order(tmp_path):
    """Flattening is for FILE names only — the stream the queue parses keeps the leg.

    merge_queue anchors on ``━━━ <leg> ━━━`` and ``✗ <leg>`` at line start, and the
    blocks are concatenated in ROSTER order, not completion order. Both halves must
    survive the mapping: the banner carries ``.github/scripts``, never the flattened
    ``.github-scripts``.
    """
    r = _run(tmp_path, plan=_plan([(LEG_PLAIN, 1.2, 0), (LEG_SLASH, 0.3, 0)]),
             parallel=2)
    assert r["rc"] == 0, r["log"]
    assert f"━━━ {LEG_SLASH} ━━━" in r["log"], r["log"]
    assert f"━━━ {LEG_SLASH.replace('/', '-')} ━━━" not in r["log"], r["log"]
    # roster order: LEG_PLAIN (index 0) first, even though the slash leg finished
    # four times faster.
    assert r["log"].index(f"━━━ {LEG_PLAIN} ━━━") < r["log"].index(f"━━━ {LEG_SLASH} ━━━"), r["log"]


# ── controls: what must not move ────────────────────────────────────────────

def test_the_serial_path_for_the_slash_leg_is_unchanged(tmp_path):
    """The serial path never touched a log path, and must stay exactly as green.

    The bug was pack-only (the serial leg is `233 passed`), so this case is green
    before the fix as well — it is here to prove the fix did not move the path that
    was already working, and to give the red above a survivor to be read against.
    """
    r = _run(tmp_path, plan=_plan([(LEG_PLAIN, 0.3, 0), (LEG_SLASH, 0.3, 0)]),
             parallel=None)
    assert "parallel:" not in r["log"], f"unset must stay silent:\n{r['log']}"
    assert r["rc"] == 0, r["log"]
    assert {name for name, _ in r["argv"]} == {LEG_PLAIN, LEG_SLASH}, r["argv"]


def test_only_the_log_path_builds_a_path_from_a_leg_name(tmp_path):
    """One mapping site, and no leftover raw concatenation.

    The pack has exactly one place where a leg NAME becomes a PATH (the per-leg
    log): the ``.rc`` records and ``legs.tsv`` are index-keyed / tab-separated, so
    a name with a slash is harmless there. Pinned as a count over the script text
    because a SECOND name-derived path would re-open this bug in a place no test
    selects — the same idiom ``test_run_tests_home_seal.py`` uses for the single
    ``mktemp -d`` site.
    """
    text = RUNNER.read_text()
    # Comments are not the contract: the block above this line explains the old
    # shape in its own words, and a pin that reads prose cannot tell "the bug is
    # back" from "the bug is documented".
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert code.count('$d.log') == 0, (
        "a leg name is being concatenated straight into a log path again")
    assert code.count("${d//\\//-}") == 1, (
        "the log-name flattening must exist exactly once, at the one site that "
        f"turns a leg name into a path (found {code.count('${d//\\//-}')})")
    assert len(re.findall(r"^\s*logf=", code, re.M)) == 1, (
        "a second per-leg log path assignment appeared; it needs the same mapping")
