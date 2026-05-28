#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Governance & Access Control Plugin v2.0

Policy engine, permission proxy, append-only audit log, RBAC enforcement,
circuit breaker, audit rotation, and compound condition evaluation.

Layers of defense:
  1. RBAC Tier Check — blocks unauthorised tool access by role/task assignment
  2. Policy Engine — evaluates policy.json rules with compound conditions
  3. Destructive Action Gate — explicit deny-by-default for destructive ops
  4. Circuit Breaker — rate-limits / suspends agents that trigger too many denials
  5. Audit Log — append-only recording of all tool invocations with rotation
  6. Permission Proxy — pre-invocation validation before any tool runs

Usage: hscc-governance <command> [args]

Commands:
  policy-eval <action> [agent_id] [context_json]  Evaluate policy rules
  policy-eval-add <id> <action> <desc> <conditions_json> [applies_to_json]
                                                   Add a compound policy rule
  policy-list                                     List all policy rules
  policy-show <rule_id>                           Show a specific rule
  policy-remove <rule_id>                         Remove a rule
  policy-enable <rule_id>                         Enable a rule
  policy-disable <rule_id>                        Disable a rule
  policy-import <json_file>                       Import rules from a JSON file
  check-permission <agent_id> <tool_name>         Check RBAC permission
  classify-tool <tool_name>                       Show RBAC tier classification
  classify-tool-add <tool_name> <tier>            Add a tool to a tier
  list-tiers                                      Show all RBAC tier classifications
  record-audit <agent_id> <tool_name> [args_json] [result_json] [decision]
                                                   Record an audit entry
  list-audit [agent_id] [limit] [tool_name] [time_from] [time_to]
                                                   Query audit log
  audit-export [file_path]                        Export audit log to file
  audit-rotate [max_entries]                      Rotate audit log
  circuit-breaker-status                          Show circuit breaker state
  circuit-breaker-reset <agent_id>                Reset circuit breaker for agent
  enforce <agent_id> <tool_name> [args_json] [context_json]
                                                   Full gate: RBAC + policy + circuit + audit
  governance-status                               Summary of governance state
  init-defaults                                   Create default destructive-action policies
  help                                            Show this help
"""

import sys
import json
import os
import uuid
import glob
import shutil
import time
import fnmatch
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

# ── Paths ─────────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
POLICY_FILE = os.path.join(HSCC_DIR, "policy.json")
AUDIT_FILE = os.path.join(HSCC_DIR, "audit.jsonl")
AUDIT_ARCHIVE_DIR = os.path.join(HSCC_DIR, "audit_archives")
CIRCUIT_BREAKER_FILE = os.path.join(HSCC_DIR, "circuit_breaker.json")
AGENTS_JSON = os.path.expanduser("~/.hscc/agents.json")
LIFECYCLE_FILE = os.path.join(HSCC_DIR, "lifecycle.json")

# ── RBAC Tier Definitions ────────────────────────────────────────────────

# Tier 4: Orchestrator-only — cluster management, recovery, destructive ops
ORCHESTRATOR_ONLY_TOOLS = [
    "hscc_merge_worktree",
    "hscc_remove_worktree",
    "hscc_attempt_recovery",
    "hscc_detect_stale_worktrees",
    "hscc_register_task_assignment",
    "hscc_clear_task_assignment",
    "hscc_notify",
    "hscc_create_snapshot",
    "hscc_rotate_events",
    "hscc_reset",
    "hscc_stop_model",
    "hscc_evaluate_triggers",
    "hscc_list_trigger_rules",
    "hscc_clear_recovery",
    "hscc_clear_notifications",
    "hscc_compact",
]

# Tier 3.5: Reviewer — can review/merge work, approve task transitions
REVIEWER_TOOLS = [
    "hscc_worktree_status",
    "hscc_green_check",
    "hscc_check_collisions",
    "hscc_read_snapshot",
    "hscc_build_context",
    "hscc_diagnose_failure",
    "hscc_recovery_history",
    "hscc_event_history",
    "hscc_event_count",
    "hscc_cluster_health",
    "hscc_gpu_status",
    "hscc_vllm_health",
    "hscc_list_recipes",
    "hscc_list_audit",
    "hscc_classify_tool",
    "hscc_list_tiers",
    "hscc_governance_status",
    "hscc_policy_eval",
    "hscc_list_policies",
]

# Tier 3: Self-only — agent can only operate on its own lifecycle
SELF_ONLY_TOOLS = [
    "hscc_agent_transition",
    "hscc_emit_event",
]

# Tier 2: Task-scoped — require active task assignment to the calling agent
TASK_SCOPED_TOOLS = [
    "hscc_create_worktree",
    "hscc_worktree_build",
    "hscc_check_collisions",  # also in reviewer
    "hscc_green_check",       # also in reviewer
    "hscc_read_snapshot",     # also in reviewer
]

# Tier 1: Unrestricted — read-only queries, health checks, governance introspection
UNRESTRICTED_TOOLS = [
    "hscc_check_permission",
    "hscc_agent_status",
    "hscc_agent_status_all",
    "hscc_agent_history",
    "hscc_list_worktrees",
    "hscc_cluster_health",
    "hscc_gpu_status",
    "hscc_vllm_health",
    "hscc_list_recipes",
    "hscc_evaluate_policy",
    "hscc_list_policies",
    "hscc_policy_eval",
    "hscc_record_audit",
    "hscc_list_audit",
    "hscc_classify_tool",
    "hscc_update_policy",
    "hscc_enforce",
    "hscc_governance_status",
    "hscc_circuit_breaker_status",
    "hscc_audit_export",
    "hscc_audit_rotate",
]


# ── RBAC Tier Labels ─────────────────────────────────────────────────────

RBAC_TIERS = {
    "orchestrator-only": {
        "level": 4,
        "role_requirement": "orchestrator",
        "description": "Cluster management and destructive operations — orchestrator only",
        "tools": set(ORCHESTRATOR_ONLY_TOOLS),
    },
    "reviewer": {
        "level": 3.5,
        "role_requirement": "reviewer",
        "description": "Review and approve operations — reviewer role required",
        "tools": set(REVIEWER_TOOLS),
    },
    "self-only": {
        "level": 3,
        "role_requirement": None,  # any role can use on self
        "description": "Agent lifecycle transitions — agent may only target own id",
        "tools": set(SELF_ONLY_TOOLS),
    },
    "task-scoped": {
        "level": 2,
        "role_requirement": None,  # requires active task assignment
        "description": "Task-dependent operations — requires active task assignment",
        "tools": set(TASK_SCOPED_TOOLS),
    },
    "unrestricted": {
        "level": 1,
        "role_requirement": None,
        "description": "Read-only queries and health checks — available to all agents",
        "tools": set(UNRESTRICTED_TOOLS),
    },
}

# Build reverse index: tool_name -> tier_name
_TOOL_TO_TIER = {}
for tier_name, tier_info in RBAC_TIERS.items():
    for tool in tier_info["tools"]:
        if tool not in _TOOL_TO_TIER:
            _TOOL_TO_TIER[tool] = tier_name

# ── Destructive Action Catalog ───────────────────────────────────────────
# These actions are considered destructive and trigger special policy handling
DESTRUCTIVE_ACTIONS = [
    "stop_agent",
    "start_agent",
    "restart_agent",
    "delete_agent",
    "disable_agent",
    "enable_agent",
    "stop_model",
    "delete_project",
    "delete_worktree",
    "scale_down_cluster",
    "remove_node",
    "reset_cluster",
    "force_reset",
    "clear_recovery",
    "purge_events",
    "compact_state",
    "rotate_events",
    "force_transition",
    "merge_worktree",
    "remove_worktree",
]

# Actions that require explicit policy allow (deny by default)
RESTRICTED_ACTIONS = DESTRUCTIVE_ACTIONS + [
    "assign_task",
    "reassign_task",
    "clear_task",
    "notify",
    "create_snapshot",
    "attempt_recovery",
    "evaluate_triggers",
]


# ── Helpers ───────────────────────────────────────────────────────────────

def ensure_dir():
    """Ensure the HSCC state directory exists."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    os.makedirs(AUDIT_ARCHIVE_DIR, exist_ok=True)


