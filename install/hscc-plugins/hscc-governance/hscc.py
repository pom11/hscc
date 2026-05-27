#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Governance & Access Control Plugin

Policy engine, permission proxy, append-only audit log, and RBAC enforcement.

This plugin provides the governance layer for the entire HSCC fleet:
  1. Policy Evaluation Engine — gates destructive operations behind policy.json rules
  2. Gateway Permission Proxy — intercepts tool invocations, validates agent permissions
  3. Append-Only Event Audit Log — records every tool invocation with full context
  4. RBAC Tool Classification — enforces 4-tier permission system across all tools

Usage: hscc-governance <command> [args]

Commands:
  policy-eval <action> [agent_id] [context]   Evaluate policy rules for an action
  check-permission <agent_id> <tool_name>     Check if agent can use a tool
  record-audit <agent_id> <tool_name> <args> [result] [policy_decision]
                                               Record a tool invocation to audit log
  list-audit [agent_id] [limit] [tool_name]    Query the audit log
  classify-tool <tool_name>                    Show RBAC tier classification
  update-policy <add|remove|list|show> [args]  Manage policy.json rules
  enforce <agent_id> <tool_name> [args]        Full gate: check-permission + policy-eval + record-audit
"""

import sys
import json
import os
import uuid
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
POLICY_FILE = os.path.join(HSCC_DIR, "policy.json")
AUDIT_FILE = os.path.join(HSCC_DIR, "audit.jsonl")
AGENTS_JSON = os.path.expanduser("~/.r2d2cc/agents.json")
LIFECYCLE_FILE = os.path.join(HSCC_DIR, "lifecycle.json")

# ── RBAC Tier Definitions ─────────────────────────────────────────────────

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

# Tier 3: Self-only — agent can only operate on its own lifecycle
SELF_ONLY_TOOLS = [
    "hscc_agent_transition",
]

# Tier 2: Task-scoped — require active task assignment to the calling agent
TASK_SCOPED_TOOLS = [
    "hscc_create_worktree",
    "hscc_worktree_status",
    "hscc_green_check",
    "hscc_check_collisions",
    "hscc_worktree_build",
]

# Tier 1: Unrestricted — read-only queries, health checks, governance introspection
UNRESTRICTED_TOOLS = [
    "hscc_check_permission",
    "hscc_agent_status",
    "hscc_agent_status_all",
    "hscc_agent_history",
    "hscc_list_worktrees",
    "hscc_diagnose_failure",
    "hscc_recovery_history",
    "hscc_emit_event",
    "hscc_event_history",
    "hscc_event_count",
    "hscc_cluster_health",
    "hscc_gpu_status",
    "hscc_vllm_health",
    "hscc_list_recipes",
    "hscc_evaluate_policy",
    "hscc_list_policies",
    "hscc_read_snapshot",
    "hscc_build_context",
    # Governance own-tools
    "hscc_policy_eval",
    "hscc_record_audit",
    "hscc_list_audit",
    "hscc_classify_tool",
    "hscc_update_policy",
    "hscc_enforce",
    "hscc_governance_status",
]


# ── RBAC Tier Labels ─────────────────────────────────────────────────────

RBAC_TIERS = {
    "orchestrator-only": {
        "level": 4,
        "description": "Cluster management and destructive operations — orchestrator only",
        "tools": ORCHESTRATOR_ONLY_TOOLS,
    },
    "self-only": {
        "level": 3,
        "description": "Agent lifecycle transitions — agent may only target own id",
        "tools": SELF_ONLY_TOOLS,
    },
    "task-scoped": {
        "level": 2,
        "description": "Task-dependent operations — requires active task assignment",
        "tools": TASK_SCOPED_TOOLS,
    },
    "unrestricted": {
        "level": 1,
        "description": "Read-only queries and health checks — available to all agents",
        "tools": UNRESTRICTED_TOOLS,
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────

def ensure_dir():
    """Ensure the HSCC state directory exists."""
    os.makedirs(HSCC_DIR, exist_ok=True)


def now_iso():
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def now_ts():
    """Return current UTC epoch timestamp in milliseconds."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


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
    """
    ensure_dir()
    with open(AUDIT_FILE, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


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
    agent = get_agent_info(agent_id)
    return agent.get("role", "unknown") if agent else "unknown"


def get_agent_status(agent_id):
    """Get agent status from lifecycle.json."""
    ensure_dir()
    lifecycle = read_json_file(LIFECYCLE_FILE, {"agents": {}})
    agents_lc = lifecycle.get("agents", {})
    lc_data = agents_lc.get(agent_id, {})
    return lc_data.get("state", "unknown")


def classify_tool(tool_name):
    """Classify a tool into its RBAC tier. Returns tier dict or default."""
    for tier_name, tier_info in RBAC_TIERS.items():
        if tool_name in tier_info["tools"]:
            return {
                "tool": tool_name,
                "tier": tier_name,
                "level": tier_info["level"],
                "description": tier_info["description"],
            }
    # Default: unrestricted — any new tool must be explicitly classified
    return {
        "tool": tool_name,
        "tier": "unrestricted",
        "level": 0,
        "description": "Unclassified — defaults to unrestricted. MUST be explicitly classified.",
        "warning": "unclassified_tool",
    }


def load_policy():
    """Load policy.json, creating default structure if missing."""
    ensure_dir()
    if not os.path.exists(POLICY_FILE):
        write_json_file(POLICY_FILE, {"rules": []})
    return read_json_file(POLICY_FILE, {"rules": []})


# ── 1. Policy Evaluation Engine ──────────────────────────────────────────

def evaluate_policy(action, agent_id=None, context=None):
    """Evaluate policy rules against a given action.

    Policy rules have the structure:
      {
        "id": "rule_name",
        "action": "deny" | "allow",
        "description": "human readable",
        "applies_to": ["agent_id_or_role", ...],   # [] means all
        "condition": {
          "metric": "event_type" | "agent_role" | "tool_name" | "cluster_state",
          "op": "eq" | "ne" | "in" | "not_in" | "gt" | "lt",
          "value": ...
        },
        "enabled": true | false
      }

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
    matched_rules = []
    first_deny = None

    agent_role = get_agent_role(agent_id) if agent_id else "unknown"

    for rule in rules:
        if not rule.get("enabled", True):
            continue

        # Check if rule applies to this agent/role
        applies_to = rule.get("applies_to", [])
        if applies_to and agent_id:
            # Check if agent_id or role is in applies_to list
            if agent_id not in applies_to and agent_role not in applies_to:
                continue

        condition = rule.get("condition", {})
        metric = condition.get("metric", "")
        op = condition.get("op", "eq")
        rule_value = condition.get("value")

        # Resolve the actual value from context
        actual_value = context.get(metric, "")
        if not actual_value:
            # Map known metrics to actual values
            metric_map = {
                "action": action,
                "agent_id": agent_id,
                "agent_role": agent_role,
                "event_type": context.get("event_type", ""),
                "tool_name": context.get("tool_name", ""),
                "cluster_state": context.get("cluster_state", ""),
            }
            actual_value = metric_map.get(metric, "")

        # Evaluate operator
        matched = False
        if op == "eq":
            matched = actual_value == rule_value
        elif op == "ne":
            matched = actual_value != rule_value
        elif op == "in":
            matched = actual_value in rule_value if isinstance(rule_value, list) else False
        elif op == "not_in":
            matched = actual_value not in rule_value if isinstance(rule_value, list) else True
        elif op == "gt":
            try:
                matched = float(actual_value) > float(rule_value)
            except (ValueError, TypeError):
                matched = False
        elif op == "lt":
            try:
                matched = float(actual_value) < float(rule_value)
            except (ValueError, TypeError):
                matched = False
        else:
            matched = actual_value == rule_value  # fallback

        if matched:
            matched_rules.append(rule)
            if rule.get("action") == "deny" and first_deny is None:
                first_deny = rule

    if first_deny:
        return {
            "action": action,
            "agent_id": agent_id,
            "policy_decision": "deny",
            "matched_rules": [r.get("id") for r in matched_rules],
            "rationale": f"Denied by policy rule '{first_deny.get('id', 'unknown')}': {first_deny.get('description', 'no description')}",
        }

    # Check if any allow rules matched explicitly
    allow_rules = [r for r in matched_rules if r.get("action") == "allow"]
    if allow_rules:
        return {
            "action": action,
            "agent_id": agent_id,
            "policy_decision": "allow",
            "matched_rules": [r.get("id") for r in allow_rules],
            "rationale": f"Explicitly allowed by policy rule(s): {', '.join(r.get('id', '?') for r in allow_rules)}",
        }

    # No rules matched — default allow (permissive by default)
    return {
        "action": action,
        "agent_id": agent_id,
        "policy_decision": "allow",
        "matched_rules": [],
        "rationale": "No matching policy rules — default allow",
    }


