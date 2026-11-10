"""One definition of "can THIS interpreter run real Hermes?", and the skip that
follows from it (t_02c98915).

Why this module exists
----------------------
The suite drives three different real-Hermes seams: the kanban library
(``hermes_cli.kanban_db``), Hermes' profile predicates
(``hermes_cli.profiles.profile_is_standalone``), and the session database
(``hermes_state.SessionDB``). Each of them needs Hermes' OWN runtime deps —
``hermes_yaml`` imports ``ruamel.yaml``, and ``hermes_cli.kanban_db`` imports
``utils`` which imports ``hermes_yaml``.

The canonical runner takes any interpreter through ``HSCC_TEST_PY``, and the
sanctioned sibling interpreter is deliberately a *thin pytest env*: pytest, no
Hermes deps. Under it, 21 tests hard-failed on ``ModuleNotFoundError: No module
named 'ruamel'`` while their siblings in the same files skipped — the same
missing module, two postures, and therefore a red dual-interpreter stamp that
could not be told apart from "this branch broke 3.13" (full measurement and the
decision to gate rather than dep-stuff the sibling env:
``docs/audits/sibling-interpreter-hermes-gap_t_02c98915.md``).

This is the gate the other half of the suite already used
(``test_board_pin.real_root``, ``test_dispatcher_wedge_benign_holds``,
``hscc-api``'s ``hs_state``). One definition, so a fourth site cannot invent a
fifth posture.

The probe is a CAPABILITY probe, never a version check
------------------------------------------------------
It asks the production loader whether the library resolves *here*. That is what
matters and it is the same question the gated test asks: under the primary
interpreter (Hermes' own venv, deps present) the probe succeeds and every gated
test RUNS; under a thin interpreter it fails and they skip with the loader's own
reason. A ``sys.version_info`` test would skip tests that could have run, and
would keep passing on an interpreter where Hermes is genuinely broken.

Per GAP-D-1 (t_b82ad0fe) the skip is a real ``pytest.skip()`` with a visible
reason, reported by ``-rs`` in the canonical runner — never a verdict string and
never a silent pass.
"""

from __future__ import annotations

import pytest

from hscc_daemon.tests.hermes_cli_plant import (evict_planted,
                                                restore_planted)


def _resolve_real_kanban():
    """``(lib, None)`` when real Hermes' kanban lib loads here, ``(None, why)``.

    A resident planted fake would make the loader answer for the plant instead
    of for this interpreter, so the borrow is ``evict_planted`` and the hand-back
    is ``restore_planted`` in a ``finally`` — a gate must not change what the
    test it just let through sees (the discipline
    ``hermes_cli_plant.evict_planted`` documents).
    """
    from hscc_daemon import autodown
    borrowed = evict_planted()
    try:
        lib = autodown._load_kanban_db_or_default()
        if lib is not None:
            return lib, None
        state = autodown.kanban_check_state() or {}
        reason = state.get("reason") or repr(state)
        return None, (f"real hermes_cli.kanban_db is not importable in this "
                      f"interpreter: {reason}")
    finally:
        restore_planted(borrowed)


def real_hermes_block_reason():
    """``None`` when real Hermes loads in this interpreter, else the reason."""
    return _resolve_real_kanban()[1]


def require_real_hermes():
    """The real ``hermes_cli.kanban_db``, or a ``pytest.skip`` naming the reason.

    For the tests that cannot mean anything without it — the deployed-waker
    fixtures (``check_orch_waker`` must report ok, which requires Hermes' own
    board enumeration) and the resolver tests whose subject is the real lib.
    """
    lib, reason = _resolve_real_kanban()
    if lib is None:
        pytest.skip(reason)
    return lib


def skip_without_real_hermes():
    """Skip the calling test when real Hermes is not importable here.

    For gated tests that use the lib only indirectly (through
    ``flightdeck``/``autodown``) and only need the verdict.
    """
    require_real_hermes()


def real_predicate_or_skip(callable_, what):
    """Call a REAL Hermes predicate; skip if this interpreter cannot run it.

    ``pytest.importorskip`` is not enough for the predicate tests: the module
    imports fine on a thin interpreter (its hermes-yaml import is function
    local), and then the *call* raises inside Hermes — which the injected-branch
    helper swallows, so the real branch silently returns a wrong answer instead
    of failing. Probing the call is what distinguishes "Hermes is here" from
    "Hermes can actually run here".

    Returns the call's result, or skips with the underlying error.
    """
    try:
        return callable_()
    except Exception as exc:  # noqa: BLE001 - the point is to name any cause
        pytest.skip(f"real {what} cannot run in this interpreter: "
                    f"{type(exc).__name__}: {exc}")
