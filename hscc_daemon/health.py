"""Health check functions for the HSCC daemon."""

import json
import os
import re
import shutil
import subprocess
import sys
import datetime
import time
import threading
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

# Upper bound on any single filesystem probe of the NAS (stat/listdir/df and
# the mount-table read). A stale/wedged NFS handle can block these calls
# indefinitely, so every probe runs inside a timeout-bounded daemon thread (see
# _run_bounded). Env-overridable; default 5s. A timeout reports ok=False, never
# hangs the daemon thread and never surfaces stale figures as NAS data.
NAS_PROBE_TIMEOUT = float(os.environ.get("HSCC_NAS_PROBE_TIMEOUT", "5"))

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
    state_entry = {"ok": ok, "details": results}
    if not ok and _intentional_window():
        # The serving layer is in an intentional-autodown window — either
        # down by design (model stopped IS why vLLM is unreachable) or waking
        # (model still loading, vLLM not answering yet). Both are expected
        # non-faults. Tag the stream so verify treats them as such, and names
        # the window. Any real fault (no intentional block) fails normally.
        state_entry["intentional"] = "autodown"
        state_entry["message"] = "intentional autodown — serving layer down by design"
    write_state("dgx", state_entry)
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
    if not ok and _intentional_window():
        # Serving layer in an intentional-autodown window (down by design or
        # waking) — vLLM being stopped/loading is why the gateway backend probe
        # fails. Tag so verify treats it as expected, not a fault.
        state_data["intentional"] = "autodown"
        state_data["message"] = "intentional autodown — serving layer down by design"
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
    proxy_data = {"ok": ok, "port": PROXY_PORT, "relaunched": True,
                  "last_check": now_iso(),
                  "message": "relaunched" if ok else "relaunch failed"}
    if ok is False and _intentional_window():
        # Fleet in an intentional-autodown window (down by design or waking) —
        # the worker proxy is not serving models while the layer is stopped or
        # still coming up. Tag so verify treats it as expected, not a real fault.
        # The proxy relaunch attempt above is intentionally left unchanged: the
        # proxy is a CPU-only management unit (not a serving unit), so starting
        # it is not a serving resurrection.
        proxy_data["intentional"] = "autodown"
        proxy_data["message"] = "intentional autodown — worker proxy not required while fleet down"
    write_state("proxy", proxy_data)
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
    """NAS check (every 30s): verify the local NAS path is a REAL NFS mount.

    Fix for a silent-failure bug: the old check reported ok=true whenever the
    directory existed, so a bare local directory left behind after the NFS
    export/mount was lost looked perfectly healthy — and `df` on an unmounted
    path silently resolved to the LOCAL boot disk, reporting local figures as
    NAS figures. The new check requires the path to actually be in the OS mount
    table and/or to sit on a different device than its parent, NEVER reports
    local disk figures as NAS figures when unmounted, and bounds every
    filesystem probe in a timeout so a wedged (stale-handle) NFS mount cannot
    hang the daemon thread.
    """
    log("Running NAS check")
    results = {"local_mount": NAS_MOUNT}

    # The path must at least exist on disk. (Existence alone is NOT enough —
    # a bare directory satisfies it — but if it does not exist at all, we're
    # done immediately.) These stat()-backed calls run INSIDE _run_bounded: on
    # a wedged (stale-handle) NFS mount even os.path.exists can block
    # indefinitely, and left unbounded they would freeze the daemon's periodic
    # NAS thread — the root cause of "NAS check silently stops running". All
    # three of _probe_mount's stat/df calls are already bounded; the existence
    # gate at the top of this check was the one fs-probe left unguarded.
    done, existence = _run_bounded(_probe_exists, NAS_PROBE_TIMEOUT, NAS_MOUNT)
    if not done:
        results["message"] = (
            f"NAS existence probe timed out after {NAS_PROBE_TIMEOUT:g}s — "
            "stale/wedged mount?"
        )
        _write_nas(False, results)
        return False
    exists, is_dir = existence
    results["mount_exists"] = exists
    results["is_dir"] = is_dir
    if not exists:
        results["message"] = f"{NAS_MOUNT} missing (NAS not mounted)"
        _write_nas(False, results)
        return False

    # Signal 1 — the OS mount table. Parsing the table never touches the NAS
    # filesystem, so a wedged (stale-handle) NFS mount cannot block it. This is
    # authoritative for "is it actually mounted": a bare directory does not
    # appear in the table; a real mount does.
    entry = _mount_entry_for(NAS_MOUNT)
    results["in_mount_table"] = entry is not None
    results["mount_fstype"] = entry[1] if entry else None
    results["mount_source"] = entry[2] if entry else None

    # Signal 2 — the filesystem probe, wrapped in a timeout-bound daemon thread
    # so a wedged handle cannot hang the daemon. A timeout reports ok=False and
    # surfaces NO figures (never treats a stale read as NAS data).
    done, probe = _run_bounded(_probe_mount, NAS_PROBE_TIMEOUT, NAS_MOUNT)
    if not done:
        results["message"] = (
            f"NAS probe timed out after {NAS_PROBE_TIMEOUT:g}s — "
            "stale/wedged mount?"
        )
        _write_nas(False, results)
        return False

    # Signal 3 — device id. A real mount changes st_dev vs. its parent; an
    # empty local directory does not. Cheap and robust corroboration.
    results["mount_is_distinct_device"] = (
        probe.get("st_dev") is not None
        and probe.get("parent_dev") is not None
        and probe.get("st_dev") != probe.get("parent_dev")
    )

    # A path is a real NAS mount iff it is in the mount table OR sits on a
    # distinct device from its parent (covers the rare case where the mount
    # table is unreachable in a restricted environment).
    is_mount = (entry is not None) or results["mount_is_distinct_device"]
    if not is_mount:
        # Bare local directory where the NFS mount used to be — the silent-
        # failure bug. Do NOT surface the probe's disk figures: in this state
        # `df` resolves to the LOCAL boot disk, which we must not report as NAS.
        results["message"] = (
            f"{NAS_MOUNT} exists but is not an NFS mount (export/mount lost)"
        )
        _write_nas(False, results)
        return False

    # Genuine mount — safe to surface probe figures as NAS figures.
    for _k in ("st_dev", "parent_dev"):
        probe.pop(_k, None)
    results.update(probe)

    # A genuinely-mounted-but-empty NAS is a DIFFERENT (healthy) condition from
    # "not mounted" — it is still a real mount, so ok stays True; we just note
    # that the expected content markers are absent.
    if not (probe.get("has_hub") or probe.get("has_huggingface")):
        results["message"] = (
            f"{NAS_MOUNT} is an NFS mount but no expected content found "
            "(hub/ or huggingface/) — genuinely empty NAS?"
        )
    else:
        results["message"] = f"{NAS_MOUNT} is an NFS mount"

    _write_nas(True, results)
    return True


