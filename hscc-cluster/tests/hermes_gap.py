"""One definition of "can THIS interpreter run hermes?" for the hscc-cluster leg
(t_b884497b).

FOUR capabilities, one module, one posture each:

* ``require_hermes_kanban_board()`` — can this interpreter reach hermes' kanban
  board API: ``hermes_cli.kanban_db`` (schema, ``init_db``, task/comment writes)
  plus ``hermes_cli.kanban_db_connect.connect`` and the dispatcher supervision in
  ``hermes_cli.kanban_db_dispatch``;
* ``require_hermes_cli_plugins()`` — can it reach ``hermes_cli.plugins``, i.e.
  ``VALID_HOOKS`` and ``invoke_hook`` (the hook-registration surface);
* ``require_hermes_cli_plugin_dispatcher()`` — can it reach
  ``hermes_cli.plugins_dispatch.PluginDispatchMixin``, the binder hermes uses to
  call every plugin hook;
* ``require_hermes_cli_profiles()`` — can it reach
  ``hermes_cli.profiles.get_active_profile_name``, which is what
  ``workflow._active_profile_name()`` resolves the ledger's ``profile_name``
  through.

WHY FOUR AND NOT ONE "hermes available" (the same question t_2cc3d482 and
t_74141b24 had to answer for their legs, answered here by measurement rather than
by taste). On the canonical interpreter each of hermes' runtime deps was blocked
one at a time (a synthetic "half-thin" env — a meta-path finder raising
``ImportError: BLOCKED:<name>``) and the imports re-tried, in a fresh interpreter
per block. ``F`` = the import fails, ``.`` = it succeeds:

    blocked                    kanban_db  _connect  _dispatch  plugins  plugins_dispatch  profiles
    (nothing)                  .          .         .          .        .                 .
    ruamel                     F          F         F          F        .                 .
    agent                      F          F         F          F        .                 F
    agent.file_safety          .          .         .          .        .                 F
    agent.deadline             F          F         F          .        .                 .
    toolsets                   F          F         F          .        .                 .
    utils                      F          F         F          F        .                 .
    hermes_constants           F          .         F          F        .                 F
    registration_lifecycle     .          .         .          F        .                 .
    hermes_cli.config          .          .         .          F        .                 .
    hermes_yaml                F          F         F          F        .                 .
    hermes_cli.archive_safe    .          .         .          .        .                 F
    hermes_cli.sqlite_util     F          F         F          .        .                 .
    hermes_cli.kanban_workflow F          F         F          .        .                 .

Read the rows, not the columns: every one of the four surfaces is separable from
at least one other, so one probe answering all of them would skip three families
for the fourth's reason — a blind spot wearing a green leg. The kanban trio is
the exception the table proves the other way: it fails and succeeds as a unit in
every row (``hermes_cli.kanban_db`` re-exports from ``_connect`` and ``_dispatch``
at import, so one cannot be importable without the others), so it is ONE
capability with three imports, not three capabilities.

What they share is the POSTURE, and that is the point of this module. Before it
existed this leg answered the same question four different ways — a module-level
``_HAS_HERMES_CLI`` flag set by a ``try``/``except ImportError`` and read by seven
``@pytest.mark.skipif`` marks (evaluated while pytest is still COLLECTING), and
three files' ``pytest.importorskip(...)``, which skips the whole module as ONE
collection item so a module of three cases reports as one skip. All of them said
``"hermes_cli not installed in this env"``: a capability name, no module, no
import error — which is precisely what ``scripts/run_tests.sh``'s stamp rule
cannot read off ``-rs``.

Both facts are in ``-rs`` today because both were measured, and one of them is a
defect the flag was hiding: 6 of the gated cases (``TestAutoUnblock``) pass on an
interpreter that HAS hermes only because ``workflow.on_kanban_task_claimed``
binds ``hermes_cli.kanban_db.connect = kanban_db_connect.connect`` into the
module ``__dict__`` (workflow.py:213-216, the
``if "connect" not in _kb.__dict__`` bind) — and the file that triggers that
binding, ``test_dispatch_hook.py``, sorts before this one. Run this file alone on
the canonical interpreter the six fail with ``AttributeError: module
'hermes_cli.kanban_db' has no attribute 'connect'``. The probe cannot fix that
(it only answers "is the module importable"), so ``_board()`` here now binds the
canonical ``connect`` itself, the way ``test_dispatch_hook.py`` and
``test_review_escalation.py`` already did.

Every probe re-checks at CALL time and caches nothing at import: the hermes-agent
tree can reach ``sys.path`` later than collection (measured inside one thin-leg
process by t_0fd2c149), and a cached "not available" silently skips cases that
could have run. Neither probe imports at module scope either.

Importable as a bare module because ``tests/conftest.py`` puts both the plugin dir
and this dir on ``sys.path`` (conftest.py:6/:9) — the same way ``clusterlib`` and
``sparkrun_fence`` are imported here.

COLLISION NOTE: ``hscc-api/tests`` and ``hscc-roles/tests`` each have a
``hermes_gap.py`` of their own. The runner runs one pytest process per plugin dir
precisely so these bare names cannot collide (see the header of
``scripts/run_tests.sh``); collecting two legs in ONE process would bind whichever
imported first, and this leg's guards would then die with an ``AttributeError``
naming the missing probe — loud, not silent.
"""

