---
name: hscc
description: "Hermes Spark Cluster Control — thin cluster-physical layer for the DGX Spark cluster (provision/stop/heal models, monitor, NAS). Agent dispatch is native Hermes kanban, NOT HSCC."
version: 3.0.0
author: Hermes Agent
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [HSCC, Cluster, Provisioning, Monitoring]
    related_skills: [hscc-cluster, hscc-model-onboard]
---

# Hermes Spark Cluster Control (HSCC)

**Post-refactor (2026-06-08): HSCC is a thin cluster-PHYSICAL layer.** The duplicated agent pipeline (coordinator, projects, orchestrator, events, governance, MCP server) was archived. Agent work now runs on **native Hermes kanban**, not HSCC.

Cluster: gateway/orchestrator node `192.0.2.10` (always-on vLLM serving Telegram + all Hermes agents), worker GPU nodes `.246 / .247 / .248` (provisioned on demand), NAS `.249`. Controlled via `sparkrun`. State in `~/.hscc/`.

## Two responsibilities, two surfaces

### 1. Agent work = native Hermes kanban (NOT HSCC)
Drive all project work through the kanban tools / `hermes kanban`. Create a task (`kanban_create` or `hermes kanban create "<task>"`) → it lands in triage → auto-decomposes into todo cards (`kanban.auto_decompose`) → the gateway's **embedded dispatcher** (`kanban.dispatch_in_gateway: true`, 60s tick) runs each ready card in its own git worktree as a worker agent → moves it to review. `default_assignee: worker-246`. Inspect with `hermes kanban boards`. **Do NOT hand-manage worktrees or dispatch** — the gateway does it. There is no more `dispatch-task`/`release-task`/`merge-worktree` CLI.

### 2. Cluster physical ops = hscc-cluster toolset
Change cluster shape via the toolset (orchestrator profile only). See the **`hscc-cluster`** skill. Tools:
- **reads**: `cluster_status`, `model_health`, `list_recipes`, `vllm_logs`, `node_diagnostics`, `nas_diagnose`
- **ops** (confirm-gated): `provision_model` (node='auto' picks idle worker, returns live base_url), `stop_model`, `restart_model`
- **heal** (confirm-gated): `remount_nas`, `repair_nas_export`, `reap_orphans`

Guarded tools return a preview unless you pass `confirm=true`.

## Live plugins

| Plugin | Role |
|---|---|
| **hscc-cluster** | Cluster ops toolset (sparkrun wrapper, incl. `provision_model`) — the live tool surface |
| **hscc_daemon** | Monitoring/self-heal daemon (launchd `com.hermes.hscc_daemon`) |
| **hscc-roles** | Agent role/profile definitions |
| **hscc-skills** | Idempotent skill/template installer |
| **hscc-bootstrap** | One-command init (skill install → state → gateway → cluster checks) |
| **hscc-commands** | HSCC slash-command surface |
| **sparkrun-hermes** | sparkrun integration for Hermes |

Model container lifecycle (provision/stop/restart) is now part of the **hscc-cluster** toolset (`provision_model` tool) — the old `hscc-provision` plugin/skill was archived. Onboarding a NEW model/quant cluster-wide (recipe + offline NAS→node cache + wire configs + launch + verify): use the **`hscc-model-onboard`** skill (e.g. FP8 → NVFP4 cutover).

**Archived 2026-06-08** (in `_archive/2026-06-08/`): hscc-agent-coordinator, hscc-projects, hscc-orchestrator, hscc-events, hscc-governance, hscc-mcp. **Archived 2026-06-10**: hscc-provision (its `provision_model` moved into hscc-cluster), hscc-chat, hscc-optimizations. Their skills are archived too. If you see a reference to `mcp_hscc_*` tools, `dispatch-task`, `release-task`, `assign-task`, or any of those plugins anywhere, it is STALE — the function moved to native kanban (dispatch) or the hscc-cluster toolset (provisioning).

## Daemon (hscc_daemon)

