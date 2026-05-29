---
name: template-audit
description: Auditing template documentation files for utility and relevance. Categorizes files as procedural (keep), reference (shrink), flavor (delete), placeholder (delete), or cross-referenced (inline or delete). Prevents bloated template directories from accumulating dead files.
category: devops
---

# template-audit — Auditing Template Documentation for Utility

Audit template files to determine which are actually useful vs. dead weight. Applies to agent identity templates, instruction files, reference docs, and any `templates/` directory.

## Trigger

User asks to audit, review, clean up, or trim template files. Common cases: "check if we need all these template files", "trim the templates", "are these templates actually used?", "clean up the template directory."

## Steps

1. **Inventory** — List all template files and their sizes:

   ```bash
   ls -la templates/
   wc -l templates/*
   ```

2. **Read each file** and categorize into one of five types:

   | Type | Characteristics | Action |
   |---|---|---|
   | Procedural | Step-by-step checklists, workflows, rules | Keep as-is |
   | Reference | CLI commands, API docs, architecture tables | Shrink to essentials (15-20 lines) |
   | Flavor | Vibe, emoji, personality, tone, "creature" description | Delete |
   | Placeholder | Empty variables like "(learn this later)", "(ask them)" | Delete |
   | Cross-referenced | Included by other files via template variables | Check if reference still works |

3. **Cross-reference check** — For each file, verify how it's consumed:
   - Template variable substitution (e.g., `{{orchestratorPrompt}}` replacement code)
   - Include directives (e.g., `<!-- include -->` or bash `source`)
   - Any code that reads these files at runtime

   ```bash
   grep -rn 'orchestratorPrompt\|SOUL.md\|HEARTBEAT.md' --include='*.py' --include='*.sh' --include='*.yaml' .
   ```

4. **Redundancy check** — For reference-type files, ask: "Is this content available from an actual tool?" Examples:
   - CLI command examples → redundant with `tool --help`
   - Tool listings → redundant with toolset definitions
   - Architecture tables → keep only if not easily discoverable
   - Mac control shortcuts → obsolete once computer_use is the primary method

5. **Decision matrix**:
   - Procedural → keep untouched (e.g., `HEARTBEAT.md` with step-by-step checks)
   - Reference → shrink to 15-20 lines (cluster spec, services table, SSH commands only)
   - Flavor/placeholder → delete
   - Cross-referenced with broken link → inline or delete
   - Duplicated between install/ and root/ → keep only in install/, sync on changes

6. **Inline before delete** — If a file's content should be preserved (e.g., `orchestrator_prompt.md` → inline into `SOUL.md`), copy content to the target before deleting the source.

7. **Verify** — Confirm final state:
   - File count matches expected (e.g., 4 files instead of 6+)
   - Total size is reasonable (not bloated)
   - Zero cross-reference breakage (no dangling `{{variable}}` strings)
   - `git status` shows expected deletions and modifications

## Verification Checklist

- [ ] Each file categorized (procedural/reference/flavor/placeholder/cross-referenced)
- [ ] Cross-references verified (no broken `{{variables}}`)
- [ ] Redundant content removed from reference files
- [ ] Placeholder/flavor files deleted
- [ ] Broken cross-refs either inlined or deleted
- [ ] Install templates synced with main templates
- [ ] File count and sizes reasonable
- [ ] No dangling template variables remain
- [ ] `git commit` with descriptive message