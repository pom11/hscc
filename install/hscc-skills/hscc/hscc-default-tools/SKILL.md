---
name: hscc-default-tools
description: HSCC is the primary toolset for cluster-physical work. Agent dispatch and project/task work are native Hermes kanban, NOT HSCC.
version: 1.0.0
license: MIT
metadata.hermes.tags: []
---

# HSCC as Primary Toolset

Hermes Spark Cluster Control (HSCC) is the primary toolset for cluster-PHYSICAL work (provision/stop/heal models, monitor, NAS). Agent dispatch and project/task work run on **native Hermes kanban**, not HSCC.

## When to use
- Any cluster/node/metrics work → `hscc-cli` and `hscc-cluster` plugin
- Any model/container lifecycle → the `hscc-cluster` toolset's `provision_model`/`stop_model`/`restart_model` tools (or `sparkrun run` directly)
- Any agent dispatch / project / task management → **native Hermes kanban** (`hermes kanban`, `kanban_*` tools) — NOT HSCC
- Any event/logging queries → daemon state in `~/.hscc/state/*.json` + `~/.hscc/events.jsonl`

## HSCC CLI Commands
Located at: `~/.hermes/hscc/bin/hscc`

| Command | Description |
|---|---|
| `hscc init` | Bootstrap HSCC: install plugins, skills, templates |
| `hscc status` | System health: Python, Git, CLI version, plugins, state, daemon, nodes |
| `hscc version` | Print version |
| `hscc reset` | Reset: clear state, remove agents, clean worktrees, stop containers |
| `hscc cluster cluster-status` | Full cluster status: workloads, system metrics, config |
| `hscc cluster status` | Cluster overview |

## Plugin Commands (Python scripts)

### hscc-cluster
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-cluster/hscc.py <cmd> [args]`

- `sparkrun-args <cmd> [args]` — Delegate directly to `sparkrun <cmd> [args]`
- `sparkrun-workloads` — Current DGX Spark workloads
- `node-system` — System metrics for a node (CPU, memory, disk, GPU, temp)
- `node-temp <node>` — Temperature for specific node
- `gateway-health` — Gateway health check
- `cluster-config` — Cluster configuration
- `nas-status` — NAS/QNAP status
- `provision_model` / `stop_model` / `restart_model` — model container lifecycle (confirm-gated; absorbed from the archived hscc-provision plugin)

### Agent dispatch / projects / tasks → native Hermes kanban

These are **no longer HSCC plugins**. The old `hscc-orchestrator`, `hscc-agent-coordinator`, `hscc-projects`, `hscc-events`, `hscc-soul`, and `hscc-governance` plugins were archived. Use native kanban instead:

- Create work: `hermes kanban create "<task>"` (or `kanban_create`) → triage → auto-decompose → the gateway's embedded dispatcher runs each ready card in its own git worktree.
- Inspect: `hermes kanban boards`.
- Do NOT hand-manage worktrees or call `dispatch-task`/`release-task`/`assign-task` — those CLIs are gone.

### sparkrun-hermes
`python3 ~/.hermes/plugins/install/hscc-plugins/sparkrun-hermes/hscc.py <cmd>` (formerly hscc-sparkrun)

- `version` — Sparkrun CLI version
- `cluster status` — Cluster status
- `cluster list` — List cluster nodes
- `cluster list --json` — List as JSON (used by daemon)
- `container status` — Container status
- `container list` — List containers
- `container status --json` — List as JSON

## Key Rules
1. **Always prefer HSCC** over raw shell commands for cluster-physical work
2. **Never bypass HSCC** for cluster ops — go through the hscc-cluster toolset (agent dispatch goes through native kanban)
3. **Output to Telegram**: strip ANSI codes, use native markdown (`*bold*`, `_italic_`, ```code```)
4. **No AI postprocessing** on cluster status — raw terminal data only
5. State lives in `~/.hscc/` (override with the `HSCC_HOME` env var)
6. Node IPs: gateway=192.0.2.10, workers=246/247/248, NAS=249

## Container Lifecycle Rules
- Native-provisioned worker vLLMs (via kanban dispatch / `provision_model`) are managed by the gateway dispatcher and the daemon's keep-alive — let those own teardown rather than hand-killing containers.
- The legacy idle monitor (cron `381ef65e40f5`, `scripts/idle-monitor.py`) is **PAUSED 2026-06-08**: it keyed off the now-stale `~/.hscc/agents.json` + absent `lifecycle.json` and would reap native-provisioned worker vLLMs as "orphan". Do not resume it without porting the daemon's BRIDGE_FILE guard first.
- Diagnose container teardown: check the daemon's last scan in `~/.hscc/state/*.json` and `~/.hscc/events.jsonl`.

## Pitfalls
- **vLLM Qwen3.6 `--reasoning-parser qwen3` bug**: causes all model output to go to `reasoning` field with `content: null`. Fix: remove `--reasoning-parser` from recipe. Also remove `--chat-template unsloth.jinja` if template file doesn't exist — both cause container startup failures.
- **sparkrun run appears stuck during weight loading**: vLLM model loading can take 15+ minutes for large models. The process output may not update for minutes. Use `process(action='poll')` to check for new output instead of `wait`, which can falsely timeout. Container may still be healthy even if output appears frozen.
- CLI: `~/.hermes/hscc/bin/hscc`
- Plugins: `~/.hermes/plugins/install/hscc-plugins/`
- State: `~/.hscc/`
- Agents state: `~/.hermes/plugins/plugin-state/`
- Hermes config: `~/.hermes/config.yaml`

## Reference Files
- `references/daemon-integration-pattern.md` — How to integrate a standalone check/cron job into the HSCC daemon's periodic loop.
