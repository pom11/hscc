"""Health check functions for the HSCC daemon."""

import json
import os
import re
import shutil
import subprocess
import sys
import datetime
import time
from pathlib import Path

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

# ── Worker auto-heal (WD1) ─────────────────────────────────────────────────
# A worker unit that stays DOWN across WORKER_AUTOHEAL_DEBOUNCE CONSECUTIVE
# checks is force-recreated via `template apply <applied-template>
# --confirm --force-recreate` — the REAL apply path (no reimplementation).
# This fixes the "container stays Up in docker but the vLLM server inside
# never answers (stuck past the CUDA banner)" class of failure that a plain
# sparkrun --ensure relaunch cannot: --ensure sees the container already
# "running" and no-ops, so the unit is detected-down every cycle forever.
#
#   * DEBOUNCE — a single down-blip must NOT fire. We need N consecutive
#     confirmed-down checks of the SAME unit (default 3 ~= 90s), so a
#     legitimate slow load or a transient probe timeout doesn't thrash the
#     fleet. A unit that comes back UP resets its streak (flapping counts 0).
#   * COOLDOWN — after an auto-heal fires, we do NOT fire again for the same
#     unit within the window (default 10min) even if it stays down, so a
#     genuinely-broken unit escalates instead of spinning in a fight with a
#     slow model load. A unit still down after that should alert, not loop.
#
# Both are CONFIGURABLE via env — same pattern as VLLM_LOAD_GRACE_MINUTES /
# WATCHDOG_BACKOFF_MINUTES in lifecycle.py, never hardcoded.
WORKER_AUTOHEAL_DEBOUNCE = int(os.environ.get("HSCC_WORKER_AUTOHEAL_DEBOUNCE", "3"))
WORKER_AUTOHEAL_COOLDOWN_MINUTES = int(
    os.environ.get("HSCC_WORKER_AUTOHEAL_COOLDOWN_MINUTES", "10"))

# In-memory per-unit debounce/cooldown bookkeeping (reset on daemon start, so a
# restart begins clean). Keyed by (node, port) — the unit identity.
_worker_down_streak = {}     # (node, port) -> consecutive down-checks
_worker_last_autoheal = {}   # (node, port) -> wall-clock ts of last auto-heal


# Inline script run under sparkrun's OWN venv python to invoke the structured
# cluster-status API. We cannot `import sparkrun` in this process: hscc_daemon
# runs under the Hermes agent venv, and sparkrun is installed (with its
# transitive deps) only in sparkrun's dedicated venv (include-system-site-
# packages=false). So we shell out to sparkrun's own interpreter — resolved
# dynamically from the `sparkrun` CLI binary's shebang, never hardcoded — and
# parse its structured JSON output. This mirrors EXACTLY how `sparkrun status`
# resolves its own defaults (SparkrunConfig → ClusterManager → resolve_hosts →
# build_ssh_kwargs → query_cluster_status) but emits structured JSON instead of
# the human-readable text we used to string-parse.
_SPARKRUN_STATUS_SCRIPT = (
    "import json,sys\n"
    "from sparkrun.core.config import SparkrunConfig,get_config_root\n"
    "from sparkrun.core.cluster_manager import ClusterManager,query_cluster_status\n"
    "from sparkrun.core.hosts import resolve_hosts\n"
    "from sparkrun.orchestration.primitives import build_ssh_kwargs\n"
    "config=SparkrunConfig()\n"
    "mgr=ClusterManager(get_config_root())\n"
    "hosts=resolve_hosts(None,None,None,mgr,config.default_hosts)\n"
    "if not hosts:\n"
    "    print(json.dumps({'host_list':[]})); sys.exit(0)\n"
    "ssh_kwargs=build_ssh_kwargs(config)\n"
    "r=query_cluster_status(hosts,ssh_kwargs=ssh_kwargs,cache_dir=str(config.cache_dir))\n"
    "print(json.dumps(r.to_dict()))\n"
)


def _sparkrun_venv_python():
    """Return the python interpreter that owns the `sparkrun` CLI.

    Resolved from the `sparkrun` executable's shebang (``#!/path/to/python``)
    so we reuse sparkrun's own venv — where the sparkrun package and its
    transitive deps actually live. Returns None if `sparkrun` is not on PATH.
    """
    sparkrun_bin = shutil.which("sparkrun")
    if not sparkrun_bin:
        return None
    try:
        with open(sparkrun_bin, "rb") as f:
            first = f.readline().decode("utf-8", "replace").strip()
        if first.startswith("#!"):
            interp = first[2:].strip()
            if interp and (shutil.which(interp) is not None or os.path.exists(interp)):
                return interp
    except OSError:
        pass
    return None


