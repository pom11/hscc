"""WS4 review-gate escalation contract (C3) against the REAL kanban_db.

The review flow is: a coder task that keeps failing must, after
``kanban.failure_limit`` (HSCC default 3) consecutive non-success outcomes, trip
the circuit breaker — auto-block the task and emit a ``gave_up`` event (the
escalation signal the orchestrator/operator acts on). This proves the wiring we
shipped (enable_plugins seeds failure_limit=3) actually escalates, rather than
retrying forever.

The breaker lives in hermes core, on the fork, so every case asks
``hermes_gap.require_hermes_kanban_board()`` first and then runs against a real
temp board (t_b884497b). This file used to answer that question with a
module-level ``pytest.importorskip("hermes_cli…")``, and both halves of that shape
are why:

  * it decided at COLLECTION, before anything in the leg could change the answer —
    and on this leg the answer once did change: ``workflow._active_profile_name``
    put ``~/.hermes/hermes-agent`` on ``sys.path`` the first time it ran and left it
    there (production's hand-rolled import fence, until t_847ab03b / PR #364 made
    the fence restore it — ``test_hermes_fence_sys_path.py`` pins that), so
    everything after the first lifecycle case saw a different interpreter state
    than the one collection had asked about. The rule outlived the incident: a
    collection-time answer stays wrong whenever the tree reaches ``sys.path`` later
    than collection (t_0fd2c149, recorded in ``hermes_gap.py``); and
  * it reported a three-case module as ONE skipped item, so ``-rs`` read
    ``[1] test_review_escalation.py:19: hermes_cli not installed in this env`` —
    a capability, no module, no import error, three cases indistinguishable from
    one.
"""

import tempfile
from pathlib import Path

import hermes_gap

# ``connect`` lives in ``hermes_cli.kanban_db_connect`` and ``_record_task_failure``
# in ``hermes_cli.kanban_db_dispatch``; the old ``kanban_db`` paths are
# removal-scheduled compat shims (or removed, in the case of
# ``_record_task_failure``) and must not be used here. Imported per call rather
# than at module scope — a module-scope import is the collection-time event above.


def _hermes():
    """``(kanban_db, kanban_db_connect, kanban_db_dispatch)``, imported now."""
    import hermes_cli.kanban_db as kb
    from hermes_cli import kanban_db_connect as _kbc
    from hermes_cli import kanban_db_dispatch as _kbd
    return kb, _kbc, _kbd


def _board():
    kb, _kbc, _ = _hermes()
    d = tempfile.mkdtemp()
    dbp = Path(d) / "kanban.db"
    kb.init_db(dbp)
    return _kbc.connect(dbp)


def _new_running_task(conn, title="C3 escalation task"):
    kb, _, _ = _hermes()
    t = kb.create_task(conn, title=title, assignee="coder", initial_status="running")
    return t.id if hasattr(t, "id") else t


def test_escalates_after_three_failures():
    """failure_limit=3: 1st/2nd failure retry (stay ready), 3rd trips the
    breaker → blocked + gave_up."""
    hermes_gap.require_hermes_kanban_board()
    kb, _, _kbd = _hermes()
    conn = _board()
    tid = _new_running_task(conn)

    states = []
    for i in (1, 2, 3):
        blocked = _kbd._record_task_failure(conn, tid, f"crash {i}",
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
    hermes_gap.require_hermes_kanban_board()
    kb, _, _kbd = _hermes()
    conn = _board()
    tid = _new_running_task(conn)
    for i in (1, 2):
        _kbd._record_task_failure(conn, tid, f"crash {i}",
                                  outcome="crashed", failure_limit=3)
    tk = kb.get_task(conn, tid)
    assert tk.status == "ready" and tk.consecutive_failures == 2


def test_stricter_per_task_limit_escalates_sooner():
    """A per-task max_retries=1 escalates on the FIRST failure (operator can be
    stricter than the board default)."""
    hermes_gap.require_hermes_kanban_board()
    kb, _, _kbd = _hermes()
    conn = _board()
    t = kb.create_task(conn, title="strict", assignee="coder",
                       initial_status="running", max_retries=1)
    tid = t.id if hasattr(t, "id") else t
    blocked = _kbd._record_task_failure(conn, tid, "crash", outcome="crashed",
                                        failure_limit=3)  # board says 3…
    tk = kb.get_task(conn, tid)
    assert blocked is True and tk.status == "blocked"  # …but per-task 1 wins
