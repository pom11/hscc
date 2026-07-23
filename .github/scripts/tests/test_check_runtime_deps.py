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


def test_fetch_failure_skips_dep(tmp_path, monkeypatch):
    """A None from the fetch skips that dep — never a spurious update."""
    lock = _lock(tmp_path)
    monkeypatch.setattr(c, "LOCK_PATH", str(lock))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(c, "_latest_release_tag", lambda repo: None)
    assert c.main() == 0
    assert "updated=false" in (tmp_path / "out").read_text()