def _write_nas(ok, results):
    """Persist a NAS check result and emit its log line."""
    write_state("nas", {"ok": ok, "details": results})
    log(f"NAS check: ok={ok} {results.get('message', '')}")


def _norm_path(path):
    """Normalize a path for string comparison (never touches the filesystem)."""
    return os.path.normpath(path.rstrip("/")) if isinstance(path, str) else path


def _mount_table():
    """Return [(mount_point, fstype, source), ...] parsed from the OS mount table.

    Reads the platform mount table WITHOUT touching the NAS filesystem, so a
    wedged (stale-handle) NFS mount can never block this. Linux reads
    /proc/mounts (a plain file read that never blocks); macOS and other
    platforms shell out to `mount` (which reads the kernel mount table, not the
    NFS server) through a timeout-bounded run_cmd. Any failure returns [] so
    the caller still corroborates with the st_dev signal.
    """
    try:
        if sys.platform == "linux":
            with open("/proc/mounts") as f:
                lines = f.read().splitlines()
            parse = _parse_linux_mount_line
        else:
            r = run_cmd(["mount"], timeout=NAS_PROBE_TIMEOUT)
            if not r.get("ok"):
                return []
            lines = r["output"].splitlines()
            parse = _parse_osx_mount_line
    except OSError:
        return []

    entries = []
    for line in lines:
        e = parse(line)
        if e is not None:
            entries.append(e)
    return entries


def _parse_linux_mount_line(line):
    """Parse a /proc/mounts line ``device mount_point fstype opts ...``."""
    parts = line.split()
    if len(parts) < 3:
        return None
    return (parts[1], parts[2], parts[0])   # (mount_point, fstype, source)


