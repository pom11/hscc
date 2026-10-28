"""Collision guard + reviewer-readiness helpers for HSCC skill installs.

Why this exists (card t_bc611809)
--------------------------------
The kanban dispatcher force-appends ``sdlc-review`` to every REVIEW-lane spawn
(``hermes_cli/kanban_db_dispatch.py`` ``lane == "review"`` -> ``claimed.skills
= [..., "sdlc-review"]``) for ANY assignee profile, and spawns with
``--skills sdlc-review``. When that name fails to load,
``hermes_cli/oneshot.py:_build_preloaded_skills_prompt`` raises
``ValueError: Unknown skill(s): sdlc-review`` -> worker exits 1 -> dispatcher
counts a crash. Two distinct profile-level states produce that one error
string, both measured on this host (2026-10-27/28):

  (a) NAME COLLISION — the profile's skills tree holds TWO different skills
      named ``sdlc-review`` (a stale flat copy from an old HSCC bootstrap at
      ``<skills>/sdlc-review``, plus the builtin category copy at
      ``<skills>/devops/sdlc-review``). Hermes' resolver
      (``tools/skills_tool._locate_skill``) refuses to guess
      ("Skill name collision ... refusing to guess"), the load fails, the name
      lands in the ``missing`` list.
  (b) GENUINE ABSENCE — the profile never had the skill at all
      (ecofire-bc-orch: review-lane spawns died 3x, dispatcher gave_up).

Hermes' exact resolution rule, read from source (2026-10-28) — replicated by
``scan_skill_names`` / ``resolution`` below:

  * candidates are collected by DIRECTORY NAME (or frontmatter ``name:``) for
    every ``SKILL.md`` under EVERY search dir, at ANY depth
    (``_collect_skill_candidates`` + ``agent.skill_utils.iter_skill_index_files``);
  * more than one candidate -> refusal, UNLESS all candidates live under ONE
    search root AND are provably the same skill: one realpath (symlink view)
    or byte-identical ``SKILL.md`` (``_provably_same_skill``);
  * legacy flat ``<name>.md`` files anywhere under a root also count as
    candidates.

Search roots for a profile spawn (``_skill_search_dirs``):
``<profile HERMES_HOME>/skills`` plus ``skills.external_dirs`` from the
profile's own ``config.yaml`` (the root ``~/.hermes/skills`` is NOT a fallback
— a profile session resolves ONLY against its own tree). Same-identical-copies
inside ONE root resolve fine, so the guard is content-aware: an HSCC flat
install is skipped only when another root holds the name with DIFFERENT
content (see ``plan_bundled_skill_writes``).

The module exposes three things:

* ``profile_skill_inventory`` / ``scan_skill_names`` — a read-only scan that
  mirrors the resolver's candidate rules for one skills root.
* ``reviewer_readiness`` — doctor finding (ii): does a reviewer-capable
  profile resolve a name EXACTLY ONCE (ok) / zero times (missing) / more than
  once (collision), with every colliding path listed.
* ``plan_bundled_skill_writes`` — the COLLISION GUARD every bundled-skill
  writer must pass through: a name is never written into a tree whose scan set
  already resolves it from another root with different content.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

#: The skill the dispatcher force-injects for review-lane spawns. Any profile
#: can be handed a review card, so every profile that CAN review must resolve
#: it exactly once.
REVIEW_SKILL = "sdlc-review"

#: Names hermes' scanner prunes (mirrors agent.skill_utils.EXCLUDED_SKILL_DIRS
#: + the org mirror gate). Kept as a local copy on purpose: hscc-bootstrap has
#: no hermes import at runtime and must work from the deployed plugins tree.
EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups", ".locks",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))
ORG_MIRROR_DIR_NAME = "_org"
ORG_ACTIVE_MARKER = ".active_org"

#: Profiles whose name marks them reviewer-capable: per-project orchestrators
#: are the fleet's review-lane reviewers (their cards go through
#: kanban_request_review) and the hand-written reviewer role profile.
REVIEWER_NAME_SUFFIX = "-orch"
REVIEWER_ROLE_PROFILES = frozenset({"reviewer"})


# ── resolver mirror: candidate scan ─────────────────────────────────────────

def _frontmatter_name(skill_md: Path) -> str | None:
    """The ``name:`` value in a SKILL.md's YAML frontmatter, best-effort.

    Hermes counts a SKILL.md as a candidate for a bare name when its
    frontmatter ``name:`` matches, not only when the directory matches — so a
    faithful scan must read the same field. Deliberately regex-simple (no
    yaml import): anything unparseable yields None and the frontmatter-name
    candidate is not counted, which can only UNDER-count collisions (doctor
    may then read ok where hermes refuses) — never over-count, and never
    misreport a genuine collision as clean, for the common dir-name case.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    for line in text.splitlines()[1:40]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("name:"):
            val = stripped[len("name:"):].strip().strip("'\"")
            return val or None
    return None


