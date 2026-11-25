"""Leg guard: hscc-bootstrap's own tests must spawn the tree and interpreter they claim.

This leg had one live violation: ``tests/test_restart_daemon.py`` used argv0
``"python3"`` for the sanity arm of
``test_import_package_stub_succeeds_with_pythonpath``, so which interpreter
answered the "bare path with no PYTHONPATH" check was a property of the machine's
PATH. It now spawns ``sys.executable`` with PYTHONPATH dropped, which reproduces
the ModuleNotFoundError for the same reason on every host.

The ban itself lives in ``scripts/hermetic_spawn_guard.py`` and is unit-tested
there; this file applies it to THIS leg dir and nothing else. Per-leg scope is
deliberate and inherited (the ``test_hermes_gap_single_posture.py`` doctrine,
t_0fd2c149): a leg's guard polices its own dir, because a repo-wide sweep would
judge one leg's fixture-data posture by another leg's rules — and a ban that
false-positives gets deleted rather than fixed. This leg carries ONE arm the
others do not: the real-install path may be spelled in exactly one file, because
this is the leg that keeps an "against the real install" tier (see
``tests/hermes_gap.py``).

Fail-closed, on purpose. A detector that cannot be loaded raises rather than
skips, and a sweep that found zero files fails rather than passing: a green
report from a job that looked at nothing is the failure mode this whole gate
exists to close, and a guard that can skip is a guard a mutant can silence.
"""

import ast
import importlib.util
from pathlib import Path

LEG_DIR = Path(__file__).resolve().parent
LEG_LABEL = 'hscc-bootstrap'


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


# --------------------------------------------------------------------------- #
# the one-owner pin — an integration tier is only pinned while it is countable
# --------------------------------------------------------------------------- #

#: Assembled at runtime: this file sweeps for the home-read spelling, so naming
#: the owner contiguously would make the sweeper its own first hit (the same
#: reason ``hscc-roles/tests/test_hermes_gap_single_posture.py`` assembles every
#: literal it hunts for).
OWNER_MODULE = "hermes" + "_gap"
OWNER_FILE = OWNER_MODULE + ".py"


def _module_level_home_constants(path):
    """``(lineno, name, head)`` for a module-level constant built off a home read.

    Reuses rule 3's own resolver rather than re-deriving a second notion of "is
    this a home read" — two definitions of that is the drift this repo's
    capability-probe doctrine exists to prevent.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    bindings = guard._literal_bindings(tree)
    roots = guard._pathlib_bindings(tree)
    for node in tree.body:                    # MODULE level, deliberately
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        found = guard.home_expression(node.value, bindings, roots)
        if found:
            out.append((node.lineno, target.id, found[0]))
    return out


def test_the_real_install_path_is_spelled_in_the_owner_only():
    """Rule 3 bites where a spawn happens; this stops the shape seeding earlier.

    Measured starting state (t_4e405e3a): FIVE modules here each defined their
    own ``REAL_VENV_PY = Path.home() / ".hermes" / hermes-agent / venv / bin /
    python`` and three handed it to a spawn argv0. The spawns are gone; this arm
    is what keeps the *constants* from creeping back, because a module-level
    home constant is invisible to rule 3 until someone spawns with it — and by
    then five modules already agree on five spellings of one machine path.

    This leg legitimately runs 25 cases against the real install (see
    ``tests/hermes_gap.py`` for why that tier is kept rather than faked). That
    posture is affordable exactly while ONE file names the install: the machine
    reads stay countable, the skip reason stays one wording, and `-rs` says
    something the runner's stamp rule can read. Scope is module level on purpose
    — a function-local read that reaches a spawn is rule 3's business, and a
    function-local read that does not (the live-state checksum in
    ``test_bootstrap_isolated.py``) is a deliberate detective control, not a leak.
    """
    offenders = []
    for path in guard.leg_files(LEG_DIR):
        if path.name == OWNER_FILE:
            continue
        for lineno, name, head in _module_level_home_constants(path):
            offenders.append("%s:%d: %s = <%s>" % (path.name, lineno, name, head))
    assert not offenders, (
        "a module-level constant in this leg is built from a machine home read. "
        "The install path belongs in tests/%s and nowhere else here — ask it "
        "for what you need (real_install_interpreter / real_install_checkout / "
        "require_real_install), which is also the shape rule 3 lets through at a "
        "spawn:\n  " % OWNER_FILE + "\n  ".join(offenders))
