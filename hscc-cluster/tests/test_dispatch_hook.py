"""WS4 resume wiring: the kanban_task_claimed hook fires on every claim.

The upstream patch (kanban_db._fire_kanban_lifecycle_hook) fires
`kanban_task_claimed` on every task claim. HSCC's handler checks branch state
to decide if a resume note is needed. Tested against the real kanban_db with a
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
    assert "kanban_task_claimed" in plugins.VALID_HOOKS


def test_hook_fires_on_each_claim(monkeypatch):
    conn = _board()
    t = kb.create_task(conn, title="each claim", assignee="coder")
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
    # kanban_task_claimed fires on every claim (including first)
    assert any(n == "kanban_task_claimed" for n, _ in fired)


def test_hook_fires_on_each_claim(monkeypatch):
    conn = _board()
    t = kb.create_task(conn, title="redispatch", assignee="coder")
    tid = t.id if hasattr(t, "id") else t
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()

    fired = []
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook",
                        lambda name, **kw: fired.append((name, kw)) or [])

    # 1st claim → hook fires
    kb.claim_task(conn, tid)
    # simulate a failure that returns the task to ready (crash path)
    kb._record_task_failure(conn, tid, "crash", outcome="crashed", failure_limit=99)
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()
    # 2nd claim → hook fires again
    kb.claim_task(conn, tid)

    claim_fires = [kw for n, kw in fired if n == "kanban_task_claimed"]
    assert len(claim_fires) == 2
    assert claim_fires[0]["task_id"] == tid
    assert claim_fires[1]["task_id"] == tid
    assert claim_fires[1].get("run_id") > 1


def test_hook_fires_on_reclaim(monkeypatch, tmp_path):
    """Engineered crash→resume: a real task branch with committed work, a crash,
    then re-dispatch → the live hook posts a resume comment build_worker_context
    surfaces. Proves the full wiring, not just that the hook fires."""
    import subprocess
    import workflow

    # real worktree on a task branch with committed step1
    repo = tmp_path / "repo"; repo.mkdir()
    def g(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "README.md").write_text("base\n"); g("add", "."); g("commit", "-qm", "base")
    g("checkout", "-q", "-b", "task-resume")
    (repo / "src").mkdir(); (repo / "src" / "step1.py").write_text("def step1(): return 1\n")
    g("add", "."); g("commit", "-qm", "step1")

    # route claim_task's invoke_hook to the REAL handler, injecting our repo
    import hermes_cli.plugins as P
    def fake_invoke(name, **kw):
        if name == "kanban_task_claimed":
            return [workflow.on_kanban_task_claimed(board=None, repo=str(repo),
                    **{k: v for k, v in kw.items() if k not in ("board", "repo", "profile_name", "assignee")})]
        return []
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke)

    # The real on_kanban_task_claimed opens (and closes) its OWN connection via
    # kanban_db.connect. Point that at the SAME temp board — with a fresh handle
    # per call, since the hook closes what it opens — so the resume comment lands
    # on this board, not the live DB.
    _dbp = tmp_path / "kanban.db"
    kb.init_db(_dbp)
    _orig_connect = kb.connect
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda *a, **k: _orig_connect(_dbp))
    conn = _orig_connect(_dbp)
    t = kb.create_task(conn, title="step task", assignee="coder")
    tid = t.id if hasattr(t, "id") else t
    conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL,branch_name='task-resume' WHERE id=?", (tid,))
    conn.commit()
    kb.claim_task(conn, tid)                                   # run 1
    kb._record_task_failure(conn, tid, "crash", outcome="crashed", failure_limit=99)
    conn.execute("UPDATE tasks SET status='ready',claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()
    kb.claim_task(conn, tid)                                   # run 2 → hook posts comment

    comments = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id=?", (tid,)).fetchall()
    assert any(a == "hscc-resume" and "Resume" in b for a, b in comments)
    ctx = kb.build_worker_context(conn, tid)
    assert "task-resume" in ctx and "step1" in ctx             # worker will see it
