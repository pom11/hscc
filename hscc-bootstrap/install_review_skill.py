"""Guarded install of the review-lane skills into Hermes profiles' OWN
skills trees (``<home>/skills`` + ``<home>/profiles/<name>/skills``).

Why this exists (card t_bc611809)
--------------------------------
The kanban dispatcher force-appends ``sdlc-review`` to the skill list of every
REVIEW-lane spawn (``hermes_cli/kanban_db_dispatch.py``: ``lane == "review"``
-> ``claimed.skills = [..., "sdlc-review"]``) and spawns the worker with
``--skills sdlc-review`` — for ANY assignee profile. Skill names resolve
against the SPAWNING PROFILE's own skills dir
(``tools/skills_tool._skill_search_dirs`` -> the profile's ``HERMES_HOME``
``/skills`` plus its ``skills.external_dirs``; there is NO fallback to the
root home), and when nothing loads, ``hermes_cli/oneshot.py`` raises
``ValueError: Unknown skill(s): sdlc-review`` -> the worker exits 1 -> the
dispatcher records a crash. So a review lane whose profile cannot resolve
that name is a dead card, whatever the assignee.

Two distinct per-profile states produce that ONE error string, both measured
on this host (2026-10-27/28):

* NAME COLLISION — the old HSCC bootstrap installed flat copies
  (``<skills>/sdlc-review``) into profile trees while hermes' own builtin sync
  maintained category copies (``<skills>/devops/sdlc-review``) with DIFFERENT
  content. Hermes' resolver (``tools/skills_tool._locate_skill``) refuses to
  guess ("Skill name collision ... Refusing to guess"), the load fails, the
  name lands in the ``missing`` list, the spawn dies.
* GENUINE ABSENCE — the profile never had the skill (ecofire-bc-orch: 3x
  review-spawn deaths -> dispatcher gave_up).

A BLIND fan-out (copy into every profile) is the wrong fix: wherever a
category copy already exists, adding another copy under a different path
CREATES the collision it was meant to cure. So every write here goes through
the collision guard in ``skill_guard.guard_write``: a name is written only
when it resolves NOWHERE else in that home's scan set. Installing where a
collision would arise is skipped and reported; installing into a home that
already resolves the name is a no-op.

Contract
--------
* Category layout only — ``<skills>/<category>/<name>`` — matching where
  hermes' builtin sync keeps these skills on this fleet (skill_guard's
  ``category_for`` reuses an existing category when one is present).
* Guard-first (``skill_guard.guard_write`` / ``skill_guard.resolution``):
  never write when the name resolves from any other path — report
  ``skipped: name already present at <path> (would create collision)``.
* Idempotent: a home that already resolves the name (at our target or
  elsewhere) is a no-op, so re-running never churns. hscc does not
  "refresh" a copy that resolves — that copy may be operator-edited or
  hermes-sync-managed (``.bundled_manifest`` provenance exists for hermes'
  own sync; hscc deliberately does not touch resolved copies), and content
  churn against another writer's layout is exactly what created June's
  collision. ``origin_hashes`` remains available for reporting provenance.
* NEVER delete an operator file: the only removals are ``--replace-top-level``
  moves, and each one is backed up first, OUTSIDE the scanned skills dir.
  hscc reports collisions; the operator decides.
* ``--dry-run`` prints the plan — including every guard decision — and writes
  nothing at all.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import backup_util
    import skill_guard
except ImportError:  # pragma: no cover - import-path robustness only
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backup_util
    import skill_guard

REVIEW_SKILL = "sdlc-review"
#: Skills this installer may place: the dispatcher-injected review skill plus
#: the two names that collided in June (card acceptance set). Absence of the
#: extras is only *fixed* for reviewer-capable homes (REVIEWER-ROLE SET);
#: ``--skill`` selects names explicitly.
INSTALLABLE = skill_guard.GUARDED_SKILLS
#: Where hermes' own sync records provenance for a synced copy (v2
#: ``name:hash`` lines) — the only evidence at install time that a differing
#: copy was never operator-edited.
MANIFEST_NAME = ".bundled_manifest"
#: Profiles created with `--no-skills` carry this marker; seeding them would
#: fight the operator's explicit choice (mirrors tools/skills_sync.py).
NO_BUNDLED_MARKER = ".no-bundled-skills"
_RUNTIME_SUFFIXES = {".pyc", ".pyo"}
_RUNTIME_DIRS = {"__pycache__"}


# ── discovery ───────────────────────────────────────────────────────────────

def default_home() -> Path:
    """The Hermes home this installer should act on."""
    return Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")).expanduser()


def bundle_source(name=REVIEW_SKILL, repo_root=None):
    """Locate the vendored ``name`` skill source in this install tree.

    Walks up from this file looking for ``install/hscc-skills/<name>``, the
    same convention ``hscc-skills/hscc.py:_find_skills_source`` uses, so it
    works from a git checkout, the deployed ``$HERMES_HOME/plugins`` copy and
    a brew libexec tree alike. Returns None when the tree ships without it.
    """
    candidates = []
    if repo_root:
        candidates.append(Path(repo_root).expanduser())
    here = Path(__file__).resolve().parent
    candidates += [here.parent, here.parent.parent]
    plugins = os.environ.get("PLUGINS")
    if plugins:
        candidates.append(Path(plugins).expanduser())
    for root in candidates:
        cand = root / "install" / "hscc-skills" / name
        if cand.is_dir():
            return cand
    return None


def skill_dirs(home):
    """``[(label, skills_dir, profile_home)]`` — the root home plus every
    named profile.

    Hermes resolves a profile spawn against ``<home>/profiles/<name>/skills``,
    NOT the root home, so "installed" must mean "present in each home that can
    spawn". Dot-prefixed entries are skipped: ``profiles/.deleted`` and
    friends are not profiles (the same trap that made `hscc verify` false-fail
    in t_1c545ebb).
    """
    home = Path(home).expanduser()
    out = [("default", home / "skills", home)]
    profiles = home / "profiles"
    if profiles.is_dir():
        try:
            children = sorted(profiles.iterdir())
        except OSError:
            return out
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            out.append((child.name, child / "skills", child))
    return out


# ── content identity ────────────────────────────────────────────────────────

def dir_hash(directory):
    """MD5 over package paths + bytes, skipping disposable runtime state.

    Deliberately the SAME encoding as hermes' ``tools/skills_sync._dir_hash``
    (relative path string, then file bytes, sorted by path): a copy hashed here
    is directly comparable with the origin hash hermes records in
    ``.bundled_manifest``, which is what lets us tell "stale but unedited" from
    "the operator edited this". Verified against the live manifest entry
    ``sdlc-review:4a4be11e...``.
    """
    directory = Path(directory)
    hasher = hashlib.md5()
    if not directory.is_dir():
        return None
    for fpath in sorted(directory.rglob("*")):
        rel = fpath.relative_to(directory)
        if any(part in _RUNTIME_DIRS for part in rel.parts):
            continue
        if not fpath.is_file() or fpath.suffix in _RUNTIME_SUFFIXES:
            continue
        try:
            hasher.update(str(rel).encode("utf-8"))
            hasher.update(fpath.read_bytes())
        except OSError:
            return None
    return hasher.hexdigest()


def origin_hashes(skills_dir):
    """``{skill_name: origin_hash}`` from a ``.bundled_manifest``; {} if absent."""
    path = Path(skills_dir) / MANIFEST_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    out = {}
    for line in lines:
        name, sep, digest = line.strip().partition(":")
        if sep and name.strip():
            out[name.strip()] = digest.strip()
    return out


def resolve_verdict(skills_dir, name=REVIEW_SKILL):
    """What the BARE ``name`` resolves to for one home, via the shared
    resolver mirror in ``skill_guard`` (the home's own skills dir plus
    external dirs from its own config).

    Returns ``("resolves"|"resolves-duplicate"|"ambiguous"|"not-found", path|None)``
    — the vocabulary the plan/summary code has always used, measured against
    the real resolver with fabricated homes (2026-10-27/28).
    """
    roots = skill_guard.profile_skill_roots(Path(skills_dir).parent)
    res = skill_guard.resolution(roots, name)
    if res["state"] == "missing":
        return "not-found", None
    if res["state"] == "collision":
        return "ambiguous", None
    if res["identical_copies"]:
        return "resolves-duplicate", Path(res["paths"][0])
    return "resolves", Path(res["paths"][0])


# ── planning / applying ─────────────────────────────────────────────────────

def _backups_dir(skills_dir):
    """Sibling of the scanned skills dir — never INSIDE it.

    A ``.bak-*`` copy left under ``skills/`` would sit in hermes' scan root and
    surface as a junk skill; ``plugins-backups/`` (install_payload) is the
    precedent for keeping backups outside what gets scanned.
    """
    return Path(skills_dir).parent / "skills-backups"


def _install_into(src, target, *, backup, stamp, backups_dir):
    """Replace ``target`` with ``src`` (backing up first). Returns backup name."""
    backed_up = None
    if target.exists():
        if backup:
            backups_dir.mkdir(parents=True, exist_ok=True)
            bak = backups_dir / f"{target.parent.name}-{target.name}.bak-{stamp}"
            shutil.move(str(target), str(bak))
            backed_up = bak.name
            backup_util.prune_backup_dir(backups_dir)
        else:
            shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target)
    return backed_up


def plan_home(label, skills_dir, name, bundle, *, dry_run, backup, stamp,
              profile_home=None, reviewer_only=False):
    """One home's plan for ONE skill: guard decision + category action.

    The card rule that shapes every branch here:
    * name resolves from ANOTHER path in this home's scan set  -> never write
      (``skip-collision`` when hermes would refuse, ``noop-resolves-elsewhere``
      when it already resolves fine);
    * name already resolves AT the intended target              -> no-op
      (the card: "installing into a profile that already resolves it once is
      a no-op"); even when its content differs from the bundle, hscc does not
      overwrite a copy that resolves — that copy may be operator-edited or
      hermes-sync-managed, and churn here is what created June's collision;
    * name resolves NOWHERE                                      -> first
      install proceeds (subject to the reviewer-only policy below).

    ``reviewer_only`` restricts *installs into homes that lack the skill* to
    the REVIEWER-ROLE SET (``skill_guard.is_reviewer_capable``): a
    non-reviewer home that is simply missing the skill is reported
    ``skipped-not-reviewer`` (informational) — the card forbids nagging or
    installing for profiles that never review. Guard decisions apply to every
    home regardless.

    Actions:
      ``write`` / ``installed``          first install (name resolves nowhere)
      ``noop-resolves-at-target``        already installed where we would put it
      ``noop-resolves-elsewhere``        already resolves from another path
      ``skip-collision``                 hermes would refuse the name (the card
                                         rule; ``reason`` lists ALL paths)
      ``skipped-not-reviewer``           non-reviewer home missing the skill
      ``skipped-local-modified``         (defensive) target exists but is
                                         unreadable for hashing — left alone
      ``would-*``                        same decision under ``--dry-run``
    """
    roots = [Path(skills_dir)] + (
        skill_guard.external_dirs_for_home(profile_home) if profile_home else [])
    category = skill_guard.category_for(name, *roots)
    target_rel = Path(category) / name
    target = Path(skills_dir) / target_rel
    res = skill_guard.resolution(roots, name)

    plan = {
        "profile": label,
        "skills_dir": str(skills_dir),
        "skill": name,
        "target": str(target),
        "resolution": res["state"],
        "action": None,
        "reason": "",
        "backup": None,
    }

    if res["state"] == "collision":
        # The card's canonical guard message — includes every colliding path so
        # the operator can decide without re-running the scan by hand.
        plan["action"] = "skip-collision"
        plan["reason"] = ("skipped: name already present at "
                          + ", ".join(res["paths"]) + " (would create collision)")
    elif res["state"] == "ok":
        resolved = {os.path.realpath(p) for p in res["paths"]}
        if os.path.realpath(str(target)) in resolved:
            plan["action"] = "noop-resolves-at-target"
            plan["reason"] = "already installed at target (no-op)"
            if target.is_dir() and dir_hash(target) != dir_hash(bundle):
                plan["reason"] = ("resolves at target with content differing "
                                  "from the bundle — left as-is (hscc never "
                                  "overwrites a resolving copy)")
        else:
            plan["action"] = "noop-resolves-elsewhere"
            plan["reason"] = ("skipped: name already present at "
                              + ", ".join(res["paths"])
                              + " (would create collision)")
    elif reviewer_only and not (label == "default"
                                or skill_guard.is_reviewer_capable(label)):
        plan["action"] = "skipped-not-reviewer"
        plan["reason"] = "non-reviewer profile missing the skill (informational)"
    else:
        # Belt-and-braces: resolution() already said missing, but the write
        # goes through the same guard the doctor/tests pin, so a future
        # divergence fails closed instead of writing.
        guard = skill_guard.guard_write(skills_dir, name, str(target_rel),
                                        extra_roots=roots[1:])
        if not guard["write"]:
            plan["action"] = "skip-collision"
            plan["reason"] = guard["reason"]
        elif target.exists() and dir_hash(target) is None:
            plan["action"] = "skipped-local-modified"
            plan["reason"] = "target exists but is unreadable — left untouched"
        else:
            plan["action"], plan["reason"] = "write", "not-installed"

    if dry_run:
        plan["action"] = "would-" + plan["action"]
    elif plan["action"] == "write":
        plan["backup"] = _install_into(bundle, target, backup=backup,
                                       stamp=stamp,
                                       backups_dir=_backups_dir(skills_dir))
        plan["action"] = "installed"
    # Every other action writes nothing, ever.

    verdict, where = resolve_verdict(skills_dir, name)
    plan["bare_name_resolves"] = verdict
    plan["resolves_to"] = str(where) if where else None
    return plan


def install_review_skill(home=None, repo_root=None, *, dry_run=False,
                         backup=True, stamp=None, replace_top_level=False,
                         skills=(REVIEW_SKILL,), reviewer_only=True):
    """Fan the bundled review-lane skills out across ``home`` — guarded.

    Returns a report dict. ``skills`` selects which bundled names to place
    (default: ``sdlc-review`` only — the dispatcher force-injection; pass
    ``skill_guard.GUARDED_SKILLS`` to place the full acceptance set).
    ``reviewer_only=True`` restricts installs into homes that LACK the skill
    to reviewer-capable profiles (the ``*-orch`` set + the reviewer role
    profile); collision reporting and the guard run for every home either
    way, and homes that already resolve the skill are no-ops regardless.

    ``replace_top_level`` (opt-in): move a flat ``<skills>/<name>`` copy out
    of the way (backed up, never deleted) when it makes the bare name
    ambiguous. Off by default because that flat copy is usually HSCC's OWN
    earlier install — an operator may have edited it, and choosing between two
    different skills with the same name is the operator's call, not a deploy
    step's.
    """
    home = Path(home or default_home()).expanduser()
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {
        "ok": False, "home": str(home), "skills": list(skills),
        "sources": {}, "dry_run": bool(dry_run),
        "targets": [], "counts": {}, "error": None,
    }
    sources = {}
    for name in skills:
        bundle = bundle_source(name, repo_root)
        if bundle is None or not (bundle / "SKILL.md").is_file():
            report["error"] = (
                f"bundled skill source not found for {name!r} (looked for "
                f"install/hscc-skills/{name} above "
                f"{Path(__file__).resolve().parent})")
            return report
        sources[name] = bundle
    report["sources"] = {k: str(v) for k, v in sources.items()}

    # The root home's manifest is no longer consulted: every non-write path
    # here is a no-op/skip, so the provenance-vs-bundle comparison it fed is
    # gone (a copy that RESOLVES is never overwritten, edited or not).
    for label, skills_dir, profile_home in skill_dirs(home):
        marker = Path(skills_dir) / NO_BUNDLED_MARKER
        if marker.exists():
            report["targets"].append({
                "profile": label, "skills_dir": str(skills_dir),
                "skill": "+".join(skills),
                "action": "skipped-no-bundled-skills",
                "reason": "profile created with --no-skills",
                "bare_name_resolves": resolve_verdict(skills_dir, REVIEW_SKILL)[0],
            })
            continue
        for name in skills:
            plan = plan_home(label, skills_dir, name, sources[name],
                             dry_run=dry_run, backup=backup, stamp=stamp,
                             profile_home=profile_home,
                             reviewer_only=reviewer_only)
            if replace_top_level:
                plan["top_level"] = _plan_top_level(
                    label, skills_dir, name, plan, dry_run=dry_run,
                    stamp=stamp, backup=backup)
                if plan["top_level"].get("action", "").endswith("-top-level"):
                    # The colliding flat copy moved aside; re-read the verdict.
                    plan["bare_name_resolves"], plan["resolves_to"] = (
                        resolve_verdict(skills_dir, name))
            report["targets"].append(plan)

    counts = {}
    for plan in report["targets"]:
        counts[plan["action"]] = counts.get(plan["action"], 0) + 1
    report["counts"] = counts
    # ok == nothing is left unresolvable: a home whose bare name stays
    # ambiguous (collision) — or, for the dispatcher-injected review skill,
    # not-found — is a real failure even if nothing was written. A
    # non-reviewer home missing an EXTRA skill is informational (the install
    # deliberately skipped it), never a failure.
    reasons: dict[str, set[str]] = {}
    for p in report["targets"]:
        verdict = p.get("bare_name_resolves")
        if verdict == "ambiguous":
            reasons.setdefault(p["profile"], set()).add("collision")
        elif verdict == "not-found" and p.get("skill") == REVIEW_SKILL:
            reasons.setdefault(p["profile"], set()).add("missing")
    report["blocked"] = sorted(f"{name} ({'+'.join(sorted(states))})"
                               for name, states in reasons.items())
    report["ok"] = not report["blocked"]
    return report


def _plan_top_level(label, skills_dir, name, plan, *, dry_run, stamp, backup):
    """Decide about the FLAT ``<skills>/<name>`` copy under --replace-top-level.

    Only ever MOVED (backed up, outside the scan root), never deleted — and
    only when the flat copy and the category copy DIFFER: that is the state
    hermes refuses to resolve ("Skill name collision"), which is the crash
    this card fixes. Byte-identical copies RESOLVE (hermes'
    ``_provably_same_skill`` ranks same-root copies and loads the shallowest),
    so moving one would change nothing and only churn the tree; and a flat
    copy with no category sibling is the sole resolution, so moving it would
    BREAK the home, not fix it.
    """
    skills_dir = Path(skills_dir)
    flat = skills_dir / name
    out = {"profile": label, "flat": str(flat), "action": "none", "backup": None}
    if not (flat / "SKILL.md").is_file():
        return out
    target = Path(plan["target"])
    if not (target / "SKILL.md").is_file():
        out["action"] = "skipped-no-category-copy"
        return out
    if dir_hash(target) == dir_hash(flat):
        out["action"] = "skipped-identical-duplicate"
        return out
    out["action"] = ("would-move" if dry_run else "moved") + "-top-level"
    if not dry_run:
        backups = _backups_dir(skills_dir)
        backups.mkdir(parents=True, exist_ok=True)
        bak = backups / f"{name}.flat.bak-{stamp}"
        shutil.move(str(flat), str(bak))
        backup_util.prune_backup_dir(backups)
        out["backup"] = bak.name
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────

def _summary_lines(report):
    lines = []
    prefix = "plan" if report.get("dry_run") else "done"
    if report.get("error"):
        lines.append(f"[review-skill] ERROR: {report['error']}")
        return lines
    src_bits = ", ".join(f"{k}={v}" for k, v in sorted(report["sources"].items()))
    lines.append(
        f"[review-skill] {prefix}: {'+'.join(report['skills'])} guarded "
        f"across {len(report['targets'])} home x skill decisions ({src_bits})")
    for plan in report["targets"]:
        mark = plan["action"]
        detail = plan.get("reason", "")
        if plan.get("backup"):
            detail += f" (backup {plan['backup']})"
        verdict = plan.get("bare_name_resolves", "?")
        extra = "" if verdict == "resolves" else f"  bare-name={verdict}"
        top = plan.get("top_level") or {}
        if top.get("action", "none") not in ("none",):
            extra += f"  flat-copy={top['action']}"
        lines.append(f"  {mark:28s} {plan['profile']:24s} "
                     f"{plan.get('skill', ''):22s} {detail}{extra}")
    for label, count in sorted(report["counts"].items()):
        lines.append(f"  {label}: {count}")
    if report.get("blocked"):
        lines.append(
            "  ⚠ still unresolvable: " + ", ".join(report["blocked"]) +
            " — two different skills share the name; decide which one wins "
            "(see --replace-top-level), do NOT guess in a deploy step.")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Guarded install of the review-lane skills into every "
                    "Hermes profile's own skills dir (collision-guarded, "
                    "idempotent, never clobbers an operator-edited copy).")
    parser.add_argument("--home", default=None,
                        help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    parser.add_argument("--repo-root", default=None,
                        help="root holding install/hscc-skills (default: auto)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan (guard decisions included), write nothing")
    parser.add_argument("--no-backup", action="store_true",
                        help="overwrite without a .bak copy (still skips edited copies)")
    parser.add_argument("--replace-top-level", action="store_true",
                        help="move a flat <skills>/<skill> copy aside (backed up) "
                             "so the category copy is unambiguous")
    parser.add_argument("--skill", action="append", default=None,
                        help="skill name to install (repeatable; default: "
                             f"{REVIEW_SKILL}; 'all' = {','.join(INSTALLABLE)})")
    parser.add_argument("--all-profiles", action="store_true",
                        help="install the requested skills into EVERY profile "
                             "(default: missing-skill installs go only to "
                             "reviewer-capable profiles)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    skills = args.skill or [REVIEW_SKILL]
    if "all" in skills:
        skills = list(INSTALLABLE)
    unknown = [s for s in skills if s not in INSTALLABLE]
    if unknown:
        parser.error(f"skills not vendored for guarded install: {unknown} "
                     f"(installable: {list(INSTALLABLE)})")

    report = install_review_skill(
        home=args.home, repo_root=args.repo_root, dry_run=args.dry_run,
        backup=not args.no_backup, replace_top_level=args.replace_top_level,
        skills=tuple(skills), reviewer_only=not args.all_profiles)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in _summary_lines(report):
            print(line)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