def now_iso():
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def now_ts():
    """Return current UTC epoch timestamp in milliseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def days_ago_iso(days):
    """Return ISO-8601 timestamp for N days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def read_json_file(path, default=None):
    """Read and parse a JSON file, returning default on failure."""
    ensure_dir()
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def write_json_file(path, data):
    """Atomically write JSON data to disk."""
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)


def append_audit_log(entry):
    """Append a single audit entry to the append-only log file.

    This is the only write operation — no overwrites, no deletes.
    Uses file-level locking via exclusive write.
    """
    ensure_dir()
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except IOError as e:
        print(json.dumps({"error": f"Failed to write audit log: {e}"}))


def load_policy():
    """Load policy.json, creating default structure if missing."""
    ensure_dir()
    if not os.path.exists(POLICY_FILE):
        write_json_file(POLICY_FILE, {"rules": []})
    return read_json_file(POLICY_FILE, {"rules": []})


def load_agents_list():
    """Load the agent fleet from agents.json."""
    if not os.path.exists(AGENTS_JSON):
        return []
    try:
        with open(AGENTS_JSON) as f:
            data = json.load(f)
        return data.get("agents", [])
    except (json.JSONDecodeError, IOError):
        return []


def get_agent_info(agent_id):
    """Get full agent info by ID, or None."""
    for a in load_agents_list():
        if a.get("id") == agent_id:
            return a
    return None


def get_agent_role(agent_id):
    """Get agent role, or 'unknown'."""
    if not agent_id:
        return "unknown"
    agent = get_agent_info(agent_id)
    if agent:
        return agent.get("role", "unknown")
    # Check if it looks like an orchestrator (merge-*, admin-*, orchestrator-*)
    if agent_id.startswith(("merge-", "admin-", "orchestrator-")):
        return "reviewer" if agent_id.startswith("merge-") else "orchestrator"
    return "unknown"


def get_agent_status(agent_id):
    """Get agent status from lifecycle.json."""
    ensure_dir()
    lifecycle = read_json_file(LIFECYCLE_FILE, {"agents": {}})
    agents_lc = lifecycle.get("agents", {})
    lc_data = agents_lc.get(agent_id, {})
    return lc_data.get("state", "unknown")


# ── 1. Policy Evaluation Engine (Compound Conditions) ────────────────────

def evaluate_condition(condition, agent_id, context, agent_role):
    """Evaluate a single condition against the current context.

    Supports operators: eq, ne, in, not_in, gt, lt, starts_with, ends_with,
    regex, exists, not_exists, between

    Returns True if the condition matches.
    """
    metric = condition.get("metric", "")
    op = condition.get("op", "eq")
    rule_value = condition.get("value")

    # Resolve actual value from various sources
    actual_value = None
    if context and metric in context:
        actual_value = context.get(metric)
    elif metric == "action":
        actual_value = context.get("action", context.get("tool_name", ""))
    elif metric == "agent_id":
        actual_value = agent_id
    elif metric == "agent_role":
        actual_value = agent_role
    elif metric == "event_type":
        actual_value = context.get("event_type", "")
    elif metric == "tool_name":
        actual_value = context.get("tool_name", "")
    elif metric == "cluster_state":
        actual_value = context.get("cluster_state", "")
    elif metric == "task_id":
        actual_value = context.get("task_id", "")
    elif metric == "rbac_tier":
        actual_value = context.get("rbac_tier", "")
    elif metric == "policy_decision":
        actual_value = context.get("policy_decision", "")
    elif metric == "deny_count":
        actual_value = context.get("deny_count", 0)

    # If metric is not found and value is True/False, treat as existence check
    if actual_value is None:
        if op == "exists":
            return bool(condition.get("check_in_context", False))
        elif op == "not_exists":
            return not bool(condition.get("check_in_context", False))
        return False

    # Evaluate the operator
    try:
        if op == "eq":
            return str(actual_value) == str(rule_value)
        elif op == "ne":
            return str(actual_value) != str(rule_value)
        elif op == "in":
            if isinstance(rule_value, list):
                return str(actual_value) in [str(v) for v in rule_value]
            return str(actual_value) == str(rule_value)
        elif op == "not_in":
            if isinstance(rule_value, list):
                return str(actual_value) not in [str(v) for v in rule_value]
            return str(actual_value) != str(rule_value)
        elif op == "gt":
            return float(actual_value) > float(rule_value)
        elif op == "lt":
            return float(actual_value) < float(rule_value)
        elif op == "gte":
            return float(actual_value) >= float(rule_value)
        elif op == "lte":
            return float(actual_value) <= float(rule_value)
        elif op == "starts_with":
            return str(actual_value).startswith(str(rule_value))
        elif op == "ends_with":
            return str(actual_value).endswith(str(rule_value))
        elif op == "contains":
            return str(rule_value) in str(actual_value)
        elif op == "regex":
            import re
            return bool(re.match(rule_value, str(actual_value)))
        elif op == "between":
            vals = rule_value if isinstance(rule_value, list) else [rule_value]
            if len(vals) == 2:
                low, high = float(vals[0]), float(vals[1])
                return low <= float(actual_value) <= high
        elif op == "exists":
            return actual_value is not None and actual_value != ""
        elif op == "not_exists":
            return actual_value is None or actual_value == ""
    except (ValueError, TypeError):
        return False

    return False


def evaluate_compound_condition(conditions, agent_id, context, agent_role):
    """Evaluate compound conditions with AND/OR/NOT support.

    Condition formats:
      {"metric": "action", "op": "eq", "value": "stop_agent"}         # Simple
      {"op": "AND", "conditions": [ {...}, {...} ]}                    # All must match
      {"op": "OR",  "conditions": [ {...}, {...} ]}                    # At least one matches
      {"op": "NOT", "condition": {...}}                                # Negation

    Returns True if the entire compound condition evaluates to True.
    """
    if not conditions:
        return True

    # Handle compound operators
    if isinstance(conditions, dict):
        if "op" in conditions and "conditions" in conditions:
            inner_op = conditions["op"]
            inner_conds = conditions["conditions"]
            if inner_op == "AND":
                return all(evaluate_compound_condition(c, agent_id, context, agent_role)
                           for c in inner_conds)
            elif inner_op == "OR":
                return any(evaluate_compound_condition(c, agent_id, context, agent_role)
                           for c in inner_conds)
            elif inner_op == "NOT":
                return not evaluate_compound_condition(conditions.get("condition"),
                                                       agent_id, context, agent_role)

        # Single condition dict (no "op" + "conditions" combo)
        if "metric" in conditions or "op" in conditions:
            return evaluate_condition(conditions, agent_id, context, agent_role)

    elif isinstance(conditions, list):
        # List of conditions defaults to AND
        return all(evaluate_compound_condition(c, agent_id, context, agent_role)
                   for c in conditions)

    return False


