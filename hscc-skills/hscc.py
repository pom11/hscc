#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Skills & Templates Installer

Install / update bundled Hermes skills and templates to ~/.hermes/ directories.

Idempotent: a file that already matches the repo is left alone. An hscc-OWNED
skill (a name in ``BUNDLED_SKILLS``) whose installed copy DIFFERS from the
repo is backed up first and then overwritten — an existing copy is never a
reason to skip, which is how a stale July copy used to survive every
bootstrap (t_45fa38e2, audit t_4c12ff7f §8). Foreign and operator skills are
never written, moved or removed by this installer.

MAIN-ONLY (t_b52e70ba, operator ruling R4): the cluster-control skills
``hscc``, ``hscc-cluster`` and ``hscc-model-onboard`` install to the main
orchestrator's profile only — ``<root>/profiles/<main_orch.main_name()>``,
never a hard-coded name. Every copy found in the root home or any other
profile is MOVED (never deleted) to that home's
``skills-backups/removed-<stamp>/`` so it stays recoverable.

Usage: hscc-skills <command> [args]

Commands:
  install              Install all skills and templates (full bootstrap)
  install-skills       Install / update all Hermes skills to ~/.hermes/skills/
  install-templates    Install / update all Hermes templates to ~/.hermes/templates/
  status               Show installation status: installed, missing, out-of-date
  uninstall            Remove all installed skills and templates (reverse install)

Environment Variables:
  HSCC_SKILLS_DIR           Override Hermes source path (default: ~/hermes-cc)
  HERMES_HOME          Override Hermes home (default: ~/.hermes)
"""

import sys
import os
import hashlib
import importlib.util
import shutil
import json
import time

# ── Constants ──────────────────────────────────────────────────────────────

HSCC_SKILLS_DIR = os.environ.get("HSCC_SKILLS_DIR", os.path.expanduser("~/.hermes"))
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _find_skills_source():
    """Locate the vendored skill source-of-truth inside the plugins repo.

    Skills are versioned at <repo>/install/hscc-skills/. Walk up from this
    file to find it so the installer works from both the active plugin path
    and the install-template path. Falls back to the live skills dir.
    """
    env = os.environ.get("HSCC_SKILLS_SRC")
    if env:
        return env
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        cand = os.path.join(d, "install", "hscc-skills")
        if os.path.isdir(cand):
            return cand
        d = os.path.dirname(d)
    return os.path.join(HERMES_HOME, "skills")


SKILLS_SOURCE = _find_skills_source()
SKILLS_DEST = os.path.join(HERMES_HOME, "skills")
TEMPLATES_SOURCE = os.path.join(HSCC_SKILLS_DIR, "Resources", "templates")
TEMPLATES_DEST = os.path.join(HERMES_HOME, "templates")

# Bundled skills (vendored under install/hscc-skills/ in the repo)
BUNDLED_SKILLS = [
    # Generic Hermes skills
    "brainstorming",
    "caveman",
    "executing-plans",
    "systematic-debugging",
    "test-driven-development",
    "verification-before-completion",
    "writing-plans",
    # HSCC cluster-control skills (native-Hermes-first: coordinator/events/
    # governance/orchestrator/projects archived 2026-06-08 with their plugins;
    # dispatch is native kanban now, cluster ops via the hscc-cluster toolset.
    # provision archived 2026-06-10 — provision_model is in the hscc-cluster
    # toolset; container lifecycle via sparkrun + the model-onboard skill).
    "hscc",
    "hscc-cluster",
    "hscc-lessons",
    "hscc-model-onboard",
    "sdlc-review",
    # devops skill group (architecture, plugins, kanban/webhook, migration helpers)
    "devops",
]

# Hermes bundled templates
BUNDLED_TEMPLATES = [
    "AGENTS.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
]

#: The skills hscc OWNS — exactly the names this installer may back up and
#: overwrite. Nothing else in a skills tree is hscc's to touch (t_45fa38e2),
#: and that boundary is what makes "always update, never skip" safe.
OWNED_SKILLS = tuple(BUNDLED_SKILLS)

#: Cluster-CONTROL skills — the owned skills that document the cluster
#: surface, and therefore belong on the main orchestrator's home ONLY (driver
#: ruling 4 on the main-only audit,
#: docs/audits/2026-11-23-main-only-surface.md §5.3 row 6 / §8 t_b52e70ba;
#: R4 in verdict-main-only.txt, 2026-11-23): "documentation of a toolset a
#: profile cannot call is a lie in the prompt". This is a SUBSET of
#: BUNDLED_SKILLS on purpose — the rest (brainstorming, caveman, sdlc-review,
#: the devops group, …) is generic Hermes material that stays on every
#: profile, so a check keyed on the whole list would report the fleet's
#: healthy skill trees as drift. These three install to the main profile only,
#: and every copy in any OTHER home is MOVED to that home's
#: ``skills-backups/removed-<stamp>/`` (never deleted). Read (never
#: re-spelled) by hscc-bootstrap/doctor_main_only.py, which is what keeps
#: doctor's verdict and this installer's ownership boundary from disagreeing.
CLUSTER_SKILLS = ("hscc", "hscc-cluster", "hscc-model-onboard")

#: Where a replaced owned-skill copy is kept: a SIBLING of the scanned
#: ``skills/`` dir, never inside it. hermes collects skill candidates by
#: directory name AND frontmatter ``name:`` at ANY depth under the scan root
#: (see hscc-bootstrap/skill_guard.py), so a ``.bak-*`` copy left in place is
#: discovered as a skill and CREATES the name collision the guard exists to
#: prevent. ``install_review_skill._backups_dir`` set this precedent.
SKILLS_BACKUPS_DIRNAME = "skills-backups"


class BackupUnavailable(RuntimeError):
    """A replacing write whose backup could not be taken.

    Raised instead of writing. A replacing write without a backup is a
    deletion (the contract install_review_skill.py already states for its own
    seam), so the caller reports this and writes nothing.
    """


class MainProfileUnresolved(RuntimeError):
    """The main orchestrator's profile could not be resolved.

    Carries no fallback name on purpose: D-A/R2 forbid a hard-coded
    ``general``, so an unreadable or missing ``hscc-roles/main_orch.py`` must
    fail closed instead of silently installing cluster-control text into a
    guessed profile.
    """


# ── Helpers ────────────────────────────────────────────────────────────────

def file_hash(path):
    """Return MD5 hex digest of a file."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (FileNotFoundError, PermissionError):
        return None


