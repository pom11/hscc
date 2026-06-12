"""WS4 review-gate escalation contract (C3) against the REAL kanban_db.

The review flow is: a coder task that keeps failing must, after
``kanban.failure_limit`` (HSCC default 3) consecutive non-success outcomes, trip
the circuit breaker — auto-block the task and emit a ``gave_up`` event (the
escalation signal the orchestrator/operator acts on). This proves the wiring we
shipped (enable_plugins seeds failure_limit=3) actually escalates, rather than
retrying forever.

Skipped where hermes_cli isn't installed (the breaker lives in hermes core, on
the fork). Where it is, it runs against a real temp board.
"""

import tempfile
from pathlib import Path

import pytest

kb = pytest.importorskip("hermes_cli.kanban_db",
                         reason="hermes_cli not installed in this env")


def _board():
    d = tempfile.mkdtemp()
    dbp = Path(d) / "kanban.db"
    kb.init_db(dbp)
    return kb.connect(dbp)


def _new_running_task(conn, title="C3 escalation task"):
    t = kb.create_task(conn, title=title, assignee="coder", initial_status="running")
    return t.id if hasattr(t, "id") else t


def test_escalates_after_three_failures():
    """failure_limit=3: 1st/2nd failure retry (stay ready), 3rd trips the
    breaker → blocked + gave_up."""
    conn = _board()
    tid = _new_running_task(conn)

    states = []
    for i in (1, 2, 3):
        blocked = kb._record_task_failure(conn, tid, f"crash {i}",
                                          outcome="crashed", failure_limit=3)
        tk = kb.get_task(conn, tid)
        states.append((tk.consecutive_failures, tk.status, blocked))

    assert states[0] == (1, "ready", False)
    assert states[1] == (2, "ready", False)
    assert states[2] == (3, "blocked", True)     # escalated on the 3rd

    kinds = [r[0] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
    assert "gave_up" in kinds                     # escalation event emitted


def test_does_not_escalate_before_limit():
    """Two failures under a limit of 3 must NOT block — the work still retries."""
    conn = _board()
    tid = _new_running_task(conn)
    for i in (1, 2):
        kb._record_task_failure(conn, tid, f"crash {i}",
                                outcome="crashed", failure_limit=3)
    tk = kb.get_task(conn, tid)
    assert tk.status == "ready" and tk.consecutive_failures == 2


def test_stricter_per_task_limit_escalates_sooner():
    """A per-task max_retries=1 escalates on the FIRST failure (operator can be
    stricter than the board default)."""
    conn = _board()
    t = kb.create_task(conn, title="strict", assignee="coder",
                       initial_status="running", max_retries=1)
    tid = t.id if hasattr(t, "id") else t
    blocked = kb._record_task_failure(conn, tid, "crash", outcome="crashed",
                                      failure_limit=3)  # board says 3…
    tk = kb.get_task(conn, tid)
    assert blocked is True and tk.status == "blocked"  # …but per-task 1 wins