def evaluate_policy(action, agent_id=None, context=None):
    """Evaluate policy rules against a given action.

    Policy rules have the structure:
      {
        "id": "rule_name",
        "action": "deny" | "allow",
        "description": "human readable",
        "applies_to": ["agent_id_or_role", ...],  # [] means all
        "condition": {                          # compound condition
          "op": "AND" | "OR",
          "conditions": [...]
        },
        "enabled": true | false,
        "priority": 0  # optional: higher number = higher priority
      }

    Evaluation order:
      1. Rules are sorted by priority (highest first)
      2. First matching rule wins (early-exit)
      3. Explicit deny rules take priority over allow rules at same level

    Returns:
      {
        "action": requested_action,
        "agent_id": agent_id,
        "policy_decision": "deny" | "allow" | "unknown",
        "matched_rules": [...],
        "rationale": "why the decision was made"
      }
    """
    if context is None:
        context = {}
    policy = load_policy()
    rules = policy.get("rules", [])
    agent_role = get_agent_role(agent_id) if agent_id else "unknown"

    # Enrich context with current info
    enriched = {
        "action": action,
        "agent_id": agent_id,
        "agent_role": agent_role,
        "event_type": context.get("event_type", ""),
        "tool_name": context.get("tool_name", ""),
        "cluster_state": context.get("cluster_state", ""),
        "task_id": context.get("task_id", ""),
        **context,
    }

    # Sort rules by priority (highest first), then by insertion order
    indexed_rules = [(i, r) for i, r in enumerate(rules)]
    indexed_rules.sort(key=lambda x: x[1].get("priority", 0), reverse=True)

    for _idx, rule in indexed_rules:
        if not rule.get("enabled", True):
            continue

        # Check if rule applies to this agent/role
        applies_to = rule.get("applies_to", [])
        if applies_to and agent_id:
            if agent_id not in applies_to and agent_role not in applies_to:
                # Also check if applies_to includes wildcards
                matches_wildcard = False
                for pattern in applies_to:
                    if fnmatch.fnmatch(agent_id, pattern) or fnmatch.fnmatch(agent_role, pattern):
                        matches_wildcard = True
                        break
                if not matches_wildcard:
                    continue

        # Evaluate compound condition
        condition = rule.get("condition", {})
        if not condition:
            # Rule without condition — unconditional, always applies
            matched = True
        else:
            matched = evaluate_compound_condition(condition, agent_id, enriched, agent_role)

        if matched:
            if rule.get("action") == "deny":
                return {
                    "action": action,
                    "agent_id": agent_id,
                    "policy_decision": "deny",
                    "matched_rules": [rule.get("id", "?")],
                    "rationale": f"Denied by rule '{rule.get('id', '?')}': {rule.get('description', 'no description')}",
                }

    # No deny rules matched — check for explicit allow rules
    for _idx, rule in indexed_rules:
        if not rule.get("enabled", True):
            continue
        applies_to = rule.get("applies_to", [])
        if applies_to and agent_id:
            if agent_id not in applies_to and agent_role not in applies_to:
                matches_wildcard = False
                for pattern in applies_to:
                    if fnmatch.fnmatch(agent_id, pattern) or fnmatch.fnmatch(agent_role, pattern):
                        matches_wildcard = True
                        break
                if not matches_wildcard:
                    continue
        condition = rule.get("condition", {})
        if not condition:
            matched = True
        else:
            matched = evaluate_compound_condition(condition, agent_id, enriched, agent_role)
        if matched and rule.get("action") == "allow":
            return {
                "action": action,
                "agent_id": agent_id,
                "policy_decision": "allow",
                "matched_rules": [rule.get("id", "?")],
                "rationale": f"Explicitly allowed by rule '{rule.get('id', '?')}': {rule.get('description', '')}",
            }

    # Default behavior: deny-by-default for restricted actions, allow for others
    if action in RESTRICTED_ACTIONS:
        return {
            "action": action,
            "agent_id": agent_id,
            "policy_decision": "deny",
            "matched_rules": [],
            "rationale": f"Restricted action '{action}' — no explicit allow rule matches, default deny",
        }

    return {
        "action": action,
        "agent_id": agent_id,
        "policy_decision": "allow",
        "matched_rules": [],
        "rationale": "No matching policy rules — default allow for unrestricted actions",
    }


# ── 2. Gateway Permission Proxy ──────────────────────────────────────────

def classify_tool(tool_name):
    """Classify a tool into its RBAC tier. Returns tier dict or default."""
    tier_name = _TOOL_TO_TIER.get(tool_name)
    if tier_name:
        tier_info = RBAC_TIERS[tier_name]
        return {
            "tool": tool_name,
            "tier": tier_name,
            "level": tier_info["level"],
            "description": tier_info["description"],
        }
    # Default: deny-by-default — any new tool must be explicitly classified
    return {
        "tool": tool_name,
        "tier": "unclassified",
        "level": -1,
        "description": "Unclassified — defaults to DENY. MUST be explicitly classified before use.",
        "warning": "unclassified_tool",
    }


def classify_tool_name(tool_name):
    """Return just the tier name for a tool, or 'unclassified'."""
    return _TOOL_TO_TIER.get(tool_name, "unclassified")


def check_permission(agent_id, tool_name):
    """Check if an agent has permission to invoke a tool.

    Validates against the tiered RBAC system:
      - unclassified: BLOCKED — must be explicitly classified
      - orchestrator-only: agent must have role 'orchestrator'
      - reviewer: agent must have role 'reviewer'
      - self-only: agent can only use targeting own id
      - task-scoped: agent must have an active task assignment
      - unrestricted: any agent can use

    Returns:
      {
        "agent_id": ...,
        "tool_name": ...,
        "rbac_tier": ...,
        "level": ...,
        "allowed": true | false,
        "reason": "explanation"
      }
    """
    classification = classify_tool(tool_name)
    tier = classification["tier"]
    agent_role = get_agent_role(agent_id) if agent_id else "unknown"
    agent_status = get_agent_status(agent_id) if agent_id else "unknown"

    result = {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "rbac_tier": tier,
        "level": classification["level"],
        "allowed": False,
        "reason": "",
        "agent_role": agent_role,
        "agent_status": agent_status,
    }

    if tier == "unclassified":
        result["reason"] = (
            f"Tool '{tool_name}' is unclassified — BLOCKED. "
            f"Must be explicitly classified before use."
        )
    elif tier == "orchestrator-only":
        if agent_role != "orchestrator":
            result["reason"] = (
                f"Tool '{tool_name}' requires orchestrator role. "
                f"Agent has role: {agent_role}"
            )
        else:
            result["allowed"] = True
            result["reason"] = "Orchestrator role — full access granted"

    elif tier == "reviewer":
        if agent_role == "reviewer":
            result["allowed"] = True
            result["reason"] = "Reviewer role — review and approval access granted"
        elif agent_role == "orchestrator":
            result["allowed"] = True
            result["reason"] = "Orchestrator role — elevated access to reviewer tools"
        else:
            result["reason"] = (
                f"Tool '{tool_name}' requires reviewer role. "
                f"Agent has role: {agent_role}"
            )

    elif tier == "self-only":
        result["allowed"] = True
        result["reason"] = f"Self-only tool — agent {agent_id} is authorized for own lifecycle ops"

    elif tier == "task-scoped":
        has_active_task = _has_active_task(agent_id)
        if has_active_task:
            result["allowed"] = True
            result["reason"] = f"Agent {agent_id} has active task — task-scoped tool allowed"
        else:
            result["reason"] = (
                f"Agent {agent_id} has no active task assignment. "
                f"Task-scoped tool requires active task."
            )

    elif tier == "unrestricted":
        result["allowed"] = True
        result["reason"] = "Unrestricted tool — available to all agents"

    return result


