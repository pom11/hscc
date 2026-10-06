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
        _touch(tmp_path / f"config.yaml.bak-202601{i:02}-000000", f"b{i}\n",
               mtime=base + i)
    removed = backup_util.prune_backups(src, keep=3)
    assert removed == 4
    left = sorted(p.name for p in tmp_path.glob("config.yaml.bak-*"))
    assert left == ["config.yaml.bak-20260104-000000",
                    "config.yaml.bak-20260105-000000",
                    "config.yaml.bak-20260106-000000"]
    assert src.exists()  # the live file is never a prune candidate


def test_prune_ignores_other_families(tmp_path):
    src = _touch(tmp_path / "config.yaml", "live\n")
    other = _touch(tmp_path / "SOUL.md.bak-20260101-000000", "s\n")
    _touch(tmp_path / "config.yaml.bak-20260101-000000", "a\n")
    _touch(tmp_path / "config.yaml.bak-20260102-000000", "b\n")
    backup_util.prune_backups(src, keep=1)
    assert other.exists()
    assert (tmp_path / "config.yaml.bak-20260102-000000").exists()
    assert not (tmp_path / "config.yaml.bak-20260101-000000").exists()


def test_prune_leaves_operator_labelled_bookmark_by_default(tmp_path):
    """A re-install's retain sweep must not delete a human bookmark."""
    src = _touch(tmp_path / "config.yaml", "live\n")
    base = 1_700_000_000
    bookmark = _touch(tmp_path / "config.yaml.bak-pre-alias-migration-121850",
                      "precious\n", mtime=base + 999)  # NEWEST by mtime
    for i in range(5):
        _touch(tmp_path / f"config.yaml.bak-2026010{i}-000000", "s\n",
               mtime=base + i)
    removed = backup_util.prune_backups(src, keep=2)
    assert removed == 3                      # only the stamped ones
    assert bookmark.read_text() == "precious\n"
    assert len(list(p for p in tmp_path.glob("config.yaml.bak-2026*"))) == 2


def test_prune_ignores_unstamped_names_by_default(tmp_path):
    """ts='A' style names (what the test suites pass) are not hygiene targets."""
    src = _touch(tmp_path / "config.yaml", "live\n")
    for t in "ABCDE":
        _touch(tmp_path / f"config.yaml.bak-{t}", "x\n")
    assert backup_util.prune_backups(src, keep=2) == 0
    assert len(list(tmp_path.glob("config.yaml.bak-*"))) == 5


def test_prune_stamped_only_false_sweeps_unstamped(tmp_path):
    src = _touch(tmp_path / "config.yaml", "live\n")
    base = 1_700_000_000
    for i, t in enumerate("ABC"):
        _touch(tmp_path / f"config.yaml.bak-{t}", "x\n", mtime=base + i)
    assert backup_util.prune_backups(src, keep=1, stamped_only=False) == 2
    assert (tmp_path / "config.yaml.bak-C").exists()


def test_prune_is_noop_when_under_keep(tmp_path):
    src = _touch(tmp_path / "config.yaml", "live\n")
    _touch(tmp_path / "config.yaml.bak-20260101-000000", "a\n")
    assert backup_util.prune_backups(src, keep=3) == 0
    assert (tmp_path / "config.yaml.bak-20260101-000000").exists()


def test_prune_survives_missing_dir(tmp_path):
    assert backup_util.prune_backups(tmp_path / "absent" / "config.yaml") == 0


def test_prune_tolerates_vanished_file(tmp_path, monkeypatch):
    src = _touch(tmp_path / "config.yaml", "live\n")
    for i in range(5):
        _touch(tmp_path / f"config.yaml.bak-2026010{i}-000000", f"b{i}\n")
    real_remove = os.remove

    def flaky(path):
        if path.endswith("config.yaml.bak-20260102-000000"):
            raise OSError("busy")
        real_remove(path)

    monkeypatch.setattr(backup_util.os, "remove", flaky)
    backup_util.prune_backups(src, keep=1)          # must not raise
    assert (tmp_path / "config.yaml.bak-20260102-000000").exists()


# ── backup_file does its own pruning ────────────────────────────────────────