def ensure_dir(path):
    """Create directory and parents if needed."""
    os.makedirs(path, exist_ok=True)


def copy_if_different(src, dst):
    """Copy src to dst only if dst doesn't exist or content differs.
    
    Returns:
        "copied"  — file was copied (new or updated)
        "skipped" — file already matches source
        "created" — directory was created for parent
    """
    if not os.path.isfile(src):
        return None

    ensure_dir(os.path.dirname(dst))

    if os.path.isfile(dst):
        if file_hash(src) == file_hash(dst):
            return "skipped"

    shutil.copy2(src, dst)
    return "copied"


def copy_skill_dir(src_dir, dest_dir, skill_name):
    """Copy an entire skill tree (SKILL.md + nested references/scripts/sub-skills).

    Walks the full tree so arbitrary depth (e.g. devops/<sub>/references/*.md)
    is preserved. Returns dict mapping relative path -> copy status.
    """
    results = {}
    dest_skill_dir = os.path.join(dest_dir, skill_name)

    for root, _dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        for fn in sorted(files):
            rel_key = fn if rel_root == "." else os.path.join(rel_root, fn)
            src_path = os.path.join(root, fn)
            dst_path = os.path.join(dest_skill_dir, rel_key)
            status = copy_if_different(src_path, dst_path)
            if status:
                results[rel_key] = status

    return results


def _backup_stamp():
    """Write-time stamp for a backup name (``20261123-050102``).

    The shape is not free: ``backup_util._STAMP_RE`` (``.bak-\\d{8}-\\d{6}``)
    is what orders a family when the retention window prunes it, and
    ``backup_util._backup_epoch`` reads the name instead of the mtime because
    ``copy2`` stamps a backup with the SOURCE's clock. A name that drifts from
    the shape is a backup the pruner can rank, which is worse than one it can
    see and keep.
    """
    return time.strftime("%Y%m%d-%H%M%S")


