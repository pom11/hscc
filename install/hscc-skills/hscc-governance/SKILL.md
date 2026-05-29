# HSCC Governance Plugin

## When to Use

Use the governance plugin (`hscc-governance`) to:
- **Gate destructive operations** — prevent agents from stopping/restarting cluster, deleting projects, scaling down
- **Enforce RBAC** — ensure only authorised agents can use specific tools
- **Audit all actions** — record every tool invocation with full context (append-only, never deletable)
- **Circuit breaker** — automatically suspend agents that trigger excessive policy violations
- **Classify new tools** — add new tools to the RBAC tier system before deployment

## Quick Reference

```
# Check if an agent can use a tool
hscc-governance check-permission <agent_id> <tool_name>

# Full enforcement gate (RBAC + policy + circuit breaker + audit)
hscc-governance enforce <agent_id> <tool_name> [args_json] [context_json]

# List all RBAC tiers
hscc-governance list-tiers

# View audit log
hscc-governance list-audit [agent_id] [limit] [tool_name]

# Check circuit breaker state
hscc-governance circuit-breaker-status [agent_id]

# Show full governance status
hscc-governance governance-status

# Create default destructive-action policies
hscc-governance init-defaults

# Evaluate a specific policy rule
hscc-governance policy-eval <action> [agent_id] [context_json]
```

## RBAC Tier System

5 tiers of access control, from most restrictive to least:

| Tier | Level | Role Required | Description | Example Tools |
|---|---|---|---|---|
| 🔴 Orchestrator-only | 4 | `orchestrator` | Cluster management, destructive ops | `hscc_stop_model`, `hscc_reset`, `hscc_merge_worktree` |
| 🟣 Reviewer | 3.5 | `reviewer` | Review and approve operations | `hscc_cluster_health`, `hscc_event_history`, `hscc_read_snapshot` |
| 🟠 Self-only | 3 | Any (self-target) | Agent lifecycle on own ID | `hscc_agent_transition`, `hscc_emit_event` |
| 🟡 Task-scoped | 2 | Any + active task | Operations tied to an assigned task | `hscc_create_worktree`, `hscc_worktree_build` |
| 🟢 Unrestricted | 1 | None (any agent) | Read-only queries, health checks | `hscc_agent_status`, `hscc_list_worktrees` |
| ⚫ Unclassified | -1 | None | **Default DENY** — must be explicitly classified | Any new tool |

**Default DENY for unclassified tools** — any tool not explicitly added to a tier is blocked. This prevents accidental privilege escalation from new tools.

## Policy Engine

### Compound Conditions

Policy rules support AND/OR/NOT composition:

```json
{
  "op": "AND",
  "conditions": [
    {"metric": "action", "op": "eq", "value": "stop_agent"},
    {"op": "OR", "conditions": [
      {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
      {"metric": "cluster_state", "op": "eq", "value": "healthy"}
    ]}
  ]
}
```

### Supported Operators

- **eq / ne** — exact match / not equal
- **in / not_in** — membership in list
- **gt / lt / gte / lte** — numeric comparison
- **starts_with / ends_with / contains** — string matching
- **regex** — Python regex matching
- **exists / not_exists** — check if field is populated
- **between** — check if value is in range `[low, high]`

### Condition Metrics

Available metric names for conditions:
- `action` — the requested action (e.g., "stop_agent", "delete_project")
- `agent_id` — the calling agent's ID
- `agent_role` — the calling agent's role
- `tool_name` — the tool being invoked
- `event_type` — event type from context
- `cluster_state` — cluster health state
- `task_id` — current task assignment
- `rbac_tier` — the tool's RBAC tier
- `deny_count` — agent's denial count (for circuit breaker rules)

### Priority-Based Evaluation

Rules with higher `priority` numbers are evaluated first. The first matching rule determines the outcome (early-exit). Default priority is 0.

## Audit Log

