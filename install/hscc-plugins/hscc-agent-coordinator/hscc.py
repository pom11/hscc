#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) - Agent Coordinator Plugin

Merges hscc-lifecycle (4 tools), hscc-worktrees (8 tools), and hscc-recovery (3 tools)
into a single unified plugin with commands:
  assign-task      Assign a task to an agent with FSM guards
  list-agents      List all agents with lifecycle state summary
  update-task      Move an agent to a new lifecycle state (with validation)
  move-task        Move a task between agents or reassign
  detect-orphans   Detect working agents with no corresponding sparkrun container
  attempt-recovery Diagnose and auto-recover failed agents
  recovery-log     View immutable recovery ledger
  list-worktrees   List active git worktrees for agent tasks

State files:
  Lifecycle: ~/.hscc/lifecycle.json
  Worktrees: ~/.hscc/worktrees.json
  Recovery:  ~/.hscc/recovery.json
  Events:    ~/.hscc/events.jsonl

Data sources:
  Agents:    ~/.hscc/agents.json
  Projects:  ~/.hscc/projects.json

FSM transitions (VALID_TRANSITIONS):
  idle     -> spawning, running, finished, failed, disabled
  spawning -> ready, running, failed, idle, disabled
  ready    -> ready, running, failed, idle, disabled
  running  -> finished, failed, idle, disabled
  finished -> idle, spawning, running, disabled
  failed   -> idle, spawning, disabled
  disabled -> idle

Guard rules:
  - max_running: Maximum concurrent running agents (default 3)
  - no_duplicate_task: Reject if another agent already has this task_id running

Usage: hscc-agent-coordinator <command> [args]
"""

import sys
import json
import os
import subprocess
import re
from datetime import datetime, timezone
from collections import Counter

# ---- Constants ----

HSCC_DIR = os.path.expanduser(os.environ.get("HSCC_HOME", "~/.hscc"))

AGENTS_JSON = os.path.join(HSCC_DIR, "agents.json")
PROJECTS_JSON = os.path.join(HSCC_DIR, "projects.json")
EVENTS_FILE = os.path.join(HSCC_DIR, "events.jsonl")
LIFECYCLE_FILE = os.path.join(HSCC_DIR, "lifecycle.json")
WORKTREES_FILE = os.path.join(HSCC_DIR, "worktrees.json")
RECOVERY_FILE = os.path.join(HSCC_DIR, "recovery.json")

# FSM transitions - same as hscc-lifecycle shared/types.ts
VALID_TRANSITIONS = {
    "idle": ["spawning", "ready", "running", "finished", "failed", "disabled"],
    "spawning": ["ready", "running", "failed", "idle", "disabled"],
    "ready": ["ready", "running", "failed", "idle", "disabled"],
    "running": ["finished", "failed", "idle", "disabled"],
    "finished": ["idle", "spawning", "running", "disabled"],
    "failed": ["idle", "spawning", "disabled"],
    "disabled": ["idle"],
}

# Resolve "finished" -> "idle" internally
FINISHED_REDIRECT = "finished"
EFFECTIVE_REDIRECT = "idle"

# Failure recipes - same as hscc-recovery
FAILURE_RECIPES = {
    "session_create_failed": {
        "description": "Agent cannot create a session with the gateway",
        "can_auto_recover": True,
        "steps": ["Check gateway health at localhost:18789", "Retry session creation"],
    },
    "model_not_loaded": {
        "description": "vLLM is not serving any model",
        "can_auto_recover": True,
        "steps": ["Check vLLM health", "Attempt to serve default recipe"],
    },
    "mcp_unavailable": {
        "description": "MCP server is not responding",
        "can_auto_recover": True,
        "steps": ["Check cluster health", "Verify MCP endpoint reachable", "Wait and retry"],
    },
    "session_timeout": {
        "description": "Agent session exceeded timeout",
        "can_auto_recover": True,
        "steps": ["Check task progress", "Extend or restart session"],
    },
    "provider_error": {
        "description": "vLLM returned 500 or connection refused",
        "can_auto_recover": True,
        "steps": ["Check cluster health", "Wait 10 seconds", "Retry health check"],
    },
    "task_rejected": {
        "description": "Agent refused the assigned task",
        "can_auto_recover": False,
        "steps": ["Escalate to orchestrator - manual intervention required"],
    },
}

MAX_RETRIES = 2
MAX_RUNNING = 3
MAX_HISTORY = 500


# ---- Helpers ----

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


def load_agents_list():
    if not os.path.exists(AGENTS_JSON):
        return []
    try:
        with open(AGENTS_JSON) as f:
            data = json.load(f)
        return data.get("agents", [])
    except (json.JSONDecodeError, IOError):
        return []


def load_agent_by_id(agent_id):
    for a in load_agents_list():
        if a.get("id") == agent_id:
            return a
    return None


def get_lifecycle(agent_id):
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    # Fallback to hscc state files
    if not lc.get("agents"):
        old_path = os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json")
        lc = read_json_file(old_path, {"agents": {}, "history": []})
    agent_lc = lc.get("agents", {}).get(agent_id, {"state": "idle", "updated_at": now_iso()})
    return agent_lc


def set_lifecycle(agent_id, state, **kwargs):
    """Update the lifecycle state for an agent. Returns the full agent_lc dict."""
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    agents_dict = lc.get("agents", {})
    current = agents_dict.get(agent_id, {"state": "idle", "updated_at": now_iso()})
    now = now_iso()
    new_entry = {"state": state, "updated_at": now}
    new_entry.update(kwargs)
    agents_dict[agent_id] = new_entry
    lc["agents"] = agents_dict

    # Record history
    record = {
        "agent_id": agent_id,
        "from": current.get("state", "idle"),
        "to": state,
        "timestamp": now,
    }
    record.update(kwargs)
    history = lc.get("history", [])
    history.append(record)
    if len(history) > MAX_HISTORY:
        lc["history"] = history[-MAX_HISTORY:]
    else:
        lc["history"] = history

    write_json_file(LIFECYCLE_FILE, lc)
    return new_entry


def emit_event(source, event_type, payload, severity="info"):
    """Append a JSON line to the events log."""
    ensure_dir()
    try:
        if not os.path.exists(EVENTS_FILE):
            with open(EVENTS_FILE, "w") as f:
                f.write("")
    except IOError:
        pass
    event = {
        "id": f"{source}.{event_type}.{datetime.now(timezone.utc).timestamp()}",
        "event_type": event_type,
        "timestamp": now_iso(),
        "severity": severity,
        "source": source,
        "payload": payload,
    }
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event) + "\n")
    except IOError:
        pass
    return event


def run_git(args, cwd=None, timeout=30):
    """Run a git command and return (ok, output)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            output = output or result.stderr.strip()
        return result.returncode == 0, output or ""
    except subprocess.TimeoutExpired:
        return False, f"git timed out after {timeout}s"
    except FileNotFoundError:
        return False, "git not found"
    except Exception as e:
        return False, str(e)


def run_shell(cmd, cwd=None, timeout=120):
    """Run a shell command safely and return (ok, output)."""
    parts = cmd.split()
    if not parts:
        return False, "empty command"
    try:
        result = subprocess.run(
            parts,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        combined = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, combined[:2000]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, "command not found or timed out"
    except Exception as e:
        return False, str(e)[:2000]


def get_running_agent_count():
    """Count agents currently in 'running' state."""
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}})
    return sum(1 for a in lc.get("agents", {}).values() if a.get("state") == "running")


def get_task_assignments():
    """Get a mapping of task_id -> agent_id for all inProgress tasks."""
    if not os.path.exists(PROJECTS_JSON):
        return {}
    try:
        with open(PROJECTS_JSON) as f:
            data = json.load(f)
        assignments = {}
        for project in data.get("projects", []):
            for roadmap in project.get("roadmaps", []):
                for sub in roadmap.get("subProjects", []):
                    for task in sub.get("tasks", []):
                        aid = task.get("assignedAgent", "").strip()
                        tid = task.get("id", "")
                        status = task.get("status", "")
                        if aid and status == "inProgress":
                            assignments[tid] = aid
        return assignments
    except (json.JSONDecodeError, IOError):
        return {}


def find_project_for_task(task_id):
    """Find the project containing a task. Returns (project, roadmap, subProject, task)."""
    if not os.path.exists(PROJECTS_JSON):
        return None, None, None, None
    try:
        with open(PROJECTS_JSON) as f:
            data = json.load(f)
        for project in data.get("projects", []):
            for roadmap in project.get("roadmaps", []):
                for sub in roadmap.get("subProjects", []):
                    for task in sub.get("tasks", []):
                        if task.get("id") == task_id:
                            return project, roadmap, sub, task
        return None, None, None, None
    except (json.JSONDecodeError, IOError):
        return None, None, None, None


