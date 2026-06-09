"""Health check functions for the HSCC daemon."""

import json
import os
import re
import shutil
import subprocess
import sys
import datetime

from . import log
from .state import now_iso, write_state
from .util import run_cmd, ssh_cmd, http_check


# These globals are set by the serving module at import time in the main hscc.py
VLLM_HEALTH_URL = ""
VLLM_RECIPE = ""
VLLM_PORT = 8000
HSCC_CLUSTER = "hscc"
PRIMARY_NODE = "192.0.2.10"
SSH_USER = "spark"
IDLE_TIMEOUT_MINUTES = 30


def check_dgx():
    """DGX check (every 5s): SSH to primary node, check GPU status, sparkrun workloads."""
    log("Running DGX check")
    results = {}
    
    # 1. SSH reachability
    ssh_result = ssh_cmd(PRIMARY_NODE, "echo reachable", timeout=8)
    results["ssh_reachable"] = ssh_result.get("success", False)
    results["ssh_error"] = ssh_result.get("error", "")
    
    # 2. GPU status
    gpu_result = ssh_cmd(
        PRIMARY_NODE,
        "nvidia-smi --query-gpu=index,name,memory.total,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null",
        timeout=10
    )
    if gpu_result.get("success") and gpu_result.get("output"):
        gpu_lines = [l.strip() for l in gpu_result["output"].split("\n") if l.strip()]
        gpus = []
        for line in gpu_lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                gpus.append({
                    "index": parts[0],
                    "name": parts[1],
                    "memory_total_mb": parts[2],
                    "memory_used_mb": parts[3],
                    "temperature": parts[4],
                    "power_watts": parts[5],
                })
        results["gpu_count"] = len(gpus)
        results["gpus"] = gpus
    else:
        results["gpu_count"] = 0
        results["gpus"] = []
    
    # 3. Sparkrun workloads
    spark_result = run_cmd(["sparkrun", "status"], timeout=10)
    if spark_result.get("success"):
        workloads = []
        for line in spark_result["output"].split("\n"):
            line = line.strip()
            if line.startswith("Job:"):
                parts = line.split()
                name = parts[1] if len(parts) > 1 else "?"
                container = "?"
                for p in parts:
                    if p.startswith("[") and p.endswith("]"):
                        container = p.strip("[]")
                workloads.append({"name": name, "container": container})
        results["workloads"] = workloads
        results["workload_count"] = len(workloads)
    else:
        results["workloads"] = []
        results["workload_count"] = 0
    
    # 4. vLLM health
    health = http_check(VLLM_HEALTH_URL, timeout=5)
    results["vllm_health"] = health
    results["vllm_healthy"] = health.get("success", False)
    
    ok = results["ssh_reachable"] and results["vllm_healthy"]
    write_state("dgx", {"ok": ok, "details": results})
    log(f"DGX check complete: ok={ok}")
    return ok


def _gateway_job_alive():
    """Return True if the Hermes gateway supervisor job is loaded."""
    if sys.platform == "darwin":
        try:
            r = run_cmd(["launchctl", "list", "ai.hermes.gateway"], timeout=5)
            if r.get("success", False):
                return True
        except Exception:
            pass
    elif shutil.which("systemctl"):
        try:
            r = run_cmd(["systemctl", "--user", "is-active", "hermes-gateway"], timeout=5)
            if r.get("output", "").strip() == "active":
                return True
        except Exception:
            pass
    
    # Last resort on any platform: is a gateway process running?
    try:
        r = run_cmd(["pgrep", "-f", "hermes.*gateway"], timeout=5)
        return bool(r.get("output", "").strip())
    except Exception:
        return False


def check_gateway():
    """Gateway check (every 10s): Hermes gateway supervisor job + vLLM backend."""
    log("Running gateway check")
    job_ok = _gateway_job_alive()
    vllm = http_check(VLLM_HEALTH_URL, timeout=5)
    vllm_ok = vllm.get("success", False)
    ok = job_ok and vllm_ok
    write_state("gateway", {"ok": ok, "gateway_job": job_ok, "vllm_healthy": vllm_ok})
    log(f"Gateway check: ok={ok} (job={job_ok} vllm={vllm_ok})")
    return ok


