"""Health check functions for the HSCC daemon."""

import json
import os
import re
import shutil
import subprocess
import sys
import datetime
import time

from . import serving
from .daemon_ops import log
from .state import now_iso, write_state
from .util import run_cmd, ssh_cmd, http_check

# Paths used for multiplex-profile verification
_HERMES_CONFIG_YAML = os.path.expanduser("~/.hermes/config.yaml")
_HERMES_GATEWAY_STATE = os.path.expanduser("~/.hermes/gateway_state.json")
_HERMES_PROFILES_DIR = os.path.expanduser("~/.hermes/profiles")


# Topology (PRIMARY_NODE, VLLM_HEALTH_URL, NAS_HOST, ...) lives in the serving
# module, which resolves it from cluster.json/serving.json/sparkrun at import and
# may re-resolve at runtime. Read those values via `serving.<NAME>` at CALL time —
# never copy them into module-level locals here, or the checks run against stale
# placeholder IPs (the refactor regression this comment guards against).
VLLM_PORT = 8000
HSCC_CLUSTER = "hscc"
SSH_USER = "spark"
IDLE_TIMEOUT_MINUTES = 30
# Local NFS mount of the cluster NAS. Env-overridable; defaults are platform
# conventional (macOS gateway = /Volumes/NAS, Linux nodes = /mnt/nas). The NAS
# check reads the local mount (the source of truth) rather than SSHing the QNAP,
# which rejects SSH and produced a permanent false "DOWN".
NAS_MOUNT = os.environ.get(
    "HSCC_NAS_MOUNT",
    "/Volumes/NAS" if sys.platform == "darwin" else "/mnt/nas")


def check_dgx():
    """DGX check (every 5s): SSH to primary node, check GPU status, sparkrun workloads."""
    log("Running DGX check")
    results = {}
    
    # 1. SSH reachability
    ssh_result = ssh_cmd(serving.PRIMARY_NODE, "echo reachable", timeout=8)
    results["ssh_reachable"] = ssh_result.get("ok", False)
    results["ssh_error"] = ssh_result.get("output", "")
    
    # 2. GPU status
    gpu_result = ssh_cmd(
        serving.PRIMARY_NODE,
        "nvidia-smi --query-gpu=index,name,memory.total,memory.used,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null",
        timeout=10
    )
    if gpu_result.get("ok") and gpu_result.get("output"):
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
    if spark_result.get("ok"):
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
    health = http_check(serving.VLLM_HEALTH_URL, timeout=5)
    results["vllm_health"] = health
    results["vllm_healthy"] = health.get("ok", False)
    
    ok = results["ssh_reachable"] and results["vllm_healthy"]
    write_state("dgx", {"ok": ok, "details": results})
    log(f"DGX check complete: ok={ok}")
    return ok


def _gateway_job_alive():
    """Return True if the Hermes gateway supervisor job is loaded."""
    if sys.platform == "darwin":
        try:
            r = run_cmd(["launchctl", "list", "ai.hermes.gateway"], timeout=5)
            if r.get("ok", False):
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


