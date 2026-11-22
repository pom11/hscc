"""The sparkrun-checkout seam on the ``hscc-cluster`` leg (t_2b2a6c3b).

WHAT THIS CARD MEASURED. With ``HOME`` + ``USERPROFILE`` pointed at a fresh empty
dir (the CI posture, and exactly what a roster-wide seal in ``scripts/run_tests.sh``
would do to every leg), ``hscc-cluster`` went ``1166 passed, 3 skipped -> 1164
passed, 5 skipped`` at ``47e7f0d0``: two cases turned into skips. Not failures —
*skips*, which is the worse shape, because the leg still prints green. The reason
string was ``no sparkrun checkout at <sealed-home>/sparkrun/src/sparkrun``: a path
that has never existed on any machine, readable as "the harness moved my HOME"
rather than the truth ("this machine has no sparkrun checkout"). The locator was

    SPARKRUN_SRC = Path(os.environ.get("SPARKRUN_SRC", str(Path.home() / ...)))

— read ONCE at import, with ``~`` as the default, so the seal silently retired two
cross-checks the workstation had been running. That is this repo's own class: "a
new skip that does not name the reason is a test that quietly stopped running".

WHAT THE SEAM GUARANTEES, and each guarantee below is a case:

  1. ``SPARKRUN_SRC`` is the ANSWER when set, not a hint — nothing else is
     consulted, so a declared checkout keeps the cases running no matter where HOME
     points, and its skip reason cannot contain a home path.
  2. Without a pin the candidates are (a) a checkout beside this repo — derived
     from ``__file``, so a seal cannot move it — then (b) the login-home default,
     the ONE candidate the seal blinds, and that fact is in the reason text.
  3. Resolution is per CALL. ``test_media_recipes`` is imported by this file at
     module scope, i.e. before any of these cases mutate the environment, so a
     re-frozen constant answers with the pre-mutation value and the pins below see
     the difference.
  4. The skip names the missing FILE (not just a directory), every path tried and
     why, and the env var that fixes it — so ``-rs``, which is all the runner's
     stamp rule reads, can distinguish "no checkout" from "harness artefact".

Deliberately NOT a fifth ``hermes_gap.py`` capability: that module's guard asserts
every capability probes a hermes-rooted module
(``test_the_probe_vocabulary_has_one_definition_per_fact``), and "is a sparkrun
checkout on this disk" is not one. See the block comment in
``test_media_recipes.py`` for the same reasoning at the point of use.
"""

import ast
import os
import sys
from pathlib import Path

import pytest

# Imported HERE (module scope, before any env mutation) on purpose: an
# import-time freeze of the locator would already have resolved by the time the
# pins below move HOME or set SPARKRUN_SRC, which is exactly the shape they must
# be able to catch. ``tests/conftest.py`` puts this dir on sys.path (conftest.py:9).
import test_media_recipes as tm

# One definition per fact, leg-wide (t_b884497b's doctrine): the guard module owns
# "a skip inside a watcher skips the watcher instead of failing it", and mutation
# P4 proved that hole was real here.
from test_hermes_gap_single_posture import _must_not_skip  # noqa: E402

LEG = Path(__file__).resolve().parent
SOURCE = LEG / "test_media_recipes.py"

#: The two source-backed cases, with the file each one reads.
SOURCE_CASES = (
    ("test_hf_cache_root_matches_sparkrun_runtime_base", "runtimes/base.py",
     'DEFAULT_CACHE_ROOT = "/cache/huggingface"\n'),
    ("test_fraction_key_is_known_so_defaults_stay_warning_free", "runtimes/sglang.py",
     'FLAG_MAP = {"gpu_memory_utilization": "--mem-fraction-static"}\n'),
)


def _fake_checkout(root, *, files):
    """A checkout-shaped ``src/sparkrun`` dir holding *files* (relpath -> text)."""
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _resolves(what, call):
    """Return ``call()``'s value, and FAIL (not skip) if it skipped.

    Same doctrine as ``test_hermes_gap_single_posture._must_not_skip``, for the
    calls that return a value instead of asserting: a locator mutated to always
    skip would otherwise skip THIS watcher and leave the leg green.
    """
    try:
        return call()
    except pytest.skip.Exception as exc:
        raise AssertionError(
            f"{what} skipped where the capability is present — a locator that "
            "answers 'missing' anyway silences every case behind it and nothing "
            "reports it") from exc


def _sealed_home(tmp_path, monkeypatch):
    """Move HOME/USERPROFILE to a fresh empty dir — the CI/seal posture, in place."""
    home = tmp_path / "sealed-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _case(name):
    return getattr(tm, name)


# --------------------------------------------------------------------------- #
# 1 — the pin carries the coverage, seal or no seal
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("case,rel,segment", SOURCE_CASES,
                         ids=[c for c, _r, _s in SOURCE_CASES])
