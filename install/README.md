# install/ — Bundled Skill Sources

This directory holds the **bundled skill sources** that the HSCC skills installer
(`hscc-skills/hscc.py install-skills`) copies into `~/.hermes/skills/`.

## What's Inside

| Directory | Contents |
|---|---|
| `hscc-skills/` | The bundled skills the installer ships: `hscc`, `hscc-cluster`, `hscc-model-onboard`, `sdlc-review`, `devops`, plus the generic skills (brainstorming, writing-plans, test-driven-development, …). |

This is the **source of truth** for the bundled skills — edit them here, then run
`hscc-skills/hscc.py install-skills` to push the changes to `~/.hermes/skills/`.

## Note

The HSCC **plugins** are the top-level `hscc-*` / `hscc_daemon` directories at the
repo root — they are loaded in place from `~/.hermes/plugins/`, not staged or
copied through here. There is no separate plugin staging area.
