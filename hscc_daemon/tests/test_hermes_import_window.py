"""t_8eafef82 — ``hermes_import.agent_tree_on_path`` owns the sys.path window.

Three things are pinned here, in increasing cost:

1. the window itself: ``sys.path`` is exactly what it was after every exit,
   on the happy path, the exception path, nested windows, a path that was
   already there (we do not remove what we did not add), and a missing dir;
2. thread-safety of the refcount. The daemon runs ``autodown`` and the wedge
   loop on their own threads (``daemon_ops`` spawns one per check), so two
   windows on the same dir really do overlap in production. The test below
   forces the overlap with barriers: thread B's window closes while thread A is
   still inside, and A must still see the dir on the path. That assertion is not
   decoration — the mutation probe in the PR replaces the refcount with a plain
   insert/remove and this test goes red.
3. the reason the leak mattered for OTHER modules: ``dispatcher_wedge`` has bare
   ``from hermes_cli import …`` readers with no resolver of their own. Measured
   under the brew keg venv (``/opt/homebrew/bin/hscc``), ``hermes_cli`` is not
   importable there by any other route, so before this card those readers could
   only answer because some earlier check had leaked the agent tree onto
   ``sys.path`` — ``_review_dispatch_config_state()`` returned ``True`` after an
   ``autodown`` resolve and ``None`` before one. Each now opens its own window,
   and the tests below plant the tree behind a gate so a green run cannot come
   from the interpreter's own hermes install.

The gate is a deliberately local copy of ``test_kanban_live_caps._HermesCliGate``
(a ``sys.meta_path`` finder that serves ONLY the planted tree). This venv installs
hermes editable through a ``.pth`` finder that outranks ``sys.path``, so without a
gate every "it resolved from the planted tree" assertion below would be green for
the wrong reason.
"""

import os
import sys
import threading
from importlib.machinery import PathFinder

import pytest

from hscc_daemon import dispatcher_wedge as dw
from hscc_daemon import hermes_import as hi

PLANTED_CAPS = (17, 5)          # deliberately not this machine's real values


class _Gate:
    """Make the planted tree the only thing ``import hermes_cli`` can resolve."""

    def __init__(self, agent_dir=None, block=False, caps=PLANTED_CAPS):
        self.agent_dir = str(agent_dir) if agent_dir else None
        self.block = block
        if agent_dir is not None:
            cli = agent_dir / "hermes_cli"
            cli.mkdir(parents=True, exist_ok=True)
            (cli / "__init__.py").write_text("# planted\n")
            (cli / "config.py").write_text(
                "def load_config():\n"
                f"    return {{'kanban': {{'max_in_progress': {caps[0]}, "
                f"'max_in_progress_per_profile': {caps[1]}}}}}\n")
            (cli / "kanban_db_dispatch.py").write_text(
                "def review_dispatch_enabled():\n    return True\n")
        self._saved_modules = {}
        self._saved_path = list(sys.path)

    def __enter__(self):
        for name in [n for n in sys.modules if n == "hermes_cli"
                     or n.startswith("hermes_cli.")]:
            self._saved_modules[name] = sys.modules.pop(name)
        self._saved_path = list(sys.path)
        if self.agent_dir:
            sys.path[:] = [p for p in sys.path
                           if os.path.realpath(p) != os.path.realpath(self.agent_dir)]
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *a):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in [n for n in sys.modules if n == "hermes_cli"
                     or n.startswith("hermes_cli.")]:
            del sys.modules[name]
        sys.modules.update(self._saved_modules)
        sys.path[:] = self._saved_path
        return False

    def find_spec(self, name, path=None, target=None):
        if not (name == "hermes_cli" or name.startswith("hermes_cli.")):
            return None
        if self.block:
            raise ModuleNotFoundError(f"No module named {name!r} (hidden by gate)")
        if name == "hermes_cli":
            spec = PathFinder.find_spec(name, path=sys.path)
        else:
            parent = sys.modules.get(name.rsplit(".", 1)[0])
            spec = PathFinder.find_spec(
                name, path=list(getattr(parent, "__path__", []) or []))
        origin = str(getattr(spec, "origin", "") or "") if spec else ""
        root = os.path.realpath(self.agent_dir or "") + os.sep
        if origin.startswith(root):
            return spec
        raise ModuleNotFoundError(
            f"No module named {name!r} (hidden by gate: resolved to {origin!r})")


def _scrub(path):
    return [p for p in sys.path
            if os.path.realpath(p) != os.path.realpath(str(path))]


@pytest.fixture(autouse=True)
def _no_window_bookkeeping_leaked():
    """A window that forgot to close is a bug even if the test passed."""
    yield
    assert hi.open_windows() == {}, (
        f"windows still registered after the test: {hi.open_windows()}")


# ── 1. the window ──────────────────────────────────────────────────────────


def test_window_opens_for_the_block_and_closes_after(tmp_path):
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    sys.path[:] = _scrub(agent)
    before = list(sys.path)
    with hi.agent_tree_on_path(str(agent)):
        assert sys.path[0] == str(agent)          # inserted at the FRONT, as before
    assert sys.path == before


def test_window_closes_even_when_the_block_raises(tmp_path):
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    sys.path[:] = _scrub(agent)
    before = list(sys.path)
    with pytest.raises(RuntimeError):
        with hi.agent_tree_on_path(str(agent)):
            assert str(agent) in sys.path
            raise RuntimeError("the import blew up")
    assert sys.path == before


def test_nested_windows_leave_the_path_exactly_once(tmp_path):
    """Overlapping windows on one dir must not fight over the entry."""
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    sys.path[:] = _scrub(agent)
    before = list(sys.path)
    with hi.agent_tree_on_path(str(agent)):
        with hi.agent_tree_on_path(str(agent)):
            assert sys.path.count(str(agent)) == 1   # not inserted twice
            assert hi.open_windows()[str(agent)][0] == 2
        # inner closed, outer still open — the path must still be there
        assert str(agent) in sys.path
    assert sys.path == before


