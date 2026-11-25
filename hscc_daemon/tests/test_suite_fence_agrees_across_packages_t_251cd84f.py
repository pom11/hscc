"""t_251cd84f — every in-suite predicate in the repo must answer what the package's
ruled predicate answers, and the roster of copies must not grow in silence.

``hscc_daemon/suite_fence`` is the daemon package's predicate (t_fdbc24f9), and that
card pinned it against the one other copy that existed then — bootstrap's
``doctor_rows.under_pytest``. Three more sites were found and ruled on this card:

* ``hscc_daemon/main_cli.py`` — its marker-only copy is GONE (the same package may not
  keep a second predicate), so it needs no copy pin here;
* ``hscc-api/repl_inject.py`` and ``hscc-project/flightdeck/core/tmux_chat.py`` —
  separate plugin payloads deployed standalone into ``~/.hermes/plugins``, so neither
  can import the daemon's predicate: each keeps its own stdlib-only copy, and those two
  copies are pinned here.

A copy is only tolerable while it is CHECKED (t_eb768c2a: "a copy without the marker is
a fence that only guards the process nobody is asking about"). So each copy is compared
against the EXPECTED answer for all four combinations of ``pytest in sys.modules`` × the
``PYTEST_CURRENT_TEST`` marker — not merely against ``suite_fence``, because two copies
that are both wrong agree happily — and the roster of copies itself is discovered from
the tree, so a fourth copy that nobody pinned is a failure rather than a drift waiting
to happen.

Loading each module by path (``importlib.util.spec_from_file_location``) is legitimate
here for the reason t_fdbc24f9 stated: the thing under test is the return value of a
function on that module, not a name another module must look up on it. The entry is
registered in ``sys.modules`` only for the length of the exec (3.14 resolves a
dataclass's defining module while building it) and removed again, so nothing leaks into
this leg's namespace (t_0c4989d7).
"""

import importlib.util
import os
import sys

from hscc_daemon import suite_fence

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The ruled original (t_fdbc24f9). Carries the marker literal too, as ``PYTEST_MARKER``.
_ORIGINAL = "hscc_daemon/suite_fence.py"

#: Every production COPY of the predicate, as measured by the discovery test below.
_PINNED = {
    "hscc-bootstrap/doctor_rows.py": "under_pytest",
    "hscc-api/repl_inject.py": "_under_pytest",
    "hscc-project/flightdeck/core/tmux_chat.py": "_under_pytest",
}

#: The four signal combinations and what each MEANS. A copy that drops either half
#: fails on the row that half exists for.
_CASES = (
    # (pytest in sys.modules, marker set) -> expected answer, meaning
    (False, False, False, "a plain operator run: the fence must not fire"),
    (False, True, True, "a child a suite item spawned (the marker crosses the spawn)"),
    (True, False, True, "the suite process itself, OUTSIDE an item (module import "
                        "during collection has no marker — measured, pytest 9.1.1)"),
    (True, True, True, "a child spawned by a suite that also imported pytest"),
)


def _discover_copies():
    """Production modules that carry the marker literal, excluding test trees.

    The quoted-literal rule is what separates a copy (``os.environ.get("PYTEST_...")``)
    from prose about the marker (``doctor_orch_subs.py`` documents it and delegates).
    """
    found = set()
    for dirpath, dirnames, filenames in os.walk(_REPO):
        # `build`/`dist` are not theoretical: a packaging case materialises
        # ``hscc-project/build/lib/flightdeck/core/tmux_chat.py`` mid-run (measured —
        # it appeared as a "fifth predicate" in a full-roster leg run). Those are
        # gitignored artefacts of a copy this pin already covers, not copies.
        dirnames[:] = [d for d in dirnames
                       if d not in ("tests", ".git", "__pycache__", "archive",
                                    "node_modules", "build", "dist", ".venv", "venv",
                                    "site-packages", ".tox", ".eggs", ".pytest_cache")]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    if '"' + suite_fence.PYTEST_MARKER + '"' in fh.read():
                        found.add(os.path.relpath(path, _REPO).replace(os.sep, "/"))
            except OSError:
                continue
    return found


