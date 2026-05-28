#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Orchestrator Plugin

Manage the agent fleet: view, configure, enable/disable, and route tasks.

Usage: hscc-orchestrator <command> [args]

Commands:
  fleet             List all agents with status summary
  agents            Detailed list of all agents
  show <agent_id>   Show details for a specific agent
  configure <agent_id> <field> <value>
                    Update an agent field (temperature, model, tools, etc.)
  enable <agent_id> Enable a disabled agent
  disable <agent_id> Disable an agent (keeps it in fleet)
  available         Show agents currently idle and available
  status            Quick status: count by role, idle/failed/etc.
  route <agent_id> <task_description>
                    Assign a task to an agent (marks it inProgress)
"""

import sys
import json
import os
import copy
from datetime import datetime, timezone

# ── Constants ──────────────────────────────────────────────────────────────

AGENTS_JSON = os.path.expanduser("~/.hscc/agents.json")

# ── Helpers ────────────────────────────────────────────────────────────────

def load_agents():
    """Load agents.json and return (full_data, agents_list)."""
    if not os.path.exists(AGENTS_JSON):
        print(json.dumps({"error": f"File not found: {AGENTS_JSON}"}))
        sys.exit(1)
    with open(AGENTS_JSON) as f:
        data = json.load(f)
    return data, data.get("agents", [])


def save_agents(data):
    """Save the full agents.json back to disk."""
    with open(AGENTS_JSON, "w") as f:
        json.dump(data, f, indent=4)


def find_agent(agents, agent_id):
    """Find an agent by ID. Returns (index, agent) or (None, None)."""
    for i, a in enumerate(agents):
        if a.get("id") == agent_id:
            return i, a
    return None, None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_fleet():
    """List all agents with a compact status summary."""
    data, agents = load_agents()
    total = len(agents)
    idle = sum(1 for a in agents if a.get("status") == "idle")
    failed = sum(1 for a in agents if a.get("status") == "failed")
    busy = total - idle - failed  # anything else (working, error, etc.)
    enabled = sum(1 for a in agents if a.get("enabled", True))
    disabled = total - enabled

    # Group by role
    by_role = {}
    for a in agents:
        role = a.get("role", "unknown")
        if role not in by_role:
            by_role[role] = {"total": 0, "idle": 0, "failed": 0}
        by_role[role]["total"] += 1
        if a.get("status") == "idle":
            by_role[role]["idle"] += 1
        elif a.get("status") == "failed":
            by_role[role]["failed"] += 1

    result = {
        "fleet_summary": {
            "total": total,
            "idle": idle,
            "failed": failed,
            "busy": busy,
            "enabled": enabled,
            "disabled": disabled,
        },
        "by_role": by_role,
    }

    # Print summary
    print(f"Fleet: {total} agents | Idle: {idle} | Failed: {failed} | Busy: {busy}")
    print(f"Enabled: {enabled} | Disabled: {disabled}")
    print()
    for role, counts in by_role.items():
        print(f"  {role}: {counts['total']} total, {counts['idle']} idle, {counts['failed']} failed")


def cmd_agents():
    """Detailed list of all agents."""
    data, agents = load_agents()

    for a in agents:
        enabled_str = "✓" if a.get("enabled", True) else "✗"
        role = a.get("role", "?")
        model = a.get("model", "")[:50]
        status = a.get("status", "?")
        tools = ", ".join(a.get("tools", []))
        print(f"{a['id']:12s} [{enabled_str}] role={role:15s} status={status:10s} tools=[{tools}]")
        print(f"{'':12s}       model={model}...")


def cmd_show(agent_id):
    """Show details for a specific agent."""
    data, agents = load_agents()
    idx, agent = find_agent(agents, agent_id)

    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    # Also find task assignments from projects
    tasks_assigned = []
    project_file = os.path.expanduser("~/.hscc/projects.json")
    if os.path.exists(project_file):
        try:
            with open(project_file) as f:
                proj = json.load(f)
            for project in proj.get("projects", []):
                for roadmap in project.get("roadmaps", []):
                    for sub in roadmap.get("subProjects", []):
                        for task in sub.get("tasks", []):
                            if task.get("assignedAgent") == agent_id:
                                tasks_assigned.append({
                                    "title": task.get("title", ""),
                                    "status": task.get("status", ""),
                                    "roadmap": roadmap.get("name", ""),
                                    "subProject": sub.get("name", ""),
                                    "priority": task.get("priority", ""),
                                })
        except Exception:
            pass

    result = copy.deepcopy(agent)
    result["currentTasks"] = tasks_assigned
    print(json.dumps(result, indent=2))


def cmd_configure(agent_id, field, value):
    """Configure an agent field."""
    data, agents = load_agents()
    idx, agent = find_agent(agents, agent_id)

    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    # Handle multi-value fields (tools, mcpServers, skills)
    if field in ("tools", "mcpServers", "skills"):
        if value == "":
            agent[field] = []
        else:
            agent[field] = [v.strip() for v in value.split(",")]
    # Handle temperature as float
    elif field == "temperature":
        try:
            agent[field] = float(value)
        except ValueError:
            print(json.dumps({"error": f"Invalid temperature value: {value}"}))
            return
    elif field == "enabled":
        agent[field] = value.lower() in ("true", "yes", "1", "on")
    elif field == "maxTokens":
        try:
            agent[field] = int(value)
        except ValueError:
            print(json.dumps({"error": f"Invalid maxTokens value: {value}"}))
            return
    else:
        agent[field] = value

    data["agents"][idx] = agent
    save_agents(data)

    print(json.dumps({
        "success": True,
        "agent_id": agent_id,
        "field": field,
        "value": agent[field],
    }, indent=2))


def cmd_enable(agent_id):
    """Enable a disabled agent."""
    data, agents = load_agents()
    idx, agent = find_agent(agents, agent_id)

    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    agent["enabled"] = True
    data["agents"][idx] = agent
    save_agents(data)
    print(json.dumps({"success": True, "agent_id": agent_id, "enabled": True}))


def cmd_disable(agent_id):
    """Disable an agent."""
    data, agents = load_agents()
    idx, agent = find_agent(agents, agent_id)

    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    agent["enabled"] = False
    agent["status"] = "idle"  # Reset status when disabled
    data["agents"][idx] = agent
    save_agents(data)
    print(json.dumps({"success": True, "agent_id": agent_id, "enabled": False}))


def cmd_available():
    """Show agents currently idle and available."""
    data, agents = load_agents()
    available = [
        a for a in agents
        if a.get("enabled", True) and a.get("status") == "idle"
    ]

    print(f"Available agents: {len(available)}")
    for a in available:
        model = a.get("model", "")[:50]
        tools = ", ".join(a.get("tools", []))
        print(f"  {a['id']:12s} role={a.get('role', '?'):15s} model={model}... tools=[{tools}]")


def cmd_status():
    """Quick status summary."""
    data, agents = load_agents()
    total = len(agents)
    by_status = {}
    for a in agents:
        s = a.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1

    by_role = {}
    for a in agents:
        r = a.get("role", "unknown")
        by_role[r] = by_role.get(r, 0) + 1

    by_enabled = {"enabled": 0, "disabled": 0}
    for a in agents:
        key = "enabled" if a.get("enabled", True) else "disabled"
        by_enabled[key] += 1

    print(json.dumps({
        "total": total,
        "by_status": by_status,
        "by_role": by_role,
        "by_enabled": by_enabled,
    }, indent=2))


def cmd_route(agent_id, task_desc):
    """Route (assign) a task to an agent."""
    data, agents = load_agents()
    idx, agent = find_agent(agents, agent_id)

    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    if not agent.get("enabled", True):
        print(json.dumps({"error": f"Agent {agent_id} is disabled"}))
        return

    # Update agent status
    agent["status"] = "working"
    data["agents"][idx] = agent
    save_agents(data)

    print(json.dumps({
        "success": True,
        "agent_id": agent_id,
        "status": "working",
        "task": task_desc,
        "timestamp": now_iso(),
    }, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "fleet": cmd_fleet,
        "agents": cmd_agents,
        "show": lambda: cmd_show(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-orchestrator show <agent_id>"})),
        "configure": lambda: cmd_configure(sys.argv[2], sys.argv[3], sys.argv[4]) if len(sys.argv) > 4 else print(json.dumps({"error": "Usage: hscc-orchestrator configure <agent_id> <field> <value>"})),
        "enable": lambda: cmd_enable(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-orchestrator enable <agent_id>"})),
        "disable": lambda: cmd_disable(sys.argv[2]) if len(sys.argv) > 2 else print(json.dumps({"error": "Usage: hscc-orchestrator disable <agent_id>"})),
        "available": cmd_available,
        "status": cmd_status,
        "route": lambda: cmd_route(sys.argv[2], sys.argv[3]) if len(sys.argv) > 3 else print(json.dumps({"error": "Usage: hscc-orchestrator route <agent_id> <task_description>"})),
    }

    if cmd not in commands:
        print(json.dumps({"error": f"Unknown command: {cmd}. Available: {list(commands.keys())}"}))
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()
