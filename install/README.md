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
repo root. The repo is the source of truth; `hscc-bootstrap/bootstrap.sh` **copies**
them into the Hermes runtime dir `~/.hermes/plugins/` (backup-then-overwrite) via
`install_payload.py`. The plugins are *not* copied through this `install/` dir —
that staging path is only for the bundled skills above.