def _sparkrun_workloads():
    """Return running sparkrun workloads as [{name, container}, ...].

    Replaces the former `sparkrun status` shell-out + `Job:` text-parsing in
    the DGX check, which was fragile to any cosmetic change in sparkrun's
    human-readable output. Instead we invoke sparkrun's own structured
    ``query_cluster_status`` API — run under sparkrun's own venv python
    (resolved from the `sparkrun` binary shebang, see ``_SPARKRUN_STATUS_SCRIPT``)
    so it is reachable at daemon runtime — and read its JSON ``to_dict()``
    output.

    We map ``ClusterStatusResult.to_dict()``'s ``groups`` (cluster members
    with job metadata) + ``solo_entries`` onto the SAME shape the old
    text-parsing produced — ``[{"name": ..., "container": ...}]`` — so nothing
    downstream that reads ``results["workloads"]`` needs to change.

    Defensive fallback: if the structured query is unreachable (no sparkrun CLI,
    unresolvable interpreter, query failure), fall back to the legacy shell-out
    text-parse path rather than reporting an empty workload list. Any other
    failure returns [] — the check degrades gracefully instead of crashing.
    """
    venv_py = _sparkrun_venv_python()
    if venv_py:
        try:
            res = run_cmd([venv_py, "-c", _SPARKRUN_STATUS_SCRIPT], timeout=25)
            if res.get("ok") and res.get("output"):
                data = json.loads(res["output"])
                return _workloads_from_cluster_status(data)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            log(f"sparkrun cluster-status JSON parse failed ({e})", "WARN")
        except Exception as e:
            log(f"sparkrun cluster-status query failed ({e})", "WARN")

    # Last resort: legacy `sparkrun status` text-parse (only if the structured
    # path is unavailable — e.g. sparkrun not on PATH at all).
    log("structured sparkrun status unavailable — falling back to "
        f"`sparkrun status` text-parse (venv_py={venv_py!r})", "WARN")
    return _sparkrun_workloads_textparse()


def _workloads_from_cluster_status(data):
    """Map a ClusterStatusResult.to_dict() into the legacy [{name, container}] shape.

    The legacy `Job:` text-parse produced ONE entry per Job line — i.e. one per
    cluster/job (cluster_id), not one per container. So we emit exactly one
    entry per ``groups`` entry and one per ``solo_entries`` entry, keeping the
    count identical to the old output. ``container`` carries the job/cluster id
    (e.g. ``sparkrun_1b6e77192e59``→ the old ``[122ebe6fc4a2]`` hash) and the
    ``name`` carries the recipe label from job metadata when available.
    """
    workloads = []
    for cid, group in (data.get("groups") or {}).items():
        meta = group.get("meta") or {}
        recipe = meta.get("recipe") or ""
        name = f"{recipe} ({cid})" if recipe else cid
        workloads.append({"name": name, "container": cid})
    for entry in data.get("solo_entries") or []:
        meta = entry.get("meta") or {}
        recipe = meta.get("recipe") or ""
        host = entry.get("host", "?")
        cid = entry.get("cluster_id", "?")
        name = f"{recipe} ({host})" if recipe else f"{cid} ({host})"
        workloads.append({"name": name, "container": cid})
    return workloads


def _sparkrun_workloads_textparse():
    """Legacy fallback: shell out to `sparkrun status` and text-parse.

    Only reached when the structured cluster-status query is unreachable
    (no `sparkrun` CLI on PATH to resolve its venv python) or fails. Keeps
    workload detection working in such degraded environments at the cost of
    the old text-parsing fragility.
    """
    spark_result = run_cmd(["sparkrun", "status"], timeout=10)
    if not spark_result.get("ok"):
        return []
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
    return workloads


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
    workloads = _sparkrun_workloads()
    results["workloads"] = workloads
    results["workload_count"] = len(workloads)
    
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


# ── Worker auto-heal helper ────────────────────────────────────────────────
# The heal ACTION is injectable (``_autoheal_worker_fn``) so tests can stub it;
# production uses the REAL template-apply path via cmd_cluster_template — the
# exact function ``hscc template apply`` dispatches to, no reimplementation.

