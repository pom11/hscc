"""Backup discipline shared by every HSCC installer/doctor writer.

Every HSCC stage that overwrites operator state (``config.yaml``,
``SOUL.md``, ``cluster-guard.py``, watchdog scripts, plugin payloads)
backs the old copy up first. Before this module each writer did it with its
own two-line ``shutil.copy(dst, f"{dst}.bak-{ts}")``, which produced the
failure modes this exists to stop:

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
complete copy exists. An interrupted run leaves at worst an orphan ``.tmp``
in the same dir (same failure shape install_triggers.py already had, and same
cleanup rule), never a truncated ``.bak``.

Retention is enforced here too: after a backup lands, the family's older
backups are pruned down to ``HSCC_BACKUP_KEEP`` (default 3), which makes the
pile self-limiting no matter who calls the writer or how often.

Age order is the *name stamp* first (``.bak-YYYYMMDD-HHMMSS``), mtime only as
fallback. Writers use ``copy2``, which preserves the SOURCE mtime, so sorting
by mtime alone can rank a freshly written backup OLDER than one written days
ago and delete the newest copy first. The name stamp is the writer's own word
on ordering and survives copies and moves.
"""

import os
import re
import shutil
import time

__all__ = [
    "BACKUP_KEEP",
    "BACKUP_INFIX",
    "atomic_copy",
    "backup_file",
    "backup_path_for",
    "prune_backups",
    "prune_backup_dir",
    "select_victims",
    "sweep_home",
]

#: How many backups of one file family survive a write. Overridable so an
#: operator who wants a deeper history can set it without touching code.
BACKUP_KEEP = int(os.environ.get("HSCC_BACKUP_KEEP", "3"))

#: The marker every HSCC timestamped backup carries in its name
#: (``<name>.bak-<stamp>``). One constant so the pruning glob and the writer
#: can never drift apart.
BACKUP_INFIX = ".bak-"

#: The machine stamp the writers put after BACKUP_INFIX. Names that match are
#: machine-made; names with ``.bak-`` but no stamp are operator-labelled
#: bookmarks (``config.yaml.bak-pre-alias-migration-121850``) and a hygiene
#: sweep must not touch them unless explicitly told to.
_STAMP_RE = re.compile(r"\.bak-(\d{8}-\d{6})")

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


def _sort_key(path):
    """Newness for one backup path: parsed name stamp first, mtime fallback.

    Tuples sort descending (newest first). A stamped name always outranks an
    unstamped one in the same family; unstamped names order by mtime.
    """
    m = _STAMP_RE.search(os.path.basename(str(path)))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (1, m.group(1), mtime) if m else (0, "", mtime)


def select_victims(paths, keep):
    """Which members of ONE family fall outside the newest ``keep``.

    Pure over resolved paths; ordering is ``_sort_key``'s. ``keep`` is clamped
    at 0 — a negative slice from the end would silently keep everything.
    """
    keep = max(0, int(keep))
    return sorted(paths, key=_sort_key, reverse=True)[keep:]


