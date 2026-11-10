"""Structural guard — exactly ONE "is this a planted fake ``hermes_cli``?" rule.

t_b62da70c's own acceptance criterion is "one predicate, no duplication", and a
fix that only moves text is not closed until re-copying it fails. This suite
already paid for the missing check twice: PR #270 folded the rule into conftest
after one file's planted fake decided a later file's board enumeration, and two
private copies of the same rule survived that fix (``test_autodown``
matched its own tmp dir plus a substring of the pytest tmp-dir marker;
``test_board_pin`` matched the marker alone). Same posture as
``scripts/tests/test_pane_matcher_single_source.py`` and
``test_no_real_addresses_committed.py``: a sweep judges every tracked file and
passes only while the predicate lives in its ONE owner.

Owner: ``hscc_daemon/tests/hermes_cli_plant.py``.

What counts as a copy:

  * a ``def`` of any of the owner's function names outside the owner — the only
    shape that can drift into a second rule;
  * the pytest tmp-dir marker literal anywhere else in the package — a
    substring match on the tmp-dir path IS the private-copy shape the two
    survivors used, and nothing else in ``hscc_daemon`` needs it;
  * the macOS private temp root literal, the other tell of the temp-root RULE
    (an ordinary temp-file test uses ``tempfile.gettempdir()`` and never names
    it).

Fragments are ASSEMBLED AT RUNTIME (the ``address_guard`` / ``ledger_scrub``
idiom): this file sweeps for those literals, so carrying one contiguously would
make the sweeper its own first hit.

Scope decisions (stated, not hidden):

  * The sweep is ``hscc_daemon/**/*.py`` — the package whose harness plants
    these fakes and whose conftest owns the autouse guarantee.
  * ``hscc-cluster/tests/sparkrun_fence.py::_under_pytest_tmp`` also names the
    tmp-dir marker and is deliberately NOT folded in: it answers a different
    question (is this ``sparkrun`` argv a per-test fake?) and requires a SECOND
    marker (a ``test_`` component) precisely so the shared pytest root still
    counts as the machine's real CLI. Making it call ``is_planted_path`` would
    widen the fence and start fencing the live CLI. Recorded here so the next
    reader does not "fix" it.
  * A test may of course *build* a plant — that is constructing a path under a
    temp root, which is what the owner's own helpers and
    ``test_hermes_cli_import_isolation._temp_planted_module`` do. What is banned
    is a second rule for deciding it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

OWNER = "hscc_daemon/tests/hermes_cli_plant.py"
SWEEP_ROOT = "hscc_daemon"

#: The owner's API. A ``def`` of one of these names anywhere else is a fork.
OWNER_FUNCTIONS = ("is_planted_path", "hermes_cli_names", "poison_reason",
                   "purge_planted", "evict_planted", "restore_planted")

#: Every file that evicts a plant must obtain the rule by import.
SITES = ("hscc_daemon/tests/conftest.py",
         "hscc_daemon/tests/test_autodown.py",
         "hscc_daemon/tests/test_board_pin.py",
         # the nested probe file B, which used to re-derive the temp-root rule
         "hscc_daemon/tests/test_hermes_cli_import_isolation.py")


def _frag(*parts):
    """Assemble a swept literal at runtime — see the module docstring."""
    return "".join(parts)


TMP_DIR_MARKER = _frag("pytest-", "of-")
PRIVATE_TEMP_ROOT = _frag("/private", "/var/folders")


def _tracked_py_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", SWEEP_ROOT],
                         cwd=str(REPO), capture_output=True, text=True,
                         check=True).stdout
    files = sorted(p for p in out.split("\0") if p.endswith(".py"))
    assert OWNER in files, (
        f"{OWNER} is not tracked — the guard would silently sweep nothing")
    return files


def test_the_predicate_is_defined_in_exactly_one_file():
    """No second file may ``def`` any part of the planted-fake predicate."""
    offenders = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        for n, line in enumerate((REPO / rel).read_text(errors="ignore")
                                 .splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("def "):
                continue
            name = stripped[len("def "):].split("(")[0].strip()
            if name in OWNER_FUNCTIONS:
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        "the planted-hermes_cli predicate has more than one definition — it "
        "lives in " + OWNER + ":\n" + "\n".join(offenders))


def test_no_other_file_carries_the_rule_literals():
    """The literals are the rule's fingerprints; a copy names at least one.

    ``TMP_DIR_MARKER`` is what both private copies substring-matched, and
    ``PRIVATE_TEMP_ROOT`` is the second half of the one temp-root tuple — an
    ordinary test needs neither.
    """
    hits = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        text = (REPO / rel).read_text(errors="ignore")
        for label, frag in (("pytest tmp-dir marker", TMP_DIR_MARKER),
                            ("macOS private temp root", PRIVATE_TEMP_ROOT)):
            if frag in text:
                hits.append(f"{rel}: {label}")
    assert not hits, (
        "a competing copy of the planted-hermes_cli rule was found (call "
        + OWNER + " instead):\n" + "\n".join(hits))


def test_every_site_that_evicts_a_plant_imports_the_owner():
    """Textual, so it holds whether or not a given file is collected here."""
    for rel in SITES:
        text = (REPO / rel).read_text(errors="ignore")
        assert "hermes_cli_plant import" in text, (
            f"{rel} handles a planted hermes_cli but does not import the one "
            f"predicate from {OWNER}")


def test_conftest_rebinds_the_owner_objects_rather_than_copying_them():
    """The acceptance claim is ONE predicate, not two that look alike.

    conftest must not be *imported* to check this (a second import re-runs its
    sparkrun-fence loader), so the already-loaded module object is looked up by
    name — the same trick ``test_hermes_cli_import_isolation._conftest`` uses.
    """
    from hscc_daemon.tests import hermes_cli_plant

    cf = None
    for name in ("hscc_daemon.tests.conftest", "conftest"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_hermes_cli_poison_reason"):
            cf = mod
            break
    if cf is None:  # pragma: no cover — only reachable outside a pytest run
        pytest.skip("conftest is not loaded under a known name in this run")
    assert cf is not None, "unreachable: pytest.skip does not return"

    # Identity, not equality: an alias proves the fixture and the predicate are
    # the same code, a re-implementation would not be.
    assert cf._is_planted_path is hermes_cli_plant.is_planted_path
    assert cf._hermes_cli_names is hermes_cli_plant.hermes_cli_names
    assert cf._hermes_cli_poison_reason is hermes_cli_plant.poison_reason
    assert cf._purge_planted_hermes_cli is hermes_cli_plant.purge_planted
    assert cf._evict_planted_hermes_cli is hermes_cli_plant.evict_planted


def test_importing_the_owner_cannot_disturb_the_running_suite():
    """Why the rule lives in a module and not in conftest.

    A test module cannot ``import conftest`` to reach the predicate: a second
    import of conftest re-runs its module-level sparkrun-fence loader and swaps
    the live fence out from under the suite. The shared module must therefore be
    genuinely inert on import — asserted in a FRESH interpreter, because the
    point of the card is that import side effects are what break a leg.
    """
    script = (
        "import sys\n"
        "path_before = list(sys.path)\n"
        "mods_before = set(sys.modules)\n"
        "import hscc_daemon.tests.hermes_cli_plant as plant\n"
        "new = sorted(set(sys.modules) - mods_before)\n"
        "assert list(sys.path) == path_before, (\n"
        "    'importing the predicate changed sys.path: %r' % sys.path)\n"
        "bad = [m for m in new if m.startswith('hermes_cli')\n"
        "       or 'conftest' in m or 'sparkrun_fence' in m]\n"
        "assert not bad, 'importing the predicate pulled in %r' % (bad,)\n"
        "assert plant.is_planted_path('/nope/x.py') is False\n"
        "print('INERT-IMPORT')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INERT-IMPORT" in proc.stdout, proc.stdout