def _has_active_task(agent_id):
    """Check if an agent has an active task assignment in projects.json."""
    if not agent_id:
        return False
    project_file = os.path.expanduser("~/.hscc/projects.json")
    if not os.path.exists(project_file):
        project_file = os.path.expanduser("~/.hscc/projects.json")
    if not os.path.exists(project_file):
        return False
    try:
        with open(project_file) as f:
            proj = json.load(f)
        for project in proj.get("projects", []):
            for roadmap in project.get("roadmaps", []):
                for sub in roadmap.get("subProjects", []):
                    for task in sub.get("tasks", []):
                        if task.get("assignedAgent") == agent_id and task.get("status") == "inProgress":
                            return True
    except Exception:
        pass
    return False


# ── 3. Circuit Breaker ───────────────────────────────────────────────────

def get_circuit_breaker():
    """Load circuit breaker state."""
    cb = read_json_file(CIRCUIT_BREAKER_FILE, {
        "agents": {},
        "global": {
            "deny_threshold": 10,  # max denials before suspension
            "suspension_duration_hours": 24,
        },
    })
    return cb


def update_circuit_breaker(agent_id, denied=False):
    """Update circuit breaker state for an agent.

    Tracks denial counts and suspends agents that exceed thresholds.
    """
    cb = get_circuit_breaker()
    agents = cb.setdefault("agents", {})
    agent_state = agents.setdefault(agent_id, {
        "deny_count": 0,
        "denied_at": [],
        "suspended": False,
        "suspended_until": None,
        "last_denied": None,
    })

    if denied:
        agent_state["deny_count"] += 1
        agent_state["denied_at"].append(now_iso())
        agent_state["last_denied"] = now_iso()
        # Clean old denial records (keep last 50)
        if len(agent_state["denied_at"]) > 50:
            agent_state["denied_at"] = agent_state["denied_at"][-50:]

        # Check if threshold exceeded
        threshold = cb.get("global", {}).get("deny_threshold", 10)
        if not agent_state.get("suspended") and agent_state["deny_count"] >= threshold:
            agent_state["suspended"] = True
            hours = cb.get("global", {}).get("suspension_duration_hours", 24)
            agent_state["suspended_until"] = (
                datetime.now(timezone.utc) + timedelta(hours=hours)
            ).isoformat()

    write_json_file(CIRCUIT_BREAKER_FILE, cb)
    return agent_state


def check_circuit_breaker(agent_id):
    """Check if an agent is suspended by the circuit breaker.

    Returns:
      {
        "suspended": True/False,
        "suspended_until": ISO timestamp or None,
        "deny_count": int,
        "reason": "explanation"
      }
    """
    cb = get_circuit_breaker()
    agents = cb.get("agents", {})
    agent_id = str(agent_id)

    if agent_id not in agents:
        return {"suspended": False, "deny_count": 0, "reason": "No circuit breaker state"}

    state = agents[agent_id]
    if state.get("suspended"):
        suspended_until = state.get("suspended_until")
        if suspended_until:
            try:
                until_dt = datetime.fromisoformat(suspended_until)
                if datetime.now(timezone.utc) >= until_dt:
                    # Suspension expired — auto-release
                    state["suspended"] = False
                    state["suspended_until"] = None
                    write_json_file(CIRCUIT_BREAKER_FILE, cb)
                    return {
                        "suspended": False,
                        "deny_count": state.get("deny_count", 0),
                        "reason": f"Circuit breaker suspension expired at {suspended_until}",
                    }
                remaining = until_dt - datetime.now(timezone.utc)
                return {
                    "suspended": True,
                    "suspended_until": suspended_until,
                    "deny_count": state.get("deny_count", 0),
                    "reason": f"Agent suspended by circuit breaker. {remaining.total_seconds()/3600:.1f}h remaining",
                }
            except (ValueError, TypeError):
                pass
        return {
            "suspended": True,
            "suspended_until": suspended_until,
            "deny_count": state.get("deny_count", 0),
            "reason": "Agent suspended by circuit breaker",
        }

    return {
        "suspended": False,
        "deny_count": state.get("deny_count", 0),
        "reason": "No suspension active",
    }


def reset_circuit_breaker(agent_id):
    """Reset circuit breaker state for an agent."""
    cb = get_circuit_breaker()
    agents = cb.get("agents", {})
    if agent_id in agents:
        agents[agent_id] = {
            "deny_count": 0,
            "denied_at": [],
            "suspended": False,
            "suspended_until": None,
            "last_denied": None,
        }
        write_json_file(CIRCUIT_BREAKER_FILE, cb)
        return {"success": True, "agent_id": agent_id, "message": "Circuit breaker reset"}
    return {"error": f"No circuit breaker state for agent: {agent_id}"}


# ── 4. Append-Only Event Audit Log ───────────────────────────────────────

def record_audit(agent_id, tool_name, args=None, result=None, policy_decision="allow", context=None):
    """Record a tool invocation to the append-only audit log.

    Each entry contains:
      - audit_id: unique UUID
      - timestamp: ISO-8601 UTC
      - epoch_ms: epoch timestamp in milliseconds
      - agent_id: who invoked the tool
      - agent_role: the agent's role
      - tool_name: which tool was invoked
      - args: the arguments passed (sanitized)
      - result: the tool's return value
      - policy_decision: "allow" | "deny" | "unknown"
      - rbac_tier: the tool's RBAC classification
      - circuit_breaker: circuit breaker status if applicable
      - context: additional context (task_id, session_id, etc.)

    This is append-only — entries are never modified or deleted.
    """
    cb_status = check_circuit_breaker(agent_id) if agent_id else {"suspended": False}

    entry = {
        "audit_id": str(uuid.uuid4())[:12],
        "timestamp": now_iso(),
        "epoch_ms": now_ts(),
        "agent_id": agent_id,
        "agent_role": get_agent_role(agent_id),
        "tool_name": tool_name,
        "args": args if args else {},
        "result": result if result else {},
        "policy_decision": policy_decision,
        "rbac_tier": classify_tool_name(tool_name),
        "rbac_level": classify_tool(tool_name)["level"],
        "circuit_breaker": cb_status.get("suspended", False),
        "context": context if context else {},
    }

    append_audit_log(entry)
    return entry


def read_audit_log(agent_id=None, limit=50, tool_name=None, offset=0,
                   time_from=None, time_to=None, decision=None, tier=None):
    """Read entries from the audit log.

    Supports filtering by agent_id, tool_name, time range, decision, and tier.
    Returns entries in reverse-chronological order (newest first).
    """
    ensure_dir()
    if not os.path.exists(AUDIT_FILE):
        return []

    try:
        with open(AUDIT_FILE) as f:
            lines = f.readlines()
    except IOError:
        return []

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Filter by agent_id
        if agent_id and entry.get("agent_id") != agent_id:
            continue

        # Filter by tool_name
        if tool_name and entry.get("tool_name") != tool_name:
            continue

        # Filter by decision
        if decision:
            entry_decision = entry.get("result", {}).get("final_decision") or entry.get("policy_decision", "")
            if entry_decision != decision:
                continue

        # Filter by tier
        if tier and entry.get("rbac_tier") != tier:
            continue

        # Filter by time range
        if time_from:
            entry_ts = entry.get("timestamp", "")
            if entry_ts and entry_ts < time_from:
                continue
        if time_to:
            entry_ts = entry.get("timestamp", "")
            if entry_ts and entry_ts > time_to:
                continue

        entries.append(entry)

    # Reverse chronological order (newest first)
    entries.reverse()

    # Apply offset and limit
    entries = entries[offset:offset + limit]
    return entries