def _load_backup_util():
    """Import ``hscc-bootstrap/backup_util.py`` lazily, or None when absent.

    Same lazy ``sys.path`` window ``_write_decision`` opens for
    ``skill_guard``: the two modules sit beside this one in the repo checkout
    (``hscc-skills/../hscc-bootstrap``) and in the deployed plugins tree.
    ``None`` here must make a caller REFUSE a write, never perform one
    unprotected — see :class:`BackupUnavailable`.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))
                        + os.sep + ".." + os.sep + "hscc-bootstrap")
        import backup_util
        return backup_util
    except ImportError:
        return None


def _load_main_orch():
    """Load ``hscc-roles/main_orch.py`` (the single identity resolver), or None.

    Both the repo checkout (``hscc-skills/../hscc-roles``) and the deployed
    plugins tree (``plugins/hscc-skills/../hscc-roles``) place the module as a
    sibling, so walk up the same way ``_find_skills_source`` does. Loading it
    by path keeps this installer from putting a generic ``main_orch`` module
    name on ``sys.path`` where another payload could shadow it.
    """
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        path = os.path.join(d, "hscc-roles", "main_orch.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(
                "_hscc_skills_main_orch", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        d = os.path.dirname(d)
    return None


def hermes_root_for_home(hermes_home):
    """The root for a home that may be profile-scoped.

    ``<root>/profiles/<name>`` -> ``<root>``; every other path is itself.
    Pure string transform, matching ``hermes_python.hermes_root`` and
    ``doctor.hermes_root_from_home``'s core rule, so a first bootstrap (where
    the profile does not exist yet) still resolves.
    """
    home = os.path.abspath(os.path.expanduser(str(hermes_home)))
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":
        return os.path.dirname(parent)
    return home


def main_profile_home(hermes_home=None):
    """``<root>/profiles/<main_name()>`` — resolved, never hard-coded (D-A/R2)."""
    home = hermes_home if hermes_home is not None else HERMES_HOME
    root = hermes_root_for_home(home)
    resolver = _load_main_orch()
    if resolver is None:
        raise MainProfileUnresolved(
            "hscc-roles/main_orch.py is not importable — cannot resolve the "
            "main orchestrator profile (no hard-coded fallback)")
    name = str(resolver.main_name())
    if not name or name in (".", "..") or os.path.basename(name) != name:
        raise MainProfileUnresolved(
            f"main orchestrator name {name!r} is not a single path component")
    return os.path.join(root, "profiles", name)


def skill_destination(skill, hermes_home=None):
    """``(skills_dir, home)`` for one bundled skill.

    Cluster-control skills target the main profile only; shared/work skills
    keep the caller's home, which is the tree the installer has always
    written. The home is returned too because the collision guard reads that
    home's config.
    """
    home = hermes_home if hermes_home is not None else HERMES_HOME
    if skill in CLUSTER_SKILLS:
        main_home = main_profile_home(home)
        return os.path.join(main_home, "skills"), main_home
    return os.path.join(home, "skills"), home


def _non_main_homes(hermes_home):
    """Every home that must not carry a main-only skill, main excluded."""
    home = os.path.abspath(os.path.expanduser(str(hermes_home)))
    root = hermes_root_for_home(home)
    main_home = os.path.realpath(main_profile_home(home))
    homes = [root]
    profiles = os.path.join(root, "profiles")
    if os.path.isdir(profiles):
        for entry in sorted(os.listdir(profiles)):
            path = os.path.join(profiles, entry)
            if os.path.isdir(path):
                homes.append(path)
    out, seen = [], set()
    for candidate in homes:
        real = os.path.realpath(candidate)
        if real == main_home or real in seen:
            continue
        seen.add(real)
        out.append(candidate)
    return out


def _main_only_copies(skills_root):
    """Every hscc main-only skill dir under *skills_root*, at any depth.

    hermes discovers a SKILL.md package at any depth, so a category copy
    (``skills/control/hscc``) is just as live as a top-level one. A directory
    only counts when it actually carries ``SKILL.md`` and its name is one of
    the main-only names; an unrelated reference subdir that happens to share
    a name is not a skill and is left alone. A selected parent is not
    descended into, so a tree cannot be listed twice.
    """
    found = []
    for root, dirs, _files in os.walk(skills_root):
        keep = []
        for name in sorted(dirs):
            path = os.path.join(root, name)
            if name in MAIN_ONLY_SKILLS and \
                    os.path.isfile(os.path.join(path, "SKILL.md")):
                found.append((path, name))
                continue  # moving the parent already carries this subtree
            keep.append(name)
        dirs[:] = keep
    return found


def move_non_main_skill_copies(hermes_home=None, stamp=None):
    """Move main-only skills out of every non-main home into dated backups.

    R4: non-main homes get NO hscc cluster-control skills. The move is
    ``os.rename`` into ``<home>/skills-backups/removed-<stamp>/`` — a sibling
    of the scanned ``skills/`` tree — so the bytes remain recoverable and no
    ``rm``/``shutil.rmtree`` touches operator data. A same-home rename also
    cannot lose a tree halfway across a copy. Returns
    ``[(home, skill, backup_path), ...]``.
    """
    stamp = stamp or _backup_stamp()
    moved = []
    for home in _non_main_homes(hermes_home if hermes_home is not None
                                else HERMES_HOME):
        skills = os.path.join(home, "skills")
        if not os.path.isdir(skills):
            continue
        for src, skill in _main_only_copies(skills):
            backups = os.path.join(home, SKILLS_BACKUPS_DIRNAME,
                                   f"removed-{stamp}")
            ensure_dir(backups)
            dst = os.path.join(backups, skill)
            n = 1
            while os.path.lexists(dst):
                n += 1
                dst = os.path.join(backups, f"{skill}-{n}")
            os.rename(src, dst)
            moved.append((home, skill, dst))
    return moved


def _sweep_non_main(hermes_home):
    """Move non-main copies; -> ``(moved, error)`` with a fail-closed error."""
    try:
        return move_non_main_skill_copies(hermes_home), None
    except MainProfileUnresolved as exc:
        return [], str(exc)


def _moved_rows(moved, hermes_home):
    """Report rows that name every moved tree and where it went."""
    root = hermes_root_for_home(hermes_home)
    rows = []
    for home, skill, dst in moved:
        rel_home = os.path.relpath(home, root)
        label = "(root)" if rel_home == "." else rel_home
        rows.append((f"{skill} [{label}]", "MOVED",
                     os.path.relpath(dst, home)))
    return rows


def skill_backups_dir(skills_dest):
    """``<home>/skills-backups`` for a skills dir — OUTSIDE the scan root."""
    return os.path.join(os.path.dirname(os.path.abspath(skills_dest)),
                        SKILLS_BACKUPS_DIRNAME)


def owned_skill_drift(src_dir, dest_dir, skill_name):
    """Files this template ships that the installed copy lacks or differs on.

    Empty list means the installed copy matches the repo for every shipped
    file. Deliberately one-directional: a file the template does NOT ship is
    operator content, never drift, and never a reason to write or to delete.
    """
    live = os.path.join(dest_dir, skill_name)
    drift = []
    for root, _dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        for fn in sorted(files):
            rel = fn if rel_root == "." else os.path.join(rel_root, fn)
            dst_path = os.path.join(live, rel)
            if not os.path.isfile(dst_path) or \
                    file_hash(os.path.join(root, fn)) != file_hash(dst_path):
                drift.append(rel)
    return drift


def _snapshot_owned_skill(skill_name, dest_dir, backup_mod, stamp=None):
    """Copy the live owned-skill tree aside and prune the family. Returns the name.

    Written under ``skills-backups/`` (see ``SKILLS_BACKUPS_DIRNAME``) with the
    ``<name>.bak-<stamp>`` shape every HSCC writer uses, then pruned by
    ``backup_util.prune_backup_dir`` to ``HSCC_BACKUP_KEEP`` (default 3) so the
    pile is self-limiting — the 27k-file / 1.5 GB lesson of t_9462260b applies
    to skills exactly as it did to hooks. Two runs inside one second collide
    on the stamp, so a counter suffix keeps the second snapshot instead of
    raising or silently reusing the first.
    """
    live = os.path.join(dest_dir, skill_name)
    backups = skill_backups_dir(dest_dir)
    ensure_dir(backups)
    base = f"{skill_name}{backup_mod.BACKUP_INFIX}{stamp or _backup_stamp()}"
    bak = os.path.join(backups, base)
    n = 1
    while os.path.exists(bak):
        n += 1
        bak = os.path.join(backups, f"{base}-{n}")
    try:
        shutil.copytree(live, bak, symlinks=False)
    except OSError as exc:
        shutil.rmtree(bak, ignore_errors=True)
        raise BackupUnavailable(
            f"could not snapshot {live} to {bak}: {exc}") from exc
    backup_mod.prune_backup_dir(backups)
    return os.path.basename(bak)


def install_owned_skill(src_dir, dest_dir, skill_name, stamp=None):
    """Backup-then-overwrite ONE hscc-owned skill. -> (per-file results, backup|None)

    The order is the whole point: the snapshot is taken BEFORE the first write
    and :class:`BackupUnavailable` aborts the call if it cannot be taken, so no
    path replaces operator bytes without a recoverable copy. A copy that
    already matches the repo needs no snapshot — nothing is being given up —
    which is what keeps a clean bootstrap from minting one backup per owned
    skill per run.

    Removals are not part of this seam: files the template does not ship stay
    where they are (the operator's, not hscc's), so the update converges the
    shipped text without ever deleting.
    """
    backup_name = None
    if os.path.isdir(os.path.join(dest_dir, skill_name)) and \
            owned_skill_drift(src_dir, dest_dir, skill_name):
        backup_mod = _load_backup_util()
        if backup_mod is None:
            raise BackupUnavailable(
                "hscc-bootstrap/backup_util.py is not importable — refusing to "
                f"overwrite the installed copy of {skill_name} without a backup")
        backup_name = _snapshot_owned_skill(skill_name, dest_dir, backup_mod,
                                            stamp=stamp)
    return copy_skill_dir(src_dir, dest_dir, skill_name), backup_name


def _write_decision(skill_name, dest_dir, hermes_home):
    """Collision-guard verdict for one owned skill about to be written.

    -> ``(skip_reason, note)``. ``skip_reason`` non-None means do not write;
    ``note`` is guard context that must still reach the report even when the
    write goes ahead.

    This module is the writer that CREATED the June collisions: BUNDLED_SKILLS
    were copied FLAT into the live skills tree while hermes' own builtin sync
    maintained category copies (``devops/sdlc-review``,
    ``software-development/{systematic-debugging,test-driven-development}``)
    with different content, and hermes refuses to resolve an ambiguous bare
    name — every spawn asking for one died with 'Unknown skill(s)'. Writes
    therefore go through the same guard install_review_skill.py uses, with one
    narrowing (t_45fa38e2):

      * the name resolves NOWHERE                      -> write (first install);
      * the name resolves AT the path this writer writes -> WRITE. Refreshing a
        copy that is already here cannot create a collision the tree does not
        already have, and the old blanket refusal is precisely what froze the
        stale copies the audit found (t_4c12ff7f §8: 'GUARDED collision' was
        why July content survived every bootstrap);
      * the name resolves only from ANOTHER path, and there is no copy here
        -> STILL REFUSED. Writing there is what created June's collision, so
        that door stays shut and the refusal is reported verbatim.

    The import is lazy and fail-open ONLY when skill_guard.py is absent (a
    half-deployed tree) — never on a guard *decision*.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))
                        + os.sep + ".." + os.sep + "hscc-bootstrap")
        import skill_guard
    except ImportError:
        return None, ""
    roots = [os.path.abspath(dest_dir)]
    ext = skill_guard.external_dirs_for_home(hermes_home)
    roots.extend(str(r) for r in ext)
    res = skill_guard.resolution(roots, skill_name)
    if res["state"] == "missing":
        return None, ""
    target = os.path.realpath(os.path.join(dest_dir, skill_name))
    try:
        at_target = any(os.path.realpath(p) == target for p in res["paths"])
    except OSError:
        at_target = False
    if at_target:
        if res["state"] == "collision":
            return None, ("refreshed a colliding copy (hermes still refuses the "
                          "bare name; adjudicate with `doctor.py --skills`): "
                          + ", ".join(res["paths"]))
        if res["state"] == "disabled":
            return None, "refreshed, but this home lists the name under skills.disabled"
        return None, ""
    if res["state"] == "collision":
        return ("collision: " + ", ".join(res["paths"]), "")
    if res["state"] == "disabled":
        return ("disabled in this home: " + ", ".join(res["paths"]), "")
    return ("already resolves elsewhere: " + ", ".join(res["paths"]), "")


