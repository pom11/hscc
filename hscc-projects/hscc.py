#!/usr/bin/env python3
"""
Hermes Spark Cluster Control — Project & Kanban Management Plugin

Manages projects, roadmaps, sub-projects, and tasks with kanban-style workflow.

Usage: hscc-projects <command> [args]

Commands:
  list                       List all projects and active project
  create <name> <desc>       Create a new project
  show                       Show current project (roadmaps, subProjects, tasks)
  status                     Quick summary of all task statuses
  list-projects              List all projects with task counts
  add-roadmap <name> <desc>  Add a roadmap to active project
  add-subproject <name> <desc> Add a sub-project to a roadmap
  add-task <roadmap> <sp> <title> <desc> Add a task to a sub-project
  update-task <task_id> <field> <value> Update a task field
  move-task <task_id> <status> Move task to a new status
  assign-task <task_id> <agent_id> Assign a task to an agent
  list-agents                List all agents and their current assignments
  search <query>             Search tasks by title or description
"""

import sys
import json
import os
import time
import uuid

# ── Paths ─────────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
PROJECTS_FILE = os.path.join(HSCC_DIR, "projects.json")


# ── Helpers ───────────────────────────────────────────────────────────────

def ensure_state():
    """Ensure the projects.json file exists with valid structure."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    if not os.path.exists(PROJECTS_FILE):
        data = {
            "projects": [],
            "activeProjectId": ""
        }
        save_state(data)
        return data
    try:
        with open(PROJECTS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"projects": [], "activeProjectId": ""}


def save_state(data):
    """Save state to projects.json."""
    os.makedirs(HSCC_DIR, exist_ok=True)
    with open(PROJECTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def find_active_project(data):
    """Find the active project or the first available project."""
    active_id = data.get("activeProjectId", "")
    if active_id:
        for proj in data.get("projects", []):
            if proj["id"] == active_id:
                return proj, None
    if data.get("projects"):
        return data["projects"][0], None
    return None, "No projects found. Create one with 'hscc-projects create'."


def find_task(data, roadmap_name, subproject_name, task_title):
    """Find a task by roadmap, subproject, and title."""
    proj, err = find_active_project(data)
    if not proj:
        return None, err
    for rm in proj.get("roadmaps", []):
        if rm["name"] == roadmap_name:
            for sp in rm.get("subProjects", []):
                if sp["name"] == subproject_name:
                    for t in sp.get("tasks", []):
                        if t["title"] == task_title:
                            return t, None
    return None, "Task not found"


# ── Commands ──────────────────────────────────────────────────────────────

def cmd_list():
    """List all projects and active project."""
    data = ensure_state()
    active_proj = None
    if data.get("activeProjectId"):
        for proj in data.get("projects", []):
            if proj["id"] == data["activeProjectId"]:
                active_proj = proj
                break
    if not active_proj and data.get("projects"):
        active_proj = data["projects"][0]
        if active_proj:
            data["activeProjectId"] = active_proj["id"]
            save_state(data)

    return {
        "total_projects": len(data.get("projects", [])),
        "active_project": active_proj,
        "projects": [{"id": p["id"], "name": p["name"]} for p in data.get("projects", [])]
    }


def cmd_create(name, description=""):
    """Create a new project."""
    data = ensure_state()
    project = {
        "id": str(uuid.uuid4()).upper(),
        "name": name,
        "description": description,
        "roadmaps": [],
        "createdAt": time.time()
    }
    data["projects"].append(project)
    data["activeProjectId"] = project["id"]
    save_state(data)
    return {"success": True, "project": project}


def cmd_show():
    """Show current project with full details."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    total_tasks = sum(
        len(sp.get("tasks", []))
        for rm in proj.get("roadmaps", [])
        for sp in rm.get("subProjects", [])
    )

    return {
        "project": proj,
        "totalTasks": total_tasks
    }


