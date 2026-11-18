"""Structural guard — exactly ONE posture per hermes capability on this leg.

``hscc-roles/tests/hermes_gap.py`` is this leg's single definition of "can THIS
interpreter run hermes?". The point only holds while nobody re-derives the answer
next door, and this leg had three private answers until t_74141b24 — a child
``returncode`` read as "not importable", a production helper's ``is not None``
frozen into three ``@pytest.mark.skipif`` marks, and a bare ``skip()`` naming no
module. Same idiom as ``hscc-api/tests/test_hermes_gap_single_posture.py`` (whose
card deliberately did NOT fold this leg in — different leg, different conftest,
different capability), ``hscc_daemon/tests/test_planted_hermes_cli_single_source.py``
and ``scripts/tests/test_pane_matcher_single_source.py``: sweep every tracked file
in scope, green only while the rule lives in its ONE owner.

What counts as a second posture (one test each):

  * a ``def`` of any of the owner's probe names outside the owner — the shape that
    becomes a competing rule;
  * the private-copy SHAPE: a ``try`` around a ``hermes*`` import whose ``except``
    arm records the failure;
  * a second skip REASON for a capability the owner already words — a wording
    drift is how ``-rs`` stops naming the missing module, which is the one thing
    the runner's stamp rule reads;
  * a file that reaches for the real ``hermes_cli`` — in an import OR in the source
    of a child it spawns — without importing the owner: the inverse drift,
    where the case cannot run on a thin sibling and goes RED instead of saying so;
  * a gated case whose gate is NOT the first statement, or a case gated that the
    owner's roster of gated cases does not name — the gate must be the decision
    this file records, not something that quietly widens (a gate that skips more
    than it should makes a leg green by silencing it).

Scope decisions (stated, not hidden):

  * The sweep is ``hscc-roles/tests/*.py``. ``hscc-api/tests`` has its own owner
    and its own guard; ``hscc_daemon`` has ``tests/real_hermes.py``;
    ``hscc-project`` has ``_require_hermes_state`` in its own file. Who owns what
    is ``scripts/run_tests.sh``'s roster block, and ``test_the_live_roster_...``
    pins that block against the code from BOTH directions.
  * ``generator.py`` / ``rolelib.py`` are out of scope: they are PRODUCTION code,
    they must not skip, and their guarded-import-with-fallback is the shipped
    behaviour on a machine without hermes. This guard is about TEST postures.

Every swept literal is ASSEMBLED AT RUNTIME (the ``address_guard`` idiom) — this
file sweeps for those literals, so carrying one contiguously would make the
sweeper its own first hit. The skip reasons are not re-typed here at all: they are
read out of the owner's own ``CAPABILITIES`` table, so there is no second copy of
the wording to drift from.
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

import hermes_gap

REPO = Path(__file__).resolve().parents[2]

OWNER = "hscc-roles/tests/hermes_gap.py"
ROSTER = "scripts/run_tests.sh"
SWEEP_ROOT = "hscc-roles/tests"

#: The probe owners the runner's roster block claims, across the repo. Extended
#: for this leg by t_74141b24; ``hscc-api/tests/test_hermes_gap_single_posture.py``
#: carries the same list and both have to move together.
ALL_OWNERS = (OWNER,
              "hscc-api/tests/hermes_gap.py",
              "hscc_daemon/tests/real_hermes.py",
              "hscc-project/tests/test_session_discovery.py")

#: Files whose cases the owner gates, and which cases. ``test_the_gated_cases_...``
#: pins this against the source in BOTH directions: every name here really calls
#: the gate first, and no test outside this list calls it anywhere.
GATED_CASES = {
    "hscc-roles/tests/test_profile_root_assert.py": (
        "test_requested_home_inside_the_native_root_resolves_back_to_live",
        "test_assert_profile_refuses_a_home_inside_the_live_root",
        "test_refused_intent_writes_nothing_for_either_profile_step"),
    "hscc-roles/tests/test_sync_profile_root.py": (
        "test_refused_intent_writes_nothing",),
    "hscc-roles/tests/test_generator.py": (
        "test_operator_comments_survive_a_real_change",
        "test_a_write_that_would_lose_keys_fails_loudly",
        "test_no_op_regeneration_never_reaches_the_writer",
        "test_unparseable_config_is_not_silently_replaced"),
}

#: The owner's module name as it appears in an import (assembled: this file
#: sweeps for it).
OWNER_MODULE = "".join(("hermes", "_gap"))
#: The real runtime modules this leg's capabilities are about (assembled).
REAL_CLI = "".join(("hermes", "_cli"))
CHILD_IMPORT_SHAPE = f"from {REAL_CLI}"


def _tracked_py_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", SWEEP_ROOT],
                         cwd=str(REPO), capture_output=True, text=True,
                         check=True).stdout
    files = sorted(p for p in out.split("\0") if p.endswith(".py"))
    assert OWNER in files, (
        f"{OWNER} is not tracked — the guard would silently sweep nothing")
    assert files, f"nothing tracked under {SWEEP_ROOT}"
    return files


def _imports_of(node):
    """Every ``(module, imported-name)`` pair under *node*, nested or not — the
    posture question is what the file DOES, not where it does it."""
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            for alias in child.names:
                yield (child.module or "", alias.name)
        elif isinstance(child, ast.Import):
            for alias in child.names:
                yield (alias.name, "")


def _gate_calls(tree):
    """``(function_name, lineno, is_first_statement)`` for every call to the
    owner's ``require_*`` API in *tree*."""
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        first = _first_executable(fn)
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
            f = call.func
            if (isinstance(f, ast.Attribute)
                    and isinstance(f.value, ast.Name)
                    and f.value.id == OWNER_MODULE
                    and f.attr.startswith("require_")):
                yield (fn.name, call.lineno,
                       first is not None and first.lineno == call.lineno)


