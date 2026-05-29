#!/usr/bin/env python3
"""
Hermes Spark Cluster Control (HSCC) — Monitoring Daemon & Watchdog

Background daemon with 5 parallel check streams, PipelineWatchdog with
auto-remediation, event-driven trigger engine, macOS notifications, and
Launchd service integration.

Usage: hscc-daemon <command> [args]

Commands:
  start              Start the daemon in the background
  stop               Gracefully stop a running daemon
  status             Show daemon status and last check results
  check [stream]     Run a single check cycle (dgx|gateway|local|heartbeat|nas|watchdog|triggers|all)
  watch [stream]     Tail check results in real-time (dgx|gateway|local|heartbeat|nas|watchdog|triggers|all)
  ed-status          Show event-driven mode status (kqueue + launchd)
  ed-install         Install event-driven launchd jobs only
  ed-uninstall       Remove event-driven launchd jobs only
  triggers           Show trigger engine status
  notify <msg>       Send a manual notification
  plist              Generate Launchd plist for auto-start
  install            Install Launchd plist and start daemon
  uninstall          Remove Launchd plist and stop daemon
  log                Show daemon log output
"""

import sys
import json
import os
import signal
import subprocess
import time
import threading
import uuid
import datetime
import collections
import shutil
from pathlib import Path

# ── Event-Driven Mode ───────────────────────────────────────────────────────
# Import event_driven module for kqueue/launchd support.
# Falls back gracefully if unavailable (non-macOS or import error).
try:
    from event_driven import (
        KqueueWatcher,
        LaunchdJobGenerator,
        EventDrivenDaemon,
        EventBridge,
        FallbackPoller,
        PERIODIC_STREAMS,
        STATE_DIR as _ED_STATE_DIR,
        run_event_drained_daemon_loop,
    )
    _EVENT_DRIVEN_AVAILABLE = True
except ImportError:
    _EVENT_DRIVEN_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────

HSCC_DIR = os.path.expanduser("~/.hscc")
STATE_DIR = os.path.join(HSCC_DIR, "state")
PID_FILE = os.path.join(HSCC_DIR, "daemon.pid")
LOG_FILE = os.path.join(HSCC_DIR, "daemon.log")
PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")
PLIST_FILE = os.path.join(PLIST_DIR, "com.nousresearch.hscc-daemon.plist")

EVENTS_FILE = os.path.join(HSCC_DIR, "events.jsonl")
TRIGGERS_FILE = os.path.join(HSCC_DIR, "triggers.json")
COOLDOWN_FILE = os.path.join(HSCC_DIR, "cooldowns.json")
WATCHDOG_BLOCK_FILE = os.path.join(HSCC_DIR, "watchdog_block.json")

# Check stream intervals (seconds)
STREAMS = {
    "dgx":          5,
    "gateway":     10,
    "local":       30,
    "heartbeat":   60,
    "nas":         30,
}

# Cluster host configuration — resolved from cluster.json at runtime
SSH_USER = "spark"
SSH_OPTS = "-o StrictHostKeyChecking=no -o ConnectTimeout=10"
NAS_HOST = None
PRIMARY_NODE = None
VLLM_HEALTH_URL = None

def _build_vllm_cmds():
    """Build vLLM SSH commands from resolved PRIMARY_NODE."""
    if PRIMARY_NODE:
        global VLLM_STOP_CMD, VLLM_START_CMD
        VLLM_STOP_CMD = f"ssh {SSH_OPTS} {SSH_USER}@{PRIMARY_NODE} 'pkill -f vllm || true'"
        VLLM_START_CMD = f"ssh {SSH_OPTS} {SSH_USER}@{PRIMARY_NODE} 'nohup vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --port 8000 > /tmp/vllm.log 2>&1 &'"
    else:
        VLLM_STOP_CMD = ""
        VLLM_START_CMD = ""

# ── Cluster Config Resolution ─────────────────────────────────────────────

CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")


