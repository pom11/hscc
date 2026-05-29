---
name: hscc
description: "Hermes Spark Cluster Control — umbrella for 12 hscc-* plugins covering cluster, agents, projects, monitoring, governance, chat, and orchestration"
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [HSCC, Cluster, Agents, Projects, Orchestration, Monitoring, Governance]
    related_skills: [hscc-cluster, hscc-projects, hscc-orchestrator, hscc-events, hscc-provision, hscc-agent-coordinator]
---

# Hermes Spark Cluster Control (HSCC)

Hermes-native cluster control: 12 Python plugins with JSON state persistence.
**12 plugins, ~9,100 lines total. Fully standalone.**

## When to Use

- Managing the DGX Spark GPU cluster (`hscc-cluster`)
- Managing projects, tasks, and kanban boards (`hscc-projects`)
- Managing the agent fleet — 21 agents with roles, status, configuration (`hscc-orchestrator`)
- Event logging, lifecycle, notifications, recovery, permissions, triggers (`hscc-events`)
- Monitoring & self-healing: daemon with watchdog, kqueue event-driven detection, launchd service (`hscc-daemon`)
- **Chat & streaming**: WebSocket gateway client with session persistence + markdown (`hscc-chat`) — **STALE**: users already interact via Telegram. Keep code for future use but do NOT prioritize.
- Agent coordination: lifecycle state machine + worktrees + recovery merged + auto-provisioning (`hscc-agent-coordinator`)
- Governance: policy engine + permission proxy + audit log + RBAC (`hscc-governance`)
- Skills/templates auto-install: idempotent bootstrap from HSCC source (`hscc-skills`)
- One-command provisioning: skill install + state validation + health checks (`hscc-bootstrap`)
- Optimization analysis: merge proposals + event-driven pattern detection (`hscc-optimizations`)

Key facts:
- State lives in `~/.hscc/` (override with the `HSCC_HOME` env var)
- 12 plugins, fully standalone Python (no external plugin dependencies)

## Architecture

### Plugin Layout

```
~/.hermes/plugins/hscc-<component>/hscc.py   ← Python tool script
~/.hermes/skills/hscc-<component>/SKILL.md   ← SKILL.md usage doc (optional)
```

State lives in `~/.hscc/` (override with the `HSCC_HOME` env var).

### Complete Plugin Inventory

| Plugin | Lines | Description |
|---|---|---|
| **hscc-daemon** | 1,758 | Monitoring daemon, 5 timer streams, PipelineWatchdog, kqueue event-driven, launchd service |
| **hscc-chat** | 1,578 | WebSocket gateway client, Ed25519+token auth, auto-reconnect, session persistence, markdown renderer |
| **hscc-agent-coordinator** | 1,545 | Lifecycle FSM + worktrees + recovery merged from 3 plugins, max-3 running guard, orphan detection |
| **hscc-governance** | 1,120 | Policy eval (eq/ne/in/gt/lt operators), 4-tier RBAC, append-only audit log, permission proxy |
| **hscc-cluster** | 289 | Cluster operations (sparkrun wrapper) |
| **hscc-provision** | 872 | Model management (HuggingFace, NAS sync, model-check.py) |
| **hscc-projects** | 499 | Kanban/project management |
| **hscc-skills** | 469 | Auto-install 7 skills + 6 templates from HSCC source, hash-diff idempotency |
| **hscc-events** | 617 | Event bus, lifecycle, notifications, recovery, permissions, triggers |
| **hscc-orchestrator** | 329 | Agent dispatch (21 agents) |
| **hscc-bootstrap** | bash | One-command: skill install → state validation → gateway check → cluster check |
| **hscc-optimizations** | 863 | Merge proposal + event-driven pattern detector |

## Plugin Pattern

Every hscc-* plugin follows this structure:

```python
#!/usr/bin/env python3
"""HSCC - <Component> Plugin — Usage: hscc-<component> <command> [args]"""

import sys, json, os

def load_state():
    with open(CONSTANTS) as f:
        return json.load(f)

def save_state(data):
    with open(CONSTANTS, "w") as f:
        json.dump(data, f, indent=4)

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {"cmd1": cmd1_fn, "cmd2": cmd2_fn}

    if cmd not in commands:
        print(json.dumps({"error": f"Unknown command: {cmd}"}))
        sys.exit(1)
    commands[cmd]()

if __name__ == "__main__":
    main()
```

Key conventions:
- Exit code 0 on success, 1 on error
- JSON output for machine-readable results, text for human-readable summaries
- Make the script executable: `chmod +x hscc.py`
- Always use `os.path.expanduser()` for paths
- Multi-value fields (tools, mcpServers, skills) use comma-separated input
- **NO SSH or network requests** — local file I/O only
- Use `write_file` for large outputs, `patch` for targeted edits

## Event-Driven Architecture (hscc-daemon)

The daemon supports two operating modes:

### Adding a New Periodic Check
When adding a new periodic check stream, you MUST update THREE places:

1. `hscc-daemon/hscc.py` — add to `STREAMS` dict (name + interval in seconds)
2. `hscc-daemon/hscc.py` — add to `check_map` in `_run_event_driven_daemon()`
3. `hscc-daemon/event_driven.py` — add to `PERIODIC_STREAMS` dict AND `STATE_STREAMS` set

If you only update `STREAMS` and `check_map` but forget `event_driven.py`, the launchd
plist will NOT be generated for the new stream and it won't fire on schedule. Always
verify with `launchctl list | grep hscc-periodic.<stream>` after restart.

### Idle Monitor
The idle monitor runs inside the daemon every 5 minutes (was a separate cron job, removed May 2026).
It scans sparkrun containers, stops orphaned ones, and kills agents idle beyond the threshold.
Controls: `HSCC_IDLE_TIMEOUT_MINUTES` env var (default 30).
Manual run: `python3 ~/.hermes/plugins/install/hscc-plugins/hscc-agent-coordinator/hscc.py idle-monitor [--dry-run]`

### Polling Mode (default)
5 timer streams: DGX every 5s, gateway every 10s, local services every 30s, heartbeat every 60s, NAS every 30s. Writes state to `~/.hscc/state/*.json`.

### Event-Driven Mode (kqueue + launchd)
- **kqueue** watches `~/.hscc/*.json` and `~/.hscc/state/*.json` — fires callbacks immediately on file changes instead of waiting for next poll
- **launchd** handles fixed-interval checks via periodic `.plist` jobs for DGX/gateway status
- **EventBridge** routes state changes to downstream reactions (notifications, pipeline checks)
- **Graceful fallback** — if kqueue unavailable (non-macOS), drops to polling mode

Key classes in `event_driven.py`:
- `KqueueWatcher` — thread-safe directory watcher with `start()`, `stop()`, `add_watch()`
- `LaunchdJobGenerator` — creates/installs/uninstalls launchd `.plist` files, `is_installed()`, `status()`
- `EventBridge` — event routing with `register()` and `fire()`
- `FallbackPoller` — polling mode when kqueue fails

See `references/event-driven-architecture.md` for full details.

## Agent Coordinator Integration

When assigning a task via `hscc-agent-coordinator assign-task`, the agent is **automatically provisioned** with a model container:
1. Queries `sparkrun status` to check if the recipe's container is running
2. If running → extracts host IP, wires agent endpoint
3. If not running → provisions on first idle host
4. If no idle hosts → reports error
5. Updates `agents.json` with correct `model`, `endpoint`, `status`

Recipe matching: agent's `model` field → defaults to `qwen3.6-35b-a3b-fp8-vllm`.

## Agent Data Model

Agent definitions come from `~/.hscc/agents.json` (21 agents):
- `dev-001` through `dev-020` — role: `developer`, name: `Builder`
- `merge-001` — role: `reviewer`, name: `Merger`

Each agent has: id, name, role, model, endpoint, systemPrompt, tools, mcpServers, skills, maxTokens, temperature, status, enabled.

Task assignments are cross-referenced from `~/.hscc/projects.json` via `assignedAgent` field.

## Lifecycle FSM