def _check_multiplex_profiles():
    """Best-effort multiplex-profile check (runs after basic gateway health).

    Returns a dict with keys:
      ok: bool — False if multiplex is enabled but profiles are missing.
      message: str — human-readable note (always present).
    When the check is skipped (missing files, parse errors), ok=True and the
    message explains why.
    """
    # 1. Read multiplex config — best-effort
    try:
        import yaml
        with open(_HERMES_CONFIG_YAML) as f:
            config = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError):
        return {"ok": True, "message": "multiplex check skipped: config.yaml not found"}
    except Exception:
        return {"ok": True, "message": "multiplex check skipped: config parse error"}

    multiplex = config.get("multiplex_profiles")
    if not multiplex:
        return {"ok": True,
                "message": "multiplex_profiles is disabled or absent"}

    # 2. Read served_profiles — best-effort
    try:
        with open(_HERMES_GATEWAY_STATE) as f:
            gw_state = json.load(f)
    except FileNotFoundError:
        # Missing gateway_state.json when multiplex is enabled is fatal.
        return {"ok": False,
                "message": "multiplex enabled but gateway_state.json is missing — no profiles served"}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"ok": False,
                "message": "multiplex enabled but gateway_state.json is corrupt — cannot parse profiles"}
    except Exception:
        return {"ok": False,
                "message": "multiplex enabled but gateway_state.json is corrupt — unexpected parse error"}

    if not isinstance(gw_state, dict):
        return {"ok": False,
                "message": "multiplex enabled but gateway_state.json is corrupt — expected JSON object"}

    served = set(gw_state.get("served_profiles") or [])
    if not served and multiplex:
        return {"ok": False,
                "message": "multiplex enabled but served_profiles is empty — all profiles unserved"}

    # 3. Determine expected roster from profile dirs — best-effort
    try:
        expected = {
            entry.name
            for entry in os.scandir(_HERMES_PROFILES_DIR)
            if entry.is_dir() and not entry.name.startswith(".")
        }
    except (FileNotFoundError, OSError):
        return {"ok": True,
                "message": "multiplex check skipped: profiles dir not found"}

    missing = expected - served
    if missing:
        return {"ok": False,
                "message": (
                    f"multiplex enabled but {len(missing)} profile(s) not served: "
                    + ", ".join(sorted(missing))
                )}

    return {"ok": True,
            "message": f"all {len(served & expected)} multiplex profile(s) served"}


def check_gateway():
    """Gateway check (every 10s): Hermes gateway supervisor job + vLLM backend + multiplex."""
    log("Running gateway check")
    job_ok = _gateway_job_alive()
    vllm = http_check(serving.VLLM_HEALTH_URL, timeout=5)
    vllm_ok = vllm.get("ok", False)
    ok = job_ok and vllm_ok

    # Multiplex-profile check (best-effort, never blocks basic gateway health)
    mux = _check_multiplex_profiles()
    if not mux["ok"]:
        ok = False

    state_data = {
        "ok": ok, "gateway_job": job_ok, "vllm_healthy": vllm_ok,
        "multiplex_ok": mux["ok"], "multiplex_message": mux["message"],
    }
    write_state("gateway", state_data)
    log(f"Gateway check: ok={ok} (job={job_ok} vllm={vllm_ok} mux={mux['ok']}) "
        f"{mux['message']}")
    return ok


PROXY_PORT = int(os.environ.get("HSCC_PROXY_PORT", "4000"))


def _ensure_cmdlib_on_path():
    """Put the sibling hscc-commands plugin dir on sys.path for the cmdlib import.

    cmdlib lives in the hscc-commands plugin (deployed beside hscc_daemon under
    ~/.hermes/plugins). We mirror hscc_daemon.hscc._load_cluster_engine, which
    resolves the hscc-cluster plugin as a sibling and imports it bare.
    Idempotent: the dir is inserted once.
    """
    cmd_dir = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "hscc-commands"))
    if cmd_dir not in sys.path:
        sys.path.insert(0, cmd_dir)


def _tp_peer_nodes():
    """IPs that are NON-primary members of a multi-node / multi-tp serving span.

    A tp peer serves its model through the span's PRIMARY and exposes no
    endpoint of its own, so a health check must never treat it as a down worker
    nor relaunch it as a solo unit. Reuses cmdlib.serving_unit_scoreboard() —
    the SAME single source of truth /cluster, /status, and
    enumerate_cluster_nodes use — rather than re-implementing tp-peer detection
    here. Best-effort: any failure (plugin missing, import error, empty/corrupt
    serving.json) yields an empty set, degrading to the pre-fix behaviour
    instead of crashing the worker check.
    """
    try:
        _ensure_cmdlib_on_path()
        from cmdlib import serving_unit_scoreboard
        score = serving_unit_scoreboard()
    except Exception:
        return set()
    if not isinstance(score, dict):
        return set()
    return {ip for ip, s in score.items() if s and s.get("tp_peer")}