def _parse_osx_mount_line(line):
    """Parse a macOS `mount` line ``source on /mount/point (options...)``.

    macOS reports the filesystem type as a bare option among the parentheses
    (e.g. ``(nfs, ...)``, ``(apfs, ...)``); options with ``=`` are key=value and
    not the type.
    """
    m = re.match(r"^(\S+)\s+on\s+(\S+)\s+\(([^)]*)\)", line)
    if not m:
        return None
    source, mount_point, options = m.group(1), m.group(2), m.group(3)
    fstype = None
    for opt in options.split(","):
        opt = opt.strip()
        if opt and "=" not in opt:
            fstype = opt
            break
    return (mount_point, fstype or "unknown", source)


def _mount_entry_for(path):
    """Return (mount_point, fstype, source) for `path` from the mount table, or None.

    Matches on the normalized mount point, so a bare directory that is not
    actually mounted returns None. For our use the mount point is the NAS path
    itself, so an exact (normalized) string match is correct — no prefix
    matching of the penultimate directory. This runs string-only (never touches
    the NAS filesystem), so it cannot block on a wedged handle.
    """
    target = _norm_path(path)
    for (mp, fstype, source) in _mount_table():
        if _norm_path(mp) == target:
            return (mp, fstype, source)
    return None


def _run_bounded(fn, timeout, *args):
    """Run fn(*args) in a daemon thread; return (True, result) or (False, None).

    The worker thread is daemonised, so if it has not finished within `timeout`
    (e.g. a wedged NFS mount blocking stat/listdir/df) it is abandoned and this
    returns (False, None) — the caller reports a timeout and never hangs the
    daemon. Any exception raised by fn is re-raised in the caller thread.
    """
    box = {"done": False, "value": None, "exc": None}

    def _target():
        try:
            box["value"] = fn(*args)
        except BaseException as e:  # capture any blocking error
            box["exc"] = e
        finally:
            box["done"] = True

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    if not box["done"]:
        return False, None
    if box["exc"] is not None:
        raise box["exc"]
    return True, box["value"]


def _probe_exists(path):
    """Return (exists, is_dir) for `path`, tolerating a wedged NFS handle.

    On a stale-handle NFS mount even os.path.exists / os.path.isdir can block
    indefinitely, so the caller runs this inside ``_run_bounded`` like every
    other fs probe. Exception-safe: any OSError reports ``exists=False``.
    """
    try:
        exists = os.path.exists(path)
        is_dir = os.path.isdir(path) if exists else False
        return exists, is_dir
    except OSError:
        return False, False


def _probe_mount(path):
    """Gather NAS filesystem facts for `path`. Returns a dict.

    Assumes `path` exists (checked by the caller). May block if the NFS handle
    is wedged, which is exactly why the caller runs it inside a timeout-bound
    daemon thread. Fully exception-safe: any OSError yields a partial dict with
    a probe_error note rather than raising. disk_* figures are only surfaced by
    the caller once it has confirmed `path` is a real mount — otherwise `df`
    would resolve to the LOCAL boot disk.
    """
    r = {}
    try:
        try:
            r["parent_dev"] = os.stat(os.path.dirname(path)).st_dev
        except OSError:
            r["parent_dev"] = None
        try:
            r["st_dev"] = os.stat(path).st_dev
        except OSError:
            r["st_dev"] = None
        try:
            du = shutil.disk_usage(path)
            r["disk_total"] = f"{du.total // (1024**3)}G"
            r["disk_used"] = f"{du.used // (1024**3)}G"
            r["disk_avail"] = f"{du.free // (1024**3)}G"
            r["disk_pct"] = f"{du.used * 100 / du.total:.1f}%"
        except OSError:
            r["disk_error"] = "df failed"
        r["has_huggingface"] = os.path.isdir(os.path.join(path, "huggingface"))
        r["has_hub"] = os.path.isdir(os.path.join(path, "hub"))
        r["has_memori"] = os.path.isfile(os.path.join(path, "hermes_memori_byodb.db"))
        hf_dir = os.path.join(path, "huggingface")
        if os.path.isdir(hf_dir):
            hf_items = [d for d in os.listdir(hf_dir) if not d.startswith(".")]
            r["huggingface_cached_items"] = len(hf_items)
    except OSError:
        r["probe_error"] = "filesystem probe failed"
    return r


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