def _family_members(directory, stem):
    """Paths in ``directory`` named ``<stem>.bak-*``."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    prefix = stem + BACKUP_INFIX
    return [os.path.join(directory, n) for n in names if n.startswith(prefix)]


def prune_backups(path, keep=BACKUP_KEEP, stamped_only=True):
    """Keep only the newest ``keep`` ``<path>.bak-*`` siblings; delete older.

    Best-effort and never raises — pruning is hygiene, and a hygiene failure
    must never fail an install. Returns the number removed. The live file
    itself is never a candidate (only ``<name>.bak-*`` names match), and by
    default only *machine-stamped* names are candidates: an operator-labelled
    bookmark (``config.yaml.bak-pre-alias-migration-121850``) is a deliberate
    act and must not be collateral of a re-install (pass
    ``stamped_only=False`` to sweep those too).
    """
    path = str(path)
    directory = os.path.dirname(path) or "."
    stem = os.path.basename(path)
    members = _family_members(directory, stem)
    if stamped_only:
        members = [m for m in members
                   if _STAMP_RE.search(os.path.basename(m))]
    removed = 0
    for victim in select_victims(members, keep):
        try:
            os.remove(victim)
            removed += 1
        except OSError:
            pass
    return removed


def atomic_copy(src, dest):
    """Copy ``src`` onto ``dest`` so ``dest`` never exists half-written.

    Streams into a hidden temp sibling, fsyncs, then ``os.replace``\\s it into
    ``dest``. The failure this removes: ``shutil.copy`` opens its destination
    ``O_TRUNC``, so an interrupted copy leaves a zero-byte file at a name that
    downstream tooling (and a human) read as a real backup. Works for any dest
    name — timestamped backups AND fixed names like ``triggers.json.bak``.

    Returns ``dest``. Raises on copy failure (after cleaning up the temp) —
    callers about to overwrite state must know the backup did not happen.
    """
    src = str(src)
    dest = str(dest)
    tmp = _tmp_name(dest)
    try:
        # copy2 onto a *fresh* temp path — never onto an existing backup name.
        shutil.copy2(src, tmp)
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass  # durability nicety here, not a correctness gate
        os.replace(tmp, dest)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dest


def backup_file(src, keep=BACKUP_KEEP, stamp=None):
    """Atomically snapshot ``src`` as ``<name>.bak-<stamp>``, then prune family.

    Returns the backup path (``str``), or ``None`` when ``src`` does not exist
    (nothing to back up — callers that need to distinguish "absent" can check
    the return value).

    Failures propagate: a caller about to overwrite operator state *must* know
    the backup did not happen.
    """
    src = str(src).rstrip("/") or str(src)
    if not os.path.exists(src):
        return None
    dest = f"{src}{BACKUP_INFIX}{stamp or _stamp()}"

    if os.path.isdir(src) and not os.path.islink(src):
        # Payload backups are directory trees; copytree to a fresh name is
        # already all-or-nothing for the *name* — nothing carries it until the
        # whole tree lands.
        shutil.copytree(src, dest, symlinks=False)
        prune_backups(src, keep=keep)
        return dest

    atomic_copy(src, dest)
    prune_backups(src, keep=keep)
    return dest


def backup_path_for(src, stamp=None):
    """The ``.bak-<stamp>`` path ``backup_file(src)`` would use (no write)."""
    src = str(src).rstrip("/") or src
    return f"{src}{BACKUP_INFIX}{stamp or _stamp()}"


def prune_backup_dir(directory, keep=BACKUP_KEEP, stamped_only=True):
    """Cap every ``<stem>`` family inside ``directory`` at ``keep`` backups.

    For piles the writers did not create next to a live file: directories
    backups were ``shutil.move``\\d into (``plugins-backups/``, ``scripts/``)
    have no target path to derive a family from, so this groups by everything
    before the first ``.bak-`` marker.

    Default hygiene only removes machine-stamped names — operator-labelled
    bookmarks survive unless ``stamped_only=False`` is passed explicitly.

    Returns ``(removed, families)``. Best-effort; never raises.
    """
    directory = str(directory)
    try:
        names = os.listdir(directory)
    except OSError:
        return 0, 0

    groups = {}
    for name in names:
        idx = name.find(BACKUP_INFIX)
        if idx <= 0:
            continue
        if stamped_only and not _STAMP_RE.search(name):
            continue
        full = os.path.join(directory, name)
        try:
            if not (os.path.isfile(full) or os.path.isdir(full)):
                continue  # dangling symlink / vanished between listdir and stat
        except OSError:
            continue
        groups.setdefault(name[:idx], []).append(full)

    removed = 0
    for members in groups.values():
        for victim in select_victims(members, keep):
            try:
                if os.path.isdir(victim) and not os.path.islink(victim):
                    shutil.rmtree(victim, ignore_errors=True)
                else:
                    os.remove(victim)
                removed += 1
            except OSError:
                pass
    return removed, len(groups)


# ── one-shot hygiene sweep (CLI) ────────────────────────────────────────────

#: Sub-directories of a Hermes home that collect HSCC backup families.
_SWEEP_SUBDIRS = ("hooks", "scripts", "plugins-backups", "plugins_backups")

#: Home-root files whose ``.bak-*`` families belong to HSCC writers.
_SWEEP_FILES = ("config.yaml", "SOUL.md")


def _hermes_root(home):
    """Root home for a possibly PROFILE-scoped ``HERMES_HOME``.

    ``<root>/profiles/<name>`` is Hermes' normal run mode; the sweep covers the
    whole tree from the root. Same profiles-grandparent rule as doctor.py and
    hscc-roles/rolelib.py.
    """
    home = os.path.expanduser(str(home))
    if os.path.basename(os.path.dirname(home)) == "profiles":
        return os.path.dirname(os.path.dirname(home))
    return home


def _group_backups(names, stamped_only):
    """{stem: [name, ...]} over ``names`` that carry a ``.bak-`` infix."""
    groups = {}
    for name in names:
        idx = name.find(BACKUP_INFIX)
        if idx <= 0:
            continue
        if stamped_only and not _STAMP_RE.search(name):
            continue
        groups.setdefault(name[:idx], []).append(name)
    return groups


def sweep_home(home=None, keep=BACKUP_KEEP, dry_run=False,
               include_labeled=False, profiles=True):
    """Prune every HSCC backup family under a Hermes root (and its profiles).

    Touches only ``.bak-`` family names inside ``_SWEEP_SUBDIRS`` and the
    ``_SWEEP_FILES`` families at each home root — never a directory whose name
    isn't a backup, so operator content can't be collateral. Operator-labelled
    backups (no machine stamp) survive unless ``include_labeled=True``.

    ``dry_run`` counts from names only (no stat), so it stays cheap on a
    27k-entry pile. Returns a summary dict.
    """
    home = _hermes_root(home or os.environ.get("HERMES_HOME")
                        or os.path.expanduser("~/.hermes"))
    homes = [home]
    if profiles:
        profiles_dir = os.path.join(home, "profiles")
        try:
            for child in sorted(os.listdir(profiles_dir)):
                pdir = os.path.join(profiles_dir, child)
                if os.path.isdir(pdir) and not child.startswith("."):
                    homes.append(pdir)
        except OSError:
            pass

    stamped_only = not include_labeled
    report = []
    for h in homes:
        for sub in _SWEEP_SUBDIRS:
            d = os.path.join(h, sub)
            if not os.path.isdir(d):
                continue
            try:
                names = os.listdir(d)
            except OSError:
                continue
            groups = _group_backups(names, stamped_only)
            if dry_run:
                n = sum(max(0, len(v) - keep) for v in groups.values())
                if n:
                    report.append({"dir": d, "would_remove": n,
                                   "families": len(groups)})
                continue
            removed, fams = prune_backup_dir(d, keep=keep,
                                             stamped_only=stamped_only)
            if removed:
                report.append({"dir": d, "removed": removed, "families": fams})
        if not os.path.isdir(h):
            continue
        for fname in _SWEEP_FILES:
            target = os.path.join(h, fname)
            members = _family_members(h, fname)
            members = [m for m in members
                       if not stamped_only or _STAMP_RE.search(os.path.basename(m))]
            if dry_run:
                n = len(select_victims(members, keep))
                if n:
                    report.append({"file": target, "would_remove": n})
                continue
            removed = 0
            for victim in select_victims(members, keep):
                try:
                    os.remove(victim)
                    removed += 1
                except OSError:
                    pass
            if removed:
                report.append({"file": target, "removed": removed})
    return {"home": home, "keep": keep, "dry_run": dry_run, "pruned": report}


def _main(argv=None):
    """CLI: prune the HSCC backup families under one Hermes home.

    Read-only by default (``--dry-run`` reports what would go) — point it at
    the live home deliberately; nothing here runs as a side effect of an
    install, because pruning the operator's history is an explicit act.
    """
    import argparse
    import json
    import sys

    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        description="Prune HSCC .bak-<stamp> families under one Hermes home.")
    ap.add_argument("--home", default=None,
                    help="Hermes root to sweep (default: HERMES_HOME; a "
                         "profile-scoped value walks up to the root)")
    ap.add_argument("--keep", type=int, default=BACKUP_KEEP,
                    help=f"newest backups kept per family (default {BACKUP_KEEP})")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-profiles", action="store_true",
                    help="sweep only the root home, skip <home>/profiles/*")
    ap.add_argument("--include-labeled", action="store_true",
                    help="also prune operator-labelled backups (names without "
                         "the machine .bak-YYYYMMDD-HHMMSS stamp)")
    args = ap.parse_args(argv)

    result = sweep_home(args.home, keep=args.keep, dry_run=args.dry_run,
                        include_labeled=args.include_labeled,
                        profiles=not args.no_profiles)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