# ── 2. Gateway Permission Proxy ──────────────────────────────────────────

def check_permission(agent_id, tool_name):
    """Check if an agent has permission to invoke a tool.

    Validates against the 4-tier RBAC system:
      - orchestrator-only: agent must have role 'orchestrator'
      - self-only: agent can only use the tool targeting its own id
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

    if tier == "orchestrator-only":
        if agent_role != "orchestrator":
            result["reason"] = f"Tool '{tool_name}' requires orchestrator role. Agent has role: {agent_role}"
        else:
            result["allowed"] = True
            result["reason"] = "Orchestrator role — full access granted"

    elif tier == "self-only":
        # Self-only tools are allowed for the agent itself
        result["allowed"] = True
        result["reason"] = f"Self-only tool — agent {agent_id} is authorized for own lifecycle ops"

    elif tier == "task-scoped":
        # Check if agent has an active task assignment
        project_file = os.path.expanduser("~/.hscc/projects.json")
        has_active_task = False
        if os.path.exists(project_file):
            try:
                with open(project_file) as f:
                    proj = json.load(f)
                for project in proj.get("projects", []):
                    for roadmap in project.get("roadmaps", []):
                        for sub in roadmap.get("subProjects", []):
                            for task in sub.get("tasks", []):
                                if task.get("assignedAgent") == agent_id and task.get("status") == "inProgress":
                                    has_active_task = True
                                    break
                            if has_active_task:
                                break
                        if has_active_task:
                            break
                    if has_active_task:
                        break
            except Exception:
                pass

        if has_active_task:
            result["allowed"] = True
            result["reason"] = f"Agent {agent_id} has active task — task-scoped tool allowed"
        else:
            result["reason"] = f"Agent {agent_id} has no active task assignment. Task-scoped tool requires active task."

    elif tier == "unrestricted":
        result["allowed"] = True
        result["reason"] = "Unrestricted tool — available to all agents"

    return result


# ── 3. Append-Only Event Audit Log ───────────────────────────────────────

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
      - context: additional context (task_id, session_id, etc.)

    This is append-only — entries are never modified or deleted.
    """
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
        "rbac_tier": classify_tool(tool_name)["tier"],
        "rbac_level": classify_tool(tool_name)["level"],
        "context": context if context else {},
    }

    append_audit_log(entry)
    return entry