def _intentional_autodown_down():
    """True when an intentional autodown is in effect AND the layer is down.

    The resurrection guard for ``check_workers`` (SAFETY BLOCKER, third vector):
    reuses ``autodown.classify()`` — the SAME single decision table the watchdog
    fork (lifecycle.py:219) and trigger engine (trigger.py:162) consult — rather
    than inventing a parallel rule. ``classify()`` returns ``expected_down`` only
    when the watchdog block is latched with ``intentional == "autodown"`` AND
    autodown state is confirmed ``"down"``.

    When this is True the ENTIRE serving layer (incl. keepalive units — C4
    reversed: fleet-down takes keepalive units too) is deliberately down by
    autodown, so ``check_workers`` must NOT relaunch anything. It became
    reachable as a resurrection vector only when the C4 keepalive exemption was
    removed (v1.10.0): autodown now tears keepalive units down, but check_workers
    still treated them as must-always-be-up.

    Returns False in every other state (no intentional block, or a transition in
    progress such as ``waking``/``should_be_up``) so ordinary keep-alive
    supervision proceeds exactly as before. Fail-safe direction: an unreadable
    block/state (or classify() raising) ⇒ False ⇒ check_workers keeps
    self-healing — we never suppress healing on an unverifiable signal.
    """
    try:
        from . import autodown
        from .lifecycle import load_watchdog_block
        return autodown.classify(load_watchdog_block(), autodown.load_config()) \
            == "expected_down"
    except Exception:
        return False


def _intentional_window():
    """True during the intentional-autodown WINDOW (down OR waking).

    The breadth-of-window companion to ``_intentional_autodown_down`` for the
    stream-TAGGING writers (check_dgx:274, check_gateway:397, check_proxy:483):
    during a WAKE the serving layer is coming up and the streams are
    legitimately not healthy yet (models still loading), so they must carry the
    ``intentional == "autodown"`` marker for ``hscc verify`` to report "coming
    up" instead of false-failing a normal transition. Reuses the SAME single
    decision table ``autodown.classify()`` via ``autodown.intentional_window``
    — no parallel representation.

    Deliberately NOT used by the ``check_workers`` resurrection guard, which
    stays keyed on the NARROWER ``_intentional_autodown_down()`` (expected_down
    only) so a genuinely wedged unit is still healed during a wake.
    """
    try:
        from . import autodown
        from .lifecycle import load_watchdog_block
        verdict = autodown.classify(
            load_watchdog_block(), autodown.load_config())
        return autodown.intentional_window(verdict)
    except Exception:
        return False


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

    §8 / Fix 1 (SAFETY BLOCKER): while autodown has deliberately torn the whole
    serving layer down (``classify() == expected_down``) this check MUST NOT
    relaunch the keep-alive units autodown stopped. That was the third
    resurrection vector. With NO intentional block it behaves exactly as before
    (the negative control the suite pins).
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

    # §8 intentional-autodown gate (Fix 1, extended to the wake): if the whole
    # fleet is in an intentional-autodown window (confirmed down by design, or
    # waking — the wake is bringing the units up), do NOT independently
    # relaunch the keep-alive units. Relaunching mid-wake would fight the wake
    # process and the units are legitimately not up yet, so the workers stream
    # is written "coming up with the wake" (ok=True + intentional marker) for
    # verify to excuse. Any other state (no block / should_be_up / healthy)
    # falls through to ordinary supervision; a wake that fails and leaves the
    # waking window (→ error) resumes healing there.
    if _intentional_window():
        _window_label = (
            "intentional autodown — fleet down by design"
            if _intentional_autodown_down()
            else "waking from autodown — fleet coming up, not relaunching")
        log(f"Workers check: intentional autodown window in effect — fleet "
            f"(incl. keepalive units) not up, NOT relaunching")
        write_state("workers", {
            "ok": True, "total": len(units), "online": 0, "relaunched": [],
            "down": [], "last_check": now_iso(),
            "intentional": "autodown",
            "message": f"{_window_label} — {len(units)} keepalive unit(s) "
                       f"not up",
        })
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