def sanitize_branch_name(name):
    """Sanitize a string into a valid git branch name."""
    return re.sub(r"[~^:?*\[\]@{}\\.\s]+", "-", name).strip("-")


def get_sparkrun_containers():
    """List currently running sparkrun/docker containers."""
    # Try sparkrun cluster-monitor snapshot
    try:
        result = subprocess.run(
            ["sparkrun", "cluster-monitor", "snapshot"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            snap = json.loads(result.stdout)
            containers = []
            for entry in snap.get("allWorkloads", snap.get("apiWorkloads", [])):
                containers.append({
                    "name": entry.get("name", ""),
                    "state": entry.get("state", "unknown"),
                    "recipe": entry.get("recipe", {}).get("name", ""),
                    "model": entry.get("model", ""),
                    "node": entry.get("node", ""),
                })
            return containers
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    # Fallback: check docker ps
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        containers = []
        for line in result.stdout.strip().split("\n"):
            if "\t" in line:
                name, status = line.split("\t", 1)
                containers.append({"name": name, "state": status})
        return containers
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def get_agent_container_name(agent_id):
    """Derive expected container name for an agent."""
    return f"sparkrun-agent-{agent_id}"


# ---- Worktree helpers ----

def get_worktrees():
    """Load worktree state from worktrees.json."""
    return read_json_file(WORKTREES_FILE, {"worktrees": {}})


def save_worktrees(data):
    """Save worktree state."""
    write_json_file(WORKTREES_FILE, data)


def list_git_worktrees(repo_path):
    """List actual git worktrees for a repo."""
    if not os.path.exists(repo_path):
        return []
    ok, output = run_git(["worktree", "list", "--porcelain"], cwd=repo_path, timeout=10)
    result = []
    if ok and output:
        current_repo = None
        current_path = None
        current_branch = None
        lines = output.split("\n")
        for line in lines:
            if line.startswith("repo "):
                current_repo = line[5:].strip()
            elif line.startswith("worktree "):
                current_path = line[9:].strip()
            elif line.startswith("HEAD "):
                current_branch = line[5:].strip()
            elif line == "":
                if current_path:
                    result.append({
                        "repo": current_repo or repo_path,
                        "path": current_path,
                        "branch": current_branch or "unknown",
                    })
                current_repo = None
                current_path = None
                current_branch = None
    return result


def task_to_key(project_id, task_id):
    """Generate a unique key for a task's worktree."""
    return f"{project_id}/{task_id}"


def task_key_to_parts(key):
    """Parse a worktree key back into project_id and task_id."""
    parts = key.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return key, ""


# ---- Command implementations ----


def cmd_assign_task():
    """
    Assign a task to an agent with full FSM validation and guard checks.

    Validates:
    1. Agent exists and is enabled
    2. Agent lifecycle FSM transition is valid (idle -> spawning -> running)
    3. Max-running guard (max 3 concurrent running agents)
    4. Duplicate-task guard (no other agent has this task_id running)
    5. Auto-creates git worktree
    6. Updates project task assignment
    7. Records history and emits events
    """
    if len(sys.argv) < 4:
        print(json.dumps({
            "error": "Usage: hscc-agent-coordinator assign-task <agent_id> <task_id> [project_id] [branch_slug]"
        }))
        return

    agent_id = sys.argv[2]
    task_id = sys.argv[3]
    project_id = sys.argv[4] if len(sys.argv) > 4 else None
    branch_slug = sys.argv[5] if len(sys.argv) > 5 else None

    # 1. Validate agent exists
    agent = load_agent_by_id(agent_id)
    if agent is None:
        available = [a["id"] for a in load_agents_list()]
        print(json.dumps({
            "error": f"Agent not found: {agent_id}",
            "available_agents": available,
        }))
        return

    if not agent.get("enabled", True):
        print(json.dumps({"error": f"Agent {agent_id} is disabled"}))
        return

    # 2. Check lifecycle state
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    # Fallback to hscc state files
    if not lc.get("agents"):
        old_path = os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json")
        lc = read_json_file(old_path, {"agents": {}, "history": []})
    current = lc.get("agents", {}).get(agent_id, {"state": "idle"})
    current_state = current.get("state", "idle")

    # 3. Max-running guard
    if current_state != "idle" and current_state != "spawning":
        print(json.dumps({
            "error": f"Agent {agent_id} is in state '{current_state}', not eligible for new assignment",
            "current_state": current_state,
        }))
        return

    running_count = get_running_agent_count()
    if running_count >= MAX_RUNNING:
        print(json.dumps({
            "error": f"Max running agents ({MAX_RUNNING}) reached. Cannot assign to {agent_id}",
            "running_count": running_count,
            "max_running": MAX_RUNNING,
        }))
        return

    # 4. Duplicate-task guard
    assignments = get_task_assignments()
    running_by_task = {}
    for tid, aid in assignments.items():
        task_lc = lc.get("agents", {}).get(aid, {})
        if task_lc.get("state") == "running":
            running_by_task[tid] = aid
    if task_id in running_by_task and running_by_task[task_id] != agent_id:
        print(json.dumps({
            "error": f"Task {task_id} is already assigned and running under agent {running_by_task[task_id]}",
            "task_id": task_id,
            "assigned_agent": running_by_task[task_id],
        }))
        return

    # 5. Transition: idle -> spawning -> running
    now = now_iso()

    # First transition: idle -> spawning
    if current_state == "idle":
        set_lifecycle(agent_id, "spawning", task_id=task_id)
        emit_event("hscc-agent-coordinator", "agent.state_changed", {
            "agent_id": agent_id,
            "from": "idle",
            "to": "spawning",
            "task_id": task_id,
        })
        print(json.dumps({
            "step": "spawning",
            "agent_id": agent_id,
            "task_id": task_id,
            "message": f"Agent {agent_id} transitioned: idle -> spawning",
        }))

    # Check if spawning succeeded — re-read from disk; the in-memory `lc`
    # snapshot is stale after set_lifecycle() wrote the new state.
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    if not lc.get("agents"):
        lc = read_json_file(os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json"),
                            {"agents": {}, "history": []})
    current = lc.get("agents", {}).get(agent_id, {"state": "idle"})
    current_state = current.get("state", "idle")

    if current_state == "spawning":
        # Second transition: spawning -> ready -> running (or skipping straight to running)
        allowed = VALID_TRANSITIONS.get(current_state, [])
        if "running" in allowed:
            new_entry = set_lifecycle(agent_id, "running", task_id=task_id)
            emit_event("hscc-agent-coordinator", "agent.state_changed", {
                "agent_id": agent_id,
                "from": current_state,
                "to": "running",
                "task_id": task_id,
            })
            emit_event("hscc-agent-coordinator", "agent.task_started", {
                "agent_id": agent_id,
                "task_id": task_id,
            }, "info")

            # 6. Auto-create git worktree
            worktree_info = None
            project_for_task = None
            if project_id:
                worktree_info = create_worktree_for_task(agent_id, task_id, project_id, branch_slug)
            elif task_id:
                # Try to find the project from task_id
                p, rm, sp, t = find_project_for_task(task_id)
                if p:
                    project_id = p.get("id", "")
                    project_for_task = p
                    worktree_info = create_worktree_for_task(agent_id, task_id, project_id, branch_slug)

            # 7. Update projects.json task assignment
            mark_task_in_progress(task_id, agent_id, project_id)

            result = {
                "success": True,
                "agent_id": agent_id,
                "previous_state": current_state,
                "new_state": "running",
                "task_id": task_id,
                "running_count": running_count + 1,
            }
            if worktree_info:
                result["worktree"] = worktree_info
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({
                "error": f"Cannot transition {current_state} -> running",
                "allowed": allowed,
            }))
    else:
        # Already past spawning, or in a different state
        result = {
            "status": "skipped_to_running",
            "agent_id": agent_id,
            "task_id": task_id,
            "previous_state": current_state,
            "message": f"Agent already in state '{current_state}', proceeding with task assignment",
        }
        mark_task_in_progress(task_id, agent_id, project_id)
        print(json.dumps(result, indent=2))


def create_worktree_for_task(agent_id, task_id, project_id, branch_slug=None):
    """Create a git worktree for an agent task. Returns worktree info or None."""
    # Find the project's git repo path
    p, rm, sp, t = find_project_for_task(task_id)
    if not p:
        return {"note": "No project found for task, skipping worktree creation"}

    # Resolve the project's git repo via the canonical resolver so a project
    # whose gitRepoPath points elsewhere is honored (never fall back to the
    # HSCC state repo).
    repo_path = get_project_repo(project_id)
    if not repo_path:
        return {"note": "No git repo found for project, skipping worktree creation"}

    # Determine worktree path. Use the full task id (UUID) so two tasks that
    # share an 8-char prefix never collide on the same directory.
    worktree_dir = os.path.join(HSCC_DIR, "worktrees", project_id, f"{agent_id}-{task_id}")

    # Sanitize branch name
    slug = branch_slug or task_id
    branch = f"task/{sanitize_branch_name(task_id)}-{sanitize_branch_name(slug)}"

    # Create parent dirs
    parent_dir = os.path.dirname(worktree_dir)
    os.makedirs(parent_dir, exist_ok=True)

    # Reuse an existing active worktree (idempotent dispatch / retry).
    if os.path.exists(worktree_dir):
        existing_wt = get_worktrees()
        key = task_to_key(project_id, task_id)
        existing = existing_wt.get("worktrees", {}).get(key, {})
        if existing.get("status") == "active":
            return {
                "note": "Worktree already exists",
                "worktree_path": worktree_dir,
                "branch": existing.get("branch") or branch,
                "base_commit": _worktree_base(worktree_dir) or "unknown",
                "status": "exists",
            }

    # Create worktree. If the branch already exists (e.g. a prior dispatch was
    # removed but its branch was left behind), check it out instead of trying to
    # create it again — `worktree add -b` would fail with "branch already exists".
    branch_exists, _ = run_git(["rev-parse", "--verify", "--quiet", branch], cwd=repo_path, timeout=5)
    if branch_exists:
        add_args = ["worktree", "add", worktree_dir, branch]
    else:
        add_args = ["worktree", "add", worktree_dir, "-b", branch, "HEAD"]
    ok, output = run_git(add_args, cwd=repo_path, timeout=30)
    if not ok:
        return {
            "error": f"Failed to create worktree: {output}",
            "worktree_path": worktree_dir,
            "branch": branch,
            "collision": True,
        }

    # Save worktree state
    wt_state = get_worktrees()
    key = task_to_key(project_id, task_id)
    wt_state["worktrees"][key] = {
        "project_id": project_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "branch": branch,
        "path": worktree_dir,
        "status": "active",
        "created_at": now_iso(),
    }
    save_worktrees(wt_state)

    # Write .claw-base with current HEAD
    head_ok, head_sha = run_git(["rev-parse", "HEAD"], cwd=repo_path, timeout=5)
    if head_ok:
        try:
            with open(os.path.join(worktree_dir, ".claw-base"), "w") as f:
                f.write(head_sha)
        except IOError:
            pass

    emit_event("hscc-agent-coordinator", "worktree.created", {
        "project_id": project_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "branch": branch,
        "path": worktree_dir,
    })

    return {
        "worktree_path": worktree_dir,
        "branch": branch,
        "base_commit": head_sha if head_ok else "unknown",
    }


def mark_task_in_progress(task_id, agent_id, project_id=None):
    """Update a task's status to inProgress in projects.json."""
    if not os.path.exists(PROJECTS_JSON):
        return
    try:
        with open(PROJECTS_JSON) as f:
            data = json.load(f)
        updated = False
        for project in data.get("projects", []):
            for roadmap in project.get("roadmaps", []):
                for sub in roadmap.get("subProjects", []):
                    for task in sub.get("tasks", []):
                        if task.get("id") == task_id:
                            task["status"] = "inProgress"
                            task["assignedAgent"] = agent_id
                            task["updatedAt"] = datetime.now(timezone.utc).timestamp()
                            updated = True
                            break
                    if updated:
                        break
                if updated:
                    break
            if updated:
                break
        if updated:
            write_json_file(PROJECTS_JSON, data)
    except (json.JSONDecodeError, IOError):
        pass


def cmd_list_agents():
    """
    List all agents with their lifecycle state, task assignments, and worktree info.

    Shows: agent_id, name, role, status, lifecycle_state, task, worktree, enabled
    """
    agents = load_agents_list()
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    # Fallback to hscc state files
    if not lc.get("agents"):
        old_path = os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json")
        lc = read_json_file(old_path, {"agents": {}, "history": []})
    assignments = get_task_assignments()
    wt_state = get_worktrees()
    worktrees = wt_state.get("worktrees", {})

    # Group by state
    by_state = {}
    total = len(agents)
    for a in agents:
        aid = a.get("id", "?")
        lc_entry = lc.get("agents", {}).get(aid, {})
        state = lc_entry.get("state", "unknown")
        if state not in by_state:
            by_state[state] = 0
        by_state[state] += 1

    # Summary
    enabled = sum(1 for a in agents if a.get("enabled", True))
    print(f"Agent Coordinator: {total} agents | "
          f"Enabled: {enabled} | "
          f"Running: {get_running_agent_count()}/{MAX_RUNNING} | "
          f"States: {dict(by_state)}")
    print()

    # Detail
    for a in agents:
        aid = a.get("id", "?")
        name = a.get("name", "?")
        role = a.get("role", "?")
        status = a.get("status", "?")
        enabled_str = "✓" if a.get("enabled", True) else "✗"

        lc_entry = lc.get("agents", {}).get(aid, {})
        lc_state = lc_entry.get("state", "unknown")
        updated = lc_entry.get("updated_at", "")[:19] if lc_entry else "N/A"

        task_id = assignments.get(aid, "")
        wt_info = None
        for key, wt in worktrees.items():
            if wt.get("agent_id") == aid and wt.get("status") == "active":
                wt_info = wt
                break

        # Build detail line
        detail = f"  [{enabled_str}] {aid:12s} {name:12s} role={role:12s} "
        detail += f"status={status:10s} lc={lc_state:12s} "
        if updated and updated != "N/A":
            detail += f"updated={updated}"
        else:
            detail += "updated=N/A"

        if task_id:
            detail += f" task={task_id[:8]}..."
        if wt_info:
            detail += f" worktree={wt_info.get('branch', '?')}"

        print(detail)

        # Show transition history for this agent (last 5)
        history = [h for h in lc.get("history", []) if h.get("agent_id") == aid]
        recent = history[-5:]
        if recent:
            transitions = " -> ".join(
                f"{h.get('from','?')}->{h.get('to','?')}"
                for h in recent
            )
            print(f"           last transitions: {transitions}")
            if recent[-1].get("failure_kind"):
                print(f"           failure: {recent[-1].get('failure_kind')}")

    print()
    print(f"Worktrees: {len([w for w in worktrees.values() if w.get('status') == 'active'])} active")


def cmd_update_task():
    """
    Update an agent's lifecycle state with full FSM validation.

    Usage: hscc-agent-coordinator update-task <agent_id> <target_state> [failure_kind] [task_id]

    Args:
        agent_id: Agent identifier
        target_state: Target state (idle, spawning, ready, running, finished, failed, disabled)
        failure_kind: Required when target is 'failed' (e.g., session_create_failed, model_not_loaded)
        task_id: Required when target is 'running'
    """
    if len(sys.argv) < 4:
        print(json.dumps({
            "error": "Usage: hscc-agent-coordinator update-task <agent_id> <target_state> [failure_kind] [task_id]"
        }))
        return

    agent_id = sys.argv[2]
    target_state = sys.argv[3]
    # Parse optional args based on target_state requirements
    failure_kind = None
    task_id = None
    if len(sys.argv) > 4:
        if target_state == "failed":
            failure_kind = sys.argv[4]
            if len(sys.argv) > 5:
                task_id = sys.argv[5]
        elif target_state == "running":
            task_id = sys.argv[4]
        else:
            # For other states, treat arg4 as failure_kind (may be ignored)
            failure_kind = sys.argv[4]
    if len(sys.argv) > 5 and target_state != "failed":
        task_id = sys.argv[5]

    # Validate agent exists
    agent = load_agent_by_id(agent_id)
    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    # Get current state
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    # Fallback to hscc state files
    if not lc.get("agents"):
        old_path = os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json")
        lc = read_json_file(old_path, {"agents": {}, "history": []})
    current = lc.get("agents", {}).get(agent_id, {"state": "idle"})
    current_state = current.get("state", "idle")

    # Validate transition
    allowed = VALID_TRANSITIONS.get(current_state, [])
    if target_state not in allowed:
        print(json.dumps({
            "error": f"Invalid transition: {current_state} -> {target_state}",
            "current_state": current_state,
            "allowed_transitions": allowed,
        }))
        return

    # Validate failure_kind is required for failed state
    if target_state == "failed" and not failure_kind:
        known_kinds = list(FAILURE_RECIPES.keys())
        print(json.dumps({
            "error": "failure_kind is required when transitioning to 'failed'",
            "known_failure_kinds": known_kinds,
        }))
        return

    # Validate task_id is required for running state
    if target_state == "running" and not task_id:
        print(json.dumps({
            "error": "task_id is required when transitioning to 'running'",
        }))
        return

    # Determine effective state (finished -> idle redirect)
    effective_state = EFFECTIVE_REDIRECT if target_state == FINISHED_REDIRECT else target_state

    # Apply transition
    kwargs = {}
    if target_state == "failed" and failure_kind:
        kwargs["failure_kind"] = failure_kind
    if target_state == "running" and task_id:
        kwargs["task_id"] = task_id

    new_entry = set_lifecycle(agent_id, effective_state, **kwargs)

    # Emit events
    emit_event("hscc-agent-coordinator", "agent.state_changed", {
        "agent_id": agent_id,
        "from": current_state,
        "to": effective_state,
        "task_id": kwargs.get("task_id"),
    })

    if effective_state == "failed":
        emit_event("hscc-agent-coordinator", "agent.failed", {
            "agent_id": agent_id,
            "failure_kind": failure_kind,
        }, "warning")

    if effective_state == "running" and task_id:
        emit_event("hscc-agent-coordinator", "agent.task_started", {
            "agent_id": agent_id,
            "task_id": task_id,
        })

    # If transitioning to idle, clear task assignment in projects.json
    if effective_state == "idle" and kwargs.get("task_id"):
        clear_task_assignment(kwargs["task_id"])

    result = {
        "agent_id": agent_id,
        "previous_state": current_state,
        "new_state": effective_state,
        "requested_state": target_state,
    }
    if target_state != effective_state:
        result["redirected"] = f"'{FINISHED_REDIRECT}' redirected to '{EFFECTIVE_REDIRECT}'"
    if failure_kind:
        result["failure_kind"] = failure_kind
    if task_id:
        result["task_id"] = task_id

    print(json.dumps(result, indent=2))


def clear_task_assignment(task_id):
    """Clear a task's assignedAgent from projects.json."""
    if not os.path.exists(PROJECTS_JSON):
        return
    try:
        with open(PROJECTS_JSON) as f:
            data = json.load(f)
        for project in data.get("projects", []):
            for roadmap in project.get("roadmaps", []):
                for sub in roadmap.get("subProjects", []):
                    for task in sub.get("tasks", []):
                        if task.get("id") == task_id:
                            task["assignedAgent"] = ""
                            task["status"] = "backlog"
                            task["updatedAt"] = datetime.now(timezone.utc).timestamp()
        write_json_file(PROJECTS_JSON, data)
    except (json.JSONDecodeError, IOError):
        pass


def cmd_move_task():
    """
    Move a task between agents or reassign a task.

    Usage: hscc-agent-coordinator move-task <task_id> <from_agent> <to_agent>

    Steps:
    1. Validate source agent is running the task
    2. Transition source agent to idle
    3. Transition target agent to running with the task
    4. Update projects.json assignment
    """
    if len(sys.argv) < 5:
        print(json.dumps({
            "error": "Usage: hscc-agent-coordinator move-task <task_id> <from_agent> <to_agent>"
        }))
        return

    task_id = sys.argv[2]
    from_agent = sys.argv[3]
    to_agent = sys.argv[4]

    # Validate agents exist
    from_a = load_agent_by_id(from_agent)
    if from_a is None:
        print(json.dumps({"error": f"Source agent not found: {from_agent}"}))
        return

    to_a = load_agent_by_id(to_agent)
    if to_a is None:
        print(json.dumps({"error": f"Target agent not found: {to_agent}"}))
        return

    if not to_a.get("enabled", True):
        print(json.dumps({"error": f"Target agent {to_agent} is disabled"}))
        return

    # Check current state of source agent
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    # Fallback to hscc state files
    if not lc.get("agents"):
        old_path = os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json")
        lc = read_json_file(old_path, {"agents": {}, "history": []})
    from_state = lc.get("agents", {}).get(from_agent, {"state": "idle"})
    if from_state.get("state") != "running":
        print(json.dumps({
            "error": f"Source agent {from_agent} is not in 'running' state (currently: {from_state.get('state')})",
        }))
        return

    # Check max-running guard on target
    running_count = get_running_agent_count()
    if running_count >= MAX_RUNNING:
        print(json.dumps({
            "error": f"Max running agents ({MAX_RUNNING}) reached",
            "running_count": running_count,
        }))
        return

    # Check target is idle or spawning
    to_state = lc.get("agents", {}).get(to_agent, {"state": "idle"})
    if to_state.get("state") not in ("idle", "spawning"):
        print(json.dumps({
            "error": f"Target agent {to_agent} is in state '{to_state.get('state')}', not eligible",
        }))
        return

    now = now_iso()
    actions = []

    # Transition source agent to idle
    set_lifecycle(from_agent, "idle")
    actions.append({
        "action": "source_reset",
        "agent": from_agent,
        "from": from_state.get("state"),
        "to": "idle",
    })
    emit_event("hscc-agent-coordinator", "agent.state_changed", {
        "agent_id": from_agent,
        "from": from_state.get("state"),
        "to": "idle",
        "task_id": task_id,
    })

    # Clean up source agent's worktree if exists
    wt_state = get_worktrees()
    for key, wt in list(wt_state.get("worktrees", {}).items()):
        if wt.get("agent_id") == from_agent and wt.get("status") == "active":
            wt["status"] = "released"
            wt["released_at"] = now
            wt["released_to_agent"] = to_agent
            actions.append({
                "action": "worktree_released",
                "worktree": key,
                "agent": from_agent,
            })

    # Transition target agent to running
    set_lifecycle(to_agent, "running", task_id=task_id)
    actions.append({
        "action": "target_start",
        "agent": to_agent,
        "from": to_state.get("state"),
        "to": "running",
        "task_id": task_id,
    })
    emit_event("hscc-agent-coordinator", "agent.state_changed", {
        "agent_id": to_agent,
        "from": to_state.get("state"),
        "to": "running",
        "task_id": task_id,
    })
    emit_event("hscc-agent-coordinator", "agent.task_started", {
        "agent_id": to_agent,
        "task_id": task_id,
    })

    # Update projects.json
    mark_task_in_progress(task_id, to_agent)
    actions.append({
        "action": "project_updated",
        "task_id": task_id,
        "from": from_agent,
        "to": to_agent,
    })

    print(json.dumps({
        "success": True,
        "task_id": task_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "running_count": running_count,
        "actions": actions,
    }, indent=2))


def cmd_detect_orphans():
    """
    Detect orphan agents: agents in working states (running, spawning, ready)
    with no corresponding sparkrun container.

    These are agents that Hermes is working on but have no sparkrun container backing them.
    Orphans are reset to idle state.

    Usage: hscc-agent-coordinator detect-orphans [--force-reset]
    """
    force_reset = "--force-reset" in sys.argv

    agents = load_agents_list()
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
    # Fallback to hscc state files
    if not lc.get("agents"):
        old_path = os.path.join(HSCC_DIR, "plugin-state", "hscc-lifecycle.json")
        lc = read_json_file(old_path, {"agents": {}, "history": []})

    # Get list of actual running containers
    containers = get_sparkrun_containers()
    container_names = {c.get("name", "") for c in containers}

    # Also check docker containers
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for name in result.stdout.strip().split("\n"):
                if name:
                    container_names.add(name)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    working_states = {"running", "spawning", "ready", "working"}
    orphans = []
    findings = []

    for a in agents:
        aid = a.get("id", "?")
        lc_entry = lc.get("agents", {}).get(aid, {})
        state = lc_entry.get("state", "idle")

        if state not in working_states:
            continue

        # Check if there's a matching container
        expected_name = get_agent_container_name(aid)
        has_container = any(
            expected_name in name or aid in name
            for name in container_names
        )

        if not has_container:
            task_id = lc_entry.get("task_id", "")
            found_task = task_id or ""
            findings.append({
                "agent_id": aid,
                "state": state,
                "expected_container": expected_name,
                "task_id": found_task,
                "has_container": False,
            })
            orphans.append(aid)

    result = {
        "total_agents_checked": len(agents),
        "working_agents": len(findings),
        "orphans_detected": len(orphans),
        "details": findings,
    }

    if orphans and force_reset:
        reset_actions = []
        for aid in orphans:
            old_state = lc.get("agents", {}).get(aid, {}).get("state", "unknown")
            set_lifecycle(aid, "idle")
            reset_actions.append({
                "agent_id": aid,
                "was": old_state,
                "now": "idle",
                "reason": "orphan_detected_no_container",
            })
        result["forced_reset"] = True
        result["reset_actions"] = reset_actions
        result["message"] = f"Reset {len(reset_actions)} orphan agents to idle"

    print(json.dumps(result, indent=2))


def cmd_attempt_recovery():
    """
    Diagnose and auto-recover a failed agent.

    Steps:
    1. Diagnose the failure kind
    2. Check health endpoints (gateway, vLLM)
    3. Apply recovery recipe
    4. If recovered, transition agent back to idle

    Usage: hscc-agent-coordinator attempt-recovery <agent_id> [failure_kind]

    Recovery outcomes:
      - recovered: health checks pass, agent is reset to idle
      - partial_recovery: some health checks pass, retry needed
      - escalation_required: all health checks failed, max retries exceeded
    """
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: hscc-agent-coordinator attempt-recovery <agent_id> [failure_kind]"
        }))
        return

    agent_id = sys.argv[2]
    failure_kind = sys.argv[3] if len(sys.argv) > 3 else None

    # Validate agent exists
    agent = load_agent_by_id(agent_id)
    if agent is None:
        print(json.dumps({"error": f"Agent not found: {agent_id}"}))
        return

    # Get failure kind from lifecycle if not specified
    if not failure_kind:
        lc = read_json_file(LIFECYCLE_FILE, {"agents": {}, "history": []})
        lc_entry = lc.get("agents", {}).get(agent_id, {})
        failure_kind = lc_entry.get("failure_kind", "unknown")

    # Get recipe
    recipe = FAILURE_RECIPES.get(failure_kind)
    if not recipe:
        print(json.dumps({
            "error": f"Unknown failure kind: {failure_kind}",
            "known_kinds": list(FAILURE_RECIPES.keys()),
        }))
        return

    # Load recovery history
    recovery = read_json_file(RECOVERY_FILE, {"history": [], "attempts": {}})
    attempts = recovery.get("attempts", {})
    agent_attempts = attempts.get(agent_id, {}).get(failure_kind, {"count": 0, "last_outcome": ""})
    retry_count = agent_attempts.get("count", 0)

    # Check max retries
    if retry_count >= MAX_RETRIES and agent_attempts.get("last_outcome") != "recovered":
        emit_event("hscc-agent-coordinator", "recovery.escalated", {
            "agent_id": agent_id,
            "scenario": failure_kind,
            "reason": "max retries exceeded",
        }, "warning")
        print(json.dumps({
            "agent_id": agent_id,
            "scenario": failure_kind,
            "outcome": "escalation_required",
            "reason": f"Max retries ({MAX_RETRIES}) exceeded",
            "previous_attempts": retry_count,
        }, indent=2))
        return

    # Execute recovery steps (health checks)
    step_results = []
    resolved = False

    # Check gateway health
    gw_ok = False
    try:
        result = subprocess.run(
            ["curl", "-sf", "--max-time", "5", "http://localhost:18789/api/health"],
            capture_output=True, text=True, timeout=8,
        )
        gw_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    step_results.append(f"gateway_health: {'ok' if gw_ok else 'unreachable'}")
    resolved = resolved or gw_ok

    # Check vLLM health (common for most failure kinds)
    if failure_kind in ("model_not_loaded", "provider_error"):
        vllm_ok = False
        try:
            result = subprocess.run(
                ["curl", "-sf", "--max-time", "5", "http://localhost:8000/v1/models"],
                capture_output=True, text=True, timeout=8,
            )
            vllm_ok = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        step_results.append(f"inference_health: {'ok' if vllm_ok else 'unreachable'}")
        if vllm_ok:
            resolved = True

    # For session issues, gateway health is sufficient
    if failure_kind in ("session_create_failed", "session_timeout"):
        if gw_ok:
            resolved = True

    # Determine outcome
    if resolved:
        outcome = "recovered"
    elif any("ok" in s for s in step_results):
        outcome = "partial_recovery"
    else:
        outcome = "escalation_required"

    # Update recovery state
    now = now_iso()
    if outcome == "recovered":
        agent_attempts["count"] = 0
    else:
        agent_attempts["count"] = agent_attempts.get("count", 0) + 1
    agent_attempts["last_outcome"] = outcome
    agent_attempts["last_attempt"] = now
    attempts[agent_id] = attempts.get(agent_id, {})
    attempts[agent_id][failure_kind] = agent_attempts
    recovery["attempts"] = attempts

    # Immutable record
    attempt_record = {
        "id": f"rec-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{agent_id[:6]}",
        "agent_id": agent_id,
        "scenario": failure_kind,
        "outcome": outcome,
        "steps_taken": step_results,
        "timestamp": now,
        "attempt_number": agent_attempts["count"],
    }
    history = recovery.get("history", [])
    history.append(attempt_record)
    if len(history) > 100:
        recovery["history"] = history[-100:]
    else:
        recovery["history"] = history
    write_json_file(RECOVERY_FILE, recovery)

    # If recovered, transition agent back to idle
    if outcome == "recovered":
        set_lifecycle(agent_id, "idle")
        emit_event("hscc-agent-coordinator", "recovery.succeeded", {
            "agent_id": agent_id,
            "scenario": failure_kind,
            "steps_taken": step_results,
        })

    # If escalation required, emit warning
    if outcome == "escalation_required":
        emit_event("hscc-agent-coordinator", "recovery.failed", {
            "agent_id": agent_id,
            "scenario": failure_kind,
            "reason": "health checks did not resolve",
        }, "warning")

    result = {
        "agent_id": agent_id,
        "scenario": failure_kind,
        "description": recipe["description"],
        "outcome": outcome,
        "steps_taken": step_results,
        "attempts_used": agent_attempts["count"],
        "max_retries": MAX_RETRIES,
        "recommendation": recipe["steps"],
    }
    if outcome == "recovered":
        result["agent_reset_to_idle"] = True
    if not recipe.get("can_auto_recover", False):
        result["auto_recover_unavailable"] = True
        result["recommendation"] = ["Manual intervention required"]

    print(json.dumps(result, indent=2))


