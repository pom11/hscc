"""One owner for the "put the Hermes agent tree on ``sys.path`` for one import"
window (t_8eafef82).

Why this module exists
----------------------
``hermes_cli`` (and the top-level ``hermes_constants``) live under
``~/.hermes/hermes-agent`` and are NOT on the interpreter's path in the
interpreter that ships HSCC — measured on this box: the brew keg venv that
``/opt/homebrew/bin/hscc`` runs in raises ``ModuleNotFoundError: No module named
'hermes_cli'`` for every one of those names, while the hermes venv the launchd
daemon runs in resolves them through its own install. So every consumer reaches
them by inserting the resolved agent dir at ``sys.path[0]``, importing, and
— this is the part that kept going wrong — putting the path back.

Three modules each carried their own copy of that insert
(``autodown._load_kanban_db_or_default``, ``kanban_live._load_hermes_config``,
``profile_roster._hermes_gate``) and two of them documented "only for the
duration of this one import" while leaving the entry in place for the life of
the process. The leak is what turned a planted fake ``hermes_cli`` in the test
suite into process-wide poison — see
``docs/audits/orch-waker-import-leak_t_2be3b04e.md`` §3 and §6. The fix for the
product side is here: the window is opened and closed in one place, so the
docstring and the code cannot drift apart in five.

The contract
------------
``agent_tree_on_path()`` puts the resolved agent dir on ``sys.path`` for the
duration of the ``with`` block and restores ``sys.path`` afterwards — including
on the exception path. ``import_hermes(name)`` is the one-line form of the same
thing.

Keep everything Hermes-side that can *trigger an import* inside the window:
once ``hermes_cli`` is in ``sys.modules`` its submodules resolve through the
parent's ``__path__`` (no ``sys.path`` needed), but a *fresh top-level* import
made at call time — ``import hermes_constants`` from inside a hermes function,
or ``hermes_cli.<submodule>`` nobody has imported yet — needs the window open.
That is why the call sites that read a config value wrap the call too, not just
the import.

Concurrency: the daemon runs several of these consumers on their own threads
(``daemon_ops`` spawns a loop per check), so the window is refcounted per path
under a lock. Without that, two overlapping windows on the same dir would let
the first one to finish pull the path out from under the other and the other's
import would raise ``ModuleNotFoundError`` — a flake we would never be able to
attribute.
"""

import contextlib
import importlib
import os
import sys
import threading

#: Default location of the Hermes runtime tree. Same default as
#: ``autodown._HERMES_AGENT_PATH`` / ``kanban_live._HERMES_AGENT_PATH``; the
#: ``HERMES_AGENT_PATH`` env var overrides it and ``HOME`` is honoured through
#: ``expanduser``, exactly as those two did.
DEFAULT_AGENT_PATH = "~/.hermes/hermes-agent"

_LOCK = threading.Lock()
#: path -> ``[windows_held, added_by_us]``. The path goes back out of
#: ``sys.path`` when the last window closes, and only if this module is the
#: one that put it there.
_WINDOWS = {}


def resolve_agent_tree():
    """The Hermes runtime dir this process should import ``hermes_cli`` from.

    Honours ``HERMES_AGENT_PATH`` then :data:`DEFAULT_AGENT_PATH`, expanded
    against ``HOME``. Does NOT check that it exists — callers decide what a
    missing tree means (``autodown`` records it as an unevaluable interlock,
    :func:`agent_tree_on_path` simply opens no window).
    """
    return os.path.expanduser(os.environ.get("HERMES_AGENT_PATH",
                                             DEFAULT_AGENT_PATH))


@contextlib.contextmanager
def agent_tree_on_path(path=None):
    """Hold the resolved agent dir on ``sys.path`` for this block only.

    ``path`` overrides the resolution (callers that keep their own
    ``_HERMES_AGENT_PATH`` module constant — and their own tests that monkeypatch
    it — pass it explicitly; ``None`` means "resolve as usual").

    Yields the path (which may not exist; the window is simply not
    opened then, and the import inside the block fails the way it would
    anyway). ``sys.path`` is restored to exactly what it was — same entries,
    same order — on every exit path, including an exception raised inside the
    block.
    """
    if path is None:
        path = resolve_agent_tree()
    with _LOCK:
        state = _WINDOWS.get(path)
        if state is None:
            added = os.path.isdir(path) and path not in sys.path
            if added:
                sys.path.insert(0, path)
            state = _WINDOWS[path] = [1, added]
        else:
            state[0] += 1
    try:
        yield path
    finally:
        with _LOCK:
            state = _WINDOWS.get(path)
            if state is None:
                pass  # bookkeeping was reset under us (a test replaced sys.path)
            elif state[0] > 1:
                state[0] -= 1
            else:
                _WINDOWS.pop(path, None)
                # Only take back what this window family added: if the entry is
                # not there (a test replaced sys.path) or was put there by
                # somebody else, removing it would be a second leak.
                if state[1]:
                    try:
                        sys.path.remove(path)
                    except ValueError:
                        pass


def import_hermes(name, path=None):
    """``importlib.import_module(name)`` with the agent tree on the path.

    The window covers the import and nothing else. Use
    :func:`agent_tree_on_path` directly when the code inside the block also
    *calls* into Hermes (a call can itself trigger a fresh top-level import).
    """
    with agent_tree_on_path(path):
        return importlib.import_module(name)


def open_windows():
    """The paths with at least one window open right now (for tests/telemetry)."""
    with _LOCK:
        return dict(_WINDOWS)
