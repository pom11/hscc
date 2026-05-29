---
name: brand-migration
description: Systematic brand/name replacement across multi-file codebases, config, state files, and templates. Covers rebrands, renames, legacy cleanup, and migration from one system name to another.
category: devops
---

# brand-migration — Systematic Brand/Name Replacement

Replace all references from one brand/name/path to another across a multi-file codebase, config, state, and templates. Covers rebrands, renames, legacy cleanup, and migration from one system name to another.

## Trigger

User asks to rename, rebrand, or replace all references from X to Y across a project. Common cases: "replace all X with Y", "rename the cluster", "remove old-brand references", "update branding."

## Steps

1. **Inventory** — Find ALL references across all relevant file types:

   ```bash
   grep -ri 'OLD_NAME' --include='*.py' --include='*.sh' --include='*.md' --include='*.json' --include='*.yaml' --include='*.toml' --include='*.yml' .
   ```

2. **Categorize each reference** into one of five buckets (decisions differ per bucket):

   | Category | Examples | Action |
   |---|---|---|
   | Code imports/vars | `OLDNAME_DIR`, `oldname-postgres`, function names | Replace with new name |
   | Config/data values | cluster names, container names, state file names | Replace AND rename the actual runtime object (docker rename, sparkrun cluster rename) |
   | State files | `oldname-agents.json`, `oldname-lifecycle.json` | Rename file + update internal references |
   | Templates/docs | `AGENTS.md`, `README.md`, inline comments | Replace in text |
   | Runtime identifiers | profile names, docker container names, user data | Keep AS-IS if only referenced in code comments, update in config/state if actually used at runtime |

3. **Pitfall — Runtime identifiers**: Names like `cluster_profile_name`, `docker_container_name`, or user-facing labels that are code comments but not actually used in the code should NOT be changed. Verify with `grep` on the actual runtime tool output.

4. **Pitfall — Template variables**: Check if template variables like `{{orchestratorPrompt}}` are actually substituted by any code. If not, inline the content or delete the file. Search across the entire project for the substitution pattern.

5. **Pitfall — Install templates**: These are COPY targets that get deployed to user machines. Every change to a live plugin must be synced to the install template. Use `shutil.copy2()` to overwrite.

6. **Pitfall — Old data files**: State files in `setup.json`, `projects.json`, `*.json` config files often hold embedded references. Must be cleaned recursively (JSON strings inside JSON values).

7. **Execute** — Apply changes file by file using `patch` for surgical edits, `write_file` for rewrites, `os.rename()` for file renames.

8. **Delete dead files** — If a file existed only as a cross-reference (e.g., `orchestrator_prompt.md` that was included via template variable), delete it and inline its content into the target.

9. **Verify** — Run comprehensive zero-refs check:

   ```bash
   # Check code files
   git -C <repo> grep -i 'OLD_NAME' -- '*.py' '*.sh'
   # Check docs
   git -C <repo> grep -i 'OLD_NAME' -- '*.md'
   # Check config/state
   git -C <repo> grep -i 'OLD_NAME' -- '*.json' '*.yaml'
   # Full filesystem sweep (including untracked)
   find . -type f \( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.sh' -o -name '*.yaml' \) -exec grep -li 'OLD_NAME' {} +
   ```

10. **Update .gitignore** — Add any new runtime/data files that shouldn't be tracked.

## Verification Checklist

- [ ] Zero `OLD_NAME` in all `.py`, `.sh` files
- [ ] Zero `OLD_NAME` in all `.md` files
- [ ] Zero `OLD_NAME` in all `.json` config/state files
- [ ] State files renamed (not just content replaced)
- [ ] Runtime objects renamed (docker containers, clusters, services)
- [ ] Install templates synced with main plugins
- [ ] `.gitignore` updated for any new data files
- [ ] `git commit` with descriptive message
- [ ] `git push` completed