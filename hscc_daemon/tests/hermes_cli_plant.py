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
