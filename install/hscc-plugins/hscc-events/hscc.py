#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Events, Lifecycle & Recovery Plugin

Manages event logging, agent lifecycle state, recovery history,
notifications, trigger rules, and policy enforcement.

Usage: hscc-events <command> [args]
"""

import sys
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
from collections import Counter
import uuid

# ── Constants ──────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
AGENTS_JSON = os.path.expanduser("~/.hscc/agents.json")

EVENTS_FILE = os.path.expanduser("~/.hscc/events.jsonl")
NOTIFICATIONS_FILE = os.path.join(HSCC_DIR, "notifications.json")
LIFECYCLE_FILE = os.path.join(HSCC_DIR, "lifecycle.json")
RECOVERY_FILE = os.path.join(HSCC_DIR, "recovery.json")
POLICY_FILE = os.path.join(HSCC_DIR, "policy.json")
TRIGGERS_FILE = os.path.join(HSCC_DIR, "triggers.json")
COOLDOWN_FILE = os.path.join(HSCC_DIR, "cooldowns.json")

VALID_TRANSITIONS = {
    "idle": ["spawning", "ready", "running", "finished", "failed", "disabled"],
    "spawning": ["ready", "running", "failed", "idle", "disabled"],
    "ready": ["ready", "running", "failed", "idle", "disabled"],
    "running": ["finished", "failed", "idle", "disabled"],
    "finished": ["idle", "spawning", "running", "disabled"],
    "failed": ["idle", "spawning", "disabled"],
    "disabled": ["idle"],
}

RESOLUTION_STATES = {"finished", "failed", "idle", "disabled"}

ORCHESTRATOR_ONLY_TOOLS = [
    "hscc_merge_worktree", "hscc_remove_worktree", "hscc_attempt_recovery",
    "hscc_detect_stale_worktrees", "hscc_register_task_assignment",
    "hscc_clear_task_assignment", "hscc_notify", "hscc_create_snapshot",
    "hscc_rotate_events", "hscc_reset", "hscc_stop_model",
    "hscc_evaluate_triggers", "hscc_list_trigger_rules",
]
SELF_ONLY_TOOLS = ["hscc_agent_transition"]
TASK_SCOPED_TOOLS = [
    "hscc_create_worktree", "hscc_worktree_status", "hscc_green_check",
    "hscc_check_collisions",
]
UNRESTRICTED_TOOLS = [
    "hscc_check_permission", "hscc_agent_status", "hscc_agent_status_all",
    "hscc_agent_history", "hscc_list_worktrees", "hscc_diagnose_failure",
    "hscc_recovery_history", "hscc_emit_event", "hscc_event_history",
    "hscc_event_count", "hscc_cluster_health", "hscc_gpu_status",
    "hscc_vllm_health", "hscc_list_recipes", "hscc_evaluate_policy",
    "hscc_list_policies", "hscc_read_snapshot", "hscc_build_context",
]

# ── Helpers ────────────────────────────────────────────────────────────────

def ensure_dir():
    os.makedirs(HSCC_DIR, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_json_file(path, default=None):
    ensure_dir()
    if not os.path.exists(path):
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def write_json_file(path, data):
    ensure_dir()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)


def parse_value(val):
    """Parse a string value to appropriate type."""
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    # Check between format "low,high"
    if "," in val:
        parts = [p.strip() for p in val.split(",")]
        try:
            return [int(p) for p in parts]
        except ValueError:
            try:
                return [float(p) for p in parts]
            except ValueError:
                return parts
    # Boolean?
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    return val


def read_events(event_type=None, limit=50, since=None):
    ensure_dir()
    if not os.path.exists(EVENTS_FILE):
        return []
    try:
        with open(EVENTS_FILE) as f:
            lines = f.readlines()
    except IOError:
        return []

    events = []
    since_ts = None
    if since:
        try:
            since_ts = datetime.fromisoformat(since).timestamp() * 1000
        except ValueError:
            pass

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if event_type:
                typ = e.get("event_type", "")
                if typ != event_type and not typ.startswith(event_type + ".") and event_type not in typ:
                    continue
            if since_ts:
                e_ts = e.get("timestamp", "")
                if e_ts:
                    try:
                        evt_ts = datetime.fromisoformat(e_ts).timestamp() * 1000
                        if evt_ts < since_ts:
                            continue
                    except ValueError:
                        pass
            events.append(e)
        except json.JSONDecodeError:
            continue

    events.reverse()
    return events[:limit]


def count_events(event_type=None, since=None):
    events = read_events(event_type=None, limit=None, since=since)
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type or e.get("event_type", "").startswith(event_type + ".")]
    return len(events)


def load_agents_list():
    if not os.path.exists(AGENTS_JSON):
        return []
    try:
        with open(AGENTS_JSON) as f:
            data = json.load(f)
        return data.get("agents", [])
    except (json.JSONDecodeError, IOError):
        return []


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_events(event_type=None):
    events = read_events(event_type=event_type, limit=50)
    if not events:
        print(f"No events found." + (f" (filter: {event_type})" if event_type else ""))
        return

    severity_counts = Counter(e.get("severity", "info") for e in events)
    type_counts = Counter(e.get("event_type", "?") for e in events)

    print(f"Events: {len(events)} shown (total: {sum(severity_counts.values())})")
    print(f"Severities: {dict(severity_counts)}")
    print(f"Types: {dict(type_counts)}")
    print()

    for e in events:
        ts = e.get("timestamp", "?")[:19]
        sev = e.get("severity", "?")
        typ = e.get("event_type", "?")
        src = e.get("source", "?")
        payload = e.get("payload", {})

        if "agent_id" in payload:
            detail = f"agent={payload['agent_id']}"
            if "task_id" in payload:
                detail += f" task={str(payload['task_id'])[:8]}..."
            for k in ("success", "exit_code", "session_id"):
                if k in payload:
                    detail += f" {k}={payload[k]}"
        elif "task_id" in payload:
            detail = f"task={str(payload['task_id'])[:8]}..."
        else:
            detail = json.dumps(payload, default=str)[:100]

        sev_marker = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(sev, "⚪")
        print(f"{sev_marker} [{ts}] {typ}")
        print(f"   source={src}  {detail}")
        print()


def cmd_event_count():
    total = count_events()
    by_type = Counter()
    for e in read_events(limit=10000):
        by_type[e.get("event_type", "?")] += 1
    print(json.dumps({"total": total, "by_type": dict(by_type)}, indent=2))


def cmd_lifecycle():
    lifecycle = read_json_file(LIFECYCLE_FILE, {"agents": {}})
    agents_list = lifecycle.get("agents", {})
    all_agents = load_agents_list()

    for a in all_agents:
        aid = a["id"]
        lc = agents_list.get(aid, {})
        state = lc.get("state", "unknown")
        updated = lc.get("updated_at", "")[:19] if lc else "N/A"
        transitions = lc.get("transitions", 0)
        print(f"{aid:12s} state={state:12s} updated={updated} transitions={transitions}")


def cmd_lifecycle_show(agent_id):
    lifecycle = read_json_file(LIFECYCLE_FILE, {"agents": {}})
    agents = lifecycle.get("agents", {})
    if agent_id not in agents:
        print(json.dumps({"error": f"No lifecycle data for agent: {agent_id}", "agent_id": agent_id}, indent=2))
        return
    all_agents = load_agents_list()
    agent_info = None
    for a in all_agents:
        if a["id"] == agent_id:
            agent_info = a
            break
    result = {"lifecycle": agents[agent_id]}
    if agent_info:
        result["agent_config"] = agent_info
    print(json.dumps(result, indent=2))


def cmd_recovery():
    recovery = read_json_file(RECOVERY_FILE, {"history": []})
    history = recovery.get("history", [])
    recent = history[-20:][::-1]
    if not recent:
        print("No recovery history.")
        return
    total = len(history)
    by_outcome = Counter(h.get("outcome", "unknown") for h in history)
    print(f"Recovery attempts: {total} total")
    print(f"Outcomes: {dict(by_outcome)}")
    print()
    for h in recent:
        ts = h.get("timestamp", "?")[:19]
        agent = h.get("agent_id", "?")
        outcome = h.get("outcome", "?")
        attempt = h.get("attempt", "?")
        reason = (h.get("reason") or h.get("description", ""))[:80]
        print(f"  [{ts}] {agent} attempt={attempt} outcome={outcome}")
        if reason:
            print(f"         {reason}")
        print()


def cmd_recovery_detail(recovery_id=None):
    recovery = read_json_file(RECOVERY_FILE, {"history": []})
    history = recovery.get("history", [])
    if recovery_id:
        for h in history:
            if h.get("id") == recovery_id:
                print(json.dumps(h, indent=2))
                return
        print(json.dumps({"error": f"Recovery record not found: {recovery_id}"}))
        return
    if history:
        print(json.dumps(history[-1], indent=2))
    else:
        print("No recovery history.")


def cmd_notifications():
    notifications = read_json_file(NOTIFICATIONS_FILE, {"notifications": []})
    notifs = notifications.get("notifications", [])
    unread = [n for n in notifs if not n.get("read", False)]
    total = len(notifs)
    print(f"Notifications: {len(unread)} unread of {total} total")
    if unread:
        print()
        priority_order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        sorted_unread = sorted(unread, key=lambda n: priority_order.get(n.get("priority", "normal"), 99))
        for n in sorted_unread:
            ts = n.get("timestamp", "?")[:19]
            pri = n.get("priority", "normal")
            title = n.get("title", "No title")
            body = (n.get("body") or "")[:100]
            ch = n.get("channel", "?")
            print(f"  [{ts}] [{pri}] [{ch}] {title}")
            if body:
                print(f"         {body}")
            print()


def cmd_notify(priority, title, body):
    ensure_dir()
    data = read_json_file(NOTIFICATIONS_FILE, {"notifications": []})
    notif = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": now_iso(),
        "read": False,
        "priority": priority,
        "title": title,
        "body": body,
        "channel": "manual",
    }
    data["notifications"].append(notif)
    write_json_file(NOTIFICATIONS_FILE, data)
    print(json.dumps({"success": True, "notification": notif}, indent=2))


def cmd_notify_read(notif_id):
    ensure_dir()
    if not os.path.exists(NOTIFICATIONS_FILE):
        print(json.dumps({"error": "No notifications file"}))
        return
    data = read_json_file(NOTIFICATIONS_FILE, {"notifications": []})
    for n in data.get("notifications", []):
        if n.get("id") == notif_id:
            n["read"] = True
            write_json_file(NOTIFICATIONS_FILE, data)
            print(json.dumps({"success": True, "id": notif_id, "read": True}))
            return
    print(json.dumps({"error": f"Notification not found: {notif_id}"}))


def cmd_notify_clear():
    ensure_dir()
    if not os.path.exists(NOTIFICATIONS_FILE):
        print(json.dumps({"success": True, "cleared": 0}))
        return
    data = read_json_file(NOTIFICATIONS_FILE, {"notifications": []})
    notifs = data.get("notifications", [])
    cleared = sum(1 for n in notifs if n.get("read", False))
    data["notifications"] = [n for n in notifs if not n.get("read", False)]
    write_json_file(NOTIFICATIONS_FILE, data)
    print(json.dumps({"success": True, "cleared": cleared, "remaining": len(data["notifications"])}))


def cmd_rules():
    rules = read_json_file(TRIGGERS_FILE, {"rules": []})
    cooldowns = read_json_file(COOLDOWN_FILE, {})
    rlist = rules.get("rules", [])
    if not rlist:
        print("No trigger rules configured.")
        return
    print(f"Trigger rules: {len(rlist)}")
    print()
    for r in rlist:
        rid = r.get("id", "?")
        active = r.get("enabled", True)
        status = "✓" if active else "✗"
        applies = r.get("applies_to", [])
        cooldown = r.get("cooldown_seconds", 0)
        last_fired = cooldowns.get(rid, "")[:19] if cooldowns.get(rid) else "never"
        print(f"  {status} {rid}")
        print(f"       applies_to={applies}")
        print(f"       cooldown={cooldown}s  last_fired={last_fired}")
        print()


def cmd_rule_add(rule_id, rule_type, metric, op, value, cooldown=0):
    ensure_dir()
    rules_data = read_json_file(TRIGGERS_FILE, {"rules": []})
    rules = rules_data.get("rules", [])
    for r in rules:
        if r.get("id") == rule_id:
            print(json.dumps({"error": f"Rule ID already exists: {rule_id}"}))
            return
    rule = {
        "id": rule_id,
        "trigger_type": rule_type,
        "applies_to": [],
        "condition": {"metric": metric, "op": op, "value": parse_value(value)},
        "cooldown_seconds": int(cooldown),
        "enabled": True,
    }
    if rule_type == "notify":
        rule["trigger_params"] = {"title": f"Trigger: {rule_id}", "body": f"Metric {metric} {op} {value}"}
        rule["applies_to"] = ["*"]
    elif rule_type == "tool_call":
        rule["trigger_params"] = {"tool_name": "", "tool_params": {}}
        rule["applies_to"] = ["*"]
    elif rule_type == "emit_event":
        rule["trigger_params"] = {"event_type": f"trigger.{rule_id}", "payload": {}}
        rule["applies_to"] = ["*"]
    rules.append(rule)
    rules_data["rules"] = rules
    write_json_file(TRIGGERS_FILE, rules_data)
    print(json.dumps({"success": True, "rule": rule}, indent=2))


def cmd_rule_remove(rule_id):
    ensure_dir()
    rules_data = read_json_file(TRIGGERS_FILE, {"rules": []})
    rules = rules_data.get("rules", [])
    before = len(rules)
    rules = [r for r in rules if r.get("id") != rule_id]
    if len(rules) == before:
        print(json.dumps({"error": f"Rule not found: {rule_id}"}))
        return
    rules_data["rules"] = rules
    write_json_file(TRIGGERS_FILE, rules_data)
    print(json.dumps({"success": True, "removed": rule_id}))


def cmd_rule_reset_cooldown(rule_id):
    cooldowns = read_json_file(COOLDOWN_FILE, {})
    if rule_id in cooldowns:
        del cooldowns[rule_id]
        write_json_file(COOLDOWN_FILE, cooldowns)
        print(json.dumps({"success": True, "rule_id": rule_id, "cooldown_cleared": True}))
    else:
        print(json.dumps({"success": True, "rule_id": rule_id, "cooldown_cleared": False, "reason": "rule had no cooldown"}))


def cmd_policy():
    policy = read_json_file(POLICY_FILE, {"rules": []})
    rules = policy.get("rules", [])
    if not rules:
        print("No policy rules configured.")
        return
    print(f"Policy rules: {len(rules)}")
    print()
    for r in rules:
        rid = r.get("id", "?")
        active = r.get("enabled", True)
        status = "✓" if active else "✗"
        applies = r.get("applies_to", [])
        cond = r.get("condition", {})
        print(f"  {status} {rid} — deny when {cond.get('metric','?')} {cond.get('op','?')} {cond.get('value','?')}")
        print(f"       applies_to={applies}")
        print()


def cmd_policy_add(rule_id, condition_type, metric, op, value):
    ensure_dir()
    policy = read_json_file(POLICY_FILE, {"rules": []})
    rules = policy.get("rules", [])
    for r in rules:
        if r.get("id") == rule_id:
            print(json.dumps({"error": f"Policy rule ID already exists: {rule_id}"}))
            return
    rule = {
        "id": rule_id,
        "action": "deny",
        "description": f"Deny when {metric} {op} {value}",
        "applies_to": [],
        "condition": {"metric": metric, "op": op, "value": parse_value(value)},
        "enabled": True,
    }
    rules.append(rule)
    policy["rules"] = rules
    write_json_file(POLICY_FILE, policy)
    print(json.dumps({"success": True, "rule": rule}, indent=2))


def cmd_policy_remove(rule_id):
    ensure_dir()
    policy = read_json_file(POLICY_FILE, {"rules": []})
    rules = policy.get("rules", [])
    before = len(rules)
    rules = [r for r in rules if r.get("id") != rule_id]
    if len(rules) == before:
        print(json.dumps({"error": f"Policy rule not found: {rule_id}"}))
        return
    policy["rules"] = rules
    write_json_file(POLICY_FILE, policy)
    print(json.dumps({"success": True, "removed": rule_id}))


def cmd_perms():
    print("Permission tool categories:")
    print()
    print("Orchestrator-only tools:")
    for t in ORCHESTRATOR_ONLY_TOOLS:
        print(f"  • {t}")
    print()
    print("Self-only tools (agent can only target own id):")
    for t in SELF_ONLY_TOOLS:
        print(f"  • {t}")
    print()
    print("Task-scoped tools (require active task assignment):")
    for t in TASK_SCOPED_TOOLS:
        print(f"  • {t}")
    print()
    print("Unrestricted tools (read-only queries, health checks):")
    for t in UNRESTRICTED_TOOLS:
        print(f"  • {t}")


def cmd_clear_recovery():
    ensure_dir()
    write_json_file(RECOVERY_FILE, {"history": []})
    print(json.dumps({"success": True, "cleared": True}))


def cmd_clear_notifications():
    ensure_dir()
    write_json_file(NOTIFICATIONS_FILE, {"notifications": []})
    print(json.dumps({"success": True, "cleared": True}))


def cmd_compact(days=7):
    """Compact events older than N days."""
    ensure_dir()
    if not os.path.exists(EVENTS_FILE):
        print(json.dumps({"success": True, "kept": 0, "removed": 0, "reason": "no events file"}))
        return
    try:
        days = int(days)
    except (ValueError, TypeError):
        days = 7
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.isoformat()

    kept = []
    removed = 0
    try:
        with open(EVENTS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    ts = e.get("timestamp", "")
                    if ts and ts < cutoff_ts:
                        removed += 1
                    else:
                        kept.append(line)
                except json.JSONDecodeError:
                    kept.append(line)
    except IOError:
        print(json.dumps({"error": "Failed to read events file"}))
        return

    with open(EVENTS_FILE, "w") as f:
        f.write("\n".join(kept) + "\n" if kept else "")

    print(json.dumps({"success": True, "kept": len(kept), "removed": removed, "days": days}))


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "events": lambda: cmd_events(sys.argv[2]) if len(sys.argv) > 2 else cmd_events(None),
        "event-count": cmd_event_count,
        "lifecycle": cmd_lifecycle,
        "lifecycle-show": lambda: cmd_lifecycle_show(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-events lifecycle-show <agent_id>"})),
        "recovery": cmd_recovery,
        "recovery-detail": lambda: cmd_recovery_detail(sys.argv[2]) if len(sys.argv) > 2 else cmd_recovery_detail(None),
        "notifications": cmd_notifications,
        "notify": lambda: cmd_notify(sys.argv[2], sys.argv[3], sys.argv[4]) if len(sys.argv) > 4 else print(json.dumps({"error": "Usage: hscc-events notify <priority> <title> <body>"})),
        "notify-read": lambda: cmd_notify_read(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-events notify-read <id>"})),
        "notify-clear": cmd_notify_clear,
        "rules": cmd_rules,
        "rule-add": lambda: cmd_rule_add(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7] if len(sys.argv) > 7 else 0) if len(sys.argv) > 6 else print(json.dumps({"error": "Usage: hscc-events rule-add <id> <type> <metric> <op> <value> [cooldown]"})),
        "rule-remove": lambda: cmd_rule_remove(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-events rule-remove <id>"})),
        "rule-reset-cooldown": lambda: cmd_rule_reset_cooldown(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-events rule-reset-cooldown <id>"})),
        "policy": cmd_policy,
        "policy-add": lambda: cmd_policy_add(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]) if len(sys.argv) > 6 else print(json.dumps({"error": "Usage: hscc-events policy-add <id> <type> <metric> <op> <value>"})),
        "policy-remove": lambda: cmd_policy_remove(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-events policy-remove <id>"})),
        "perms": cmd_perms,
        "clear-recovery": cmd_clear_recovery,
        "clear-notifications": cmd_clear_notifications,
        "compact": lambda: cmd_compact(sys.argv[2]) if len(sys.argv) > 2 else cmd_compact(7),
    }

    if cmd not in commands:
        print(json.dumps({"error": f"Unknown command: {cmd}. Available: {list(commands.keys())}"}))
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()
