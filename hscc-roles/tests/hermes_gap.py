"""One definition of "can THIS interpreter run hermes' runtime?" for the
hscc-roles leg (t_74141b24).

TWO capabilities, one module, one posture each:

* ``require_hermes_cli_profiles()`` — can this interpreter reach
  ``hermes_cli.profiles.get_profile_dir``, which is what
  ``rolelib.native_profiles_dir()`` resolves the writer's root through;
* ``require_hermes_cli_config_writer()`` — can this interpreter reach
  ``hermes_cli.config.atomic_config_write``, which is the writer
  ``generator._hermes_atomic_config_write()`` delegates to.

They are different capabilities and stay separately probed (the same
distinction t_2cc3d482 drew for ``hscc-api``): ``hermes_cli.config`` pulls the
ruamel round-trip machinery, ``hermes_cli.profiles`` does not, and skipping one
family for the other's reason is a blind spot wearing a green leg.

What they share is the POSTURE, and that is the point of this module. Before it
existed this leg answered the same question three different ways:

  * ``test_profile_root_assert.py`` inferred "not importable" from a CHILD
    process's non-zero exit — so a child that died for any reason at all (a real
    ``rolelib`` bug included) reported itself as "environment", and the skip
    reason named no module;
  * ``test_generator.py``'s ``_hermes_writer_available()`` asked production code
    for a bool and threw the error away, as a ``@pytest.mark.skipif`` — evaluated
    at COLLECTION, so it can only ever skip, including cases that had become
    runnable by the time they ran;
  * and a fourth site skipped with ``"the refusal lives in hermes' writer"``,
    which names a capability and no import.

One missing module, three postures, and a leg nobody could read. The rule this
module is the answer to lives in ``scripts/run_tests.sh``'s
``sibling interpreter is THIN on purpose`` roster block (the LIVE authority for
which probes exist), and the measurement behind it is
``docs/audits/sibling-interpreter-hermes-gap_t_02c98915.md``.
``hscc-roles/tests/test_hermes_gap_single_posture.py`` keeps the postures from
regrowing.

Both probes re-check at CALL time and cache nothing at import: the hermes-agent
tree can reach ``sys.path`` later than collection (measured inside one thin-leg
process by t_0fd2c149), and a cached "not available" silently skips cases that
could have run. Neither probe imports at module scope either — this leg's cases
spawn CHILDREN and hand them a rewritten environment, so an import that succeeded
here at collection time would say nothing about any later call.

Importable as a bare module because ``tests/conftest.py`` puts the plugin dir on
``sys.path`` and pytest adds the test dir; the same way ``rolelib`` is imported
here.

COLLISION NOTE: ``hscc-api/tests`` has a ``hermes_gap.py`` of its own. The
runner runs one pytest process per plugin dir precisely so these bare names
cannot collide (see the header of ``scripts/run_tests.sh``); collecting both
legs in ONE process would bind whichever imported first, and this leg's guards
would then die with an ``AttributeError`` naming the missing probe — loud, not
silent.
"""

import pytest


# --------------------------------------------------------------------------- #
# Capability 1 — hermes' own profile resolver
# --------------------------------------------------------------------------- #

def hermes_cli_profiles_import_error():
    """``None`` when ``hermes_cli.profiles`` imports here, else the error as text.

    Keyed on the EXACT import the production code performs
    (``rolelib.native_profiles_dir``, rolelib.py:132:
    ``from hermes_cli.profiles import get_profile_dir``) — a probe that asked a
    neighbouring import would answer a different question.

    The capability matters because ``native_profiles_dir()`` swallows this import
    error and returns ``None`` — which is right for production (a machine without
    hermes must still be able to run) and wrong for a test that is asserting the
    native root refused a write: with ``None`` the divergence arm of
    ``rolelib.assert_profile_root`` has nothing to compare against, so the
    incident-guard cannot fire and the case is not failing, it is unsatisfiable.
    Measured at ``2d4320c6``: three cases in ``test_profile_root_assert.py`` and
    two in ``test_sync_profile_root.py`` went RED on the thin sibling for exactly
    this reason (``DID NOT RAISE ProfileRootMismatch`` / ``rc=0`` where ``2`` was
    the contract), while the leg's other hermes skips said nothing about it.
    """
    try:
        from hermes_cli.profiles import get_profile_dir  # noqa: F401
        return None
    except Exception as exc:  # noqa: BLE001 — the exception IS the report
        return f"{type(exc).__name__}: {exc}"