def _walk_skill_mds(skills_dir: Path) -> list[Path]:
    """Every discoverable ``SKILL.md`` under *skills_dir*, resolver prunes applied."""
    out: list[Path] = []
    if not skills_dir.is_dir():
        return out
    skills_dir_str = str(skills_dir)
    org_root = os.path.join(skills_dir_str, ORG_MIRROR_DIR_NAME)
    active_org = None
    marker = Path(org_root) / ORG_ACTIVE_MARKER
    if marker.exists():
        try:
            active_org = (marker.read_text(encoding="utf-8").strip() or None)
        except OSError:
            active_org = None
    for root, dirs, files in os.walk(skills_dir_str, followlinks=True):
        has_skill_md = "SKILL.md" in files
        if root == skills_dir_str and ORG_MIRROR_DIR_NAME in dirs and active_org is None:
            dirs.remove(ORG_MIRROR_DIR_NAME)
        elif root == org_root:
            dirs[:] = [d for d in dirs if d == active_org]
        dirs[:] = [d for d in dirs
                   if d not in EXCLUDED_SKILL_DIRS and not (has_skill_md and d in SKILL_SUPPORT_DIRS)]
        if "SKILL.md" in files:
            out.append(Path(root) / "SKILL.md")
    return sorted(out)


def scan_skill_names(skills_dir) -> dict:
    """``{name: [skill_dir_or_md, ...]}`` — every bare name the resolver could
    match in ONE root, faithful to ``_collect_skill_candidates`` +
    ``_is_package_owned_markdown`` + ``is_skill_support_path``:

      * ``SKILL.md`` packages register their DIRECTORY name and their
        frontmatter ``name:`` (if different);
      * legacy flat ``<stem>.md`` files register ``stem`` ONLY when no
        ancestor dir (inside the root) holds a SKILL.md — a ``.md`` inside a
        skill package is a linked file, not a candidate (this is why every
        category ``DESCRIPTION.md`` in the fleet is NOT a collision for the
        literal name ``DESCRIPTION``) — and never when it sits in a support
        dir (references/templates/scripts/assets) of a skill package.
    """
    skills_dir = Path(skills_dir)
    names: dict[str, list[Path]] = {}

    def _add(name, loc):
        names.setdefault(name, []).append(loc)

    def _support_dir_of(rel_parts):
        """True when any non-leaf component of rel_parts is a support dir
        sitting directly inside a skill package (hermes is_skill_support_path
        for paths relative to the scan root)."""
        for idx, part in enumerate(rel_parts[:-1]):
            if idx > 0 and part in SKILL_SUPPORT_DIRS:
                if (skills_dir / Path(*rel_parts[:idx]) / "SKILL.md").is_file():
                    return True
        return False

    for smd in _walk_skill_mds(skills_dir):
        _add(smd.parent.name, smd.parent)
        fm = _frontmatter_name(smd)
        if fm and fm != smd.parent.name:
            _add(fm, smd.parent)
    # Legacy flat <name>.md anywhere (a real candidate class in the resolver).
    if skills_dir.is_dir():
        for md in sorted(skills_dir.rglob("*.md")):
            if md.name == "SKILL.md":
                continue
            rel_parts = md.relative_to(skills_dir).parts
            if any(p in EXCLUDED_SKILL_DIRS for p in rel_parts):
                continue
            if _support_dir_of(rel_parts):
                continue
            # Package-owned markdown must not register (hermes:
            # _is_package_owned_markdown — any ancestor dir has SKILL.md).
            ancestor_owned = False
            for depth in range(1, len(rel_parts)):
                if (skills_dir / Path(*rel_parts[:depth]) / "SKILL.md").is_file():
                    ancestor_owned = True
                    break
            if ancestor_owned:
                continue
            _add(md.stem, md)
    return names