def test_a_pinned_checkout_runs_the_case_even_with_home_sealed(tmp_path, monkeypatch,
                                                               case, rel, segment):
    """The seal must not retire a cross-check the machine can supply.

    HOME is moved to an empty dir (so the login-home default is blind) and
    ``SPARKRUN_SRC`` is pointed at a checkout holding the file. The case must RUN —
    which here means "does not skip": ``_must_not_skip`` treats a skip as a failure
    precisely so a locator mutated to always skip cannot turn this watcher yellow.
    """
    _sealed_home(tmp_path, monkeypatch)
    monkeypatch.delenv(tm.SPARKRUN_PIN, raising=False)
    _fake_checkout(tmp_path / "checkout", files={rel: segment})
    monkeypatch.setenv(tm.SPARKRUN_PIN, str(tmp_path / "checkout"))

    _must_not_skip(f"{case} with {tm.SPARKRUN_PIN} pointing at a checkout that has {rel}",
                   lambda: _case(case)())


@pytest.mark.parametrize("case,rel,segment", SOURCE_CASES,
                         ids=[c for c, _r, _s in SOURCE_CASES])
def test_a_pin_holding_a_checkout_without_the_file_skips_without_naming_home(
        tmp_path, monkeypatch, case, rel, segment):
    """A pin is the answer, so the reason is HOME-independent BY CONSTRUCTION.

    The checkout exists but lacks the file this case reads. The case must skip
    (there is nothing to assert against), and the reason must name the missing
    file, the path it probed and the pin — and must NOT contain the sealed home,
    which is the whole difference between "this machine has no sparkrun checkout"
    and the ``<sealed-home>/sparkrun/…`` string this card found in ``-rs``.
    """
    home = _sealed_home(tmp_path, monkeypatch)
    _fake_checkout(tmp_path / "checkout", files={})          # dir exists, no files
    monkeypatch.setenv(tm.SPARKRUN_PIN, str(tmp_path / "checkout"))

    with pytest.raises(pytest.skip.Exception) as caught:
        _case(case)()
    reason = str(caught.value)
    assert rel in reason, f"reason does not name the file it looked for: {reason}"
    assert str(tmp_path / "checkout") in reason, \
        f"reason does not name the path it probed: {reason}"
    assert f"{tm.SPARKRUN_PIN}=" in reason, (
        f"reason does not name the pin in the form an operator can paste "
        f"({tm.SPARKRUN_PIN}=<path>): {reason} — a bare mention of the name is not "
        "enough, and this pin was weak before it was measured: probe p3 (the "
        "instruction clause deleted) survived a version that only asked for the "
        "token, because the candidate label in the same reason already spells it")
    assert str(home) not in reason, (
        f"reason reaches for a home although the pin answered: {reason} — this is "
        "the string that read as 'the harness moved my HOME'")
    # A skip that is really an environment fact must not also be a green lie: the
    # reason has to say the check skips by right, so a reader knows what is missing.
    assert "checkout" in reason.lower(), reason


# --------------------------------------------------------------------------- #
# 2 — what the candidates are, and which one a seal can blind
# --------------------------------------------------------------------------- #

def test_only_the_documented_last_candidate_moves_with_home(monkeypatch, tmp_path):
    """Unpinned, the sibling candidate is ``__file__``-derived and cannot be sealed.

    The order is a contract: a sibling checkout (repo-relative) is tried before the
    login-home default, so the only thing a roster-wide seal can take away is the
    candidate the docstring calls the workstation default — and it is the last one
    tried, not the first.
    """
    home = _sealed_home(tmp_path, monkeypatch)
    monkeypatch.delenv(tm.SPARKRUN_PIN, raising=False)

    cands = tm._sparkrun_candidates()
    paths = [p for _why, p in cands]
    assert paths == [tm.REPO.parent / "sparkrun" / "src" / "sparkrun",
                     home / "sparkrun" / "src" / "sparkrun"], \
        f"candidate list/order changed: {paths}"
    assert str(home) not in str(paths[0]), (
        "the first candidate resolves through HOME — a seal would blind every "
        "unpinned machine's only chance of running the source cases")
    assert str(home) in str(paths[-1]), (
        "the last candidate is no longer the login-home default, so the block "
        "comment and the skip reason describe a locator that no longer exists")


def test_an_explicit_pin_short_circuits_the_candidates(monkeypatch, tmp_path):
    """With the pin set, ``~`` is not consulted at all — one candidate, the pin."""
    home = _sealed_home(tmp_path, monkeypatch)
    monkeypatch.setenv(tm.SPARKRUN_PIN, str(tmp_path / "declared"))

    assert tm._sparkrun_candidates() == [(f"{tm.SPARKRUN_PIN} pin",
                                          tmp_path / "declared")]
    assert str(home) not in "".join(str(p) for _w, p in tm._sparkrun_candidates())