def require_hermes_cli_profiles():
    """Skip the calling test when hermes' profile resolver is unreachable here.

    A real ``pytest.skip()`` carrying the import error itself, because that text
    is what ``scripts/run_tests.sh``'s stamp rule reads off ``-rs``: it is the
    difference between "the sibling env is thin" (correct) and "hermes is
    installed here and the guard is still broken" (a finding).

    PARENT/CHILD: most gated cases assert on a CHILD process spawned with
    ``sys.executable``. The gate runs in the parent. That is the safe direction,
    and it is worth saying why: the children get ``dict(os.environ)`` with the
    kanban pins and ``HSCC_REGISTRY`` removed and HOME/HERMES_HOME rewritten
    (``_child_env``), none of which is on the child's import path, so the
    child's ``sys.path`` is the interpreter's own plus the script dir — a
    parent that cannot import it means the child cannot either. A parent that
    CAN and a child that cannot leaves the case RUNNING and RED, which is the
    honest outcome; the old code called that "environment" and skipped it.
    """
    error = hermes_cli_profiles_import_error()
    if error is not None:
        pytest.skip("hermes_cli.profiles not importable on this interpreter "
                    f"({error}) — these cases assert on hermes' own native root "
                    "(rolelib.native_profiles_dir -> get_profile_dir); needs the "
                    "hermes-agent runtime / hermes venv, not just pytest "
                    "(t_74141b24)")


# --------------------------------------------------------------------------- #
# Capability 2 — hermes' own round-trip config writer
# --------------------------------------------------------------------------- #

def hermes_cli_config_writer_import_error():
    """``None`` when hermes' writer imports here, else the error as text.

    Keyed on the import production performs (``generator._hermes_atomic_config_write``,
    generator.py:847: ``from hermes_cli.config import atomic_config_write``).
    The four cases behind it pin what only the real writer can give: operator
    comments surviving a regeneration, and a payload that would drop a mapping
    path REFUSING instead of falling back. The fallback writer's own contract is
    asserted separately (``test_fallback_writer_preserves_foreign_keys_without_hermes``)
    and runs on the thin interpreter too, so gating these four loses nothing.
    """
    try:
        from hermes_cli.config import atomic_config_write  # noqa: F401
        return None
    except Exception as exc:  # noqa: BLE001 — the exception IS the report
        return f"{type(exc).__name__}: {exc}"


def require_hermes_cli_config_writer():
    """Skip the calling test when hermes' round-trip writer is unreachable here.

    Replaces ``test_generator.py``'s ``_hermes_writer_available()`` and its three
    ``@pytest.mark.skipif`` marks. Not a cosmetic swap: a mark evaluates its
    predicate while pytest is still COLLECTING, so the answer is frozen before
    anything in the leg has had the chance to reach the hermes tree, and it can
    only ever move a case from running to skipped. Asking here instead means the
    case runs whenever it can run — and that the reason in ``-rs`` names the
    module that failed rather than "hermes' round-trip writer is not importable
    here".
    """
    error = hermes_cli_config_writer_import_error()
    if error is not None:
        pytest.skip("hermes_cli.config not importable on this interpreter "
                    f"({error}) — the write these cases pin is delegated to "
                    "hermes' own round-trip writer "
                    "(generator._hermes_atomic_config_write); needs the "
                    "hermes-agent runtime / hermes venv, not just pytest "
                    "(t_74141b24)")


# --------------------------------------------------------------------------- #
# the posture, stated once so the guard can pin it
# --------------------------------------------------------------------------- #

#: Every probe this module owns, in definition order.
#: ``test_hermes_gap_single_posture.py`` pins that the owner defines exactly
#: these and that no other file in the leg defines one of them.
PROBES = (
    "hermes_cli_profiles_import_error",
    "require_hermes_cli_profiles",
    "hermes_cli_config_writer_import_error",
    "require_hermes_cli_config_writer",
)

#: One entry per capability: ``(<import the probe performs>, <the skip REASON the
#: owner writes for it>)``. The guard reads THIS table rather than carrying its
#: own copy of the wording — a private skip reason is the fingerprint it is
#: looking for, and a second copy of the wording would be a second place for a
#: posture to drift. Each reason must stay able to name the failing module, which
#: is the part the stamp rule reads off ``-rs``.
CAPABILITIES = (
    ("hermes_cli.profiles",
     "hermes_cli.profiles not importable on this interpreter"),
    ("hermes_cli.config",
     "hermes_cli.config not importable on this interpreter"),
)