```
idle → spawning, ready, running, finished, failed, disabled
spawning → ready, running, failed, idle, disabled
ready → ready, running, failed, idle, disabled
running → finished, failed, idle, disabled
finished → idle, spawning, running, disabled
failed → idle, spawning, disabled
disabled → idle
```

## Event System

Events live in `~/.hscc/events.jsonl` (JSONL, most recent first when read). Structure:

```json
{"id":"uuid","event_type":"agent.task_dispatched","timestamp":"2026-05-27T09:10:56.223Z","severity":"info","source":"hermes-agents","payload":{...}}
```

Event types: `agent.task_dispatched`, `agent.task_completed`, `agent.task_reassigned`, `agent.sessions_cleaned`, `agent.configured`, `permission.task_cleared`, `permission.denied`.

**Filtering pitfall**: When filtering events by prefix in Python, use substring matching (`event_type not in typ`) rather than just `.startswith()` because `agent.task` should match `agent.task_completed`, `agent.task_dispatched`, `agent.task_reassigned`.

## Permissions Model

Tool categories (all use `hscc_` prefix):
- **Orchestrator-only (Tier 4 🔴)**: recovery, merge, snapshot, notify, triggers, reset — 16 tools
- **Self-only (Tier 3 🟠)**: agent_transition — can only target own ID
- **Task-scoped (Tier 2 🟡)**: worktree operations — require active task assignment
- **Unrestricted (Tier 1 🟢)**: read-only queries, health checks — 25 tools
- **Default deny**: unknown tools are blocked

## Governance Plugin Details

The governance plugin (`hscc-governance`) enforces three layers:
1. **RBAC tier check** — blocks orchestrator-only tools from non-orchestrator agents
2. **Policy evaluation** — evaluates `~/.hscc/policy.json` rules with operators (eq, ne, in, not_in, gt, lt)
3. **Audit recording** — every tool invocation logged to `~/.hscc/audit.jsonl` (append-only, never deleted)

Commands: `policy-eval`, `check-permission`, `record-audit`, `list-audit`, `enforce`, `classify-tool`, `update-policy`, `governance-status`.

## Skills & Templates

The skills plugin (`hscc-skills`) auto-installs 7 bundled skills + 4 templates from HSCC source:
- **Skills**: brainstorming, caveman, executing-plans, systematic-debugging, test-driven-development, verification-before-completion, writing-plans
- **Templates**: AGENTS.md, HEARTBEAT.md, SOUL.md, TOOLS.md
- **Deleted**: IDENTITY.md (flavor text), USER.md (empty placeholders) — too much dead weight

Uses MD5 hash comparison for idempotency — skips if destination matches source.

Commands: `install`, `install-skills`, `install-templates`, `status`, `uninstall`.

## Bootstrap Command

The bootstrap script (`hscc-bootstrap/bootstrap.sh`) runs the full initialization:
1. Skill install (hscc-skills) — with `--skip-skills` flag
2. State validation — verify all `~/.hscc/` state files exist
3. Gateway health check (localhost:18789) — with `--skip-gateway` flag
4. Cluster health check (sparkrun status) — with `--skip-cluster` flag

Flags: `--skip-skills`, `--skip-gateway`, `--skip-cluster`, `--verbose`, `--json`

## Launchd Service (hscc-daemon)

The monitoring daemon can run as a macOS launchd service:
- Plist at `~/Library/LaunchAgents/com.hermes.hscc-daemon.plist`
- Auto-start on login, log to `~/Library/Logs/hscc-daemon.log`
- Auto-restart on crash (`KeepAlive: SuccessfulExit: false`)
- Graceful SIGTERM handling via daemon's built-in stop command
- Helper script: `~/.hermes/plugins/hscc-daemon/launchd-setup.sh`

## Tips

- Use `fleet` for a quick overview, `agents` for full details
- Use `available` before routing to find idle agents
- `hscc-orchestrator` reads agent config from `~/.hscc/agents.json` — never copy state, always read source of truth
- When loading state, always handle FileNotFoundError gracefully
- Use the umbrella `hscc-*` skills for per-component details
- The `adhd` skill is the recommended approach for architecture design: diverge → focus → converge → spec → build
- **Install templates live in `install/hscc-plugins/`** — after cleaning main plugins, always sync them back to avoid drift

