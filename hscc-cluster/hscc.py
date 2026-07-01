#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Cluster Management Plugin

Usage: hscc-cluster <command> [args]

Commands:
  cluster-status   Show running workloads and idle hosts
  hosts            List all cluster hosts and saved clusters
  monitor          Single snapshot of CPU/RAM/GPU metrics
  jobs             List all sparkrun jobs running
  stop <id>        Stop a running workload by container ID
  info             Detailed cluster configuration
"""

import sys
import json
import subprocess
import os

# ── Template subcommand ────────────────────────────────────────────────────

def cmd_cluster_template():
    """Route cluster-template commands to the template engine."""
    from cluster_template_cli import cmd_cluster_template as _cmd
    return _cmd(sys.argv[2:])

# ── Constants ─────────────────────────────────────────────────────────────

SPARKRUN = "sparkrun"
CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")


# ── Helpers ───────────────────────────────────────────────────────────────

def run_cmd(args, timeout=30, as_json=False):
    """Run a command and return structured output."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout.strip()
        error = result.stderr.strip()
        rc = result.returncode
        resp = {"success": rc == 0, "returncode": rc, "output": output}
        if error:
            resp["error"] = error
        if as_json and output:
            try:
                resp["json"] = json.loads(output)
            except json.JSONDecodeError:
                pass
        return resp
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"command timed out after {timeout}s", "command": args}
    except FileNotFoundError:
        return {"success": False, "error": f"command not found: {args[0]}", "command": args}


