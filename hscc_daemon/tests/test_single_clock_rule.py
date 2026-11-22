"""The single-clock rule, enforced — card t_ed416771.

THE RULE
  A test case runs on ONE clock, and it is the clock that WROTE the data the
  case judges. A file may anchor its fixture clock to a fixed instant; what it
  may not do is let the code under test answer one of its questions from the
  wall clock, because then the verdict is a fact about the date pytest happened
  to run on.

WHY A RULE AND NOT A FIX-LIST
  This is the third card on the same defect (t_8ad06a53 found the first one
  while its own gate was red; t_3c11f9d1 pinned that file). A literal anchor is
  invisible to review — it looks careful and stable — and it decays silently:
  ``hscc_daemon/tests/test_node_pause_expiry.py`` was green on main at 12:45 UTC
  and red at 14:14 UTC on an unrelated PR, with no commit in between. So the
  guard below is behavioural, not a comment count.

WHAT IS ENFORCED HERE
  1. ``TestDeclaration``  — a file with a literal instant must declare its clock
     with a ``fixture-clock:`` line. Cheap, and it is what makes the next rule
     readable: the declaration states which clock the file's age claims answer
     to.
  2. ``TestTheShiftIsReal`` — the shift machinery is proven to move the shipped
     clock, two-sided (with it the clock moves, without it it does not). Every
     green in rule 3 rests on this; a shift that silently did nothing would be
     the loudest kind of false green.
  3. ``TestFixturesSurviveTheWallClock`` — the whole set of anchor files runs
     green with the wall clock moved forward AND backward. Forward catches the
     "not yet due" bombs (the span-guard kind); backward catches the "already
     due" kind, which are the ones that can sit green for months and lie about
     nothing.

SCOPE: tests only. Nothing here changes shipped behaviour.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent.parent

#: A fixed instant written into a fixture: ``datetime(20xx,``, ``dt.datetime(20xx,``.
#: Deliberately not matched inside this file's own pattern source (the ``\d\d``
#: below is not a year), so the guard cannot trip on itself.
_ANCHOR = re.compile(r"(?:datetime\.)*datetime\(\s*20\d\d\s*,")
#: The declaration a file with an anchor must carry. Kept as a substring so the
#: line can be written as ``# fixture-clock:`` or ``#: fixture-clock:``.
MARKER = "fixture-clock:"

#: How far the wall clock travels. One year plus a fortnight in both directions:
#: past every horizon this suite's fixtures can express, and far enough that a
#: "just bump the literal forward" repair cannot dodge it (that move simply
#: changes which direction reddens).
SHIFT_DAYS = float(os.environ.get("HSCC_WALL_SHIFT_PIN_DAYS", "400"))


def _anchor_files():
    """Every test file that fixes an instant, plus any that declares a clock."""
    out = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name in {Path(__file__).name, "wallclock_shift.py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _ANCHOR.search(text) or MARKER in text:
            out.append(path)
    return out


def _run_pytest(files, shift_days):
    """One pytest run over ``files``, with the wall clock at real + ``shift_days``.

    A SUBPROCESS, on purpose: the shift swaps ``sys.modules["datetime"]`` for
    the whole process, and the leg cannot afford a guard that mutates the clock
    out from under the cases that still have to run.
    """
    env = dict(os.environ)
    env["HSCC_REPO_ROOT"] = str(REPO_ROOT)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(TESTS_DIR), str(REPO_ROOT)] +
        [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
    if shift_days:
        env["HSCC_WALL_SHIFT_DAYS"] = str(shift_days)
    else:
        env.pop("HSCC_WALL_SHIFT_DAYS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest",
         *[str(p.relative_to(REPO_ROOT)) for p in files],
         "-q", "-p", "no:cacheprovider", "-p", "wallclock_shift"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=1800)


class TestDeclaration:
    """A literal anchor must name the clock its cases are judged by."""

    def test_every_literal_anchor_declares_its_clock(self):
        offenders = []
        for path in _anchor_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            hits = len(_ANCHOR.findall(text))
            if MARKER not in text:
                offenders.append(f"{path.name}: {hits} literal instant(s), no "
                                 f"'{MARKER}' declaration")
        assert not offenders, (
            "these files fix a fixture clock to a literal instant without "
            f"declaring which clock their age claims answer to (add a "
            f"'{MARKER}' line at the anchor, naming the instant and saying "
            "whether any code path can still answer from the wall clock):\n  "
            + "\n  ".join(offenders))


class TestTheShiftIsReal:
    """The shift is proven to move the shipped clock — from both sides."""

    def _run(self, mode, shift_days):
        env = dict(os.environ)
        env["HSCC_WALL_SHIFT_DAYS"] = str(shift_days)
        env["HSCC_REPO_ROOT"] = str(REPO_ROOT)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(TESTS_DIR), str(REPO_ROOT)] +
            [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
        return subprocess.run(
            [sys.executable, "-m", "wallclock_shift", mode],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=300)

    def test_with_the_machinery_the_shipped_clock_moves(self):
        got = self._run("with", SHIFT_DAYS)
        assert got.returncode == 0, (
            "the shift machinery claims to move the shipped clock and cannot: "
            f"{got.stdout}{got.stderr}")

    def test_without_the_machinery_the_clock_stays_put(self):
        got = self._run("without", SHIFT_DAYS)
        assert got.returncode == 0, (
            "the shipped clock reads the shifted instant with the machinery "
            f"ABSENT — the environment is already shifted, so the sweep below "
            f"would prove nothing: {got.stdout}{got.stderr}")


_COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")


def _counts(stdout):
    """The pytest summary counts, so a shifted run can be compared to its own
    control by CASE COUNT and not only by exit code."""
    tail = stdout[-3000:]
    out = {}
    for num, word in _COUNTS.findall(tail):
        out[word.rstrip("s")] = int(num)
    return out


@pytest.fixture(scope="class")
def anchor_control():
    """Green in isolation, and how many cases that took.

    The leg runs this directory as ONE process and these cases are
    order-sensitive, so an isolated run may legitimately differ. When it does,
    the honest answer is "not provable here", not a green.
    """
    files = _anchor_files()
    assert files, "nothing anchored — the sweep would guard nothing"
    got = _run_pytest(files, 0)
    if got.returncode != 0:
        pytest.skip(
            "the anchored files are not green in an isolated run, so a "
            f"shift run would prove nothing. Tail:\n{got.stdout[-2000:]}")
    return {"files": files, "passed": _counts(got.stdout).get("passed", 0)}


class TestFixturesSurviveTheWallClock:
    """No case in an anchored file may change verdict when the date moves."""

    @pytest.mark.parametrize("direction", [1, -1], ids=["forward", "backward"])
    def test_the_verdicts_do_not_move_with_the_date(self, anchor_control,
                                                    direction):
        """The rule, executed.

        ``test_node_pause_expiry.py`` reddened in production on the forward
        direction; the five ``test_cron_watchers.py`` cases t_ed416771 swept
        redden on the backward one (their stamps came from the wall clock while
        their verdicts were judged at a 2026-11-01 literal, so the claim was
        only true while real now sat AFTER the literal). One direction is not
        enough to call a suite clock-clean.
        """
        got = _run_pytest(anchor_control["files"], direction * SHIFT_DAYS)
        # rc == 0 alone is not enough: a run that collected a third of the
        # cases would be green and prove nothing. The shifted run has to answer
        # for the SAME number of cases the control run answered for.
        moved = _counts(got.stdout)
        assert moved.get("passed", 0) == anchor_control["passed"], (
            f"the shifted run answered for {moved.get('passed', 0)} cases while "
            f"its control answered for {anchor_control['passed']} — a run that "
            "collects less proves less, whatever its exit code says. Tail:\n"
            f"{got.stdout[-1500:]}")
        assert got.returncode == 0, (
            f"moving the wall clock {direction * SHIFT_DAYS:+.0f} days changed "
            f"the verdict of an anchored test file — two clocks in one case "
            "(the case stamps or reads on one clock and judges on the other). "
            "Fix it the way the rule says: hand the fixture clock to the call "
            "under test, or judge the real writer's stamp by the real clock.\n"
            f"---- tail ----\n{got.stdout[-4000:]}")
