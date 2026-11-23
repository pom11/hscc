"""t_c11f0d88: the kanban read surfaces must answer the same with or without a
flightdeck project registry on the machine.

WHY THIS FILE EXISTS. ``hscc kanban stale`` (``kanban_cli.py:253``),
``hscc kanban blocked`` (``kanban_blocked.py:441``) and the live board widget
(``kanban_live.py:1310``) all narrow their board list to *registered project
boards* via ``autodown._enum_board_names(..., registered_only=True)``. That
seam reads the operator's ``~/.flightdeck/registry.yaml`` (or
``$FLIGHTDECK_REGISTRY``), and an absent registry is ``(set(), None)`` — "no
project boards", deliberately (``autodown.py:341-343``). ``test_listing_json_valid``
patched the kanban-lib seam and nothing else, so its fake ``hscc`` board cleared
the filter only because the OPERATOR has an ``hscc`` project registered:

    $ pytest -q hscc_daemon/tests/test_kanban_cli.py -k listing_json_valid
    1 passed                       # workstation: the operator's registry is read
    $ FLIGHTDECK_REGISTRY=/tmp/does-not-exist-registry.yaml pytest -q … same …
    1 failed  # IndexError: list index out of range  — the runner's exact red

So the whole family was green on a laptop and red on a clean runner, silently,
because ``HSCC_TESTS_ENFORCE`` is unset and the leg verdict turns that into a
warning annotation.

THE GUARD. Poison ``FLIGHTDECK_REGISTRY`` with a path that does not exist — the
CI posture — and re-run the family out of process; the answer must be the same
as the un-poisoned answer. Out of process because the poison must hold for the
whole run (the ``t_c51c87d7`` pattern), and because the files under test are the
ones whose fixtures would otherwise normalise the difference away.

Teeth: measured, not asserted. Deleting the pins in ``test_kanban_cli.py`` makes the
TWO behavioural tests below red — and red even on a workstation where the un-pinned
family still reports "all passed" (probe M2 in the card handoff / the audit doc). The
structural assertions in ``test_the_pinned_seam_is_still_there`` are the cheap backstop
that fires even when the sub-process cannot be spawned.
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(__file__)

#: The files that reach the ``registered_only=True`` narrowing through their CLI
#: entry points. Every one of them pins ``registered_board_names``; this guard is
#: what keeps them pinned.
TARGETS = ("test_kanban_cli.py", "test_kanban_blocked.py", "test_kanban_live.py")

#: A path shaped like a registry and guaranteed not to exist (the premise test
#: below pins that): the CI posture is a machine that has never registered a
#: project, exactly the ``FLIGHTDECK_REGISTRY=/tmp/does-not-exist.yaml`` repro.
MISSING_REGISTRY = os.path.join(
    tempfile.gettempdir(), "hscc-t_c11f0d88-no-such-flightdeck-registry.yaml")

#: A ``monkeypatch.setattr(..., "registered_board_names", lambda ...: ({"x", ...}, ...))``
#: — i.e. a pin that hands the narrowing something to keep. Written against the
#: two-line call shape the family uses.
_NON_EMPTY_PIN = re.compile(
    r"""registered_board_names",\s*lambda[^:]*:\s*\(\s*\{[^{}]+\}""")


def _run_poisoned(extra_env=None):
    """Run the family in a child pytest with a missing project registry."""
    env = dict(os.environ)
    env["FLIGHTDECK_REGISTRY"] = os.path.abspath(MISSING_REGISTRY)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TARGETS, "-p", "no:cacheprovider"],
        env=env, cwd=TESTS_DIR, capture_output=True, text=True, timeout=600)


def _run_plain():
    """The same run with the machine's own registry posture (usually present)."""
    env = dict(os.environ)
    env.pop("FLIGHTDECK_REGISTRY", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TARGETS, "-p", "no:cacheprovider"],
        env=env, cwd=TESTS_DIR, capture_output=True, text=True, timeout=600)


def _counts(proc):
    """``(passed, failed, errors, skipped)`` read off the pytest summary line."""
    out = proc.stdout + proc.stderr
    def _num(pattern):
        m = re.search(pattern, out)
        return int(m.group(1)) if m else 0
    return (_num(r"(\d+) passed"), _num(r"(\d+) failed"),
            _num(r"(\d+) errors?"), _num(r"(\d+) skipped"))


