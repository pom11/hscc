"""Tests for the runtime-dependency release checker (GitHub Action side)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_runtime_deps as c  # noqa: E402


def _lock(tmp_path):
    p = tmp_path / "runtime-versions.json"
    p.write_text(json.dumps({"dependencies": {
        "hermes-agent": {"repo": "NousResearch/hermes-agent", "tag": "v2026.7.20"},
        "sparkrun": {"repo": "spark-arena/sparkrun", "tag": "v0.2.40"},
    }}))
    return p


def test_no_update_when_current(tmp_path, monkeypatch):
    lock = _lock(tmp_path)
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(c, "_latest_release_tag",
                        lambda repo: {"NousResearch/hermes-agent": "v2026.7.20",
                                      "spark-arena/sparkrun": "v0.2.40"}[repo])
    assert c.main() == 0
    out = (tmp_path / "out").read_text()
    assert "updated=false" in out
    # lock untouched
    assert json.loads(lock.read_text())["dependencies"]["sparkrun"]["tag"] == "v0.2.40"


def test_detects_and_writes_bump(tmp_path, monkeypatch):
    lock = _lock(tmp_path)
    body = tmp_path / "body.md"
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("DEP_PR_BODY_FILE", str(body))
    monkeypatch.setattr(c, "_latest_release_tag",
                        lambda repo: {"NousResearch/hermes-agent": "v2026.7.20",
                                      "spark-arena/sparkrun": "v0.2.41"}[repo])
    assert c.main() == 0
    out = (tmp_path / "out").read_text()
    assert "updated=true" in out
    assert "sparkrun v0.2.40 -> v0.2.41" in out
    # lock rewritten with the new tag
    assert json.loads(lock.read_text())["dependencies"]["sparkrun"]["tag"] == "v0.2.41"
    # PR body has the checklist + release link
    text = body.read_text()
    assert "Cluster verification checklist" in text
    assert "releases/tag/v0.2.41" in text


def test_fetch_failure_exits_nonzero(tmp_path, monkeypatch):
    """When a dep cannot be fetched the script exits 1 — CI goes red."""
    lock = _lock(tmp_path)
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(c, "_latest_release_tag", lambda repo: None)
    assert c.main() == 1


def test_no_downgrade_when_latest_is_older(tmp_path, monkeypatch):
    """A backport tag newer in date but older in semver does NOT trigger a bump."""
    lock = _lock(tmp_path)
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    # latest=v0.2.40.1 (backport released after v0.2.41) is older → no bump
    monkeypatch.setattr(c, "_latest_release_tag",
                        lambda repo: {"NousResearch/hermes-agent": "v2026.7.20",
                                      "spark-arena/sparkrun": "v0.2.40.1"}[repo])
    # Lock has sparkrun at v0.2.41
    lock.write_text(json.dumps({"dependencies": {
        "hermes-agent": {"repo": "NousResearch/hermes-agent", "tag": "v2026.7.20"},
        "sparkrun": {"repo": "spark-arena/sparkrun", "tag": "v0.2.41"},
    }}))
    assert c.main() == 0
    out = (tmp_path / "out").read_text()
    assert "updated=false" in out
    # lock untouched
    assert json.loads(lock.read_text())["dependencies"]["sparkrun"]["tag"] == "v0.2.41"


# --- open-PR detection (the stale-PR bug fix) ---

def test_open_pr_number_open(tmp_path, monkeypatch):
    """An OPEN PR for the branch is returned (the edit path)."""
    monkeypatch.setattr(c.subprocess, "run", _gh_view_payload(
        {"number": 19, "state": "OPEN"}))
    assert c.find_open_pr() == 19


def test_open_pr_number_merged_not_returned(tmp_path, monkeypatch):
    """A MERGED PR is NOT returned — the old bug re-edited PR #19 forever.

    ``gh pr view`` returns a PR even after it is merged, so the fix must gate
    on state==\"OPEN\" or it will keep force-pushing + editing the already-merged
    PR instead of opening a fresh one.
    """
    monkeypatch.setattr(c.subprocess, "run", _gh_view_payload(
        {"number": 19, "state": "MERGED"}))
    assert c.find_open_pr() is None


def test_open_pr_number_closed_not_returned(monkeypatch):
    """A CLOSED (not merged) PR for the branch is also not edited."""
    monkeypatch.setattr(c.subprocess, "run", _gh_view_payload(
        {"number": 19, "state": "CLOSED"}))
    assert c.find_open_pr() is None


def test_open_pr_number_no_pr(monkeypatch):
    """No PR for the branch → None (the create path)."""
    r = _Result(1, "", "no PR")
    monkeypatch.setattr(c.subprocess, "run", lambda *a, **k: r)
    assert c.find_open_pr() is None


def test_open_pr_number_gh_failure(monkeypatch):
    """A gh failure yields None (best-effort → attempt create)."""
    class Boom:
        returncode = 1
        stdout = ""
        stderr = "boom"
    monkeypatch.setattr(c.subprocess, "run", lambda *a, **k: Boom())
    assert c.find_open_pr() is None


def test_open_pr_number_cli(capsys, monkeypatch):
    """The --find-open-pr CLI prints the OPEN PR number, nothing otherwise."""
    monkeypatch.setattr(c.subprocess, "run", _gh_view_payload(
        {"number": 19, "state": "OPEN"}))
    assert c.cli_find_open_pr() == 0
    assert capsys.readouterr().out.strip() == "19"

    monkeypatch.setattr(c.subprocess, "run", _gh_view_payload(
        {"number": 19, "state": "MERGED"}))
    assert c.cli_find_open_pr() == 0
    assert capsys.readouterr().out.strip() == ""


class _Result:
    """Minimal stand-in for subprocess.CompletedProcess (rc/stdout/stderr)."""

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_view_payload(data):
    """Build a subprocess.run stub that returns a gh pr view JSON payload."""
    def _run(*args, **kwargs):
        return _Result(0, json.dumps(data), "")
    return _run


# ── sparkrun fork-branch rule in the generated PR body (t_39d7c915) ────────

def test_sparkrun_bump_body_carries_fork_rule(tmp_path, monkeypatch):
    """A bump that touches sparkrun MUST carry the fork-branch rule: the
    deployed runtime is pom11/sparkrun@feat/proxy-least-in-flight-041 and a
    worker who `git checkout main`s silently drops the proxy balancer.

    The branch assertions are EXACT (`-041` suffix): a bare
    ``feat/proxy-least-in-flight`` substring is also satisfied by the
    superseded `-040` rollback branch name, so a rule that regressed to the
    old name would still pass a substring check."""
    lock = _lock(tmp_path)
    body = tmp_path / "body.md"
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("DEP_PR_BODY_FILE", str(body))
    monkeypatch.setattr(c, "_latest_release_tag",
                        lambda repo: {"NousResearch/hermes-agent": "v2026.7.20",
                                      "spark-arena/sparkrun": "v0.2.41"}[repo])
    assert c.main() == 0
    text = body.read_text()
    assert "feat/proxy-least-in-flight-041" in text
    # exact, not substring: every mention must carry the -041 suffix
    assert (text.count("feat/proxy-least-in-flight")
            == text.count("feat/proxy-least-in-flight-041"))
    assert "pom11/sparkrun" in text
    assert "checkout main" in text
    assert "rebase" in text
    assert "rev-parse --abbrev-ref HEAD" in text
    assert "fork-branch rule" in text


def test_hermes_only_bump_body_has_no_fork_rule(tmp_path, monkeypatch):
    """hermes-agent-only bumps must NOT carry the sparkrun rule (noise trains
    readers to skim it)."""
    lock = _lock(tmp_path)
    body = tmp_path / "body.md"
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("DEP_PR_BODY_FILE", str(body))
    monkeypatch.setattr(c, "_latest_release_tag",
                        lambda repo: {"NousResearch/hermes-agent": "v2026.9.99",
                                      "spark-arena/sparkrun": "v0.2.40"}[repo])
    assert c.main() == 0
    text = body.read_text()
    assert "fork-branch rule" not in text
    assert "feat/proxy-least-in-flight" not in text


def test_fork_rule_loader_failure_is_loud_not_silent(tmp_path, monkeypatch):
    """If the canonical guard module cannot load, the body must say so loudly
    (a silently rule-less bump PR is how the fork branch gets dropped)."""
    monkeypatch.setattr(c, "_REPO_ROOT", str(tmp_path / "nowhere"))
    lines = c._fork_rule_lines(["sparkrun"])
    assert len(lines) == 1
    assert "GUARD UNAVAILABLE" in lines[0]
