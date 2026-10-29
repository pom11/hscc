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
#: CANONICAL PATH IS THE PROFILE HOME (`<profile>/.no-bundled-skills`, written
#: by hermes_cli/profiles.py:1349 and read by tools/skills_sync.py:368,468) —
#: not inside skills/. Round-2 review B3: the first cut checked the skills/
#: position only, which nothing upstream reads (dead guard).
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

    The root label is ``"default (root)"`` (round-2 review N1): a real profile
    may be named ``default``, and labelling the root home the same made
    ``report["blocked"]`` ambiguous about WHICH tree was broken.
    """
    home = Path(home).expanduser()
    out = [("default (root)", home / "skills", home)]
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
    external dirs from its own config, minus ``skills.disabled``).

    Returns ``("resolves"|"resolves-duplicate"|"ambiguous"|"disabled"|"not-found", path|None)``
    — the vocabulary the plan/summary code has always used, extended with
    ``disabled`` (round-2 review S2: a name the operator listed under
    ``skills.disabled`` loads off-disk but ``build_preloaded_skills_prompt``
    counts as MISSING for a preload spawn, so a spawn dies with the card's
    error string even though the tree holds the skill).
    """
    home = Path(skills_dir).parent
    roots = skill_guard.profile_skill_roots(home)
    res = skill_guard.resolution(roots, name,
                                 disabled=skill_guard.disabled_skills_for_home(home))
    if res["state"] == "missing":
        return "not-found", None
    if res["state"] == "collision":
        return "ambiguous", None
    if res["state"] == "disabled":
        return "disabled", Path(res["paths"][0])
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


def _install_into(src, target, *, stamp, backups_dir):
    """Replace ``target`` with ``src``, ALWAYS backing up first.

    Round-2 review S3: the first cut honoured ``backup=False`` with
    ``shutil.rmtree(target)`` — a real deletion on the one path whose contract
    is 'no deletions ever' (card body; module docstring). Backing up is now
    unconditional: the reachable case is a pre-existing ``target`` directory
    without a SKILL.md (a half-written copy the scanner sees as 'missing'),
    and moving it aside into ``backups_dir`` (OUTSIDE the scan root) costs
    nothing and keeps the operator's bytes recoverable. There is no code path
    here that removes content without a backup copy.
    """
    backed_up = None
    if target.exists():
        backups_dir.mkdir(parents=True, exist_ok=True)
        bak = backups_dir / f"{target.parent.name}-{target.name}.bak-{stamp}"
        shutil.move(str(target), str(bak))
        backed_up = bak.name
        backup_util.prune_backup_dir(backups_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target)
    return backed_up


