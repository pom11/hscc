"""Tests for the dependency-bump PR watcher (cluster-side poller)."""

import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dep_pr_watcher as w  # noqa: E402


class _FakeKB:
    """Records create_task calls; honours idempotency_key like the real lib."""

    def __init__(self):
        self.tasks = {}      # idempotency_key -> id
        self.updates = []
        self._n = 0

    def connect_closing(self):
        kb = self

        class _Ctx:
            def __enter__(self_):
                return kb

            def __exit__(self_, *a):
                return False
        return _Ctx()

    def create_task(self, conn, *, title, body, assignee, created_by,
                    session_id, idempotency_key):
        if idempotency_key in self.tasks:
            return self.tasks[idempotency_key]
        self._n += 1
        tid = f"t_{self._n:08d}"
        self.tasks[idempotency_key] = tid
        return tid

    def execute(self, *a):
        self.updates.append(a)

    def commit(self):
        pass


def _pr(num, reviewer="pom11"):
    return {"number": num, "title": f"bump {num}",
            "url": f"https://github.com/pom11/hscc/pull/{num}",
            "reviewRequests": [{"login": reviewer}] if reviewer else []}


def test_reviewer_filter(monkeypatch):
    """Only PRs with the required reviewer requested are returned."""
    payload = json.dumps([_pr(1, "pom11"), _pr(2, "someone-else"), _pr(3, None)])

    class R:
        returncode = 0
        stdout = payload
        stderr = ""
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(w, "REVIEWER", "pom11")
    prs = w.list_review_prs()
    assert [p["number"] for p in prs] == [1]


def test_gh_failure_is_silent(monkeypatch):
    """A gh failure yields [] (never raises)."""
    class R:
        returncode = 1
        stdout = ""
        stderr = "boom"
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: R())
    assert w.list_review_prs() == []


def test_ensure_cards_idempotent():
    kb = _FakeKB()
    prs = [_pr(42)]
    first = w.ensure_cards(prs, _kb=kb)
    second = w.ensure_cards(prs, _kb=kb)
    assert first == second == [(42, "t_00000001")]
    # workspace set to a worktree for each create
    assert any("worktree" in str(u) for u in kb.updates)


def test_silent_when_idle(monkeypatch):
    monkeypatch.setattr(w, "list_review_prs", lambda: [])
    out = io.StringIO()
    with redirect_stdout(out):
        rc = w.main()
    assert rc == 0 and out.getvalue() == ""  # empty stdout => no delivery


def test_reports_created_card(monkeypatch):
    monkeypatch.setattr(w, "list_review_prs", lambda: [_pr(7)])
    monkeypatch.setattr(w, "ensure_cards", lambda prs: [(7, "t_x")])
    out = io.StringIO()
    with redirect_stdout(out):
        w.main()
    assert "PR #7" in out.getvalue() and "t_x" in out.getvalue()