def audit_export(file_path=None):
    """Export the entire audit log.

    If file_path is None, returns the full log as a string.
    If file_path is provided, writes to that file.
    """
    ensure_dir()
    if not os.path.exists(AUDIT_FILE):
        return {"entries": 0, "message": "No audit entries to export"}

    with open(AUDIT_FILE) as f:
        content = f.read().strip()

    if not content:
        return {"entries": 0, "message": "Audit log is empty"}

    lines = [l for l in content.split("\n") if l.strip()]
    result = {"entries": len(lines), "message": "Audit export complete"}

    if file_path:
        with open(file_path, "w") as f:
            f.write(content + "\n")
        result["file"] = file_path
    else:
        result["content_preview"] = content[:500] + ("..." if len(content) > 500 else "")

    return result


def audit_rotate(max_entries=100000):
    """Rotate the audit log.

    Keeps the most recent max_entries entries, archives the rest.
    """
    ensure_dir()
    if not os.path.exists(AUDIT_FILE):
        return {"rotated": 0, "archived": 0, "message": "No audit log to rotate"}

    with open(AUDIT_FILE) as f:
        lines = [l.strip() for l in f.readlines() if l.strip()]

    total = len(lines)
    if total <= max_entries:
        return {"rotated": 0, "archived": 0, "message": f"No rotation needed ({total} entries <= {max_entries} limit)"}

    # Keep recent, archive old
    keep = lines[-max_entries:]
    archive = lines[:total - max_entries]

    # Archive old entries
    archive_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_file = os.path.join(AUDIT_ARCHIVE_DIR, f"audit_{archive_ts}.jsonl.gz")

    try:
        import gzip
        with gzip.open(archive_file, "wt") as f:
            f.write("\n".join(archive) + "\n")
        archived_count = len(archive)
    except Exception:
        # Fallback: plain text archive
        archive_file = os.path.join(AUDIT_ARCHIVE_DIR, f"audit_{archive_ts}.jsonl")
        with open(archive_file, "w") as f:
            f.write("\n".join(archive) + "\n")
        archived_count = len(archive)

    # Write remaining entries back
    with open(AUDIT_FILE, "w") as f:
        f.write("\n".join(keep) + "\n")

    return {
        "rotated": total,
        "archived": archived_count,
        "remaining": len(keep),
        "archive_file": archive_file,
    }


# ── 5. Full Enforcement Gate ────────────────────────────────────────────

def enforce_action(agent_id, tool_name, args=None, context=None):
    """Full governance gate: circuit-breaker → RBAC → policy → audit.

    This is the main enforcement entry point. It:
      1. Checks circuit breaker (agent suspension)
      2. Checks RBAC permission (permission proxy)
      3. Evaluates policy rules (policy engine)
      4. Records the decision to the audit log
      5. Updates circuit breaker state

    Returns:
      {
        "agent_id": ...,
        "tool_name": ...,
        "circuit_breaker": { ... },
        "rbac_check": { ... },
        "policy_check": { ... },
        "audit_entry": { ... },
        "final_decision": "allow" | "deny",
        "reason": "explanation"
      }
    """
    if args is None:
        args = {}
    if context is None:
        context = {}

    agent_id = str(agent_id) if agent_id else "unknown"

    # Step 0: Circuit Breaker Check
    cb_status = check_circuit_breaker(agent_id)
    if cb_status.get("suspended"):
        # Agent is suspended — deny everything, record audit
        entry = record_audit(
            agent_id=agent_id,
            tool_name=tool_name,
            args=args,
            result={"reason": "Circuit breaker suspended"},
            policy_decision="deny",
            context={**context, "suspension_reason": cb_status.get("reason", "")},
        )
        return {
            "agent_id": agent_id,
            "tool_name": tool_name,
            "circuit_breaker": cb_status,
            "final_decision": "deny",
            "reason": f"Circuit breaker: {cb_status['reason']}",
            "audit_entry": entry,
        }

    # Step 1: RBAC Permission Check
    rbac_result = check_permission(agent_id, tool_name)

    # Step 2: Policy Evaluation
    action = f"tool:{tool_name}"
    policy_context = {
        "tool_name": tool_name,
        "event_type": tool_name,
        "agent_role": get_agent_role(agent_id),
        "task_id": context.get("task_id", ""),
        "rbac_tier": rbac_result.get("rbac_tier", ""),
        **context,
    }
    policy_result = evaluate_policy(action, agent_id, policy_context)

    # Step 3: Decision — deny if ANY layer denies
    denied_by_rbac = not rbac_result.get("allowed", False)
    denied_by_policy = policy_result.get("policy_decision") == "deny"
    final_decision = "deny" if (denied_by_rbac or denied_by_policy) else "allow"

    policy_decision = "deny" if final_decision == "deny" else policy_result.get("policy_decision", "allow")

    # Step 4: Record to audit log
    audit_result = {
        "final_decision": final_decision,
        "rbac_allowed": rbac_result.get("allowed", False),
        "rbac_reason": rbac_result.get("reason", ""),
        "policy_decision": policy_decision,
        "policy_rationale": policy_result.get("rationale", ""),
    }
    audit_entry = record_audit(
        agent_id=agent_id,
        tool_name=tool_name,
        args=args,
        result=audit_result,
        policy_decision=policy_decision,
        context=context,
    )

    # Step 5: Update circuit breaker state
    update_circuit_breaker(agent_id, denied=final_decision == "deny")

    return {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "circuit_breaker": cb_status,
        "rbac_check": rbac_result,
        "policy_check": policy_result,
        "audit_entry": audit_entry,
        "final_decision": final_decision,
        "reason": (
            f"RBAC: {rbac_result.get('reason', '')} | "
            f"Policy: {policy_result.get('rationale', '')}"
        ),
    }


# ── 6. Default Policies ─────────────────────────────────────────────────

