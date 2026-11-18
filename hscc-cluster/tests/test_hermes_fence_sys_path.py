"""The hermes import fence must not leave the runtime dir on ``sys.path`` (t_847ab03b).

THE BUG THIS FILE PINS. ``workflow._active_profile_name()`` used to retry the
``hermes_cli`` import by doing ``sys.path.insert(0, ~/.hermes/hermes-agent)`` and never
removing it. So the first lifecycle case in a process changed the interpreter's import
posture for every case that came after it — measured on ``~/miniconda3/envs/p313`` at
``1fd3c6cf``: ONE capability (``hermes_cli.kanban_db``) skipped with TWO different
import errors in the same run (9 cases "No module named 'hermes_cli'" before the fence,
4 cases "No module named 'ruamel'" after it). The fence now opens
``hscc_daemon.hermes_import.agent_tree_on_path``, which restores ``sys.path`` in a
``finally``; the cases below are what fails if that restore ever goes away.

WHY THE ASSERTION IS NOT VACUOUS. Every case here runs behind
``_BlockHermesOutsideTree``, a ``sys.meta_path`` finder that refuses ``hermes_cli*``
unless the planted agent tree is on ``sys.path``. So "the fake profile name came back"
and "the window was open" are the same fact — a fence that never opened the window could
only return ``None`` and would fail the first assertion, not quietly pass the second.
It also stops the operator's real ``~/.hermes/hermes-agent`` from resolving the lookup
behind the test's back on the hermes venv.

WHY NOTHING HERE SKIPS. These cases plant their own hermes and block the real one, so
they run on every interpreter — which is the point: a leak is invisible on an
interpreter where hermes happens to be importable anyway. That is also why none of them
consult ``hermes_gap`` (see ``test_hermes_gap_single_posture`` for why a file that does
touch real hermes must gate first).
"""

import contextlib
import importlib
import sys

import pytest

import workflow


#: What the planted tree's ``get_active_profile_name()`` answers. Distinct from every
#: real profile name so a green run cannot be the real hermes answering.
FAKE_PROFILE_NAME = "fence-probe-profile"

#: The module names the fence's import touches — everything that must be out of
#: ``sys.modules`` before the lookup, and back exactly as it was afterwards.
_FENCE_MODULES = ("hermes_cli", "hermes_cli.profiles")

_ABSENT = object()


class _BlockHermesOutsideTree:
    """``sys.meta_path`` finder: ``hermes_cli*`` resolves only while *tree* is on ``sys.path``.

    Deliberately NOT a capability probe — it does not answer "is hermes installed", it
    answers "is the window open", which is the only hermes question a no-leak assertion
    can ask without re-deriving the leg's posture (and the reason this file does not
    import ``hermes_gap``).
    """

    def __init__(self, tree):
        self.tree = tree

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "hermes_cli" and not fullname.startswith("hermes_cli."):
            return None
        if self.tree not in sys.path:
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (fence probe: agent tree not on sys.path)")
        return None


def _plant_agent_tree(root, body=None):
    """Write a minimal ``<root>/hermes_cli/profiles.py`` and return the tree path.

    ``body`` replaces the module body, so a case can plant one that raises at import
    time (the exception path of the window) instead of one that answers.
    """
    pkg = root / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "profiles.py").write_text(
        body if body is not None
        else f"def get_active_profile_name():\n    return {FAKE_PROFILE_NAME!r}\n")
    return str(root)


@contextlib.contextmanager
def _blocked_hermes(monkeypatch, tree):
    """Block ``hermes_cli*`` behind *tree*, and leave ``sys.modules`` as it was.

    The eviction is explicit rather than ``monkeypatch.delitem``: those keys usually do
    NOT exist before the test, and ``delitem`` restores only what it deleted — so a
    monkeypatch-only teardown would hand the next case in the process a fake
    ``hermes_cli`` whose tmp dir is still on disk, the exact cross-file poison
    ``hscc_daemon/tests`` had to fix (suite-order-dependence, item 4).
    """
    saved = {name: sys.modules.pop(name, _ABSENT) for name in _FENCE_MODULES}
    monkeypatch.setattr(sys, "meta_path",
                        [_BlockHermesOutsideTree(tree)] + list(sys.meta_path))
    importlib.invalidate_caches()
    try:
        yield tree
    finally:
        for name, value in saved.items():
            if value is _ABSENT:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    """A planted agent tree, hermes blocked outside it, ``sys.modules`` clean after."""
    tree = _plant_agent_tree(tmp_path / "agent-tree")
    with _blocked_hermes(monkeypatch, tree):
        monkeypatch.setenv("HERMES_AGENT_PATH", tree)
        yield tree


# --------------------------------------------------------------------------- #
# the DoD case: a case that runs the fence leaves sys.path byte-identical
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("window_owner", ["peer", "local"])
def test_a_case_that_runs_the_fence_leaves_sys_path_byte_identical(
        agent_tree, monkeypatch, window_owner):
    """``sys.path`` after the fence == ``sys.path`` before it, entry for entry.

    ``peer`` is the sanctioned path (``hermes_import.agent_tree_on_path``); ``local``
    is the fallback this module owns for an install without ``hscc_daemon``. Both must
    restore, or the fallback is the leak wearing a different costume.
    """
    consulted = []
    if window_owner == "local":
        def _no_peer():
            consulted.append("attempted")
            raise ImportError("no hscc_daemon in this install")

        monkeypatch.setattr(workflow, "_hermes_import_module", _no_peer)

    before = list(sys.path)
    assert agent_tree not in before, "the planted tree must start off the path"

    assert workflow._active_profile_name() == FAKE_PROFILE_NAME, (
        "the fake profile name is the ONLY evidence the window was open; if this is "
        "None the fence never opened and the byte-identity assertion below is vacuous")
    assert list(sys.path) == before, (
        f"_active_profile_name() mutated sys.path: added "
        f"{[p for p in sys.path if p not in before]!r}, removed "
        f"{[p for p in before if p not in sys.path]!r} — the fence must restore it in "
        f"a finally (t_847ab03b)")

    if window_owner == "local":
        assert consulted == ["attempted"], (
            "the fallback was never attempted, so this run proved nothing about it")


