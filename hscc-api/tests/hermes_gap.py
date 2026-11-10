"""One definition of "can THIS interpreter run hermes' session DB?" for the
hscc-api leg (t_02c98915).

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