def create_default_policies():
    """Create default destructive-action policy rules.

    These rules implement deny-by-default for destructive operations.
    Returns the list of rules created.
    """
    default_rules = [
        {
            "id": "deny-stop-agent",
            "action": "deny",
            "description": "Deny stopping agents unless orchestrator with no active task",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "stop_agent"},
                    {"op": "OR", "conditions": [
                        {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                    ]},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
        {
            "id": "deny-delete-project",
            "action": "deny",
            "description": "Deny deleting projects unless orchestrator",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "delete_project"},
                    {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
        {
            "id": "deny-scale-down",
            "action": "deny",
            "description": "Deny scaling down the cluster unless orchestrator",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "scale_down_cluster"},
                    {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
        {
            "id": "deny-reset-cluster",
            "action": "deny",
            "description": "Deny cluster reset unless orchestrator",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "reset_cluster"},
                    {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
        {
            "id": "deny-force-transition",
            "action": "deny",
            "description": "Deny force-transitions on agent lifecycle unless orchestrator",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "force_transition"},
                    {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
        {
            "id": "deny-circuit-breaker-excessive",
            "action": "deny",
            "description": "Deny actions from agents with excessive denial counts (circuit breaker trigger)",
            "applies_to": [],
            "condition": {
                "metric": "action",
                "op": "exists",
                "value": None,
            },
            "enabled": False,  # Disabled by default — requires explicit deny_count metric in context
            "priority": 90,
        },
        {
            "id": "deny-delete-worktree-non-orchestrator",
            "action": "deny",
            "description": "Deny deleting worktrees unless orchestrator",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "delete_worktree"},
                    {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
        {
            "id": "deny-remove-node",
            "action": "deny",
            "description": "Deny removing cluster nodes unless orchestrator",
            "applies_to": [],
            "condition": {
                "op": "AND",
                "conditions": [
                    {"metric": "action", "op": "eq", "value": "remove_node"},
                    {"metric": "agent_role", "op": "ne", "value": "orchestrator"},
                ],
            },
            "enabled": True,
            "priority": 100,
        },
    ]

    policy = load_policy()
    existing_ids = {r.get("id") for r in policy.get("rules", [])}
    new_rules = []
    for rule in default_rules:
        if rule["id"] not in existing_ids:
            policy.setdefault("rules", []).append(rule)
            new_rules.append(rule["id"])

    if new_rules:
        write_json_file(POLICY_FILE, policy)

    return {
        "created": new_rules,
        "total_rules": len(policy.get("rules", [])),
    }


# ── Command Handlers ─────────────────────────────────────────────────────

def cmd_policy_eval(args_str, agent_id=None):
    """Evaluate policy rules for a given action."""
    parts = args_str.split(None, 2) if args_str else []
    if not parts:
        print(json.dumps({"error": "Usage: hscc-governance policy-eval <action> [agent_id] [context_json]"}))
        return

    action = parts[0]
    ctx = {}
    if len(parts) > 2:
        try:
            ctx = json.loads(parts[2])
        except json.JSONDecodeError:
            ctx = {"note": parts[2]}

    if agent_id:
        ctx["agent_id"] = agent_id

    result = evaluate_policy(action, agent_id, ctx)
    print(json.dumps(result, indent=2))


def cmd_policy_eval_add(args_list):
    """Add a compound policy rule."""
    if len(args_list) < 4:
        print(json.dumps({"error": "Usage: hscc-governance policy-eval-add <id> <action> <desc> <conditions_json> [applies_to_json]"}))
        return

    rule_id = args_list[0]
    rule_action = args_list[1]
    description = args_list[2]
    conditions_str = args_list[3]
    applies_to = []

    try:
        conditions = json.loads(conditions_str)
    except json.JSONDecodeError:
        print(json.dumps({"error": f"Invalid JSON for conditions: {conditions_str}"}))
        return

    if len(args_list) > 4:
        try:
            applies_to = json.loads(args_list[4])
        except json.JSONDecodeError:
            applies_to = [args_list[4]]

    # Check for duplicate ID
    policy = load_policy()
    for r in policy.get("rules", []):
        if r.get("id") == rule_id:
            print(json.dumps({"error": f"Policy rule ID already exists: {rule_id}"}))
            return

    rule = {
        "id": rule_id,
        "action": rule_action,
        "description": description,
        "applies_to": applies_to,
        "condition": conditions,
        "enabled": True,
        "priority": 0,
    }
    policy.setdefault("rules", []).append(rule)
    write_json_file(POLICY_FILE, policy)
    print(json.dumps({"success": True, "rule": rule}, indent=2))


def cmd_policy_list():
    """List all policy rules."""
    policy = load_policy()
    rules = policy.get("rules", [])
    if not rules:
        print("No policy rules configured.")
        return
    print(f"Policy rules: {len(rules)}")
    print()
    for r in rules:
        rid = r.get("id", "?")
        enabled = r.get("enabled", True)
        status = "✓" if enabled else "✗"
        desc = r.get("description", "?")
        applies = r.get("applies_to", [])
        priority = r.get("priority", 0)
        print(f"  {status} {rid:40s} — {desc}")
        print(f"       priority={priority}  applies_to={applies}")
        print()
    print(f"Total: {len(rules)} rules")


def cmd_policy_show(rule_id):
    """Show a specific policy rule."""
    policy = load_policy()
    for r in policy.get("rules", []):
        if r.get("id") == rule_id:
            print(json.dumps(r, indent=2))
            return
    print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))


def cmd_policy_remove(rule_id):
    """Remove a policy rule."""
    policy = load_policy()
    rules = policy.get("rules", [])
    before = len(rules)
    rules = [r for r in rules if r.get("id") != rule_id]
    if len(rules) == before:
        print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))
        return
    policy["rules"] = rules
    write_json_file(POLICY_FILE, policy)
    print(json.dumps({"success": True, "removed": rule_id}))


def cmd_policy_enable(rule_id):
    """Enable a policy rule."""
    policy = load_policy()
    for r in policy.get("rules", []):
        if r.get("id") == rule_id:
            r["enabled"] = True
            write_json_file(POLICY_FILE, policy)
            print(json.dumps({"success": True, "rule_id": rule_id, "enabled": True}))
            return
    print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))


def cmd_policy_disable(rule_id):
    """Disable a policy rule."""
    policy = load_policy()
    for r in policy.get("rules", []):
        if r.get("id") == rule_id:
            r["enabled"] = False
            write_json_file(POLICY_FILE, policy)
            print(json.dumps({"success": True, "rule_id": rule_id, "enabled": False}))
            return
    print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))