def _first_executable(fn):
    """A function's first real statement, docstring skipped."""
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body[0] if body else None


# --------------------------------------------------------------------------- #
# anti-vacuity: the sweep has work to do, or it proves nothing
# --------------------------------------------------------------------------- #

def test_the_sweep_sweeps_the_owner_and_the_gated_files():
    """A guard whose predicate is vacuously true is worse than no guard.

    Pins the three things every test below leans on: the owner is in the swept
    set, the owner really defines the probe names it advertises, and every gated
    file is still tracked and still imports the owner. Without the last one the
    structural tests could pass on an empty sweep.
    """
    files = _tracked_py_files()
    assert OWNER in files
    owner_text = (REPO / OWNER).read_text()
    for name in hermes_gap.PROBES:
        assert f"def {name}(" in owner_text, (
            f"{OWNER} advertises {name!r} in PROBES but does not define it — the "
            "guard would be checking a name that does not exist")
    assert hermes_gap.CAPABILITIES, "the owner's capability table is empty"
    for capability, reason in hermes_gap.CAPABILITIES:
        assert reason in owner_text, (
            f"{OWNER}: the CAPABILITIES reason for {capability} is not the wording "
            "the module actually skips with")
    for rel in GATED_CASES:
        assert rel in files, f"{rel} vanished from the sweep"
        tree = ast.parse((REPO / rel).read_text())
        assert any(mod == OWNER_MODULE for mod, _ in _imports_of(tree)), (
            f"{rel} no longer imports the gate from {OWNER}")


# --------------------------------------------------------------------------- #
# the shapes of a second posture
# --------------------------------------------------------------------------- #

def test_no_other_file_defines_a_probe_of_a_hermes_capability():
    """Only the owner may ``def`` a capability probe for this leg."""
    offenders = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        for n, line in enumerate((REPO / rel).read_text().splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("def "):
                continue
            name = stripped[len("def "):].split("(")[0].strip()
            if name in hermes_gap.PROBES:
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        f"a hermes capability probe has more than one definition — it lives in "
        f"{OWNER}:\n" + "\n".join(offenders))


