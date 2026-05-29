---
name: hscc-default-tools
description: HSCC is the primary toolset for all cluster, agent, and project work. Always go through HSCC; never bypass it.
---

# HSCC as Primary Toolset

Hermes Spark Cluster Control (HSCC) is the primary toolset for all cluster, agent, and project work.

## When to use
- Any cluster/node/metrics work → `hscc-cli` and `hscc-cluster` plugin
- Any agent management → `hscc-orchestrator` and `hscc-agent-coordinator` plugins
- Any project/task management → `hscc-projects` plugin
- Any model/container lifecycle → `hscc-provision` plugin
- Any event/logging queries → `hscc-events` plugin
- Any governance/policy → `hscc-governance` plugin

## HSCC CLI Commands
Located at: `~/.hermes/hscc/bin/hscc`

| Command | Description |
|---|---|
| `hscc init` | Bootstrap HSCC: install plugins, skills, templates |
| `hscc status` | System health: Python, Git, CLI version, plugins, state, daemon, nodes |
| `hscc chat` | Hermes gateway chat via WebSocket streaming |
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

### hscc-orchestrator
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-orchestrator/hscc.py <cmd> [args]`

- `fleet` — Fleet summary: total, idle, failed, busy, enabled/disabled by role
- `agents` — Detailed list of all agents
- `status` — Quick status: count by role, idle/failed/etc.
- `show <agent_id>` — Show details for a specific agent
- `configure <agent_id> <field> <value>` — Update agent field (temperature, model, etc.)
- `enable <agent_id>` — Enable a disabled agent
- `disable <agent_id>` — Disable an agent (keeps in fleet)
- `available` — Show agents currently idle and available
- `route <agent_id> <task_description>` — Assign a task to an agent

### hscc-provision
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-provision/hscc.py <cmd> [args]`

- `status` — Overview: agent/recipe/container count, idle hosts
- `list` — List available recipes
- `run <recipe>` — Run a recipe
- `stop` — Stop containers
- `assign <agent>` — Assign agent
- `unassign <agent>` — Unassign agent
- `health` — Health check
- `cleanup` — Cleanup resources
- `registry` — Registry management

### hscc-projects
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-projects/hscc.py <cmd> [args]`

- `list` — List all projects
- `create <name> <desc>` — Create project
- `show` — Full project detail
- `status` — Task status summary
- `list-projects` — Projects with counts
- `add-roadmap`, `add-subproject`, `add-task` — Build hierarchy
- `update-task`, `move-task`, `assign-task` — Manage tasks
- `list-agents` — Agents + assignments
- `search <query>` — Search tasks

### hscc-events
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-events/hscc.py <cmd> [args]`

- `events` — List recent events
- `event-count` — Count of events
- `lifecycle` — Agent lifecycle events
- `lifecycle-show <agent>` — Lifecycle events for specific agent
- `recovery` — Recovery events
- `recovery-detail <id>` — Recovery event details
- `notifications` — Pending notifications
- `notify` — Send notification
- `notify-read <id>` — Mark notification read
- `notify-clear` — Clear notifications
- `rules` — Event rules
- `rule-add`, `rule-remove`, `rule-reset-cooldown` — Rule management
- `policy` — Policy management
- `policy-add`, `policy-remove` — Policy rules
- `perms` — Permissions
- `clear-recovery` — Clear recovery data
- `clear-notifications` — Clear all notifications
- `compact` — Compact logs

### hscc-soul
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-soul/hscc.py <cmd> [args]`

- `list` — List all agent soul files
- `get <agent_id>` — View soul file for agent
- `update <agent_id> <file>` — Update agent soul file
- `validate` — Validate all soul files

### hscc-sparkrun
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-sparkrun/hscc.py <cmd>`

- `version` — Sparkrun CLI version
- `cluster status` — Cluster status
- `cluster list` — List cluster nodes
- `cluster list --json` — List as JSON (used by daemon)
- `container status` — Container status
- `container list` — List containers
- `container status --json` — List as JSON

### hscc-governance
`python3 ~/.hermes/plugins/install/hscc-plugins/hscc-governance/hscc.py <cmd>`

- `audit-log` — View audit log
- `policy list` — List active policies
- `policy check <action>` — Check if action is allowed

## Key Rules
1. **Always prefer HSCC** over raw shell commands for cluster/agent work
2. **Never bypass HSCC** — go through HSCC plugins, not the gateway directly
3. **Output to Telegram**: strip ANSI codes, use native markdown (`*bold*`, `_italic_`, ```code```)
4. **No AI postprocessing** on cluster status — raw terminal data only
5. State lives in `~/.hscc/` (override with the `HSCC_HOME` env var)
6. Node IPs: gateway=192.0.2.10, workers=246/247/248, NAS=249

## Container Lifecycle Rules
- **NEVER start a sparkrun container without assigning work first.** The idle monitor (runs every 5 min via daemon, kills agents idle > 30 min) will shut down containers with no active agent tasks. This causes a cycle: start container → no work → idle monitor kills → start again.
- **Correct order**: route a task to agents → verify agent state is "running" → start containers. The agent state update prevents the idle monitor from killing the container during provisioning.
- The idle monitor is **integrated into the HSCC daemon** (every 5 min via launchd). Old separate cron job `9508e87f9729` was removed. No manual pausing needed.
- Diagnose idle kills: `python3 ~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/hscc.py idle-monitor --dry-run` or check `~/.hscc/state/idle.json` for the last daemon scan. Agent state/lifecycle in `~/.hscc/lifecycle.json`.

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
