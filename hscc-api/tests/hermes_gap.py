"""One definition of "can THIS interpreter run hermes' runtime?" for the
hscc-api leg (t_02c98915, and a second capability added by t_2cc3d482).

TWO capabilities, one module, one posture each way:

* ``require_hermes_state()`` — can this interpreter build a real
  ``hermes_state.SessionDB`` (the gateway / session-resolver family);
* ``require_hermes_profile_library()`` — can this interpreter reach
  ``hermes_cli.profiles`` at all (the ``HSCC_API_PROFILES_ROOT`` family in
  ``test_profile_root.py``).

They are different capabilities and must stay separately probed: ``hermes_state``
needs ``ruamel.yaml`` and nothing of the CLI's, and a thin interpreter that
lacks one does not necessarily lack the other. What they share is the posture —
a capability probe, never ``sys.version_info``, and a real ``pytest.skip()``
carrying the import error itself.

The suite's real-state-DB tests (``test_gateway_store_tail``,
``test_session_resolver``) build a genuine ``hermes_state.SessionDB``, which
needs HERMES' own runtime deps in the *test* interpreter — ``hermes_state``
pulls ``hermes_yaml``, which pulls ``ruamel.yaml``.

``scripts/run_tests.sh`` selects its interpreter through ``HSCC_TEST_PY`` and the
sanctioned sibling (``~/miniconda3/envs/p313/bin/python``) is deliberately a thin
pytest env: pytest, no hermes deps. There those tests hard-failed on
``ModuleNotFoundError: No module named 'ruamel'`` while their siblings skipped —
the same missing module, two postures, and so a red sibling leg that could not be
told apart from "this branch broke 3.13". Measurement, roster and the decision to
gate rather than dep-stuff the sibling env:
``docs/audits/sibling-interpreter-hermes-gap_t_02c98915.md``.

Importable as a bare module because pytest puts this directory on ``sys.path`` —
the same way ``gateway_driver`` is imported here.

The probe is a CAPABILITY probe (can the import be made here?), never a
``sys.version_info`` check: under any interpreter that can import hermes the
gated tests run. Per GAP-D-1 (t_b82ad0fe) the skip is a real ``pytest.skip()``
carrying the import error itself, visible through the canonical runner's ``-rs``.
"""

import sys

import pytest

try:
    from hermes_state import SessionDB  # noqa: F401  (the probe IS the import)
    _IMPORT_ERROR = None
except Exception as _exc:  # noqa: BLE001 — the exception IS the report
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def hermes_state_import_error():
    """``None`` when ``hermes_state`` imports here, else the error as text.

    The module-level attempt above is the fast path. A failure is re-tried at
    call time on purpose: the hermes-agent tree can reach ``sys.path`` later
    than this module's import (the production resolvers insert it themselves),
    and a stale "not available" would silently skip tests that could have run —
    a false-green of the kind GAP-D-1 was written against.
    """
    if _IMPORT_ERROR is None:
        return None
    try:
        import hermes_state  # noqa: F401  (re-check, do not cache the failure)
        return None
    except Exception as exc:  # noqa: BLE001 — the exception IS the report
        return f"{type(exc).__name__}: {exc}"


def require_hermes_state():
    """Skip the calling test when this interpreter cannot run ``SessionDB``."""
    error = hermes_state_import_error()
    if error is not None:
        pytest.skip("hermes_state not importable on this interpreter "
                    f"({error}) — needs the hermes-agent runtime / "
                    "hermes venv, not just pytest (t_02c98915)")


# --------------------------------------------------------------------------- #
# Capability 2 — can this interpreter reach hermes' OWN profile library?
# --------------------------------------------------------------------------- #

def hermes_profile_library_import_error():
    """``None`` when ``hermes_cli.profiles`` imports here, else the error as text.

    The capability ``test_profile_root.py`` needs is NOT ``SessionDB``: its
    gated cases spawn CHILDREN (``sys.executable``, an env with ``PYTHONPATH``
    and the virtualenv vars dropped — see ``_child_env``) that resolve and write
    through the REAL ``hermes_cli.profiles``:
    ``profile_root._native_profiles_dir()`` for the root answer and
    ``routes_profiles._profiles_module()`` for the write. When that import fails
    the root answers ``None``, the write raises ``ModuleNotFoundError`` and
    ``profile_root.divergence()`` omits the key — three failure shapes, one
    cause (measured: ``6 failed`` on the thin interpreter at ``72f65838``).

    Unlike :func:`hermes_state_import_error` there is no module-level attempt:
    importing the real ``hermes_cli`` into this leg's ``sys.modules`` at
    collection time is exactly what the profile tests inject a fake for
    (``fake_hermes``, ``test_c8_hermes_cli_unimportable_...``), and the probe is
    called once per gated test, so there is nothing to cache.

    The probe runs in the PARENT while the assertion runs in the CHILD, and the
    two can only disagree in the SAFE direction: the child's ``sys.path`` is a
    subset of the parent's apart from the repo dirs (``PYTHONPATH``,
    ``VIRTUAL_ENV`` and ``CONDA_PREFIX`` are dropped for the child, and the
    parent has the plugin dir plus the test dir inserted by ``conftest``), so a
    parent that cannot import it means the child cannot either, while a parent
    that CAN and a child that cannot leaves the test RUNNING and red. Under-report
    (a false skip) is not reachable; over-report keeps the gate honest.
    """
    try:
        from hermes_cli.profiles import get_profile_dir  # noqa: F401
        return None
    except Exception as exc:  # noqa: BLE001 — the exception IS the report
        return f"{type(exc).__name__}: {exc}"


def require_hermes_profile_library():
    """Skip the calling test when hermes' profile library is unreachable here.

    Same posture as :func:`require_hermes_state` — a real ``pytest.skip()``
    carrying the import error itself, visible through the canonical runner's
    ``-rs``, keyed on a capability and never on a version (t_02c98915).

    ANTI-VACUITY (the constraint this gate was written under, t_2cc3d482). The
    predicate is the one ``profile_root._native_profiles_dir()`` itself uses, so
    the gate can only fire when the assertion it guards is unsatisfiable for
    that same reason, and ``test_f1_hermes_gate_agrees_with_the_resolver_it_guards``
    pins that equality on EVERY interpreter, gated or not. The unimportable branch
    of the guard is not this gate's coverage either: ``test_c8_...`` and
    ``test_c9_...`` assert it in-process with ``roots.hermes = None`` injected,
    so they run (and must pass) on the thin interpreter too.
    """
    error = hermes_profile_library_import_error()
    if error is not None:
        pytest.skip("hermes_cli.profiles not importable on this interpreter "
                    f"({error}) — these cases spawn a child on "
                    f"{sys.executable} that resolves and writes through the "
                    "real hermes profile library; needs the hermes-agent "
                    "runtime / hermes venv, not just pytest (t_2cc3d482)")