def read_audit_log(agent_id=None, limit=50, tool_name=None, offset=0):
    """Read entries from the audit log.

    Supports filtering by agent_id and tool_name, plus pagination via limit.
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

        entries.append(entry)

    # Reverse chronological order (newest first)
    entries.reverse()

    # Apply offset and limit
    entries = entries[offset:offset + limit]
    return entries


# ── 4. Full Enforcement Gate ────────────────────────────────────────────

def enforce_action(agent_id, tool_name, args=None, context=None):
    """Full governance gate: check-permission → policy-eval → record-audit.

    This is the main enforcement entry point. It:
      1. Checks RBAC permission (permission proxy)
      2. Evaluates policy rules (policy engine)
      3. Records the decision to the audit log
      4. Returns a comprehensive decision object

    Returns:
      {
        "agent_id": ...,
        "tool_name": ...,
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

    # Step 1: RBAC Permission Check
    rbac_result = check_permission(agent_id, tool_name)

    # Step 2: Policy Evaluation
    action = f"tool:{tool_name}"
    policy_context = {
        "tool_name": tool_name,
        "event_type": tool_name,
        "agent_role": get_agent_role(agent_id),
    }
    policy_result = evaluate_policy(action, agent_id, policy_context)

    # Step 3: Decision — deny if EITHER rbac or policy denies
    denied_by_rbac = not rbac_result.get("allowed", False)
    denied_by_policy = policy_result.get("policy_decision") == "deny"
    final_decision = "deny" if (denied_by_rbac or denied_by_policy) else "allow"

    if denied_by_rbac:
        policy_decision = "deny"
    elif denied_by_policy:
        policy_decision = "deny"
    else:
        policy_decision = policy_result.get("policy_decision", "allow")

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

    return {
        "agent_id": agent_id,
        "tool_name": tool_name,
        "rbac_check": rbac_result,
        "policy_check": policy_result,
        "audit_entry": audit_entry,
        "final_decision": final_decision,
        "reason": (
            f"RBAC: {rbac_result.get('reason', '')} | "
            f"Policy: {policy_result.get('rationale', '')}"
        ),
    }


# ── Command Handlers ─────────────────────────────────────────────────────

def cmd_policy_eval(args_str, agent_id=None):
    """Evaluate policy rules for a given action.

    Usage: hscc-governance policy-eval <action> [agent_id] [context_json]
    """
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


def cmd_check_permission(agent_id, tool_name):
    """Check if agent can invoke a tool.

    Usage: hscc-governance check-permission <agent_id> <tool_name>
    """
    if not agent_id or not tool_name:
        print(json.dumps({"error": "Usage: hscc-governance check-permission <agent_id> <tool_name>"}))
        return

    result = check_permission(agent_id, tool_name)
    print(json.dumps(result, indent=2))


def cmd_record_audit(agent_id, tool_name, args_str=None, result_str=None, policy_decision=None):
    """Record a tool invocation to the audit log.

    Usage: hscc-governance record-audit <agent_id> <tool_name> [args_json] [result_json] [policy_decision]
    """
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


def cmd_list_audit(agent_id=None, limit=None, tool_name=None):
    """Query the audit log.

    Usage: hscc-governance list-audit [agent_id] [limit] [tool_name]
    """
    lim = 50
    if limit:
        try:
            lim = int(limit)
        except ValueError:
            pass

    entries = read_audit_log(agent_id=agent_id, limit=lim, tool_name=tool_name)

    if not entries:
        filters = []
        if agent_id:
            filters.append(f"agent={agent_id}")
        if tool_name:
            filters.append(f"tool={tool_name}")
        print(f"No audit entries found." + (f" ({', '.join(filters)})" if filters else ""))
        return

    print(f"Audit entries: {len(entries)} shown")
    print()

    for e in entries:
        ts = e.get("timestamp", "?")[:19]
        agent = e.get("agent_id", "?")
        tool = e.get("tool_name", "?")
        tier = e.get("rbac_tier", "?")
        decision = e.get("result", {}).get("final_decision") or e.get("policy_decision", "?")

        marker = {"allow": "✓", "deny": "✗", "unknown": "?"}.get(decision, "?")
        args_summary = json.dumps(e.get("args", {}), default=str)[:80]

        print(f"  {marker} [{ts}] agent={agent} tool={tool} tier={tier} decision={decision}")
        print(f"      args={args_summary}")
        print()

    # Summary
    decisions = {}
    for e in entries:
        d = e.get("result", {}).get("final_decision") or e.get("policy_decision", "?")
        decisions[d] = decisions.get(d, 0) + 1
    print(f"Summary: {dict(decisions)}")


def cmd_classify_tool(tool_name):
    """Show RBAC tier classification for a tool.

    Usage: hscc-governance classify-tool <tool_name>
    """
    if not tool_name:
        print(json.dumps({"error": "Usage: hscc-governance classify-tool <tool_name>"}))
        return

    result = classify_tool(tool_name)
    print(json.dumps(result, indent=2))


def cmd_list_tiers():
    """Show the full RBAC tier classification for all known tools."""
    print("HSCC Governance — RBAC Tier Classification")
    print("=" * 60)
    print()

    for tier_name in ["orchestrator-only", "self-only", "task-scoped", "unrestricted"]:
        tier = RBAC_TIERS[tier_name]
        tools = tier["tools"]
        marker = {
            "orchestrator-only": "🔴",
            "self-only": "🟠",
            "task-scoped": "🟡",
            "unrestricted": "🟢",
        }.get(tier_name, "⚪")

        print(f"  {marker} Tier {tier['level']}: {tier_name}")
        print(f"      {tier['description']}")
        print(f"      Tools ({len(tools)}):")
        for t in sorted(tools):
            print(f"        • {t}")
        print()

    # Count total
    total = sum(len(t["tools"]) for t in RBAC_TIERS.values())
    print(f"Total classified tools: {total}")


def cmd_update_policy(action, *args):
    """Manage policy.json rules.

    Usage:
      hscc-governance update-policy list              List all policy rules
      hscc-governance update-policy show <rule_id>    Show a specific rule
      hscc-governance update-policy add <id> <action> <description> <metric> <op> <value> [applies_to_json]
      hscc-governance update-policy remove <rule_id>  Remove a rule
      hscc-governance update-policy enable <rule_id>  Enable a rule
      hscc-governance update-policy disable <rule_id> Disable a rule
    """
    policy = load_policy()

    if action == "list":
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
            cond = r.get("condition", {})
            print(f"  {status} {rid:20s} — {desc}")
            print(f"       applies_to={applies}  metric={cond.get('metric','?')} {cond.get('op','?')} {cond.get('value','?')}")
            print()
        return

    if action == "show":
        if not args or not args[0]:
            print(json.dumps({"error": "Usage: hscc-governance update-policy show <rule_id>"}))
            return
        rule_id = args[0]
        for r in policy.get("rules", []):
            if r.get("id") == rule_id:
                print(json.dumps(r, indent=2))
                return
        print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))
        return

    if action == "add":
        if len(args) < 6:
            print(json.dumps({"error": "Usage: hscc-governance update-policy add <id> <action> <description> <metric> <op> <value> [applies_to_json]"}))
            return
        rule_id, rule_action, description, metric, op, value = args[:6]
        applies_to = []
        if len(args) > 6:
            try:
                applies_to = json.loads(args[6])
            except json.JSONDecodeError:
                applies_to = [args[6]]

        # Parse value
        try:
            parsed_value = int(value)
        except ValueError:
            try:
                parsed_value = float(value)
            except ValueError:
                if value.lower() in ("true", "yes"):
                    parsed_value = True
                elif value.lower() in ("false", "no"):
                    parsed_value = False
                else:
                    parsed_value = value

        # Check for duplicate ID
        for r in policy.get("rules", []):
            if r.get("id") == rule_id:
                print(json.dumps({"error": f"Policy rule ID already exists: {rule_id}"}))
                return

        rule = {
            "id": rule_id,
            "action": rule_action,
            "description": description,
            "applies_to": applies_to,
            "condition": {
                "metric": metric,
                "op": op,
                "value": parsed_value,
            },
            "enabled": True,
        }
        policy.setdefault("rules", []).append(rule)
        write_json_file(POLICY_FILE, policy)
        print(json.dumps({"success": True, "rule": rule}, indent=2))
        return

    if action == "remove":
        if not args or not args[0]:
            print(json.dumps({"error": "Usage: hscc-governance update-policy remove <rule_id>"}))
            return
        rule_id = args[0]
        rules = policy.get("rules", [])
        before = len(rules)
        rules = [r for r in rules if r.get("id") != rule_id]
        if len(rules) == before:
            print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))
            return
        policy["rules"] = rules
        write_json_file(POLICY_FILE, policy)
        print(json.dumps({"success": True, "removed": rule_id}))
        return

    if action == "enable":
        if not args or not args[0]:
            print(json.dumps({"error": "Usage: hscc-governance update-policy enable <rule_id>"}))
            return
        rule_id = args[0]
        rules = policy.get("rules", [])
        for r in rules:
            if r.get("id") == rule_id:
                r["enabled"] = True
                write_json_file(POLICY_FILE, policy)
                print(json.dumps({"success": True, "rule_id": rule_id, "enabled": True}))
                return
        print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))
        return

    if action == "disable":
        if not args or not args[0]:
            print(json.dumps({"error": "Usage: hscc-governance update-policy disable <rule_id>"}))
            return
        rule_id = args[0]
        rules = policy.get("rules", [])
        for r in rules:
            if r.get("id") == rule_id:
                r["enabled"] = False
                write_json_file(POLICY_FILE, policy)
                print(json.dumps({"success": True, "rule_id": rule_id, "enabled": False}))
                return
        print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))
        return

    print(json.dumps({"error": f"Unknown update-policy action: {action}. Use: add, remove, enable, disable, list, show"}))