def check_local():
    """Local services check (every 30s): Docker, Ollama, PostgreSQL, hscc-daemon."""
    log("Running local services check")
    services = {}
    
    # Docker
    docker_ok = False
    try:
        r = run_cmd(["docker", "info"], timeout=5)
        docker_ok = r.get("success", False)
    except Exception:
        pass
    services["docker"] = {"running": docker_ok}
    
    # Ollama
    ollama = http_check("http://localhost:11434/api/tags", timeout=3)
    services["ollama"] = {"running": ollama.get("success", False)}
    
    # PostgreSQL
    pg_ok = False
    try:
        r = run_cmd(
            ["docker", "ps", "--filter", "name=hscc-postgres", "--format", "{{.Status}}"],
            timeout=5
        )
        status = r.get("output", "").strip()
        pg_ok = bool(status)
    except Exception:
        pass
    services["postgresql"] = {"running": pg_ok}
    
    # HSCC daemon
    hscc_ok = False
    try:
        r = run_cmd(["pgrep", "-f", "hscc-daemon"], timeout=3)
        hscc_ok = bool(r.get("output", "").strip())
    except Exception:
        pass
    services["hscc-daemon"] = {"running": hscc_ok}
    services.pop("hermes", None)  # remove old hermes entry
    
    # Node.js / npm
    node_r = run_cmd(["node", "--version"], timeout=3)
    npm_r = run_cmd(["npm", "--version"], timeout=3)
    spark_r = run_cmd(["sparkrun", "--version"], timeout=3)
    tools = {
        "node": {"version": node_r.get("output", "").strip()} if node_r.get("success") else {},
        "npm": {"version": npm_r.get("output", "").strip()} if npm_r.get("success") else {},
        "sparkrun": {"version": spark_r.get("output", "").strip()} if spark_r.get("success") else {},
    }
    
    all_running = docker_ok and ollama.get("success", False)
    write_state("local", {"ok": all_running, "services": services, "tools": tools})
    log(f"Local check: ok={all_running}, docker={docker_ok}, ollama={ollama.get('success')}")
    return all_running


def check_heartbeat():
    """Heartbeat check (every 60s): agent fleet status, system health."""
    log("Running heartbeat check")
    data = {}
    
    # Agent fleet
    agents_file = os.path.expanduser("~/.hscc/agents.json")
    if os.path.exists(agents_file):
        try:
            with open(agents_file) as f:
                agents_data = json.load(f)
            agents = agents_data.get("agents", [])
            from . import refresh_live_workers, reconcile_lifecycle
            refresh_live_workers(agents)
            reconcile_lifecycle(agents)
            total = len(agents)
            idle = sum(1 for a in agents if a.get("status") == "idle")
            working = sum(1 for a in agents if a.get("status") == "working")
            failed = sum(1 for a in agents if a.get("status") == "failed")
            enabled = sum(1 for a in agents if a.get("enabled", True))
            data["fleet"] = {
                "total": total,
                "idle": idle,
                "working": working,
                "failed": failed,
                "enabled": enabled,
                "disabled": total - enabled,
            }
        except (json.JSONDecodeError, IOError):
            data["fleet"] = {"error": "failed to read agents.json"}
    else:
        data["fleet"] = {"error": "agents.json not found"}
    
    # Host system info (platform-aware)
    if sys.platform == "darwin":
        sys_info = {"os": "macOS"}
        sys_r = run_cmd(["sw_vers"], timeout=3)
        if sys_r.get("success"):
            for line in sys_r["output"].split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    sys_info[key.strip()] = val.strip()
    else:
        import platform as _platform
        sys_info = {"os": _platform.system() or "Linux"}
        sys_info["ProductVersion"] = _platform.release()
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        sys_info["ProductName"] = line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass
    
    # Disk space
    df_r = run_cmd(["df", "-h", "/"], timeout=3)
    if df_r.get("success"):
        parts = df_r["output"].split("\n")[1].split()
        if len(parts) >= 5:
            sys_info["disk_total"] = parts[1]
            sys_info["disk_used"] = parts[2]
            sys_info["disk_avail"] = parts[3]
            sys_info["disk_pct"] = parts[4]
    
    # Load average
    sys_r2 = run_cmd(["sysctl", "vm.loadavg"], timeout=3)
    if sys_r2.get("success"):
        sys_info["loadavg"] = sys_r2["output"].strip()
    
    uptime_r = run_cmd(["uptime"], timeout=3)
    if uptime_r.get("success"):
        sys_info["uptime"] = uptime_r["output"].strip()
    
    data["system"] = sys_info
    data["ok"] = True
    write_state("heartbeat", data)
    log(f"Heartbeat: fleet={data.get('fleet', {})}")
    return True


