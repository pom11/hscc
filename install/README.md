# install/ — Bundled Skill Sources

This directory holds the **bundled skill sources** that the HSCC skills installer
(`hscc-skills/hscc.py install-skills`) copies into a Hermes home's `skills/` tree.

The cluster-control skills — `hscc`, `hscc-cluster`, `hscc-model-onboard` — are
**main-profile only** (operator ruling R4, card `t_b52e70ba`). They install to
`<root>/profiles/<main_orch.main_name()>/skills/`, never a hard-coded name.
Any copy found in the root home or another profile is **moved** (never deleted)
to that home's `skills-backups/removed-<stamp>/`. The shared/work skills
(generic set, `sdlc-review`, `devops`) keep installing into the caller's
`HERMES_HOME/skills/`.

## What's Inside

| Directory | Contents |
|---|---|
| `hscc-skills/` | The 13 vendored skill directories listed below — all 13 are installed by the installer. |

The complete vendored set, matching `ls install/hscc-skills` exactly:

`brainstorming`, `caveman`, `devops`, `executing-plans`, `hscc`, `hscc-cluster`,
`hscc-lessons`, `hscc-model-onboard`, `sdlc-review`, `systematic-debugging`,
`test-driven-development`, `verification-before-completion`, `writing-plans`.

`devops` is a bundle of 7 sub-skills (`brand-migration`, `hscc-architecture`,
`hscc-plugins`, `kanban-orchestrator`, `kanban-worker`, `template-audit`,
`webhook-subscriptions`).

**Vendored = installed.** The installer copies every name in `BUNDLED_SKILLS`
(`hscc-skills/hscc.py`) — all 13 above, including `hscc-lessons`. Two gates keep the
two directions honest (`hscc_daemon/tests/test_surface_coverage_gates_t_c13de3cc.py`):
a name in the list with no directory here fails, and a directory here that the list
does not name fails too. Treat this table as both the complete *source* tree and the
installed set.

This is the **source of truth** for the bundled skills — edit them here, then run
`hscc-skills/hscc.py install-skills` to push the changes to `~/.hermes/skills/`.

## Note

The HSCC **plugins** are the top-level `hscc-*` / `hscc_daemon` directories at the
repo root. The repo is the source of truth; `hscc-bootstrap/bootstrap.sh` **copies**
them into the Hermes runtime dir `~/.hermes/plugins/` (backup-then-overwrite) via
`install_payload.py`. The plugins are *not* copied through this `install/` dir —
that staging path is only for the bundled skills above.