def test_no_other_file_probes_a_hermes_import_with_a_local_try_except():
    """The private-copy SHAPE: record an import failure instead of asking.

    This is what ``test_generator.py`` did through production code and what
    ``test_profile_root_assert.py`` did through a child's exit status. It reads as
    harmless and is not: the answer is captured once (at import, or whenever the
    child last ran) and can only ever move a case from running to skipped.
    """
    offenders = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        tree = ast.parse((REPO / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            mods = {mod for mod, _ in _imports_of(node)
                    if mod.startswith(REAL_CLI) and mod != OWNER_MODULE}
            if not mods:
                continue
            recorded = [ast.unparse(stmt) for handler in node.handlers
                        for stmt in handler.body]
            offenders.append(f"{rel}:{node.lineno}: imports {sorted(mods)}, "
                             f"records it as {recorded}")
    assert not offenders, (
        "a file probes a hermes import for itself instead of asking "
        f"{OWNER} (a probe that caches is a silent blind spot):\n"
        + "\n".join(offenders))


def test_no_other_file_skips_with_a_hermes_capability_reason():
    """One skip REASON per capability, written in one place.

    The reasons come from the owner's own table, so this catches a private copy
    even when it copies the wording exactly — and a file that words it DIFFERENTLY
    is caught by the pair below it (a gate that skips without calling the owner has
    to say something, and the something is the fingerprint).
    """
    hits = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        text = (REPO / rel).read_text()
        for capability, reason in hermes_gap.CAPABILITIES:
            if reason in text:
                hits.append(f"{rel}: {capability} reason {reason!r}")
    assert not hits, (
        f"a skip reason for a hermes capability was written outside {OWNER} "
        "(call the probe instead):\n" + "\n".join(hits))


def test_every_file_that_reaches_for_the_real_hermes_cli_gates_through_owner():
    """The inverse drift: a case that needs the real ``hermes_cli`` without the
    shared gate goes RED on a thin sibling, and the stamp rule calls that a
    regression.

    Two shapes, because this leg drives production through CHILDREN: the AST
    import (an in-process case) and the import statement texted into a child —
    which is how ``test_profile_root_assert.py`` reaches the resolver, and which
    an import-only sweep would walk straight past.
    """
    offenders = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        text = (REPO / rel).read_text()
        tree = ast.parse(text)
        pairs = list(_imports_of(tree))
        needs_gate = (any(mod == REAL_CLI or mod.startswith(REAL_CLI + ".")
                          for mod, _ in pairs)
                      or CHILD_IMPORT_SHAPE in text)
        has_gate = any(mod == OWNER_MODULE for mod, _ in pairs)
        if needs_gate and not has_gate:
            offenders.append(rel)
    assert not offenders, (
        "these files reach the real hermes_cli without the shared gate (they will "
        f"FAIL, not skip, on a thin interpreter): {offenders} — import the probe "
        f"from {OWNER}")


def test_the_gated_cases_are_exactly_the_named_ones_and_the_gate_is_first():
    """The gate is a recorded decision, not something that widens by accident.

    Both directions: every case in ``GATED_CASES`` calls the owner's gate as its
    FIRST statement (before any subprocess spawn or write — a gate after the work
    has already run has decided nothing), and NO other test function in the leg
    calls it. Adding a gated case is therefore a visible edit to this table, the
    same way the runner's roster block works: silence is not an option.
    """
    seen = set()
    for rel, cases in GATED_CASES.items():
        tree = ast.parse((REPO / rel).read_text())
        calls = {fn: (lineno, is_first) for fn, lineno, is_first in _gate_calls(tree)}
        for case in cases:
            assert case in calls, (
                f"{rel}::{case} is listed as gated but no longer calls the gate — "
                "it now runs unguarded on a thin interpreter")
            lineno, is_first = calls[case]
            assert is_first, (
                f"{rel}::{case} calls the gate at line {lineno}, which is not its "
                "first statement — everything before it already ran (spawned, "
                "wrote) on an interpreter that cannot satisfy the case")
            seen.add((rel, case))
    extra = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        tree = ast.parse((REPO / rel).read_text())
        for fn, _lineno, _first in _gate_calls(tree):
            if (rel, fn) not in seen:
                extra.append(f"{rel}::{fn}")
    assert not extra, (
        "test(s) call the gate that this guard does not know about: "
        f"{sorted(extra)} — either they belong in GATED_CASES (say why the case "
        "cannot run without hermes) or the gate does not belong in them")


# --------------------------------------------------------------------------- #
# the runner's roster block is the LIVE authority — pin it to the code
# --------------------------------------------------------------------------- #

def test_the_live_roster_in_the_runner_names_exactly_the_probes_that_exist():
    """``scripts/run_tests.sh``'s roster comment and the probe modules agree.

    The audit page is the dated measurement; the runner's block is what a reader of
    a red leg consults. A probe that exists but is not listed is a capability
    nobody can find; a gate the block names with no module behind it is a roster
    that lies. Both directions, one test — the audit's own roster went stale
    exactly this way (two families added, neither listed).
    """
    roster = (REPO / ROSTER).read_text()
    advertised = set(re.findall(r"\b_?require_[a-z_]+\b", roster))
    assert advertised, f"{ROSTER} advertises no capability gate at all"
    texts = {}
    for rel in ALL_OWNERS:
        path = REPO / rel
        assert path.exists(), f"probe owner {rel} is gone"
        texts[rel] = path.read_text()

    unbacked = sorted(name for name in advertised
                      if not any(f"def {name}(" in text for text in texts.values()))
    assert not unbacked, (
        f"{ROSTER}'s roster names gate(s) that no probe owner defines: "
        f"{unbacked} — the roster is the live authority, so it has to be true")

    defined = set(re.findall(r"^def (require_[a-z_]+)\(", texts[OWNER],
                             re.MULTILINE))
    assert defined == {n for n in hermes_gap.PROBES if n.startswith("require_")}, (
        f"{OWNER} defines {sorted(defined)} but advertises "
        f"{sorted(n for n in hermes_gap.PROBES if n.startswith('require_'))}")
    missing = sorted(name for name in defined if name not in advertised)
    assert not missing, (
        f"{ROSTER}'s roster does not list this leg's probe(s) {missing} — a "
        "capability a reader cannot look up is how the audit's roster went stale "
        "in the first place")


# --------------------------------------------------------------------------- #
# the posture itself, executed rather than asserted from text
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("capability,probe,getter", (
    (REAL_CLI + ".profiles", "require_hermes_cli_profiles",
     "hermes_cli_profiles_import_error"),
    (REAL_CLI + ".config", "require_hermes_cli_config_writer",
     "hermes_cli_config_writer_import_error"),
))
def test_the_probes_are_capability_probes_on_both_sides(capability, probe, getter):
    """Each probe, run in BOTH directions on every interpreter.

    Where the import succeeds the gate must NOT skip — a gate that skipped
    everywhere would make this leg green by silencing it, and on the canonical
    interpreter that is the only failure mode that is even observable. Where it
    fails, the skip must carry the import error itself, because that text is what
    ``scripts/run_tests.sh``'s stamp rule reads off ``-rs``.

    Deliberately NOT gated by the thing it tests: it runs on both sides, on both
    interpreters, and is keyed on a capability — never on ``sys.version_info``,
    which is the posture this whole exercise exists to forbid.
    """
    require = getattr(hermes_gap, probe)
    error = getattr(hermes_gap, getter)()
    if error is None:
        try:
            require()
        except pytest.skip.Exception as exc:
            pytest.fail(
                f"{probe} skipped although {capability} imports on this "
                f"interpreter ({exc}) — every gated case here would pass "
                "vacuously")
    else:
        with pytest.raises(pytest.skip.Exception) as exc:
            require()
        assert error in str(exc.value), (
            "the skip must carry the import error itself, which is what the "
            f"runner's stamp rule reads, got: {exc.value}")
        assert capability in str(exc.value), (
            f"the skip must name the capability ({capability}) it probed, got: "
            f"{exc.value}")
