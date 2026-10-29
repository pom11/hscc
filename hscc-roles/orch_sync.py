"""Bring existing ``<project>-orch`` profiles in line with the orchestrator template.

``generator.generate_profile`` rewrites a profile's whole ``config.yaml``; live
orchestrators carry hand-tuned configs, so they are never regenerated. This
module touches exactly two things and nothing else:

* **SOUL.md** — the base identity (everything before ``## Role:``) and the
  ``## Operational`` tail are re-rendered from the current template; the
  project's own ``## Role:`` section is kept byte-for-byte. A SOUL without both
  markers is left alone and reported. The previous file is kept as
  ``SOUL.md.bak-<stamp>`` whenever it changes.
* **skills/** — every skill in ``ORCH_SKILL_PACK`` the profile does not
  already have (matched by skill NAME anywhere under ``skills/`` — a second
  copy under another category is a name collision that makes spawns fail
  ``Unknown skill(s)``) is copied from the shared skills tree. Nothing is ever
  removed or overwritten.

Config, memory, sessions and project-specific skills are never touched.
"""

from __future__ import annotations

import datetime
import os
import shutil

import generator

ORCH_SKILL_PACK = (
    "kanban-orchestrator",
    "sdlc-review",
    "writing-plans",
    "brainstorming",
    "systematic-debugging",
    "verification-before-completion",
    "executing-plans",
)

ROLE_MARK = "\n## Role: "
OPS_MARK = "\n## Operational"


def compose(soul: str, name: str, base_identity: str):
    """Return the synced SOUL text, or None when the SOUL lacks the markers."""
    role_at = soul.find(ROLE_MARK)
    if role_at < 0:
        return None
    ops_at = soul.find(OPS_MARK, role_at)
    if ops_at < 0:
        return None
    role_section = soul[role_at + 1:ops_at].rstrip()
    ops = generator._PROJECT_ORCH_OPS.format(name=name)
    return f"{base_identity.rstrip()}\n\n{role_section}\n\n{ops}"


def _skill_names(skills_dir: str) -> set:
    names = set()
    for root, _dirs, files in os.walk(skills_dir):
        if "SKILL.md" in files:
            names.add(os.path.basename(root))
    return names


def find_skill(source: str, name: str):
    """The shared copy of ``name``: a category copy (``<cat>/<name>``) beats a
    top-level one, ties break alphabetically. None when absent."""
    hits = []
    for root, _dirs, files in os.walk(source):
        if os.path.basename(root) == name and "SKILL.md" in files:
            rel = os.path.relpath(root, source)
            hits.append((-rel.count(os.sep), rel))
    return sorted(hits)[0][1] if hits else None


def sync_profile(pdir: str, base_identity: str, skills_source: str,
                 dry_run: bool = False, stamp: str | None = None) -> dict:
    name = os.path.basename(os.path.normpath(pdir))
    report = {"profile": name, "soul": "unchanged", "skills_added": [],
              "skills_missing_source": []}
    soul_path = os.path.join(pdir, "SOUL.md")
    try:
        with open(soul_path, encoding="utf-8") as fh:
            soul = fh.read()
    except OSError:
        report["soul"] = "missing"
        soul = None
    if soul is not None:
        new = compose(soul, name, base_identity)
        if new is None:
            report["soul"] = "unrecognized (left alone)"
        elif new != soul:
            report["soul"] = "updated"
            if not dry_run:
                stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(soul_path, f"{soul_path}.bak-{stamp}")
                with open(soul_path, "w", encoding="utf-8") as fh:
                    fh.write(new)
    skills_dir = os.path.join(pdir, "skills")
    have = _skill_names(skills_dir) if os.path.isdir(skills_dir) else set()
    for skill in ORCH_SKILL_PACK:
        if skill in have:
            continue
        rel = find_skill(skills_source, skill)
        if rel is None:
            report["skills_missing_source"].append(skill)
            continue
        report["skills_added"].append(skill)
        if not dry_run:
            shutil.copytree(os.path.join(skills_source, rel),
                            os.path.join(skills_dir, rel), symlinks=True)
    return report


def orchestrator_dirs(profiles_dir: str, only=None) -> list:
    """Every per-project ``*-orch`` profile dir (the cluster-wide
    ``orchestrator`` keeps its own SOUL and is not a project orchestrator)."""
    out = []
    try:
        entries = sorted(os.listdir(profiles_dir))
    except OSError:
        return out
    for entry in entries:
        if not generator._is_project_orchestrator(entry):
            continue
        if only and entry not in only:
            continue
        pdir = os.path.join(profiles_dir, entry)
        if os.path.isdir(pdir):
            out.append(pdir)
    return out


def sync_all(profiles_dir: str, base_identity: str, skills_source: str,
             only=None, dry_run: bool = False) -> list:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return [sync_profile(p, base_identity, skills_source, dry_run=dry_run, stamp=stamp)
            for p in orchestrator_dirs(profiles_dir, only)]