def cmd_recovery_log():
    """
    View the immutable recovery ledger.

    Shows recovery attempt history with outcomes, steps taken, and timestamps.
    Each record is immutable - records are appended but never modified.

    Usage: hscc-agent-coordinator recovery-log [agent_id] [limit]
    """
    agent_id = sys.argv[2] if len(sys.argv) > 2 else None
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 20

    recovery = read_json_file(RECOVERY_FILE, {"history": []})
    history = recovery.get("history", [])

    # Filter by agent if specified
    if agent_id:
        history = [h for h in history if h.get("agent_id") == agent_id]

    recent = history[-limit:]
    if not recent:
        print(json.dumps({"message": "No recovery history found."}, indent=2))
        return

    # Summary
    by_outcome = Counter(h.get("outcome", "unknown") for h in history)
    total = len(history)

    print(f"Recovery Ledger: {total} total records | Showing last {len(recent)}")
    print(f"Outcomes: {dict(by_outcome)}")
    print()

    for h in recent:
        ts = h.get("timestamp", "?")[:19]
        agent = h.get("agent_id", "?")
        scenario = h.get("scenario", "?")
        outcome = h.get("outcome", "?")
        attempt_num = h.get("attempt_number", "?")
        steps = h.get("steps_taken", [])
        record_id = h.get("id", "?")

        outcome_marker = {"recovered": "RECOVERED", "partial_recovery": "PARTIAL",
                          "escalation_required": "ESCALATED"}.get(outcome, outcome)

        print(f"  [{ts}] {record_id}")
        print(f"    agent={agent} scenario={scenario} attempt={attempt_num} outcome={outcome_marker}")
        if steps:
            for step in steps:
                print(f"      - {step}")
        print()

    # Show attempt tracking per agent
    attempts = recovery.get("attempts", {})
    if attempts:
        print("Attempt tracking:")
        for aid, scenarios in attempts.items():
            for kind, info in scenarios.items():
                count = info.get("count", 0)
                last = info.get("last_outcome", "?")
                print(f"  {aid}/{kind}: attempts={count} last={last}")


