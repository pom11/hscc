"""Tests for ensure_review_feature — idempotent, safe re-apply of the kanban
review feature into the Hermes runtime checkout."""

import os

import ensure_review_feature as erf


def _make_runtime(tmp_path, *, marker=False, git=True):
    """Build a fake hermes runtime dir with tools/kanban_tools.py."""
    hermes = tmp_path / "hermes-agent"
    (hermes / "tools").mkdir(parents=True)
    body = "def _handle_submit_review(): pass\n" if marker else "def x(): pass\n"
    if marker:
        body += 'name="kanban_submit_review"\n'
    (hermes / "tools" / "kanban_tools.py").write_text(body)
    if git:
        (hermes / ".git").mkdir()
    return hermes


def test_already_present_is_noop(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=True)
    calls = []
    monkeypatch.setattr(erf, "_git", lambda *a, **k: calls.append(a) or (True, "", ""))
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res == {"status": "already_present", "ok": True}
    assert calls == []  # never touched git


def test_missing_runtime_skips(tmp_path):
    res = erf.ensure_review_feature(hermes_dir=str(tmp_path / "nope"))
    assert res["status"] == "skipped_missing" and res["ok"] is True


def test_not_git_reports(tmp_path):
    hermes = _make_runtime(tmp_path, marker=False, git=False)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "not_git" and res["ok"] is False


def test_dirty_repo_refuses(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)
    # status --porcelain returns uncommitted changes
    monkeypatch.setattr(erf, "_git",
                        lambda args, cwd, **k: (True, " M tools/kanban_tools.py", ""))
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "dirty" and res["ok"] is False


def test_applies_when_absent_and_clean(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)
    seq = []

    def fake_git(args, cwd, **k):
        seq.append(args[0])
        if args[0] == "status":
            return True, "", ""            # clean
        if args[0] == "fetch":
            return True, "", ""
        if args[0] == "rev-parse":
            return True, "abc123", ""
        if args[0] == "cherry-pick":
            return True, "", ""
        return True, "", ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res == {"status": "applied", "ok": True}
    assert "cherry-pick" in seq and "fetch" in seq


def test_conflict_aborts_cleanly(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)
    seq = []

    def fake_git(args, cwd, **k):
        seq.append(tuple(args))
        if args[0] == "status":
            return True, "", ""
        if args[0] == "fetch":
            return True, "", ""
        if args[0] == "rev-parse":
            return True, "abc123", ""
        if args[:2] == ["cherry-pick", "--abort"]:
            return True, "", ""
        if args[0] == "cherry-pick":
            return False, "", "CONFLICT (content)"
        return True, "", ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "conflict" and res["ok"] is False
    # must have aborted to leave the runtime clean
    assert ("cherry-pick", "--abort") in seq


def test_fetch_failure_reports(tmp_path, monkeypatch):
    hermes = _make_runtime(tmp_path, marker=False)

    def fake_git(args, cwd, **k):
        if args[0] == "status":
            return True, "", ""
        if args[0] == "fetch":
            return False, "", "could not read from remote"
        return True, "", ""

    monkeypatch.setattr(erf, "_git", fake_git)
    res = erf.ensure_review_feature(hermes_dir=str(hermes))
    assert res["status"] == "fetch_failed" and res["ok"] is False
