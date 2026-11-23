# install/ — Bundled Skill Sources

This directory holds the **bundled skill sources** that the HSCC skills installer
(`hscc-skills/hscc.py install-skills`) copies into `~/.hermes/skills/`.

## What's Inside

| Directory | Contents |
|---|---|
| `hscc-skills/` | The 13 vendored skill directories listed below — 12 are installed by the installer, `hscc-lessons` is not (see *Vendored ≠ installed*). |

The complete vendored set, matching `ls install/hscc-skills` exactly:

`brainstorming`, `caveman`, `devops`, `executing-plans`, `hscc`, `hscc-cluster`,
`hscc-lessons`, `hscc-model-onboard`, `sdlc-review`, `systematic-debugging`,
`test-driven-development`, `verification-before-completion`, `writing-plans`.

`devops` is a bundle of 7 sub-skills (`brand-migration`, `hscc-architecture`,
`hscc-plugins`, `kanban-orchestrator`, `kanban-worker`, `template-audit`,
`webhook-subscriptions`).

**Vendored ≠ installed.** The installer copies only the names in `BUNDLED_SKILLS`
(`hscc-skills/hscc.py`) — 12 of the 13 above. `hscc-lessons` is a real skill with a
shipped `SKILL.md`, but it is not in `BUNDLED_SKILLS`, so `hscc bootstrap` never
installs it and never updates it (audit F12,
`docs/audits/2026-11-23-agent-surface.md` §8). Listing it there is a one-line code
change in `hscc-skills/hscc.py` and is **not done as of this page**; until it is, treat
this table as the complete *source* tree, not the installed set.

This is the **source of truth** for the bundled skills — edit them here, then run
`hscc-skills/hscc.py install-skills` to push the changes to `~/.hermes/skills/`.

## Note

The HSCC **plugins** are the top-level `hscc-*` / `hscc_daemon` directories at the
repo root. The repo is the source of truth; `hscc-bootstrap/bootstrap.sh` **copies**
them into the Hermes runtime dir `~/.hermes/plugins/` (backup-then-overwrite) via
`install_payload.py`. The plugins are *not* copied through this `install/` dir —
that staging path is only for the bundled skills above.
