"""Hermetic tests for hscc-bootstrap/backup_util.py (t_9462260b).

Every test runs in tmp_path with monkeypatched env — nothing here may read or
write the operator's real ~/.hermes, and none of it shells out.
"""
import os
import stat
import time
from pathlib import Path

import pytest

import backup_util


def _touch(path, text="v\n", mtime=None):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# ── backup_file: atomicity ──────────────────────────────────────────────────

def test_backup_file_creates_named_backup(tmp_path):
    src = _touch(tmp_path / "cluster-guard.py", "# live\n")
    bak = backup_util.backup_file(src)
    assert bak is not None
    assert Path(bak).name.startswith("cluster-guard.py.bak-")
    assert Path(bak).read_text() == "# live\n"
    assert src.read_text() == "# live\n"  # source untouched


def test_backup_file_absent_source_returns_none(tmp_path):
    assert backup_util.backup_file(tmp_path / "nope.py") is None
    assert not list(tmp_path.glob("nope.py.bak-*"))


def test_backup_file_no_temp_leftover(tmp_path):
    backup_util.backup_file(_touch(tmp_path / "config.yaml", "a\n"))
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*tmp"))


def test_backup_file_failure_leaves_no_empty_bak(tmp_path, monkeypatch):
    """THE regression (t_9462260b): a copy that dies mid-way must not leave a
    zero-byte .bak masquerading as a rollback point."""
    src = _touch(tmp_path / "cluster-guard.py", "x" * 4096 + "\n")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(backup_util.shutil, "copy2", boom)
    with pytest.raises(OSError):
        backup_util.backup_file(src)

    assert not list(tmp_path.glob("cluster-guard.py.bak-*"))
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*tmp"))


def test_backup_file_partial_write_never_becomes_bak(tmp_path, monkeypatch):
    """copy2 that truncates the temp then fails: temp cleaned, no .bak."""
    src = _touch(tmp_path / "config.yaml", "y" * 2048 + "\n")

    def half(src_, dst_, *a, **k):
        with open(dst_, "wb") as fh:      # the O_TRUNC the old code exposed
            fh.write(b"partial")
        raise OSError("killed mid-copy")

    monkeypatch.setattr(backup_util.shutil, "copy2", half)
    with pytest.raises(OSError):
        backup_util.backup_file(src)
    assert not list(tmp_path.glob("config.yaml.bak-*"))
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_file_replaces_existing_bak_atomically(tmp_path):
    """A previous backup at the same name is replaced whole, never appended."""
    src = _touch(tmp_path / "config.yaml", "second\n")
    bak = backup_util.backup_path_for(src, stamp="20260101-000000")
    Path(bak).write_text("first\n")
    assert backup_util.backup_file(src, stamp="20260101-000000") == bak
    assert Path(bak).read_text() == "second\n"


def test_backup_file_preserves_mode_and_timestamps(tmp_path):
    src = _touch(tmp_path / "cluster-guard.py", "# hook\n")
    os.chmod(src, 0o755)
    old = time.time() - 4000
    os.utime(src, (old, old))
    bak = Path(backup_util.backup_file(src))
    assert bak.stat().st_mode & stat.S_IXUSR
    assert abs(bak.stat().st_mtime - old) < 2


def test_backup_file_explicit_stamp_is_deterministic(tmp_path):
    src = _touch(tmp_path / "SOUL.md", "s\n")
    bak = backup_util.backup_file(src, stamp="20261006-034500")
    assert Path(bak).name == "SOUL.md.bak-20261006-034500"


def test_backup_file_directory_tree(tmp_path):
    pkg = tmp_path / "hscc-cluster"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("pkg\n")
    (pkg / "sub" / "deep.py").write_text("deep\n")
    bak = backup_util.backup_file(pkg, stamp="s1")
    assert Path(bak).is_dir()
    assert (Path(bak) / "sub" / "deep.py").read_text() == "deep\n"


# ── prune_backups ───────────────────────────────────────────────────────────

def test_prune_keeps_newest_n(tmp_path):
    src = _touch(tmp_path / "config.yaml", "live\n")
    base = 1_700_000_000
    for i in range(7):
        _touch(tmp_path / f"config.yaml.bak-{i:04d}", f"b{i}\n",
               mtime=base + i)
    removed = backup_util.prune_backups(src, keep=3)
    assert removed == 4
    left = sorted(p.name for p in tmp_path.glob("config.yaml.bak-*"))
    assert left == ["config.yaml.bak-0004", "config.yaml.bak-0005",
                    "config.yaml.bak-0006"]
    assert src.exists()  # the live file is never a prune candidate


