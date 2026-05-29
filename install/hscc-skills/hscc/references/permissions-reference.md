# HSCC Permission Categories

All tool names use the `hscc_` prefix. Unknown tools are **denied by default**.

## Source

Implemented in `~/.hermes/plugins/hscc-permissions/hscc.py`.

## Permission Categories

### Orchestrator-Only
Only the orchestrator (admin) can call these. Agents cannot.

| Tool | Purpose |
|---|---|
| `hscc_merge_worktree` | Merge worktree changes into main branch |
| `hscc_remove_worktree` | Clean up a worktree |
| `hscc_attempt_recovery` | Trigger recovery for a failed agent |
| `hscc_detect_stale_worktrees` | Find abandoned worktrees |
| `hscc_register_task_assignment` | Register which task an agent is working on |
| `hscc_clear_task_assignment` | Clear an agent's current task |
| `hscc_notify` | Send notifications to channels |
| `hscc_create_snapshot` | Create a state snapshot |
| `hscc_rotate_events` | Rotate/clean event log |
| `hscc_reset` | Reset system state |
| `hscc_stop_model` | Stop a running model instance |
| `hscc_evaluate_triggers` | Evaluate trigger rules against current state |
| `hscc_list_trigger_rules` | List available trigger rules |

### Self-Only
Agent can only target its own `agent_id`. The orchestrator can target any agent.

| Tool | Purpose |
|---|---|
| `hscc_agent_transition` | Transition an agent's lifecycle state (idle→running→finished/etc.) |

### Task-Scoped
Agent must have an active task assignment AND the `task_id` parameter must match.

| Tool | Purpose |
|---|---|
| `hscc_create_worktree` | Create a worktree for the assigned task |
| `hscc_worktree_status` | Check status of a worktree |
| `hscc_green_check` | Verify task output (syntax, tests, etc.) |
| `hscc_check_collisions` | Check for file conflicts with other agents |

### Unrestricted (Read-Only)
Any agent can call these. No permissions check needed.

| Tool | Purpose |
|---|---|
| `hscc_check_permission` | Check if a tool call would be allowed |
| `hscc_agent_status` | Get status of a specific agent |
| `hscc_agent_status_all` | Get status of all agents |
| `hscc_agent_history` | Get an agent's session history |
| `hscc_list_worktrees` | List all worktrees |
| `hscc_diagnose_failure` | Diagnose why an agent failed |
| `hscc_recovery_history` | Get recovery attempt history |
| `hscc_emit_event` | Emit a new event |
| `hscc_event_history` | Query the event log |
| `hscc_event_count` | Count events (optionally filtered) |
| `hscc_cluster_health` | Check cluster health |
| `hscc_gpu_status` | Check GPU availability |
| `hscc_vllm_health` | Check vLLM model server health |
| `hscc_list_recipes` | List deployment recipes |
| `hscc_evaluate_policy` | Evaluate policy rules |
| `hscc_list_policies` | List policy rules |
| `hscc_read_snapshot` | Read a previous snapshot |
| `hscc_build_context` | Build context for an agent |

## Permission Enforcement Logic

The core `checkPermission(agentId, toolName, toolParams)` function follows this decision tree:

```
1. Is it a bootstrap tool? → Allowed only if no orchestrator set, or caller IS orchestrator
2. Is it orchestrator-only? → Allowed only if caller IS orchestrator
3. Is it self-only? → Allowed if caller targets own agent_id (or is orchestrator)
4. Is it task-scoped? → Allowed if caller has active task assignment AND task_id matches
5. Is it unrestricted? → Always allowed
6. Default → DENIED (unknown tool)
```

Each denial emits a `permission.denied` event with reason.