def test_backup_file_prunes_family_after_write(tmp_path):
    src = _touch(tmp_path / "cluster-guard.py", "live\n")
    base = 1_700_000_000
    for i in range(6):
        _touch(tmp_path / f"cluster-guard.py.bak-2026010{i}-000000", f"o{i}\n",
               mtime=base + i)
    bak = backup_util.backup_file(src, keep=3, stamp="20260201-000000")
    left = sorted(p.name for p in tmp_path.glob("cluster-guard.py.bak-*"))
    assert len(left) == 3
    assert "cluster-guard.py.bak-20260201-000000" in left


def test_backup_file_honours_env_keep(monkeypatch, tmp_path):
    monkeypatch.setenv("HSCC_BACKUP_KEEP", "1")
    import importlib
    importlib.reload(backup_util)
    try:
        assert backup_util.BACKUP_KEEP == 1
        src = _touch(tmp_path / "config.yaml", "live\n")
        base = 1_700_000_000
        for i in range(4):
            _touch(tmp_path / f"config.yaml.bak-2026010{i}-000000", "o\n",
                   mtime=base + i)
        backup_util.backup_file(src, stamp="20260201-000000")
        left = sorted(p.name for p in tmp_path.glob("config.yaml.bak-*"))
        assert left == ["config.yaml.bak-20260201-000000"]
    finally:
        monkeypatch.delenv("HSCC_BACKUP_KEEP")
        importlib.reload(backup_util)


# ── prune_backup_dir (moved-aside piles) ────────────────────────────────────

def test_prune_backup_dir_caps_every_family(tmp_path):
    base = 1_700_000_000
    for fam in ("hscc-cluster", "hscc_daemon"):
        for i in range(6):
            d = tmp_path / f"{fam}.bak-2026010{i}-000000"
            d.mkdir()
            (d / "__init__.py").write_text(f"{fam}{i}\n")
            os.utime(d, (base + i, base + i))
    (tmp_path / "operator-notes.txt").write_text("keep me\n")
    removed, families = backup_util.prune_backup_dir(tmp_path, keep=3)
    assert (removed, families) == (6, 2)
    assert (tmp_path / "operator-notes.txt").exists()
    for fam in ("hscc-cluster", "hscc_daemon"):
        left = sorted(p.name for p in tmp_path.glob(f"{fam}.bak-*"))
        assert left == [f"{fam}.bak-2026010{i}-000000" for i in (3, 4, 5)]
        assert (tmp_path / left[-1] / "__init__.py").read_text().startswith(fam)


def test_prune_backup_dir_mixed_files_and_dirs(tmp_path):
    base = 1_700_000_000
    for i in range(5):
        _touch(tmp_path / f"requirements.txt.bak-2026010{i}-000000", "r\n",
               mtime=base + i)
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
    _touch(tmp_path / "a.bak-20260101-000000", "x\n")
    _touch(tmp_path / "a.bak-20260102-000000", "x\n")
    removed, _ = backup_util.prune_backup_dir(tmp_path, keep=0)
    assert removed == 2
    assert not list(tmp_path.glob("*.bak-*"))


# ── age order is the NAME STAMP, not mtime ──────────────────────────────────

def test_fresh_backup_of_old_mtime_file_is_never_pruned_first(tmp_path):
    """copy2 preserves the SOURCE mtime. A backup of a file whose checkout is
    days old would sort older than yesterday's backup by mtime — the pruner
    would then delete the copy that just saved the current state. The name
    stamp is the writer's own word on ordering and must win."""
    src = _touch(tmp_path / "cluster-guard.py", "# current\n")
    old = time.time() - 40 * 86400
    os.utime(src, (old, old))

    # yesterday's backup, with a NEW mtime (a copy/move refreshed it)
    _touch(tmp_path / "cluster-guard.py.bak-20261005-120000", "# yesterday\n")
    os.utime(tmp_path / "cluster-guard.py.bak-20261005-120000",
             (time.time(), time.time()))

    bak = backup_util.backup_file(src, keep=1, stamp="20261006-120000")
    assert Path(bak).read_text() == "# current\n"          # newest kept
    assert (tmp_path / "cluster-guard.py.bak-20261005-120000").exists() is False