def check_nas():
    """NAS check (every 30s): disk SMART, RAID, NFS clients."""
    log("Running NAS check")
    results = {}
    NAS_HOST = "192.0.2.20"
    
    # SSH to NAS
    ssh_ok = ssh_cmd(NAS_HOST, "echo reachable", timeout=8)
    results["ssh_reachable"] = ssh_ok.get("success", False)
    
    # Disk SMART / df
    if ssh_ok.get("success"):
        df_r = ssh_cmd(NAS_HOST, "df -h /mnt/nas 2>/dev/null || df -h / 2>/dev/null", timeout=10)
        if df_r.get("success") and df_r.get("output"):
            lines = df_r["output"].split("\n")
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 5:
                    results["disk_total"] = parts[1]
                    results["disk_used"] = parts[2]
                    results["disk_avail"] = parts[3]
                    results["disk_pct"] = parts[4]
        
        # NFS clients
        nfs_r = ssh_cmd(NAS_HOST, "nfsstat -e 2>/dev/null | grep -c '^[0-9]' || true", timeout=8)
        nfs_output = nfs_r.get("output", "0").strip().split("\n")[0].strip()
        try:
            results["nfs_clients"] = int(nfs_output) if nfs_output else 0
        except ValueError:
            results["nfs_clients"] = 0
        
        # Mount check
        mount_r = ssh_cmd(
            NAS_HOST,
            "cat /proc/mounts 2>/dev/null | grep nfs || mount 2>/dev/null | grep nfs || echo none",
            timeout=8
        )
        results["nfs_mounts"] = mount_r.get("output", "none").strip()
    else:
        results["error"] = ssh_ok.get("error", "SSH failed")
    
    ok = results["ssh_reachable"]
    write_state("nas", {"ok": ok, "details": results})
    log(f"NAS check: ok={ok}")
    return ok


def check_idle_monitor():
    """Check if idle agents are being cleaned up properly."""
    from .state import write_state
    
    log("Running idle monitor check")
    
    try:
        with open(os.path.expanduser("~/.hscc/agents.json")) as f:
            agents = json.load(f)
        
        idle_agents = [a for a in agents.get("agents", []) if a.get("status") == "idle"]
        stale_idle = [a for a in idle_agents if a.get("last_heartbeat")]
        
        log(f"Idle monitor: {len(idle_agents)} idle, {len(stale_idle)} with heartbeat")
        
        write_state("idle", {
            "ok": len(idle_agents) < 100,
            "idle_count": len(idle_agents),
            "stale_count": len(stale_idle),
            "last_check": now_iso(),
            "message": f"{len(idle_agents)} idle agents",
        })
        return len(idle_agents) < 100
        
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"Idle monitor error: {e}", "WARN")
        write_state("idle", {
            "ok": False,
            "error": str(e),
            "last_check": now_iso(),
            "message": "Idle monitor check failed",
        })
        return False


def check_workers():
    """Check worker node health and availability."""
    from .state import write_state
    
    log("Running workers check")
    
    try:
        workers_file = os.path.expanduser("~/.hscc/workers.json")
        if not os.path.exists(workers_file):
            log("No workers.json found, skipping workers check")
            write_state("workers", {
                "ok": True,
                "message": "No workers configured",
                "last_check": now_iso(),
            })
            return True
        
        with open(workers_file) as f:
            data = json.load(f)
        
        workers = data.get("workers", [])
        online = [w for w in workers if w.get("status") == "online"]
        
        log(f"Workers check: {len(online)}/{len(workers)} online")
        
        write_state("workers", {
            "ok": len(online) > 0,
            "total": len(workers),
            "online": len(online),
            "last_check": now_iso(),
            "message": f"{len(online)}/{len(workers)} workers online",
        })
        return len(online) > 0
        
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log(f"Workers check error: {e}", "WARN")
        write_state("workers", {
            "ok": False,
            "error": str(e),
            "last_check": now_iso(),
            "message": "Workers check failed",
        })
        return False