def cmd_list_worktrees():
    """
    List active git worktrees for agent tasks.

    Shows: project, task, agent, branch, path, status, git status summary.

    Usage: hscc-agent-coordinator list-worktrees [project_id]
    """
    project_id = sys.argv[2] if len(sys.argv) > 2 else None
    wt_state = get_worktrees()
    worktrees = wt_state.get("worktrees", {})

    # Filter by project if specified
    if project_id:
        worktrees = {k: v for k, v in worktrees.items() if v.get("project_id") == project_id}

    active = {k: v for k, v in worktrees.items() if v.get("status") in ("active", "merging", "merged")}
    all_count = len(worktrees)

    print(f"Worktrees: {all_count} total | {len(active)} with status")
    print()

    if not worktrees:
        print("No worktrees registered.")
        print()
        print("Git worktrees in repo:")
        # List actual git worktrees
        repo_path = os.path.expanduser("~/.hscc")
        if os.path.exists(os.path.join(repo_path, ".git")):
            git_wts = list_git_worktrees(repo_path)
            if git_wts:
                print(f"\n  In {repo_path}:")
                for wt in git_wts:
                    print(f"    {wt['path']}: branch={wt['branch']}")
        return

    for key, wt in sorted(worktrees.items()):
        pid = wt.get("project_id", "?")
        tid = wt.get("task_id", "?")[:8]
        aid = wt.get("agent_id", "?")
        branch = wt.get("branch", "?")
        path = wt.get("path", "?")
        status = wt.get("status", "?")
        created = wt.get("created_at", "?")[:19]

        # Try to get git status
        git_status = ""
        git_ok, git_out = run_git(["status", "--short"], cwd=path, timeout=5)
        if git_ok and git_out:
            lines = git_out.strip().split("\n")
            mod_count = sum(1 for l in lines if l.strip())
            if mod_count > 0:
                git_status = f" ({mod_count} files changed)"

        status_icon = {"active": "●", "merged": "✓", "merging": "...", "released": "○", "removed": "x"}.get(status, "?")

        print(f"  {status_icon} {key}")
        print(f"    project={pid} task={tid} agent={aid}")
        print(f"    branch={branch} status={status}")
        print(f"    path={path}{git_status}")
        print(f"    created={created}")
        print()