def test_the_fence_prefers_the_sanctioned_peer_window(agent_tree, monkeypatch):
    """The window comes from ``hscc_daemon.hermes_import``, not from a hand-rolled insert.

    The migration is the card; a future "just insert it here" edit would keep these
    cases green on the success path and re-open the leak, so the *consultation* is
    pinned, not the outcome (whether the peer import succeeds depends on the
    interpreter, and the fallback handles that either way).
    """
    consulted = []
    real = workflow._hermes_import_module

    def _spy():
        consulted.append("attempted")
        return real()

    monkeypatch.setattr(workflow, "_hermes_import_module", _spy)

    assert workflow._active_profile_name() == FAKE_PROFILE_NAME
    assert consulted == ["attempted"], (
        "_active_profile_name() resolved hermes without ever reaching "
        "hscc_daemon.hermes_import — the sanctioned context manager is the whole "
        "point of t_847ab03b")


# --------------------------------------------------------------------------- #
# the window really is the only thing that makes hermes importable, and it is
# released on every exit path
# --------------------------------------------------------------------------- #

def test_hermes_resolves_only_inside_the_window(agent_tree):
    """Control for the assertions above: outside the window the import CANNOT work.

    Without this, a ``_BlockHermesOutsideTree`` that simply never blocked would make
    every byte-identity assertion above pass while testing nothing.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hermes_cli.profiles")

    before = list(sys.path)
    with workflow._agent_tree_window(agent_tree):
        assert agent_tree in sys.path, "the window must put the tree on the path"
        mod = importlib.import_module("hermes_cli.profiles")
        assert mod.get_active_profile_name() == FAKE_PROFILE_NAME
    assert list(sys.path) == before, "the window did not restore sys.path"


def test_the_window_releases_the_path_when_the_import_raises(tmp_path, monkeypatch):
    """The restore is a ``finally``, not a success-path cleanup.

    The planted tree exists (so the window opens it) but its ``hermes_cli`` raises at
    import time — the thin-sibling shape (``hermes_cli`` found, ``ruamel`` missing). The
    lookup must still return None AND must not leave the tree behind.
    """
    tree = _plant_agent_tree(
        tmp_path / "broken-tree",
        body="raise ImportError(\"No module named 'ruamel'\")\n")
    with _blocked_hermes(monkeypatch, tree):
        monkeypatch.setenv("HERMES_AGENT_PATH", tree)
        before = list(sys.path)
        assert workflow._active_profile_name() is None, (
            "a hermes whose import raises must fall back to None, not reach the hook")
        assert list(sys.path) == before, (
            "the tree survived an import that raised — the restore must be in a "
            "finally, not after the import")


def test_a_missing_agent_tree_returns_none_and_adds_nothing(tmp_path, monkeypatch):
    """``None`` on failure stays observable, and a non-existent dir never reaches the path.

    The old fence guarded the insert with ``os.path.isdir``; a window that inserted
    unconditionally would add a phantom entry here and survive as one. Real hermes is
    blocked behind the missing dir, so ``None`` here means the lookup really failed
    rather than "this interpreter has no hermes anyway".
    """
    missing = str(tmp_path / "no-such-hermes-tree")
    with _blocked_hermes(monkeypatch, missing):
        monkeypatch.setenv("HERMES_AGENT_PATH", missing)
        before = list(sys.path)
        assert workflow._active_profile_name() is None
        assert list(sys.path) == before
        assert missing not in sys.path


@pytest.mark.parametrize("window_owner", ["peer", "local"])
def test_a_preexisting_agent_tree_entry_is_not_taken_back(
        tmp_path, monkeypatch, window_owner):
    """A path somebody else put on ``sys.path`` is not the fence's to remove.

    ``finally: sys.path.remove(path)`` — the naive fix — passes every byte-identity case
    above (there the fence added the entry) and breaks THIS one, which is why both
    directions are pinned (same pair the leg keeps for ``fleet._daemon_mod`` in
    ``test_fleet.py``, and for the window itself in
    ``hscc_daemon/tests/test_hermes_import_window.py``).

    Reaching it takes the awkward shape: the window only opens when the import failed,
    so the dir must already be on the path AND hold no importable ``hermes_cli`` —
    otherwise the first attempt succeeds and no window is ever opened to remove
    anything. A fence that then takes the entry back is removing a path it never added.
    """
    empty = tmp_path / "empty-agent-tree"
    empty.mkdir()
    consulted = []
    if window_owner == "local":
        def _no_peer():
            consulted.append("attempted")
            raise ImportError("no hscc_daemon in this install")

        monkeypatch.setattr(workflow, "_hermes_import_module", _no_peer)

    with _blocked_hermes(monkeypatch, str(tmp_path / "never-on-the-path")):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(empty))
        original = list(sys.path)
        before = [str(empty)] + original
        sys.path[:] = before
        try:
            assert workflow._active_profile_name() is None
            assert str(empty) in sys.path, (
                "the fence removed a sys.path entry it did not add — its finally must "
                "take back only what it inserted itself")
            assert list(sys.path) == before
        finally:
            sys.path[:] = original
    if window_owner == "local":
        assert consulted == ["attempted"], (
            "the fallback was never attempted, so this run proved nothing about it")