def check_proxy():
    """Keep the sparkrun LiteLLM worker proxy alive (worker load-balancer).

    Role-worker profiles + orchestrator subagents reach the worker GPUs through
    this proxy (http://localhost:PROXY_PORT). If it dies, all worker work falls
    back onto the orchestrator. Health-check it and relaunch via `sparkrun proxy
    start` over the keep-alive worker nodes if it's down. No-op when there are no
    keep-alive workers configured.
    """
    from .state import write_state

    log("Running proxy check")
    nodes = sorted(serving.KEEPALIVE_NODES)
    if not nodes:
        write_state("proxy", {"ok": True, "message": "no workers — proxy not needed",
                              "last_check": now_iso()})
        return True

    url = f"http://localhost:{PROXY_PORT}/v1/models"
    if http_check(url, timeout=5).get("ok"):
        write_state("proxy", {"ok": True, "port": PROXY_PORT,
                              "last_check": now_iso(), "message": "proxy healthy"})
        return True

    log(f"Worker proxy down on :{PROXY_PORT} — relaunching", "WARN")
    r = run_cmd(["sparkrun", "proxy", "start", "--cluster", serving.HSCC_CLUSTER,
                 "--hosts", ",".join(nodes), "--port", str(PROXY_PORT)], timeout=90)
    ok = r.get("ok", False)
    write_state("proxy", {"ok": ok, "port": PROXY_PORT, "relaunched": True,
                          "last_check": now_iso(),
                          "message": "relaunched" if ok else "relaunch failed"})
    return ok


def check_local():
    """Local services check (every 30s): Docker, Ollama, PostgreSQL, hscc_daemon.

    By default (HSCC_LOCAL_REQUIRE not set), missing services are informational —
    the check reports ok=True and just notes what is unavailable. Only when
    HSCC_LOCAL_REQUIRE lists specific services does their absence cause a failure.
    """
    log("Running local services check")
    services = {}

    # Docker
    docker_ok = False
    try:
        r = run_cmd(["docker", "info"], timeout=5)
        docker_ok = r.get("ok", False)
    except Exception:
        pass
    services["docker"] = {"running": docker_ok}

    # Ollama
    ollama = http_check("http://localhost:11434/api/tags", timeout=3)
    services["ollama"] = {"running": ollama.get("ok", False)}

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
        r = run_cmd(["pgrep", "-f", "hscc_daemon"], timeout=3)
        hscc_ok = bool(r.get("output", "").strip())
    except Exception:
        pass
    services["hscc_daemon"] = {"running": hscc_ok}
    services.pop("hermes", None)  # remove old hermes entry

    # Node.js / npm
    node_r = run_cmd(["node", "--version"], timeout=3)
    npm_r = run_cmd(["npm", "--version"], timeout=3)
    spark_r = run_cmd(["sparkrun", "--version"], timeout=3)
    tools = {
        "node": {"version": node_r.get("output", "").strip()} if node_r.get("ok") else {},
        "npm": {"version": npm_r.get("output", "").strip()} if npm_r.get("ok") else {},
        "sparkrun": {"version": spark_r.get("output", "").strip()} if spark_r.get("ok") else {},
    }

    # Collect missing services
    missing = [name for name, info in services.items() if not info.get("running")]

    # Determine required services from env var
    required_str = os.environ.get("HSCC_LOCAL_REQUIRE", "")
    required = {s.strip().lower() for s in required_str.split(",") if s.strip()} if required_str else set()

    if required:
        # Required mode: missing required services fail the check. A required
        # service this check does not even track (typo / unsupported name) is
        # unverifiable — treat it as missing rather than silently passing.
        untracked = required - set(services)
        missing_required = sorted((required & {m for m in missing}) | untracked)
        if missing_required:
            ok = False
            msg = f"required but missing: {', '.join(missing_required)}"
        else:
            ok = True
            msg = "all required services running"
    else:
        # Default (informational) mode: absence is informational
        ok = True
        if missing:
            msg = f"informational: {', '.join(missing)} not available"
        else:
            msg = "all services available"

    write_state("local", {"ok": ok, "services": services, "tools": tools, "message": msg})
    log(f"Local check: ok={ok}, message={msg}")
    return ok


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
            from .lifecycle import refresh_live_workers, reconcile_lifecycle
            refresh_live_workers()
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
        if sys_r.get("ok"):
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
    if df_r.get("ok"):
        parts = df_r["output"].split("\n")[1].split()
        if len(parts) >= 5:
            sys_info["disk_total"] = parts[1]
            sys_info["disk_used"] = parts[2]
            sys_info["disk_avail"] = parts[3]
            sys_info["disk_pct"] = parts[4]
    
    # Load average
    sys_r2 = run_cmd(["sysctl", "vm.loadavg"], timeout=3)
    if sys_r2.get("ok"):
        sys_info["loadavg"] = sys_r2["output"].strip()
    
    uptime_r = run_cmd(["uptime"], timeout=3)
    if uptime_r.get("ok"):
        sys_info["uptime"] = uptime_r["output"].strip()
    
    data["system"] = sys_info
    data["ok"] = True
    write_state("heartbeat", data)
    log(f"Heartbeat: fleet={data.get('fleet', {})}")
    return True


