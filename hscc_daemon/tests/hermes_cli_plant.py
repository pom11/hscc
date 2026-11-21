"""ONE definition of "is this ``hermes_cli`` a planted fake?", plus the two safe
ways to get rid of one.

Why this module exists (t_b62da70c, spun out of PR #270 / t_2be3b04e)
---------------------------------------------------------------------
PR #270 put the authoritative predicate in ``conftest.py`` after one file
planting a fake ``hermes_cli`` silently decided what a LATER file's board
enumeration saw (``docs/audits/orch-waker-import-leak_t_2be3b04e.md``). Two
private copies of the same rule survived that fix —
``test_autodown.TestLoadKanbanDb`` (substring-matched the test's own tmp dir and
the pytest tmp-dir marker) and ``test_board_pin._evict_planted_hermes_cli``
(substring-matched that marker alone). Three definitions of one hazard is how
this class of bug keeps coming back (t_b014c2b8 hit it from the other direction
in ``8e2283a7``). All three sites now call the functions here.

Why a real module and not conftest
----------------------------------
A test module must not ``import conftest``: a second import of conftest re-runs
its module-level sparkrun-fence loader and swaps the live fence out from under
the running suite — the same class of hazard this file family is about. So the
RULE lives here, in a plain module with no import-time side effects that every
site can import directly (cf. ``profile_roster_sandbox.py``), while conftest
keeps the autouse *fixture* (fixtures need ``pytest``) and simply binds these
functions under its historic private names.

The rule (unchanged from #270, deliberately narrow)
---------------------------------------------------
A resident ``hermes_cli`` is a planted fake iff the file it was loaded from
resolves under a pytest temp root. The real ``~/.hermes/hermes-agent`` tree is
never a plant and is never touched by anything here. Two shapes are poison:

  1. the module's OWN ``__file__`` is under a temp root (a planted fake still
     resident), or
  2. a REAL module holds a collaborator bound to such a fake. The real
     ``hermes_cli.kanban_db_connect`` imported while a fake is resident binds
     ``_kb = <fake>`` for the life of the process — so evicting the fake alone
     does NOT repair it, and the next consumer gets a real file with a fake
     inside it.

``evict_planted`` handles shape 1 and hands back what it removed, so a fixture
that merely *borrowed* the name can put it back (that is what
``test_board_pin.real_root`` does); ``poison_reason`` + ``purge_planted`` handle
shape 2, which needs the whole ``hermes_cli*`` subtree dropped and re-imported.
"""

import os
import sys
import tempfile
from types import ModuleType

# Roots pytest creates tmp dirs under. ``realpath`` because macOS mounts the
# temp dir through a symlink (/tmp -> /private/tmp) and a planted path arrives
# in either spelling.
TEMP_ROOT_MARKERS = (os.path.realpath(tempfile.gettempdir()),
                     os.path.realpath("/private/var/folders"))

# Attributes through which a real hermes_cli module binds its siblings at import
# time (``hermes_cli/kanban_db_*.py`` all do ``import kanban_db as _kb``).
COLLABORATOR_ATTRS = ("_kb", "kanban_db")


def is_planted_path(path):
    """True for a path under a pytest temp root — i.e. a planted fake tree.

    The rule is deliberately narrow: the real ``~/.hermes/hermes-agent`` tree is
    never a plant and is never touched by any of this.
    """
    origin = str(path or "")
    return bool(origin) and os.path.realpath(origin).startswith(TEMP_ROOT_MARKERS)


def hermes_cli_names():
    """Every resident ``hermes_cli`` / ``hermes_cli.*`` module name."""
    return [n for n in sys.modules
            if n == "hermes_cli" or n.startswith("hermes_cli.")]


