#!/usr/bin/env python3
"""
HSCC Model Idle Monitor Daemon

Background service that periodically scans running sparkrun containers
and shuts down any model that has been idle (no active agent) for > 30 min.

Usage:
  python3 hscc-model-idle-monitor.py           # One-shot scan
  python3 hscc-model-idle-monitor.py --daemon   # Run continuously (default 5 min interval)

Strategy:
  1. List all running sparkrun containers (sparkrun status)
  2. For each container, extract host IP from model reference
  3. Check if any agent references this model/container
  4. If an agent references it AND is idle > 30 min → stop the container
  5. If NO agent references it → stop the container (orphan)
  6. Exception: anything on the gateway node (244) is never auto-stopped —
     it runs the always-on orchestrator vLLM (Telegram + all Hermes agents)

Environment:
  HSCC_GATEWAY_NODE          - Gateway IP, always protected (default: 192.0.2.10)

Environment:
  HSCC_IDLE_TIMEOUT_MINUTES  - Idle timeout (default: 30)
  HSCC_SCAN_INTERVAL         - Daemon interval in minutes (default: 5)
"""

import sys
import json
import os
import time
import subprocess
import re
from datetime import datetime, timezone, timedelta

HSCC_DIR = os.path.expanduser("~/.hscc")
AGENTS_JSON = os.path.join(HSCC_DIR, "agents.json")
LIFECYCLE_JSON = os.path.join(HSCC_DIR, "lifecycle.json")
IDLE_TIMEOUT = int(os.environ.get("HSCC_IDLE_TIMEOUT_MINUTES", "30"))
SCAN_INTERVAL = int(os.environ.get("HSCC_SCAN_INTERVAL", "5"))
# Persistent state between scans — prevents killing containers whose agents
# just transitioned from idle→running (stale updated_at issue).
prev_agent_states = {}
# Gateway node runs the always-on orchestrator vLLM (serves Telegram + all Hermes agents). It has no agent row, so it must be protected explicitly or the
# Hermes agent). It has no agent row, so it must be protected explicitly or the
# orphan rule reaps it every scan and the daemon thrashes restarting it.
GATEWAY_NODE = os.environ.get("HSCC_GATEWAY_NODE", "192.0.2.10")


def now_utc():
    return datetime.now(timezone.utc)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_agents():
    data = read_json(AGENTS_JSON, {"agents": []})
    return data.get("agents", [])


def load_lifecycle():
    return read_json(LIFECYCLE_JSON, {"agents": {}})


