---
name: hscc-architecture
description: "Building Hermes-native cluster management components (hscc-*). Covers architecture, naming, patterns, and component mapping."
category: devops
---

# HSCC (Hermes Spark Cluster Control) Architecture

Building standalone Python/Shell components for cluster management. All components use the `hscc-` prefix.

## Component Types

| Type | Location | Format | Example |
|------|----------|--------|---------|
| **hscc-plugins** | `~/.hermes/plugins/hscc-*` | Python/Shell scripts | `hscc_cluster.py` |
| **hscc-skills** | `~/.hermes/skills/hscc-*` | SKILL.md + references/ | `hscc-cluster/SKILL.md` |
| **hscc-soul** | `~/.hermes/memory/` | Markdown files | `hscc-soul.md` |
| **hscc-tools** | `~/.hermes/memory/` | Markdown reference | `hscc-tools.md` |
| **hscc-agents** | `~/.hermes/agents/` | JSON/markdown | `agents.json` |

## Naming Convention

**All components use `hscc-` prefix** (Hermes Spark Cluster Control):
- `hscc-cluster` — cluster status, hosts, jobs, GPU monitoring
- `hscc-agents` — agent fleet management, lifecycle, dispatch
- `hscc-projects` — kanban boards, roadmaps, task tracking
- `hscc-sparkrun` — model run/stop/monitor via sparkrun CLI
- `hscc-soul` — orchestrator identity and rules
- `hscc-tools` — infrastructure reference (node table, commands)
- `hscc-worktrees` — project workspace management

## Implementation Pattern

**Python preferred over TypeScript.** Each hscc plugin is a standalone Python script that:
1. Accepts commands via CLI args or stdin
2. Outputs structured JSON
3. Wraps `sparkrun` CLI or SSH calls to cluster nodes
4. Reads config from `~/.hscc/` state files

## Key Data Sources

| File | Contents |
|------|----------|
| `~/.hscc/cluster.json` | Host definitions (gateway, workers, NAS) |
| `~/.hscc/agents.json` | Agent fleet (roles, state) |
| `~/.hscc/projects.json` | Projects, tasks, kanban state |
| `~/.hscc/network.json` | Network config |
| `~/.hscc/events.jsonl` | Event log |
| `~/.config/sparkrun/clusters/` | sparkrun cluster YAML definitions |

**State location**: All HSCC state lives under `~/.hscc/` (override with the `HSCC_HOME` env var).

## hscc Plugins

- `hscc-agents` — agent CRUD, fleet status, dispatch
- `hscc-cluster` — status, hosts, monitor, jobs
- `hscc-projects` — kanban, tasks, roadmaps
- `hscc-agent-coordinator` — lifecycle FSM, worktrees, recovery, executor bridge
- `hscc-events` — event log/search
- `hscc-notifications` — alerting
- `hscc-permissions` — access control
- `hscc-policy` — execution policies
- `hscc-soul` — orchestrator context
- `hscc-triggers` — event-driven triggers
- `hscc-gateway` — service management

## Cluster Architecture

- **Gateway node**: 192.0.2.10 (orchestrator)
- **Worker nodes**: .246, .247, .248 (GB10, 128GB each)
- **NAS**: 192.0.2.20 (QNAP, NFS mount at /mnt/nas)
- **sparkrun CLI**: installed at `/opt/homebrew/bin/sparkrun`
- **Default cluster**: "hscc" (all 4 nodes)

## Pitfalls

- Do NOT build TypeScript plugins for hscc — use Python standalone scripts
- Do NOT reuse any legacy plugin naming for new components — use `hscc-*`
