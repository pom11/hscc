<p align="center">
  <img src="assets/hscc.png" alt="HSCC — Hermes Spark Cluster Control" width="640">
</p>

# HSCC — Hermes Spark Cluster Control

**Turn a DGX Spark GPU cluster into a self-running team of specialized AI agents.**

HSCC is the operational backbone that lets you say *"build X"* in chat and have a fleet of role-specialized agents brainstorm it, decompose it into tasks, write the code, review it, and land it — across multiple GPU nodes, hands-off.

It runs on a cluster of DGX Spark (GB10 / Grace-Blackwell, `sm_121a`) nodes serving LLMs via vLLM, orchestrated by [Hermes](https://github.com/NousResearch/hermes-agent) agents. HSCC is a set of pure-stdlib Python plugins. You develop in a work repo (e.g. `~/dev/hscc`); bootstrap copies the plugins into the Hermes runtime dir `~/.hermes/plugins/`, with runtime state in `~/.hscc/`.

The design is **native-Hermes-first**: agent work runs on Hermes' built-in kanban dispatcher + git worktrees. HSCC contributes the thin physical layer (cluster control, monitoring, model lifecycle) plus a role framework that turns the fleet into specialized, self-extending workers.

## Why we built this

Hermes is an excellent *single* agent: one brain, one chat, one model, with a built-in kanban dispatcher and git-worktree execution. But the moment you have a **GPU cluster and want a team of agents**, you hit gaps Hermes doesn't fill on its own — it knows how to *think* and dispatch work, not how to run the physical cluster underneath it or behave like a specialized organization.

HSCC exists to close that gap without forking or fighting Hermes. It leans on Hermes' engine and wraps it in the operational shell a real fleet needs.

### What Hermes was missing (and HSCC adds)

| Gap in stock Hermes | What HSCC adds |
|---|---|
| No physical cluster control — can't provision / stop / heal vLLM models, no NAS or node-health awareness | A sparkrun-backed cluster toolset (orchestrator-only) + a self-heal daemon that keeps worker models alive and relaunches crashed ones |
| Profiles exist, but no way to define a *roster* of specialists — every worker is generic | A role framework: each role is a spec file → generated into a Hermes profile with a layered SOUL (shared base + role disposition); 22+ roles ship, and new ones are minted on demand |
| The review-status dispatch path is present in core but inert — no producer, no reviewer skill | A `kanban_submit_review` producer + an `sdlc-review` skill, so code is gated (diff + tests + spec) and merged to an integration branch before it counts as done |
| No master "run hands-off" control | An autonomy flag (`~/.hscc/autonomy`) + a "do it autonomously" phrase trigger that lets the orchestrator run idea→shipped without pausing for approval |
| No cluster-aware setup | One bootstrap command that detects the cluster from sparkrun and installs everything — no hardcoded topology |

The result: Hermes goes from *one smart agent* to a *self-running, specialized fleet* spread across your GPU nodes — with quality gates and an off switch.

## Why HSCC

- **A whole org, not one bot** — 22+ role profiles (architect, coder, reviewer, QA, and a full business roster), each with its own identity, skills, and disposition. New roles are minted on demand.
- **Autonomous quality** — code is gated by a reviewer agent (diff + tests + spec) before it lands on an integration branch; main stays human-gated.
- **Hands-off when you want it** — flip autonomy on (or just say "do it autonomously") and the fleet runs idea→shipped without babysitting.
- **Spreads across your GPUs** — work dispatches to worker nodes that are health-monitored and kept alive automatically.
- **Portable** — one bootstrap command detects your cluster and installs everything; no hardcoded topology.

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

Clone the repo to a work location of your choice (`~/dev/hscc` here), then run bootstrap. Bootstrap **copies** the plugin tree into `~/.hermes/plugins/` (backing up any existing copy), checks prerequisites (a configured sparkrun cluster + Hermes), detects your cluster topology, asks a couple of questions, then installs everything HSCC needs — skills, role profiles, `~/.hscc` state + `serving.json`, and the monitoring daemon. It does **not** start vLLM models; bring those up explicitly when ready. Use `--yes` for a non-interactive run.

To pick up later changes, `git pull` in your work repo and re-run bootstrap — it re-copies the plugins into the runtime dir. Each run backs up the previous runtime copy as `<dir>.bak-<timestamp>`; pass `--no-backup` to overwrite in place without the backups.

---

## Cluster topology

Topology is detected from `sparkrun cluster list` — HSCC makes no assumptions about IPs, node count, or whether a NAS is present. A reference layout:

| Node             | Role          | Notes |
|------------------|---------------|-------|
| Gateway / orchestrator | Always-on vLLM serving the Hermes orchestrator + chat. Never reaped. |
| Worker(s)        | Per-node vLLM, kept alive + health-monitored by the daemon. |
| NAS (optional)   | HF model cache, NFS-mounted to every node. Containers serve from this offline cache (`HF_HUB_OFFLINE=1`). |

Worker models are declared in `~/.hscc/serving.json` as keep-alive units; the daemon health-checks them and relaunches a crashed one with the node's own recipe.

## Model serving

Models are served via sparkrun recipes against the (optional) NAS-backed cache:

```
sparkrun run <recipe>.yaml --cluster <name> --hosts <node-ip> --port 8000 --ensure
```

The model name is read from the recipe's `model:` field and recorded in `serving.json`.

---

## The fleet

Agent work flows through native Hermes kanban: an idea is brainstormed into a spec, decomposed into a dependency-ordered task graph, and each task is dispatched to a **role-specialized worker** running in its own git worktree.

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

---

## Plugins

| Plugin | Role |
|--------|------|
| **hscc-cluster** | Cluster ops toolset (orchestrator-only): `cluster_status`, `model_health`, `provision_model`, `stop_model`, `restart_model`, self-heal (`remount_nas`, `repair_nas_export`, `reap_orphans`). Reads live truth from `sparkrun status` + `serving.json`. |
| **hscc-roles** | Role framework: author + generate role-specialized profiles; autonomy flag CLI. |
| **hscc_daemon** | Monitoring + self-heal daemon (launchd on macOS / systemd --user on Linux): vLLM/gateway/NAS health, worker keep-alive, trigger engine, Operations-topic notifications. |
| **hscc-commands** | Operator slash commands for incident response: `/cluster`, `/orch-restart`, `/cluster-restart`. Run directly in the gateway (not via the LLM) so they work even when the orchestrator model is wedged. |
| **sparkrun-hermes** | Official Hermes plugin for sparkrun: a single guarded `sparkrun_exec` CLI passthrough + the run/setup/registry skills. |
| **hscc-bootstrap** | Preflight-gated, topology-detecting installer. |
| **hscc-skills** | Idempotent installer for bundled skills + templates. |
| **hscc-model-onboard** *(skill)* | Bring a new model/quant online cluster-wide end-to-end. |

---

## Layout

```
~/.hermes/plugins/        # this repo — the plugins
~/.hermes/profiles/       # generated role profiles (build artifacts, not tracked)
~/.hermes/skills/         # installed skills
~/.hscc/                  # runtime state (serving.json, autonomy, state/, events.jsonl)
~/.sparkrun-local/recipes # vLLM serving recipes
```

## Conventions

- Pure-stdlib Python; no external plugin dependencies.
- Cluster control is orchestrator-only; worker roles never touch the cluster.
- State is read from live sources (`sparkrun status`, `serving.json`), never stale caches.
- "Strip/archive" means move to `_archive/<date>/`, never delete.
- Restart the gateway after prompt/plugin/config edits: `launchctl kickstart -k gui/$UID/ai.hermes.gateway`.

## License

MIT — see [LICENSE](LICENSE).
