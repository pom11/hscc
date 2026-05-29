---
name: hscc-orchestrator
description: 'Manage the agent fleet: view, configure, enable/disable, and route tasks.'
tags: [hsc, agents, fleet, routing, configuration, task-assignment]
---

# HSCC Agent Orchestrator

Manage the agent fleet: view, configure, enable/disable, and route tasks.

## When to use

- User wants to see agent fleet status or configure agents
- User asks which agents are available or their current status
- User wants to enable/disable specific agents
- User needs to route tasks to agents or check agent details

## Commands

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py <command> [args]
```

### `fleet`
List all agents with a compact status summary (counts by role, status).

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py fleet
```

### `agents`
Detailed list of all agents with role, status, model, and tools.

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py agents
```

### `show <agent_id>`
Show full details for a specific agent, including current task assignments.

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py show dev-001
```

### `configure <agent_id> <field> <value>`
Update an agent field. Supported fields: `temperature`, `model`, `tools`, `mcpServers`, `skills`, `maxTokens`, `enabled`, `systemPrompt`.

For multi-value fields (`tools`, `mcpServers`, `skills`), use comma-separated values:

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py configure dev-001 temperature 0.5
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py configure dev-001 tools "filesystem,shell,web"
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py configure dev-001 enabled false
```

### `enable <agent_id>`
Enable a disabled agent.

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py enable dev-002
```

### `disable <agent_id>`
Disable an agent (resets its status to idle).

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py disable dev-002
```

### `available`
Show only agents that are enabled and idle.

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py available
```

### `status`
Quick JSON summary: total, counts by status/role/enabled.

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py status
```

### `route <agent_id> <task_description>`
Mark an agent as working (route a task to it).

```bash
python3 ~/.hermes/plugins/hscc-orchestrator/hscc.py route dev-001 "Implement context pressure model"
```

## Agent data

Agent definitions come from `~/.hscc/agents.json`:
- `dev-001` through `dev-020` — role: `developer`, name: `Builder`
- `merge-001` — role: `reviewer`, name: `Merger`

Task assignments are linked from `~/.hscc/projects.json` (cross-referenced by `assignedAgent` field).

## Pitfalls

### NEVER start containers without work assigned

**The idle monitor will kill any container that has no active work.** The hscc-daemon's idle monitor scans every 5 minutes and stops containers whose agents are idle for >30 minutes (default).

Workflow must be:
1. **Assign work** to agents first (`route`, delegate tasks, cron jobs)
2. **Then** start provision containers

Starting containers and then doing nothing = wasted GPU hours + auto-killed containers.

### Runs inside hscc-daemon

The idle monitor is the `idle` periodic stream of the **hscc-daemon** (every 5 min),
alongside its dgx/gateway/local/nas/heartbeat health checks. It is not a separate
cron job. To check it is running:
```bash
python3 ~/.hermes/plugins/hscc-daemon/hscc.py status
tail -f ~/.hscc/daemon.log   # look for "Running idle monitor check"
```
To pause it, stop the daemon (`hscc.py stop`).

## Tips

- Use `fleet` for a quick overview, `agents` for full details
- Use `available` before routing to find idle agents
- Use `show <id>` to see an agent's current tasks from the projects plugin
- Agent config changes are saved directly to `agents.json` — they persist across sessions