## Quick Commands (Telegram/Discord Integration)

Hermes `quick_commands` in `~/.hermes/config.yaml` lets you register slash commands that run shell commands directly — no AI in between. Use `type: exec` for terminal commands.

### Basic Pattern

```yaml
quick_commands:
  hscc_cluster_status:
    type: exec
    command: hscc cluster cluster-status
```

### Telegram-Friendly Formatting

When output goes to Telegram, Discord, or other messaging platforms, the raw output must use Telegram-native markdown (`*bold*`, `_italic_`, `• bullets`, `═══`). Strip ANSI escape codes and avoid terminal table borders.

Example inline Python script for clean Telegram output:

```yaml
quick_commands:
  hscc_cluster_status:
    type: exec
    command: |
      python3 << 'PYEOF'
import subprocess, json, re

def clean(s):
    s = re.sub(r'\x1b\[[0-9;]*m', '', s)
    s = s.replace('\r', '').replace('\n\n', '\n')
    return ' '.join(s.split())

print("🧬 *HSCC Cluster Status*\n")

# Workloads
print("_WORKLOADS_")
r = subprocess.run(['sparkrun','status'], capture_output=True, text=True)
if r.returncode == 0:
    for line in r.stdout.strip().split('\n'):
        c = clean(line)
        if c: print(f"  • {c}")
else:
    print("  • No workloads")

print()
print("_SYSTEM METRICS_")
# ... parse JSON and print with **bold**, • bullets, etc.
PYEOF
```

Key formatting rules for messaging platforms:
- `*bold*` → Telegram bold
- `_italic_` → Telegram italic (must be on own line, not inline)
- `• ` or `• ` → bullet points
- `═══` or `───` → section separators (avoid `=====` which renders as HTML underline)
- Strip all ANSI codes (`\x1b[...m`)
- No pipe tables (Telegram has no table syntax)

## Diagnostics

Key things to check when auditing HSCC health (syntax, daemon, state files, agent lifecycle, cluster status):
- **Daemon node resolution**: `hscc-daemon/hscc.py` resolves `PRIMARY_NODE`/`NAS_HOST` from `cluster.json` → `sparkrun cluster list --json` → hardcoded defaults (gateway `.244`, NAS `.249`). `PRIMARY_NODE` is the **gateway** (it runs the orchestrator vLLM), not a worker. `_rebuild_vllm_cmds()` must run after any `PRIMARY_NODE` change so the vLLM health/stop/start commands track it.
- **Empty state files**: `cluster.json`, `config.yaml`, `config.json`, `models.json` should all be populated. If empty, create from cluster.json template (see `references/state-files-reference.md`).
- **Stale agents**: agents with `state: idle` and `since: 2026-05-22` are dormant. Clean via `hermes-lifecycle.json` and `hermes-agents.json` (see `references/stale-agent-cleanup.md`).

## References

- `references/monitoring-daemon-design.md` — ADHD design pool for monitoring daemon: health checks, circuit breakers, graded alerting, self-protection
- `references/agents-data-model.md` — Agent data model from hscc-orchestrator
- `references/event-driven-architecture.md` — kqueue + launchd event system
- `references/permissions-reference.md` — RBAC tier reference
- `references/event-types.md` — Event type catalog
- `references/integration-assign-task-provision.md` — assign-task ↔ provision wiring, sparkrun parsing pitfalls, subprocess ~ expansion
- `references/state-files-reference.md` — HSCC state file formats and expected contents (cluster.json, config.yaml, config.json, models.json)
- `references/stale-agent-cleanup.md` — Procedure for removing stale idle agents from lifecycle and agents JSON
- `references/multi-node-provisioning-troubleshooting.md` — Multi-node vLLM provisioning failure modes, diagnosis steps, and May 28 2026 session record
- `references/idle-monitor-integration.md` — How idle monitor was integrated into daemon from standalone cron job (May 2026), pitfall about updating 3 files when adding streams
