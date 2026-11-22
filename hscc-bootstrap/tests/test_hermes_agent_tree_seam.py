"""The real-hermes-tree seam on the ``hscc-bootstrap`` leg (t_5b560adf).

WHAT WAS MEASURED. With ``HOME`` + ``USERPROFILE`` pointed at a fresh empty dir —
the CI posture, and exactly what the roster-wide seal in ``scripts/run_tests.sh``
(PR #606) does to every leg — this leg went ``1329 passed`` -> ``1294 passed, 35
skipped`` at ``96a9f38a``. Thirty-five cases retired as *skips*, which is the worse
shape: the leg still prints green. And that subset took **230.35 s unsealed against
2.74 s sealed** — the seal bought an ~80x faster leg by deleting the leg's most
expensive coverage, and a future reader sees the speed and concludes nothing was
lost. The locator was

    REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    needs_runtime = pytest.mark.skipif(not REAL_VENV_PY.is_file(), reason="…")

one candidate, derived from ``~``, frozen at import, with no way for a machine to
declare the checkout it does have. That is the same fault #604 closed for the
sparkrun checkout on ``hscc-cluster``; this file pins the same guarantees here.

GUARANTEES, one case each:

  1. ``HERMES_AGENT_PATH`` is the ANSWER when set, not a hint — nothing else is
     consulted, so the 35 cases keep running under a seal on a machine that declares
     its tree, and a pinned skip reason cannot name a home at all.
  2. Unpinned, the candidates are (a) a checkout beside this repo — ``__file__``-
     derived, so a seal cannot move it — then (b) the login-home default, the ONE
     candidate a seal blinds, tried last.
  3. Resolution is per CALL for the paths the cases use. The five consumer modules
     are imported by this file at module scope, i.e. before any case below mutates
     the environment, so a re-frozen constant answers with the pre-mutation value
     and the pins here see the difference.
  4. A skip names the capability that went untested (the module's own words), every
     path tried and why, and the pin in the pasteable ``HERMES_AGENT_PATH=<path>``
     form — so ``-rs``, which is all the runner's stamp rule reads, distinguishes
     "this machine has no hermes checkout" from "the harness moved my HOME".
  5. The collection-time gate and the call-time resolvers state the SAME capability,
     so a module cannot quietly swap in a blanket skip.

``pytest.skip`` inside these helpers is the point, so a watcher that merely called
them and ignored the outcome would prove nothing: ``_must_not_skip`` turns an
unexpected skip into a FAILURE, exactly as ``hscc-cluster``'s
``test_hermes_gap_single_posture._must_not_skip`` does for its leg.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

import hermes_tree

# Imported HERE (module scope, before any env mutation) on purpose: an import-time
# freeze of the locator would already have resolved by the time the cases below move
# HOME or set the pin, which is exactly the shape they must be able to catch.
# ``tests/conftest.py`` puts this dir on sys.path, and pytest prepends it for the
# test modules themselves (same import shape as ``_video_plugin_harness``).
import test_bootstrap_dry_run as dry_run            # noqa: E402
import test_bootstrap_isolated as isolated          # noqa: E402
import test_bootstrap_main_orch_e2e as main_orch    # noqa: E402
import test_bootstrap_node_recovery_stage as node_recovery  # noqa: E402
import test_skill_resolver_xval as xval             # noqa: E402

LEG = Path(__file__).resolve().parent
SEAM = LEG / "hermes_tree.py"

#: The five modules whose cases the seam carries, with the marker each one gates on.
CONSUMERS = (
    (dry_run, "needs_runtime"),
    (isolated, "needs_runtime"),
    (main_orch, "needs_runtime"),
    (node_recovery, "needs_runtime"),
    (xval, "needs_hermes"),
)

#: A checkout-shaped plant: the dir plus the interpreter the leg spawns.
PLANT_INTERPRETER = Path("venv") / "bin" / "python"


def _plant_checkout(root):
    """A hermes-agent checkout stand-in: the dir + ``venv/bin/python``."""
    py = root / PLANT_INTERPRETER
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    py.chmod(0o755)
    return root


def _sealed_home(tmp_path, monkeypatch):
    """Move HOME/USERPROFILE to a fresh empty dir — the seal posture, in place."""
    home = tmp_path / "sealed-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _must_not_skip(what, call):
    """Return ``call()``'s value, and FAIL (not skip) if it skipped.

    A locator mutated to always skip would otherwise skip THIS watcher and leave the
    leg green, which is the exact failure mode this card exists to close.
    """
    try:
        return call()
    except pytest.skip.Exception as exc:
        raise AssertionError(
            f"{what} skipped where the capability is present — a locator that "
            "answers 'missing' anyway silences every case behind it and nothing "
            "reports it") from exc


# --------------------------------------------------------------------------- #
# 1 — the pin carries the coverage, seal or no seal
# --------------------------------------------------------------------------- #

def test_a_pinned_checkout_resolves_with_home_sealed(tmp_path, monkeypatch):
    """The seal must not retire coverage the machine can supply.

    HOME is moved to an empty dir (so the login-home default is blind) and the pin
    names a checkout holding the interpreter. The resolver must ANSWER.
    """
    _sealed_home(tmp_path, monkeypatch)
    monkeypatch.delenv(hermes_tree.AGENT_PIN, raising=False)
    plant = _plant_checkout(tmp_path / "agent")

    monkeypatch.setenv(hermes_tree.AGENT_PIN, str(plant))
    agent_dir, venv_python = _must_not_skip(
        f"{hermes_tree.AGENT_PIN} pointing at a checkout holding {PLANT_INTERPRETER}",
        lambda: hermes_tree.runtime_or_skip("seam watcher"))
    assert (agent_dir, venv_python) == (plant, plant / PLANT_INTERPRETER)
    # Both halves come from ONE candidate: a case that symlinked one checkout's
    # venv and spawned another's interpreter would test a tree that does not exist.
    assert venv_python.is_relative_to(agent_dir)


def test_a_pin_holding_a_non_checkout_skips_naming_the_pin_not_home(
        tmp_path, monkeypatch):
    """A pin is the answer, so the reason is HOME-independent BY CONSTRUCTION."""
    home = _sealed_home(tmp_path, monkeypatch)
    declared = tmp_path / "declared"          # exists, but is no checkout
    declared.mkdir()
    monkeypatch.setenv(hermes_tree.AGENT_PIN, str(declared))

    capability = "some_case — consulting the live resolver"
    with pytest.raises(pytest.skip.Exception) as caught:
        hermes_tree.runtime_or_skip(capability)
    reason = str(caught.value)
    assert capability in reason, (
        f"reason does not name what went untested: {reason} — 'N skipped' with no "
        "capability is how a leg quietly stops testing")
    assert str(declared) in reason, f"reason does not name the path it probed: {reason}"
    assert f"{hermes_tree.AGENT_PIN}=" in reason, (
        f"reason does not name the pin in the form an operator can paste "
        f"({hermes_tree.AGENT_PIN}=<path>): {reason} — #604's probe p3 survived a "
        "reason that only mentioned the name in prose, because the candidate "
        "labels in the same string already spell it")
    assert str(home) not in reason, (
        f"reason reaches for a home although the pin answered: {reason} — this is "
        "the string that reads as 'the harness moved my HOME'")
    assert "hermes-agent checkout" in reason.lower(), reason


# --------------------------------------------------------------------------- #
# 2 — what the candidates are, and which one a seal can blind
# --------------------------------------------------------------------------- #

def test_an_explicit_pin_short_circuits_the_candidates(monkeypatch, tmp_path):
    """With the pin set, ``~`` is not consulted at all — one candidate, the pin."""
    home = _sealed_home(tmp_path, monkeypatch)
    declared = tmp_path / "declared"
    monkeypatch.setenv(hermes_tree.AGENT_PIN, str(declared))

    assert hermes_tree.agent_candidates() == [
        (f"{hermes_tree.AGENT_PIN} pin", declared)]
    assert str(home) not in "".join(str(p) for _w, p in hermes_tree.agent_candidates())


def test_only_the_documented_last_candidate_moves_with_home(monkeypatch, tmp_path):
    """Unpinned, the sibling candidate is ``__file__``-derived and cannot be sealed.

    The order is a contract: a repo-relative checkout is tried before the login-home
    default, so the only thing a roster-wide seal can take away is the candidate the
    seam calls the workstation default — and it is the last one tried, not the first.
    """
    home = _sealed_home(tmp_path, monkeypatch)
    monkeypatch.delenv(hermes_tree.AGENT_PIN, raising=False)

    paths = [p for _why, p in hermes_tree.agent_candidates()]
    assert paths == [hermes_tree.REPO.parent / "hermes-agent",
                     home / ".hermes" / "hermes-agent"], \
        f"candidate list/order changed: {paths}"
    assert str(home) not in str(paths[0]), (
        "the first candidate resolves through HOME — a seal would blind every "
        "unpinned machine's only chance of running the installer cases")
    assert str(home) in str(paths[-1]), (
        "the last candidate is no longer the login-home default, so the seam's "
        "docstring and the skip reason describe a locator that no longer exists")


def test_the_first_candidate_that_is_a_checkout_wins(tmp_path, monkeypatch):
    """First match wins, and 'no candidate is a checkout' is the only reason to skip."""
    a = _plant_checkout(tmp_path / "a")
    b = _plant_checkout(tmp_path / "b")
    monkeypatch.setattr(hermes_tree, "agent_candidates", lambda: [("a", a), ("b", b)])
    assert _must_not_skip("the FIRST usable checkout",
                          lambda: hermes_tree.agent_dir_or_skip("seam watcher")) == a

    empty = tmp_path / "empty"                    # a dir named hermes-agent, no venv
    empty.mkdir()
    monkeypatch.setattr(hermes_tree, "agent_candidates",
                        lambda: [("no venv here", empty)])
    with pytest.raises(pytest.skip.Exception) as caught:
        hermes_tree.venv_python_or_skip("seam watcher")
    assert str(empty) in str(caught.value), \
        f"every tried path must appear in the reason: {caught.value}"


def test_a_directory_is_not_a_checkout(tmp_path, monkeypatch):
    """The gate is the INTERPRETER, not the directory name.

    A stale ``~/.hermes/hermes-agent`` left by a removed install (or a file where a
    dir should be) must not count as coverage: every case behind such a gate would
    spawn a path that does not exist. The last two asserts are the same claim at the
    resolver level, on a machine whose only candidates are such dirs — HOME sealed
    away the workstation default and ``REPO`` moved the sibling to an empty tmp dir.
    """
    monkeypatch.delenv(hermes_tree.AGENT_PIN, raising=False)
    bare = tmp_path / "hermes-agent"
    bare.mkdir()
    assert hermes_tree.is_checkout(bare) is False
    as_file = tmp_path / "also"
    as_file.write_text("not a dir\n", encoding="utf-8")
    assert hermes_tree.is_checkout(as_file) is False

    _sealed_home(tmp_path, monkeypatch)
    monkeypatch.setattr(hermes_tree, "REPO", tmp_path / "repo")
    assert hermes_tree.resolve_runtime() is None
    assert hermes_tree.runtime_available() is False


# --------------------------------------------------------------------------- #
# 3 — the shape of the answer, not just the value
# --------------------------------------------------------------------------- #

def test_the_locator_freezes_nothing_at_module_import_time():
    """No module-level name reads the environment or ``~`` — AST, not text.

    The regression this pins is the one that shipped: ``REAL_VENV_PY =
    Path.home() / ...`` at module scope, a collection-time answer about the
    filesystem. Text matching would reddens the prose above and the quoted old
    shape in the seam's docstring, so the sweep walks the tree instead. It covers
    the seam module AND every consumer, because a consumer that re-froze its own
    constant would silently outrank the seam again.
    """
    offenders = []
    for source in [SEAM] + [Path(module.__file__) for module, _marker in CONSUMERS]:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for stmt in tree.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or stmt.value is None:
                continue
            for node in ast.walk(stmt.value):
                if isinstance(node, ast.Attribute) and node.attr == "environ":
                    offenders.append(f"{source.name}:{stmt.lineno} (os.environ)")
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("home", "expanduser")):
                    offenders.append(
                        f"{source.name}:{stmt.lineno} (Path.home/expanduser)")
    assert not offenders, (
        f"module-level environment read(s) {offenders} — the locator must resolve "
        "per call, or a seal/fixture that moves HOME mid-run answers with a value "
        "captured before it moved")


def test_the_pin_name_is_the_documented_one():
    """The seam's name is its documentation, and it is production's name too.

    ``hscc_daemon/hermes_import.py`` / ``autodown.py`` / ``kanban_live.py`` and
    ``hscc-roles/hscc.py`` all honour ``HERMES_AGENT_PATH``, and that is the variable
    the runner declares for the machine's tree before it seals the roster (PR #606).
    A rename on either side has to be a deliberate edit here, in the commit that
    explains it — a leg reading a name nothing sets is how coverage dies twice.
    """
    assert hermes_tree.AGENT_PIN == "HERMES_AGENT_PATH"
    root = hermes_tree.REPO
    production = root / "hscc_daemon" / "hermes_import.py"
    text = production.read_text(encoding="utf-8")
    assert f'os.environ.get("{hermes_tree.AGENT_PIN}"' in text, (
        f"{production.name} no longer honours {hermes_tree.AGENT_PIN} — the seam "
        "would be reading a variable production ignores")
    seam_text = SEAM.read_text(encoding="utf-8")
    assert seam_text.count(hermes_tree.AGENT_PIN) >= 2, (
        f"{hermes_tree.AGENT_PIN} is named once in the leg and nowhere else — a pin "
        "nobody documents is a pin nobody finds")


def test_a_bogus_hermes_home_cannot_answer_for_the_agent_tree(tmp_path, monkeypatch):
    """``HERMES_HOME`` points at a PROFILE dir on a dispatched worker, not a checkout.

    t_8579fba4 / t_417610e6 removed exactly this join from ``apply_patches.py`` and
    ``ensure_review_feature.py``; the seam must not put it back. A profile dir that
    *looks* like a usable checkout, armed beside a machine with no pin, has to be
    refused — answering from it would test a tree that cannot exist.
    """
    profile = tmp_path / "profiles" / "worker"
    (profile / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    (profile / "hermes-agent" / "venv" / "bin" / "python").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.delenv(hermes_tree.AGENT_PIN, raising=False)
    monkeypatch.setattr(hermes_tree, "REPO", tmp_path / "repo")   # no sibling checkout
    _sealed_home(tmp_path, monkeypatch)

    assert hermes_tree.resolve_runtime() is None, (
        "the seam resolved through HERMES_HOME — on a dispatched worker that is a "
        "profile dir, and the join is the bug t_8579fba4 removed in two modules")


def test_the_gate_and_the_resolver_state_one_capability():
    """No module may invent its own skip text: the marker's reason IS the seam's.

    A blanket ``pytest.skip("needs the machine")`` behind a marker is how a leg
    retires coverage without naming it. Each consumer builds its marker reason from
    ``hermes_tree.runtime_skip_reason(its CAPABILITY)``, so pinning the equality
    pins that the reason names the module and the capability — and that the
    collection-time gate and the call-time resolver ask the same question.
    """
    for module, marker_name in CONSUMERS:
        marker = None
        for obj in vars(module).values():
            marks = getattr(obj, "pytestmark", None) or []
            for mark in marks:
                if mark.name == "skipif" and mark.args and isinstance(mark.args[0], bool):
                    marker = mark
                    break
            if marker:
                break
        assert marker is not None, (
            f"{module.__name__}: no skipif marker built from a boolean condition — "
            "the gate that retires these cases has to be the seam's")
        reason = marker.kwargs["reason"]
        assert reason == hermes_tree.runtime_skip_reason(module.CAPABILITY), (
            f"{module.__name__} carries its own skip text — a reason the seam does "
            "not generate is a reason that can go stale about what it checked")
        assert Path(module.__file__).stem in module.CAPABILITY, (
            f"{module.__name__}.CAPABILITY does not name its own module: "
            f"{module.CAPABILITY!r} — a reader of -rs has to know which file went "
            "untested")


def test_the_seam_is_importable_from_a_copy_at_an_absolute_path():
    """Provenance gate for the mutation probes (skill ``mutation-probing``).

    The probes mutate a COPY of this leg; a copy that is not the module actually
    imported makes every "CAUGHT" meaningless. This asserts the in-process answer,
    and the probe driver re-runs the same check against its own copy.
    """
    assert os.path.isabs(hermes_tree.__file__)
    assert Path(hermes_tree.__file__).resolve().parent == LEG.resolve()
    assert "hermes_tree" in sys.modules
