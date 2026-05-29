---
name: hscc-events
description: Manages event logging, agent lifecycle, recovery, notifications, triggers, and policy
tags: [hsc, cluster, events, lifecycle, recovery, notifications, triggers, policy]
---

# HSCC Events, Lifecycle & Recovery

Manages event logging, agent lifecycle state, recovery history, notifications, trigger rules, and policy enforcement.

## When to use

- User wants to review the event log or search for specific events
- User asks about agent lifecycle state or recovery attempts
- User wants to check or create notifications
- User needs to view or manage trigger rules and policy rules
- User asks about permission categories for tools

## Commands

```bash
python3 ~/.hermes/plugins/hscc-events/hscc.py <command> [args]
```

### `events <type>`
List recent events (last 50). Optionally filter by event type prefix:

```bash
python3 ~/.hermes/plugins/hscc-events/hscc.py events
python3 ~/.hermes/plugins/hscc-events/hscc.py events agent.task
```

### `event-count`
Show total event count by type.

### `lifecycle`
Show current lifecycle state for all agents:

```bash
python3 ~/.hermes/plugins/hscc-events/hscc.py lifecycle
```

### `lifecycle-show <agent_id>`
Show lifecycle details for a specific agent:

```bash
python3 ~/.hermes/plugins/hscc-events/hscc.py lifecycle-show dev-001
```

### `recovery`
Show recovery history (most recent 20 attempts with outcome counts).

### `recovery-detail [id]`
Show detailed recovery attempt. Without ID, shows most recent.

### `notifications`
Show unread notifications sorted by priority (critical → low).

### `notify <priority> <title> <body>`
Create a manual notification. Priority: `critical`, `high`, `normal`, `low`.

```bash
python3 ~/.hermes/plugins/hscc-events/hscc.py notify critical "Cluster overloaded" "GPU usage above 90%"
```

### `notify-read <id>`
Mark a notification as read.

### `notify-clear`
Clear all read notifications.

### `rules`
List trigger rules with cooldown status.

### `rule-add <id> <type> <metric> <op> <value> [cooldown_seconds]`
Add a trigger rule. Types: `notify`, `tool_call`, `emit_event`.
Operators: `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `between`.

```bash
python3 ~/.hermes/plugins/hscc-events/hscc.py rule-add high-usage notify total_active_agents gt 3 300
```

### `rule-remove <id>`
Remove a trigger rule by ID.

### `rule-reset-cooldown <id>`
Reset cooldown on a specific rule so it can fire again.

### `policy`
List deny policy rules.

### `policy-add <id> <type> <metric> <op> <value>`
Add a deny policy rule. Prevents actions when conditions are met.

### `policy-remove <id>`
Remove a policy rule.

### `perms`
Show permission tool categories (orchestrator-only, self-only, task-scoped, unrestricted).

### `clear-recovery`
Clear all recovery history.

### `clear-notifications`
Clear all notifications.

### `compact [days]`
Compact old events, keeping only the last N days (default: 7).

## Lifecycle FSM

Valid state transitions:

```
idle → spawning, ready, running, finished, failed, disabled
spawning → ready, running, failed, idle, disabled
ready → ready, running, failed, idle, disabled
running → finished, failed, idle, disabled
finished → idle, spawning, running, disabled
failed → idle, spawning, disabled
disabled → idle
```

## Data files

| File | Content |
|---|---|
| `~/.hscc/events.jsonl` | Event log (JSONL format) — appended by HSCC plugins (e.g. agent-coordinator) and read/compacted here |
| `~/.hscc/lifecycle.json` | Agent lifecycle state |
| `~/.hscc/recovery.json` | Recovery attempt history |
| `~/.hscc/notifications.json` | Notifications |
| `~/.hscc/triggers.json` | Trigger rules |
| `~/.hscc/cooldowns.json` | Rule cooldown timestamps |
| `~/.hscc/policy.json` | Deny policy rules |

## Tips

- Use `events agent.task` to filter to task-related events only
- Recovery attempts track `attempt` count (1, 2, 3...) and `outcome` (recovered, exhausted, manual)
- Trigger rules use the same condition operators as policy rules (eq, neq, gt, gte, lt, lte, between)
- Policy rules with `action: "deny"` block actions; rules with `action: "trigger"` emit actions
- The `perms` command shows which tool categories exist — useful for debugging permission issues

## Source

All logic is implemented in Python in `~/.hermes/plugins/hscc-events/hscc.py`:

| Logic | Where |
|---|---|
| Event emit/read/compact | `hscc-events/hscc.py` |
| Lifecycle FSM transitions | `hscc-agent-coordinator/hscc.py` (`VALID_TRANSITIONS`) |
| Trigger rule evaluation & cooldowns | `hscc-events/hscc.py` |
| Permission categories (tool classification) | `hscc-permissions/hscc.py` |
| Policy evaluation | `hscc-policy/hscc.py` |
| Advisory file locking | `hscc-events/hscc.py` |

## Cross-references

- **hscc-cluster** — cluster status feeds trigger metrics (`active_worktree_count`)
- **hscc-orchestrator** — agent status/lifecycle tracked here; `route` sets working state
- **hscc-projects** — task IDs in event payloads; assignments via `assignedAgent` field