def test_select_victims_prefers_stamp_over_mtime(tmp_path):
    a = _touch(tmp_path / "c.bak-20260101-000000", "a\n", mtime=1_000_000_000)
    b = _touch(tmp_path / "c.bak-20260102-000000", "b\n", mtime=946_684_800)
    victims = backup_util.select_victims([str(a), str(b)], keep=1)
    assert [Path(v).name for v in victims] == ["c.bak-20260101-000000"]


def test_select_victims_negative_keep_keeps_nothing(tmp_path):
    a = _touch(tmp_path / "c.bak-20260101-000000", "a\n")
    assert len(backup_util.select_victims([str(a)], keep=-5)) == 1


# ── max_age_s: the sweep's 7-day floor on top of the keep window ────────────

def test_age_floor_spares_young_backups_beyond_keep(tmp_path):
    """Card rule: prune stale baks older than 7 days, keep newest N as safety.
    Beyond the keep window, a YOUNG backup must survive the sweep."""
    now = 1_760_000_000.0
    day = 86400.0
    members = []
    for d in range(1, 13):                      # 12 backups, 1..12 days old
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - d * day))
        members.append(_touch(tmp_path / f"c.bak-{stamp}", "x\n"))
    victims = backup_util.select_victims(members, keep=3,
                                         max_age_s=7 * day, now=now)
    # newest 3 spared by the keep window; of the rest only the >7d ones go
    assert len(victims) == 12 - 3 - 4           # days 4..7 (4 files) also spared
    ages = sorted(now - backup_util._backup_epoch(v) for v in victims)
    assert all(a > 7 * day for a in ages)


def test_writers_have_no_age_floor_hard_keep_n(tmp_path):
    """backup_file must bound the pile HARD: 20 fresh same-day writes leave
    exactly keep-N even though none is 7 days old."""
    src = _touch(tmp_path / "cluster-guard.py", "live\n")
    for i in range(20):
        backup_util.backup_file(src, keep=3,
                                stamp=f"20261006-{10 + i // 60:02d}{i % 60:02d}00")
    assert len(list(tmp_path.glob("cluster-guard.py.bak-*"))) == 3


def test_backup_epoch_prefers_stamp_over_mtime(tmp_path):
    # stamp says January, mtime says "just now" — the stamp is write time
    p = _touch(tmp_path / "c.bak-20260101-000000", "x\n", mtime=time.time())
    epoch = backup_util._backup_epoch(p)
    expected = time.mktime(time.strptime("20260101-000000", "%Y%m%d-%H%M%S"))
    assert abs(epoch - expected) < 2


def test_backup_epoch_falls_back_to_mtime_for_labels(tmp_path):
    p = _touch(tmp_path / "c.bak-bookmark", "x\n", mtime=1_234_567_890)
    assert backup_util._backup_epoch(p) == 1_234_567_890


def test_backup_epoch_bad_stamp_falls_back_not_crashes(tmp_path):
    p = _touch(tmp_path / "c.bak-20261399-999999", "x\n", mtime=999.0)  # impossible
    assert backup_util._backup_epoch(p) == 999.0


def test_unstamped_names_order_by_mtime(tmp_path):
    old = _touch(tmp_path / "c.bak-label-a", "a\n", mtime=1_000_000_000)
    new = _touch(tmp_path / "c.bak-label-b", "b\n", mtime=2_000_000_000)
    victims = backup_util.select_victims([str(old), str(new)], keep=1)
    assert [Path(v).name for v in victims] == ["c.bak-label-a"]


def test_stamped_outranks_unstamped_in_same_family(tmp_path):
    """Mixed family (a human bookmark + machine stamps): the machine stamp
    sorts above an unstamed name so a stale bookmark can't evict a real
    backup — and vice versa the bookmark is never treated as 'newest'."""
    stamp = _touch(tmp_path / "c.bak-20260101-000000", "s\n",
                   mtime=1_000_000_000)
    label = _touch(tmp_path / "c.bak-bookmark", "l\n", mtime=2_000_000_000)
    victims = backup_util.select_victims([str(stamp), str(label)], keep=1)
    assert [Path(v).name for v in victims] == ["c.bak-bookmark"]


# ── prune_backup_dir: labelled bookmarks ────────────────────────────────────

