#!/usr/bin/env python3
"""Subprocess payload for the protocol-violation bound regression test.

Drives the REAL deployed hermes-agent dispatcher reclaim path
(``kanban_db_dispatch.detect_crashed_workers``) against an isolated temp
board, simulating a worker that exits rc=0 WITHOUT a terminal kanban call,
and asserts the retry behaviour is bounded + surfaced.

Runs as its own subprocess so the supervisor-side functions ``connect`` /
``create_task`` / ``claim_task`` take their writable path: the caller strips
``HERMES_DELEGATED_CHILD_CONTEXT`` from this subprocess's environment (a real
dispatcher supervisor is not a delegate_task child). Uses a real temp board,
never the operator's live DB.

Exit code 0 + prints ``PVB_PASS`` on success; any assertion failure raises
(non-zero exit) so the parent test sees the failure.
"""
import os
import tempfile
from pathlib import Path

if not os.environ.get("PVB_STRIPPED") == "1":
    raise SystemExit("payload must run with HERMES_DELEGATED_CHILD_CONTEXT stripped")

from hermes_cli import kanban_db as kb            # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_dispatch as _kbd # noqa: E402


def _board():
    os.environ["HERMES_KANBAN_CRASH_GRACE_SECONDS"] = "0"
    d = tempfile.mkdtemp(prefix="pvb-sim-")
    dbp = Path(d) / "kanban.db"
    kb.init_db(dbp)
    return _kbc.connect(dbp)


def _dead_exit(conn, tid, pid):
    assert kb.claim_task(conn, tid) is not None, "task not claimable"
    _kbd._set_worker_pid(conn, tid, pid)
    _kbd._record_worker_exit(pid, 0)
    original_alive = kb._pid_alive
    kb._pid_alive = lambda p: False
    try:
        return _kbd.detect_crashed_workers(conn)
    finally:
        kb._pid_alive = original_alive


def _new_ready_task(conn):
    t = kb.create_task(conn, title="pvb sim", assignee="coder")
    return t.id if hasattr(t, "id") else t


def _event_payloads(conn, tid, kind):
    import json as _json
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
        (tid, kind)).fetchall()
    out = []
    for r in rows:
        p = r[0]
        out.append(_json.loads(p) if isinstance(p, str) else (p or {}))
    return out


def main():
    conn = _board()
    tid = _new_ready_task(conn)
    budget = _kbd._PROTOCOL_VIOLATION_FAILURE_LIMIT
    assert budget == 3, f"budget must map to default failure_limit 3, got {budget}"

    # Violations 1 and 2: below budget -> re-queued (ready), surfaced distinctly.
    for i, pid in enumerate((991100, 991101), start=1):
        _dead_exit(conn, tid, pid)
        tk = kb.get_task(conn, tid)
        events = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
        runs = conn.execute(
            "SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        assert tk.status == "ready", f"violation #{i} below budget -> retry, got {tk.status}"
        assert tk.consecutive_failures == 0, "below-budget violations must not tick unified counter"
        assert events[-1] == "protocol_violation", "clean rc=0 must surface as protocol_violation"
        last_run = runs[-1]
        assert last_run[0] == "crashed", "clean exit books a crashed run"
        assert "protocol violation" in (last_run[1] or "").lower(), "run error carries corrective message"

    # Violation 3: budget exhausted -> BLOCKED (gave_up), NOT re-queued.
    _dead_exit(conn, tid, 991102)
    tk = kb.get_task(conn, tid)
    assert tk.status == "blocked", "card must stop after budget, not loop"
    gave_up = _event_payloads(conn, tid, "gave_up")
    assert len(gave_up) == 1, "exactly one gave_up / auto-block event"
    assert gave_up[0].get("protocol_violations") == budget
    assert gave_up[0].get("protocol_violation_limit") == budget
    assert gave_up[0].get("failures", 0) in (0, 1)
    assert tk.consecutive_failures >= 1

    conn.close()
    print("PVB_PASS")


if __name__ == "__main__":
    main()