def check_nas():
    """NAS check (every 30s): check the local NAS mount point."""
    log("Running NAS check")
    results = {}

    # Check local NFS mount (module-level NAS_MOUNT, env-overridable)
    exists = os.path.exists(NAS_MOUNT)
    results["local_mount"] = NAS_MOUNT
    results["mount_exists"] = exists

    if exists:
        try:
            st = shutil.disk_usage(NAS_MOUNT)
            results["disk_total"] = f"{st.total // (1024**3)}G"
            results["disk_used"] = f"{st.used // (1024**3)}G"
            results["disk_avail"] = f"{st.free // (1024**3)}G"
            results["disk_pct"] = f"{st.used * 100 / st.total:.1f}%"
        except OSError:
            results["disk_error"] = "df failed"

        # Check key directories exist
        results["has_huggingface"] = os.path.isdir(os.path.join(NAS_MOUNT, "huggingface"))
        results["has_hub"] = os.path.isdir(os.path.join(NAS_MOUNT, "hub"))
        results["has_memori"] = os.path.isfile(os.path.join(NAS_MOUNT, "hermes_memori_byodb.db"))

        # Try to count cached models
        try:
            hf_dir = os.path.join(NAS_MOUNT, "huggingface")
            if os.path.isdir(hf_dir):
                hf_items = [d for d in os.listdir(hf_dir) if not d.startswith(".")]
                results["huggingface_cached_items"] = len(hf_items)
        except OSError:
            pass
    else:
        results["error"] = f"{NAS_MOUNT} not mounted"

    ok = exists
    write_state("nas", {"ok": ok, "details": results})
    log(f"NAS check: ok={ok} mount_exists={exists}")
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


# Persisted relaunch timestamps per worker UNIT (node, port), so a unit that is
# mid-load (vLLM takes minutes) is not relaunched again before its grace window
# elapses. Keyed by (node, port) so co-located models on a node are tracked
# independently (G1 — multi-model-per-node supervision). Stores WALL-CLOCK time
# (time.time()) so the grace window survives daemon restarts. Persisted to
# ~/.hscc/worker_relaunch.json (best-effort — missing/corrupt file treated as empty).
_WORKER_RELATCH_FILE = os.path.expanduser("~/.hscc/worker_relaunch.json")
_worker_relaunch_at = {}


