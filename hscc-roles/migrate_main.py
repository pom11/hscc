"""Migrate an existing install to the ONE main orchestrator (design 2026-11-19).

The main profile, its board and the resolver already exist (M1). This module
adds the data-preserving half of the migration:

  * the legacy catch-all's ``memories/`` and ``sessions/`` are COPIED into the
    main profile — per file, only where the destination does not already have
    that path. The main profile always wins and the source is never moved or
    deleted (the profiles stay on disk for history);
  * the legacy catch-all and the legacy cluster-wide role are marked retired in
    their SOUL ("retired — use <main>"), idempotently.

Everything here is a no-op on a fresh install and a byte-identical no-op on a
second run. Config files are not touched at all: the generator's existing
``merge_preserving`` path owns them and already never drops operator keys.

``main_orch`` is the only owner of the legacy names and the retirement text, so
this module carries no duplicate literals.
"""

from __future__ import annotations

import os
import shutil

import main_orch

#: Profile-relative directories carried over from the legacy catch-all.
CARRY_DIRS = ("memories", "sessions")


def _relative(profile: str, path: str, profiles_dir: str) -> str:
    """A stable, human-readable ``profile/rel`` label for the report."""
    rel = os.path.relpath(path, os.path.join(profiles_dir, profile))
    return f"{profile}/{rel}"


def _copy_missing(src: str, dest: str, *, dest_profile: str,
                  profiles_dir: str, report: dict) -> None:
    """Copy ``src`` into ``dest`` recursively, never overwriting anything.

    A missing destination path is copied; an existing one (file OR directory)
    is left untouched and reported as skipped. Source files are never removed.
    """
    if not os.path.isdir(src):
        return
    if os.path.lexists(dest):
        if not os.path.isdir(dest):
            report["errors"].append(
                {"path": _relative(dest_profile, dest, profiles_dir),
                 "error": "destination exists and is not a directory"})
            return
    else:
        try:
            os.makedirs(dest)
        except OSError as exc:
            report["errors"].append(
                {"path": _relative(dest_profile, dest, profiles_dir),
                 "error": str(exc)})
            return
        report["created_dirs"].append(
            _relative(dest_profile, dest, profiles_dir))
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dest if rel == "." else os.path.join(dest, rel)
        dirs.sort()
        for directory in dirs:
            wanted = os.path.join(target_root, directory)
            if os.path.exists(wanted):
                continue
            try:
                os.makedirs(wanted)
            except OSError as exc:
                report["errors"].append(
                    {"path": _relative(dest_profile, wanted, profiles_dir),
                     "error": str(exc)})
                continue
            report["created_dirs"].append(
                _relative(dest_profile, wanted, profiles_dir))
        for filename in sorted(files):
            source = os.path.join(root, filename)
            wanted = os.path.join(target_root, filename)
            label = _relative(dest_profile, wanted, profiles_dir)
            if os.path.lexists(wanted):
                report["skipped"].append(label)
                continue
            try:
                shutil.copy2(source, wanted)
            except OSError as exc:
                report["errors"].append({"path": label, "error": str(exc)})
                continue
            report["copied"].append(label)


def _retire(profiles_dir: str, profile: str, main: str) -> str:
    """Mark one legacy profile retired in its SOUL; return the action taken."""
    pdir = os.path.join(profiles_dir, profile)
    if not os.path.isdir(pdir):
        return "absent"
    if os.path.realpath(pdir) == os.path.realpath(
            os.path.join(profiles_dir, main)):
        return "current"
    soul_path = os.path.join(pdir, "SOUL.md")
    try:
        with open(soul_path, encoding="utf-8") as fh:
            existing = fh.read()
    except FileNotFoundError:
        existing = ""
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    if main_orch.RETIRED_MARKER in existing.casefold():
        return "already-retired"
    block = main_orch.retirement_block(main)
    text = block if not existing else f"{block}\n{existing.lstrip()}"
    try:
        with open(soul_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return "error"
    return "retired"


def migrate(profiles_dir: str, main: str = None) -> dict:
    """Run the data-preserving migration; return a report (never raises).

    ``profiles_dir`` is the Hermes profiles root. The caller must have ensured
    the main profile exists first (``ensure_orchestrator(None)``); when it does
    not, the report says so and nothing is written.
    """
    main = main or main_orch.main_name()
    report = {
        "main": main,
        "copied": [],
        "skipped": [],
        "created_dirs": [],
        "retired": {},
        "errors": [],
    }
    main_dir = os.path.join(profiles_dir, main)
    if not os.path.isdir(main_dir):
        report["missing"] = True
        return report

    legacy = main_orch.legacy_general_profile()
    legacy_dir = os.path.join(profiles_dir, legacy)
    if (os.path.isdir(legacy_dir)
            and os.path.realpath(legacy_dir) != os.path.realpath(main_dir)):
        for sub in CARRY_DIRS:
            _copy_missing(
                os.path.join(legacy_dir, sub),
                os.path.join(main_dir, sub),
                dest_profile=main,
                profiles_dir=profiles_dir,
                report=report,
            )

    for name in (legacy, main_orch.LEGACY_ORCHESTRATOR_ROLE):
        action = _retire(profiles_dir, name, main)
        report["retired"][name] = action
        if action in ("unreadable", "error"):
            report["errors"].append({"path": f"{name}/SOUL.md",
                                     "error": f"retirement: {action}"})

    report["changed"] = bool(
        report["copied"] or report["created_dirs"]
        or any(action == "retired" for action in report["retired"].values())
        or report["errors"]
    )
    return report