# --------------------------------------------------------------------------
# Engine-wedge probe (E stream) — v1.12.0
# --------------------------------------------------------------------------
# A regressed vLLM/engine process can keep listening and answer the HTTP
# handshake (200) while never emitting generated tokens — a "200 that hangs".
# Reachability checks (/health, tcp, ps) cannot see this. This probe sends a
# tiny STREAMING chat-completions request directly to each serving.json unit's
# primary node and requires REAL generated text within a SHORT timeout.
#
# Alerting is hung on the existing trigger-engine path: when a unit is wedged
# we write this stream with ``ok: False``, which trigger_engine() (trigger.py)
# turns into a ``state.engine_wedge.degraded`` pseudo-event that operator rules
# (triggers.json) can fire on — no new notification channel.
# --------------------------------------------------------------------------

ENGINE_WEDGE_STREAM = "engine_wedge"
# Short wall-clock bound for the ENTIRE streaming probe (connect + first token
# + a read of the stream). A healthy engine streams tens of tokens well under
# this; a wedged engine stalls with zero tokens. Never alert on a single slow
# response — the consecutive-failure threshold handles that separately.
ENGINE_WEDGE_TIMEOUT = int(os.environ.get("HSCC_ENGINE_WEDGE_TIMEOUT", "10"))
# A unit may legitimately be mid-load (weights staging) for minutes; a loading
# unit is NOT a wedged unit. A unit unseen before (first probe since daemon
# start) within this many seconds of its first probe is reported "loading" and
# is not a wedge candidate.
ENGINE_WEDGE_LOAD_GRACE = int(os.environ.get("HSCC_ENGINE_WEDGE_LOAD_GRACE", "240"))
# Consecutive probe failures required before a unit is declared wedged.
# Debounce so a single slow response never fires the alert.
ENGINE_WEDGE_THRESHOLD = int(os.environ.get("HSCC_ENGINE_WEDGE_THRESHOLD", "2"))
# Tokens to request per probe ("tens of tokens" — enough to confirm the engine
# is actually generating, not just returning the HTTP handshake). Deliberately
# small so a healthy unit returns within the short timeout.
ENGINE_WEDGE_MAX_TOKENS = int(os.environ.get("HSCC_ENGINE_WEDGE_MAX_TOKENS", "40"))

# In-memory per-unit wedge state, keyed by unit id. Lives only for the lifetime
# of this daemon process: ``first_seen`` (first tick we probed the unit),
# ``consecutive_failures`` (wedged-signal streak), ``last_success`` (wall clock
# of last healthy probe). Persisted nowhere — a daemon restart resets streaks,
# which is correct: on restart a healthy unit streams text immediately, and a
# still-loading unit is back inside the load grace via a fresh ``first_seen``.
_engine_wedge_units = {}


def _engine_wedge_unit_key(u):
    """Stable per-unit key. Prefer the unit's explicit id; fall back to the
    primary node + port so co-located units on one node stay distinct."""
    uid = u.get("id")
    if uid:
        return uid
    nodes = u.get("nodes") or []
    return f"{nodes[0] if nodes else '?'}:{u.get('port') or '?'}"


def _read_sse_content_until(resp, stop_event, out_box):
    """Drain an OpenAI-compatible SSE stream on a helper thread.

    Accumulates ``choices[0].delta.content`` text into ``out_box`` (a plain
    list) until [DONE], EOF, or ``stop_event`` is set. Runs off the caller's
    stack so the caller can enforce an OVERALL outer deadline on the streaming
    probe with a bounded ``join(timeout=...)`` regardless of per-read socket
    timeouts. Best-effort: any parse/transport error just stops accumulating;
    the resulting (possibly empty) text is what the caller judges.
    """
    buf = b""
    fp = getattr(resp, "fp", None)
    try:
        while not stop_event.is_set():
            if fp is None:
                break
            chunk = fp.read(256)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    return
                try:
                    obj = json.loads(payload)
                    delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        out_box.append(content)
                except Exception:
                    pass
    except Exception:
        pass