def _owned_skill_write(src, skill, dest_dir, hermes_home):
    """Apply the guard then backup+overwrite.

    -> ``(status, detail, copied, unchanged)`` so both callers keep their own
    totals. Shared by ``install`` and ``install-skills`` so the two entry
    points cannot drift into two different update policies.
    """
    skip, note = _write_decision(skill, dest_dir, hermes_home)
    if skip:
        return "GUARDED", skip[:120], 0, 0
    try:
        results, backup_name = install_owned_skill(src, dest_dir, skill)
    except BackupUnavailable as exc:
        return "BACKUP-FAILED", str(exc)[:120], 0, 0
    if not results:
        # An owned dir with nothing in it is a broken template, not a success:
        # say so rather than reporting the skill as up to date.
        return "EMPTY", "source ships no files", 0, 0
    copies = sum(1 for s in results.values() if s == "copied")
    skips = sum(1 for s in results.values() if s == "skipped")
    detail = f"+{copies} copied" + (f", {skips} unchanged" if skips else "")
    if backup_name:
        detail += f" (backup {backup_name})"
    if note:
        detail += f" — {note}"
    return ("UPDATED" if copies else "UP-TO-DATE"), detail, copies, skips


def format_status_table(items):
    """Format a list of (name, status, detail) tuples as a readable table."""
    if not items:
        return "  (nothing to report)"

    # Calculate column widths
    name_w = max((len(item[0]) for item in items), default=20)
    name_w = max(name_w, 10)
    detail_w = 30

    lines = []
    header = f"  {'Name':<{name_w}}  {'Status':<12}  {'Detail'}"
    lines.append(header)
    lines.append(f"  {'-' * name_w}  {'-' * 12}  {'-' * detail_w}")

    for name, status, detail in items:
        status_label = status.upper().ljust(12)
        lines.append(f"  {name:<{name_w}}  {status_label}  {detail}")

    return "\n".join(lines)


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_install():
    """Install all skills and templates."""
    print("=" * 60)
    print("  HSCC Skills & Templates Installer")
    print(f"  Source: {HSCC_SKILLS_DIR}")
    print(f"  Dest:   {HERMES_HOME}")
    print("=" * 60)
    print()

    # Verify source
    if not os.path.isdir(SKILLS_SOURCE):
        print(f"[WARN] Skills source not found: {SKILLS_SOURCE}")
        print(f"       Setting HSCC_SKILLS_DIR env var or placing skills at ~/hermes-cc/skills/")
        print()

    if not os.path.isdir(TEMPLATES_SOURCE):
        print(f"[WARN] Templates source not found: {TEMPLATES_SOURCE}")
        print(f"       Setting HSCC_SKILLS_DIR env var or placing templates at ~/hermes-cc/Resources/templates/")
        print()

    # Install skills
    print("─── Skills ───")
    skills_results = {}
    skills_copied = 0
    skills_skipped = 0
    skills_missing = 0

    for skill in BUNDLED_SKILLS:
        src = os.path.join(SKILLS_SOURCE, skill)
        if not os.path.isdir(src):
            skills_missing += 1
            skills_results[skill] = ("MISSING", f"not found in source")
            continue

        try:
            dest, guard_home = skill_destination(skill, HERMES_HOME)
        except MainProfileUnresolved as exc:
            skills_missing += 1
            skills_results[skill] = ("MAIN-UNRESOLVED", str(exc)[:120])
            continue
        status, detail, copies, unchanged = _owned_skill_write(
            src, skill, dest, guard_home)
        if status in ("GUARDED", "BACKUP-FAILED", "EMPTY"):
            skills_skipped += 1
        else:
            skills_copied += copies
            skills_skipped += unchanged
        skills_results[skill] = (status, detail)

    moved, move_error = _sweep_non_main(HERMES_HOME)
    print(format_status_table(
        [(s, r[0], r[1]) for s, r in skills_results.items()]
        + _moved_rows(moved, HERMES_HOME)
    ))
    print()
    if move_error:
        print(f"[ERROR] non-main skill cleanup skipped: {move_error}")
    elif moved:
        print(f"Moved {len(moved)} main-only skill tree(s) out of non-main "
              "homes into dated skills-backups/removed-<stamp>/ dirs.")
    print()

    # Install templates
    print("─── Templates ───")
    template_results = {}
    templates_copied = 0
    templates_skipped = 0
    templates_missing = 0

    for tmpl in BUNDLED_TEMPLATES:
        src = os.path.join(TEMPLATES_SOURCE, tmpl)
        dst = os.path.join(TEMPLATES_DEST, tmpl)

        if not os.path.isfile(src):
            templates_missing += 1
            template_results[tmpl] = ("MISSING", "not found in source")
            continue

        status = copy_if_different(src, dst)
        if status == "copied":
            templates_copied += 1
            template_results[tmpl] = ("UPDATED", "installed")
        elif status == "skipped":
            templates_skipped += 1
            template_results[tmpl] = ("UP-TO-DATE", "already installed")
        else:
            templates_skipped += 1
            template_results[tmpl] = ("UP-TO-DATE", "already installed")

    print(format_status_table(
        [(t, r[0], r[1]) for t, r in template_results.items()]
    ))
    print()

    # Summary
    total_copied = skills_copied + templates_copied
    total_skipped = skills_skipped + templates_skipped
    print("─── Summary ───")
    print(f"  Skills:     {skills_copied} copied, {skills_skipped} skipped, {skills_missing} missing from source")
    print(f"  Templates:  {templates_copied} copied, {templates_skipped} skipped, {templates_missing} missing from source")
    print(f"  Total:      {total_copied} installed, {total_skipped} unchanged")

    if total_copied > 0:
        print()
        print("  Done! Skills and templates are now installed.")
        print(f"  Skills:  {SKILLS_DEST}")
        print(f"  Templates: {TEMPLATES_DEST}")
    else:
        print()
        print("  Everything is already up to date. Nothing to do.")