def test_window_does_not_remove_an_entry_it_did_not_add(tmp_path):
    """If the dir was already on the path by other means, leaving it is the
    caller's business — removing it here would be a second, worse leak."""
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    sys.path.insert(0, str(agent))
    try:
        before = list(sys.path)
        with hi.agent_tree_on_path(str(agent)):
            assert sys.path == before
        assert sys.path == before
    finally:
        sys.path[:] = _scrub(agent)


def test_missing_dir_opens_no_window_and_raises_like_any_import(tmp_path):
    before = list(sys.path)
    missing = tmp_path / "no-such-tree"
    with hi.agent_tree_on_path(str(missing)) as path:
        assert path == str(missing)
        assert str(missing) not in sys.path
        with pytest.raises(ModuleNotFoundError):
            hi.import_hermes("hermes_cli_definitely_not_here")
    assert sys.path == before
    assert hi.open_windows() == {}


# ── 2. the refcount, under a forced overlap ────────────────────────────────


def test_a_second_thread_closing_its_window_keeps_the_first_threads(tmp_path):
    """B closes while A is still inside ⇒ A must still see the dir on the path.

    This is the production shape (autodown's tick and the wedge loop overlap),
    and the shape an unrefcounted ``finally`` gets wrong: whichever window
    closes first would pull the entry out from under the other, and the loser's
    next import would raise ``ModuleNotFoundError`` — in the daemon that reads
    as "kanban interlock unevaluable" or "review dispatch unknown", i.e. a
    fail-open nobody can attribute to a race.
    """
    agent = tmp_path / "hermes-agent"
    agent.mkdir()
    path = str(agent)
    sys.path[:] = _scrub(agent)
    before = list(sys.path)
    entered = threading.Barrier(2, timeout=10)
    b_left = threading.Barrier(2, timeout=10)
    seen = {}

    def worker_a():
        with hi.agent_tree_on_path(path):
            entered.wait()
            seen["a_inside"] = path in sys.path
            b_left.wait()                      # B has now fully left its window
            seen["a_after_b_left"] = path in sys.path

    def worker_b():
        with hi.agent_tree_on_path(path):
            entered.wait()
        b_left.wait()

    threads = [threading.Thread(target=t) for t in (worker_a, worker_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads), "a worker hung on a barrier"
    assert seen == {"a_inside": True, "a_after_b_left": True}, seen
    assert sys.path == before
    assert hi.open_windows() == {}


def test_import_hermes_resolves_through_the_window_only(tmp_path, monkeypatch):
    """The one-liner form: reachable inside, invisible after (and the test does
    not pass because hermes happens to be installed here)."""
    agent = tmp_path / "hermes-agent"
    with _Gate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        sys.path[:] = _scrub(agent)
        before = list(sys.path)
        mod = hi.import_hermes("hermes_cli.config")
        assert mod.load_config() == {
            "kanban": {"max_in_progress": PLANTED_CAPS[0],
                       "max_in_progress_per_profile": PLANTED_CAPS[1]}}
        assert os.path.realpath(mod.__file__).startswith(
            os.path.realpath(str(agent)) + os.sep)
        assert str(agent) not in sys.path
        assert sys.path == before


# ── 3. the consumers that used to ride on the leak ─────────────────────────


def test_wedge_cap_readers_do_not_depend_on_call_order(tmp_path, monkeypatch):
    """``dispatcher_wedge`` reads its caps with no resolver of its own.

    Nothing in this test calls ``autodown._load_kanban_db_or_default`` first —
    that is the whole assertion. Pre-fix (and in the keg venv today) the bare
    import raised and both readers answered ``None`` = "no caps configured",
    which is the same silent lie GAP-D-4 was filed for.
    """
    agent = tmp_path / "hermes-agent"
    with _Gate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        sys.path[:] = _scrub(agent)
        before = list(sys.path)
        assert dw._read_max_in_progress() == PLANTED_CAPS[0]
        assert dw._read_max_per_profile() == PLANTED_CAPS[1]
        assert sys.path == before, "the reader leaked the tree like the old loader"


def test_wedge_review_dispatch_is_answerable_without_a_prior_resolve(
        tmp_path, monkeypatch):
    """The measured case: ``_review_dispatch_config_state()`` was ``None`` in a
    fresh process and ``True`` only after something leaked. Now always True here."""
    agent = tmp_path / "hermes-agent"
    with _Gate(agent):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        sys.path[:] = _scrub(agent)
        assert dw._review_dispatch_config_state() is True
        assert str(agent) not in sys.path


def test_negative_control_the_gate_really_hides_hermes_cli(tmp_path, monkeypatch):
    """If this stops raising, every assertion above is green for the wrong reason.

    The blocker is also what makes "None = genuinely unreachable" testable on a
    machine that has hermes installed: the readers must fall back, not crash, and
    must still leave no residue.
    """
    agent = tmp_path / "hermes-agent"
    with _Gate(agent, block=True):
        monkeypatch.setenv("HERMES_AGENT_PATH", str(agent))
        for name in [n for n in sys.modules if n == "hermes_cli"
                     or n.startswith("hermes_cli.")]:
            del sys.modules[name]
        with pytest.raises(ModuleNotFoundError):
            import hermes_cli.config  # noqa: F401
        before = list(sys.path)
        assert dw._read_max_in_progress() is None
        assert dw._read_max_per_profile() is None
        assert dw._review_dispatch_config_state() is None
        assert dw._assignee_is_nonspawnable("whoever") is False
        assert sys.path == before