def external_dirs_for_home(hermes_home) -> list[Path]:
    """``skills.external_dirs`` from *hermes_home*/config.yaml (existing dirs
    only, ``~``/``${VAR}`` expanded, home-relative, local skills dir skipped) —
    the same rules as ``agent.skill_utils.get_external_skills_dirs``. A missing
    or unreadable config means no external dirs."""
    config = Path(hermes_home).expanduser() / "config.yaml"
    try:
        import yaml  # available in the hermes venv and via requirements.txt
        cfg = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    skills_cfg = cfg.get("skills") if isinstance(cfg, dict) else None
    if not isinstance(skills_cfg, dict):
        return []
    entries = skills_cfg.get("external_dirs")
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list):
        return []
    home = Path(hermes_home).expanduser()
    local = (home / "skills").resolve()
    out: list[Path] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        p = Path(os.path.expandvars(os.path.expanduser(entry.strip())))
        if not p.is_absolute():
            p = home / p
        try:
            p = p.resolve()
        except OSError:
            continue
        if p == local or p in out:
            continue
        if p.is_dir():
            out.append(p)
    return out


# ── readiness verdict ────────────────────────────────────────────────────────

def _skill_md_of(p: Path) -> Path:
    """The SKILL.md (or legacy flat .md itself) for a candidate location."""
    return p if p.suffix == ".md" else p / "SKILL.md"


def _identical(paths: list[Path]) -> bool:
    """True when every candidate's markdown is one file (same realpath) or
    byte-identical — hermes' ``_provably_same_skill`` predicate."""
    try:
        mds = [_skill_md_of(p) for p in paths]
        if len({os.path.realpath(str(m)) for m in mds}) == 1:
            return True
        return len({hashlib.sha256(m.read_bytes()).hexdigest() for m in mds}) == 1
    except OSError:
        return False


def resolution(skills_roots, name: str) -> dict:
    """How *name* resolves for a session whose scan set is *skills_roots*.

    Returns ``{"state": "ok"|"missing"|"collision", "count": int,
    "paths": [...], "identical_copies": bool}``.

    ``ok``            exactly one candidate, or several candidates that are
                      provably the SAME skill inside ONE root (hermes ranks
                      those and loads the shallowest — not a failure).
    ``missing``       zero candidates.
    ``collision``     >1 candidates that hermes would refuse: different
                      content, an equal-rank tie, or spread across roots.
    """
    candidates: list[Path] = []
    roots_seen = set()
    for root in skills_roots:
        root = Path(root)
        hits = scan_skill_names(root).get(name, [])
        candidates.extend(hits)
        if hits:
            roots_seen.add(str(root))
    # Dedupe by realpath (the same dir reached twice through roots).
    uniq: dict[str, Path] = {}
    for c in candidates:
        try:
            uniq.setdefault(os.path.realpath(str(c)), c)
        except OSError:
            uniq.setdefault(str(c), c)
    candidates = list(uniq.values())
    if not candidates:
        return {"state": "missing", "count": 0, "paths": [], "identical_copies": False}
    if len(candidates) == 1:
        return {"state": "ok", "count": 1,
                "paths": [str(candidates[0])], "identical_copies": False}
    same_root = len(roots_seen) <= 1 and _identical(candidates)
    if same_root:
        return {"state": "ok", "count": len(candidates),
                "paths": [str(c) for c in candidates], "identical_copies": True}
    return {"state": "collision", "count": len(candidates),
            "paths": [str(c) for c in candidates], "identical_copies": False}


