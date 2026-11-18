"""Structural guard — exactly ONE posture per hermes capability on this leg.

``hscc-api/tests/hermes_gap.py`` is the leg's single definition of "can THIS
interpreter run hermes?". That is the whole point of it, and the point is only
kept while nobody re-derives the answer next door: the audit this guard serves
(``docs/audits/sibling-interpreter-hermes-gap_t_02c98915.md``) was written
because one missing module ended up with two postures, and the leg became
indistinguishable from "this branch broke 3.13". ``test_gateway_degraded_tail``
was the last private copy — a module-level ``try/except`` plus a
``_HAS_HERMES_STATE`` flag — and it was folded back in by t_0fd2c149. Until this
file existed, nothing stopped it regrowing.

Same idiom as ``hscc_daemon/tests/test_planted_hermes_cli_single_source.py``
and ``scripts/tests/test_pane_matcher_single_source.py``: a sweep over every
tracked file in scope, green only while the rule lives in its ONE owner.

What counts as a second posture (each caught by its own test below):

  * a ``def`` of any of the owner's probe names outside the owner — the only
    shape that becomes a competing rule;
  * the private-copy SHAPE: a ``try`` that imports a ``hermes*`` module whose
    ``except`` arm records the failure — exactly what the folded-in copy was,
    and the shape that silently caches at COLLECTION (the owner re-checks at
    call time on purpose, see ``hermes_state_import_error``);
  * the owner's skip REASONS — a file that skips with a hermes-capability
    reason without calling the owner has its own wording, and a wording drift is
    how a leg stops telling the operator which import is actually missing;
  * a file that imports the REAL ``hermes_state`` (so it builds a real
    ``SessionDB``) without importing the owner — the inverse drift: an ungated
    real-DB case does not skip on the thin sibling, it goes RED, which the
    runner's stamp rule calls a regression.

Scope decisions (stated, not hidden):

  * The sweep is ``hscc-api/tests/*.py`` — the leg the owner belongs to. The
    other legs have their own owners (``hscc_daemon/tests/real_hermes.py``,
    ``hscc-project/tests/test_session_discovery.py::_require_hermes_state``) and
    ``scripts/run_tests.sh``'s roster block above is the authority for who owns
    what; ``test_the_live_roster_...`` pins that block against the code.
  * ``hscc-roles/tests/test_profile_root_assert.py`` once carried its own
    ``pytest.skip("hermes_cli not importable ...")`` with no probe module behind
    it. Deliberately NOT folded into this sweep — it is a different leg with a
    different capability, and folding it in here would make an hscc-api guard
    police another plugin's fixtures. Recorded as found-by-sweep and carded
    separately (t_0fd2c149); t_74141b24 resolved it the same way, ONE rung over:
    ``hscc-roles/tests/hermes_gap.py`` plus that leg's own guard
    ``hscc-roles/tests/test_hermes_gap_single_posture.py``. So the two legs share
    an idiom and an owner list, not a sweep.
  * ``routes_orchestrator.py`` imports ``hermes_state`` in PRODUCTION code and
    is out of scope: production code must not skip, it must answer honestly.

Fragments are ASSEMBLED AT RUNTIME (the ``address_guard`` idiom): this file
sweeps for those literals, so carrying one contiguously would make the sweeper
its own first hit. That is also why no exemption list is needed — the guard
passes its own sweep.
"""

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

OWNER = "hscc-api/tests/hermes_gap.py"
ROSTER = "scripts/run_tests.sh"
SWEEP_ROOT = "hscc-api/tests"

#: The owner's API. A ``def`` of one of these names anywhere else is a fork.
OWNER_PROBES = ("hermes_state_import_error", "require_hermes_state",
                "hermes_profile_library_import_error",
                "require_hermes_profile_library")

#: Files that must obtain the gate from the owner (the four real-state-DB
#: families the audit's roster lists for this leg).
GATED_FILES = ("hscc-api/tests/test_gateway_backfill.py",
               "hscc-api/tests/test_gateway_degraded_tail.py",
               "hscc-api/tests/test_gateway_store_tail.py",
               "hscc-api/tests/test_session_resolver.py")

#: One entry per capability: what the skip REASON looks like when the owner
#: writes it. A private copy has to say something, and that something is the
#: fingerprint.
CAPABILITY_REASONS = (
    ("hermes_state",
     "".join(("hermes", "_state not importable on this interpreter"))),
    ("hermes_cli.profiles",
     "".join(("hermes", "_cli.profiles not importable on this interpreter"))),
)