def test_prune_backup_dir_stamped_only_spares_labels(tmp_path):
    base = 1_700_000_000
    for i in range(5):
        _touch(tmp_path / f"c.bak-2026010{i}-00000{i}", "s\n", mtime=base + i)
    bookmark = _touch(tmp_path / "c.bak-pre-panic-fix-20260617", "keep\n",
                      mtime=base - 100)
    removed, fams = backup_util.prune_backup_dir(tmp_path, keep=2,
                                                stamped_only=True)
    assert removed == 3
    assert bookmark.exists()
    assert len(list(p for p in tmp_path.glob("c.bak-*")
                    if backup_util._STAMP_RE.search(p.name))) == 2


def test_prune_backup_dir_without_stamped_only_removes_labels(tmp_path):
    base = 1_700_000_000
    for i in range(3):
        _touch(tmp_path / f"c.bak-2026010{i}-00000{i}", "s\n", mtime=base + i)
    bookmark = _touch(tmp_path / "c.bak-pre-panic-fix", "x\n", mtime=base - 100)
    removed, _ = backup_util.prune_backup_dir(tmp_path, keep=2,
                                             stamped_only=False)
    # 4 members, keep 2 newest (stamped rank above the unstamped bookmark)
    assert removed == 2
    assert not bookmark.exists()
    assert len(list(tmp_path.glob("c.bak-*"))) == 2


def test_prune_backup_dir_dangling_symlink_is_skipped(tmp_path):
    (tmp_path / "c.bak-20260101-000000").symlink_to(tmp_path / "nowhere")
    _touch(tmp_path / "c.bak-20260102-000000", "ok\n")
    removed, _ = backup_util.prune_backup_dir(tmp_path, keep=1)
    assert removed == 0                      # dangling entry is not counted
    assert (tmp_path / "c.bak-20260102-000000").exists()


# ── atomic_copy (fixed-name backups) ────────────────────────────────────────

def test_atomic_copy_never_leaves_empty_dest_on_failure(tmp_path, monkeypatch):
    src = _touch(tmp_path / "triggers.json", '{"rules": []}\n')
    dest = tmp_path / "triggers.json.bak"
    dest.write_text("PREVIOUS REAL BACKUP\n")   # a fixed-name .bak already there

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(backup_util.shutil, "copy2", boom)
    with pytest.raises(OSError):
        backup_util.atomic_copy(src, dest)
    # the existing backup survives intact — no truncation, no empty file
    assert dest.read_text() == "PREVIOUS REAL BACKUP\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_copy_replaces_fixed_name_atomically(tmp_path):
    src = _touch(tmp_path / "triggers.json", '{"rules": [1]}\n')
    dest = tmp_path / "triggers.json.bak"
    dest.write_text("OLD\n")
    assert backup_util.atomic_copy(src, dest) == str(dest)
    assert dest.read_text() == '{"rules": [1]}\n'
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_copy_preserves_mode(tmp_path):
    src = _touch(tmp_path / "cluster-guard.py", "# hook\n")
    os.chmod(src, 0o700)
    dest = tmp_path / "cluster-guard.py.bak"
    backup_util.atomic_copy(src, dest)
    assert dest.stat().st_mode & 0o777 == 0o700


# ── the pile the card measured: 27k hooks baks ──────────────────────────────