import pytest

#: The phrase every skip in this leg uses, so the guard can assemble it instead
#: of copying it (a second copy of the wording is a second place to drift).
NOT_IMPORTABLE = "not importable on this interpreter"

#: The kanban capability's label, used as the key into CAPABILITY_WHY.
_KANBAN = "hermes' kanban board"

#: Capability name -> the imports it probes, in the order they are tried. The
#: kanban trio is one entry because the table above shows it fails as a unit.
CAPABILITY_MODULES = (
    (_KANBAN,
     ("hermes_cli.kanban_db",
      "hermes_cli.kanban_db_connect",
      "hermes_cli.kanban_db_dispatch")),
    ("hermes' plugin/hook registry", ("hermes_cli.plugins",)),
    ("hermes' plugin hook binder", ("hermes_cli.plugins_dispatch",)),
    ("hermes' active-profile resolver", ("hermes_cli.profiles",)),
)

#: What each capability is FOR on this leg, in the skip reason. Read by the
#: guard so the wording lives in exactly one file.
CAPABILITY_WHY = {
    _KANBAN:
        "these cases build a real temp board (init_db/connect) and drive "
        "hermes' own dispatcher writes against it",
    "hermes' plugin/hook registry":
        "these cases assert on hermes' own VALID_HOOKS and monkeypatch its "
        "invoke_hook",
    "hermes' plugin hook binder":
        "these cases drive the handler through hermes' own "
        "PluginDispatchMixin._invoke_hook_callback, the binder every plugin "
        "hook goes through",
    "hermes' active-profile resolver":
        "these cases assert that the ledger's profile_name is the one "
        "workflow._active_profile_name() resolves through hermes",
}


def _first_import_error(modules):
    """``(None, None)`` when every module in *modules* imports here, else
    ``(the module that failed, the error as text)``.

    Keyed on the EXACT imports the cases and production perform — a probe that
    asked a neighbouring import would answer a different question, which is the
    reason ``plugins_dispatch`` is its own capability even though
    ``hermes_cli.plugins`` imports it: the binder cases never import the registry,
    and the registry needs ``hermes_cli.config`` + ``registration_lifecycle``
    while the binder needs neither.
    """
    for module in modules:
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001 — the exception IS the report
            return module, f"{type(exc).__name__}: {exc}"
    return None, None


def _skip(capability, module, error):
    pytest.skip(f"{module} {NOT_IMPORTABLE} ({error}) — {CAPABILITY_WHY[capability]}; "
                "needs the hermes-agent runtime / hermes venv, not just pytest "
                "(t_b884497b)")


# --------------------------------------------------------------------------- #
# Capability 1 — hermes' kanban board (schema + connect + dispatcher)
# --------------------------------------------------------------------------- #

def hermes_kanban_board_import_error():
    """``None`` when the kanban trio imports here, else ``"<module>: <error>"``."""
    module, error = _first_import_error(CAPABILITY_MODULES[0][1])
    return None if module is None else f"{module}: {error}"