def _load_worker_relaunch_timestamps():
    """Load persisted worker relaunch timestamps from disk (best-effort)."""
    try:
        with open(_WORKER_RELATCH_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Keys are "(node, port)" strings; values are wall-clock floats.
            for key_str, ts in data.items():
                try:
                    parts = key_str.strip("()").split(",")
                    key = (parts[0], int(parts[1]))
                    if isinstance(ts, (int, float)):
                        _worker_relaunch_at[key] = float(ts)
                except (ValueError, IndexError):
                    pass
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_worker_relaunch_timestamps():
    """Persist worker relaunch timestamps to disk (best-effort)."""
    try:
        serializable = {f"({k[0]},{k[1]})": v for k, v in _worker_relaunch_at.items()}
        tmp = _WORKER_RELATCH_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(serializable, f)
        os.replace(tmp, _WORKER_RELATCH_FILE)
    except OSError:
        pass


def check_workers():
    """Keep-alive worker check (UNIT-keyed, G1): health-check each serving.json
    keep-alive worker UNIT on its own port and relaunch a crashed one with its
    own recipe + port. A node may carry several units (co-located models) on
    distinct ports — each is supervised independently.

    Relaunch is rate-limited per (node,port) by VLLM_LOAD_GRACE_MINUTES so a unit
    still loading isn't thrashed. Relaunch stops only the unit's own recipe (not
    the whole node) so a co-located sibling isn't killed.

    Relaunch uses subprocess.Popen (detached, fire-and-forget) so the long
    weight-staging phase (5–10+ min) is not killed by a subprocess timeout.
    Output is captured in ~/.hscc/relaunch-<node>-<port>.log.
    """
    from .state import write_state
    from .lifecycle import VLLM_LOAD_GRACE_MINUTES

    log("Running workers check")
    serving_data = serving.load_serving()
    units = [u for u in serving.keepalive_units(serving_data)
             if u["node"] not in serving.ORCH_NODES]

    if not units:
        write_state("workers", {"ok": True, "message": "no keep-alive workers",
                                "last_check": now_iso()})
        return True

    # Load persisted grace timestamps on each check (survives restarts)
    _load_worker_relaunch_timestamps()

    # tp-peer awareness (the SAME primary-node-only blind spot /cluster had
    # before v1.6.0): a node that is a NON-primary member of a multi-node /
    # multi-tp span reports no endpoint of its own and must NEVER be counted
    # down nor relaunched as a solo unit. Compute the peer set once per check,
    # reusing cmdlib.serving_unit_scoreboard() (see _tp_peer_nodes).
    tp_peers = _tp_peer_nodes()

    online, relaunched, down = [], [], []
    now_wall = time.time()
    grace_secs = VLLM_LOAD_GRACE_MINUTES * 60
    for u in units:
        node, port, recipe = u["node"], u["port"], u["recipe"]
        key = (node, port)
        label = u["id"]
        if node in tp_peers:
            # Span member: its endpoint lives on the span's primary, so no
            # standalone health probe applies. Report it online (not down);
            # never relaunch it as a solo unit.
            online.append(label)
            continue
        url = f"http://{node}:{port}/health"
        if http_check(url, timeout=5).get("ok"):
            online.append(label)
            _worker_relaunch_at.pop(key, None)
            continue
        # Down. Respect the grace window after a relaunch (mid-load == not dead).
        last = _worker_relaunch_at.get(key)
        if last and now_wall - last < grace_secs:
            down.append(label)  # still within load grace — leave it
            continue
        if not recipe:
            down.append(label)
            log(f"Worker {label} down but no recipe in serving.json — skipping", "WARN")
            continue
        log(f"Worker {label} down — relaunching ({recipe}) on :{port}")
        # Stop only THIS recipe on the node (not --all) so a co-located sibling
        # on another port survives.
        stop_result = run_cmd(["sparkrun", "stop", recipe, "--hosts", node], timeout=60)
        if not stop_result.get("ok"):
            log(f"sparkrun stop for {label} failed: {stop_result.get('output', '')}", "WARN")
        # Record relaunch time before launching so we do not thrash even if
        # Popen itself raises.
        _worker_relaunch_at[key] = time.time()
        _save_worker_relaunch_timestamps()
        log_path = os.path.expanduser(f"~/.hscc/relaunch-{node}-{port}.log")
        try:
            with open(log_path, "a") as log_file:
                subprocess.Popen(
                    ["sparkrun", "run", recipe, "--cluster", serving.HSCC_CLUSTER,
                     "--hosts", node, "--port", str(port),
                     "--no-follow", "--ensure"],
                    stdout=log_file, stderr=log_file,
                    start_new_session=True,
                )
            relaunched.append(label)
        except Exception as e:
            log(f"ERROR: failed to launch worker {label}: {e}", "ERROR")
            down.append(label)

    ok = not down
    write_state("workers", {
        "ok": ok, "total": len(units), "online": len(online),
        "relaunched": relaunched, "down": down, "last_check": now_iso(),
        "message": f"{len(online)}/{len(units)} online"
                   + (f", relaunched {relaunched}" if relaunched else "")
                   + (f", down {down}" if down else ""),
    })
    log(f"Workers check: {len(online)}/{len(units)} online"
        + (f", relaunched {relaunched}" if relaunched else "")
        + (f", down {down}" if down else ""))
    return ok
