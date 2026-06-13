---
name: hscc-architecture
description: "Building Hermes-native cluster management components (hscc-*). Covers architecture, naming, patterns, and component mapping."
category: devops
version: 1.0.0
license: MIT
metadata.hermes.tags: []
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

**All components use `hscc-` / `hscc_` prefix** (Hermes Spark Cluster Control):
- `hscc-cluster` — cluster status, hosts, jobs, GPU monitoring, `provision_model`
- `hscc_daemon` — monitoring/self-heal daemon
- `hscc-roles` — agent role/profile definitions
- `sparkrun-hermes` — model run/stop/monitor via sparkrun CLI (formerly hscc-sparkrun)
- `hscc-tools` — infrastructure reference (node table, commands)

> Agent fleet/dispatch, kanban boards/tasks, worktrees, soul files are now **native Hermes kanban**, not HSCC plugins. The old `hscc-agents`/`hscc-projects`/`hscc-soul`/`hscc-worktrees` components were archived 2026-06-08.

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

## hscc Plugins (active)

- `hscc-cluster` — status, hosts, monitor, jobs, `provision_model`/`stop_model`/`restart_model`
- `hscc_daemon` — monitoring/self-heal daemon (launchd)
- `hscc-roles` — agent role/profile definitions
- `hscc-skills` — skill/template installer
- `hscc-bootstrap` — one-command init
- `hscc-commands` — slash-command surface
- `sparkrun-hermes` — sparkrun CLI integration

> **Archived 2026-06-08** (now native Hermes kanban or gone): hscc-agents, hscc-projects, hscc-agent-coordinator, hscc-events, hscc-notifications, hscc-permissions, hscc-policy, hscc-soul, hscc-triggers, hscc-gateway, hscc-orchestrator, hscc-governance, hscc-mcp. **Archived 2026-06-10**: hscc-provision (its `provision_model` moved into hscc-cluster), hscc-chat, hscc-optimizations. If you see these referenced as current, it is STALE.

## Cluster Architecture

- **Gateway node**: 192.0.2.10 (orchestrator)
- **Worker nodes**: .246, .247, .248 (GB10, 128GB each)
- **NAS**: 192.0.2.20 (QNAP, NFS mount at /mnt/nas)
- **sparkrun CLI**: installed at `/opt/homebrew/bin/sparkrun`
- **Default cluster**: "hscc" (all 4 nodes)

## Pitfalls

- Do NOT build TypeScript plugins for hscc — use Python standalone scripts
- Do NOT reuse any legacy plugin naming for new components — use `hscc-*`