#: The real session-DB module (assembled: this file sweeps for it).
REAL_STATE_DB = "".join(("hermes", "_state"))
#: The owner's module name, as it appears in an import statement.
OWNER_MODULE = "".join(("hermes", "_gap"))


def _tracked_py_files():
    out = subprocess.run(["git", "ls-files", "-z", "--", SWEEP_ROOT],
                         cwd=str(REPO), capture_output=True, text=True,
                         check=True).stdout
    files = sorted(p for p in out.split("\0") if p.endswith(".py"))
    assert OWNER in files, (
        f"{OWNER} is not tracked — the guard would silently sweep nothing")
    assert files, f"nothing tracked under {SWEEP_ROOT}"
    return files


def _imports_of(tree):
    """Every ``(module, imported-name)`` pair a file imports, module-level or
    not — the posture question is what the file DOES, not where."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                yield (node.module or "", alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield (alias.name, "")


# --------------------------------------------------------------------------- #
# anti-vacuity: the sweep has work to do, or it proves nothing
# --------------------------------------------------------------------------- #

def test_the_sweep_sweeps_the_owner_and_the_gated_files():
    """A guard whose predicate is vacuously true is worse than no guard.

    Pins the two things every test below depends on: the owner is in the swept
    set, and the four real-state-DB files really do import it. If one of them
    stops importing it, THIS fails before the structural tests can quietly pass
    on an empty set.
    """
    files = _tracked_py_files()
    assert OWNER in files
    for rel in GATED_FILES:
        assert rel in files, f"{rel} vanished from the sweep"
        tree = ast.parse((REPO / rel).read_text())
        assert any(mod == OWNER_MODULE for mod, _ in _imports_of(tree)), (
            f"{rel} no longer imports the gate from {OWNER}")


# --------------------------------------------------------------------------- #
# the three shapes of a second posture
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
            if name in OWNER_PROBES:
                offenders.append(f"{rel}:{n}: {stripped}")
    assert not offenders, (
        f"a hermes capability probe has more than one definition — it lives in "
        f"{OWNER}:\n" + "\n".join(offenders))


def test_no_other_file_probes_a_hermes_import_with_a_local_try_except():
    """The private-copy SHAPE: record an import failure instead of asking.

    This is the mutant that lived in ``test_gateway_degraded_tail`` before
    t_0fd2c149 — ``try: from hermes_state import SessionDB / except:
    _HAS_HERMES_STATE = False``. It reads as harmless and it is not: the flag is
    computed at COLLECTION, so a hermes tree that reaches ``sys.path`` later
    than collection (the production resolvers insert it themselves) leaves the
    cases skipped although they could run, and nothing reports that.
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
                    if mod.startswith("hermes") and mod != OWNER_MODULE}
            if not mods:
                continue
            recorded = [ast.unparse(stmt) for handler in node.handlers
                        for stmt in handler.body]
            offenders.append(f"{rel}:{node.lineno}: imports {sorted(mods)}, "
                             f"records it as {recorded}")
    assert not offenders, (
        "a file probes a hermes import for itself instead of asking "
        f"{OWNER} (a cached-at-collection probe is a silent blind spot):\n"
        + "\n".join(offenders))


def test_no_other_file_skips_with_a_hermes_capability_reason():
    """One skip REASON per capability, written in one place.

    The runner's stamp rule (``scripts/run_tests.sh``) reads a sibling leg by
    its ``-rs`` lines: a leg is green when the only non-passing lines are skips
    that name the missing hermes import. A private wording can name the
    capability while dropping the import error itself, which is the one thing a
    reader needs to tell "thin env" from "dep present and still broken".
    """
    hits = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        text = (REPO / rel).read_text()
        for capability, reason in CAPABILITY_REASONS:
            if reason in text:
                hits.append(f"{rel}: {capability} reason {reason!r}")
    assert not hits, (
        f"a skip reason for a hermes capability was written outside {OWNER} "
        "(call the probe instead):\n" + "\n".join(hits))


