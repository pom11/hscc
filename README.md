<p align="center">
  <img src="assets/hscc.png" alt="HSCC — Hermes Spark Cluster Control" width="640">
</p>

# HSCC — Hermes Spark Cluster Control

**Turn a DGX Spark GPU cluster into a self-running team of specialized AI agents.**

Say *"build X"* in chat and a fleet of role-specialized agents brainstorms, decomposes, codes, reviews, and lands it — across multiple GPU nodes, hands-off.

[![v1.8.4](https://img.shields.io/badge/version-1.8.4-blue.svg)](CHANGELOG.md)
[![MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![pure-stdlib](https://img.shields.io/badge/python-pure--stdlib-orange.svg)](README.md)
[![1000+ tests](https://img.shields.io/badge/tests-1000%2B-brightgreen.svg)](https://github.com/pom11/hscc)

HSCC runs on a cluster of DGX Spark (GB10 / Grace-Blackwell, `sm_121a`) nodes serving LLMs via vLLM, orchestrated by [Hermes](https://github.com/NousResearch/hermes-agent) agents. It is a set of pure-stdlib Python plugins — no pip dependencies. You develop in a work repo (`~/dev/hscc`); bootstrap copies the plugins into the Hermes runtime (`~/.hermes/plugins/`), with runtime state in `~/.hscc/`.

The design is **native-Hermes-first**: agent work runs on Hermes' built-in kanban dispatcher and git worktrees. HSCC does not build a dispatcher — it *hooks into* the one Hermes already ships, adding the thin physical layer (cluster control, monitoring, model lifecycle), a role framework for specialized workers, and a review gate on the dispatch path.

## In plain words (ELI5)

You have a few powerful computers and a team of AI helpers. **HSCC is the manager that runs them for you.** You say what you want built; it splits the work across the computers, hands each piece to the right specialist — one plans, one writes the code, one checks it — and keeps the whole thing running: it restarts anything that crashes, and only pings you when it's genuinely stuck. One command gives you a whole working team, instead of babysitting a single chatbot.

## What it does

- **A whole org, not one bot** — 22+ role profiles (architect, coder, reviewer, QA, engineers, analysts), each with its own identity, skills, and disposition. New roles are minted on demand.
- **Autonomous quality gates** — code is reviewed by a dedicated agent (diff + tests + spec) before it lands on an integration branch. Main stays human-gated.
- **Spreads across your GPUs and self-heals** — work dispatches to worker nodes kept alive and health-monitored by the daemon. Crashed models relaunch automatically.
- **Failure escalation** — a cron watcher reassigns repeatedly-failing tasks to stronger models and pings you on Telegram when even that doesn't fix it. Silent when nothing is stuck.
- **One-command install** — `git clone` + `bootstrap.sh` detects your cluster from sparkrun, asks two questions (or `--yes` for zero), and installs everything.

## How it looks in practice

You type in chat: *"Build a REST API for the project tracker in ~/dev/tracker."*

1. The **orchestrator** brainstorms a spec with the **architect**, then decomposes it into a dependency-ordered task graph on the kanban board.
2. Tasks dispatch to role-specialized workers — **backend-engineer** scaffolds the API, **qa** writes tests, **coder** fills in the routes — each in its own git worktree on a GPU worker node.
3. When the coder finishes, it submits its work to **review**. The **reviewer** agent checks the diff, runs the tests, confirms the work matches the spec, and merges to the integration branch.
4. The whole chain runs hands-off. If a worker model crashes, the daemon relaunches it. If a task fails repeatedly, the escalation watcher reassigns it — and alerts you on Telegram if it gets stuck.

This runs on a DGX Spark cluster with Hermes agents and sparkrun — not a turnkey product for arbitrary hardware, but a proven pattern if you already have (or are building) that stack.

## Why we built this

Hermes is an excellent *single* agent: one brain, one chat, one model, with a built-in kanban dispatcher and git-worktree execution. But the moment you have a **GPU cluster and want a team of agents**, you hit gaps Hermes doesn't fill on its own — it knows how to *think* and dispatch work, not how to run the physical cluster underneath it or behave like a specialized organization.

HSCC exists to close that gap without forking or fighting Hermes. It leans on Hermes' engine and wraps it in the operational shell a real fleet needs.

### What Hermes was missing (and HSCC adds)

| Gap in stock Hermes | What HSCC adds |
|---|---|
| No physical cluster control — can't provision / stop / heal vLLM models, no NAS or node-health awareness | A sparkrun-backed cluster toolset (orchestrator-only) + a self-heal daemon that keeps worker models alive and relaunches crashed ones |
| Profiles exist, but no way to define a *roster* of specialists — every worker is generic | A role framework: each role is a spec file, generated into a Hermes profile with a layered SOUL (shared base + role disposition); 22+ roles ship, new ones minted on demand |
| The review-status dispatch path is present in core but inert — no producer, no reviewer skill | A `kanban_submit_review` producer + an `sdlc-review` skill, so code is gated (diff + tests + spec) and merged to an integration branch before it counts as done |
| No "run hands-off" control | An autonomy flag (`~/.hscc/autonomy`) + a "do it autonomously" trigger that lets the orchestrator run idea→shipped without pausing for approval |
| No cluster-aware setup | One bootstrap command that detects the cluster from sparkrun and installs everything — no hardcoded topology |

The result: Hermes goes from *one smart agent* to a *self-running, specialized fleet* spread across your GPU nodes — with quality gates and an off switch.

## Requirements

- A configured [sparkrun](https://sparkrun.dev/) DGX Spark cluster (one or more GB10 nodes)
- [Hermes](https://github.com/NousResearch/hermes-agent) installed (`~/.hermes/hermes-agent`)
- A control host for the daemon — macOS (launchd) or Linux (systemd --user); Python 3 (stdlib only)
- Optional: a NAS for the offline model cache

---

## Quick start

```
git clone https://github.com/pom11/hscc ~/dev/hscc
~/dev/hscc/hscc-bootstrap/bootstrap.sh
```

Bootstrap copies the plugin tree into `~/.hermes/plugins/`, checks prerequisites (sparkrun cluster + Hermes), detects your cluster topology, asks a couple of questions, then installs everything — skills, role profiles, `~/.hscc` state, and the monitoring daemon. It does **not** start vLLM models; bring those up explicitly when ready. Use `--yes` for a fully non-interactive run.

To pick up later changes, `git pull` in your work repo and re-run bootstrap — it backs up the previous runtime copy before overwriting. Pass `--no-backup` to skip backups.

### First things after install

All fleet operations go through one unified `hscc` command:

```
hscc status                               # daemon health + all streams at a glance
hscc verify                               # full smoke-test of the entire stack
hscc cluster status                       # running workloads + idle hosts
hscc stats                                # fleet completions & tool activity (last 7 days)
hscc throughput                           # vLLM token throughput + per-node queue depth
hscc autoscale                            # scaling advice from current queue depth (read-only)
hscc template list                        # see shipped cluster layouts (1-8 nodes)
hscc template preview 4node-coding        # dry-run against the live cluster
hscc template apply 4node-coding --confirm  # apply the layout
hscc project standup                      # fleet-wide project/kanban digest
hscc project review <card>                # review + merge a card
hscc project --help                       # full flightdeck command list under the project verb
```

`hscc project …` is the project/kanban orchestration domain (formerly the
standalone `flightdeck` tool, physically relocated into `hscc-project/` and
reached through the `project` verb group). Use `hscc project standup` for your
daily fleet digest (NEEDS YOU / FAILING / STALE / RUNNING / DRIFT), `hscc
project review <card>` to review and merge a finished card, and `hscc project
--help` for the full flightdeck command surface. The complete reference lives
in [`hscc-project/docs/COMMANDS.md`](hscc-project/docs/COMMANDS.md), and the
`flightdeck X` ↔ `hscc project X` mapping plus the naming-collision notes are
in [`docs/PROJECT-COMMANDS.md`](docs/PROJECT-COMMANDS.md).

`hscc --help` shows the full grouped command reference with examples. The orchestrator agent also has parity — it can call these same capabilities (verify, stats, throughput, autoscale, templates) directly as tools, not just the operator CLI.

---

## Cluster topology

Topology is detected from `sparkrun cluster list` — HSCC makes no assumptions about IPs, node count, or whether a NAS is present. A reference layout:

| Node | Role |
|---|---|
| Gateway / orchestrator | Always-on vLLM serving the Hermes orchestrator + chat. Never reaped. |
| Worker(s) | Per-node vLLM, kept alive + health-monitored by the daemon. |
| NAS (optional) | HF model cache, NFS-mounted to every node. Containers serve from this offline cache (`HF_HUB_OFFLINE=1`). |

Worker models are declared in `~/.hscc/serving.json` as keep-alive units; the daemon health-checks them and relaunches a crashed one with the node's own recipe.

## Model serving

Models are served via sparkrun recipes against the (optional) NAS-backed cache:

```
sparkrun run <recipe>.yaml --cluster <name> --hosts <node-ip> --port 8000 --ensure
```

The model name is read from the recipe's `model:` field and recorded in `serving.json`.

### Cluster templates

Instead of provisioning by hand, apply a template that describes intent (which recipes, how many workers) and resolves to the live cluster at apply — auto-assigning nodes + ports, and refusing layouts that don't fit (checked against `sparkrun show` VRAM). A node-count library ships for 1–8 nodes, including multi-family and 2-models-per-node layouts. See [hscc-cluster/templates/](hscc-cluster/templates/README.md).

Templates are **topology-free by default** (schema v2): no IPs, no ports — placement is inferred against the live cluster at apply. Since **v1.7.0** (schema v3) a template may also state placement and routing *explicitly*:

- **`nodes:`** per unit — optional. When present the resolver's inference is bypassed and the list is used verbatim; `nodes[0]` is the span primary (exposes the endpoint), the rest are tp peers. Explicit nodes make placement deterministic but hardcode a specific cluster, so the **shipped templates deliberately omit them** and stay portable.
- **`allow_colocation:`** — default `false`. Two units naming the same node is a hard error unless *both* opt in, in which case apply warns about VRAM contention.
- **`routing:`** — maps a consumer (`delegation`, `compaction`, `auxiliaries`) to a symbolic unit (`orchestrator` or `family-<name>`), resolved to that unit's live endpoint at apply time. `auxiliaries` covers the 8 **text** tasks only — never `vision`/`web_extract` — and an id is never written to an endpoint that doesn't advertise it (probe-before-write).

**Key semantic:** *omission means do-not-touch.* An omitted `routing` key means apply does not write that config key at all — the live value survives. Apply never silently re-routes something tuned by hand.

```yaml
routing:
  delegation: family-reasoning
  compaction: orchestrator
  auxiliaries: orchestrator
```

`hscc template validate` is now two layers — **structural** (offline: no cluster state, no resolver) and **placement** (live) — and reports them separately. `--structural-only` skips the live layer (usable in CI), and a non-zero exit means either layer failed. `apply` runs the same validation as its pre-flight gate and blocks before touching anything. Full schema detail: [docs/DESIGN-template-explicit-placement.md](docs/DESIGN-template-explicit-placement.md) and [hscc-cluster/templates/README.md](hscc-cluster/templates/README.md).

```
hscc template list                          # see the shipped layouts
hscc template preview 4node-coding          # dry-run against the live cluster
hscc template apply 4node-coding --confirm  # = the live setup
hscc template validate 4node-coding --structural-only  # offline, CI-friendly
```

---

## The fleet

Agent work flows through native Hermes kanban — HSCC adds a pre-dispatch hook (cluster-aware host routing) and the review gate, but the dispatch loop is Hermes'. An idea is brainstormed into a spec, decomposed into a dependency-ordered task graph, and each task dispatches to a **role-specialized worker** running in its own git worktree.

### Roles

A role is a single spec file (`hscc-roles/roles/<name>.yaml`). A generator builds it into a Hermes profile with a **layered SOUL** (shared base character + role disposition + thin operational facts) and the full Hermes toolset **minus cluster control** — only the orchestrator can change the cluster.

```
python3 ~/.hermes/plugins/hscc-roles/hscc.py generate          # build all profiles from specs
python3 ~/.hermes/plugins/hscc-roles/hscc.py create <name> "…" # author a new role on demand
python3 ~/.hermes/plugins/hscc-roles/hscc.py list              # roles + whether profile exists
```

The roster ships with a full org — orchestrator, architect, coder, reviewer, qa; backend/frontend/devops/security/data engineers; ml-engineer, ml-researcher, data-scientist; product-manager, technical-writer, ux-designer; financial-analyst, business-analyst, market-researcher; content-writer, social-media-manager, project-manager. New roles are minted (by a human or the orchestrator) with `create`, so the roster is data, not code.

### Review loop

Code tasks are gated. When a coder finishes, it submits its task to `review` status; the dispatcher spawns a review agent loading the `sdlc-review` skill, which checks the diff, runs the tests, and confirms the work matches the spec. Approved work merges to an **integration branch** (main stays human-gated); rejected work is sent back with change requests.

### Autonomy

A master flag at `~/.hscc/autonomy` (default off) governs whether the orchestrator pauses for approval:

```
python3 ~/.hermes/plugins/hscc-roles/hscc.py autonomy        # show
python3 ~/.hermes/plugins/hscc-roles/hscc.py autonomy on     # hands-off
python3 ~/.hermes/plugins/hscc-roles/hscc.py autonomy off    # ask-first
```

Saying *"do it autonomously"* flips it on: the orchestrator writes a best-judgment spec without back-and-forth and lets the fleet run. Autonomy never bypasses the reviewer gate or merges to main.

### Failure escalation

A Hermes-cron watcher (`scripts/escalate_watcher_run.py`) runs every 15 minutes and reassigns repeatedly-failing tasks to the strong model tier. When even the strong tier fails, it posts a human-attention alert to your Telegram group — deduped so a stuck task is not re-announced every tick, and silent when nothing is stuck. This is an opt-in *acting* automation; the `hscc escalate` CLI command provides a dry-run view of what would be escalated.

A daily, human-gated dependency-update loop keeps the cluster's Hermes and sparkrun runtime dependencies current via automated PRs and kanban verification cards.

---

## Plugins

Each plugin has its own README with details — linked below.

| Plugin | Role |
|--------|------|
| [**hscc-cluster**](hscc-cluster/README.md) | Cluster-control toolset (orchestrator-only): live **discovery** (`discovery_status`, power-draw idle, auto-adopt), status/health, model lifecycle (`provision_model`/`stop_model`/`restart_model`), self-heal, the **topology-free template engine** ([templates/](hscc-cluster/templates/README.md)), and the resume/work-flow helpers. |
| [**hscc-roles**](hscc-roles/README.md) | Role framework: author + generate role-specialized profiles (incl. `reviewer`); autonomy flag CLI. |
| [**hscc_daemon**](hscc_daemon/README.md) | Monitoring + self-heal daemon (launchd / systemd --user): vLLM/gateway/NAS health, **unit-keyed worker keep-alive** (multi-model-per-node), trigger engine, Operations notifications, startup cruft self-clean. |
| [**hscc-commands**](hscc-commands/README.md) | Operator slash commands (run in the gateway, not the LLM): `/cluster`, `/status`, `/orch-restart`, `/cluster-restart` (template re-apply), `/heal`, `/template`. |
| [**sparkrun-hermes**](sparkrun-hermes/README.md) | Official Hermes plugin for sparkrun: a guarded `sparkrun_exec` CLI passthrough + run/setup/registry skills. |
| [**hscc-bootstrap**](hscc-bootstrap/README.md) | Preflight-gated, topology-detecting installer (doctor -> copy -> patches -> wire -> daemon). |
| [**hscc-skills**](hscc-skills/README.md) | Idempotent installer for bundled skills + templates (sources in [install/](install/README.md)). |
| [**memori** / **memori_byodb**](memori_byodb/README.md) | Memory-provider plugins (HSCC runs the BYODB variant: NAS-backed store + offline augmentation). |

Also: [`patches/`](patches/MANIFEST.md) — curated hermes/sparkrun upstream patches
(run official + reapply the delta); [`docs/`](docs/README.md) — design specs +
per-workstream plans; [`scripts/`](scripts/README.md) — pure-shell `--no-agent`
operator watchdogs (proxy / workers / cluster / NAS) registered as Hermes cron
jobs, installed by bootstrap into `~/.hermes/scripts/`.

---

## Layout

The repo (you edit here) is separate from the Hermes runtime (bootstrap copies
into it):

```
~/dev/hscc/                 # the work repo — what you clone + edit
├── hscc-cluster/           # cluster toolset + discovery + template engine   -> README
│   └── templates/          #   topology-free node-count template library     -> README
├── hscc-roles/             # role framework                                  -> README
├── hscc_daemon/            # monitoring + self-heal daemon                   -> README
├── hscc-commands/          # operator slash commands                         -> README
├── hscc-bootstrap/         # the installer                                   -> README
├── hscc-skills/            # skill installer                                 -> README
├── sparkrun-hermes/        # sparkrun passthrough plugin                     -> README
├── memori/ · memori_byodb/ # memory providers                               -> README
├── install/                # vendored skill sources                         -> README
├── patches/                # curated hermes/sparkrun upstream patches        -> MANIFEST
├── docs/                   # design specs + plans                           -> README
├── scripts/                # operator watchdog shell scripts                 -> README
└── _archive/               # superseded code (moved, never deleted)

# Hermes runtime (bootstrap targets — not edited directly):
~/.hermes/plugins/          # installed copy of the plugins above
~/.hermes/profiles/         # generated role profiles (build artifacts)
~/.hermes/skills/           # installed skills
~/.hermes/scripts/          # installed operator watchdog scripts
~/.hscc/                    # runtime state (serving.json, applied_template.json,
                            #   cluster.json, autonomy, rollback/, state/)
~/.sparkrun-local/recipes/  # vLLM serving recipes
```

## Conventions

- Pure-stdlib Python; no external plugin dependencies.
- Cluster control is orchestrator-only; worker roles never touch the cluster.
- State is read from live sources (`sparkrun status`, `serving.json`), never stale caches.
- "Strip/archive" means move to `_archive/<date>/`, never delete.
- Restart the gateway after prompt/plugin/config edits: `launchctl kickstart -k gui/$UID/ai.hermes.gateway`.

## License

MIT — see [LICENSE](LICENSE).