def cmd_install_skills():
    """Install / update Hermes skills only (collision-guarded, backup-first)."""
    print("─── Installing Hermes Skills ───")
    print(f"  Source: {SKILLS_SOURCE}")
    print(f"  Dest:   {SKILLS_DEST}")
    try:
        main_skills = os.path.join(main_profile_home(HERMES_HOME), "skills")
    except MainProfileUnresolved as exc:
        print(f"[ERROR] {exc}")
        print("        Refusing to install without a resolved main profile.")
        sys.exit(1)
    print(f"  Main:   {main_skills} ({', '.join(CLUSTER_SKILLS)} only)")
    print()

    if not os.path.isdir(SKILLS_SOURCE):
        print(f"[ERROR] Skills source not found: {SKILLS_SOURCE}")
        sys.exit(1)

    results = []
    copied = 0
    guarded = 0
    failed = 0
    backups = []
    for skill in BUNDLED_SKILLS:
        src = os.path.join(SKILLS_SOURCE, skill)
        if not os.path.isdir(src):
            results.append((skill, "MISSING", "not found"))
            continue
        dest, guard_home = skill_destination(skill, HERMES_HOME)
        status, detail, copies, _unchanged = _owned_skill_write(
            src, skill, dest, guard_home)
        if status == "GUARDED":
            guarded += 1
        elif status == "BACKUP-FAILED":
            failed += 1
        else:
            copied += copies
            if "backup " in detail:
                backups.append((skill, os.path.join(
                    skill_backups_dir(dest),
                    detail.split("backup ")[-1].rstrip(")"))))
        results.append((skill, status, detail))

    moved, move_error = _sweep_non_main(HERMES_HOME)
    results.extend(_moved_rows(moved, HERMES_HOME))

    print(format_status_table(results))
    print()
    if guarded:
        print(f"{guarded} skill(s) GUARDED (name already resolves elsewhere and "
              "nothing is installed at the target — writing there would CREATE a "
              "collision; the existing copy wins).")
    if failed:
        print(f"{failed} skill(s) BACKUP-FAILED: nothing was written. hscc refuses "
              "a replacing write it cannot back up — restore "
              "hscc-bootstrap/backup_util.py and re-run.")
    if backups:
        print("Replaced copies were snapshotted first:")
        for skill, backup_path in backups:
            print(f"  {skill}: {backup_path}")
    if move_error:
        print(f"[ERROR] non-main skill cleanup skipped: {move_error}")
    elif moved:
        print(f"Moved {len(moved)} main-only skill tree(s) out of non-main "
              "homes into dated skills-backups/removed-<stamp>/ dirs "
              "(move, not delete).")
    print(f"Done. {copied} file(s) installed, rest unchanged.")