def cmd_status():
    """Quick summary of all task statuses."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    status_counts = {"backlog": 0, "inProgress": 0, "done": 0, "cancelled": 0, "review": 0}
    total = 0

    for rm in proj.get("roadmaps", []):
        for sp in rm.get("subProjects", []):
            for t in sp.get("tasks", []):
                status = t.get("status", "backlog")
                if status in status_counts:
                    status_counts[status] += 1
                else:
                    status_counts[status] = status_counts.get(status, 0) + 1
                total += 1

    return {
        "project": proj["name"],
        "totalTasks": total,
        "byStatus": status_counts
    }


def cmd_list_projects():
    """List all projects with task counts."""
    data = ensure_state()
    result = []
    for proj in data.get("projects", []):
        total = sum(
            len(sp.get("tasks", []))
            for rm in proj.get("roadmaps", [])
            for sp in rm.get("subProjects", [])
        )
        result.append({
            "id": proj["id"],
            "name": proj["name"],
            "description": proj.get("description", ""),
            "totalTasks": total,
            "roadmaps": len(proj.get("roadmaps", []))
        })
    return {"projects": result}


def cmd_add_roadmap(name, description=""):
    """Add a roadmap to active project."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    roadmap = {
        "id": str(uuid.uuid4()).upper(),
        "name": name,
        "description": description,
        "subProjects": []
    }
    proj["roadmaps"].append(roadmap)
    save_state(data)
    return {"success": True, "roadmap": roadmap}


def cmd_add_subproject(roadmap_name, name, description=""):
    """Add a sub-project to a roadmap."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    for rm in proj.get("roadmaps", []):
        if rm["name"] == roadmap_name:
            subproject = {
                "id": str(uuid.uuid4()).upper(),
                "name": name,
                "description": description,
                "tasks": []
            }
            rm["subProjects"].append(subproject)
            save_state(data)
            return {"success": True, "subProject": subproject}
    return {"error": f"Roadmap '{roadmap_name}' not found"}


def cmd_add_task(roadmap_name, subproject_name, title, description=""):
    """Add a task to a sub-project."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    for rm in proj.get("roadmaps", []):
        if rm["name"] == roadmap_name:
            for sp in rm.get("subProjects", []):
                if sp["name"] == subproject_name:
                    task = {
                        "id": str(uuid.uuid4()).upper(),
                        "title": title,
                        "description": description,
                        "status": "backlog",
                        "priority": "medium",
                        "assignedAgent": "",
                        "labels": [],
                        "createdAt": time.time(),
                        "updatedAt": time.time(),
                        "output": "",
                        "artifacts": []
                    }
                    sp["tasks"].append(task)
                    save_state(data)
                    return {"success": True, "task": task}
            return {"error": f"Sub-project '{subproject_name}' not found in roadmap '{roadmap_name}'"}
    return {"error": f"Roadmap '{roadmap_name}' not found"}


def cmd_update_task(task_id, field, value):
    """Update a task field."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    valid_fields = ("status", "priority", "assignedAgent", "labels", "title", "description", "output")
    if field not in valid_fields:
        return {"error": f"Invalid field: {field}. Valid: {', '.join(valid_fields)}"}

    for rm in proj.get("roadmaps", []):
        for sp in rm.get("subProjects", []):
            for t in sp.get("tasks", []):
                if t["id"] == task_id:
                    if field == "labels":
                        try:
                            t["labels"] = json.loads(value)
                        except json.JSONDecodeError:
                            t["labels"] = [v.strip() for v in value.split(",")]
                    else:
                        t[field] = value
                    t["updatedAt"] = time.time()
                    save_state(data)
                    return {"success": True, "task": t}
    return {"error": f"Task '{task_id}' not found"}


def cmd_move_task(task_id, new_status):
    """Move task to a new status."""
    return cmd_update_task(task_id, "status", new_status)


def cmd_assign_task(task_id, agent_id):
    """Assign a task to an agent."""
    return cmd_update_task(task_id, "assignedAgent", agent_id)


def cmd_list_agents():
    """List all agents and their current assignments."""
    agents_file = os.path.expanduser("~/.r2d2cc/agents.json")
    try:
        with open(agents_file, "r") as f:
            agents_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"error": "agents.json not found"}

    result = []
    for agent in agents_data.get("agents", []):
        assignment = None
        data = ensure_state()
        for proj in data.get("projects", []):
            for rm in proj.get("roadmaps", []):
                for sp in rm.get("subProjects", []):
                    for t in sp.get("tasks", []):
                        if t.get("assignedAgent") == agent["id"] and t.get("status") != "done":
                            assignment = {
                                "task": t["title"],
                                "roadmap": rm["name"],
                                "subProject": sp["name"],
                                "status": t["status"],
                                "priority": t.get("priority", "medium")
                            }
                            break
                if assignment:
                    break
            if assignment:
                break
        result.append({
            "id": agent["id"],
            "name": agent["name"],
            "role": agent["role"],
            "status": agent.get("status", "unknown"),
            "temperature": agent.get("temperature"),
            "currentAssignment": assignment
        })
    return {"agents": result}


def cmd_search(query):
    """Search tasks by title or description."""
    data = ensure_state()
    proj, err = find_active_project(data)
    if not proj:
        return {"error": err}

    results = []
    query_lower = query.lower()
    for rm in proj.get("roadmaps", []):
        for sp in rm.get("subProjects", []):
            for t in sp.get("tasks", []):
                if query_lower in t["title"].lower() or query_lower in t.get("description", "").lower():
                    results.append({
                        "roadmap": rm["name"],
                        "subProject": sp["name"],
                        "task": {
                            "id": t["id"],
                            "title": t["title"],
                            "description": t.get("description", ""),
                            "status": t["status"],
                            "assignedAgent": t.get("assignedAgent", "")
                        }
                    })
    return {"query": query, "results": results, "count": len(results)}


# ── Command Map ───────────────────────────────────────────────────────────

COMMANDS = {
    "list": cmd_list,
    "create": cmd_create,
    "show": cmd_show,
    "status": cmd_status,
    "list-projects": cmd_list_projects,
    "add-roadmap": cmd_add_roadmap,
    "add-subproject": cmd_add_subproject,
    "add-task": cmd_add_task,
    "update-task": cmd_update_task,
    "move-task": cmd_move_task,
    "assign-task": cmd_assign_task,
    "list-agents": cmd_list_agents,
    "search": cmd_search,
}


# ── Entry Point ───────────────────────────────────────────────────────────

USAGE = """
Hermes Spark Cluster Control — Project & Kanban Management

