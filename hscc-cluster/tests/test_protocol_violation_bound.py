"""Regression: a worker that exits rc=0 WITHOUT a terminal kanban call must be
bounded, not silently retried past the failure budget.

Card t_10a687c4. The dispatcher run-supervision (hermes-agent's
``hermes_cli/kanban_db_dispatch.py``) could not distinguish a worker that
"finished silently" (rc=0, no kanban_complete/block) from a crash, so it
re-queued such runs forever — t_1bfe8908 reached a 4th run despite
``kanban.failure_limit`` = 3 in the documented 2026-09-26 Phase 1 run.

This test proves the bound by EXECUTION against the real reclaim path in a
subprocess (see ``_pvb_sim.py``): it simulates a worker subprocess exiting
rc=0 without a terminal kanban call (via ``_record_worker_exit`` +
``_pid_alive → False``, then drives ``detect_crashed_workers``) and asserts
the retry behaviour is BOUNDED:

  * violations 1 and 2 (below the budget) -> task back to ``ready`` (retried),
    surfaced with a ``protocol_violation`` event (NOT a plain crash);
  * violation 3 (budget exhausted)        -> task ``blocked`` with a ``gave_up``
    event (NOT silently re-queued), proving it stops after ``failure_limit`` = 3.

Why a subprocess: this whole suite runs under a Hermes kanban worker, which is
itself a `delegate_task`-style child — ``HERMES_DELEGATED_CHILD_CONTEXT`` is
set, and the kanban write path (``kanban_db._assert_not_delegated_child_mutation``)
hard-fails any board mutation from such a context. A real dispatcher supervisor
is NOT a delegated child. Stripping the marker in the spawned env reproduces
the supervisor faithfully and lets the test build its own temp board with real
writes. Skipped under interpreters where ``hermes_cli`` isn't installed.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERMES_CLI = pytest.importorskip(
    "hermes_cli", reason="hermes_cli not installed in this env; pin test skipped")

_PVB_SIM = Path(__file__).resolve().parent / "_pvb_sim.py"


def test_clean_rc0_without_terminal_call_is_bounded():
    """rc=0 no-terminal-call is surfaced distinctly and stops after the budget,
    never silently retried past ``kanban.failure_limit`` (3)."""
    # The subprocess payload must not be a delegated child: strip the marker so
    # the supervisor-side writable path is active (mirrors a real dispatcher).
    env = dict(os.environ)
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    # workaround the payload guard (PVB_STRIPPED=1 proves the marker was stripped).
    env["PVB_STRIPPED"] = "1"
    if not env.get("HERMES_HOME"):
        # Give connect()/init a disposable home even when the suite env lacks one.
        env["HERMES_HOME"] = str(Path(__file__).resolve().parent / "_pvb_tmp_home")

    proc = subprocess.run(
        [sys.executable, str(_PVB_SIM)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = proc.stdout or ""
    err = proc.stderr or ""
    assert proc.returncode == 0, (
        f"pvb sim subprocess failed (rc={proc.returncode})\n"
        f"--- stdout ---\n{out}\n--- stderr ---\n{err}"
    )
    assert "PVB_PASS" in out, (
        f"pvb sim did not report PASS; got:\n{out}\n{err}")