def cmd_install_templates():
    """Install / update Hermes templates only."""
    print("─── Installing Hermes Templates ───")
    print(f"  Source: {TEMPLATES_SOURCE}")
    print(f"  Dest:   {TEMPLATES_DEST}")
    print()

    if not os.path.isdir(TEMPLATES_SOURCE):
        print(f"[ERROR] Templates source not found: {TEMPLATES_SOURCE}")
        sys.exit(1)

    results = []
    copied = 0
    for tmpl in BUNDLED_TEMPLATES:
        src = os.path.join(TEMPLATES_SOURCE, tmpl)
        dst = os.path.join(TEMPLATES_DEST, tmpl)
        status = copy_if_different(src, dst)
        if status == "copied":
            copied += 1
            results.append((tmpl, "UPDATED", "installed"))
        else:
            results.append((tmpl, "UP-TO-DATE", "already installed"))

    print(format_status_table(results))
    print()
    print(f"Done. {copied} template(s) installed, rest unchanged.")


def cmd_status():
    """Show installation status for all skills and templates."""
    print("=" * 60)
    print("  HSCC Skills & Templates — Status Report")
    print("=" * 60)
    print()

    # Skills status
    print("─── Skills ───")
    skill_status = []
    for skill in BUNDLED_SKILLS:
        src_skill = os.path.join(SKILLS_SOURCE, skill)
        try:
            dst_dir, _guard_home = skill_destination(skill, HERMES_HOME)
        except MainProfileUnresolved as exc:
            skill_status.append((skill, "⚠ MAIN-UNRESOLVED", str(exc)[:120]))
            continue
        dst_skill = os.path.join(dst_dir, skill)

        if not os.path.isdir(src_skill):
            skill_status.append((skill, "⚠ MISSING", "source not found"))
            continue

        installed_files = os.listdir(dst_skill) if os.path.isdir(dst_skill) else []

        if not installed_files:
            n_src = sum(len(fs) for _r, _d, fs in os.walk(src_skill))
            skill_status.append((skill, "⚠ NOT INSTALLED", f"{n_src} file(s) in source"))
            continue

        # Use the WRITER's own definition of drift, so `status` and `install`
        # can never disagree about the same tree. The old compare walked only
        # the top level AND only files present on both sides, so it reported
        # ✓ OK for a skill whose nested references/*.md was stale — or missing.
        out_of_date = owned_skill_drift(src_skill, dst_dir, skill)
        if out_of_date:
            skill_status.append((skill, "⚠ OUT-OF-DATE",
                                 f"{len(out_of_date)} file(s) need update"))
        else:
            skill_status.append((skill, "✓ OK", f"{len(installed_files)} file(s)"))

    print(format_status_table(skill_status))
    print()

    # Templates status
    print("─── Templates ───")
    template_status = []
    for tmpl in BUNDLED_TEMPLATES:
        src_path = os.path.join(TEMPLATES_SOURCE, tmpl)
        dst_path = os.path.join(TEMPLATES_DEST, tmpl)

        if not os.path.isfile(src_path):
            template_status.append((tmpl, "⚠ MISSING", "source not found"))
            continue

        if not os.path.isfile(dst_path):
            template_status.append((tmpl, "⚠ NOT INSTALLED", "target not found"))
            continue

        if file_hash(src_path) != file_hash(dst_path):
            template_status.append((tmpl, "⚠ OUT-OF-DATE", "source differs"))
        else:
            template_status.append((tmpl, "✓ OK", "matches source"))

    print(format_status_table(template_status))
    print()

    # Locations
    print("─── Locations ───")
    print(f"  Hermes Skills:  {SKILLS_SOURCE}")
    print(f"  Hermes Skills:  {SKILLS_DEST}")
    try:
        print(f"  Main profile:   {main_profile_home(HERMES_HOME)}")
    except MainProfileUnresolved as exc:
        print(f"  Main profile:   UNRESOLVED — {exc}")
    print(f"  Hermes Templates: {TEMPLATES_SOURCE}")
    print(f"  Hermes Templates: {TEMPLATES_DEST}")