def _autoheal_cluster_dir():
    """Sibling hscc-cluster plugin dir (same resolution as hscc.py)."""
    return Path(__file__).resolve().parent.parent / "hscc-cluster"


def _default_autoheal_worker(key, label, node, port):
    """REAL auto-heal: force-recreate the down unit by re-applying the currently
    applied template with ``--force-recreate``.

    Reuses the existing apply path (``cmd_cluster_template`` → the same function
    ``hscc template apply <name> --confirm --force-recreate`` dispatches to) —
    NOT a parallel mechanism. Resolves the currently-applied template from the
    same state ``hscc template status`` reads (``~/.hscc/applied_template.json``).
    Returns the apply result dict.

    Injectable for tests via monkeypatching ``_autoheal_worker_fn``; a test that
    exercises THIS function patches ``cmd_cluster_template`` to prove the real
    path is reached with force-recreate, never touching a real node/docker.
    """
    cluster_dir = str(_autoheal_cluster_dir())
    if cluster_dir not in sys.path:
        sys.path.insert(0, cluster_dir)
    try:
        from cluster_template_cli import cmd_cluster_template
        from cluster_template import applied_status
    except ImportError as e:
        log(f"Auto-heal {label}: cannot load apply path ({e})", "ERROR")
        return {"ok": False, "error": f"apply path unavailable: {e}"}
    # The currently applied template (what `hscc template status` reports).
    state = applied_status().get("applied") or {}
    name = state.get("template")
    if not name:
        log(f"Auto-heal {label}: no applied template recorded — cannot force-recreate", "WARN")
        return {"ok": False, "error": "no applied template recorded"}
    log(f"Auto-heal {label}: force-recreating via template apply '{name}' --force-recreate")
    return cmd_cluster_template(["apply", name, "--confirm", "--force-recreate"])


# Production default; tests monkeypatch this to inject the heal call.
_autoheal_worker_fn = _default_autoheal_worker


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
            # Back UP — reset the consecutive-down debounce so a flapping unit
            # (down→up→down) never accumulates toward an auto-heal. The cooldown
            # timestamp is deliberately NOT reset: it still guards against an
            # immediate re-fire if this unit flaps back down within the window.
            _worker_down_streak.pop(key, None)
            continue
        # DEBOUNCED AUTO-HEAL (WD1): the unit is DOWN. Increment its
        # consecutive-down streak and, once it crosses the debounce threshold
        # AND is out of this unit's cooldown, force-recreate it via the REAL
        # template-apply path (--force-recreate). This is what fixes a unit
        # that stays "Up" in docker but whose vLLM never answers — a plain
        # sparkrun --ensure relaunch no-ops on it (--ensure sees the container
        # already running). Debounce/cooldown are configurable, not hardcoded.
        # The streak counts REGARDLESS of the gentle-relaunch grace below:
        # mid-load a unit is still not-serving, but the debounce (3 checks
        # ≈ 90s) is far shorter than a load (minutes), so a slow load alone can
        # never trip it — yet a genuinely wedged unit will.
        streak = _worker_down_streak.get(key, 0) + 1
        _worker_down_streak[key] = streak
        cooldown_s = WORKER_AUTOHEAL_COOLDOWN_MINUTES * 60
        if (streak >= WORKER_AUTOHEAL_DEBOUNCE
                and now_wall - _worker_last_autoheal.get(key, 0.0) >= cooldown_s):
            log(f"Auto-heal: worker {label} ({node}:{port}) down {streak}x "
                f"consecutively — force-recreate via template apply")
            _worker_last_autoheal[key] = now_wall
            _worker_down_streak[key] = 0  # finalise this debounce round
            heal_result = _autoheal_worker_fn(key, label, node, port)
            heal_note = (heal_result.get("status") or heal_result.get("ok")
                         or heal_result.get("error") or "?")
            log(f"Auto-heal result for {label}: {heal_note} "
                f"{heal_result.get('note') or ''}".strip())
            # Announce through the existing watchdog notification channel (the
            # Telegram ops topic) — reuse it, do not build a new one.
            try:
                from .telegram import notify_operations
                notify_operations(
                    f"🤖 HSCC auto-heal: worker `{label}` ({node}:{port}) down "
                    f"{streak}x consecutively — re-applied template "
                    f"`{heal_result.get('template') or '?'}` with --force-recreate "
                    f"→ {heal_note}")
            except Exception as e:
                log(f"Auto-heal notify failed: {e}", "WARN")
            down.append(label)
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