def resolve_cluster_config():
    """Resolve gateway/workers/NAS from cluster.json or sparkrun, update globals."""
    global NAS_HOST, PRIMARY_NODE, VLLM_HEALTH_URL
    try:
        with open(CLUSTER_JSON) as f:
            config = json.load(f)
        gateway = config.get("gateway", {})
        workers = config.get("workers", [])
        nas_devices = config.get("nasDevices", [])

        # Primary node: use first worker if available, else gateway
        if workers:
            PRIMARY_NODE = workers[0].get("ip")
        elif gateway:
            PRIMARY_NODE = gateway.get("ip")

        # NAS
        if nas_devices:
            NAS_HOST = nas_devices[0].get("ip")

        # vLLM URL
        if PRIMARY_NODE:
            VLLM_HEALTH_URL = f"http://{PRIMARY_NODE}:8000/health"

        # Build SSH commands
        _build_vllm_cmds()
        return

    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Fallback: try sparkrun cluster list
    try:
        result = subprocess.run(
            "timeout 2 sparkrun cluster list --json",
            shell=True, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            clusters = json.loads(result.stdout.strip())
            for cluster in clusters:
                if cluster.get("default"):
                    hosts = cluster.get("hosts", [])
                    if hosts:
                        PRIMARY_NODE = hosts[0].split(":")[0]
                        VLLM_HEALTH_URL = f"http://{PRIMARY_NODE}:8000/health"
                        _build_vllm_cmds()
                    break
    except Exception:
        pass


# Resolve cluster config at import time — after log() is defined
resolve_cluster_config()

# ── Logging ────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    """Write a timestamped log line to the daemon log file."""
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[{ts}] [{level:>5s}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except IOError:
        pass
    # Also print if daemon is running in foreground mode
    if not os.path.exists(PID_FILE):
        print(line)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── State Management ───────────────────────────────────────────────────────

def ensure_state_dir():
    os.makedirs(STATE_DIR, exist_ok=True)


def write_state(stream_name, data):
    """Write check result to ~/.hscc/state/<stream>.json."""
    ensure_state_dir()
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
    entry = {
        "timestamp": now_iso(),
        "stream": stream_name,
        **data,
    }
    tmp = filepath + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(entry, f, indent=2, default=str)
        os.replace(tmp, filepath)
    except (OSError, IOError) as e:
        log(f"write_state({stream_name}) error: {e}", "ERROR")
        # Fallback: write directly
        try:
            with open(filepath, "w") as f:
                json.dump(entry, f, indent=2, default=str)
        except (OSError, IOError):
            pass
    return entry


def read_state(stream_name):
    """Read the last result for a stream, or None."""
    filepath = os.path.join(STATE_DIR, f"{stream_name}.json")
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_all_states():
    """Read all state files."""
    ensure_state_dir()
    states = {}
    for fn in os.listdir(STATE_DIR):
        if fn.endswith(".json"):
            stream = fn[:-5]  # strip .json
            filepath = os.path.join(STATE_DIR, fn)
            try:
                with open(filepath) as f:
                    states[stream] = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
    return states


# ── SSH Helper ─────────────────────────────────────────────────────────────

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
        return {"success": False, "error": f"timed out after {timeout}s", "command": args}
    except FileNotFoundError:
        return {"success": False, "error": f"not found: {args[0]}", "command": args}


def ssh_cmd(host, command, timeout=20):
    """Run a command via SSH on a cluster host."""
    return run_cmd(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
         f"{SSH_USER}@{host}", command],
        timeout=timeout,
    )


def http_check(url, timeout=5):
    """HTTP health check."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--connect-timeout", str(timeout), "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 2
        )
        code = result.stdout.strip()
        return {"success": code.startswith(("2", "3")), "http_code": code, "url": url}
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}


# ── Stream Checks ──────────────────────────────────────────────────────────

def check_dgx():
    """DGX check (every 5s): SSH to primary node, check GPU status, sparkrun workloads."""
    log("Running DGX check")

    results = {}

    # 1. SSH reachability
    ssh_result = ssh_cmd(PRIMARY_NODE, "echo reachable", timeout=8)
    results["ssh_reachable"] = ssh_result.get("success", False)
    results["ssh_error"] = ssh_result.get("error", "")

    # 2. GPU status
    gpu_result = ssh_cmd(PRIMARY_NODE,
                         "nvidia-smi --query-gpu=index,name,memory.total,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null",
                         timeout=10)
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


def check_gateway():
    """Gateway check (every 10s): HTTP check on Hermes gateway port."""
    log("Running gateway check")

    # Hermes gateway default port
    for port in [18789, 18788, 18790]:
        url = f"http://localhost:{port}/api/health"
        health = http_check(url, timeout=5)
        if health.get("success"):
            result = {"ok": True, "port": port, "health": health}
            write_state("gateway", result)
            log(f"Gateway check: healthy on port {port}")
            return True

    # Also try SSH to gateway
    gw_result = ssh_cmd(PRIMARY_NODE, "echo ok", timeout=8)
    result = {
        "ok": False,
        "ssh_reachable": gw_result.get("success", False),
        "http_checks": [],
    }
    write_state("gateway", result)
    log(f"Gateway check: not healthy")
    return False


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
        r = run_cmd(["docker", "ps", "--filter", "name=hscc-postgres", "--format", "{{.Status}}"],
                     timeout=5)
        status = r.get("output", "").strip()
        pg_ok = bool(status)
    except Exception:
        pass
    services["postgresql"] = {"running": pg_ok}

    # HSCC daemon (managed by launchd)
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

    all_running = docker_ok and ollama.get("success", False) and oclaw_ok
    write_state("local", {
        "ok": all_running,
        "services": services,
        "tools": tools,
    })
    log(f"Local check: ok={all_running}, docker={docker_ok}, ollama={ollama.get('success')}, oclaw={oclaw_ok}")
    return all_running


def reconcile_lifecycle(agents):
    """Sync lifecycle.json to the authoritative agents.json status.

    Nothing transitions lifecycle running->idle on normal task completion, so
    lifecycle.json drifts to a stale 'running' while agents.json status (kept
    current by provision/heartbeat) already reads 'idle'. Converge the FSM to
    the authoritative field: when status is idle but lifecycle says running,
    write the idle transition. Only running->idle is reconciled — transitional
    states (spawning/ready/finished) are left untouched to avoid racing an
    in-flight spawn.
    """
    lc_file = os.path.expanduser("~/.hscc/lifecycle.json")
    if not os.path.exists(lc_file):
        return
    try:
        with open(lc_file) as f:
            lc_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return

    states = lc_data.get("agents", {})
    status_by_id = {a.get("id"): a.get("status") for a in agents}
    changed = []
    for aid, entry in states.items():
        if entry.get("state") == "running" and status_by_id.get(aid) == "idle":
            entry["state"] = "idle"
            entry["updated_at"] = now_iso()
            entry["reconciled"] = True
            changed.append(aid)

    if changed:
        lc_data["agents"] = states
        tmp = lc_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(lc_data, f, indent=2, default=str)
        os.replace(tmp, lc_file)
        log(f"Reconciled lifecycle running->idle for {len(changed)} agent(s): {', '.join(changed)}")


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

    # macOS system info
    sys_r = run_cmd(["sw_vers"], timeout=3)
    sys_info = {"os": "macOS"}
    if sys_r.get("success"):
        for line in sys_r["output"].split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                sys_info[key.strip()] = val.strip()

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
        mount_r = ssh_cmd(NAS_HOST, "cat /proc/mounts 2>/dev/null | grep nfs || mount 2>/dev/null | grep nfs || echo none", timeout=8)
        results["nfs_mounts"] = mount_r.get("output", "none").strip()
    else:
        results["error"] = ssh_ok.get("error", "SSH failed")

    ok = results["ssh_reachable"]
    write_state("nas", {"ok": ok, "details": results})
    log(f"NAS check: ok={ok}")
    return ok


# ── PipelineWatchdog ───────────────────────────────────────────────────────

FAILURE_HISTORY_KEY = "watchdog_failures"


def load_watchdog_block():
    """Load the block state file."""
    try:
        with open(WATCHDOG_BLOCK_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"blocked": False, "reason": "", "blocked_at": None, "failures": [], "auto_restart_count": 0}


def save_watchdog_block(data):
    """Save the block state file."""
    tmp = WATCHDOG_BLOCK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, WATCHDOG_BLOCK_FILE)


def cleanup_old_failures(failures, window_minutes=10):
    """Keep only failures within the last window_minutes."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=window_minutes)
    result = []
    for f in failures:
        ts = f.get("timestamp", "")
        if not ts:
            result.append(f)
            continue
        try:
            entry_time = datetime.datetime.fromisoformat(ts).replace(tzinfo=datetime.timezone.utc)
            if entry_time > cutoff:
                result.append(f)
        except (ValueError, TypeError):
            result.append(f)
    return result


def pipeline_watchdog():
    """Watchdog cycle (every 30s): check DGX+gateway, auto-restart vLLM, block on 3 failures."""
    log("Running PipelineWatchdog")

    block = load_watchdog_block()

    # If currently blocked, don't run checks, just report
    if block.get("blocked"):
        log("Watchdog: blocked, skipping checks")
        write_state("watchdog", {
            "ok": False,
            "blocked": True,
            "reason": block.get("reason", ""),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "last_check": now_iso(),
            "message": f"Pipeline blocked: {block['reason']}",
        })
        return False

    # Run DGX + gateway checks
    dgx_ok = check_dgx()
    gw_ok = check_gateway()

    if dgx_ok and gw_ok:
        # Success — reset failure history if within window
        success_entry = {"timestamp": now_iso(), "dgx": True, "gateway": True}
        failures = block.get("failures", [])
        failures.append(success_entry)
        block["failures"] = cleanup_old_failures(failures, window_minutes=10)
        block["failed_count"] = 0
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": True,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "message": "Pipeline healthy",
            "auto_restart_count": block.get("auto_restart_count", 0),
        })
        log("Watchdog: pipeline healthy")
        return True

    # Failure — record it
    failure_entry = {"timestamp": now_iso(), "dgx": dgx_ok, "gateway": gw_ok}
    failures = block.get("failures", [])
    failures.append(failure_entry)
    block["failures"] = cleanup_old_failures(failures, window_minutes=10)
    block["failed_count"] = len(block["failures"])

    # Count recent failures
    recent = [f for f in block["failures"] if not f.get("dgx", True) or not f.get("gateway", True)]

    if len(recent) >= 3:
        # BLOCK — don't do more checks or restarts
        block["blocked"] = True
        block["blocked_at"] = now_iso()
        reason = f"3 consecutive failures in 10min: DGX={'OK' if dgx_ok else 'FAIL'} GW={'OK' if gw_ok else 'FAIL'}"
        block["reason"] = reason
        log(f"Watchdog: BLOCKING pipeline — {reason}")
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": True,
            "reason": reason,
            "last_check": now_iso(),
            "message": "PIPELINE BLOCKED — manual intervention required",
            "auto_restart_count": block.get("auto_restart_count", 0),
        })
        # Send macOS notification
        send_macos_notification(
            "🚨 HSCC Pipeline Blocked",
            reason,
            priority="critical",
        )
        return False

    # 1-2 failures — try auto-restart vLLM
    if not dgx_ok:
        log("Watchdog: attempting vLLM auto-restart")
        restart_result = ssh_cmd(PRIMARY_NODE,
                                 "pkill -f vllm; sleep 2; nohup vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --port 8000 > /tmp/vllm_recover.log 2>&1 & echo started",
                                 timeout=15)
        restart_ok = restart_result.get("success", False)
        count = block.get("auto_restart_count", 0) + 1
        block["auto_restart_count"] = count
        block["last_restart"] = now_iso()
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "auto_restart": True,
            "restart_result": restart_result.get("success", False),
            "restart_output": restart_result.get("output", "")[:200],
            "auto_restart_count": count,
            "last_check": now_iso(),
            "message": f"Auto-restart #{count} attempted",
        })
        log(f"Watchdog: vLLM auto-restart #{count}: {'success' if restart_ok else 'failed'}")
        send_macos_notification(
            "⚠️ HSCC vLLM Auto-Restart",
            f"Auto-restart #{count} of vLLM attempted on {PRIMARY_NODE}: {'OK' if restart_ok else 'FAILED'}",
            priority="high",
        )
    else:
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "message": "Degraded — gateway not reachable",
        })

    return not (not dgx_ok and not gw_ok)