def cmd_uninstall():
    """Remove all installed skills and templates."""
    print("─── Uninstalling HSCC Skills & Templates ───")
    print(f"  Removing from: {HERMES_HOME}")
    print()

    removed = 0
    removed_dir = 0

    # Remove skills
    if os.path.isdir(SKILLS_DEST):
        for skill in BUNDLED_SKILLS:
            skill_path = os.path.join(SKILLS_DEST, skill)
            if os.path.isdir(skill_path):
                shutil.rmtree(skill_path)
                removed += 1
                print(f"  Removed skill: {skill}")
            else:
                print(f"  (skipped, not installed): {skill}")
        print()

    # Remove templates
    if os.path.isdir(TEMPLATES_DEST):
        for tmpl in BUNDLED_TEMPLATES:
            tmpl_path = os.path.join(TEMPLATES_DEST, tmpl)
            if os.path.isfile(tmpl_path):
                os.remove(tmpl_path)
                removed_dir += 1
                print(f"  Removed template: {tmpl}")
            else:
                print(f"  (skipped, not installed): {tmpl}")
        print()

    print(f"Done. Removed {removed} skill(s) and {removed_dir} template(s).")


# ── Command Map ───────────────────────────────────────────────────────────

COMMANDS = {
    "install": cmd_install,
    "install-skills": cmd_install_skills,
    "install-templates": cmd_install_templates,
    "status": cmd_status,
    "uninstall": cmd_uninstall,
}


# ── Entry Point ───────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print("""
Hermes Spark Cluster Control (HSCC) — Skills & Templates Installer

Usage: hscc-skills <command> [args]

Commands:
  install              Install all skills and templates (full bootstrap)
  install-skills       Install / update all Hermes skills to ~/.hermes/skills/
  install-templates    Install / update all Hermes templates to ~/.hermes/templates/
  status               Show installation status
  uninstall            Remove installed skills and templates (reverse)

Environment Variables:
  HSCC_SKILLS_DIR           Override Hermes source path (default: ~/hermes-cc)
  HERMES_HOME          Override Hermes home (default: ~/.hermes)
        """.strip())
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    COMMANDS[cmd]()


if __name__ == "__main__":
    main()
