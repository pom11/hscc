---
name: hscc-default-tools
description: HSCC is the primary toolset for cluster-physical work. Agent dispatch and project/task work are native Hermes kanban, NOT HSCC.
---

# HSCC as Primary Toolset

Hermes Spark Cluster Control (HSCC) is the primary toolset for cluster-PHYSICAL work (provision/stop/heal models, node pause, monitor, NAS). Agent dispatch and project/task work run on **native Hermes kanban**, not HSCC. The parent **`hscc`** skill carries the full current CLI surface and the per-verb-group rules; this doc is the quick map.

## When to use
- Any cluster/node/metrics work → the `hscc` CLI and the `hscc-cluster` plugin tools
- Any model/container lifecycle → the `hscc-cluster` tools `provision_model` / `stop_model` / `restart_model` (never hand-built `sparkrun run` against the fleet)
- Parking one node → `hscc node pause|resume|paused` (or the `node_pause`/`node_resume`/`node_paused` tools); a model spanning several nodes is refused with no override
- Idle fleet power → `hscc autodown` — operator policy: stays OFF; agents only read (`autodown_status`)
- Any agent dispatch / project / task management → **native Hermes kanban** (`hermes kanban`, `kanban_*` tools) — NOT HSCC
- Any event/logging queries → daemon state in `~/.hscc/state/*.json` + `~/.hscc/events.jsonl` (read via `hscc status`, `hscc log`, `hscc triggers`)

## HSCC CLI
`hscc` on PATH (brew formula, module `hscc_cli`; it forwards to the DEPLOYED code — a checkout edit is not live until installed). The authoritative verb list prints on any unknown verb: `hscc nosuchverb`. Groups: `cluster` (status/hosts/monitor/jobs/info/stop/up/down/history), `template` (list/status/preview/validate/apply), `autodown` (status/enable/disable/wake/cancel), `kanban` (blocked/stale/monitor/pause/resume/paused/enforce), `cards` (monitor), `merge-queue` (status/list/add/remove/pause/resume/enable/disable), `orch` (inject, waker), `main` (start/status/wakeups), `node` (pause/resume/paused), `api`, plus daemon/service verbs (`status`, `verify`, `stats`, `throughput`, `autoscale`, `escalate`, `heal-status`, `heal-clear`, `notify`, `check`, `watch`, `triggers`, `log`, `plist`, `install`, `uninstall`, `bootstrap`, `update`, `start`, `stop`, `start-daemon`).

## hscc-cluster agent tools (registered with Hermes — call by name)
Reads: `cluster_status`, `list_recipes`, `pick_node`, `discovery_status`, `nas_status`, `node_paused`, `autodown_status`, `merge_queue_status`, `model_health`, `vllm_logs`, `node_diagnostics`, `nas_diagnose`, `cluster_verify`, `cluster_throughput`, `fleet_stats`, `autoscale_advice`, `list_templates`, `preview_template`, `orch_message`
Guarded (preview unless `confirm=true`): `provision_model`, `stop_model`, `restart_model`, `node_pause`, `node_resume`, `remount_nas`, `repair_nas_export`, `reap_orphans`, `apply_template`

## Agent dispatch / projects / tasks → native Hermes kanban
These are **no longer HSCC plugins**. The old `hscc-orchestrator`, `hscc-agent-coordinator`, `hscc-projects`, `hscc-events`, `hscc-soul`, and `hscc-governance` plugins were archived. Use native kanban instead:

- Create work: `hermes kanban create "<task>"` (or `kanban_create`) → triage → auto-decompose → the gateway's embedded dispatcher runs each ready card in its own git worktree.
- Inspect: `hermes kanban boards`; hygiene reads: `hscc kanban blocked|stale|monitor`.
- Do NOT hand-manage worktrees or call `dispatch-task`/`release-task`/`assign-task` — those CLIs are gone.

### sparkrun-hermes (low-level, debug)
`python3 ~/.hermes/plugins/install/hscc-plugins/sparkrun-hermes/hscc.py <cmd>` (formerly hscc-sparkrun) passes through to the SAME sparkrun underneath (`cluster list --json`, `container status --json`, …) — same transport, same facts, no second truth.

## Key Rules
1. **sparkrun owns the fleet.** Every fleet fact goes through the `hscc` CLI or the `hscc-cluster` tools (which delegate to sparkrun). Never raw `ssh` to a node, never local `docker` for fleet facts — a local `docker ps` on the daemon host answers *empty*, not *error*.
2. **Never bypass HSCC** for cluster ops — where a tool exists for a verb it calls the CLI's own verb function; do not build a second path.
3. **Autodown stays OFF** (operator policy): read it (`hscc autodown status` / `autodown_status`), never arm it; enable/disable/wake/cancel are CLI + operator scope.
4. **Output for chat surfaces** (REPL, app): strip ANSI codes, use native markdown (`*bold*`, `_italic_`, ```code```).
5. **No AI postprocessing** on cluster status — raw terminal data only.
6. State lives in `~/.hscc/` (readers take an explicit `hscc_dir`/`$HSCC_HOME`; some modules key off `HSCC_DIR`).
7. Node addresses in docs/commits are placeholders: gateway=192.0.2.10, workers=.246/.247/.248, NAS=.249 (TEST-NET). The repo is public — never a live address.

## Container Lifecycle Rules
- Native-provisioned worker vLLMs (via kanban dispatch / `provision_model`) are managed by the gateway dispatcher and the daemon's keep-alive — let those own teardown rather than hand-killing containers.
- The legacy idle monitor (cron `381ef65e40f5`, `scripts/idle-monitor.py`) is **PAUSED 2026-06-08**: it keyed off the now-stale `~/.hscc/agents.json` + absent `lifecycle.json` and would reap native-provisioned worker vLLMs as "orphan". Do not resume it without porting the daemon's BRIDGE_FILE guard first. Its successor is `hscc autodown` — which per operator policy stays OFF.
- Diagnose container teardown: check the daemon's last scan in `~/.hscc/state/*.json` and `~/.hscc/events.jsonl`.

## Pitfalls
- **vLLM Qwen3.6 `--reasoning-parser qwen3` bug**: causes all model output to go to `reasoning` field with `content: null`. Fix: remove `--reasoning-parser` from recipe. Also remove `--chat-template unsloth.jinja` if template file doesn't exist — both cause container startup failures.
- **sparkrun run appears stuck during weight loading**: vLLM model loading can take 15+ minutes for large models. The process output may not update for minutes. Use `process(action='poll')` to check for new output instead of `wait`, which can falsely timeout. Container may still be healthy even if output appears frozen.
- **CLI**: `hscc` (brew, `hscc_cli`) — forwards to the deployed code.
- **Plugins**: `~/.hermes/plugins/`; **State**: `~/.hscc/`; **Hermes config**: `~/.hermes/config.yaml`.

## Reference Files
- `references/daemon-integration-pattern.md` — How to integrate a standalone check/cron job into the HSCC daemon's periodic loop.