Usage: hscc-projects <command> [args]

Commands:
  list                       List all projects and active project
  create <name> <desc>       Create a new project
  show                       Show current project (roadmaps, subProjects, tasks)
  status                     Quick summary of all task statuses
  list-projects              List all projects with task counts
  add-roadmap <name> <desc>  Add a roadmap to active project
  add-subproject <name> <desc> Add a sub-project to a roadmap
  add-task <roadmap> <sp> <title> <desc> Add a task to a sub-project
  update-task <id> <field> <value> Update a task field
  move-task <id> <status>    Move task to new status (backlog/inProgress/done/cancelled)
  assign-task <id> <agent>   Assign task to an agent
  list-agents                List all agents and their current assignments
  search <query>             Search tasks by title or description
""".strip()


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    fn = COMMANDS[cmd]

    try:
        if cmd == "create":
            if len(sys.argv) < 3:
                print("Usage: hscc-projects create <name> [description]")
                sys.exit(1)
            name = sys.argv[2]
            desc = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
            result = fn(name, desc)
        elif cmd == "add-roadmap":
            if len(sys.argv) < 3:
                print("Usage: hscc-projects add-roadmap <name> [description]")
                sys.exit(1)
            name = sys.argv[2]
            desc = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
            result = fn(name, desc)
        elif cmd == "add-subproject":
            if len(sys.argv) < 4:
                print("Usage: hscc-projects add-subproject <roadmap> <name> [description]")
                sys.exit(1)
            roadmap = sys.argv[2]
            name = sys.argv[3]
            desc = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
            result = fn(roadmap, name, desc)
        elif cmd == "add-task":
            if len(sys.argv) < 5:
                print("Usage: hscc-projects add-task <roadmap> <subproject> <title> [description]")
                sys.exit(1)
            roadmap = sys.argv[2]
            sp = sys.argv[3]
            title = sys.argv[4]
            desc = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""
            result = fn(roadmap, sp, title, desc)
        elif cmd == "update-task":
            if len(sys.argv) < 4:
                print("Usage: hscc-projects update-task <task_id> <field> <value>")
                sys.exit(1)
            task_id = sys.argv[2]
            field = sys.argv[3]
            value = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
            result = fn(task_id, field, value)
        elif cmd == "move-task":
            if len(sys.argv) < 3:
                print("Usage: hscc-projects move-task <task_id> <status>")
                sys.exit(1)
            task_id = sys.argv[2]
            status = sys.argv[3]
            result = fn(task_id, status)
        elif cmd == "assign-task":
            if len(sys.argv) < 3:
                print("Usage: hscc-projects assign-task <task_id> <agent_id>")
                sys.exit(1)
            task_id = sys.argv[2]
            agent_id = sys.argv[3]
            result = fn(task_id, agent_id)
        elif cmd == "search":
            if len(sys.argv) < 3:
                print("Usage: hscc-projects search <query>")
                sys.exit(1)
            query = " ".join(sys.argv[2:])
            result = fn(query)
        else:
            result = fn()

        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