def profile_skill_roots(profile_home) -> list[Path]:
    """The scan set for a profile session: its own ``skills`` dir plus the
    external dirs from its own config. (NOT the root home — hermes has no
    fallback from a profile HERMES_HOME to ``~/.hermes``; grounded in
    ``tools/skills_tool._skill_search_dirs`` + ``_skills_dir``.)"""
    home = Path(profile_home).expanduser()
    roots = [home / "skills"]
    roots += external_dirs_for_home(home)
    return roots


def is_reviewer_capable(profile_name: str) -> bool:
    """REVIEWER-ROLE SET membership, derived from on-disk naming (the rule
    stated in the PR): every per-project orchestrator (``*-orch`` — the fleet
    routes review cards to orchestrators, and any assignee can be handed one
    because the dispatcher force-injects the skill) plus the reviewer role
    profile itself. Deliberately NAME-based: profile.yaml on disk carries only
    a routing ``description``, no role tag (grounded: generator.py writes
    ``{description, description_auto}`` via hermes' write_profile_meta), so
    name+role-spec is the only stable on-disk signal. A profile that merely
    HAS sdlc-review is not a reviewer; a profile that lacks it is never
    nagged unless it is in this set."""
    return (profile_name.endswith(REVIEWER_NAME_SUFFIX)
            or profile_name in REVIEWER_ROLE_PROFILES)