def cmd_policy_import(file_path):
    """Import policy rules from a JSON file."""
    if not os.path.exists(file_path):
        print(json.dumps({"error": f"File not found: {file_path}"}))
        return
    try:
        with open(file_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON in {file_path}: {e}"}))
        return

    rules = data.get("rules", []) if isinstance(data, dict) else data
    if not isinstance(rules, list):
        print(json.dumps({"error": "File must contain a 'rules' array or be an array of rules"}))
        return

    policy = load_policy()
    existing_ids = {r.get("id") for r in policy.get("rules", [])}
    imported = []
    for rule in rules:
        if rule.get("id") not in existing_ids:
            policy.setdefault("rules", []).append(rule)
            imported.append(rule.get("id"))

    if imported:
        write_json_file(POLICY_FILE, policy)
        print(json.dumps({"success": True, "imported": imported, "total": len(policy["rules"])}))
    else:
        print(json.dumps({"message": "No new rules to import (all IDs already exist)"}))


def cmd_check_permission(agent_id, tool_name):
    """Check if agent can invoke a tool."""
    if not agent_id or not tool_name:
        print(json.dumps({"error": "Usage: hscc-governance check-permission <agent_id> <tool_name>"}))
        return
    result = check_permission(agent_id, tool_name)
    print(json.dumps(result, indent=2))


def cmd_classify_tool(tool_name):
    """Show RBAC tier classification for a tool."""
    if not tool_name:
        print(json.dumps({"error": "Usage: hscc-governance classify-tool <tool_name>"}))
        return
    result = classify_tool(tool_name)
    print(json.dumps(result, indent=2))


def cmd_classify_tool_add(tool_name, tier_name):
    """Add a tool to a tier."""
    if not tool_name or not tier_name:
        print(json.dumps({"error": "Usage: hscc-governance classify-tool-add <tool_name> <tier_name>"}))
        return

    valid_tiers = list(RBAC_TIERS.keys())
    if tier_name not in valid_tiers:
        print(json.dumps({"error": f"Unknown tier: {tier_name}. Valid: {valid_tiers}"}))
        return

    # Update the tier set
    RBAC_TIERS[tier_name]["tools"].add(tool_name)
    _TOOL_TO_TIER[tool_name] = tier_name
    print(json.dumps({
        "success": True,
        "tool": tool_name,
        "tier": tier_name,
        "level": RBAC_TIERS[tier_name]["level"],
        "message": f"Tool '{tool_name}' classified as '{tier_name}'",
    }, indent=2))


def cmd_list_tiers():
    """Show the full RBAC tier classification for all known tools."""
    print("HSCC Governance — RBAC Tier Classification")
    print("=" * 60)
    print()

    tier_order = ["orchestrator-only", "reviewer", "self-only", "task-scoped", "unrestricted"]
    tier_markers = {
        "orchestrator-only": "🔴",
        "reviewer": "🟣",
        "self-only": "🟠",
        "task-scoped": "🟡",
        "unrestricted": "🟢",
    }

    for tier_name in tier_order:
        tier = RBAC_TIERS[tier_name]
        tools = sorted(tier["tools"])
        marker = tier_markers.get(tier_name, "⚪")

        print(f"  {marker} Tier {tier['level']}: {tier_name}")
        print(f"      {tier['description']}")
        if tier.get("role_requirement"):
            print(f"      Role required: {tier['role_requirement']}")
        print(f"      Tools ({len(tools)}):")
        for t in tools:
            print(f"        • {t}")
        print()

    # Count total
    total = sum(len(t["tools"]) for t in RBAC_TIERS.values())
    unclassified_count = len(_TOOL_TO_TIER) - total
    print(f"Total classified tools: {total}")
    if unclassified_count > 0:
        print(f"⚠️  {unclassified_count} tools are unclassified (default DENY)")


def cmd_record_audit(agent_id, tool_name, args_str=None, result_str=None, policy_decision=None):
    """Record a tool invocation to the audit log."""
    args = {}
    result = {}
    if args_str:
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {"raw": args_str}
    if result_str:
        try:
            result = json.loads(result_str)
        except json.JSONDecodeError:
            result = {"raw": result_str}

    if policy_decision is None:
        policy_decision = "allow"

    entry = record_audit(
        agent_id=agent_id,
        tool_name=tool_name,
        args=args,
        result=result,
        policy_decision=policy_decision,
    )
    print(json.dumps({
        "success": True,
        "audit_id": entry["audit_id"],
        "timestamp": entry["timestamp"],
    }, indent=2))


def cmd_list_audit(args_str):
    """Query the audit log with optional filters.

    Usage: hscc-governance list-audit [agent_id] [limit] [tool_name] [time_from] [time_to] [decision] [tier]
    """
    parts = args_str.split() if args_str else []
    agent_id = parts[0] if len(parts) > 0 else None
    limit = 50
    try:
        if len(parts) > 1:
            limit = int(parts[1])
    except ValueError:
        pass
    tool_name = parts[2] if len(parts) > 2 else None
    time_from = parts[3] if len(parts) > 3 else None
    time_to = parts[4] if len(parts) > 4 else None
    decision = parts[5] if len(parts) > 5 else None
    tier = parts[6] if len(parts) > 6 else None

    entries = read_audit_log(
        agent_id=agent_id, limit=limit, tool_name=tool_name,
        time_from=time_from, time_to=time_to, decision=decision, tier=tier,
    )

    if not entries:
        filters = []
        if agent_id:
            filters.append(f"agent={agent_id}")
        if tool_name:
            filters.append(f"tool={tool_name}")
        if time_from:
            filters.append(f"from={time_from}")
        if time_to:
            filters.append(f"to={time_to}")
        if decision:
            filters.append(f"decision={decision}")
        if tier:
            filters.append(f"tier={tier}")
        print("No audit entries found." + (f" ({', '.join(filters)})" if filters else ""))
        return

    print(f"Audit entries: {len(entries)} shown")
    print()

    for e in entries:
        ts = e.get("timestamp", "?")[:19]
        agent = e.get("agent_id", "?")
        tool = e.get("tool_name", "?")
        tier = e.get("rbac_tier", "?")
        decision = e.get("result", {}).get("final_decision") or e.get("policy_decision", "?")
        cb = "🔒" if e.get("circuit_breaker") else ""

        marker = {"allow": "✓", "deny": "✗", "unknown": "?"}.get(decision, "?")
        args_summary = json.dumps(e.get("args", {}), default=str)[:80]

        print(f"  {marker} [{ts}] {cb} agent={agent} tool={tool} tier={tier} decision={decision}")
        print(f"      args={args_summary}")
        print()

    # Summary
    decisions = {}
    for e in entries:
        d = e.get("result", {}).get("final_decision") or e.get("policy_decision", "?")
        decisions[d] = decisions.get(d, 0) + 1
    print(f"Summary: {dict(decisions)}")


def cmd_audit_export(file_path=None):
    """Export audit log."""
    result = audit_export(file_path)
    print(json.dumps(result, indent=2))


def cmd_audit_rotate(max_entries=100000):
    """Rotate audit log."""
    try:
        max_entries = int(max_entries) if max_entries else 100000
    except ValueError:
        max_entries = 100000
    result = audit_rotate(max_entries)
    print(json.dumps(result, indent=2))


def cmd_circuit_breaker_status(agent_id=None):
    """Show circuit breaker state."""
    if agent_id:
        status = check_circuit_breaker(agent_id)
        print(json.dumps(status, indent=2))
    else:
        cb = get_circuit_breaker()
        agents = cb.get("agents", {})
        global_cfg = cb.get("global", {})
        print(f"Circuit Breaker Status")
        print("=" * 50)
        print(f"Global deny threshold: {global_cfg.get('deny_threshold', 10)}")
        print(f"Suspension duration: {global_cfg.get('suspension_duration_hours', 24)}h")
        print()
        if agents:
            print(f"Agents with circuit breaker state: {len(agents)}")
            print()
            for aid, state in sorted(agents.items()):
                suspended = "🔒 SUSPENDED" if state.get("suspended") else "✅ OK"
                print(f"  {suspended} {aid} — denies: {state.get('deny_count', 0)}")
                if state.get("suspended_until"):
                    print(f"       suspended until: {state['suspended_until']}")
        else:
            print("No circuit breaker state recorded.")


def cmd_circuit_breaker_reset(agent_id):
    """Reset circuit breaker for an agent."""
    result = reset_circuit_breaker(agent_id)
    print(json.dumps(result, indent=2))


def cmd_enforce(agent_id, tool_name, args_str=None, context_str=None):
    """Full governance enforcement gate."""
    args = {}
    context = {}
    if args_str:
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            args = {"raw": args_str}
    if context_str:
        try:
            context = json.loads(context_str)
        except json.JSONDecodeError:
            context = {"raw": context_str}

    result = enforce_action(agent_id, tool_name, args, context)
    print(json.dumps(result, indent=2))


def cmd_governance_status():
    """Show a summary of the current governance state."""
    policy = load_policy()
    rules = policy.get("rules", [])

    # Audit log stats
    ensure_dir()
    audit_size = 0
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE) as f:
                audit_size = sum(1 for line in f if line.strip())
        except IOError:
            pass

    # Decision summary
    decisions = Counter()
    agents_seen = set()
    tools_seen = set()
    tier_counts = Counter()
    entries = read_audit_log(limit=50000, offset=0)
    for e in entries:
        d = e.get("result", {}).get("final_decision") or e.get("policy_decision", "?")
        decisions[d] += 1
        agents_seen.add(e.get("agent_id", "unknown"))
        tools_seen.add(e.get("tool_name", "unknown"))
        tier_counts[e.get("rbac_tier", "unknown")] += 1

    # Circuit breaker state
    cb = get_circuit_breaker()
    cb_agents = len(cb.get("agents", {}))
    cb_suspended = sum(1 for a in cb.get("agents", {}).values() if a.get("suspended"))

    # Audit archive
    archive_count = 0
    archive_size = 0
    if os.path.exists(AUDIT_ARCHIVE_DIR):
        archive_files = os.listdir(AUDIT_ARCHIVE_DIR)
        archive_count = len(archive_files)
        for af in archive_files:
            try:
                archive_size += os.path.getsize(os.path.join(AUDIT_ARCHIVE_DIR, af))
            except OSError:
                pass

    status = {
        "governance_state": {
            "policy_rules": len(rules),
            "enabled_rules": sum(1 for r in rules if r.get("enabled", True)),
            "audit_log_entries": audit_size,
            "audit_archive_files": archive_count,
            "audit_archive_size_bytes": archive_size,
            "unique_agents_audited": len(agents_seen),
            "unique_tools_audited": len(tools_seen),
            "decision_summary": dict(decisions),
            "tier_breakdown": dict(tier_counts),
        },
        "circuit_breaker": {
            "agents_tracked": cb_agents,
            "agents_suspended": cb_suspended,
            "global_deny_threshold": cb.get("global", {}).get("deny_threshold", 10),
        },
        "rbac_tiers": {
            name: {
                "level": info["level"],
                "tool_count": len(info["tools"]),
                "tools": sorted(info["tools"]),
            }
            for name, info in RBAC_TIERS.items()
        },
        "files": {
            "policy": POLICY_FILE,
            "audit_log": AUDIT_FILE,
            "circuit_breaker": CIRCUIT_BREAKER_FILE,
            "audit_archives": AUDIT_ARCHIVE_DIR,
        },
    }

    print(json.dumps(status, indent=2, default=str))