def cmd_enforce(agent_id, tool_name, args_str=None, context_str=None):
    """Full governance enforcement gate.

    Usage: hscc-governance enforce <agent_id> <tool_name> [args_json] [context_json]
    """
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
    """Show a summary of the current governance state.

    Shows: policy rules count, audit log size, RBAC tier breakdown.
    """
    policy = load_policy()
    rules = policy.get("rules", [])

    # Audit log size
    ensure_dir()
    audit_size = 0
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE) as f:
                audit_size = sum(1 for line in f if line.strip())
        except IOError:
            pass

    # Tier breakdown
    tier_summary = {}
    for tier_name, tier_info in RBAC_TIERS.items():
        tier_summary[tier_name] = {
            "level": tier_info["level"],
            "tool_count": len(tier_info["tools"]),
            "tools": sorted(tier_info["tools"]),
        }

    # Audit decisions summary
    decisions = {}
    agents_seen = set()
    tools_seen = set()
    entries = read_audit_log(limit=10000, offset=0)
    for e in entries:
        d = e.get("result", {}).get("final_decision") or e.get("policy_decision", "?")
        decisions[d] = decisions.get(d, 0) + 1
        agents_seen.add(e.get("agent_id", "unknown"))
        tools_seen.add(e.get("tool_name", "unknown"))

    status = {
        "governance_state": {
            "policy_rules": len(rules),
            "audit_log_entries": audit_size,
            "unique_agents_audited": len(agents_seen),
            "unique_tools_audited": len(tools_seen),
            "decision_summary": decisions,
        },
        "rbac_tiers": tier_summary,
        "audit_log_file": AUDIT_FILE,
        "policy_file": POLICY_FILE,
    }

    print(json.dumps(status, indent=2, default=str))


