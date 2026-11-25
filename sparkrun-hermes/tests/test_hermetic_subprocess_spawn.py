"""Leg guard: sparkrun-hermes's own tests must spawn the tree and interpreter they claim.

No violation was found in this leg when the ban landed; the guard is prospective — the class it catches arrived in hscc-cluster from an ordinary-looking test addition, and nothing stops it arriving here.

The ban itself lives in ``scripts/hermetic_spawn_guard.py`` and is unit-tested
there; this file applies it to THIS leg dir and nothing else. Per-leg scope is
deliberate and inherited (the ``test_hermes_gap_single_posture.py`` doctrine,
t_0fd2c149): a leg's guard polices its own dir, because a repo-wide sweep would
judge one leg's fixture-data posture by another leg's rules — and a ban that
false-positives gets deleted rather than fixed.

Fail-closed, on purpose. A detector that cannot be loaded raises rather than
skips, and a sweep that found zero files fails rather than passing: a green
report from a job that looked at nothing is the failure mode this whole gate
exists to close, and a guard that can skip is a guard a mutant can silence.
"""

import importlib.util
from pathlib import Path

LEG_DIR = Path(__file__).resolve().parent
LEG_LABEL = 'sparkrun-hermes'


def _load_detector():
    """Load ``scripts/hermetic_spawn_guard.py`` by path.

    By path, not by ``sys.path`` insert: this suite has a documented history of
    path and ``sys.modules`` leaks across files, and a guard should not add one.
    Walking up for ``scripts/`` rather than hardcoding a parent depth keeps this
    working for ``scripts`` and ``.github/scripts``, whose tests sit one level
    deeper than a plugin's.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "scripts" / "hermetic_spawn_guard.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location(
                "hermetic_spawn_guard_under_test", candidate)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise AssertionError(
        "scripts/hermetic_spawn_guard.py is missing above %s — the detector was "
        "deleted or this file moved; a skip here would silence the ban" % here)


guard = _load_detector()

#: One source holding BOTH banned shapes. If the sweep cannot see it, the sweep
#: is not looking, and every green below it is unprovenanced.
_MUST_BE_BANNED = """
import subprocess
from pathlib import Path
subprocess.run(["python", "x.py"], cwd="/Users/hscc-fixture/dev/hscc")
subprocess.run([str(Path.home() / "venv" / "bin" / "python"), "y.py"])
""".strip()


def test_the_sweep_bites_on_a_source_that_must_be_banned():
    """Non-vacuity arm: provenance for the leg's own green.

    Run first, so a leg that went green over an empty file list or a neutered
    detector reports as what it is instead of passing.
    """
    kinds = sorted(v.kind for v in guard.scan_source(_MUST_BE_BANNED, "probe.py"))
    assert kinds == sorted([guard.KIND_INTERPRETER,
                              guard.KIND_HOME_PATH,
                              guard.KIND_HOME_EXPRESSION]), (
        "the detector returned %r for a source that is banned by construction: "
        "the sweep below cannot be trusted" % kinds)


def test_no_leg_test_spawns_a_path_resolved_interpreter_or_a_home_path():
    violations, scanned = guard.scan_leg(LEG_DIR)
    assert scanned > 0, (
        "leg %r swept ZERO python files under %s — the sweep root is wrong, and "
        "a green here would mean nothing" % (LEG_LABEL, LEG_DIR))
    assert not violations, guard.report(violations, LEG_LABEL, scanned)