def _served_model_name(node, port, timeout=5):
    """First model id the unit reports at /v1/models, or None.

    Kept deliberately cheap and failure-tolerant: if we cannot ask, the caller
    falls back and the probe fails as it did before rather than crashing.
    """
    import urllib.request
    try:
        url = "http://%s:%s/v1/models" % (node, port)
        with urllib.request.urlopen(urllib.request.Request(url),
                                    timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        for entry in (data.get("data") or []):
            mid = entry.get("id")
            if mid:
                return mid
    except Exception:
        pass
    return None


def _probe_unit_generation(node, port, timeout=None, max_tokens=None):
    """Send a tiny streaming chat-completions request to a unit's direct
    endpoint and confirm it returns REAL generated text within a short window.

    Returns ``{"ok": bool, "status": int|None, "error": str, "text": str}``.

    Core signal (the wedge signature): the unit returns HTTP 200 but produces
    NO content deltas within ``timeout`` seconds ⇒ ``ok: False`` with a
    "wedged" classification. HTTP/transport errors ⇒ ``ok: False`` with a
    "down" error — unreachable, a distinct condition from "answering but never
    generating". The caller separates the two in the stream's details.
    """
    import urllib.request
    import urllib.error
    import threading as _threading

    timeout = timeout or ENGINE_WEDGE_TIMEOUT
    max_tokens = max_tokens or ENGINE_WEDGE_MAX_TOKENS
    # Ask the unit which model it actually serves. This used to send a
    # placeholder "x", which vLLM answers with HTTP 404 (no such model) — so
    # the probe could NEVER return ok against a real engine, and every unit was
    # classified "down" forever. A 404 also means the server answered, which is
    # positive evidence of liveness, making the old reading exactly backwards.
    model = _served_model_name(node, port) or "x"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": "Reply with the single word: ok."}],
        "max_tokens": max_tokens,
        "stream": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://{node}:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            out_box = []
            stop = _threading.Event()
            t = _threading.Thread(
                target=_read_sse_content_until, args=(resp, stop, out_box),
                daemon=True)
            t.start()
            # Overall wall-clock bound on the whole streaming probe. If the
            # worker thread is still blocked in a socket read when this
            # returns, stop_event tells it to abandon and we judge the (empty)
            # text already gathered.
            t.join(timeout=timeout)
            stop.set()
            text = "".join(out_box)
        if text.strip():
            return {"ok": True, "status": status, "text": text.strip(),
                    "error": ""}
        return {"ok": False, "status": status, "error": "wedged"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": "down"}
    except Exception as e:
        return {"ok": False, "status": None, "error": "down"}


def check_engine_wedge():
    """Per-unit inference-wedge probe (E stream): confirm EVERY serving.json
    unit actually STREAMS real generated text, not merely answers HTTP 200.

    Reachability checks cannot see a wedged engine: a regressed process keeps
    listening and returns the HTTP handshake while never emitting generated
    tokens. This probe POSTs a tiny STREAMING chat-completions request directly
    to each unit's primary node (``nodes[0]:port``) and requires real generated
    text within a SHORT timeout.

    ONE concern, kept clean: the stream reports per-unit status and the overall
    ``ok`` reflects only the WEDGE signal (a unit answering 200 with zero
    tokens). A genuinely-down unit (transport/HTTP error) is recorded in
    ``details.down`` but does NOT fail this stream — a down unit is the
    ``workers``/``gateway`` streams' job, and failing here too would double-fire
    the escalation. One wedged unit never marks a healthy sibling unhealthy.

    Guards:
      * intentional autodown in effect => SKIP probing entirely; write
        ``ok: True`` + ``intentional: "autodown"`` so ``hscc verify`` neither
        flags the stream stale nor fails it during a deliberate teardown.
      * unit still loading (unseen within the load grace) => report "loading",
        not a wedge candidate.
      * never alert on a single slow response => consecutive-failure debounce
        (``ENGINE_WEDGE_THRESHOLD``).

    Alerting is hung on the existing trigger-engine path: ``ok: False`` makes
    trigger_engine() emit a ``state.engine_wedge.degraded`` pseudo-event that
    operator rules can fire on. No new notification channel.
    """
    from .state import now_iso, write_state

    log("Running engine-wedge check")
    serving_data = serving.load_serving()
    if not isinstance(serving_data, dict):
        serving_data = {"units": []}
    units = [u for u in (serving_data.get("units") or []) if isinstance(u, dict)]

    # SKIP entirely during an intentional autodown: the serving layer is down
    # by design (or waking), so probing would only report a false wedge against
    # a deliberately stopped layer. Write ok:True + the intentional marker so
    # verify neither flags the stream stale nor fails it.
    if _intentional_window():
        log("Engine-wedge check: intentional autodown in effect — SKIPPING probe")
        write_state(ENGINE_WEDGE_STREAM, {
            "ok": True, "skipped": "intentional autodown", "units": [],
            "wedged": [], "loading": [], "down": [], "ok_units": [],
            "last_check": now_iso(), "intentional": "autodown",
            "message": "intentional autodown — probe skipped by design",
        })
        return True

    if not units:
        write_state(ENGINE_WEDGE_STREAM, {
            "ok": True, "units": 0, "wedged": [], "loading": [], "down": [],
            "ok_units": [], "last_check": now_iso(),
            "message": "no serving units configured",
        })
        return True

    now_wall = time.time()
    wedged, loading, down, ok_units = [], [], [], []

    for u in units:
        nodes = [n for n in (u.get("nodes") or []) if n]
        if not nodes:
            continue
        key = _engine_wedge_unit_key(u)
        node, port = nodes[0], u.get("port") or serving.serving_port(serving_data)
        label = u.get("id") or f"{node}:{port}"

        st = _engine_wedge_units.setdefault(key, {
            "first_seen": now_wall, "consecutive_failures": 0,
            "last_success": 0.0,
        })

        # A unit we've only just met may still be loading weights (minutes);
        # a loading unit is NOT a wedged unit. Skip it for the load grace and
        # report "loading" so the stream tells the truth without alarming.
        if now_wall - st["first_seen"] < ENGINE_WEDGE_LOAD_GRACE:
            loading.append({"unit": label, "node": node, "port": port,
                            "status": "loading"})
            continue

        probe = _probe_unit_generation(node, port)
        if probe["ok"]:
            st["consecutive_failures"] = 0
            st["last_success"] = now_wall
            ok_units.append({"unit": label, "node": node, "port": port,
                             "status": "ok"})
            continue

        if probe["error"] == "wedged":
            st["consecutive_failures"] += 1
            if st["consecutive_failures"] >= ENGINE_WEDGE_THRESHOLD:
                stalled_s = (now_wall - st["last_success"]) if st["last_success"] else None
                wedged.append({"unit": label, "node": node, "port": port,
                               "status": "wedged",
                               "message": "HTTP 200 but no generated tokens "
                                          "within %ss" % ENGINE_WEDGE_TIMEOUT,
                               "last_success": st["last_success"] or None,
                               "stalled_for_s": round(stalled_s) if stalled_s else None})
            else:
                # Debouncing: one (or a few) slow/empty responses is not yet a
                # declared wedge — report transparently but do not fail.
                loading.append({"unit": label, "node": node, "port": port,
                                "status": "checking",
                                "consecutive_failures": st["consecutive_failures"]})
            continue

        # Down (transport/HTTP error) — recorded but NOT a wedge-stream failure
        # (the unit isn't answering at all; that's the workers/gateway streams'
        # job). Keep the streak reset so a unit that comes back healthy starts
        # clean instead of inheriting stale wedge credit.
        st["consecutive_failures"] = 0
        down.append({"unit": label, "node": node, "port": port,
                     "status": "down",
                     "message": probe.get("error") or "unreachable"})

    ok = not wedged
    msg_parts = [f"{len(ok_units)}/{len(units)} streaming ok"]
    if loading:
        msg_parts.append(f"{len(loading)} loading/checking")
    if down:
        msg_parts.append(f"{len(down)} down (not wedge)")
    if wedged:
        w = ", ".join(w_["unit"] for w_ in wedged)
        msg_parts.append(f"WEDGED: {w}")
        log(f"Engine-wedge check: WEDGED unit(s) detected: {w}", "ERROR")

    write_state(ENGINE_WEDGE_STREAM, {
        "ok": ok, "units": len(units), "ok_units": ok_units,
        "wedged": wedged, "loading": loading, "down": down,
        "last_check": now_iso(), "message": ", ".join(msg_parts),
    })
    log(f"Engine-wedge check: {', '.join(msg_parts)}")
    return ok
