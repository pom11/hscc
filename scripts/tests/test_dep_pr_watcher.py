"""Tests for the dependency-bump PR watcher (cluster-side poller)."""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dep_pr_watcher as w  # noqa: E402


class _FakeKB:
    """Records create_task calls; honours idempotency_key like the real lib."""

    def __init__(self):
        self.tasks = {}      # idempotency_key -> id
        self.updates = []
        self._n = 0

    def connect_closing(self, *, board=None):
        kb = self

        class _Ctx:
            def __enter__(self_):
                return kb

            def __exit__(self_, *a):
                return False
        return _Ctx()

    def create_task(self, conn, *, title, body, assignee, created_by,
                    session_id, idempotency_key, board=None):
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


# ── liveness stamp on every pass (GAP-B-4, t_87663869) ─────────────────────
# The dep-watcher once failed 17 consecutive daily runs unnoticed
# (t_3aadd2c0): an idle run that leaves no trace is indistinguishable from a
# dead cron job. Every main() path must stamp; the stamp must never be able
# to break the watcher's real work; stdout must stay exactly as delivery-
# sensitive as before (hermes cron run history treats stdout as the record).

class _FakeCW:
    def __init__(self, ok=True):
        self.calls = []
        self.fail_calls = []
        self.ok = ok

    def stamp(self, stream, **fields):
        self.calls.append((stream, fields))
        return self.ok

    def stamp_failure(self, stream, error):
        self.fail_calls.append((stream, str(error)))
        return True


def test_idle_pass_stamps(monkeypatch):
    """The GAP-B-4 core: 'ran, nothing to do' STILL stamps — that is the only
    thing separating a healthy idle watcher from a dead one."""
    fake = _FakeCW()
    monkeypatch.setattr(w, "_stamp_module", lambda: fake)
    monkeypatch.setattr(w, "list_review_prs", lambda: [])
    with redirect_stdout(io.StringIO()):
        assert w.main() == 0
    assert fake.calls == [("dep_pr_watcher",
                           {"prs": 0, "cards": 0, "idle": True})]


def test_active_pass_stamps_with_counts(monkeypatch):
    fake = _FakeCW()
    monkeypatch.setattr(w, "_stamp_module", lambda: fake)
    monkeypatch.setattr(w, "list_review_prs", lambda: [_pr(9)])
    monkeypatch.setattr(w, "ensure_cards", lambda prs: [(9, "t_9")])
    monkeypatch.setattr(w, "_deliver", lambda body: None)
    with redirect_stdout(io.StringIO()):
        assert w.main() == 0
    assert fake.calls == [("dep_pr_watcher",
                           {"prs": 1, "cards": 1, "idle": False})]


def test_stamp_missing_is_loud_on_stderr_not_stdout(monkeypatch):
    """No stamp module -> the watcher still does its job silently on stdout,
    but the lost detector is printed to stderr (run history shows it)."""
    monkeypatch.setattr(w, "_stamp_module", lambda: None)
    monkeypatch.setattr(w, "list_review_prs", lambda: [])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert w.main() == 0
    assert out.getvalue() == ""          # idle delivery semantics unchanged
    assert "not importable" in err.getvalue()


def test_stamp_write_failure_warns(monkeypatch):
    fake = _FakeCW(ok=False)
    monkeypatch.setattr(w, "_stamp_module", lambda: fake)
    monkeypatch.setattr(w, "list_review_prs", lambda: [])
    err = io.StringIO()
    with redirect_stdout(io.StringIO()), redirect_stderr(err):
        assert w.main() == 0
    assert "stamp write failed" in err.getvalue()


def test_main_never_raises_from_the_stamp_path(monkeypatch):
    """A stamp that EXPLODES must not take the watcher down — the watcher's
    real work outranks its own liveness signal."""
    class Exploding:
        def stamp(self, *a, **k):
            raise RuntimeError("stamp blew up")
    monkeypatch.setattr(w, "_stamp_module", lambda: Exploding())
    monkeypatch.setattr(w, "list_review_prs", lambda: [])
    with redirect_stdout(io.StringIO()):
        assert w.main() == 0