# ── Trigger Engine ─────────────────────────────────────────────────────────

def load_triggers():
    """Load trigger rules from triggers.json."""
    try:
        with open(TRIGGERS_FILE) as f:
            data = json.load(f)
        return data.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_cooldowns():
    """Load cooldown timestamps."""
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cooldowns(data):
    """Save cooldown timestamps."""
    tmp = COOLDOWN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, COOLDOWN_FILE)


def read_events_tail(limit=100):
    """Read last N lines from events.jsonl."""
    try:
        with open(EVENTS_FILE) as f:
            lines = f.readlines()
        return [l.strip() for l in lines[-limit:] if l.strip()]
    except (FileNotFoundError, IOError):
        return []


def evaluate_trigger(rule, event):
    """Check if a trigger rule matches an event."""
    trigger_type = rule.get("trigger_type", "")
    condition = rule.get("condition", {})
    metric = condition.get("metric", "")
    op = condition.get("op", "")
    value = condition.get("value")

    # Determine which field to check
    event_value = None
    if metric == "severity":
        event_value = event.get("severity", "info")
    elif metric == "event_type":
        event_value = event.get("event_type", "")
    elif metric == "source":
        event_value = event.get("source", "")
    elif metric == "failed_dgx":
        state = read_state("dgx")
        event_value = not state.get("ok", True) if state else False
    elif metric == "vllm_down":
        state = read_state("dgx")
        event_value = not state.get("vllm_healthy", True) if state else False
    elif metric == "watchdog_blocked":
        state = read_state("watchdog")
        event_value = state.get("blocked", False) if state else False
    elif metric.startswith("state."):
        stream = metric[6:]
        state = read_state(stream)
        if state:
            detail = state.get("details", {})
            event_value = detail.get(metric[6:], None)
        else:
            event_value = None

    if event_value is None:
        return False

    # Evaluate comparison
    try:
        if op == "==":
            return str(event_value) == str(value)
        elif op == "!=":
            return str(event_value) != str(value)
        elif op == ">":
            return float(event_value) > float(value)
        elif op == ">=":
            return float(event_value) >= float(value)
        elif op == "<":
            return float(event_value) < float(value)
        elif op == "<=":
            return float(event_value) <= float(value)
        elif op == "contains":
            return str(value) in str(event_value)
        elif op == "matches":
            import re
            return bool(re.match(str(value), str(event_value)))
    except (ValueError, TypeError):
        return False

    return False


