"""Backup discipline shared by every HSCC installer/doctor writer.

Every HSCC stage that overwrites operator state (``config.yaml``,
``SOUL.md``, ``cluster-guard.py``, watchdog scripts, plugin payloads)
backs the old copy up first. Before this module each writer did it with its
own two-line ``shutil.copy(dst, f"{dst}.bak-{ts}")``, which produced the
failure mode this exists to stop:

* **Unbounded growth.** Nothing ever deleted a backup. One bootstrap (or
  ``doctor --fix``) run added one file per writer, and the pile under
  ``~/.hermes/hooks/`` alone reached 27k files / ~1.5 GB (t_9462260b audit).
* **A backup that can be empty.** ``shutil.copy`` opens the destination with
  ``O_TRUNC`` and then streams. If the copy dies mid-way — the disk is full,
  the source read fails, the process is killed — the ``.bak`` that survives is
  a truncated file, and the "safety copy" is now worthless. Measured on this
  fleet: 9 empty ``cluster-guard.py.bak-*`` files from the t_267f9d88 test
  window, i.e. the rollback point itself was zeroed.

So: copy into a temp file in the destination directory, sync it, then
``os.replace`` it into place as ``<name>.bak-<stamp>``. ``os.replace`` is
atomic within a filesystem, so the backup name only ever appears once a
complete copy exists. An interrupted run leaves at worst an orphan
``.tmp`` in the same dir (same failure shape install_triggers.py already had,
and same cleanup rule), never a truncated ``.bak``.

Retention is enforced here too: after a backup lands, the family's older
backups are pruned down to ``HSCC_BACKUP_KEEP`` (default 3). That makes the
pile self-limiting no matter who calls the writer or how often.
"""

import os
import shutil
import tempfile
import time

__all__ = [
    "BACKUP_KEEP",
    "BACKUP_INFIX",
    "backup_file",
    "backup_path_for",
    "prune_backups",
    "prune_backup_dir",
]

#: How many backups of one file family survive a write. Overridable so an
#: operator who wants a deeper history can set it without touching code.
BACKUP_KEEP = int(os.environ.get("HSCC_BACKUP_KEEP", "3"))

#: The marker every HSCC timestamped backup carries in its name
#: (``<name>.bak-<stamp>``). Kept as one constant so the pruning glob and the
#: writer can never drift apart.
BACKUP_INFIX = ".bak-"

_TMP_SUFFIX = ".tmp"


def _stamp():
    """Second-resolution timestamp used in backup names (``20261006-034500``)."""
    return time.strftime("%Y%m%d-%H%M%S")


def _tmp_name(bak_path):
    """Temp name for ``bak_path``: a hidden sibling that is NOT itself a backup.

    Leading dot + ``.tmp`` tail keeps it out of the ``*.bak-*`` glob, so a
    half-written temp can never be mistaken for a retained backup.
    """
    return os.path.join(os.path.dirname(str(bak_path)) or ".",
                        f".{os.path.basename(str(bak_path))}{_TMP_SUFFIX}")


def prune_backups(path, keep=BACKUP_KEEP):
    """Keep only the newest ``keep`` ``<path>.bak-*`` siblings; delete older.

    Sorted by mtime (newest first), which is what a rollback actually wants.
    Best-effort and never raises — pruning is hygiene, and a hygiene failure
    must never fail an install. Returns the number removed.
    """
    path = str(path)
    directory = os.path.dirname(path) or "."
    stem = os.path.basename(path)
    keep = max(0, int(keep))

    try:
        names = os.listdir(directory)
    except OSError:
        return 0

    removed = 0
    candidates = []
    for name in names:
        if not name.startswith(stem + BACKUP_INFIX):
            continue
        full = os.path.join(directory, name)
        try:
            candidates.append((os.path.getmtime(full), full))
        except OSError:
            continue  # vanished between listdir and stat
    candidates.sort(reverse=True)

    for _mtime, victim in candidates[keep:]:
        try:
            os.remove(victim)
            removed += 1
        except OSError:
            pass
    return removed


def backup_file(src, keep=BACKUP_KEEP, stamp=None, dest=None):
    """Atomically snapshot ``src`` as ``<name>.bak-<stamp>``, then prune family.

    Returns the backup path, or ``None`` when ``src`` does not exist (nothing
    to back up — callers that need to distinguish "absent" simply check for the
    file themselves first).

    The copy lands as a temp sibling and is ``os.replace``\\d into the backup
    name, so a crash mid-copy cannot leave an empty ``.bak`` pretending to be a
    rollback point. Failures propagate: a caller that is about to overwrite
    operator state *must* know the backup did not happen.
    """
    src = str(src)
    if dest is None:
        if not os.path.exists(src):
            return None
        dest = f"{src.rstrip('/') or src}{BACKUP_INFIX}{stamp or _stamp()}"
    dest = str(dest)

    if os.path.isdir(src) and not os.path.islink(src):
        # Payload backups are directory trees; a copytree to a fresh name is
        # already all-or-nothing (nothing carries the final name until it lands).
        shutil.copytree(src, dest, symlinks=False)
        prune_backups(src, keep=keep)
        return dest

    tmp = _tmp_name(dest)
    try:
        # copy2 onto a *fresh* temp path: no truncation of anything that is
        # already a real backup name.
        shutil.copy2(src, tmp)
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass  # fsync is a durability nicety here, not a correctness gate
        os.replace(tmp, dest)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

    prune_backups(src, keep=keep)
    return dest


