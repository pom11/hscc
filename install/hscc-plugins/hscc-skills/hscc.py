#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Skills & Templates Installer

Install / update bundled Hermes skills and templates to ~/.hermes/ directories.
Idempotent: skips files that are already installed (hash-matched).

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
import shutil
import json

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
    # HSCC cluster-control skills
    "hscc",
    "hscc-agent-coordinator",
    "hscc-cluster",
    "hscc-events",
    "hscc-governance",
    "hscc-orchestrator",
    "hscc-projects",
    "hscc-provision",
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

        results = copy_skill_dir(src, SKILLS_DEST, skill)
        if not results:
            skills_skipped += 1
            skills_results[skill] = ("SKIPPED", "no new files")
            continue

        copies = sum(1 for s in results.values() if s in ("copied",))
        skips = sum(1 for s in results.values() if s == "skipped")
        skills_copied += copies
        skills_skipped += skips
        status_str = f"+{copies} copied" + (f", {skips} unchanged" if skips else "")
        skills_results[skill] = ("UPDATED" if copies else "UP-TO-DATE", status_str)

    print(format_status_table(
        [(s, r[0], r[1]) for s, r in skills_results.items()]
    ))
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
    """Install / update Hermes skills only."""
    print("─── Installing Hermes Skills ───")
    print(f"  Source: {SKILLS_SOURCE}")
    print(f"  Dest:   {SKILLS_DEST}")
    print()

    if not os.path.isdir(SKILLS_SOURCE):
        print(f"[ERROR] Skills source not found: {SKILLS_SOURCE}")
        sys.exit(1)

    results = []
    copied = 0
    for skill in BUNDLED_SKILLS:
        src = os.path.join(SKILLS_SOURCE, skill)
        if not os.path.isdir(src):
            results.append((skill, "MISSING", "not found"))
            continue
        file_results = copy_skill_dir(src, SKILLS_DEST, skill)
        copies = sum(1 for s in file_results.values() if s == "copied")
        if copies > 0:
            copied += copies
            results.append((skill, "UPDATED", f"+{copies} file(s)"))
        else:
            results.append((skill, "UP-TO-DATE", "already installed"))

    print(format_status_table(results))
    print()
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
        dst_skill = os.path.join(SKILLS_DEST, skill)

        if not os.path.isdir(src_skill):
            skill_status.append((skill, "⚠ MISSING", "source not found"))
            continue

        installed_files = os.listdir(dst_skill) if os.path.isdir(dst_skill) else []
        source_files = os.listdir(src_skill)

        if not installed_files:
            skill_status.append((skill, "⚠ NOT INSTALLED", f"{len(source_files)} file(s) in source"))
            continue

        out_of_date = []
        for src_file in source_files:
            src_path = os.path.join(src_skill, src_file)
            dst_path = os.path.join(dst_skill, src_file)
            if os.path.isfile(src_path) and os.path.isfile(dst_path):
                if file_hash(src_path) != file_hash(dst_path):
                    out_of_date.append(src_file)

        if out_of_date:
            skill_status.append((skill, "⚠ OUT-OF-DATE", f"{len(out_of_date)} file(s) need update"))
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