Only continuous process. Live code: `~/.hermes/plugins/hscc_daemon/hscc.py` (run `start-daemon` foreground under launchd `com.hermes.hscc_daemon`). Polling streams write `~/.hscc/state/*.json`; a trigger engine evaluates `~/.hscc/events.jsonl`. Notifications are cross-platform desktop (macOS osascript / Linux notify-send, falling back to `~/.hscc/notifications.json` when neither is available) + `events.jsonl` — the daemon does **not** post to Telegram.

- **Daemon health check**: `ps aux | grep hscc_daemon | grep -v grep` — `launchctl list | grep hscc` exit code is often STALE (`-9` while running). Trust the process list + `hscc.py status`.
- **Daemon node resolution**: resolves `PRIMARY_NODE` (=gateway .244, runs the orchestrator vLLM) / `NAS_HOST` from `cluster.json` → `sparkrun cluster list --json` → hardcoded defaults. `_rebuild_vllm_cmds()` must run after any `PRIMARY_NODE` change.

## Telegram Operations-topic messages = CRON, not the daemon

Two Hermes cron jobs (`~/.hermes/cron/jobs.json`) deliver to `telegram:0:140` (Operations topic):
- `138f8b56d790` orch-tick — every 1m, interpreting agent
- `381ef65e40f5` idle-monitor — every 5m, `scripts/idle-monitor.py` (no_agent, verbatim). **PAUSED 2026-06-08**: it keys off stale `~/.hscc/agents.json` + absent `lifecycle.json` and would reap native-provisioned worker vLLMs as "orphan". Resume: `hermes cron resume 381ef65e40f5` — but port the daemon's BRIDGE_FILE guard first.

Manage: `hermes cron list|pause|resume <id>`.

## Model name must agree in 3 places

Clients query vLLM by exact model name; a stale name = 404 even when the server is healthy. Keep these in sync with what `curl http://192.0.2.10:8000/v1/models` reports (`data[0].id`):
1. `~/.hermes/config.yaml` `model.default` — THE big one (every agent + cron with `model:null` inherits it)
2. `~/.hscc/models.json` `primary_model` + `models[].name`
3. `~/.hscc/serving.json` unit `model`/`recipe`

`hermes status` shows the effective `Model:` live (no restart needed). `hermes model` is interactive OAuth only — set the default by editing `config.yaml` directly. Best practice: never hardcode the quant suffix; resolve from `/v1/models`.

## Convention

- Restart the gateway after any prompt/plugin/config edit: `launchctl kickstart -k gui/$UID/ai.hermes.gateway`.
- Dual-layout: when editing a live plugin/skill, sync its `install/hscc-skills/` (skills) or `install/hscc-plugins/` (plugins) template to avoid drift. The `hscc-skills` installer's `BUNDLED_SKILLS` list controls what gets (re)seeded.
- Never edit sparkrun recipes; switch to an alternative if one breaks. Never patch Hermes core.
- "Strip/archive" = MOVE to `_archive/<date>/`, never delete.

## References (live subsystems)

- `references/event-driven-architecture.md` — daemon kqueue + launchd event system
- `references/monitoring-daemon-design.md` — daemon health checks, circuit breakers, graded alerting
- `references/cron-job-management.md` — cron workflow (list before create, update vs create, delivery targets)
- `references/cron-job-context-accumulation.md` — use `no_agent: true` + script for stateless cron jobs
- `references/x-feed-cron-design.md` / `references/xurl-api-quirks.md` — X feed cron design + API quirks
- `references/multi-node-provisioning-troubleshooting.md` — multi-node vLLM provisioning failure modes
- `references/session-continuity-multi-repo.md` — finding uncommitted work across repos
- `references/version-update-procedure.md` — Hermes version update workflow

> Dead-pipeline reference docs (orchestrator-tick-flow, task-creation-workflow, agents-data-model, permissions-reference, integration-assign-task-provision, kanban-sync-fix, stale-agent-cleanup, idle-monitor-integration, event-types, hscc-13-plugins-audit) were archived 2026-06-08 to `_archive/2026-06-08/hscc-references/` — they describe the removed agent pipeline.