def plan_home(label, skills_dir, name, bundle, *, dry_run, stamp,
              profile_home=None, reviewer_only=False, honor_opt_out=False):
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
      ``skip-disabled``                  present but listed under
                                         ``skills.disabled`` (S2): a preload
                                         spawn still counts it missing, and a
                                         second copy would be a collision — so
                                         hscc reports instead of writing, and
                                         the operator removes the config entry
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
    disabled = (skill_guard.disabled_skills_for_home(profile_home)
                if profile_home else frozenset())
    res = skill_guard.resolution(roots, name, disabled=disabled)

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
        if res.get("equal_rank"):
            plan["reason"] += (" — identical copies at equal rank: hermes ranks "
                               "same-root copies only when their depths differ, "
                               "so it refuses this name")
    elif res["state"] == "disabled":
        plan["action"] = "skip-disabled"
        plan["reason"] = ("present but listed under skills.disabled — a preload "
                          "spawn still counts it missing; hscc does not undo an "
                          "operator opt-out (remove the name from "
                          "config.yaml to restore)")
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
    elif reviewer_only and not (label == "default (root)"
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
    if honor_opt_out and plan["action"] in ("write", "would-write"):
        # DECIDED HERE, BEFORE THE WRITE (round-2 review B3, the actual defect
        # behind "wrote sdlc-review anyway"): a caller CANNOT suppress a write
        # it learns about after the fact — by the time plan_home returns, the
        # copy is on disk and the action reads ``installed``, so a caller-side
        # rewrite only relabels a write that already happened (measured: a
        # marker-carrying profile with the full trio got all three skills
        # written while the report claimed them skipped). The marker's whole
        # effect must therefore be an input to this decision.
        plan["action"] = ("would-skip-no-bundled-skills" if dry_run
                          else "skipped-no-bundled-skills")
        plan["reason"] = (f"home carries {NO_BUNDLED_MARKER} (canonical "
                          f"<home>/ path) — bundled seeding opt-out honoured; "
                          f"hscc writes nothing here")
    elif plan["action"] == "write":
        plan["backup"] = _install_into(bundle, target, stamp=stamp,
                                       backups_dir=_backups_dir(skills_dir))
        plan["action"] = "installed"
    # Every other action writes nothing, ever.

    verdict, where = resolve_verdict(skills_dir, name)
    plan["bare_name_resolves"] = verdict
    plan["resolves_to"] = str(where) if where else None
    return plan


def install_review_skill(home=None, repo_root=None, *, dry_run=False,
                         stamp=None, replace_top_level=False,
                         skills=(REVIEW_SKILL,), reviewer_only=True):
    """Fan the bundled review-lane skills out across ``home`` — guarded.

    Returns a report dict. ``skills`` selects which bundled names to place
    (default: ``sdlc-review`` only — the dispatcher force-injection; pass
    ``skill_guard.GUARDED_SKILLS`` to place the full acceptance set).
    ``reviewer_only=True`` restricts installs into homes that LACK the skill
    to reviewer-capable profiles (the ``*-orch`` set + the reviewer role
    profile); collision reporting and the guard run for every home either
    way, and homes that already resolve the skill are no-ops regardless.

    There is deliberately NO ``--no-backup`` escape hatch (S3): every write
    that replaces anything is backed up first, because a replacing write
    without a backup is a deletion and the contract here is 'no deletions
    ever'.

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
        # B3 (round-2 review, STATED DECISION): the opt-out marker lives at the
        # CANONICAL ``<profile home>/.no-bundled-skills`` path
        # (hermes_cli/profiles.py:1349 writes it, tools/skills_sync.py:368,468
        # reads it via ``_hermes_home()``), not inside ``skills/`` — the first
        # cut checked the dead skills/ position. Where the marker BINDS and
        # where it is OVERRIDDEN is a policy call, made per home:
        #
        #   * REVIEWER-ROLE SET (``*-orch`` + ``reviewer`` + the root home):
        #     overridden for the WHOLE guarded trio. Grounding — hscc-roles
        #     creates every orchestrator with ``create_profile(no_skills=True)``
        #     (generator.py:470), the exact call that WRITES the marker
        #     (profiles.py ``_finish_profile_layout``): inside an hscc-minted
        #     ``*-orch`` the marker is HSCC's OWN footprint, not an operator
        #     opt-out. Honouring it there would (a) leave every orchestrator
        #     unable to answer the dispatcher's force-injected review spawn
        #     (kanban_db_dispatch.py:2073-2075) and (b) fail the card's own
        #     acceptance — the repro must print an EMPTY missing-list for all
        #     THREE names across the five card profiles, and the two
        #     hand-fixed ``ecofire-*-orch`` carry the marker (measured
        #     2026-10-29: they were exactly the two failing homes).
        #   * everywhere else (a non-reviewer home, reachable only via
        #     --all-profiles): the marker BINDS for every name — that home has
        #     no review-readiness obligation and the operator's ``--no-skills``
        #     is the expressed wish there. Without this clause the marker would
        #     guard nothing at all under the default reviewer-only fan-out,
        #     which is precisely the dead-guard defect the review flagged.
        #
        # The collision guard still decides every write, and doctor reports an
        # opted-out reviewer-capable home that cannot resolve the injected name
        # RED — never silent.
        opted_out = skill_guard.bundled_seeding_opted_out(profile_home)
        reviewer_home = (label == "default (root)"
                         or skill_guard.is_reviewer_capable(label))
        # Where the marker BINDS, it binds before anything is written (see the
        # honor_opt_out note in plan_home).
        honor_marker = opted_out and not reviewer_home
        if honor_marker and all(s != REVIEW_SKILL for s in skills):
            report["targets"].append({
                "profile": label, "skills_dir": str(skills_dir),
                "skill": "+".join(skills),
                "action": "skipped-no-bundled-skills",
                "reason": f"home carries {NO_BUNDLED_MARKER} (seeding opt-out "
                          f"honoured — non-reviewer home)",
                "opted_out": True,
                "bare_name_resolves": resolve_verdict(skills_dir, REVIEW_SKILL)[0],
            })
            continue
        for name in skills:
            plan = plan_home(label, skills_dir, name, sources[name],
                             dry_run=dry_run, stamp=stamp,
                             profile_home=profile_home,
                             reviewer_only=reviewer_only,
                             honor_opt_out=honor_marker)
            if opted_out:
                plan["opted_out"] = True
                if reviewer_home:
                    plan["opt_out_override"] = (
                        f"{NO_BUNDLED_MARKER} is present but overridden for the "
                        f"reviewer-capable home: the marker was minted by "
                        f"hscc's own create_profile(no_skills=True), and "
                        f"review readiness for {name} wins (see module docstring)")
            if replace_top_level:
                plan["top_level"] = _plan_top_level(
                    label, skills_dir, name, plan, dry_run=dry_run,
                    stamp=stamp)
                if plan["top_level"].get("action", "").endswith("-top-level"):
                    # The colliding flat copy moved aside; re-read the verdict.
                    plan["bare_name_resolves"], plan["resolves_to"] = (
                        resolve_verdict(skills_dir, name))
            report["targets"].append(plan)

    counts = {}
    for plan in report["targets"]:
        counts[plan["action"]] = counts.get(plan["action"], 0) + 1
    report["counts"] = counts
    # ok == nothing is left unresolvable WHERE IT MATTERS: a collision blocks
    # anywhere (it breaks the bare name for any spawn asking it), while a
    # missing review skill only counts against reviewer-capable homes + the
    # root (the same REVIEWER-ROLE SET the doctor check uses — the card
    # forbids treating a non-reviewer profile's absence as a failure). A
    # non-reviewer home missing an EXTRA skill is informational, never
    # blocked. A name the operator listed under ``skills.disabled`` counts as
    # unresolvable too (S2: build_preloaded_skills_prompt treats disabled as
    # missing), for reviewer-capable homes only.
    reasons: dict[str, set[str]] = {}
    for p in report["targets"]:
        verdict = p.get("bare_name_resolves")
        reviewer_home = (p["profile"] == "default (root)"
                         or skill_guard.is_reviewer_capable(p["profile"]))
        if verdict == "ambiguous":
            reasons.setdefault(p["profile"], set()).add("collision")
        elif verdict == "disabled" and p.get("skill") == REVIEW_SKILL \
                and reviewer_home:
            reasons.setdefault(p["profile"], set()).add("disabled")
        elif verdict == "not-found" and p.get("skill") == REVIEW_SKILL \
                and reviewer_home:
            reasons.setdefault(p["profile"], set()).add("missing")
    report["blocked"] = sorted(f"{name} ({'+'.join(sorted(states))})"
                               for name, states in reasons.items())
    report["ok"] = not report["blocked"]
    return report


def _plan_top_level(label, skills_dir, name, plan, *, dry_run, stamp):
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
        colliding = [b for b in report["blocked"] if "collision" in b]
        missing = [b for b in report["blocked"]
                   if "missing" in b and "disabled" not in b]
        disabled = [b for b in report["blocked"] if "disabled" in b]
        if colliding:
            lines.append(
                "  ⚠ collision (two different skills share the name — hscc "
                "will not guess which wins): " + ", ".join(colliding) +
                " — decide by hand, or move HSCC's own stale flat copy aside "
                "with --replace-top-level (backed up, never deleted).")
        if missing:
            lines.append(
                "  ⚠ review skill still missing for: " + ", ".join(missing) +
                " — the guarded install skipped these; re-run without "
                "--dry-run to place them (missing installs go to "
                "reviewer-capable profiles + the root home only).")
        if disabled:
            lines.append(
                "  ⚠ review skill present but DISABLED for: "
                + ", ".join(disabled) + " — remove the name from that home's "
                "config.yaml (skills.disabled); hscc never undoes an operator "
                "opt-out, but the spawn would still die on it.")
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
        replace_top_level=args.replace_top_level,
        skills=tuple(skills), reviewer_only=not args.all_profiles)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in _summary_lines(report):
            print(line)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