# ---- Executor bridge (HSCC task -> Hermes kanban worker) ----

BRIDGE_FILE = os.path.join(HSCC_DIR, "bridge.json")


def load_bridge():
    return read_json_file(BRIDGE_FILE, {"tasks": {}})


def save_bridge(data):
    write_json_file(BRIDGE_FILE, data)


def find_hermes_bin():
    """Locate the hermes CLI binary."""
    candidate = os.path.expanduser("~/.local/bin/hermes")
    if os.path.exists(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return "hermes"


def run_hermes(args, timeout=120):
    """Run a hermes CLI command. Returns (ok, output)."""
    try:
        result = subprocess.run(
            [find_hermes_bin()] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        out = (result.stdout or "").strip()
        if result.returncode != 0:
            out = out or (result.stderr or "").strip()
        return result.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"hermes timed out after {timeout}s"
    except FileNotFoundError:
        return False, "hermes not found"
    except Exception as e:
        return False, str(e)


def _load_projects():
    return read_json_file(PROJECTS_JSON, {"projects": [], "activeProjectId": ""})


def get_project_record(project_id):
    for p in _load_projects().get("projects", []):
        if p.get("id") == project_id:
            return p
    return None


def get_project_repo(project_id):
    """Resolve the git repo path for a project, or None if not provisioned."""
    p = get_project_record(project_id)
    if p and p.get("gitRepoPath"):
        rp = p["gitRepoPath"]
        if os.path.exists(os.path.join(rp, ".git")):
            return rp
    default_rp = os.path.join(HSCC_DIR, "projects", project_id)
    if os.path.exists(os.path.join(default_rp, ".git")):
        return default_rp
    return None


def get_project_board(project_id):
    """Resolve the kanban board slug for a project."""
    p = get_project_record(project_id)
    if p and p.get("boardSlug"):
        return p["boardSlug"]
    short = re.sub(r"[^a-z0-9]+", "-", str(project_id).lower())[:8].strip("-")
    return f"hscc-{short}"


def resolve_profile(agent_id, override=None):
    """Resolve the Hermes profile a kanban worker should run as."""
    if override:
        return override
    agent = load_agent_by_id(agent_id) if agent_id else None
    if agent and agent.get("profile"):
        return agent["profile"]
    return "default"


def ensure_board(board, repo_path):
    """Idempotently ensure a kanban board exists and is bound to repo_path."""
    run_hermes(["kanban", "boards", "create", board, "--switch"], timeout=30)
    if repo_path:
        run_hermes(["kanban", "boards", "set-default-workdir", board, repo_path], timeout=30)


def cmd_dispatch_task():
    """
    EXECUTOR (guarded): mirror an HSCC task into its project's kanban board as a
    BLOCKED worktree task. Pre-creates the git worktree so the worker's cwd lands
    in the isolated checkout. Nothing runs until 'release-task' unblocks it.

    Usage: hscc-agent-coordinator dispatch-task <task_id> [project_id] [profile]
    """
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: dispatch-task <task_id> [project_id] [profile]"}))
        return

    task_id = sys.argv[2]
    project_id = sys.argv[3] if len(sys.argv) > 3 else None
    profile_override = sys.argv[4] if len(sys.argv) > 4 else None

    p, rm, sp, task = find_project_for_task(task_id)
    if not p:
        print(json.dumps({"error": f"Task {task_id} not found in any project"}))
        return
    project_id = project_id or p.get("id", "")

    repo_path = get_project_repo(project_id)
    if not repo_path:
        print(json.dumps({
            "error": f"Project {project_id} has no git repo. Create the project via hscc-projects so it is provisioned.",
            "expected": os.path.join(HSCC_DIR, "projects", project_id),
        }))
        return

    agent_id = (task.get("assignedAgent") or "").strip() or "worker"
    profile = resolve_profile(task.get("assignedAgent"), profile_override)

    # Pre-create the worktree (reuses existing machinery). Derive a readable
    # branch slug from the task title.
    title_slug = sanitize_branch_name((task.get("title") or task.get("name") or "")[:40]) or None
    wt = create_worktree_for_task(agent_id, task_id, project_id, branch_slug=title_slug)
    if wt.get("error"):
        print(json.dumps({"error": "worktree creation failed", "detail": wt}))
        return
    wt_path = wt.get("worktree_path")
    branch = wt.get("branch")
    if not wt_path:
        # Defensive: recover path/branch from state if the helper returned a note.
        key = task_to_key(project_id, task_id)
        existing = get_worktrees().get("worktrees", {}).get(key, {})
        wt_path = existing.get("path")
        branch = branch or existing.get("branch")
    if not wt_path:
        print(json.dumps({"error": "could not resolve worktree path", "detail": wt}))
        return
    # The kanban worker only changes cwd into the workspace if it exists on disk.
    # Refuse to dispatch a worker that would land in a missing directory.
    if not os.path.isdir(wt_path):
        print(json.dumps({"error": "worktree path does not exist on disk; cannot dispatch", "path": wt_path}))
        return
    if not branch:
        print(json.dumps({"error": "could not resolve worktree branch", "detail": wt}))
        return

    board = get_project_board(project_id)
    ensure_board(board, repo_path)

    title = task.get("title") or task.get("name") or task_id
    body = task.get("description", "") or ""
    idem = f"hscc-{project_id}-{task_id}"

    create_args = ["kanban", "--board", board, "create", title]
    if body:
        create_args += ["--body", body]
    create_args += [
        "--assignee", profile,
        "--workspace", f"worktree:{wt_path}",
        "--initial-status", "blocked",
        "--idempotency-key", idem,
        "--json",
    ]
    if branch:
        create_args += ["--branch", branch]

    ok, out = run_hermes(create_args, timeout=60)
    if not ok:
        print(json.dumps({"error": "kanban create failed", "detail": out, "board": board}))
        return
    try:
        kanban_id = json.loads(out).get("id")
    except (json.JSONDecodeError, AttributeError):
        kanban_id = None
    if not kanban_id:
        print(json.dumps({"error": "could not parse kanban task id", "raw": out[:500]}))
        return

    bridge = load_bridge()
    bridge["tasks"][task_id] = {
        "hscc_task_id": task_id,
        "project_id": project_id,
        "agent_id": agent_id,
        "kanban_id": kanban_id,
        "board": board,
        "profile": profile,
        "worktree": wt_path,
        "branch": branch,
        "status": "blocked",
        "dispatched_at": now_iso(),
    }
    save_bridge(bridge)

    emit_event("hscc-agent-coordinator", "task.dispatched", {
        "task_id": task_id, "project_id": project_id,
        "kanban_id": kanban_id, "board": board, "worktree": wt_path,
    })

    print(json.dumps({
        "success": True,
        "guarded": True,
        "task_id": task_id,
        "kanban_id": kanban_id,
        "board": board,
        "profile": profile,
        "worktree": wt_path,
        "branch": branch,
        "status": "blocked",
        "next": f"hscc-agent-coordinator release-task {task_id}",
        "message": "Mirrored as a BLOCKED kanban task. Run release-task to dispatch a worker.",
    }, indent=2))


def cmd_release_task():
    """
    Guarded 'go': unblock the kanban mirror so the gateway dispatcher spawns a
    worker in the pre-created worktree. Marks the HSCC task inProgress.

    Usage: hscc-agent-coordinator release-task <task_id>
    """
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: release-task <task_id>"}))
        return
    task_id = sys.argv[2]
    bridge = load_bridge()
    entry = bridge.get("tasks", {}).get(task_id)
    if not entry:
        print(json.dumps({"error": f"No dispatch bridge for task {task_id}. Run dispatch-task first."}))
        return

    board = entry["board"]
    kanban_id = entry["kanban_id"]
    ok, out = run_hermes(["kanban", "--board", board, "unblock", kanban_id], timeout=30)
    if not ok:
        print(json.dumps({"error": "unblock failed", "detail": out}))
        return

    # Nudge the dispatcher for immediacy (gateway also runs it on an interval).
    run_hermes(["kanban", "--board", board, "dispatch"], timeout=60)

    entry["status"] = "released"
    entry["released_at"] = now_iso()
    save_bridge(bridge)
    # Record the real agent id (not the worker profile) as the task assignee.
    agent_id = entry.get("agent_id") or ""
    if not agent_id:
        _, _, _, t = find_project_for_task(task_id)
        agent_id = (t.get("assignedAgent") or "").strip() if t else ""
    mark_task_in_progress(task_id, agent_id, entry.get("project_id"))

    emit_event("hscc-agent-coordinator", "task.released", {
        "task_id": task_id, "kanban_id": kanban_id, "board": board,
    })
    print(json.dumps({
        "success": True, "task_id": task_id, "kanban_id": kanban_id,
        "board": board, "status": "released",
        "message": "Task unblocked; dispatcher will spawn a worker in the worktree.",
    }, indent=2))


def cmd_task_status():
    """
    Show the kanban status + worker log tail for a dispatched HSCC task.

    Usage: hscc-agent-coordinator task-status <task_id> [log_lines]
    """
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: task-status <task_id> [log_lines]"}))
        return
    task_id = sys.argv[2]
    log_lines = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 40
    entry = load_bridge().get("tasks", {}).get(task_id)
    if not entry:
        print(json.dumps({"error": f"No dispatch bridge for task {task_id}"}))
        return
    board, kanban_id = entry["board"], entry["kanban_id"]
    ok, show = run_hermes(["kanban", "--board", board, "show", kanban_id], timeout=30)
    _, log = run_hermes(["kanban", "--board", board, "log", kanban_id], timeout=30)
    log_tail = "\n".join(log.splitlines()[-log_lines:]) if log else ""
    print(json.dumps({
        "task_id": task_id, "kanban_id": kanban_id, "board": board,
        "bridge_status": entry.get("status"),
        "show": show, "log_tail": log_tail,
    }, indent=2, default=str))


def cmd_cancel_task():
    """
    Cancel a dispatched task: block the kanban mirror, reclaim any running worker,
    and return the agent to idle.

    Usage: hscc-agent-coordinator cancel-task <task_id>
    """
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: cancel-task <task_id>"}))
        return
    task_id = sys.argv[2]
    bridge = load_bridge()
    entry = bridge.get("tasks", {}).get(task_id)
    if not entry:
        print(json.dumps({"error": f"No dispatch bridge for task {task_id}"}))
        return
    board, kanban_id = entry["board"], entry["kanban_id"]
    run_hermes(["kanban", "--board", board, "reclaim", kanban_id], timeout=30)
    ok, out = run_hermes(["kanban", "--board", board, "block", kanban_id], timeout=30)
    entry["status"] = "cancelled"
    entry["cancelled_at"] = now_iso()
    save_bridge(bridge)

    # Return the assigned agent to idle if one is tracked.
    p, rm, sp, task = find_project_for_task(task_id)
    if task:
        aid = (task.get("assignedAgent") or "").strip()
        if aid and load_agent_by_id(aid):
            set_lifecycle(aid, "idle", task_id=task_id)
    emit_event("hscc-agent-coordinator", "task.cancelled", {
        "task_id": task_id, "kanban_id": kanban_id, "board": board,
    }, "warning")
    print(json.dumps({
        "success": ok, "task_id": task_id, "kanban_id": kanban_id,
        "status": "cancelled", "detail": out,
    }, indent=2))


def cmd_send_message():
    """
    Post a message to a dispatched task's kanban thread (inter-agent comms the
    worker can read).

    Usage: hscc-agent-coordinator send-message <task_id> <message...>
    """
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: send-message <task_id> <message...>"}))
        return
    task_id = sys.argv[2]
    message = " ".join(sys.argv[3:])
    entry = load_bridge().get("tasks", {}).get(task_id)
    if not entry:
        print(json.dumps({"error": f"No dispatch bridge for task {task_id}"}))
        return
    board, kanban_id = entry["board"], entry["kanban_id"]
    ok, out = run_hermes(["kanban", "--board", board, "comment", kanban_id, message], timeout=30)
    print(json.dumps({"success": ok, "task_id": task_id, "kanban_id": kanban_id, "detail": out}))


# ---- Worktree lifecycle ----

def _resolve_worktree(project_id, task_id):
    key = task_to_key(project_id, task_id)
    return key, get_worktrees().get("worktrees", {}).get(key, {})


def _worktree_base(wt_path):
    """Return the base commit recorded in .claw-base, or None."""
    base_file = os.path.join(wt_path, ".claw-base")
    try:
        with open(base_file) as f:
            return f.read().strip()
    except IOError:
        return None


def _changed_files(wt_path):
    """Files changed in a worktree relative to its base commit (committed + working)."""
    base = _worktree_base(wt_path)
    files = set()
    if base:
        ok, out = run_git(["diff", "--name-only", f"{base}", "HEAD"], cwd=wt_path, timeout=10)
        if ok and out:
            files.update(l.strip() for l in out.splitlines() if l.strip())
    ok2, out2 = run_git(["status", "--porcelain"], cwd=wt_path, timeout=10)
    if ok2 and out2:
        for line in out2.splitlines():
            name = line[3:].strip()
            if name:
                files.add(name)
    return files


def cmd_merge_worktree():
    """
    Merge a task's worktree branch back into the project repo's default branch.
    Reports conflicts without discarding work (no auto-abort).

    Usage: hscc-agent-coordinator merge-worktree <project_id> <task_id> [--no-ff]
    """
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: merge-worktree <project_id> <task_id> [--no-ff]"}))
        return
    project_id, task_id = sys.argv[2], sys.argv[3]
    no_ff = "--no-ff" in sys.argv[4:]
    key, wt = _resolve_worktree(project_id, task_id)
    if not wt:
        print(json.dumps({"error": f"No worktree for {key}"}))
        return
    repo_path = get_project_repo(project_id)
    if not repo_path:
        print(json.dumps({"error": f"No git repo for project {project_id}"}))
        return
    branch = wt.get("branch")
    merge_args = ["merge", "--no-edit"] + (["--no-ff"] if no_ff else []) + [branch]
    ok, out = run_git(merge_args, cwd=repo_path, timeout=60)
    if not ok:
        # Detect a conflict; leave it for manual resolution (do not abort).
        conflicted = "conflict" in out.lower() or "CONFLICT" in out
        print(json.dumps({
            "success": False,
            "conflict": conflicted,
            "branch": branch,
            "detail": out[:1000],
            "hint": "Resolve in the repo, or run 'git merge --abort' there manually." if conflicted else "",
        }, indent=2))
        return
    wt_state = get_worktrees()
    if key in wt_state.get("worktrees", {}):
        wt_state["worktrees"][key]["status"] = "merged"
        wt_state["worktrees"][key]["merged_at"] = now_iso()
        save_worktrees(wt_state)
    emit_event("hscc-agent-coordinator", "worktree.merged", {
        "project_id": project_id, "task_id": task_id, "branch": branch,
    })
    print(json.dumps({"success": True, "branch": branch, "detail": out[:500]}, indent=2))


def cmd_remove_worktree():
    """
    Remove a task's git worktree. Refuses if the worktree has uncommitted changes
    unless --force is given (safety guard against losing work).

    Usage: hscc-agent-coordinator remove-worktree <project_id> <task_id> [--force]
    """
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: remove-worktree <project_id> <task_id> [--force]"}))
        return
    project_id, task_id = sys.argv[2], sys.argv[3]
    force = "--force" in sys.argv[4:]
    key, wt = _resolve_worktree(project_id, task_id)
    if not wt:
        print(json.dumps({"error": f"No worktree for {key}"}))
        return
    wt_path = wt.get("path")
    branch = wt.get("branch")
    merged = wt.get("status") == "merged"
    repo_path = get_project_repo(project_id)
    if not repo_path:
        print(json.dumps({"error": f"No git repo for project {project_id}"}))
        return
    # Safety: refuse to drop uncommitted work unless forced. If we cannot verify
    # the worktree is clean (git status failed), treat that as unsafe too.
    if wt_path and os.path.isdir(wt_path) and not force:
        dirty_ok, dirty_out = run_git(["status", "--porcelain"], cwd=wt_path, timeout=10)
        if not dirty_ok:
            print(json.dumps({
                "error": "cannot verify worktree is clean; refusing to remove",
                "path": wt_path,
                "detail": dirty_out,
                "hint": "Pass --force to remove anyway (this may discard uncommitted work).",
            }, indent=2))
            return
        if dirty_out.strip():
            print(json.dumps({
                "error": "worktree has uncommitted changes; refusing to remove",
                "path": wt_path,
                "changes": dirty_out.strip().splitlines()[:20],
                "hint": "Pass --force to remove anyway (this discards uncommitted work).",
            }, indent=2))
            return
    rm_args = ["worktree", "remove"] + (["--force"] if force else []) + [wt_path]
    ok, out = run_git(rm_args, cwd=repo_path, timeout=30)
    if not ok and os.path.isdir(wt_path):
        print(json.dumps({"error": "git worktree remove failed", "detail": out}))
        return
    # Drop the branch only if it was merged, so re-dispatch starts clean and we
    # don't accumulate stale task/* branches. Never delete unmerged work.
    branch_removed = False
    if branch and merged:
        bok, _ = run_git(["branch", "-d", branch], cwd=repo_path, timeout=10)
        branch_removed = bok
    wt_state = get_worktrees()
    if key in wt_state.get("worktrees", {}):
        wt_state["worktrees"][key]["status"] = "removed"
        wt_state["worktrees"][key]["removed_at"] = now_iso()
        save_worktrees(wt_state)
    # Reap the dispatch bridge entry so it doesn't accumulate stale records.
    bridge = load_bridge()
    if task_id in bridge.get("tasks", {}):
        del bridge["tasks"][task_id]
        save_bridge(bridge)
    emit_event("hscc-agent-coordinator", "worktree.removed", {
        "project_id": project_id, "task_id": task_id, "path": wt_path,
    })
    print(json.dumps({
        "success": True, "removed": wt_path,
        "branch_removed": branch_removed, "detail": out,
    }, indent=2))


def cmd_check_collisions():
    """
    Detect files modified by more than one active worktree (potential merge
    collisions).

    Usage: hscc-agent-coordinator check-collisions [project_id]
    """
    project_id = sys.argv[2] if len(sys.argv) > 2 else None
    worktrees = get_worktrees().get("worktrees", {})
    if project_id:
        worktrees = {k: v for k, v in worktrees.items() if v.get("project_id") == project_id}
    active = {k: v for k, v in worktrees.items() if v.get("status") == "active"}

    file_map = {}
    for key, wt in active.items():
        path = wt.get("path", "")
        if not path or not os.path.exists(path):
            continue
        for f in _changed_files(path):
            file_map.setdefault(f, []).append(key)

    collisions = {f: keys for f, keys in file_map.items() if len(keys) > 1}
    print(json.dumps({
        "project_id": project_id,
        "active_worktrees": len(active),
        "collision_count": len(collisions),
        "collisions": collisions,
    }, indent=2))


def cmd_detect_stale():
    """
    Detect active worktrees that look abandoned: no commits beyond base and older
    than the threshold, or whose agent is finished/idle.

    Usage: hscc-agent-coordinator detect-stale [project_id] [--hours N]
    """
    args = sys.argv[2:]
    hours = 24
    project_id = None
    i = 0
    while i < len(args):
        if args[i] == "--hours" and i + 1 < len(args):
            try:
                hours = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            project_id = args[i]
            i += 1

    worktrees = get_worktrees().get("worktrees", {})
    if project_id:
        worktrees = {k: v for k, v in worktrees.items() if v.get("project_id") == project_id}
    active = {k: v for k, v in worktrees.items() if v.get("status") == "active"}
    lc = read_json_file(LIFECYCLE_FILE, {"agents": {}})

    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stale = []
    for key, wt in active.items():
        path = wt.get("path", "")
        reasons = []
        created = wt.get("created_at", "")
        try:
            created_dt = datetime.fromisoformat(created)
        except (ValueError, TypeError):
            created_dt = None
        base = _worktree_base(path) if path and os.path.exists(path) else None
        has_commits = False
        if path and os.path.exists(path) and base:
            ok, out = run_git(["rev-list", "--count", f"{base}..HEAD"], cwd=path, timeout=10)
            has_commits = ok and out.strip().isdigit() and int(out.strip()) > 0
        if created_dt and created_dt < cutoff and not has_commits:
            reasons.append(f"no commits and older than {hours}h")
        aid = wt.get("agent_id", "")
        astate = lc.get("agents", {}).get(aid, {}).get("state", "")
        if astate in ("finished", "idle", "failed"):
            reasons.append(f"agent state is '{astate}'")
        if reasons:
            stale.append({"key": key, "path": path, "agent_id": aid, "reasons": reasons})
    print(json.dumps({
        "project_id": project_id, "threshold_hours": hours,
        "active_worktrees": len(active), "stale_count": len(stale), "stale": stale,
    }, indent=2))


def cmd_green_check():
    """
    Run the project's verifier inside a task's worktree and report pass/fail.
    Detection order: explicit cmd after '--', ./verify.sh, make test, npm test,
    pytest.

    Usage: hscc-agent-coordinator green-check <project_id> <task_id> [-- cmd...]
    """
    if len(sys.argv) < 4:
        print(json.dumps({"error": "Usage: green-check <project_id> <task_id> [-- cmd...]"}))
        return
    project_id, task_id = sys.argv[2], sys.argv[3]
    key, wt = _resolve_worktree(project_id, task_id)
    if not wt:
        print(json.dumps({"error": f"No worktree for {key}"}))
        return
    path = wt.get("path")
    if not path or not os.path.exists(path):
        print(json.dumps({"error": f"Worktree path missing: {path}"}))
        return

    explicit = None
    if "--" in sys.argv:
        idx = sys.argv.index("--")
        explicit = sys.argv[idx + 1:]

    if explicit:
        cmd = explicit
        label = " ".join(explicit)
    elif os.path.exists(os.path.join(path, "verify.sh")):
        cmd = ["bash", "verify.sh"]
        label = "./verify.sh"
    elif os.path.exists(os.path.join(path, "Makefile")):
        cmd = ["make", "test"]
        label = "make test"
    elif os.path.exists(os.path.join(path, "package.json")):
        cmd = ["npm", "test"]
        label = "npm test"
    elif os.path.exists(os.path.join(path, "pytest.ini")) or os.path.exists(os.path.join(path, "pyproject.toml")) or os.path.isdir(os.path.join(path, "tests")):
        cmd = ["python3", "-m", "pytest", "-q"]
        label = "pytest"
    else:
        print(json.dumps({"green": None, "detail": "no verifier found in worktree"}))
        return

    try:
        result = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=600)
        passed = result.returncode == 0
        tail = ((result.stdout or "") + (result.stderr or "")).strip()[-2000:]
    except subprocess.TimeoutExpired:
        passed, tail = False, "verifier timed out after 600s"
    except FileNotFoundError as e:
        passed, tail = False, f"verifier not runnable: {e}"
    emit_event("hscc-agent-coordinator", "worktree.green_check", {
        "project_id": project_id, "task_id": task_id, "green": passed, "verifier": label,
    }, "info" if passed else "warning")
    print(json.dumps({"green": passed, "verifier": label, "output_tail": tail}, indent=2))


def cmd_list_dispatched():
    """List all HSCC tasks dispatched to kanban via the bridge."""
    bridge = load_bridge().get("tasks", {})
    print(json.dumps({"count": len(bridge), "tasks": bridge}, indent=2, default=str))


# ---- Main ----

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    commands = {
        "assign-task": cmd_assign_task,
        "list-agents": cmd_list_agents,
        "update-task": cmd_update_task,
        "move-task": cmd_move_task,
        "detect-orphans": cmd_detect_orphans,
        "attempt-recovery": cmd_attempt_recovery,
        "recovery-log": cmd_recovery_log,
        "list-worktrees": cmd_list_worktrees,
        # Executor bridge (HSCC task -> Hermes kanban worker)
        "dispatch-task": cmd_dispatch_task,
        "release-task": cmd_release_task,
        "task-status": cmd_task_status,
        "cancel-task": cmd_cancel_task,
        "send-message": cmd_send_message,
        "list-dispatched": cmd_list_dispatched,
        # Worktree lifecycle
        "merge-worktree": cmd_merge_worktree,
        "remove-worktree": cmd_remove_worktree,
        "check-collisions": cmd_check_collisions,
        "detect-stale": cmd_detect_stale,
        "green-check": cmd_green_check,
    }

    if cmd not in commands:
        print(json.dumps({
            "error": f"Unknown command: {cmd}",
            "available_commands": list(commands.keys()),
        }))
        sys.exit(1)

    commands[cmd]()


if __name__ == "__main__":
    main()