def test_the_first_candidate_holding_the_file_wins(tmp_path, monkeypatch):
    """First match wins, and "no candidate has it" is the only reason to skip.

    Both mutants are cheap and both were invisible before this file: returning the
    LAST candidate (a second checkout would quietly answer a different question),
    and skipping on a candidate set where the file does exist.
    """
    a = _fake_checkout(tmp_path / "a", files={"runtimes/sglang.py": "x"})
    b = _fake_checkout(tmp_path / "b", files={"runtimes/base.py": "y"})
    monkeypatch.setattr(tm, "_sparkrun_candidates", lambda: [("a", a), ("b", b)])

    assert _resolves("runtimes/sglang.py, held by the FIRST candidate",
                     lambda: tm._sparkrun_src_or_skip("runtimes/sglang.py")) == a
    assert _resolves("runtimes/base.py, held by the SECOND candidate",
                     lambda: tm._sparkrun_src_or_skip("runtimes/base.py")) == b

    missing = tmp_path / "nope"
    monkeypatch.setattr(tm, "_sparkrun_candidates",
                        lambda: [("nothing here", missing)])
    with pytest.raises(pytest.skip.Exception) as caught:
        tm._sparkrun_src_or_skip("runtimes/base.py")
    assert str(missing) in str(caught.value), \
        f"every tried path must appear in the reason: {caught.value}"


# --------------------------------------------------------------------------- #
# 3 — the shape of the answer, not just the value
# --------------------------------------------------------------------------- #

def test_the_locator_freezes_nothing_at_module_import_time():
    """No module-level name in the leg's media suite reads the environment.

    The regression this pins is the one that shipped: ``SPARKRUN_SRC =
    Path(os.environ.get(...))`` at module scope. A constant like that is a
    collection-time answer about the filesystem — the same failure the leg's hermes
    probe module documents for imports (``hermes_gap.py``: "Every probe re-checks at
    CALL time and caches nothing at import"). AST, not text, so the prose describing
    the old shape in the block comment above can stay.
    """
    tree = ast.parse(SOURCE.read_text())
    frozen = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            for node in ast.walk(stmt.value):
                if isinstance(node, ast.Attribute) and node.attr == "environ":
                    frozen.append(f"{SOURCE.name}:{stmt.lineno}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("home", "expanduser"):
                    frozen.append(f"{SOURCE.name}:{stmt.lineno} (Path.home/expanduser)")
    assert not frozen, (
        f"module-level environment read(s) {frozen} in {SOURCE.name} — the locator "
        "must resolve per call, or a seal/fixture that moves HOME mid-run answers "
        "with a value captured before it moved")


def test_the_pin_names_are_the_documented_ones():
    """The seam's name is its documentation: the runner, CI and an operator all
    reach for ``SPARKRUN_SRC`` / ``SGLANG_SA_FIXTURE``, and the skip reason is the
    only place a reader learns them. Renaming one is allowed only in the same
    commit as the block comment that explains it.
    """
    assert tm.SPARKRUN_PIN == "SPARKRUN_SRC"
    assert tm.SA_FIXTURE_PIN == "SGLANG_SA_FIXTURE"
    text = SOURCE.read_text()
    for pin in (tm.SPARKRUN_PIN, tm.SA_FIXTURE_PIN):
        assert text.count(pin) >= 2, (
            f"{pin} is named once in the file and nowhere else — a pin nobody "
            "documents is a pin nobody finds")


def test_the_sa_fixture_seam_answers_at_call_time(tmp_path, monkeypatch):
    """The cached-parser fixture has the same seam shape (call-time, loud skip).

    Its two cases skip on every machine that has not cached the upstream file;
    that is honest today, and it stays honest only while the value is read per call
    and the reason names the variable.
    """
    monkeypatch.delenv(tm.SA_FIXTURE_PIN, raising=False)
    with pytest.raises(pytest.skip.Exception) as caught:
        tm._sa_fixture_or_skip()
    assert tm.SA_FIXTURE_PIN in str(caught.value)

    fixture = tmp_path / "server_args.py"
    fixture.write_text("raise SystemExit('error: unrecognized arguments: --x')\n")
    monkeypatch.setenv(tm.SA_FIXTURE_PIN, str(fixture))
    _must_not_skip(f"{tm.SA_FIXTURE_PIN} naming a real file",
                   lambda: tm._sa_fixture_or_skip())
    assert tm._sa_fixture_or_skip() == fixture


# --------------------------------------------------------------------------- #
# 4 — the leg's own guard still owns the hermes question
# --------------------------------------------------------------------------- #

#: There is deliberately NO second guard here for "who may define ``require_*``":
#: that rule belongs to ``test_hermes_gap_single_posture`` alone, and a second copy
#: of it in prose-matching form fires on its own docstring (measured on the first
#: draft of this file). The sparkrun locator is not in that vocabulary because it
#: is not a hermes capability — see the block comment in ``test_media_recipes.py``.


def test_the_seam_is_importable_from_a_copy_at_an_absolute_path():
    """Provenance gate for the mutation probes (skill ``mutation-probing``).

    The probes mutate a COPY of this leg; a copy that is not the module actually
    imported makes every "CAUGHT" below meaningless. This asserts the in-process
    answer here, and the probe driver re-runs the same check against its own copy.
    """
    assert os.path.isabs(tm.__file__)
    assert Path(tm.__file__).resolve().parent == LEG.resolve()
    assert "test_media_recipes" in sys.modules
