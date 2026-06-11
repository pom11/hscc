# HSCC Event Types

> **STALE (historical).** The `agent.*` event types below were emitted by the old HSCC agent pipeline (`hscc-events` + `hscc-agent-coordinator`), both archived 2026-06-08. Agent dispatch is now native Hermes kanban, which does not emit these. The daemon still appends operational events to `~/.hscc/events.jsonl`, but the `agent.task_*`/`permission.*` schema here no longer applies. Kept for history only.

Events are stored in `~/.hscc/events.jsonl` as one JSON object per line.

## Source

Was implemented in the archived `hscc-events/hscc.py` (emit/read/compact) and `hscc-agent-coordinator/hscc.py` (lifecycle events).

## Event Structure

```json
{
  "id": "uuid",
  "event_type": "agent.task_dispatched",
  "timestamp": "2026-05-27T09:10:56.223Z",
  "severity": "info",
  "source": "hscc-agent-coordinator",
  "payload": {...}
}
```

## Valid Event Types

### `agent.task_dispatched`
Emitted when a task is dispatched to an agent.

Payload:
```json
{
  "agent_id": "dev-001",
  "task_id": "3E0329B4-89E6-478A-A41A-B88F83FC51A8",
  "session_id": "task-3E0329B4-89E6-478A-A41A-B88F83FC51A8",
  "success": true,
  "worktree_path": "~/.hscc/worktrees/<project>/dev-001-<task_id>"
}
```

### `agent.task_completed`
Emitted when a task finishes (success or failure).

Payload:
```json
{
  "agent_id": "dev-001",
  "task_id": "57C381A7-C50F-44EE-91BE-A91A5BF9157D",
  "exit_code": 1
}
```

### `agent.task_reassigned`
Emitted when a task is moved from one agent to another.

Payload:
```json
{
  "task_id": "57C381A7-C50F-44EE-91BE-A91A5BF9157D",
  "from_agent_id": "dev-001",
  "to_agent_id": "dev-002"
}
```

### `agent.sessions_cleaned`
Emitted when an agent's session state is cleaned up.

Payload:
```json
{
  "agent_id": "dev-001"
}
```

### `agent.configured`
Emitted when an agent's configuration is changed.

Payload:
```json
{
  "agent_id": "dev-001",
  "field": "temperature",
  "value": 0.5
}
```

### `permission.task_cleared`
Emitted when an agent's task assignment is cleared.

Payload:
```json
{
  "agent_id": "dev-001"
}
```

### `permission.denied`
Emitted when a tool call is denied by permissions.

Payload:
```json
{
  "agent_id": "dev-001",
  "tool_name": "hscc_merge_worktree",
  "reason": "orchestrator-only tool"
}
```

## Severity Levels

- `info` — normal operational events
- `warning` — things that might need attention
- `error` — actual failures

## Filtering Events

**Pitfall**: When filtering by prefix in Python, use substring matching rather than just `.startswith()`:

```python
# WRONG: only matches exact "agent.task" (no such type)
typ.startswith("agent.task")  # → False for all

# CORRECT: matches agent.task_dispatched, agent.task_completed, agent.task_reassigned
"agent.task" in typ  # → True for all three
```

This is because the actual types use `.` as separators: `agent.task_dispatched` not `agent.task.dispatched`.

## Event Writing

Events are appended atomically using `fs.openSync` with `O_WRONLY | O_CREAT | O_APPEND`. This ensures that concurrent writes don't corrupt the file — POSIX guarantees atomicity for writes <= PIPE_BUF (typically 4096 bytes), and our JSON lines are well under that.