def poison_reason():
    """Name why the resident ``hermes_cli`` cache must not survive, else None.

    See the module docstring for the two shapes; both are observed at the end of
    the leg that PR #270 fixed.
    """
    for name in hermes_cli_names():
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if is_planted_path(getattr(mod, "__file__", "")):
            return f"{name} is planted under a temp root"
        for attr in COLLABORATOR_ATTRS:
            other = getattr(mod, attr, None)
            if isinstance(other, ModuleType) and is_planted_path(
                    getattr(other, "__file__", "")):
                return (f"{name}.{attr} is bound to planted "
                        f"{getattr(other, '__name__', '?')}")
    return None


def purge_planted(saved=None):
    """Drop the whole ``hermes_cli`` subtree, optionally restoring ``saved``.

    Evicting only the planted entries is not enough (shape 2 above): a real
    module already holding the fake has to be re-imported too, so the entire
    ``hermes_cli*`` subtree goes and the next real consumer rebinds cleanly from
    ``~/.hermes/hermes-agent``. ``saved`` (the cache as it was at the START of
    the test that poisoned it) is put back by identity so the process keeps the
    objects its earlier tests were given.
    """
    for name in hermes_cli_names():
        del sys.modules[name]
    if saved:
        sys.modules.update(saved)


def evict_planted():
    """Take every planted ``hermes_cli`` out of ``sys.modules``, and return them.

    Narrower than ``purge_planted``: only modules whose OWN file is under a
    pytest temp root go, so a real module that has already been contaminated
    stays put. That is the right tool for a test that is about to resolve the
    real library through the production loader and needs the planted name free
    for one moment; the caller that borrows the name SHOULD hand it back with
    ``restore_planted`` (an unfinalised borrow is exactly the cross-file
    corruption PR #270 was written against). The autouse conftest fixture is
    still the process-wide guarantee — this is the mid-test tool.

    Returns ``{name: module}`` for the caller to restore.
    """
    evicted = {}
    for name in hermes_cli_names():
        mod = sys.modules.get(name)
        if mod is not None and is_planted_path(getattr(mod, "__file__", "")):
            evicted[name] = sys.modules.pop(name)
    return evicted


def restore_planted(evicted):
    """Hand back what ``evict_planted`` borrowed, by identity."""
    for name, mod in (evicted or {}).items():
        sys.modules[name] = mod


# ---------------------------------------------------------------------------
# The OTHER residue: hermes' REAL tree, left behind by a closed window
# (t_572b4856)
# ---------------------------------------------------------------------------
#
# ``hermes_import.agent_tree_on_path`` restores ``sys.path`` correctly — measured
# over a whole ``hscc_daemon`` leg: 804 events touching the agent tree, zero
# entries left on the path at the end. What a window cannot take back is what it
# *imported*: ``sys.modules`` keeps every module the block loaded, and a resident
# package carries ``__path__`` pointing into ``~/.hermes/hermes-agent``, so from
# then on ``import hermes_cli.<anything>`` resolves with the tree nowhere on
# ``sys.path``. A FAILED submodule import is enough to create it — ``from
# hermes_cli import kanban_db`` on a thin interpreter imports the parent fine,
# raises on the child, and leaves the parent resident.
#
# That is an order dependency, not a capability: the same ``importorskip`` line
# answers ``No module named 'hermes_cli'`` in a file run alone and ``No module
# named 'utils'`` in the full leg (full measurement, both interpreters:
# ``docs/audits/daemon-alone-run-order-dependence_t_572b4856.md``). The rule here
# is what lets the conftest fixture hand the NEXT test the cache it was given,
# instead of the cache some earlier test's window happened to leave.
#
# Deliberately narrow, like ``is_planted_path``: it recognises a module by where
# its own file lives, so a planted temp-root fake is still the plant rule's job
# and this one never touches it.
#
# Cost matters here: the fixture calls this for EVERY test in a 4000-test leg, so
# the classifier must not do a syscall per resident module. Two things keep it
# flat — a plain string prefix test before any ``realpath``, and a verdict cache
# keyed by the module OBJECT (a module's ``__file__`` does not change), so after
# the first scan a call is one dict lookup per module.

