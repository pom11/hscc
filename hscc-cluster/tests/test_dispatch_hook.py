"""WS4 resume wiring: the pre_kanban_dispatch hook fires on RE-dispatch.

The core patch (kanban_db.claim_task) fires `pre_kanban_dispatch` only when a
re-claim opens run_id > 1 — so first claims are untouched and a re-dispatched
worker can be handed a resume note. Tested against the real kanban_db with a
spy on invoke_hook.

Skipped where hermes_cli isn't installed.
"""

import tempfile
from pathlib import Path

import pytest

kb = pytest.importorskip("hermes_cli.kanban_db",
                         reason="hermes_cli not installed in this env")
import hermes_cli.plugins as plugins  # noqa: E402


def _board():
    d = tempfile.mkdtemp()
    dbp = Path(d) / "kanban.db"
    kb.init_db(dbp)
    return kb.connect(dbp)


def test_hook_in_valid_hooks():
    assert "pre_kanban_dispatch" in plugins.VALID_HOOKS


def test_hook_not_fired_on_first_claim(monkeypatch):
    conn = _board()
    t = kb.create_task(conn, title="first claim", assignee="coder")
    tid = t.id if hasattr(t, "id") else t
    # task starts in triage/ready depending on flow — force ready
    fired = []
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook",
                        lambda name, **kw: fired.append((name, kw)) or [])
    # ensure it's claimable (ready)
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    # run_id == 1 → hook must NOT fire
    assert not any(n == "pre_kanban_dispatch" for n, _ in fired)


def test_hook_fires_on_redispatch(monkeypatch):
    conn = _board()
    t = kb.create_task(conn, title="redispatch", assignee="coder")
    tid = t.id if hasattr(t, "id") else t
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()

    fired = []
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook",
                        lambda name, **kw: fired.append((name, kw)) or [])

    # 1st claim (run 1) — no hook
    kb.claim_task(conn, tid)
    # simulate a failure that returns the task to ready (crash path)
    kb._record_task_failure(conn, tid, "crash", outcome="crashed", failure_limit=99)
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()
    # 2nd claim (run 2) — hook MUST fire
    kb.claim_task(conn, tid)

    dispatch_fires = [kw for n, kw in fired if n == "pre_kanban_dispatch"]
    assert len(dispatch_fires) == 1
    assert dispatch_fires[0]["task_id"] == tid
    assert dispatch_fires[0]["run_id"] > 1