def get_sparkrun_containers():
    """
    Parse sparkrun status output to get running containers.
    Returns list of: {"id": str, "host": str, "recipe": str, "container_name": str, "container_id": str}
    
    Output format:
      Job: @registry/recipe  (tp=1)  [abc123]  (1 container(s))
        solo       192.0.2.10                           Up 12 hours               sparkrun-eugr-vllm
    """
    try:
        result = subprocess.run(
            ["sparkrun", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []

        containers = []
        current_container_id = None
        current_recipe = None
        current_mode = None

        for line in result.stdout.split("\n"):
            line_stripped = line.strip()
            # Match container ID: [abc123def456] on Job line
            id_match = re.search(r"\[([a-f0-9]+)\]", line_stripped)
            if id_match:
                current_container_id = id_match.group(1)
                # Extract recipe from Job: line
                job_match = re.search(r"Job:\s+(@\S+/\S+)", line_stripped)
                if job_match:
                    current_recipe = job_match.group(1)
                continue

            # Match host line: solo 192.0.2.12 Up 2 minutes
            host_match = re.match(
                r"^\s*(solo|multi)\s+(\d+\.\d+\.\d+\.\d+)\s+Up\s+.*$", line_stripped
            )
            if host_match:
                mode = host_match.group(1)
                host_ip = host_match.group(2)
                if current_container_id:
                    # Container name is the last token on the host line
                    name_parts = line_stripped.split()
                    container_name = name_parts[-1] if name_parts else ""
                    containers.append({
                        "id": current_container_id,       # sparkrun job ID (12-char hex)
                        "container_id": current_container_id, # same — used for sparkrun stop
                        "host": host_ip,
                        "mode": mode,
                        "recipe": current_recipe or "unknown",
                        "docker_name": container_name,
                    })
                current_container_id = None
                current_recipe = None
                current_mode = mode

        return containers
    except Exception:
        return []


def get_agent_models():
    """
    Build a map of model identifiers -> agents.
    Format: vllm-192.0.2.XXX/Recipe -> [agent_ids]
    """
    agents = load_agents()
    model_to_agents = {}
    for agent in agents:
        model = agent.get("model", "")
        if model:
            if model not in model_to_agents:
                model_to_agents[model] = []
            model_to_agents[model].append(agent["id"])
    return model_to_agents


def get_agent_states():
    """Get map of agent_id -> lifecycle state."""
    lc = load_lifecycle()
    return lc.get("agents", {})


def parse_container_model(container, model_to_agents):
    """
    Determine which model/recipe a container is running.
    Match container to agents by host IP.
    """
    host = container["host"]
    matched_agents = []

    for model_str, agent_ids in model_to_agents.items():
        # model format: vllm-192.0.2.XXX/Recipe/Name
        ip_match = re.search(r"vllm-(\d+\.\d+\.\d+\.\d+)", model_str)
        if ip_match and ip_match.group(1) == host:
            matched_agents.extend(agent_ids)

    return matched_agents


def get_container_last_used(container_id):
    """
    Try to determine when a container was last used.
    We'll use the lifecycle history or a simple heuristic.
    """
    lc = load_lifecycle()
    history = lc.get("history", [])

    last_assignment = None
    for entry in reversed(history):
        cid = entry.get("sparkrun_id") or entry.get("container_id")
        if cid and container_id in cid:
            return datetime.fromisoformat(entry.get("timestamp", ""))

    return None


def check_container_idle(container, agents, agent_states, prev_agent_states=None):
    """
    Determine if a container should be shut down.
    Returns: {"shutdown": True/False, "reason": "..."}

    prev_agent_states: dict of agent_id → previous "state" from last scan.
    Used to detect state transitions so we don't kill containers whose agents
    just moved from idle→running (stale updated_at would give false-positive
    idle duration).
    """
    host = container["host"]
    recipe = container.get("recipe", "")
    container_id = container["container_id"]

    # Never auto-stop anything on the gateway node — it runs the orchestrator
    # vLLM (Telegram + all Hermes agents) which has no agent row and would
    # otherwise be reaped as an orphan, thrashing against the daemon's restart.
    if host == GATEWAY_NODE:
        return {"shutdown": False, "reason": "gateway orchestrator (protected)"}

    # If container has no associated agents → orphan → stop it
    if not agents:
        return {
            "shutdown": True,
            "reason": "no agents reference this container (orphan)",
            "container_id": container_id,
            "host": host,
            "recipe": recipe,
        }

    # Check if any agent referencing this container is actively running
    has_running_agent = False
    has_idle_agent = False
    oldest_idle_time = None
    fresh_transition = False  # agent was previously active, just went idle

    for agent_id in agents:
        state_entry = agent_states.get(agent_id, {})
        state = state_entry.get("state", "idle")
        prev_state = prev_agent_states.get(agent_id, None) if prev_agent_states else None

        if state == "running":
            has_running_agent = True
            break
        elif state == "idle":
            has_idle_agent = True
            # Detect fresh transition: agent was previously active, just went idle
            # The updated_at timestamp is stale from when it was last running
            if prev_state and prev_state in ("running", "spawning", "ready"):
                fresh_transition = True
            updated = state_entry.get("updated_at", "")
            if updated:
                try:
                    idle_time = datetime.fromisoformat(updated)
                    if oldest_idle_time is None or idle_time < oldest_idle_time:
                        oldest_idle_time = idle_time
                except:
                    pass
        elif state in ("spawning", "ready"):
            # Still provisioning, don't touch
            return {"shutdown": False, "reason": f"agent {agent_id} is in '{state}' state"}

    # If any agent is actively running → keep container
    if has_running_agent:
        return {"shutdown": False, "reason": "agent is actively running"}

    # Guard: if all agents just transitioned from active → idle, skip the idle check.
    # The updated_at timestamp is stale from when the agent was last running,
    # not when it just went idle. Killing now would terminate a container
    # immediately after a task completed.
    if fresh_transition and has_running_agent is False and has_idle_agent is True:
        return {
            "shutdown": False,
            "reason": "agents just transitioned from active to idle (stale updated_at)",
            "container_id": container_id,
            "host": host,
        }

    # If idle agent(s) exist, check timeout
    if has_idle_agent and oldest_idle_time:
        idle_duration = now_utc() - oldest_idle_time
        if idle_duration < timedelta(minutes=IDLE_TIMEOUT):
            remaining = IDLE_TIMEOUT - idle_duration.total_seconds() / 60
            return {
                "shutdown": False,
                "reason": f"agent idle {idle_duration.total_seconds()/60:.0f}min (timeout: {IDLE_TIMEOUT}min, {remaining:.0f}min remaining)",
                "container_id": container_id,
                "host": host,
                "idle_minutes": idle_duration.total_seconds() / 60,
            }
        return {
            "shutdown": True,
            "reason": f"agent idle for {idle_duration.total_seconds()/60:.0f} minutes (exceeded {IDLE_TIMEOUT}min threshold)",
            "container_id": container_id,
            "host": host,
            "recipe": recipe,
            "idle_minutes": idle_duration.total_seconds() / 60,
        }

    # Fallback: no clear state info, safe to keep
    return {"shutdown": False, "reason": "cannot determine agent state"}


def stop_container(container_id):
    """Stop a sparkrun container. Returns (ok, message)."""
    try:
        result = subprocess.run(
            ["sparkrun", "stop", container_id],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or result.stdout.strip()
    except Exception as e:
        return False, str(e)


def run_scan(dry_run=False, prev_agent_states=None):
    """
    Perform one idle-container scan.
    Returns summary dict with 'prev_states' key for caller to persist.
    """
    if prev_agent_states is None:
        prev_agent_states = {}
    result = {
        "timestamp": now_utc().isoformat(),
        "containers_scanned": 0,
        "kept": [],
        "stopped": [],
        "errors": [],
    }

    containers = get_sparkrun_containers()
    model_to_agents = get_agent_models()
    agent_states = get_agent_states()

    result["containers_scanned"] = len(containers)

    if not containers:
        result["message"] = "No running sparkrun containers found"
        return result

    # Track which agents are associated with kept containers for next scan
    current_states = {}

    for container in containers:
        matched_agents = parse_container_model(container, model_to_agents)
        check = check_container_idle(container, matched_agents, agent_states, prev_agent_states)

        if check.get("shutdown"):
            result["stopped"].append({
                "container": container,
                "check": check,
            })
            if not dry_run:
                ok, msg = stop_container(container["container_id"])
                result["stopped"][-1]["stopped"] = ok
                result["stopped"][-1]["stop_message"] = msg
                if not ok:
                    result["errors"].append(f"Failed to stop {container['container_id']}: {msg}")
        else:
            result["kept"].append({
                "container": container,
                "reason": check.get("reason", "unknown"),
            })
            # Track agents on kept containers for next scan
            for aid in matched_agents:
                state_entry = agent_states.get(aid, {})
                current_states[aid] = state_entry.get("state", "idle")

    # Persist state for next scan
    result["prev_states"] = current_states
    return result


def main():
    import argparse

    parser = argparse.ArgumentParser(description="HSCC Model Idle Monitor")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually stop containers")
    args = parser.parse_args()

    if args.daemon:
        scan_num = 0
        print(f"Starting HSCC idle monitor daemon (interval: {SCAN_INTERVAL}min, timeout: {IDLE_TIMEOUT}min)")
        while True:
            try:
                scan_num += 1
                result = run_scan(dry_run=args.dry_run, prev_agent_states=prev_agent_states)
                if result.get("prev_states"):
                    prev_agent_states.clear()
                    prev_agent_states.update(result["prev_states"])
                print(f"[{now_utc().strftime('%H:%M:%S')}] Scan #{scan_num}: {result['containers_scanned']} containers, "
                      f"{len(result['kept'])} kept, {len(result['stopped'])} stopped")
                for s in result["stopped"]:
                    reason = s["check"]["reason"]
                    status = "✓ stopped" if s.get("stopped") else "✗ failed"
                    print(f"  {status} {s['container']['container_id']} on {s['container']['host']}: {reason[:80]}")
                for e in result["errors"]:
                    print(f"  ERROR: {e}")
            except Exception as e:
                print(f"  ERROR: {e}")
            time.sleep(SCAN_INTERVAL * 60)
    else:
        result = run_scan(dry_run=args.dry_run, prev_agent_states=prev_agent_states)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