_ROOT_ENV = None                     # $HERMES_AGENT_PATH the cache was built for
_ROOTS = ("", "")                    # (raw, realpath) spellings
_REALPATH_MEMO = {}
_VERDICT_CACHE = {}                  # root -> {module object: bool} (identity-keyed)


def _realpath(path):
    hit = _REALPATH_MEMO.get(path)
    if hit is None:
        hit = _REALPATH_MEMO[path] = os.path.realpath(path)
    return hit


def _agent_tree_roots():
    """(raw, realpath) spellings of the agent tree, cached per $HERMES_AGENT_PATH.

    Both are needed: a module's ``__file__`` is spelled with whatever form of the
    root sat on ``sys.path`` when it was imported, and ``hermes_import`` hands
    out the un-resolved form.
    """
    global _ROOT_ENV, _ROOTS
    env = os.environ.get("HERMES_AGENT_PATH")
    if _ROOT_ENV != env or not _ROOTS[0]:
        try:
            from hscc_daemon import hermes_import
            raw = hermes_import.resolve_agent_tree()
        except Exception:  # pragma: no cover — the fallback is the same resolution
            raw = os.path.expanduser(env or "~/.hermes/hermes-agent")
        raw = os.path.expanduser(str(raw))
        _ROOTS = (raw, _realpath(raw))
        _ROOT_ENV = env
    return _ROOTS


def agent_tree_root():
    """realpath of the Hermes agent tree THIS process resolves hermes from.

    Resolved through production's own ``hermes_import.resolve_agent_tree`` so the
    test rule and the shipped code cannot disagree about where hermes lives. Call
    it at fixture SETUP, not at teardown: a test that monkeypatches
    ``HERMES_AGENT_PATH`` to a temp tree is planting, and plants are the rule
    above.
    """
    return _agent_tree_roots()[1]


def is_agent_tree_path(path, root=None):
    """True for a path inside hermes' REAL agent tree (never a temp-root plant)."""
    origin = str(path or "")
    if not origin:
        return False
    raw, real = _agent_tree_roots()
    r = root or real
    # No syscall unless the string could already be under one of the trees. This
    # rejects every stdlib and site-packages path, which is >99% of the calls.
    if not (origin.startswith(r) or origin.startswith(raw)
            or origin.startswith(real)):
        return False
    resolved = _realpath(origin)
    return resolved == r or resolved.startswith(r + os.sep)


def _is_agent_tree_module(mod, r):
    """Verdict for one module object, cached by identity (see the note above)."""
    cache = _VERDICT_CACHE.setdefault(r, {})
    try:
        verdict = cache.get(mod)
    except TypeError:                      # unhashable module object
        verdict = None
    if verdict is None:
        verdict = is_agent_tree_path(getattr(mod, "__file__", ""), r)
        if not verdict:
            search = getattr(mod, "__path__", None)
            if isinstance(search, str):
                search = [search]
            verdict = bool(search) and any(
                is_agent_tree_path(p, r) for p in search)
        try:
            cache[mod] = verdict
        except TypeError:
            pass
    return verdict


def resident_under_agent_tree(root=None):
    """Names of resident modules loaded from hermes' real tree.

    Covers packages with no ``__file__`` (their ``__path__`` is what makes
    submodules resolvable, so a ``__path__`` under the tree is the residue that
    matters) and skips a ``__path__`` that is a plain string, which would
    otherwise iterate as characters.
    """
    r = root or agent_tree_root()
    return [name for name, mod in list(sys.modules.items())
            if mod is not None and _is_agent_tree_module(mod, r)]


def restore_agent_tree_cache(root, saved):
    """Undo one test's window residue: drop what it imported, keep what it found.

    ``root`` / ``saved`` come from the fixture (resolved + snapshotted BEFORE the
    test ran). Modules that were already resident when the test started are put
    back by identity, exactly as ``purge_planted`` does for a poisoned cache, so
    an earlier test keeps the module objects it was handed; anything the test's
    own window imported goes.
    """
    for name in resident_under_agent_tree(root):
        if name not in saved:
            del sys.modules[name]
    if saved:
        sys.modules.update(saved)