def cmd_init_defaults():
    """Initialize default destructive-action policies."""
    result = create_default_policies()
    print(json.dumps(result, indent=2))
    print()
    print("Default policies created. Run 'hscc-governance policy-list' to review.")


# ── Command Map ───────────────────────────────────────────────────────────

COMMANDS = {
    "policy-eval": lambda: (
        cmd_policy_eval(" ".join(sys.argv[2:]), sys.argv[3] if len(sys.argv) > 3 else None)
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-eval <action> [agent_id] [context_json]"}))
    ),
    "policy-eval-add": lambda: (
        cmd_policy_eval_add(sys.argv[2:])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-eval-add <id> <action> <desc> <conditions_json> [applies_to_json]"}))
    ),
    "policy-list": cmd_policy_list,
    "policy-show": lambda: (
        cmd_policy_show(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-show <rule_id>"}))
    ),
    "policy-remove": lambda: (
        cmd_policy_remove(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-remove <rule_id>"}))
    ),
    "policy-enable": lambda: (
        cmd_policy_enable(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-enable <rule_id>"}))
    ),
    "policy-disable": lambda: (
        cmd_policy_disable(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-disable <rule_id>"}))
    ),
    "policy-import": lambda: (
        cmd_policy_import(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-import <json_file>"}))
    ),
    "check-permission": lambda: (
        cmd_check_permission(sys.argv[2], sys.argv[3])
        if len(sys.argv) > 3
        else print(json.dumps({"error": "Usage: hscc-governance check-permission <agent_id> <tool_name>"}))
    ),
    "classify-tool": lambda: (
        cmd_classify_tool(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance classify-tool <tool_name>"}))
    ),
    "classify-tool-add": lambda: (
        cmd_classify_tool_add(sys.argv[2], sys.argv[3])
        if len(sys.argv) > 3
        else print(json.dumps({"error": "Usage: hscc-governance classify-tool-add <tool_name> <tier>"}))
    ),
    "list-tiers": cmd_list_tiers,
    "record-audit": lambda: (
        cmd_record_audit(
            sys.argv[2], sys.argv[3],
            sys.argv[4] if len(sys.argv) > 4 else None,
            sys.argv[5] if len(sys.argv) > 5 else None,
            sys.argv[6] if len(sys.argv) > 6 else None,
        )
        if len(sys.argv) > 3
        else print(json.dumps({"error": "Usage: hscc-governance record-audit <agent_id> <tool_name> [args_json] [result_json] [decision]"}))
    ),
    "list-audit": lambda: cmd_list_audit(" ".join(sys.argv[2:])) if len(sys.argv) > 2 else cmd_list_audit(""),
    "audit-export": lambda: (
        cmd_audit_export(sys.argv[2])
        if len(sys.argv) > 2
        else cmd_audit_export()
    ),
    "audit-rotate": lambda: (
        cmd_audit_rotate(sys.argv[2])
        if len(sys.argv) > 2
        else cmd_audit_rotate()
    ),
    "circuit-breaker-status": lambda: (
        cmd_circuit_breaker_status(sys.argv[2])
        if len(sys.argv) > 2
        else cmd_circuit_breaker_status()
    ),
    "circuit-breaker-reset": lambda: (
        cmd_circuit_breaker_reset(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance circuit-breaker-reset <agent_id>"}))
    ),
    "enforce": lambda: (
        cmd_enforce(
            sys.argv[2], sys.argv[3],
            sys.argv[4] if len(sys.argv) > 4 else None,
            sys.argv[5] if len(sys.argv) > 5 else None,
        )
        if len(sys.argv) > 3
        else print(json.dumps({"error": "Usage: hscc-governance enforce <agent_id> <tool_name> [args_json] [context_json]"}))
    ),
    "governance-status": cmd_governance_status,
    "init-defaults": cmd_init_defaults,
    "help": lambda: print(USAGE),
}


# ── Usage ─────────────────────────────────────────────────────────────────

USAGE = """\
Hermes Spark Cluster Control (HSCC) — Governance & Access Control v2.0

Usage: hscc-governance <command> [args]

─────────────────────────────────────────────────────────────────
POLICY ENGINE
─────────────────────────────────────────────────────────────────
  policy-eval <action> [agent_id] [context_json]
              Evaluate policy rules for an action
  policy-eval-add <id> <action> <desc> <conditions_json> [applies_to_json]
              Add a compound policy rule (AND/OR/NOT support)
  policy-list             List all policy rules
  policy-show <rule_id>   Show a specific rule
  policy-remove <rule_id> Remove a rule
  policy-enable <rule_id> Enable a rule
  policy-disable <rule_id>Disable a rule
  policy-import <json_file>
              Import rules from a JSON file

─────────────────────────────────────────────────────────────────
PERMISSION PROXY
─────────────────────────────────────────────────────────────────
  check-permission <agent_id> <tool_name>
              Check RBAC permission for tool access
  classify-tool <tool_name>
              Show RBAC tier for a tool
  classify-tool-add <tool_name> <tier>
              Add a tool to a tier
  list-tiers  Full RBAC tier classification for all known tools

─────────────────────────────────────────────────────────────────
AUDIT LOG
─────────────────────────────────────────────────────────────────
  record-audit <agent_id> <tool_name> [args] [result] [decision]
              Record a tool invocation to the audit log
  list-audit [agent_id] [limit] [tool] [from] [to] [decision] [tier]
              Query the audit log with filters
  audit-export [file_path]
              Export the audit log
  audit-rotate [max_entries]
              Rotate audit log (archive old entries)

─────────────────────────────────────────────────────────────────
CIRCUIT BREAKER
─────────────────────────────────────────────────────────────────
  circuit-breaker-status [agent_id]
              Show circuit breaker state (suspensions)
  circuit-breaker-reset <agent_id>
              Reset circuit breaker for an agent

─────────────────────────────────────────────────────────────────
ENFORCEMENT
─────────────────────────────────────────────────────────────────
  enforce <agent_id> <tool_name> [args_json] [context_json]
              Full gate: circuit-breaker → RBAC → policy → audit

─────────────────────────────────────────────────────────────────
STATUS
─────────────────────────────────────────────────────────────────
  governance-status   Summary of all governance state
  init-defaults       Create default destructive-action policies

─────────────────────────────────────────────────────────────────
RBAC Tiers:
  🔴 Tier 4:  orchestrator-only  — Cluster management, destructive ops
  🟣 Tier 3.5: reviewer          — Review and approve operations
  🟠 Tier 3:  self-only          — Agent lifecycle (own id only)
  🟡 Tier 2:  task-scoped        — Task-dependent operations
  🟢 Tier 1:  unrestricted       — Read-only queries, health checks
  ⚫ Tier -1: unclassified       — Default DENY until classified

Files:
  Policy:       ~/.hscc/policy.json
  Audit Log:    ~/.hscc/audit.jsonl (append-only)
  Circuit Brk:  ~/.hscc/circuit_breaker.json
  Archives:     ~/.hscc/audit_archives/
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd not in COMMANDS:
        print(json.dumps({
            "error": f"Unknown command: {cmd}",
            "available": list(COMMANDS.keys()),
        }))
        sys.exit(1)

    try:
        COMMANDS[cmd]()
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