def require_hermes_kanban_board():
    """Skip the calling test when hermes' kanban board is unreachable here.

    The board cases cannot be satisfied another way: ``_try_auto_unblock`` and
    the escalation contract read and write hermes' own schema, so with the module
    missing the case is not failing, it is unsatisfiable — and a bare ``skip()``
    naming no module is what hid that (and, in ``TestAutoUnblock``'s case, hid a
    real ``AttributeError`` behind a green leg).

    PARENT/CHILD: ``test_protocol_violation_bound.py`` drives the reclaim path in
    a CHILD spawned with ``sys.executable``. The gate runs in the parent, which is
    the safe direction: the child's env differs from the parent's only in dropping
    ``HERMES_DELEGATED_CHILD_CONTEXT`` and setting ``PVB_STRIPPED``/``HERMES_HOME``
    (test_protocol_violation_bound.py:47-61), none of which is on its import path,
    so a parent that cannot import it means the child cannot either. A parent that
    CAN and a child that cannot leaves the case RUNNING and RED — the honest
    outcome.
    """
    module, error = _first_import_error(CAPABILITY_MODULES[0][1])
    if module is not None:
        _skip(_KANBAN, module, error)


# --------------------------------------------------------------------------- #
# Capability 2 — hermes' plugin/hook registry
# --------------------------------------------------------------------------- #

def hermes_cli_plugins_import_error():
    """``None`` when ``hermes_cli.plugins`` imports here, else the error as text."""
    module, error = _first_import_error(CAPABILITY_MODULES[1][1])
    return None if module is None else error


def require_hermes_cli_plugins():
    """Skip the calling test when hermes' hook registry is unreachable here.

    Separated from the kanban board because ``plugins`` survives without
    ``ruamel``/``toolsets``/``agent.deadline`` while the board does not (table
    above): one gate would skip ``VALID_HOOKS`` assertions for a YAML-writer
    reason they do not depend on.
    """
    module, error = _first_import_error(CAPABILITY_MODULES[1][1])
    if module is not None:
        _skip("hermes' plugin/hook registry", module, error)


# --------------------------------------------------------------------------- #
# Capability 3 — hermes' plugin hook binder
# --------------------------------------------------------------------------- #

def hermes_cli_plugin_dispatcher_import_error():
    """``None`` when ``hermes_cli.plugins_dispatch`` imports here, else the error."""
    module, error = _first_import_error(CAPABILITY_MODULES[2][1])
    return None if module is None else error


def require_hermes_cli_plugin_dispatcher():
    """Skip the calling test when hermes' own callback binder is unreachable.

    ``hermes_cli.plugins`` imports this module, but not the other way round, and
    blocking ``registration_lifecycle`` or ``hermes_cli.config`` breaks the
    registry while the binder is fine — so a case that only needs the binder must
    not be skipped for the registry's reason.
    """
    module, error = _first_import_error(CAPABILITY_MODULES[2][1])
    if module is not None:
        _skip("hermes' plugin hook binder", module, error)


# --------------------------------------------------------------------------- #
# Capability 4 — hermes' active-profile resolver
# --------------------------------------------------------------------------- #

def hermes_cli_profiles_import_error():
    """``None`` when ``hermes_cli.profiles`` imports here, else the error as text.

    Keyed on the import production performs — ``from hermes_cli.profiles import
    get_active_profile_name``, which sits in ``workflow._active_profile_name``'s
    inner ``_resolve()`` (named by symbol, not by line number: t_847ab03b / PR #364
    moved it when it put the call inside the import window, and a number quoted here
    would drift again the next time that function is edited).
    Same capability name as ``hscc-roles/tests/hermes_gap.py`` on purpose: an
    operator reading ``-rs`` across legs should see one wording per capability.
    """
    module, error = _first_import_error(CAPABILITY_MODULES[3][1])
    return None if module is None else error


def require_hermes_cli_profiles():
    """Skip the calling test when hermes' profile resolver is unreachable here.

    ``workflow._active_profile_name()`` swallows this import error and returns
    ``None`` — right for production (a machine without hermes must still log),
    wrong for a case asserting the ledger stamped a REAL name: with ``None`` the
    assertion compares against a sentinel and proves nothing.
    """
    module, error = _first_import_error(CAPABILITY_MODULES[3][1])
    if module is not None:
        _skip("hermes' active-profile resolver", module, error)


# --------------------------------------------------------------------------- #
# the same capability, asked of a CHILD process
# --------------------------------------------------------------------------- #