def fire_trigger_action(rule, event):
    """Fire the action defined by a trigger rule."""
    rule_id = rule.get("id", "?")
    trigger_params = rule.get("trigger_params", {})
    action_type = rule.get("trigger_type", "")

    if action_type == "notify":
        title = trigger_params.get("title", f"Trigger: {rule_id}")
        body = trigger_params.get("body", f"Rule {rule_id} fired")
        send_macos_notification(title, body, priority="normal")
        log(f"Trigger {rule_id}: notification sent — {title}")

    elif action_type == "emit_event":
        event_type = trigger_params.get("event_type", f"trigger.{rule_id}")
        payload = {**trigger_params.get("payload", {}), "trigger_rule": rule_id,
                    "source_event": event}
        emit_event(event_type, payload, source="trigger_engine")
        log(f"Trigger {rule_id}: event emitted — {event_type}")

    elif action_type == "auto_restart":
        restart_result = ssh_cmd(PRIMARY_NODE,
                                 "pkill -f vllm; sleep 2; nohup vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --port 8000 > /tmp/vllm_restart.log 2>&1 & echo started",
                                 timeout=15)
        log(f"Trigger {rule_id}: auto-restart vLLM {'success' if restart_result.get('success') else 'failed'}")
        send_macos_notification("⚠️ HSCC Auto-Restart",
                                f"Trigger {rule_id} triggered vLLM restart: {'OK' if restart_result.get('success') else 'FAILED'}",
                                priority="high")

    elif action_type == "block_pipeline":
        block = load_watchdog_block()
        block["blocked"] = True
        block["blocked_at"] = now_iso()
        block["reason"] = f"Trigger rule {rule_id} triggered block"
        save_watchdog_block(block)
        log(f"Trigger {rule_id}: pipeline BLOCKED")
        send_macos_notification("🚨 HSCC Pipeline Blocked",
                                f"Trigger rule {rule_id} blocked the pipeline: {block['reason']}",
                                priority="critical")


def trigger_engine():
    """Evaluate all trigger rules against recent events and state checks."""
    log("Running TriggerEngine")

    rules = load_triggers()
    if not rules:
        write_state("triggers", {
            "ok": True,
            "rules_evaluated": 0,
            "actions_fired": 0,
            "last_check": now_iso(),
            "message": "No trigger rules configured",
        })
        return True

    cooldowns = load_cooldowns()
    actions_fired = 0

    # 1. Check recent events
    event_lines = read_events_tail(limit=50)
    recent_events = []
    for line in event_lines:
        try:
            recent_events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # 2. Also check current state for state-based triggers
    state_snapshots = read_all_states()

    targets = list(recent_events)

    # Add state-based "pseudo-events" for state-triggered rules
    for stream, state_data in state_snapshots.items():
        if state_data and "ok" in state_data and not state_data.get("ok"):
            targets.append({
                "event_type": f"state.{stream}.degraded",
                "severity": "warning",
                "source": "daemon_trigger_engine",
                "stream": stream,
                "state": state_data,
                "_source_event": False,
            })

    log(f"TriggerEngine: evaluating {len(rules)} rules against {len(targets)} events/states")

    for rule in rules:
        if not rule.get("enabled", True):
            continue

        rule_id = rule.get("id", "")
        cooldown = rule.get("cooldown_seconds", 0)
        now = time.time()

        # Check cooldown
        if cooldown > 0 and rule_id in cooldowns:
            last_fired = cooldowns[rule_id]
            if now - last_fired < cooldown:
                log(f"TriggerEngine: rule {rule_id} on cooldown ({int(now - last_fired)}/{cooldown}s)")
                continue

        # Evaluate against each target
        for target in targets:
            if evaluate_trigger(rule, target):
                log(f"TriggerEngine: rule {rule_id} matched!")
                fire_trigger_action(rule, target)
                if cooldown > 0:
                    cooldowns[rule_id] = now
                    save_cooldowns(cooldowns)
                actions_fired += 1
                break  # one match per rule per cycle

    write_state("triggers", {
        "ok": True,
        "rules_evaluated": len(rules),
        "events_checked": len(recent_events),
        "actions_fired": actions_fired,
        "last_check": now_iso(),
        "message": f"Evaluated {len(rules)} rules, fired {actions_fired} actions",
    })
    log(f"TriggerEngine complete: {actions_fired} actions fired")
    return True