def read_json_file(path):
    """Read and parse a JSON file, returning None on failure."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── Commands ──────────────────────────────────────────────────────────────

def cmd_cluster_status():
    """Show running workloads and idle hosts on the DGX Spark cluster."""
    result = run_cmd([SPARKRUN, "status"], timeout=15)
    output = result.get("output", "")
    
    workloads = []
    idle_hosts = []
    total_hosts = 0
    
    for line in output.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("Idle"):
            continue
        
        # Job line: "Job: @official/qwen3.6...  [1b6e77192e59]  (1 container(s))"
        if stripped.startswith("Job:"):
            parts = stripped.split()
            name = parts[1] if len(parts) > 1 else "?"
            # Extract tp/pp — handle "(tp=1," and "pp=1)" parsing
            tp = pp = "?"
            for p in parts:
                if "tp=" in p:
                    tp = p.strip("(),").split("=")[1]
                if "pp=" in p:
                    pp = p.strip("(),").split("=")[1]
            # Extract container ID
            container_id = "?"
            for p in parts:
                if p.startswith("[") and p.endswith("]"):
                    container_id = p.strip("[]")
                    break
            workloads.append({
                "name": name,
                "tp": tp,
                "pp": pp,
                "container_id": container_id,
            })
        # Solo host line: "  solo       192.0.2.10                           Up 26 minutes             sparkrun-eugr-vllm"
        elif "solo" in stripped and "." in stripped and ("Up " in stripped or "since" in stripped.lower()):
            parts = stripped.split()
            for i, p in enumerate(parts):
                if p == "solo" and i+1 < len(parts):
                    ip = parts[i+1]
                    total_hosts += 1
                    break
        # "  192.0.2.11" — check raw line for indentation
        elif line.startswith("  ") and stripped.count(".") >= 3 and not stripped.startswith(("logs:", "stop:", "solo")):
            idle_hosts.append(stripped)
            total_hosts += 1
        # Total line: "Total: 1 container(s) across 4 host(s)"
        elif stripped.startswith("Total:"):
            try:
                parts = stripped.split()
                for i, p in enumerate(parts):
                    if p.endswith("host(s)") and i-1 >= 0:
                        total_hosts = int(parts[i-1])
            except (ValueError, IndexError):
                pass
    
    if total_hosts == 0:
        total_hosts = len(workloads) + len(idle_hosts)
    
    return {
        "workloads": workloads,
        "idle_hosts": idle_hosts,
        "total_hosts": total_hosts,
        "raw_output": output,
    }


def cmd_hosts():
    """List all cluster hosts, saved clusters, and live status."""
    cluster_list = run_cmd([SPARKRUN, "cluster", "list"])
    status = run_cmd([SPARKRUN, "status"])

    hosts = []
    cluster_data = read_json_file(CLUSTER_JSON)
    if cluster_data:
        for host in cluster_data.get("workers", []):
            hosts.append({
                "id": host.get("id"),
                "name": host.get("name"),
                "ip": host.get("ip"),
                "role": host.get("role"),
                "ssh_user": host.get("sshUser"),
            })
        gateway = cluster_data.get("gateway", {})
        if gateway:
            hosts.append({
                "id": gateway.get("id"),
                "name": gateway.get("name"),
                "ip": gateway.get("ip"),
                "role": gateway.get("role"),
                "ssh_user": gateway.get("sshUser"),
            })
        for nas in cluster_data.get("nasDevices", []):
            hosts.append({
                "id": nas.get("id"),
                "name": nas.get("hostname"),
                "ip": nas.get("ip"),
                "role": "nas",
                "ssh_user": nas.get("sshUser"),
            })

    return {
        "saved_clusters": cluster_list,
        "live_status": status,
        "hosts": hosts,
    }


def cmd_monitor():
    """Single snapshot of CPU/RAM/GPU metrics across all nodes."""
    try:
        result = subprocess.run(
            "timeout 3 " + SPARKRUN + " cluster monitor --simple --json",
            shell=True, capture_output=True, text=True, timeout=10
        )
        output = result.stdout.strip()
        if output:
            first_line = output.split("\n")[0]
            try:
                return {"success": True, "output": first_line, "json": json.loads(first_line)}
            except json.JSONDecodeError:
                return {"success": False, "error": "Failed to parse monitor output", "output": first_line}
        return {"success": False, "error": "No monitor output received"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Monitor command timed out after 10s"}
    except FileNotFoundError:
        return {"success": False, "error": "sparkrun not found or timeout not installed"}


def cmd_jobs():
    """List all sparkrun jobs currently running on the cluster."""
    return run_cmd([SPARKRUN, "cluster", "status"])


def cmd_stop(container_id):
    """Stop a running sparkrun workload by container ID."""
    return run_cmd([SPARKRUN, "stop", container_id], timeout=30)


def cmd_info():
    """Show detailed cluster configuration."""
    cluster_data = read_json_file(CLUSTER_JSON)
    cluster_list = run_cmd([SPARKRUN, "cluster", "list"])
    default_cluster = run_cmd([SPARKRUN, "cluster", "show", "hscc"])

    cluster_files = {}
    clusters_dir = os.path.expanduser("~/.config/sparkrun/clusters")
    if os.path.isdir(clusters_dir):
        for f in os.listdir(clusters_dir):
            if f.endswith((".yaml", ".yml")):
                cluster_path = os.path.join(clusters_dir, f)
                try:
                    with open(cluster_path, "r") as fh:
                        cluster_files[f] = fh.read()
                except Exception:
                    cluster_files[f] = "<unable to read>"

    return {
        "cluster_config": cluster_data,
        "saved_clusters": cluster_list,
        "default_cluster": default_cluster,
        "cluster_files": cluster_files,
    }


def cmd_profile_status():
    """Show running kanban task counts per profile."""
    from profile_status import get_profile_status as _get_status
    return _get_status()


# ── Command Map ───────────────────────────────────────────────────────────

COMMANDS = {
    "cluster-status": cmd_cluster_status,
    "hosts": cmd_hosts,
    "monitor": cmd_monitor,
    "jobs": cmd_jobs,
    "stop": cmd_stop,
    "info": cmd_info,
    "cluster-template": cmd_cluster_template,
    "profile-status": cmd_profile_status,
}


# ── Entry Point ───────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print("""
Hermes Spark Cluster Control (HSCC)

Usage: hscc-cluster <command> [args]

Commands:
  cluster-status      Show running workloads and idle hosts
  hosts               List all cluster hosts and saved clusters
  monitor             Single snapshot of CPU/RAM/GPU metrics
  jobs                List all sparkrun jobs running
  stop <id>           Stop a running workload by container ID
  info                Detailed cluster configuration
  cluster-template    Manage cluster templates (list|preview|apply)
  profile-status      Show running kanban task counts per profile
        """.strip())
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    fn = COMMANDS[cmd]

    try:
        if cmd == "stop":
            if len(sys.argv) < 3:
                print("Usage: hscc-cluster stop <container_id>")
                sys.exit(1)
            container_id = sys.argv[2]
            result = fn(container_id)
        elif cmd == "cluster-template":
            # Pass remaining args through to template CLI
            result = fn()
        else:
            result = fn()

        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