### Features
- **Append-only** — entries are never modified or deleted, only appended
- **Full context** — every entry captures agent, tool, args, result, policy decision, RBAC tier, circuit breaker state
- **Query with filters** — filter by agent, tool, time range, decision, tier
- **Rotation** — auto-archive old entries to prevent unbounded growth
- **Export** — export to file for external analysis

### Audit Log Queries

```bash
# All entries, last 20 (default)
hscc-governance list-audit

# Filtered by agent
hscc-governance list-audit dev-001 50

# Filtered by tool
hscc-governance list-audit "" 50 hscc_stop_model

# Filtered by time range
hscc-governance list-audit "" 100 "" "2026-05-01T00:00:00" "2026-05-28T00:00:00"

# Filtered by decision
hscc-governance list-audit "" 50 "" "" "" deny

# Filtered by tier
hscc-governance list-audit "" 50 "" "" "" "" "orchestrator-only"
```

### Audit Log Rotation

```bash
# Rotate: keep last 100,000 entries, archive the rest
hscc-governance audit-rotate 100000

# Export entire log
hscc-governance audit-export /tmp/audit-export.jsonl
```

## Circuit Breaker

### Automatic Suspension

When an agent exceeds the denial threshold (default: 10 denials), the circuit breaker automatically suspends them:

- **Default threshold**: 10 denials
- **Suspension duration**: 24 hours
- **Auto-release**: Suspensions expire automatically; no manual reset needed
- **Denial tracking**: Keeps last 50 denial timestamps per agent

### Checking Status

```bash
# Global status (all agents)
hscc-governance circuit-breaker-status

# Specific agent
hscc-governance circuit-breaker-status dev-001
```

### Manual Reset

```bash
hscc-governance circuit-breaker-reset dev-001
```

## Default Policies

Run `hscc-governance init-defaults` to create these security rules:

| Rule | Description |
|---|---|
| `deny-stop-agent` | Blocks agent stop/restart unless orchestrator |
| `deny-delete-project` | Blocks project deletion unless orchestrator |
| `deny-scale-down` | Blocks cluster scale-down unless orchestrator |
| `deny-reset-cluster` | Blocks cluster reset unless orchestrator |
| `deny-force-transition` | Blocks forced lifecycle transitions unless orchestrator |
| `deny-delete-worktree` | Blocks worktree deletion unless orchestrator |
| `deny-remove-node` | Blocks node removal unless orchestrator |

## Integration with Agent Coordinator

The governance plugin works with `hscc-agent-coordinator` to enforce RBAC on all agent lifecycle operations. When an agent coordinator tool is invoked:

1. **Circuit breaker check** — is the agent suspended?
2. **RBAC check** — does the agent have the right tier?
3. **Policy evaluation** — do any policy rules match?
4. **Audit recording** — log the decision
5. **Circuit breaker update** — record denial if blocked

## Troubleshooting

### "Tool is unclassified — BLOCKED"
A tool is being invoked but hasn't been added to any RBAC tier. Fix:
```bash
hscc-governance classify-tool-add <tool_name> <tier_name>
```

### "Agent has no active task assignment"
Task-scoped tools require the agent to have an `inProgress` task in `~/.hscc/projects.json`. Assign a task first via the agent coordinator.

### "Circuit breaker suspended"
The agent has exceeded the denial threshold. Wait for suspension to expire or reset:
```bash
hscc-governance circuit-breaker-reset <agent_id>
```

### "Restricted action — default deny"
The action is in the RESTRICTED_ACTIONS list and no policy rule allows it. Add an explicit allow rule:
```bash
hscc-governance policy-eval-add allow-dev-stop-agent allow "Allow dev-001 to stop agents" \
  '{"op":"AND","conditions":[{"metric":"action","op":"eq","value":"stop_agent"},{"metric":"agent_id","op":"eq","value":"dev-001"}]}' \
  '["dev-001"]'
```