# ── macOS Notifications ────────────────────────────────────────────────────

def send_macos_notification(title, body, priority="normal", app_id="com.nousresearch.hscc-daemon"):
    """Send a macOS notification via osascript (UNUserNotificationCenter)."""
    urgency_map = {
        "critical": "critical",
        "high": "high",
        "normal": "normal",
        "low": "low",
    }
    urgency = urgency_map.get(priority, "normal")

    # Escape quotes for AppleScript
    title_esc = title.replace('"', '\\"')
    body_esc = body.replace('"', '\\"')

    script = f'''
    do shell script "/usr/bin/osascript << 'EOF'
    notify with title \"{title_esc}\" subtitle \"\"
    content \"{body_esc}\"
    end notify
    set d to current date
    tell application \"System Events\" to set notificationCenterPrefs to POSIX file \"/usr/bin/osascript\"
    end tell
    EOF\""
    '''

    # Simpler approach using macOS `osascript` with UserNotification framework
    script = f'''
    do shell script "/usr/bin/osascript -e '
        display notification \"{body_esc}\" with title \"{title_esc}\" sound name \"Glass\"
    '" 2>/dev/null
    '''

    # Use the simplest, most reliable method
    simple_script = f"display notification \"{body_esc}\" with title \"{title_esc}\""

    try:
        result = subprocess.run(
            ["osascript", "-e", simple_script],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            log(f"macOS notification sent: {title}")
            return True
    except Exception:
        pass

    # Fallback: write to notifications file
    try:
        notif_data = {"notifications": []}
        if os.path.exists(os.path.join(HSCC_DIR, "notifications.json")):
            with open(os.path.join(HSCC_DIR, "notifications.json")) as f:
                notif_data = json.load(f)
        notif_data["notifications"].append({
            "id": str(uuid.uuid4())[:8],
            "timestamp": now_iso(),
            "read": False,
            "priority": priority,
            "title": title,
            "body": body,
            "channel": "daemon",
        })
        with open(os.path.join(HSCC_DIR, "notifications.json"), "w") as f:
            json.dump(notif_data, f, indent=2)
        log(f"Notification saved to file (macOS notify failed): {title}")
    except Exception:
        pass

    return False


# ── Event Emitter ──────────────────────────────────────────────────────────

def emit_event(event_type, payload, severity="info", source="hscc-daemon"):
    """Append an event to events.jsonl."""
    event = {
        "event_type": event_type,
        "severity": severity,
        "source": source,
        "timestamp": now_iso(),
        "payload": payload,
    }
    try:
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
        return event
    except IOError:
        log(f"Failed to write event: {event_type}", "ERROR")
        return None


# ── Daemon Lifecycle ───────────────────────────────────────────────────────

def get_pid():
    """Read PID from file, return None if not running."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        # Check if process exists
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None
    except (FileNotFoundError, ValueError):
        return None


def save_pid():
    """Write current PID to file."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def write_stopped():
    """Remove PID file."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


def get_daemon_log_tail(lines=50):
    """Read last N lines from daemon log."""
    try:
        with open(LOG_FILE) as f:
            all_lines = f.readlines()
        return all_lines[-lines:]
    except FileNotFoundError:
        return []


def stream_watcher(stream=None, interval=2):
    """Tail the state directory for updates in real-time."""
    print(f"Watching {stream or 'all'} streams (Ctrl+C to stop)...\n")
    last_state = {}

    for stream_name, stream_path in STATE_DIR.items():
        fp = os.path.join(STATE_DIR, stream_name)
        try:
            with open(fp) as f:
                last_state[stream_name] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    try:
        while True:
            if stream and stream != "all":
                stream_names = [stream]
            else:
                stream_names = list(STATE_DIR.items())

            for sn in stream_names:
                if sn == "all" and isinstance(STATE_DIR, dict):
                    # When stream_names is from STATE_DIR keys
                    pass

            fp = os.path.join(STATE_DIR, f"{sn}.json")
            try:
                with open(fp) as f:
                    current = json.load(f)
                if current != last_state.get(sn):
                    ts = current.get("timestamp", "?")[:19]
                    ok = current.get("ok", current.get("blocked", "n/a"))
                    msg = current.get("message", current.get("ok", ""))
                    print(f"\n[{ts}] {sn:12s} ok={str(ok):5s} | {msg}")
                    last_state[sn] = current
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")


# ── Launchd Plist ──────────────────────────────────────────────────────────

PLIST_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nousresearch.hscc-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>{PYTHON_PATH}</string>
        <string>start-daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{HOMEDIR}</string>
    <key>StandardOutPath</key>
    <string>{HOMEDIR}/.hscc/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{HOMEDIR}/.hscc/daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{PATH_ENV}</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>WatchPaths</key>
    <array>
        <string>{HOMEDIR}/.hscc/events.jsonl</string>
    </array>
    <key>ExitTimeOut</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
"""


def generate_plist():
    """Generate the Launchd plist with resolved paths."""
    hmdir = os.path.expanduser("~")
    python_path = shutil.which("python3") or "/usr/bin/python3"
    path_env = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

    return PLIST_CONTENT.format(
        PYTHON_PATH=python_path,
        HOMEDIR=hmdir,
        PATH_ENV=path_env,
    )


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_start():
    """Start the daemon in the background."""
    existing_pid = get_pid()
    if existing_pid:
        print(f"Daemon already running (PID {existing_pid})")
        # Verify it's alive
        try:
            os.kill(existing_pid, 0)
            return
        except OSError:
            # Stale PID file, clean up
            write_stopped()

    print("Starting hscc-daemon...")
    log("Daemon starting")

    # Fork into background
    pid = os.fork()
    if pid > 0:
        # Parent — write PID and exit
        try:
            # The child will get the real PID
            save_pid()
            print(f"hscc-daemon started (PID {pid})")
        except Exception:
            print(f"hscc-daemon started (child PID {pid})")
        return

    # Child — become daemon
    os.setsid()
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    # Change to safe directory
    os.chdir(os.path.expanduser("~"))

    # Re-fork so no controlling terminal
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — write PID and run
    save_pid()

    try:
        run_daemon_loop()
    except Exception as e:
        log(f"Daemon crashed: {e}", "ERROR")
        write_stopped()
        os._exit(1)


def _sigterm_handler(signum, frame):
    """Handle SIGTERM for graceful shutdown."""
    log(f"Received signal {signum}, shutting down...")
    write_stopped()
    os._exit(0)


def run_daemon_loop():
    """Main daemon event loop.

    On macOS with kqueue available: uses event-driven mode (kqueue watchers +
    launchd periodic jobs). Falls back to original timer-based polling on any
    failure or non-macOS platform.
    """
    ensure_state_dir()
    log("Daemon loop started")

    stop_event = threading.Event()

    def stop_handler(signum, frame):
        log(f"Received signal {signum}, stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    # ── Try event-driven mode ────────────────────────────────────────────
    if _EVENT_DRIVEN_AVAILABLE:
        try:
            _run_event_driven_daemon(stop_event)
            return
        except Exception as e:
            log(f"Event-driven mode failed ({e}), falling back to polling", "WARN")

    # ── Fallback: original polling mode ──────────────────────────────────
    log("Using fallback polling mode")

    def run_periodic(check_fn, interval, stream_name):
        """Run a check function periodically until stop_event."""
        while not stop_event.is_set():
            try:
                check_fn()
            except Exception as e:
                log(f"Check {stream_name} error: {e}", "ERROR")
            stop_event.wait(interval)

    def run_watchdog_loop():
        """Run watchdog at 30s."""
        while not stop_event.is_set():
            try:
                pipeline_watchdog()
            except Exception as e:
                log(f"Watchdog error: {e}", "ERROR")
            stop_event.wait(30)

    def run_trigger_loop():
        """Run trigger engine at 15s."""
        while not stop_event.is_set():
            try:
                trigger_engine()
            except Exception as e:
                log(f"Trigger engine error: {e}", "ERROR")
            stop_event.wait(15)

    # Create threads for each stream
    threads = []

    # Individual check threads
    for stream_name, interval in STREAMS.items():
        t = threading.Thread(
            target=run_periodic,
            args=(globals()[f"check_{stream_name}"], interval, stream_name),
            daemon=True,
        )
        t.start()
        threads.append(t)
        log(f"Started {stream_name} check thread (interval={interval}s)")

    # Watchdog thread
    wd = threading.Thread(target=run_watchdog_loop, daemon=True)
    wd.start()
    threads.append(wd)
    log("Started watchdog thread (interval=30s)")

    # Trigger engine thread
    te = threading.Thread(target=run_trigger_loop, daemon=True)
    te.start()
    threads.append(te)
    log("Started trigger engine thread (interval=15s)")

    log("All threads started, daemon loop running (polling mode)")

    # Wait for stop signal
    while not stop_event.is_set():
        stop_event.wait(1)

    log("Daemon loop stopped")
    write_stopped()


def _run_event_driven_daemon(stop_event: threading.Event) -> None:
    """Run the daemon loop using event-driven architecture.

    This function replaces the polling loops with:
      - kqueue watchers on ~/.hscc/state/ and ~/.hscc/ directories
      - launchd periodic jobs for fixed-interval checks
      - Fallback to polling if any component fails

    If kqueue is unavailable, starts a FallbackPoller for backward compatibility.
    """
    # Build the check map
    check_map = {
        "dgx": check_dgx,
        "gateway": check_gateway,
        "local": check_local,
        "heartbeat": check_heartbeat,
        "nas": check_nas,
    }

    # Register event bridge callbacks for downstream reactions
    from event_driven import EventBridge, EventDrivenDaemon

    bridge = EventBridge(check_map)

    def on_state_change(stream: str) -> None:
        """State file changed — log and optionally trigger reactions."""
        log(f"Event-driven state change: {stream}")
        # State changes trigger re-evaluation of trigger rules
        try:
            trigger_engine()
        except Exception as e:
            log(f"Post-state-change trigger eval error: {e}", "ERROR")

    bridge.register_all_callback(on_state_change)

    # Create event-driven daemon
    daemon = EventDrivenDaemon(
        check_map=check_map,
        watchdog_fn=pipeline_watchdog,
        trigger_fn=lambda: None,  # bridge handles trigger re-eval
        install_launchd=True,
    )
    # Replace the internal bridge with our enhanced one
    daemon._event_bridge = bridge

    # Start the daemon
    daemon.start()
    log("Event-driven daemon started (kqueue + launchd)")

    # Wait for stop signal
    while not stop_event.is_set():
        stop_event.wait(1)

    log("Event-driven daemon stopping...")
    daemon.stop()
    log("Event-driven daemon stopped")


def cmd_stop():
    """Stop the daemon."""
    pid = get_pid()
    if not pid:
        print("Daemon is not running")
        write_stopped()
        return

    print(f"Stopping hscc-daemon (PID {pid})...")
    log("Daemon stop requested")

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 10 seconds for graceful shutdown
        for i in range(10):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print(f"hscc-daemon stopped (PID {pid})")
                return
        # Force kill if still alive
        os.kill(pid, signal.SIGKILL)
        print(f"hscc-daemon force-killed (PID {pid})")
    except ProcessLookupError:
        print("hscc-daemon already stopped")
    except Exception as e:
        print(f"Error stopping daemon: {e}")
    finally:
        write_stopped()


def cmd_status():
    """Show daemon status and last check results."""
    pid = get_pid()

    print("=" * 60)
    print("  HSCC Daemon Status")
    print("=" * 60)

    # Daemon state
    if pid:
        print(f"  Status:    RUNNING (PID {pid})")
        try:
            os.kill(pid, 0)
            print(f"  Process:   alive")
        except OSError:
            print(f"  Process:   stale PID file")
            pid = None
    else:
        print(f"  Status:    STOPPED")

    print()

    # Read all state files
    states = read_all_states()

    if not states:
        print("  No state data yet (no checks have run)")
        return

    # Stream summary
    print("  ── Check Streams ──────────────────────")
    print(f"  {'Stream':<12s} {'Status':<8s} {'Last Check':<22s} {'OK'}")
    print(f"  {'─'*12} {'─'*8} {'─'*22} {'─'*8}")

    for stream_name in ["dgx", "gateway", "local", "heartbeat", "nas", "watchdog", "triggers"]:
        state = states.get(stream_name)
        if not state:
            print(f"  {stream_name:<12s} {'—':<8s} {'never':<22s} —")
            continue

        ok = state.get("ok", state.get("blocked", "?"))
        ts = state.get("timestamp", "?")[:19]
        status_str = "BLOCKED" if state.get("blocked") else ("OK" if ok is True else "FAIL" if ok is False else str(ok))
        ok_str = "✓" if ok is True else ("🚨" if ok is False else "—")
        print(f"  {stream_name:<12s} {status_str:<8s} {ts:<22s} {ok_str}")

    print()

    # Watchdog details
    wd_state = states.get("watchdog")
    if wd_state:
        print("  ── PipelineWatchdog ───────────────────")
        print(f"  Blocked:   {wd_state.get('blocked', False)}")
        if wd_state.get("blocked"):
            print(f"  Reason:    {wd_state.get('reason', '')}")
        print(f"  Restarts:  {wd_state.get('auto_restart_count', 0)}")
        print()

    # Trigger engine
    tr_state = states.get("triggers")
    if tr_state:
        print("  ── Trigger Engine ─────────────────────")
        print(f"  Rules:     {tr_state.get('rules_evaluated', 0)}")
        print(f"  Actions:   {tr_state.get('actions_fired', 0)}")
        print()

    print("=" * 60)


def cmd_check(stream=None):
    """Run a single check cycle."""
    check_map = {
        "dgx": check_dgx,
        "gateway": check_gateway,
        "local": check_local,
        "heartbeat": check_heartbeat,
        "nas": check_nas,
        "watchdog": pipeline_watchdog,
        "triggers": trigger_engine,
    }

    if stream and stream == "all":
        results = {}
        for name, fn in check_map.items():
            print(f"Running {name}...")
            try:
                ok = fn()
                results[name] = ok
            except Exception as e:
                print(f"  Error: {e}")
                results[name] = False
        print()
        print("Results:")
        for name, ok in results.items():
            status = "OK" if ok else "FAIL"
            print(f"  {name:<12s} {status}")
        return

    if stream and stream in check_map:
        fn = check_map[stream]
        print(f"Running {stream} check...")
        try:
            ok = fn()
            state = read_state(stream)
            print(f"  Result: {'OK' if ok else 'FAIL'}")
            if state:
                msg = state.get("message", "")
                if msg:
                    print(f"  Detail: {msg}")
        except Exception as e:
            print(f"  Error: {e}")
        return

    # Default: run DGX check
    print("Running DGX check...")
    try:
        ok = check_dgx()
        print(f"  Result: {'OK' if ok else 'FAIL'}")
    except Exception as e:
        print(f"  Error: {e}")


def cmd_watch(stream=None):
    """Tail check results in real-time."""
    stream_watcher(stream)


def cmd_triggers():
    """Show trigger engine status."""
    rules = load_triggers()
    cooldowns = load_cooldowns()
    last_check = read_state("triggers")

    print("Trigger Engine Status")
    print(f"  Rules configured: {len(rules)}")
    print(f"  Cooldowns: {len(cooldowns)} active")
    print()

    if last_check:
        print(f"  Last run:  {last_check.get('timestamp', '?')[:19]}")
        print(f"  Rules eval: {last_check.get('rules_evaluated', 0)}")
        print(f"  Actions:   {last_check.get('actions_fired', 0)}")
    else:
        print("  No check results yet")
    print()

    if rules:
        print("  Rules:")
        for r in rules:
            rid = r.get("id", "?")
            enabled = "✓" if r.get("enabled", True) else "✗"
            cooldown = r.get("cooldown_seconds", 0)
            last = cooldowns.get(rid, "never")
            if isinstance(last, (int, float)):
                last = datetime.datetime.fromtimestamp(last).isoformat()[:19]
            else:
                last = str(last)[:19]
            print(f"    {enabled} {rid:<20s} cooldown={cooldown:>4s}s  last_fired={last}")
    else:
        print("  No rules configured.")


def cmd_notify(msg):
    """Send a manual notification."""
    ts = now_iso()
    title = f"HSCC Manual: {ts[:19]}"
    print(f"Sending notification: {title}")
    ok = send_macos_notification(title, msg, priority="normal")
    print(f"  {'Sent' if ok else 'Failed'}")


def cmd_plist():
    """Generate and display Launchd plist."""
    plist = generate_plist()
    print(plist)
    print(f"\n# To install: {plist}")
    print(f"#   sudo cp <(python3 {__file__} plist) {PLIST_FILE}")
    print(f"#   launchctl load {PLIST_FILE}")


def cmd_install():
    """Install Launchd plist and start daemon."""
    print("Installing hscc-daemon Launchd service...")

    # Stop any running instance first
    pid = get_pid()
    if pid:
        print(f"  Stopping existing daemon (PID {pid})")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            os.kill(pid, 0)
        except OSError:
            pass

    # Generate and install plist
    plist_path = Path(PLIST_DIR)
    plist_path.mkdir(parents=True, exist_ok=True)

    plist_content = generate_plist()
    plist_file = plist_path / "com.nousresearch.hscc-daemon.plist"
    with open(plist_file, "w") as f:
        f.write(plist_content)

    print(f"  Plist installed: {plist_file}")

    # Load with launchctl
    result = subprocess.run(
        ["launchctl", "load", str(plist_file)],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        print("  Loaded into launchd")
        print(f"\n  hscc-daemon is now managed by launchd.")
        print(f"  To check status: launchctl list | grep hscc")
        print(f"  To uninstall:    hscc-daemon uninstall")
    else:
        # Plist was created but launchctl failed — try starting manually
        print(f"  launchctl load failed, starting manually...")
        cmd_start()
        print(f"  Plist created at {plist_file}")
        print(f"  To load on next boot: launchctl load {plist_file}")


def cmd_uninstall():
    """Remove Launchd plist and stop daemon."""
    plist_file = Path(PLIST_DIR) / "com.nousresearch.hscc-daemon.plist"

    # Stop daemon
    pid = get_pid()
    if pid:
        print(f"  Stopping daemon (PID {pid})")
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
        except OSError:
            pass
    write_stopped()

    # Unload and remove plist
    if plist_file.exists():
        result = subprocess.run(
            ["launchctl", "unload", str(plist_file)],
            capture_output=True, text=True, timeout=10
        )
        plist_file.unlink()
        print(f"  Plist removed: {plist_file}")
        print("  hscc-daemon uninstalled")
    else:
        print("  No plist found — nothing to remove")


def cmd_log():
    """Show daemon log output."""
    lines = get_daemon_log_tail(50)
    if not lines:
        print("No daemon log entries.")
        return
    for line in lines:
        print(line.rstrip())


# ── Internal: start-daemon (called by Launchd) ─────────────────────────────

def cmd_start_daemon():
    """Internal entry point: run the daemon loop directly (used by Launchd)."""
    log("start-daemon invoked (Launchd mode)")
    write_stopped()  # Remove any stale PID
    ensure_state_dir()
    try:
        run_daemon_loop()
    except Exception as e:
        log(f"start-daemon crashed: {e}", "ERROR")
        write_stopped()
        raise


# ── Event-Driven CLI Commands ─────────────────────────────────────────────────

def cmd_ed_status() -> None:
    """Show event-driven mode status."""
    if not _EVENT_DRIVEN_AVAILABLE:
        print("Event-driven mode: not available (event_driven.py not found)")
        print("  Daemon will use polling fallback.")
        return

    try:
        from event_driven import cmd_event_status
        cmd_event_status()
    except Exception as e:
        print(f"Error getting event-driven status: {e}")


def cmd_ed_install() -> None:
    """Install event-driven launchd jobs."""
    if not _EVENT_DRIVEN_AVAILABLE:
        print("Event-driven mode: not available (event_driven.py not found)")
        return

    try:
        from event_driven import cmd_install_event_driven
        cmd_install_event_driven()
    except Exception as e:
        print(f"Error installing event-driven jobs: {e}")


def cmd_ed_uninstall() -> None:
    """Remove event-driven launchd jobs."""
    if not _EVENT_DRIVEN_AVAILABLE:
        print("Event-driven mode: not available (event_driven.py not found)")
        return

    try:
        from event_driven import cmd_uninstall_event_driven
        cmd_uninstall_event_driven()
    except Exception as e:
        print(f"Error uninstalling event-driven jobs: {e}")


# ── Entry Point ────────────────────────────────────────────────────────────

USAGE = """
Hermes Spark Cluster Control (HSCC) — Monitoring Daemon & Watchdog

Usage: hscc-daemon <command> [args]

Commands:
  start              Start the daemon in the background
  stop               Gracefully stop a running daemon
  status             Show daemon status and last check results
  check [stream]     Run a single check cycle (dgx|gateway|local|heartbeat|nas|watchdog|triggers|all)
  watch [stream]     Tail check results in real-time
  triggers           Show trigger engine status
  notify <msg>       Send a manual macOS notification
  plist              Generate Launchd plist for auto-start
  install            Install Launchd plist and start daemon
  uninstall          Remove Launchd plist and stop daemon
  log                Show daemon log output

Internal (called by Launchd):
  start-daemon       Start daemon loop directly
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print(USAGE.strip())
        sys.exit(0)

    cmd = sys.argv[1].lower()

    commands = {
        "start": lambda: cmd_start(),
        "stop": lambda: cmd_stop(),
        "status": lambda: cmd_status(),
        "check": lambda: cmd_check(sys.argv[2] if len(sys.argv) > 2 else None),
        "watch": lambda: cmd_watch(sys.argv[2] if len(sys.argv) > 2 else None),
        "ed-status": lambda: cmd_ed_status(),
        "ed-install": lambda: cmd_ed_install(),
        "ed-uninstall": lambda: cmd_ed_uninstall(),
        "triggers": lambda: cmd_triggers(),
        "notify": lambda: cmd_notify(" ".join(sys.argv[2:])) if len(sys.argv) > 2 else print("Usage: hscc-daemon notify <message>"),
        "plist": lambda: cmd_plist(),
        "install": lambda: cmd_install(),
        "uninstall": lambda: cmd_uninstall(),
        "log": lambda: cmd_log(),
        "start-daemon": lambda: cmd_start_daemon(),
    }

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(commands.keys())}")
        sys.exit(1)

    try:
        commands[cmd]()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