def test_prune_ignores_other_families(tmp_path):
    src = _touch(tmp_path / "config.yaml", "live\n")
    other = _touch(tmp_path / "SOUL.md.bak-0001", "s\n")
    _touch(tmp_path / "config.yaml.bak-0001", "a\n")
    _touch(tmp_path / "config.yaml.bak-0002", "b\n")
    backup_util.prune_backups(src, keep=1)
    assert other.exists()
    assert (tmp_path / "config.yaml.bak-0002").exists()
    assert not (tmp_path / "config.yaml.bak-0001").exists()


def test_prune_is_noop_when_under_keep(tmp_path):
    src = _touch(tmp_path / "config.yaml", "live\n")
    _touch(tmp_path / "config.yaml.bak-0001", "a\n")
    assert backup_util.prune_backups(src, keep=3) == 0
    assert (tmp_path / "config.yaml.bak-0001").exists()


def test_prune_survives_missing_dir(tmp_path):
    assert backup_util.prune_backups(tmp_path / "absent" / "config.yaml") == 0


def test_prune_tolerates_vanished_file(tmp_path, monkeypatch):
    src = _touch(tmp_path / "config.yaml", "live\n")
    for i in range(5):
        _touch(tmp_path / f"config.yaml.bak-{i}", f"b{i}\n")
    real_remove = os.remove

    def flaky(path):
        if path.endswith("config.yaml.bak-2"):
            raise OSError("busy")
        real_remove(path)

    monkeypatch.setattr(backup_util.os, "remove", flaky)
    backup_util.prune_backups(src, keep=1)          # must not raise
    assert (tmp_path / "config.yaml.bak-2").exists()


# ── backup_file does its own pruning ────────────────────────────────────────

def test_backup_file_prunes_family_after_write(tmp_path):
    src = _touch(tmp_path / "cluster-guard.py", "live\n")
    base = 1_700_000_000
    for i in range(6):
        _touch(tmp_path / f"cluster-guard.py.bak-old{i}", f"o{i}\n",
               mtime=base + i)
    bak = backup_util.backup_file(src, keep=3, stamp="9999")
    os.utime(bak, (base + 100, base + 100))         # the new bak is newest
    left = sorted(p.name for p in tmp_path.glob("cluster-guard.py.bak-*"))
    assert len(left) == 3
    assert "cluster-guard.py.bak-9999" in left


def test_backup_file_honours_env_keep(monkeypatch, tmp_path):
    monkeypatch.setenv("HSCC_BACKUP_KEEP", "1")
    import importlib
    importlib.reload(backup_util)
    try:
        assert backup_util.BACKUP_KEEP == 1
        src = _touch(tmp_path / "config.yaml", "live\n")
        base = 1_700_000_000
        for i in range(4):
            _touch(tmp_path / f"config.yaml.bak-o{i}", "o\n", mtime=base + i)
        bak = backup_util.backup_file(src, stamp="z")
        os.utime(bak, (base + 99, base + 99))
        assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 1
    finally:
        monkeypatch.delenv("HSCC_BACKUP_KEEP")
        importlib.reload(backup_util)


# ── prune_backup_dir (moved-aside piles) ────────────────────────────────────

def test_prune_backup_dir_caps_every_family(tmp_path):
    base = 1_700_000_000
    for fam in ("hscc-cluster", "hscc_daemon"):
        for i in range(6):
            d = tmp_path / f"{fam}.bak-{i:04d}"
            d.mkdir()
            (d / "__init__.py").write_text(f"{fam}{i}\n")
            os.utime(d, (base + i, base + i))
    (tmp_path / "operator-notes.txt").write_text("keep me\n")
    removed, families = backup_util.prune_backup_dir(tmp_path, keep=3)
    assert (removed, families) == (6, 2)
    assert (tmp_path / "operator-notes.txt").exists()
    for fam in ("hscc-cluster", "hscc_daemon"):
        left = sorted(p.name for p in tmp_path.glob(f"{fam}.bak-*"))
        assert len(left) == 3
        assert (tmp_path / left[-1] / "__init__.py").read_text().startswith(fam)


def test_prune_backup_dir_mixed_files_and_dirs(tmp_path):
    base = 1_700_000_000
    for i in range(5):
        _touch(tmp_path / f"requirements.txt.bak-{i:04d}", "r\n", mtime=base + i)
    removed, _ = backup_util.prune_backup_dir(tmp_path, keep=2)
    assert removed == 3
    assert len(list(tmp_path.glob("requirements.txt.bak-*"))) == 2


