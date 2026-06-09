# install/ — Plugin & Skill Staging Area

This directory is a **staging and template area** for the Hermes plugin/skill system. It contains reference copies, installable templates, and archived plugins that tools and installation scripts use as seed sources.

## What's Inside

| Directory | Contents |
|---|---|
| `hscc-cli/` | Installable CLI tool (the hscc binary) |
| `hscc-plugins/` | Archived / inactive plugins (e.g. hscc-agent-coordinator, hscc-chat, hscc-cluster, hscc_daemon, hscc-governance, hscc-orchestrator, etc.) |
| `hscc-skills/` | Bundled skills (e.g. brainstorming, devops, sdlc-review, test-driven-development, etc.) |
| `hscc-templates/` | Templates for new plugins and skills *(not yet provisioned)* |

## ⚠️ Important: Do Not Edit Files Here

**The live, active plugins and skills are the top-level `hscc-*` directories at the root of this repository** (e.g. `hscc-cli/`, `hscc-skills/` in the repo root).

Files inside `install/` are **not** the running versions. They serve as:

- **Source references** for installation scripts
- **Archives** of older or deprecated plugins
- **Templates** for scaffolding new plugins/skills

## Divergence Warning

Files in this directory **may diverge** from their top-level counterparts. They should **not** be considered the source of truth for what is actually running. If you need to modify a plugin or skill, edit the top-level `hscc-*` directory, not the copy here.

### When files here are authoritative

Only in these specific cases should you edit `install/` contents:

- Updating the install script that pulls from this staging area
- Adding a new template (`hscc-templates/`)
- Archiving a deprecated plugin (`hscc-plugins/`)

---

> **Rule of thumb:** If it affects runtime behavior, edit the top-level `hscc-*` directory. If it affects installation templates or archives, edit `install/`.