def test_27k_hook_pile_prunes_to_keep(tmp_path):
    """The audit shape: one family, thousands of stamps, must collapse to keep
    without touching the live hook file."""
    live = _touch(tmp_path / "cluster-guard.py", "# live hook\n")
    base = 1_700_000_000
    for i in range(2000):          # 2000 stands in for the measured 27,119
        _touch(tmp_path / f"cluster-guard.py.bak-20260101-{i:06d}", "old\n",
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
        _touch(hooks / f"cluster-guard.py.bak-2026010{i}-00000{i}", "o\n",
               mtime=base + i)
    prof = home / "profiles" / "worker" / "hooks"
    prof.mkdir(parents=True)
    _touch(prof / "cluster-guard.py", "# live\n")
    for i in range(5):
        _touch(prof / f"cluster-guard.py.bak-2026010{i}-00000{i}", "o\n",
               mtime=base + i)
    bdir = home / "plugins-backups"
    bdir.mkdir()
    for i in range(5):
        _touch(bdir / f"hscc-cluster.bak-2026010{i}-00000{i}.txt", "o\n",
               mtime=base + i)
    # operator-labelled bookmark: must SURVIVE a default sweep
    _touch(bdir / "hscc-cluster.bak-pre-alias-migration-121850.txt", "keep\n",
           mtime=base - 100)
    # root config family incl. one labelled bookmark
    _touch(home / "config.yaml", "live\n")
    for i in range(5):
        _touch(home / f"config.yaml.bak-2026010{i}-10000{i}", "c\n",
               mtime=base + i)
    _touch(home / "config.yaml.bak-pre-panic-fix", "keep\n", mtime=base - 50)
    return home


def test_cli_bare_invocation_deletes_nothing(tmp_path, capsys):
    """THE safety pin (t_9462260b review): a bare `backup_util.py --home ...`
    must NOT prune. Deleting requires --apply, or the tool is one typo away
    from destroying the operator's whole rollback history."""
    home = _fake_home(tmp_path)
    import sys
    old = sys.argv
    sys.argv = ["backup_util.py", "--home", str(home), "--keep", "1"]
    try:
        assert backup_util._main() == 0
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    assert len(list((home / "hooks").glob("*.bak-*"))) == 5
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 5
    assert len(list((home / "plugins-backups").glob("*.bak-*"))) == 6
    assert (home / "config.yaml").exists()


def test_cli_dry_run_flag_is_accepted_and_stays_read_only(tmp_path, capsys):
    home = _fake_home(tmp_path)
    import sys
    old = sys.argv
    sys.argv = ["backup_util.py", "--home", str(home), "--dry-run"]
    try:
        assert backup_util._main() == 0
    finally:
        sys.argv = old
    assert '"dry_run": true' in capsys.readouterr().out
    assert len(list((home / "hooks").glob("*.bak-*"))) == 5


def test_cli_dry_run_removes_nothing(tmp_path, capsys):
    home = _fake_home(tmp_path)
    assert backup_util._main(["--home", str(home), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out
    assert len(list((home / "hooks").glob("*.bak-*"))) == 5


def test_cli_prunes_home_and_profiles(tmp_path, capsys):
    home = _fake_home(tmp_path)
    assert backup_util._main(
        ["--home", str(home), "--keep", "2", "--apply"]) == 0
    assert len(list((home / "hooks").glob("*.bak-*"))) == 2
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 2
    # plugins-backups: 5 stamped collapse to 2, the labelled bookmark survives
    stamped = [p for p in (home / "plugins-backups").glob("*.bak-*")
               if backup_util._STAMP_RE.search(p.name)]
    assert len(stamped) == 2
    assert (home / "plugins-backups"
            / "hscc-cluster.bak-pre-alias-migration-121850.txt").exists()
    # root config family: 5 stamped -> 2, labelled bookmark survives, live kept
    cfg_stamped = [p for p in home.glob("config.yaml.bak-*")
                   if backup_util._STAMP_RE.search(p.name)]
    assert len(cfg_stamped) == 2
    assert (home / "config.yaml.bak-pre-panic-fix").exists()
    assert (home / "config.yaml").read_text() == "live\n"
    assert (home / "hooks" / "cluster-guard.py").read_text() == "# live\n"


def test_cli_include_labeled_prunes_bookmarks(tmp_path, capsys):
    home = _fake_home(tmp_path)
    assert backup_util._main(["--home", str(home), "--keep", "0",
                              "--include-labeled", "--apply"]) == 0
    assert not list((home / "plugins-backups").glob("*.bak-*"))
    assert not list(home.glob("config.yaml.bak-*"))
    assert not list((home / "hooks").glob("*.bak-*"))
    # live files untouched
    assert (home / "config.yaml").exists()
    assert (home / "hooks" / "cluster-guard.py").exists()


def test_cli_profile_scoped_home_walks_up_to_root(tmp_path, capsys, monkeypatch):
    home = _fake_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "worker"))
    assert backup_util._main(["--keep", "1", "--apply"]) == 0
    assert len(list((home / "hooks").glob("*.bak-*"))) == 1
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 1


def test_cli_no_profiles_skips_profile_homes(tmp_path, capsys):
    home = _fake_home(tmp_path)
    assert backup_util._main(["--home", str(home), "--keep", "1",
                              "--no-profiles", "--apply"]) == 0
    assert len(list((home / "hooks").glob("*.bak-*"))) == 1
    assert len(list((home / "profiles" / "worker" / "hooks").glob("*.bak-*"))) == 5