# ---------------------------------------------------------------------------
# premise
# ---------------------------------------------------------------------------

def test_the_registry_path_this_guard_poisons_really_is_absent():
    """If the poisoned path ever exists, every test below proves nothing."""
    assert not os.path.exists(os.path.abspath(MISSING_REGISTRY)), (
        f"{MISSING_REGISTRY} exists — the 'no registry' posture this guard "
        "stands for is no longer being measured")
    # The guard runs the files it names; a self-run would recurse.
    assert SELF not in TARGETS
    for name in TARGETS:
        assert os.path.exists(os.path.join(TESTS_DIR, name)), name


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

def test_the_kanban_read_surfaces_are_green_with_no_project_registry():
    """CI posture: no flightdeck registry anywhere on the machine.

    This is the acceptance measurement of t_c11f0d88 — the family must be green
    when ``registered_board_names()`` has nothing to hand it, because that is
    the only posture the runner has.
    """
    proc = _run_poisoned()
    passed, failed, errors, skipped = _counts(proc)
    assert proc.returncode == 0, (
        "the kanban family is RED with no project registry — a test in "
        f"{TARGETS} reads host registry state:\n"
        + proc.stdout[-6000:] + proc.stderr[-2000:])
    assert (failed, errors) == (0, 0)
    # A green report from a run that executed nothing would be the same shape:
    # pin that the run actually collected the family.
    assert passed > 50, f"suspiciously few tests ran: {proc.stdout[-2000:]}"


def test_the_answer_does_not_move_when_the_registry_appears():
    """The same run, this time with whatever registry the host has.

    'Hermetic' does not mean 'passes only when the poison is set' — it means the
    answer is the same in both postures. Counts (not just green-ness) are
    compared, so a test that quietly skips itself out of one posture is caught.
    """
    poisoned = _counts(_run_poisoned())
    plain = _counts(_run_plain())
    assert poisoned == plain, (
        "the kanban family answers differently with and without a project "
        f"registry: poisoned={poisoned} plain={plain} — a case is "
        "registry-dependent (or silently skipping in one posture)")
    assert poisoned[1] == poisoned[2] == 0, (
        f"the family is not green in both postures: {poisoned} / {plain}")


# ---------------------------------------------------------------------------
# anti-vacuity
# ---------------------------------------------------------------------------

def test_the_pinned_seam_is_still_there():
    """The pins this guard exists to protect must still be in the sources.

    Behavioural coverage above can only fail if the child run happens; a reader
    (or a future conftest change that swallows collection) can silence that. The
    two shapes below are what the family's hermeticity actually rests on:

      * ``test_kanban_cli.py`` pins ``registered_board_names`` INSIDE
        ``test_listing_json_valid`` — the case that was red on the runner;
      * the same file carries the autouse fence that answers an *unpinned*
        reliance with an error rather than with the host's registry.
    """
    src = Path(TESTS_DIR, "test_kanban_cli.py").read_text(encoding="utf-8")
    assert "autouse=True" in src and "registered_board_names" in src, (
        "test_kanban_cli.py lost its project-registry fence (t_c11f0d88)")

    # The case itself, with its pin, still present. "Present" means a pin that
    # answers with a NON-EMPTY board set: the same test also measures the
    # product rule with an empty one, and a checker that only looked for the
    # symbol would stay green on a file that had deleted the narrowing pin and
    # kept that measurement (measured, t_c11f0d88 probe M3).
    m = re.search(r"def test_listing_json_valid\(.*?\n(?=    def |\nclass |\Z)",
                  src, re.S)
    assert m, "test_listing_json_valid vanished from test_kanban_cli.py"
    assert _NON_EMPTY_PIN.search(m.group(0)), (
        "test_listing_json_valid no longer pins registered_board_names to a "
        "non-empty board set — it will pass on a workstation that has an "
        "'hscc' project registered and IndexError on a clean runner (the exact "
        "red of t_c11f0d88)")

    # The family files that reach the narrowing keep their own pins.
    for name in ("test_kanban_blocked.py", "test_kanban_live.py"):
        body = Path(TESTS_DIR, name).read_text(encoding="utf-8")
        assert "registered_board_names" in body, (
            f"{name} reached the registered-only narrowing without a pin")