def test_crash_is_stamped_as_a_failed_pass(monkeypatch):
    """run_with_failure_stamp (the __main__ contract) records 'cron fired and
    the script raised' (ok=False) — distinct from both 'never fired' (stale)
    and a healthy idle pass — and re-raises untouched."""
    fake = _FakeCW()
    monkeypatch.setattr(w, "_stamp_module", lambda: fake)

    def boom():
        raise ValueError("gh exploded")
    monkeypatch.setattr(w, "list_review_prs", boom)
    with pytest.raises(ValueError, match="gh exploded"):
        w.run_with_failure_stamp()
    assert fake.fail_calls == [("dep_pr_watcher", "gh exploded")]


def test_clean_exit_never_stamps_a_failure(monkeypatch):
    """SystemExit/return from main must not leave an ok=False stamp — a clean
    idle pass must read healthy, not 'failing'."""
    fake = _FakeCW()
    monkeypatch.setattr(w, "_stamp_module", lambda: fake)
    monkeypatch.setattr(w, "list_review_prs", lambda: [])
    assert w.run_with_failure_stamp() == 0
    assert fake.fail_calls == []


def test_stamp_module_resolves_the_sibling_file(tmp_path, monkeypatch):
    """_stamp_module must find hscc_daemon/cron_watchers.py in a bare repo
    checkout (the installed runtime covers the other homes)."""
    monkeypatch.setitem(sys.modules, "hscc_daemon", None)  # force fallback
    cw = w._stamp_module()
    assert cw is not None and cw.CHECK_NAME.startswith("cron watchers")


def test_reports_created_card(monkeypatch):
    monkeypatch.setattr(w, "list_review_prs", lambda: [_pr(7)])
    monkeypatch.setattr(w, "ensure_cards", lambda prs: [(7, "t_x")])
    out = io.StringIO()
    with redirect_stdout(out):
        w.main()
    assert "PR #7" in out.getvalue() and "t_x" in out.getvalue()


# ── sparkrun fork-branch rule in the generated card body (t_39d7c915) ──────

def _pr_named(num, title, reviewer="pom11"):
    return {"number": num, "title": title,
            "url": f"https://github.com/pom11/hscc/pull/{num}",
            "reviewRequests": [{"login": reviewer}]}


def test_card_body_carries_fork_rule_for_sparkrun():
    """The bump card is the text a bump worker reads; when sparkrun is in the
    title it MUST carry the fork-branch rule inline (the deployed runtime is
    pom11/sparkrun@feat/proxy-least-in-flight-041, and 'reinstall sparkrun' /
    'git checkout main' silently drops the proxy balancer).

    EXACT (`-041`) assertions: the bare branch name is a substring of the live
    one AND of the superseded `-040` rollback name, so a substring check would
    pass vacuously against either. Literal, not the guard constant: this is the
    tripwire that fires if FORK_BRANCH ever regresses to a superseded branch."""
    body = w._card_body(_pr_named(5, "chore(deps): runtime bump — sparkrun v0.2.40 -> v0.2.41"))
    assert "feat/proxy-least-in-flight-041" in body
    assert (body.count("feat/proxy-least-in-flight")
            == body.count("feat/proxy-least-in-flight-041"))
    assert "pom11/sparkrun" in body
    assert "checkout main" in body
    assert "rebase" in body
    assert "rev-parse --abbrev-ref HEAD" in body
    assert "fork-branch rule" in body


def test_card_body_quiet_for_hermes_only_bump():
    body = w._card_body(_pr_named(6, "chore(deps): runtime bump — hermes-agent v2026.9.1 -> v2026.9.2"))
    assert "fork-branch rule" not in body
    assert "feat/proxy-least-in-flight" not in body
    # the original 6 steps still ship
    assert "6. Report what worked" in body


def test_card_body_loader_failure_is_loud(tmp_path, monkeypatch):
    """No reachable dep_bump_guard.py -> the card says SO instead of shipping
    a rule-less sparkrun bump card."""
    real_isfile = __import__("os").path.isfile
    monkeypatch.setattr(
        __import__("os").path, "isfile",
        lambda p: False if "dep_bump_guard.py" in str(p) else real_isfile(p))
    body = w._card_body(_pr_named(7, "bump sparkrun"))
    assert "GUARD UNAVAILABLE" in body