def test_prune_backup_dir_missing_dir(tmp_path):
    assert backup_util.prune_backup_dir(tmp_path / "nope") == (0, 0)


def test_prune_backup_dir_ignores_non_backup_names(tmp_path):
    keep_me = _touch(tmp_path / "cluster-guard.py", "live\n")
    also = _touch(tmp_path / "notes.bak", "note\n")   # no '-' infix
    removed, _ = backup_util.prune_backup_dir(tmp_path, keep=1)
    assert removed == 0
    assert keep_me.exists() and also.exists()


def test_prune_backup_dir_keep_zero(tmp_path):
    _touch(tmp_path / "a.bak-1", "x\n")
    _touch(tmp_path / "a.bak-2", "x\n")
    removed, _ = backup_util.prune_backup_dir(tmp_path, keep=0)
    assert removed == 2
    assert not list(tmp_path.glob("*.bak-*"))


# ── the pile the card measured: 27k hooks baks ──────────────────────────────

def test_27k_hook_pile_prunes_to_keep(tmp_path):
    """The audit shape: one family, thousands of stamps, must collapse to keep
    without touching the live hook file."""
    live = _touch(tmp_path / "cluster-guard.py", "# live hook\n")
    base = 1_700_000_000
    for i in range(2000):          # 2000 stands in for the measured 27,119
        _touch(tmp_path / f"cluster-guard.py.bak-2026{i:08d}", "old\n",
               mtime=base + i)
    removed = backup_util.prune_backups(live, keep=3)
    assert removed == 1997
    assert live.read_text() == "# live hook\n"
    assert len(list(tmp_path.glob("*.bak-*"))) == 3


# ── CLI ─────────────────────────────────────────────────────────────────────

def _fake_home(tmp_path):
    home = tmp_path / "hermes"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    _touch(hooks / "cluster-guard.py", "# live\n")
    base = 1_700_000_000
    for i in range(5):
        _touch(hooks / f"cluster-guard.py.bak-{i:04d}", "o\n", mtime=base + i)
    prof = home / "profiles" / "worker" / "hooks"
    prof.mkdir(parents=True)
    _touch(prof / "cluster-guard.py", "# live\n")
    for i in range(5):
        _touch(prof / f"cluster-guard.py.bak-{i:04d}", "o\n", mtime=base + i)
    bdir = home / "plugins-backups"
    bdir.mkdir()
    for i in range(5):
        _touch(bdir / f"hscc-cluster.bak-{i:04d}.txt", "o\n", mtime=base + i)
    return home


def test_cli_dry_run_removes_nothing(tmp_path, capsys):
    home = _fake_home(tmp_path)
    monkey_argv = ["backup_util.py", "--home", str(home), "--dry-run"]
    import sys
    old = sys.argv
    sys.argv = monkey_argv
    try:
        assert backup_util._main() == 0
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    assert len(list((home / "hooks").glob("*.bak-*"))) == 5


def test_cli_prunes_home_and_profiles(tmp_path, capsys):
    home = _fake_home(tmp_path)
    import sys
    old = sys.argv
    sys.argv = ["backup_util.py", "--home", str(home), "--keep", "2"]
    try:
        assert backup_util._main() == 0
    finally:
        sys.argv = old
    assert len(list((home / "hooks").glob("*.bak-*"))) == 2
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 2
    assert len(list((home / "plugins-backups").glob("*.bak-*"))) == 2
    assert (home / "hooks" / "cluster-guard.py").read_text() == "# live\n"


def test_cli_profile_scoped_home_walks_up_to_root(tmp_path, capsys, monkeypatch):
    home = _fake_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "worker"))
    import sys
    old = sys.argv
    sys.argv = ["backup_util.py", "--keep", "1"]
    try:
        assert backup_util._main() == 0
    finally:
        sys.argv = old
    assert len(list((home / "hooks").glob("*.bak-*"))) == 1
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 1


def test_cli_no_profiles_skips_profile_homes(tmp_path, capsys):
    home = _fake_home(tmp_path)
    import sys
    old = sys.argv
    sys.argv = ["backup_util.py", "--home", str(home), "--keep", "1",
                "--no-profiles"]
    try:
        assert backup_util._main() == 0
    finally:
        sys.argv = old
    assert len(list((home / "hooks").glob("*.bak-*"))) == 1
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 5