def backup_path_for(src, stamp=None):
    """The ``.bak-<stamp>`` path ``backup_file(src)`` would use (no write)."""
    return f"{src}{BACKUP_INFIX}{stamp or _stamp()}"


def prune_backup_dir(directory, keep=BACKUP_KEEP):
    """Cap every ``<stem>`` family inside ``directory`` at ``keep`` backups.

    For piles the writers did not create: directories where backups were
    ``shutil.move``\\d (``plugins-backups/``, ``scripts/``) have no live file
    next to them, so ``prune_backups`` cannot derive the family from a target
    path. Groups by everything before the first ``.bak-`` marker.

    Returns ``(removed, families)``. Best-effort; never raises.
    """
    directory = str(directory)
    keep = max(0, int(keep))
    try:
        names = os.listdir(directory)
    except OSError:
        return 0, 0

    groups = {}
    for name in names:
        idx = name.find(BACKUP_INFIX)
        if idx <= 0:
            continue
        full = os.path.join(directory, name)
        try:
            if not os.path.isfile(full) and not os.path.isdir(full):
                continue
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        groups.setdefault(name[:idx], []).append((mtime, full))

    removed = 0
    for entries in groups.values():
        entries.sort(reverse=True)
        for _mtime, victim in entries[keep:]:
            try:
                if os.path.isdir(victim) and not os.path.islink(victim):
                    shutil.rmtree(victim, ignore_errors=True)
                else:
                    os.remove(victim)
                removed += 1
            except OSError:
                pass
    return removed, len(groups)


def _main():
    """CLI: prune the HSCC backup families under one Hermes home.

    Read-only by default (``--dry-run`` prints what would go). Prunes, per file
    family, down to ``HSCC_BACKUP_KEEP`` newest:

      <hermes>/hooks/*.bak-*                     (cluster-guard hook pile)
      <hermes>/plugins-backups/**                (payload backups, incl. legacy)
      <hermes>/scripts/*.bak-*                   (watchdog script pile)
      <hermes>/profiles/*/hooks/…                (profile-scoped homes)
      <hermes>/profiles/*/plugins-backups/**
      <hermes>/config.yaml / SOUL.md families    (root + each profile)

    Anything whose name is not a ``.bak-`` family is left alone, and whole
    operator-named directories are never entered — this is a backup-name-scope
    tool, not a general cleaner.
    """
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        description="Prune HSCC .bak-<stamp> families under one Hermes home.")
    ap.add_argument("--home", default=None,
                    help="Hermes root to prune (default: HERMES_HOME, root-resolved)")
    ap.add_argument("--keep", type=int, default=BACKUP_KEEP)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-profiles", action="store_true",
                    help="prune only the root home, skip <home>/profiles/*")
    args = ap.parse_args()

    home = args.home or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    home = os.path.expanduser(home)
    # A profile-scoped HERMES_HOME is Hermes' normal run mode; walk up to the
    # root so one invocation covers the whole tree (same rule doctor.py uses).
    if os.path.basename(os.path.dirname(home)) == "profiles":
        home = os.path.dirname(os.path.dirname(home))

    homes = [home]
    if not args.no_profiles:
        profiles = os.path.join(home, "profiles")
        try:
            for child in sorted(os.listdir(profiles)):
                pdir = os.path.join(profiles, child)
                if os.path.isdir(pdir) and not child.startswith("."):
                    homes.append(pdir)
        except OSError:
            pass

    report = []
    for h in homes:
        for sub in ("hooks", "scripts", "plugins-backups", "plugins_backups"):
            d = os.path.join(h, sub)
            if not os.path.isdir(d):
                continue
            if args.dry_run:
                n, fams = _count_only(d, args.keep)
            else:
                n, fams = prune_backup_dir(d, keep=args.keep)
            if n:
                report.append({"dir": d, "removed": n, "families": fams})
        for fname in ("config.yaml", "SOUL.md"):
            target = os.path.join(h, fname)
            n = 0 if args.dry_run else prune_backups(target, keep=args.keep)
            if n:
                report.append({"file": target, "removed": n})

    print(json.dumps({"home": home, "keep": args.keep,
                      "dry_run": args.dry_run, "pruned": report}, indent=2))
    return 0


def _count_only(directory, keep):
    """How many entries ``prune_backup_dir(directory, keep)`` would remove."""
    try:
        names = os.listdir(directory)
    except OSError:
        return 0, 0
    groups = {}
    for name in names:
        idx = name.find(BACKUP_INFIX)
        if idx > 0:
            groups.setdefault(name[:idx], []).append(name)
    return sum(max(0, len(v) - keep) for v in groups.values()), len(groups)


if __name__ == "__main__":
    raise SystemExit(_main())