# ── Command Map ───────────────────────────────────────────────────────────

COMMANDS = {
    "policy-eval": lambda: (
        cmd_policy_eval(" ".join(sys.argv[2:]), sys.argv[3] if len(sys.argv) > 3 else None)
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance policy-eval <action> [agent_id] [context_json]"}))
    ),
    "check-permission": lambda: (
        cmd_check_permission(sys.argv[2], sys.argv[3])
        if len(sys.argv) > 3
        else print(json.dumps({"error": "Usage: hscc-governance check-permission <agent_id> <tool_name>"}))
    ),
    "record-audit": lambda: (
        cmd_record_audit(
            sys.argv[2], sys.argv[3],
            sys.argv[4] if len(sys.argv) > 4 else None,
            sys.argv[5] if len(sys.argv) > 5 else None,
            sys.argv[6] if len(sys.argv) > 6 else None,
        )
        if len(sys.argv) > 3
        else print(json.dumps({"error": "Usage: hscc-governance record-audit <agent_id> <tool_name> [args_json] [result_json] [policy_decision]"}))
    ),
    "list-audit": lambda: (
        cmd_list_audit(
            sys.argv[2] if len(sys.argv) > 2 else None,
            sys.argv[3] if len(sys.argv) > 3 else None,
            sys.argv[4] if len(sys.argv) > 4 else None,
        )
    ),
    "classify-tool": lambda: (
        cmd_classify_tool(sys.argv[2])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance classify-tool <tool_name>"}))
    ),
    "update-policy": lambda: (
        cmd_update_policy(*sys.argv[2:])
        if len(sys.argv) > 2
        else print(json.dumps({"error": "Usage: hscc-governance update-policy <add|remove|list|show|enable|disable> [args]"}))
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
    "list-tiers": cmd_list_tiers,
    "help": lambda: print(USAGE),
}


# ── Entry Point ───────────────────────────────────────────────────────────

USAGE = """\
Hermes Spark Cluster Control (HSCC) — Governance & Access Control

Usage: hscc-governance <command> [args]

Commands:
  policy-eval <action> [agent_id] [context_json]
              Evaluate policy rules for an action
  check-permission <agent_id> <tool_name>
              Check RBAC permission for tool access
  record-audit <agent_id> <tool_name> [args] [result] [decision]
              Record a tool invocation to the audit log
  list-audit [agent_id] [limit] [tool_name]
              Query the append-only audit log
  classify-tool <tool_name>
              Show RBAC tier for a tool
  update-policy <add|remove|list|show|enable|disable> [args]
              Manage policy.json rules
  enforce <agent_id> <tool_name> [args] [context]
              Full gate: RBAC + policy + audit in one call
  governance-status
              Summary of governance state (rules, audit, tiers)
  list-tiers
              Full RBAC tier classification for all known tools
  help
              Show this usage message

RBAC Tiers:
  Tier 4 (🔴) orchestrator-only  — Cluster management, destructive ops
  Tier 3 (🟠) self-only         — Agent lifecycle (own id only)
  Tier 2 (🟡) task-scoped       — Task-dependent operations
  Tier 1 (🟢) unrestricted       — Read-only queries, health checks

Files:
  Policy:    ~/.hscc/policy.json
  Audit Log: ~/.hscc/audit.jsonl (append-only)
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