def _load(rel_path, attr):
    """The sibling copy, by path, registered only for the length of the exec."""
    path = os.path.join(_REPO, rel_path)
    assert os.path.exists(path), (
        f"{rel_path} is gone — the predicate pinned here moved and the copies are "
        "now unpinned")
    name = rel_path.replace("/", "_").replace(".py", "") + "_pin"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    saved = sys.modules.get(name)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        return getattr(mod, attr)
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def _set_signals(monkeypatch, *, in_modules: bool, marker: bool) -> None:
    if in_modules:
        monkeypatch.setitem(sys.modules, "pytest",
                            sys.modules.get("pytest", object()))
    else:
        monkeypatch.delitem(sys.modules, "pytest", raising=False)
    if marker:
        monkeypatch.setenv(suite_fence.PYTEST_MARKER, "some.module::case (call)")
    else:
        monkeypatch.delenv(suite_fence.PYTEST_MARKER, raising=False)


def test_the_roster_of_copies_is_exactly_the_pinned_set():
    """A fourth copy must be pinned here, not discovered by an outage.

    The rule this enforces is per package, not per repo: one copy per package, each
    stdlib-only (t_fdbc24f9). A new production module that re-derives the predicate
    belongs in ``_PINNED`` with the ruling that made it a separate copy.
    """
    found = _discover_copies()
    assert found == set(_PINNED) | {_ORIGINAL}, (
        f"in-suite predicates in the tree: {sorted(found)}; pinned here: "
        f"{sorted(set(_PINNED) | {_ORIGINAL})}. Add the copy with its per-package "
        "ruling, or remove it.")


def test_every_copy_answers_the_expected_answer_for_every_signal_combination(
        monkeypatch):
    """Each copy against the ANSWER (not only against ``suite_fence``), all four rows.

    The marker-only row is the spawned child; the ``sys.modules``-only row is this
    process outside an item. Those two rows are the whole content of this card: the
    copies in ``repl_inject`` and ``tmux_chat`` passed the first and failed the second
    until t_251cd84f gave them both halves.
    """
    copies = {rel: _load(rel, attr) for rel, attr in sorted(_PINNED.items())}
    for in_modules, marker, expected, meaning in _CASES:
        _set_signals(monkeypatch, in_modules=in_modules, marker=marker)
        mine = suite_fence.under_pytest()
        assert mine == expected, (
            f"suite_fence.under_pytest() said {mine}, expected {expected} — {meaning}")
        for rel, fn in copies.items():
            got = fn()
            assert got == expected, (
                f"{rel} said {got}, expected {expected} — {meaning}")
            assert got == mine, (
                f"{rel} disagrees with suite_fence with pytest={in_modules} "
                f"marker={marker}: {got} != {mine} ({meaning})")


def test_every_copy_carries_a_spelling_that_cannot_silently_diverge(monkeypatch):
    """One marker name, four copies: a rename in one copy must not become a second
    channel, and no copy may swap the marker for a private env var.

    Checked behaviourally on the row only the marker can answer — a copy that renamed
    or dropped the marker answers False there while ``suite_fence`` answers True.
    """
    assert suite_fence.PYTEST_MARKER == "PYTEST_CURRENT_TEST"
    _set_signals(monkeypatch, in_modules=False, marker=True)
    for rel, attr in sorted(_PINNED.items()):
        assert _load(rel, attr)() is True, (
            f"{rel} no longer reads the {suite_fence.PYTEST_MARKER} marker — it is a "
            "fence that cannot see a suite-spawned child at all")


def test_the_two_tmux_modules_still_answer_the_same_question(monkeypatch):
    """``main_cli`` guards tmux with the daemon's predicate; ``repl_inject`` guards the
    same resource with its own. If they ever diverge, one refuses the operator's server
    while the other resolves a live socket — which is the divergence this card's own
    guard (``test_main_cli_tmux_fence_t_251cd84f.py``) cannot see from inside one leg.
    """
    repl = _load("hscc-api/repl_inject.py", _PINNED["hscc-api/repl_inject.py"])
    import hscc_daemon.main_cli as mc
    for in_modules, marker, expected, meaning in _CASES:
        _set_signals(monkeypatch, in_modules=in_modules, marker=marker)
        assert mc._under_pytest() == expected, (
            f"main_cli._under_pytest said {mc._under_pytest()}, expected {expected} — "
            f"{meaning}")
        assert repl() == expected, (
            f"repl_inject._under_pytest said {repl()}, expected {expected} — {meaning}")