def discover_profiles(hermes_home) -> list[tuple[str, Path]]:
    """``[(name, profile_home)]`` — every non-dot dir under
    ``<hermes_home>/profiles``."""
    home = Path(hermes_home).expanduser()
    out = []
    profiles = home / "profiles"
    if profiles.is_dir():
        try:
            for child in sorted(profiles.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    out.append((child.name, child))
        except OSError:
            pass
    return out


def duplicate_names(skills_root) -> dict:
    """``{name: [paths]}`` for every SKILL.md-derived NAME with >1 distinct
    skill dir in one root — the card's ``find ... -name SKILL.md | dirname |
    basename | uniq -d`` equivalent, widened to frontmatter ``name:`` too.

    Deliberately SKILL.md-only: legacy flat ``<stem>.md`` files (category
    ``DESCRIPTION.md`` files across the fleet) DO count as resolver candidates
    in ``resolution`` — hermes really would refuse a load of that literal
    name — but flagging every ``DESCRIPTION`` on every profile would turn
    finding (i) into pure nag noise. The names that break spawns are the ones
    with SKILL.md packages.
    """
    dup = {}
    smd_names: dict[str, list[Path]] = {}
    for smd in _walk_skill_mds(Path(skills_root)):
        smd_names.setdefault(smd.parent.name, []).append(smd.parent)
        fm = _frontmatter_name(smd)
        if fm and fm != smd.parent.name:
            smd_names.setdefault(fm, []).append(smd.parent)
    for name, locs in smd_names.items():
        uniq = {}
        for loc in locs:
            uniq.setdefault(os.path.realpath(str(loc)), loc)
        if len(uniq) > 1:
            dup[name] = [str(p) for p in uniq.values()]
    return dup


def readiness_for_home(hermes_home, *, name: str = REVIEW_SKILL) -> dict:
    """Doctor verdict for ONE home (root or profile). Read-only.

    Returns ``{"home", "label", "reviewer", "state", "paths", "duplicates"}``.
    """
    home = Path(hermes_home).expanduser()
    label = home.name if home.name != "home" else str(home)
    roots = profile_skill_roots(home)
    res = resolution(roots, name)
    dups: dict[str, list[str]] = {}
    for root in roots:
        for dn, paths in duplicate_names(root).items():
            dups.setdefault(dn, []).extend(p for p in paths if p not in dups.get(dn, []))
    return {
        "home": str(home), "label": label,
        "reviewer": is_reviewer_capable(label),
        "state": res["state"], "count": res["count"],
        "identical_copies": res["identical_copies"],
        "paths": res["paths"], "duplicates": dups,
    }


# ── the collision guard for bundled-skill writes ────────────────────────────

#: The names whose absence/collision killed review-lane spawns (card
#: acceptance set). ``sdlc-review`` is dispatcher-force-injected for review
#: cards; the reviewer role profile preloads the other two
#: (hscc-roles/roles/reviewer.yaml), so a reviewer-capable profile missing any
#: of them can crash a spawn.
GUARDED_SKILLS = ("sdlc-review", "systematic-debugging", "test-driven-development")

#: Category dirs the guarded installer writes into, mirroring where hermes'
#: OWN builtin sync keeps these skills on this fleet (measured 2026-10-28:
#: ``skills/devops/sdlc-review`` from the hermes-agent bundle,
#: ``skills/software-development/{systematic-debugging,test-driven-development}``).
#: Writing the same path the builtin sync reconciles means the two writers
#: converge on one copy instead of creating a name collision — the June
#: flat-layout mistake was writing a THIRD layout nobody else reconciles.
CATEGORY_BY_NAME = {
    "sdlc-review": "devops",
    "systematic-debugging": "software-development",
    "test-driven-development": "software-development",
}
DEFAULT_CATEGORY = "devops"


def category_for(name: str, *scan_roots) -> str:
    """Category dir to write *name* under for these roots.

    Host-consistent: if any root already holds the name under a category
    (single-segment path, e.g. ``devops/sdlc-review``), reuse that category so
    the installer never invents a second layout. Only a fresh install with no
    existing category copy falls back to the documented mapping above.
    """
    for root in scan_roots:
        for loc in scan_skill_names(Path(root)).get(name, []):
            try:
                rel = Path(loc).relative_to(Path(root))
            except ValueError:
                continue
            if len(rel.parts) >= 2:
                return rel.parts[0]
    return CATEGORY_BY_NAME.get(name, DEFAULT_CATEGORY)


def guard_write(skills_root, name: str, target_rel: str,
                extra_roots=()) -> dict:
    """Decide ONE bundled-skill write: ``(skills_root/target_rel) <- bundle``.

    The card rule: a bundled skill must NEVER be written where its NAME
    already resolves from any other path for that tree. Returns
    ``{"write": bool, "action": str, "reason": str, "paths": [...]}``:

      ``write``               name resolves nowhere -> first install proceeds
      ``skip-collision``      name already present elsewhere in the scan set
                              (would create a collision / a redundant copy)
      ``noop-resolves``       the only candidate IS the intended target (the
                              tree already resolves the name from where this
                              installer itself writes — caller may still
                              content-compare to update or skip)

    ``extra_roots`` extends the scan set (e.g. a profile's external_dirs).
    The root-vs-profile spread is NOT a concern: a profile session scans only
    its own roots (``tools/skills_tool._skill_search_dirs``), so a copy in the
    root home cannot collide with a profile's copy for a profile spawn — and
    vice versa. Grounded 2026-10-28; documented in the PR body.
    """
    target = Path(skills_root) / target_rel
    target_real = os.path.realpath(str(target))
    candidates: list[Path] = []
    for root in (Path(skills_root), *[Path(r) for r in extra_roots]):
        candidates.extend(scan_skill_names(root).get(name, []))
    others: list[Path] = []
    for c in candidates:
        try:
            if os.path.realpath(str(c)) == target_real:
                continue
        except OSError:
            pass
        if str(c) not in [str(o) for o in others]:
            others.append(c)
    if others:
        return {"write": False, "action": "skip-collision",
                "reason": (f"skipped: name already present at "
                           f"{', '.join(str(o) for o in others)} "
                           f"(would create collision)"),
                "paths": [str(o) for o in others]}
    return {"write": True, "action": "write",
            "reason": "name resolves nowhere in the scan set", "paths": []}