#: The traceback lines that mean "hermes, or a dependency hermes needs, is not
#: importable in that interpreter" — i.e. environment, not a bug.
_IMPORT_ERROR_MARKERS = ("ModuleNotFoundError", "ImportError")


def child_import_error(returncode, stderr):
    """The child's import failure line, or ``None`` when that is not why it died.

    ``None`` means "do NOT skip": a child that died of a ``sqlite3`` error, a
    missing file or an assertion is a finding, and reading it as environment is
    the failure this leg keeps hitting.
    """
    if returncode == 0:
        return None
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    return last if any(m in last for m in _IMPORT_ERROR_MARKERS) else None


def require_child_ran_hermes(returncode, stderr):
    """Skip when the CHILD this case spawned could not import hermes' board.

    ``test_protocol_violation_bound.py`` never uses hermes in this process — it
    runs ``_pvb_sim.py`` with ``sys.executable`` and an env that only drops
    ``HERMES_DELEGATED_CHILD_CONTEXT``. So this process's ``sys.path`` says nothing
    about the child's: the child gets ``sys.executable`` plus its own script dir,
    never the parent's path. That conclusion does not depend on what this process's
    path holds, and when this probe was written it held something that made the
    difference loud — ``workflow._active_profile_name()`` used to insert
    ``~/.hermes/hermes-agent`` into *this* process's ``sys.path`` the first time it
    ran and leave it there (production's hand-rolled import fence, until
    t_847ab03b / PR #364 made the fence restore it;
    ``test_hermes_fence_sys_path.py`` is what pins that now). Either way the child
    started afterwards did not inherit it. Measured on the thin sibling when this
    probe was written (t_b884497b): the in-process gate passed after that fence and
    the child still could not import hermes — which is why the case has to ask the
    child, and why asking it from the child's own stderr is stronger than a second
    probe here: the answer comes from the run that happened, not from a guess about
    the env.
    """
    error = child_import_error(returncode, stderr)
    if error is not None:
        pytest.skip(f"hermes_cli.kanban_db {NOT_IMPORTABLE} in the child this case "
                    f"spawns with sys.executable ({error}) — "
                    f"{CAPABILITY_WHY[_KANBAN]}; the child gets sys.executable plus "
                    "its own script dir, not this process's sys.path (t_b884497b)")


# --------------------------------------------------------------------------- #
# the posture, stated once so the guard can pin it
# --------------------------------------------------------------------------- #

#: Every probe this module owns, in definition order.
#: ``test_hermes_gap_single_posture.py`` pins that the owner defines exactly
#: these and that no other file in the leg defines one of them.
PROBES = (
    "hermes_kanban_board_import_error",
    "require_hermes_kanban_board",
    "hermes_cli_plugins_import_error",
    "require_hermes_cli_plugins",
    "hermes_cli_plugin_dispatcher_import_error",
    "require_hermes_cli_plugin_dispatcher",
    "hermes_cli_profiles_import_error",
    "require_hermes_cli_profiles",
    "child_import_error",
    "require_child_ran_hermes",
)

#: One entry per capability: ``(capability label, the modules it probes,
#: ``require_*``, its getter)``. The guard reads THIS table rather than carrying
#: its own copy of the vocabulary — a private skip reason is the fingerprint it is
#: looking for, and a second list of the same surfaces would be a second place to
#: drift. (``hscc-roles``'s ``CAPABILITIES`` holds ``(import, reason)``; this one
#: holds the modules plus the accessor names because one gate here spans three
#: modules, so the reason is DERIVED from the failing module rather than typed
#: once per capability.)
CAPABILITIES = (
    (_KANBAN,
     ("hermes_cli.kanban_db",
      "hermes_cli.kanban_db_connect",
      "hermes_cli.kanban_db_dispatch"),
     "require_hermes_kanban_board", "hermes_kanban_board_import_error"),
    ("hermes' plugin/hook registry",
     ("hermes_cli.plugins",),
     "require_hermes_cli_plugins", "hermes_cli_plugins_import_error"),
    ("hermes' plugin hook binder",
     ("hermes_cli.plugins_dispatch",),
     "require_hermes_cli_plugin_dispatcher",
     "hermes_cli_plugin_dispatcher_import_error"),
    ("hermes' active-profile resolver",
     ("hermes_cli.profiles",),
     "require_hermes_cli_profiles", "hermes_cli_profiles_import_error"),
)
