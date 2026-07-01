import subprocess
from pathlib import Path

import apply_patches


def _git(d, *args):
    return subprocess.run(["git", "-C", str(d), *args],
                          capture_output=True, text=True)


def _init_repo(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "f.txt").write_text("line1\n")
    _git(d, "add", "f.txt")
    _git(d, "commit", "-qm", "base")


def _make_patch(tmp_path) -> Path:
    """Create a one-commit patch from a scratch repo."""
    src = tmp_path / "src"
    _init_repo(src)
    (src / "f.txt").write_text("line1\nline2\n")
    _git(src, "commit", "-aqm", "add line2")
    out = tmp_path / "patches"
    out.mkdir()
    _git(src, "format-patch", "-1", "-o", str(out))
    return next(out.glob("*.patch"))


def test_list_patches_reads_real_set():
    # The repo ships real hermes patch set; sparkrun is empty (patches
    # landed upstream as 9e4513f + 37a7bdb).
    assert len(apply_patches.list_patches("hermes")) >= 1
    assert len(apply_patches.list_patches("sparkrun")) == 0


def test_check_passes_when_patch_applies(tmp_path, monkeypatch):
    patch = _make_patch(tmp_path)
    monkeypatch.setitem(apply_patches.SETS, "synthetic", patch.parent)
    target = tmp_path / "target"
    _init_repo(target)  # same base → patch applies cleanly
    res = apply_patches.apply_set("synthetic", target, check=True)
    assert res["ok"] is True
    assert res["patches"][0]["applies"] is True


def test_check_fails_when_upstream_moved(tmp_path, monkeypatch):
    patch = _make_patch(tmp_path)
    monkeypatch.setitem(apply_patches.SETS, "synthetic", patch.parent)
    target = tmp_path / "target"
    _init_repo(target)
    # Upstream advanced incompatibly: the context the patch expects is gone.
    (target / "f.txt").write_text("totally different content\n")
    _git(target, "commit", "-aqm", "upstream moved")
    res = apply_patches.apply_set("synthetic", target, check=True)
    assert res["ok"] is False
    assert res["patches"][0]["applies"] is False
    assert res["patches"][0]["error"]


def test_apply_for_real_commits_patch(tmp_path, monkeypatch):
    patch = _make_patch(tmp_path)
    monkeypatch.setitem(apply_patches.SETS, "synthetic", patch.parent)
    target = tmp_path / "target"
    _init_repo(target)
    res = apply_patches.apply_set("synthetic", target, check=False)
    assert res["ok"] is True
    assert (target / "f.txt").read_text() == "line1\nline2\n"


def test_apply_aborts_cleanly_on_conflict(tmp_path, monkeypatch):
    patch = _make_patch(tmp_path)
    monkeypatch.setitem(apply_patches.SETS, "synthetic", patch.parent)
    target = tmp_path / "target"
    _init_repo(target)
    (target / "f.txt").write_text("conflict\n")
    _git(target, "commit", "-aqm", "moved")
    res = apply_patches.apply_set("synthetic", target, check=False)
    assert res["ok"] is False
    assert res["failed_on"]
    # am must have been aborted — repo is not mid-am
    status = _git(target, "status").stdout
    assert "rebase" not in status.lower() and "am" not in status.lower().split("\n")[0]


def test_non_git_target_errors(tmp_path, monkeypatch):
    patch = _make_patch(tmp_path)
    monkeypatch.setitem(apply_patches.SETS, "synthetic", patch.parent)
    plain = tmp_path / "plain"
    plain.mkdir()
    res = apply_patches.apply_set("synthetic", plain, check=True)
    assert res["ok"] is False
    assert "not a git checkout" in res["error"]