def test_every_file_that_touches_a_real_hermes_state_db_gates_through_owner():
    """The inverse drift: an ungated real-``SessionDB`` case goes RED, not soft.

    A file that imports the real ``hermes_state`` builds a real session DB, and
    on the thin sibling that import cannot succeed — so if the file does not
    also import the owner it has no gate, and the leg loses its "a FAILED in a
    sibling leg is a regression" property. ``_FakeSessionDB`` stand-ins
    (``test_routes_sessions``, ``test_routes_activity``) never import the real
    module, so they are correctly absent rather than exempted.
    """
    offenders = []
    for rel in _tracked_py_files():
        if rel == OWNER:
            continue
        tree = ast.parse((REPO / rel).read_text())
        pairs = list(_imports_of(tree))
        needs_gate = any(mod == REAL_STATE_DB for mod, _ in pairs)
        has_gate = any(mod == OWNER_MODULE for mod, _ in pairs)
        if needs_gate and not has_gate:
            offenders.append(rel)
    assert not offenders, (
        "these files build a real hermes_state.SessionDB without the shared "
        f"gate (they will FAIL, not skip, on a thin interpreter): "
        f"{offenders} — import the probe from {OWNER}")


# --------------------------------------------------------------------------- #
# the runner's roster block is the LIVE authority — pin it to the code
# --------------------------------------------------------------------------- #

def test_the_live_roster_in_the_runner_names_exactly_the_probes_that_exist():
    """``scripts/run_tests.sh``'s roster comment and the probe modules agree.

    The doc is the measurement of record; the runner's roster is what a reader
    of a red leg actually consults, so a probe that exists but is not listed is
    a capability nobody can find — and one listed but missing is a roster that
    lies. Both directions, one test: the audit's own roster went stale exactly
    this way (two families added, neither listed).
    """
    roster = (REPO / ROSTER).read_text()
    # ``require_*`` / ``_require_*`` names the roster block advertises as gates.
    advertised = set(re.findall(r"\b_?require_[a-z_]+\b", roster))
    owners = (OWNER, "hscc_daemon/tests/real_hermes.py",
              "hscc-project/tests/test_session_discovery.py",
              # This leg's guard does not police another leg's probe modules, so
              # an hscc-roles gate the roster advertises would otherwise read here
              # as "a gate no probe owner defines". Its own guard
              # (hscc-roles/tests/test_hermes_gap_single_posture.py) carries the
              # same list and pins ITS owner's exports back against the block.
              "hscc-roles/tests/hermes_gap.py")
    texts = {rel: (REPO / rel).read_text() for rel in owners}
    for rel in owners:
        assert (REPO / rel).exists(), f"probe owner {rel} is gone"

    unbacked = sorted(name for name in advertised
                      if not any(f"def {name}(" in text
                                 for text in texts.values()))
    assert not unbacked, (
        f"{ROSTER}'s roster names gate(s) that no probe owner defines: "
        f"{unbacked} — the roster is the live authority, so it has to be true")

    # ...and every capability the hscc-api owner exports is advertised there.
    owner_text = texts[OWNER]
    defined = set(re.findall(r"^def (require_[a-z_]+)\(", owner_text,
                             re.MULTILINE))
    assert defined, f"no capability probe defined in {OWNER}"
    missing = sorted(name for name in defined if name not in advertised)
    assert not missing, (
        f"{ROSTER}'s roster does not list this leg's probe(s) {missing} — a "
        "capability a reader cannot look up is how the audit's roster went "
        "stale in the first place")


# --------------------------------------------------------------------------- #
# the posture itself, executed rather than asserted from text
# --------------------------------------------------------------------------- #

def test_the_probes_are_capability_probes_on_both_sides():
    """The owner's posture, run in BOTH directions on every interpreter.

    Same shape as ``test_profile_root.test_f1_...`` (t_2cc3d482), for the
    capability this card folded in: where ``hermes_state`` imports, the gate
    must NOT skip — a gate that skipped everywhere would make the leg green by
    silencing it, and this is the test that turns that into a red build. Where
    it does not import, the skip must carry the import error itself, because
    that text is what the stamp rule reads.

    Deliberately NOT gated by the thing it tests: it has to run on both sides.
    """
    from hermes_gap import hermes_state_import_error, require_hermes_state

    error = hermes_state_import_error()
    if error is None:
        try:
            require_hermes_state()
        except pytest.skip.Exception as exc:
            pytest.fail(
                "the gate skipped although hermes_state imports on this "
                f"interpreter — every real-DB case would pass vacuously ({exc})")
    else:
        with pytest.raises(pytest.skip.Exception) as exc:
            require_hermes_state()
        assert error in str(exc.value), (
            "the skip must carry the import error itself, which is what the "
            f"runner's stamp rule reads, got: {exc.value}")
