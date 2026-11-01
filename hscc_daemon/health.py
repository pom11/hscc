"""Health check functions for the HSCC daemon."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import datetime
import time
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from . import serving
from . import sparkrun_bin
from . import heal_lease
from . import heal_breaker
from . import launch_guard
from .desktop import emit_event, send_macos_notification
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
# checks is recreated via `sparkrun stop <recipe> --hosts <span>` + a re-issued
# `sparkrun run` for THAT unit only (see _default_autoheal_worker). Since F3
# (HEAL_CASCADE 2026-10-14) this no longer re-applies the whole template with
# `--force-recreate`, which stopped and re-ran every healthy unit in the fleet
# to heal one.
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
# Startup grace (durable, container-age keyed): a unit whose container is
# younger than this AND has never served is still LOADING, not dead, and must
# NOT be auto-healed — and that must hold ACROSS a daemon restart. The
# debounce/cooldown books are in-memory (reset on daemon start, so a restart
# begins clean); keying the grace on container creation time (docker inspect)
# + a persisted has-served flag makes it durable, so a still-loading unit never
# gets a fresh 3-strike countdown into a force-recreate after a restart.
# Default derives from the repository's real observed load bound:
# VLLM_LOAD_GRACE_MINUTES (lifecycle.py:125, default 20 min = 1200s) — the
# documented large-model weight-staging phase (5-10+ min to first serving).
# Configurable via env, never hardcoded.
WORKER_AUTOHEAL_LOAD_GRACE = int(
    os.environ.get("HSCC_WORKER_AUTOHEAL_LOAD_GRACE", "1200"))

# In-memory per-unit debounce/cooldown bookkeeping (reset on daemon start, so a
# restart begins clean). Keyed by (node, port) — the unit identity.
_worker_down_streak = {}     # (node, port) -> consecutive down-checks
_worker_last_autoheal = {}   # (node, port) -> wall-clock ts of last auto-heal

# Durable per-unit container-age bookkeeping — the signal the startup grace
# keys on. For each (node, port): the container's creation epoch (from docker
# inspect) and whether that container has ever answered /health. PERSISTED to
# disk (worker_container_state.json) so it survives daemon restarts — the whole
# point of keying on container age, not the in-memory debounce. A changed
# created_ts means the container was recreated → a freshly-loading container
# gets a fresh grace (has_served resets).
_worker_container_state = {}  # (node, port) -> {"created_ts": float|None, "has_served": bool}
_WORKER_CONTAINER_STATE_FILE = os.path.expanduser(
    "~/.hscc/worker_container_state.json")


# Inline script run under sparkrun's OWN venv python to invoke the structured
# cluster-status API. We cannot `import sparkrun` in this process: hscc_daemon
# runs under the Hermes agent venv, and sparkrun is installed (with its
# transitive deps) only in sparkrun's dedicated venv (include-system-site-
# packages=false). So we shell out to sparkrun's own interpreter — resolved
# dynamically from the `sparkrun` CLI binary's shebang, never hardcoded — and
# parse its structured JSON output. This mirrors EXACTLY how `sparkrun status`
# resolves its own defaults (SparkrunConfig → ClusterManager → resolve_hosts →
# api.status → classify_cluster_status) but emits structured JSON instead of
# the human-readable text we used to string-parse.
#
# sparkrun 0.3.6 REMOVED `query_cluster_status` — `api.status` is now THE status
# source (owns executor resolution + the merge) and `classify_cluster_status`
# (pure presentation, no ssh) turns that snapshot into the ClusterStatusResult
# whose to_dict() we consume. Ported to that API; the old import raised
# ImportError every tick, forcing the fragile `sparkrun status` text-parse
# fallback and a DGX false-FAIL on a healthy cluster.
_SPARKRUN_STATUS_SCRIPT = (
    "import json,sys\n"
    "import sparkrun.api as api\n"
    "from sparkrun.core.config import SparkrunConfig,get_config_root\n"
    "from sparkrun.core.cluster_manager import ClusterManager,classify_cluster_status\n"
    "from sparkrun.core.hosts import resolve_hosts\n"
    "from sparkrun.orchestration.primitives import build_ssh_kwargs\n"
    "config=SparkrunConfig()\n"
    "mgr=ClusterManager(get_config_root())\n"
    "hosts=resolve_hosts(None,None,None,mgr,config.default_hosts)\n"
    "if not hosts:\n"
    "    print(json.dumps({'host_list':[]})); sys.exit(0)\n"
    "ssh_kwargs=build_ssh_kwargs(config)\n"
    "snapshot=api.status(hosts,ssh_kwargs=ssh_kwargs)\n"
    "result=classify_cluster_status(snapshot,cache_dir=str(config.cache_dir),host_list=hosts)\n"
    "print(json.dumps(result.to_dict()))\n"
)


def _sparkrun_venv_python():
    """Return the python interpreter that owns the `sparkrun` CLI.

    Thin delegate to :func:`hscc_daemon.sparkrun_bin.sparkrun_venv_python` —
    PATH-INDEPENDENT resolution of the CLI (PATH first, then a deterministic
    candidate list including ``~/.local/bin/sparkrun``) plus the shebang read,
    so a launchd-supervised daemon whose service PATH omits ``~/.local/bin``
    still reaches sparkrun's own venv. The old implementation was a bare
    ``shutil.which("sparkrun")``, which returned None under exactly that
    service PATH and silently killed the structured status path AND every
    worker-relaunch shell-out (t_b543e530, live 2026-10-08). Kept as a seam so
    tests monkeypatch one name.
    """
    return sparkrun_bin.sparkrun_venv_python()


def _sparkrun_cli():
    """Absolute path to the ``sparkrun`` CLI (never the bare name).

    Thin delegate to :func:`hscc_daemon.sparkrun_bin.sparkrun_cli` (PATH first,
    then a deterministic candidate list). Every shell-out to sparkrun in this
    module must route through :func:`_sparkrun_exec_argv` so a
    service-supervised PATH (no ``~/.local/bin``) cannot produce
    ``[Errno 2] No such file or directory: 'sparkrun'`` on the recovery path.
    Returns None only when sparkrun is genuinely not installed anywhere.
    """
    return sparkrun_bin.sparkrun_cli()


def _sparkrun_exec_argv(argv):
    """Swap a bare ``sparkrun`` argv[0] for the absolute CLI path.

    The single execution chokepoint for every sparkrun shell-out/Popen in the
    daemon health/relaunch paths. Command BUILDERS keep expressing the logical
    command (bare ``sparkrun`` — grep-stable, test-stable); this resolves it at
    EXEC time. When the CLI cannot be resolved at all the argv passes through
    unchanged and the subprocess raises the loud ``[Errno 2]`` it raises today
    — never a silent no-op (t_b543e530).
    """
    return sparkrun_bin.exec_argv(argv)


def _sparkrun_workloads():
    """Return running sparkrun workloads as [{name, container}, ...].

    Replaces the former `sparkrun status` shell-out + `Job:` text-parsing in
    the DGX check, which was fragile to any cosmetic change in sparkrun's
    human-readable output. Instead we invoke sparkrun's own structured
    ``api.status``/``classify_cluster_status`` API — run under sparkrun's own
    venv python (resolved from the `sparkrun` binary shebang, see
    ``_SPARKRUN_STATUS_SCRIPT``) so it is reachable at daemon runtime — and read
    its JSON ``to_dict()`` output.

    We map ``ClusterStatusResult.to_dict()``'s ``groups`` (cluster members
    with job metadata) + ``solo_entries`` onto the SAME shape the old
    text-parsing produced — ``[{"name": ..., "container": ...}]`` — so nothing
    downstream that reads ``results["workloads"]`` needs to change.

    Returns ``(workloads, degraded)``. ``degraded`` is the anti-fail-open
    signal (t_b543e530): True ONLY when BOTH status paths are unavailable —
    sparkrun's venv python could not be resolved (or the structured query
    raised) AND the legacy `sparkrun status` shell-out failed (CLI missing
    from a service PATH is the real-world case). An empty workload list then
    means "we could not look", which is NOT the same fact as "the fleet is
    empty", and the DGX check must publish it as a degraded state instead of
    logging a plain ok=True.
    """
    venv_py = _sparkrun_venv_python()
    if venv_py:
        try:
            res = run_cmd([venv_py, "-c", _SPARKRUN_STATUS_SCRIPT], timeout=25)
            if res.get("ok") and res.get("output"):
                data = json.loads(res["output"])
                return _workloads_from_cluster_status(data), False
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            log(f"sparkrun cluster-status JSON parse failed ({e})", "WARN")
        except Exception as e:
            log(f"sparkrun cluster-status query failed ({e})", "WARN")

    # Legacy `sparkrun status` text-parse, only when the structured path is
    # unavailable. The argv is logical (`sparkrun status`) and resolved to the
    # absolute CLI at execution time by _sparkrun_exec_argv inside
    # _sparkrun_workloads_textparse.
    log("structured sparkrun status unavailable — falling back to "
        f"`sparkrun status` text-parse (venv_py={venv_py!r})", "WARN")
    workloads, cli_ok = _sparkrun_workloads_textparse()
    if not cli_ok:
        log("FLEET VISIBILITY LOST: sparkrun structured status AND the "
            "`sparkrun status` shell-out are both unavailable — the workload "
            "list below is UNKNOWN, not empty. Auto-heal relaunch cannot "
            "reach the sparkrun CLI either.", "WARN")
    return workloads, not cli_ok


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
    (no `sparkrun` CLI anywhere to resolve its venv python) or fails. Keeps
    workload detection working in such degraded environments at the cost of
    the old text-parsing fragility.

    Returns ``(workloads, cli_ok)`` where ``cli_ok`` is False when the shell-out
    itself failed (CLI unresolvable / non-zero exit). The old code returned a
    bare ``[]`` in that case — which the DGX check then reported as a healthy
    empty fleet; that fail-open was the whole bug (t_b543e530). The logical
    argv is resolved to the absolute CLI by ``util.run_cmd`` at exec time.
    """
    spark_result = run_cmd(sparkrun_bin.argv("status"), timeout=10)
    if not spark_result.get("ok"):
        return [], False
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
    return workloads, True


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
    
    # 3. Sparkrun workloads — with the fail-open CLOSED (t_b543e530). When
    # BOTH status paths are dead (venv python unresolvable AND the legacy
    # shell-out failed) the workload list is UNKNOWN, not empty: the dgx
    # stream must read DEGRADED (ok=False + WARN log line, which trigger.py
    # turns into a state.dgx.degraded pseudo-event and `hscc verify` shows
    # RED) instead of the old silent ok=True + [].
    workloads, degraded = _sparkrun_workloads()
    results["workloads"] = workloads
    results["workload_count"] = len(workloads)
    results["fleet_visibility"] = "degraded" if degraded else "ok"
    
    # 4. vLLM health
    health = http_check(serving.VLLM_HEALTH_URL, timeout=5)
    results["vllm_health"] = health
    results["vllm_healthy"] = health.get("ok", False)
    
    # Two honest verdicts, deliberately kept apart:
    #   serving_ok — can the serving layer be reached (ssh + vLLM). This is
    #     what we RETURN, so the watchdog's restart/breaker semantics are
    #     unchanged: a sparkrun *status-polling* outage must never trigger a
    #     stop/run cycle against a healthy vLLM.
    #   publish_ok — is the DGX CHECK healthy (serving ok AND we can actually
    #     see the fleet). This is what the STREAM says. An empty workload list
    #     we could not verify is not a healthy read, so the stream goes RED
    #     with a named degraded message and is deliberately NOT excused as
    #     "intentional" — we do not know the fleet state, so we must not
    #     claim it is down by design.
    serving_ok = results["ssh_reachable"] and results["vllm_healthy"]
    publish_ok = serving_ok and not degraded
    state_entry = {"ok": publish_ok, "details": results}
    if not publish_ok and degraded:
        state_entry["message"] = (
            "FLEET VISIBILITY DEGRADED — sparkrun status unavailable "
            "(structured query + `sparkrun status` shell-out both failed); "
            "workload list UNKNOWN, auto-heal relaunch cannot reach sparkrun")
    elif not publish_ok and _intentional_window():
        # The serving layer is in an intentional-autodown window — either
        # down by design (model stopped IS why vLLM is unreachable) or waking
        # (model still loading, vLLM not answering yet). Both are expected
        # non-faults. Tag the stream so verify treats them as such, and names
        # the window. Any real fault (no intentional block) fails normally.
        state_entry["intentional"] = "autodown"
        state_entry["message"] = "intentional autodown — serving layer down by design"
    write_state("dgx", state_entry)
    log(f"DGX check complete: ok={serving_ok}"
        + (" — stream DEGRADED: fleet visibility lost (sparkrun unreachable)"
           if degraded else ""))
    return serving_ok


#: Reason the last gateway check could NOT reach the liveness transports
#: (None = the transports answered). Read by check_gateway() so the stream
#: says "degraded" instead of "gateway down" when the probe itself broke —
#: GAP-F-3's lesson is that a check which fails open looks green forever, and
#: a check which fails CLOSED kills a live gateway's stream.
_GATEWAY_PROBE_UNAVAILABLE = None


def _gateway_job_alive():
    """Is the Hermes gateway job alive? True / False, or None when UNKNOWN.

    The verdict is NOT computed here. It comes from the single implementation
    in ``hscc-bootstrap/gateway_liveness.py`` (see
    :mod:`hscc_daemon.gateway_probe` for why it is loaded, not imported),
    because this function used to be the SECOND gateway-liveness implementation
    in the tree and it kept the transport the first one was rewritten to drop:
    ``pgrep -f "hermes.*gateway"``. Measured on the operator machine (t_f766ddd5),
    macOS pgrep cannot SEE the gateway at all — ``pgrep -f .`` lists 1210 pids
    while ``ps -axww`` lists 1216, and the gateway's pid is in the second set
    and not the first. A pattern cannot match a pid the matcher never receives,
    so that last-resort leg returned False for a live gateway; it was hidden
    only because the launchctl leg above it answered first. It is gone.

    Trust order now (same as the doctor's gateway row): the supervisor the
    gateway is installed under (launchctl / systemctl), then a token scan of
    ``ps`` argv for when no supervisor can answer.

    A probe that cannot RUN is UNKNOWN, and this function must not turn UNKNOWN
    into "dead": when the shared module itself cannot be loaded we keep the last
    answer we ever got (fail-open on the STREAM bit — an auto-heal relaunch of a
    healthy gateway is worse than one missed cycle) and record the reason in
    ``_GATEWAY_PROBE_UNAVAILABLE`` so the state entry says DEGRADED rather than
    pretending we know.
    """
    global _GATEWAY_PROBE_UNAVAILABLE, _GATEWAY_LAST_ANSWER
    try:
        from .gateway_probe import load_gateway_liveness
        mod = load_gateway_liveness()
    except Exception as exc:
        _GATEWAY_PROBE_UNAVAILABLE = f"liveness module unavailable: {exc}"
        log(f"Gateway liveness probe DEGRADED: {exc} — keeping last known "
            f"gateway_job value rather than reporting a live gateway dead",
            "ERROR")
        return _GATEWAY_LAST_ANSWER
    _GATEWAY_PROBE_UNAVAILABLE = None
    # Read the answer AS GIVEN, including None: `bool(None)` would silently turn
    # "no transport could answer" into a confident "dead", which is the same
    # false negative this card is about.
    alive, detail = mod.job_is_alive()
    if alive is None:
        # Every transport was silent (missing binary / timeout / exec error):
        # nothing was read, so there is no verdict to publish. Apply the same
        # rule as the unloadable-module path above — keep the last answer the
        # transports really gave, and say out loud that this cycle did not
        # verify anything.
        _GATEWAY_PROBE_UNAVAILABLE = f"no liveness transport answered: {detail}"
        return _GATEWAY_LAST_ANSWER
    _GATEWAY_LAST_ANSWER = alive
    return alive


#: Last verdict the transports actually returned (True/False), or None when no
#: check has ever run. Only consulted when the probe cannot run at all — see
#: :func:`_gateway_job_alive` for the why.
_GATEWAY_LAST_ANSWER = None


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

    # 3. Determine expected roster from profile dirs — best-effort.
    # Shares ``profile_roster.list_profiles_on_disk`` with ``verify``'s
    # multiplex check so the two surfaces can never disagree about what the
    # gateway owes (t_1c545ebb): the gateway builds ``served_profiles`` from
    # Hermes' own enumeration, which skips dot-dirs (``.deleted`` is Hermes'
    # tombstone dir), names outside the profile-id grammar, and marker-less
    # directories (a stub with only ``workspaces/``). Enumerating "every dir
    # under profiles/" demanded those be served — a permanent false-fail.
    from . import profile_roster
    if not os.path.isdir(_HERMES_PROFILES_DIR):
        return {"ok": True,
                "message": "multiplex check skipped: profiles dir not found"}
    expected = set(profile_roster.list_profiles_on_disk(_HERMES_PROFILES_DIR))

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
    probe_degraded = _GATEWAY_PROBE_UNAVAILABLE
    vllm = http_check(serving.VLLM_HEALTH_URL, timeout=5)
    vllm_ok = vllm.get("ok", False)
    # ``job_ok`` is None only when the transports could not answer at all AND
    # there was no previous answer to fall back on. An UNKNOWN job is not
    # evidence that the gateway is down, and the response to a red gateway
    # stream is an auto-heal relaunch — so unknown does not force the stream
    # red. It publishes as ``gateway_job: null`` plus a named degradation below,
    # so nothing here can claim the job was verified when it was not.
    ok = (job_ok is not False) and vllm_ok

    # Multiplex-profile check (best-effort, never blocks basic gateway health)
    mux = _check_multiplex_profiles()
    if not mux["ok"]:
        ok = False

    state_data = {
        "ok": ok, "gateway_job": job_ok, "vllm_healthy": vllm_ok,
        "multiplex_ok": mux["ok"], "multiplex_message": mux["message"],
    }
    if probe_degraded:
        # The liveness transports could not run, so `gateway_job` above is the
        # LAST answer they gave, not this cycle's. Publish that as a named
        # degradation (same shape as check_dgx's FLEET VISIBILITY DEGRADED) —
        # a stream built from a stale read must say so instead of looking like
        # a fresh verification.
        state_data["gateway_job_degraded"] = probe_degraded
    if not ok and _intentional_window():
        # Serving layer in an intentional-autodown window (down by design or
        # waking) — vLLM being stopped/loading is why the gateway backend probe
        # fails. Tag so verify treats it as expected, not a fault.
        state_data["intentional"] = "autodown"
        state_data["message"] = "intentional autodown — serving layer down by design"
    write_state("gateway", state_data)
    log(f"Gateway check: ok={ok} (job={job_ok} vllm={vllm_ok} mux={mux['ok']}) "
        f"{mux['message']}"
        + ("" if not probe_degraded
           else f" — gateway_job DEGRADED (last known value): {probe_degraded}"))
    return ok


PROXY_PORT = int(os.environ.get("HSCC_PROXY_PORT", "4000"))


def _proxy_models_probe(url, timeout=5):
    """GET the proxy /v1/models endpoint and PARSE ITS BODY.

    The 2026-10-11 outage showed why a bare reachability check is not health:
    a litellm proxy whose discovery sweep wiped its model list answers HTTP 200
    with ``{"data": []}`` forever — reachable, and serving nothing. A check
    that only looks at the status code calls that state healthy while every
    worker-model call behind it 404s.

    Returns ``{"ok": bool, "models": [id, ...], "error": str}`` where ok means
    "a parseable /v1/models body was received" — NOT "the proxy is healthy";
    the alias requirement lives in :func:`_proxy_models_healthy`. Network /
    HTTP / JSON failures return ok=False with the error text (never raises).
    """
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        body = req.read()
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        models = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
    except Exception as e:
        return {"ok": False, "models": [], "error": f"unparseable /v1/models body: {e}"}
    return {"ok": True, "models": models, "error": ""}


def _proxy_expected_aliases(serving_data=None, relaunch_hosts=None):
    """Aliases the proxy MUST serve — scoped to what `proxy start --hosts` can
    actually reach (PR #22 review: expected aliases must match the relaunch
    host set, or a correctly-configured fleet flaps unhealthy forever).

    The relaunch host set (see _proxy_relaunch_hosts) is what the proxy is
    pointed at: non-uniform fleets point ONLY at keep-alive WORKER nodes, so an
    orchestrator-only unit's ``orchestrator-model`` is NOT expected — requiring
    it made every check relaunch a proxy that could never satisfy it (a
    self-inflicted relaunch-every-tick flapper). A unit's alias is expected
    only when at least one of its nodes is in the host set; a unit with no
    node info is expected wherever the proxy points. Uniform pools point at
    EVERY serving node and serve both role aliases.

    Empty set = nothing reachable to require (no units at all) — the caller
    then falls back to the weakest honest requirement: at least one model.
    """
    if serving_data is None:
        serving_data = serving.load_serving()
    units = serving_data.get("units") if isinstance(serving_data, dict) else None
    units = [u for u in (units or []) if isinstance(u, dict)]
    if not units:
        return set()
    if serving.is_uniform_pool(serving_data):
        return {"worker-model", "orchestrator-model"}
    if relaunch_hosts is None:
        relaunch_hosts = serving.KEEPALIVE_NODES
    host_set = set(relaunch_hosts)
    expected = set()
    for u in units:
        nodes = [n for n in (u.get("nodes") or []) if n]
        if not nodes or any(n in host_set for n in nodes):
            expected.add(serving._role_alias(u.get("role")))
    return expected


def _proxy_models_healthy(probe, expected_aliases):
    """(healthy, reason) for a proxy probe against the alias requirement.

    expected_aliases empty (no serving.json) → require AT LEAST ONE model:
    an empty catalogue is unhealthy even when nothing is configured.
    With aliases configured → every expected alias must appear among the
    served model ids. 200 + empty/missing ids is unhealthy by construction
    (that is precisely the outage shape).
    """
    if not probe.get("ok"):
        return False, f"probe failed: {probe.get('error') or 'no response'}"
    models = set(probe.get("models") or [])
    if not models:
        return False, "empty model catalogue (HTTP ok, data: [])"
    if not expected_aliases:
        return True, f"{len(models)} model(s) served (no serving.json; >=1 required)"
    missing = set(expected_aliases) - models
    if missing:
        return False, (f"missing expected alias(es) {sorted(missing)}; "
                       f"served: {sorted(models)}")
    return True, f"expected aliases {sorted(expected_aliases)} all served"


# ── per-unit alias coverage (t_be4b94ca operator addendum, 2026-10-22) ──────
#
# WHY this lives at UNIT granularity when the proxy check above exists: the
# 15:10 hand-launch came up with `--served-model-name` = base name only on
# two of three units (an unquoted dry-run command line). check_proxy stayed
# GREEN all afternoon — the MERGED proxy catalogue still contained both
# aliases because the one correctly-launched unit served them — while every
# aliased request silently funneled to that single node (11 queued there, 0
# on the other two) and the engine-wedge judge said "busy, not wedged". A
# proxy-level alias check CANNOT see a per-unit gap by construction; only
# asking each unit's own /v1/models can. Same lesson as _proxy_backend_drift
# (F4): "the catalogue lists the ids" is not "the backend serves them".

def unit_expected_aliases(serving_data, unit):
    """Alias ids the unit's OWN vLLM must advertise (served-model-name set).

    uniform_pool: BOTH role aliases — the pool's premise is that every unit
    runs the same recipe and may serve either role (see
    _proxy_expected_aliases / _proxy_relaunch_hosts), so a unit missing
    either alias silently halves the fleet's routable capacity.
    Otherwise: the unit's own role alias (a non-uniform worker legitimately
    does not serve the orchestrator alias — demanding it would warn on every
    healthy fleet).
    """
    if serving.is_uniform_pool(serving_data):
        return {"worker-model", "orchestrator-model"}
    return {serving._role_alias(unit.get("role"))}


def check_unit_serving_aliases(serving_data=None, probe=None, timeout=5):
    """Per-unit /v1/models alias check (pure read; never launches/restarts).

    For every serving.json unit, probe each of its nodes' own
    ``http://<node>:<port>/v1/models`` and compare against
    :func:`unit_expected_aliases`.

    Returns ``{"ok", "findings", "unverified", "message"}``:
      ok=False — at least one unit answered with a parseable catalogue that
      LACKS an expected alias (findings name the unit and the missing ids);
      ok=None  — no mismatch seen but at least one unit could not be asked
      (fail-closed: 'could not check' is never 'serves the aliases');
      ok=True  — every probed unit serves its expected aliases, none
      unverified.

    A unit with no node/port yields no probe (nothing to ask), and units that
    are merely DOWN are the unverified case, not a finding — check_workers
    owns the down verdict. ``probe`` defaults to the same body-parsing GET
    the proxy catalogue check uses (:func:`_proxy_models_probe`); tests patch
    the seam, never a socket.
    """
    if probe is None:
        probe = _proxy_models_probe
    if serving_data is None:
        serving_data = serving.load_serving()
    units = serving_data.get("units") if isinstance(serving_data, dict) else None
    findings, unverified = [], []
    probed = 0
    for u in (units or []):
        if not isinstance(u, dict):
            continue
        nodes = [n for n in (u.get("nodes") or []) if n]
        port = u.get("port")
        unit_id = u.get("id") or ",".join(nodes)
        if not nodes or not port:
            continue
        expected = unit_expected_aliases(serving_data, u)
        models, unit_unverified, answered = set(), [], 0
        for node in nodes:
            url = f"http://{node}:{port}/v1/models"
            try:
                res = probe(url, timeout=timeout)
            except Exception as e:  # probe must not raise the check down
                res = {"ok": False, "models": [], "error": str(e)}
            if not res.get("ok"):
                unit_unverified.append(f"{node}: {res.get('error') or 'no response'}")
            else:
                models.update(res.get("models") or [])
                answered += 1
                probed += 1
        if unit_unverified:
            unverified.append(f"unit {unit_id!r} ({','.join(nodes)}:{port}) — "
                              f"probe failed: {'; '.join(unit_unverified)}")
        # A MISMATCH needs an ANSWER. A unit no node replied to has an empty
        # model set for the same reason it is unverified — we never asked it —
        # so calling that a missing-alias finding would report every DOWN unit
        # as misconfigured (and 'down' is check_workers' verdict, not ours).
        # A unit that DID answer with an empty catalogue still counts: an
        # answered-but-empty /v1/models is a real gap, not a missing probe.
        missing = expected - models
        if missing and answered:
            findings.append(
                f"unit {unit_id!r} ({','.join(nodes)}:{port}) missing expected "
                f"alias(es) {sorted(missing)}; served: {sorted(models)}")
    if findings:
        message = f"{len(findings)} unit(s) serve a model set without the expected aliases"
        return {"ok": False, "findings": findings, "unverified": unverified,
                "message": message}
    if unverified:
        return {"ok": None, "findings": [], "unverified": unverified,
                "message": f"{len(unverified)} unit(s) could not be probed "
                           f"(no alias verdict available)"}
    return {"ok": True, "findings": [], "unverified": [],
            "message": f"all {probed} probed endpoint(s) serve the expected aliases"}


# Tests monkeypatch this name (the seam) rather than urllib, mirroring the
# _launch_guard_status_fn pattern.
_unit_alias_check_fn = check_unit_serving_aliases


def _proxy_needs_restart(probe):
    """True when the relaunch must pass ``--restart``.

    Plain ``sparkrun proxy start`` refuses a proxy that is already running, so
    the 2026-10-11 shape — a LIVE proxy whose discovery emptied its catalogue —
    was detected but could never be healed. Restart only when the proxy
    answered with a parseable but EMPTY catalogue: it serves nothing, so there
    is no in-flight traffic to drop. A partial catalogue (one alias missing)
    still serves the other alias, so it never restarts; an unreachable proxy
    is not running and plain start brings it up.
    """
    return bool(probe.get("ok")) and not (probe.get("models") or [])


def _proxy_relaunch_hosts(serving_data=None):
    """Hosts to pass to `sparkrun proxy start` when relaunching the LB.

    Non-uniform pools keep the historical behaviour: the keep-alive WORKER
    nodes only (serving.KEEPALIVE_NODES deliberately excludes orchestrator
    nodes, serving.py _resolve_serving_overlay). Under a uniform_pool that is
    WRONG — every serving node may serve either role, and the 2026-10-11
    outage was exactly a proxy that lost the orchestrator's endpoint: with the
    orchestrator node absent from --hosts, its model is never added back, the
    aliases never reappear, and the LB serves only half the fleet forever.
    So a uniform pool relaunches over ALL serving nodes — every node named by
    every serving.json unit (orchestrators, keep-alive AND non-keep-alive
    workers, tp peers included; a probe of a peer's absent endpoint is
    harmless), unioned with KEEPALIVE_NODES so an env-only override
    (serving.json absent, HSCC_KEEPALIVE_NODES set) still reaches the fleet.
    """
    if serving_data is None:
        serving_data = serving.load_serving()
    nodes = set(serving.KEEPALIVE_NODES)
    if serving.is_uniform_pool(serving_data):
        units = serving_data.get("units") if isinstance(serving_data, dict) else None
        for u in (units or []):
            if isinstance(u, dict):
                nodes.update(n for n in (u.get("nodes") or []) if n)
    return sorted(nodes)


PROXY_RELAUNCH_COOLDOWN_S = int(os.environ.get("HSCC_PROXY_RELAUNCH_COOLDOWN_S", "300"))


def _proxy_relaunch_stamp():
    """Raw `last_relaunch` stamp from the proxy state file, or None.

    Separate from _proxy_relaunch_cooldown_state() so check_proxy can CARRY
    the stamp forward verbatim on suppressed ticks — re-stamping during
    cooldown would extend the window every tick and make it perpetual."""
    from .state import read_state
    prev = read_state("proxy")
    if not isinstance(prev, dict):
        return None
    stamp = prev.get("last_relaunch")
    return stamp or None


# LOUD + ESCALATING RELAUNCH (t_43b6e245, docs/audits/PROXY_RELAUNCH_NOOP_2026-10-18.md).
# On 2026-10-18 ~36 plain `sparkrun proxy start` attempts over 3h09m did nothing
# because a hollow tracked PID (alive per sparkrun's state file, serving nothing)
# made every start refuse — "Error: proxy already running ... Use --restart" —
# and hscc discarded the command output that said exactly that. After N
# consecutive failed attempts the stream must stop being polite: (a) alert once
# per notify-cooldown (desktop notify + events.jsonl row), and (b) escalate from
# the refused plain start to the stop -> reap -> verified-start repair that
# clears the hollow PID. 3 sits above a one-off transient (one refused tick heals
# by itself on the next cooldown window) and well below the incident's ~15 min
# of silent no-ops.
PROXY_RELAUNCH_MAX_CONSECUTIVE = int(
    os.environ.get("HSCC_PROXY_RELAUNCH_MAX_CONSECUTIVE", "3"))
PROXY_STALL_NOTIFY_COOLDOWN_S = float(
    os.environ.get("HSCC_PROXY_STALL_NOTIFY_COOLDOWN_S", "1800"))
# `sparkrun proxy start` returns after sparkrun's own 2s startup grace; litellm
# needs several more seconds to bind the port. A single owner check right after
# start returns is therefore NOT a verdict — the 2026-10-18 01:41:42
# "repair UNVERIFIED — port owner None" line was this race, not a dead repair.
# Verification polls while the port is still unbound (never while a disagreeing
# owner is already visible — that is an immediate verdict).
PROXY_REPAIR_VERIFY_WAIT_S = float(
    os.environ.get("HSCC_PROXY_REPAIR_VERIFY_WAIT_S", "20"))


def _proxy_relaunch_streak():
    """Consecutive failed relaunch attempts, read DURABLY from the stream state.

    Same durability rule as the cooldown stamp and the F5 breaker ledger: the
    incident's counters were state-file based and survived — a stall counter
    that reset on daemon restart could never alert during a crash-looping
    outage. Cleared only by the healthy branch (an exit code is not success)."""
    from .state import read_state
    prev = read_state("proxy")
    if not isinstance(prev, dict):
        return 0
    try:
        return max(0, int(prev.get("relaunch_streak") or 0))
    except (TypeError, ValueError):
        return 0


def _proxy_stall_stamp():
    """Raw `stall_alerted_at` stamp from the proxy state file, or None."""
    from .state import read_state
    prev = read_state("proxy")
    if not isinstance(prev, dict):
        return None
    stamp = prev.get("stall_alerted_at")
    return stamp or None


def _seconds_since_stamp(stamp):
    """Seconds elapsed since an ISO stamp; None if missing/unparsable."""
    if not stamp:
        return None
    try:
        then = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def _proxy_relaunch_cooldown_state():
    """Seconds since the last proxy relaunch attempt, read DURABLY.

    PR #22 review: a persistently-missing alias must not hammer `sparkrun
    proxy start` every check tick. The stamp lives in the proxy stream state
    file (written by check_proxy itself), NOT in module memory — a daemon
    restart must not reset the cooldown (the in-memory state flaw). Returns
    None when there is no recorded relaunch (or the timestamp is unreadable),
    meaning "no cooldown".
    """
    return _seconds_since_stamp(_proxy_relaunch_stamp())


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



# ── Proxy live-vs-healthy backend comparison (F4, HEAL_CASCADE 2026-10-14) ──
#
# The incident's second failure: after recovery, :4000 was an ORPHAN litellm
# routing to ONE backend (.246) while `sparkrun proxy status` and `proxy sync`
# both reported "in sync" — because both read the CONFIG FILE on disk, which
# listed all 4. `proxy start --restart` did not kill the orphan either: the new
# instance silently bound :1708, so the orphan kept :4000 and the gateway check
# stayed OK for ~2 hours while all load went to one node.
#
# Root cause, read out of sparkrun's own source (we do not edit it):
# ProxyEngine.apply_desired_state() writes the config, then returns early
#
#     if not self.is_running():
#         logger.debug("Proxy not running — new config applies on next start")
#         return added, removed
#
# and is_running() is only `process_exists(_read_pid())` — the STATE-FILE PID,
# never the actual owner of the port. So with a live proxy on :4000 that sparkrun
# does not recognise (PID file lost / PID reused / second instance bound another
# port), every auto-discover sweep REWRITES the config and then skips the restart
# that would have applied it. That is exactly the observed "Auto-discover enabled
# (every 30s)" + config file holding all 4 backends + live router serving one.
#
# hscc cannot fix sparkrun's state file, but it can stop trusting it: compare
# what the RUNNING proxy actually routes to against the units that are actually
# healthy, and repair when they disagree.


def _proxy_backend_probe(url=None, timeout=5):
    """GET the RUNNING proxy's /v1/model/info and return the api_base set it
    actually routes to, as {"host:port", ...}.

    /v1/model/info is the only endpoint that reveals the LIVE router: it returns
    each model's ``litellm_params.api_base``. It answers without auth on the
    sparkrun-launched proxy (verified against the running fleet). Note this is
    deliberately NOT /v1/models — that lists model ids only, so the 2026-10-11
    "200 with empty catalogue" check cannot see a proxy that serves the right
    aliases through ONE backend.

    Returns {"ok": bool, "backends": set, "error": str}. ok=False means "could
    not ask the proxy" (caller must NOT treat that as drift).
    """
    url = url or f"http://localhost:{PROXY_PORT}/v1/model/info"
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        body = req.read()
    except Exception as e:
        return {"ok": False, "backends": set(), "error": str(e)}
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "backends": set(),
                "error": f"unparseable /v1/model/info body: {e}"}
    backends = set()
    for m in (data.get("data") or []):
        base = ((m.get("litellm_params") or {}).get("api_base")) or ""
        hostport = _hostport_from_api_base(base)
        if hostport:
            backends.add(hostport)
    return {"ok": True, "backends": backends, "error": ""}


def _hostport_from_api_base(api_base):
    """'http://10.0.0.2:8001/v1' -> '10.0.0.2:8001'; '' when unparseable.

    Hostnames and ports are kept verbatim so the comparison never has to
    resolve anything (a proxy may legitimately use a name or a loopback).
    """
    if not api_base:
        return ""
    try:
        parsed = urllib.parse.urlparse(api_base)
    except Exception:
        return ""
    host = parsed.hostname
    if not host:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


def _fleet_unit_backends(serving_data=None, probe=None):
    """(healthy_backends, fleet_hostports) for the units the proxy should route to.

    ``healthy_backends`` = {"node:port"} for every keep-alive worker whose
    /health answers 200 RIGHT NOW, plus the orchestrator node when serving.json
    declares a uniform pool (that is when the proxy is expected to route
    orchestrator-model). A unit still loading is absent from this set, which is
    what keeps a slow load from looking like drift.

    ``fleet_hostports`` = every node:port hscc owns. Backends outside this set
    are ignored by the comparison — the proxy may be configured with endpoints
    hscc does not supervise, and demanding them would relaunch the LB forever.
    """
    if probe is None:
        probe = http_check
    if serving_data is None:
        serving_data = serving.load_serving()
    healthy, all_hp = set(), set()
    try:
        units = serving.keepalive_units(serving_data) or []
    except Exception:
        units = []
    for u in units:
        node, port = u.get("node"), u.get("port")
        if not node or not port:
            continue
        all_hp.add(f"{node}:{port}")
        if node in serving.ORCH_NODES:
            continue
        if probe(f"http://{node}:{port}/health", timeout=5).get("ok"):
            healthy.add(f"{node}:{port}")
    if serving.is_uniform_pool(serving_data):
        try:
            orch_port = int(serving.serving_port(serving_data) or serving.VLLM_PORT)
        except Exception:
            orch_port = serving.VLLM_PORT
        for node in serving.ORCH_NODES:
            all_hp.add(f"{node}:{orch_port}")
            if probe(f"http://{node}:{orch_port}/health", timeout=5).get("ok"):
                healthy.add(f"{node}:{orch_port}")
    return healthy, all_hp


def _proxy_backend_drift(live_backends, healthy_backends, fleet_hostports):
    """(healthy, reason) — does the RUNNING proxy route to what is actually up?

    Only two directions matter, both scoped to hscc's own fleet:
      * MISSING: a unit is healthy but the live router has no backend for it —
        the incident shape (3 units up, proxy routed to 1) and the node-reboot
        shape (rebooted nodes came back healthy, the running proxy never
        re-added them).
      * DEAD: the live router points at a fleet backend whose unit is NOT
        healthy — load is being sent to a dead worker.
    An empty live set is NOT drift here: the catalogue check already owns
    "proxy answers but serves nothing", and reporting it twice would make one
    fault look like two.
    """
    if not live_backends:
        return True, "no live backends to compare (catalogue check owns that)"
    scoped_fleet = set(fleet_hostports)
    live_in_fleet = {b for b in live_backends if b in scoped_fleet}
    missing = set(healthy_backends) - live_in_fleet
    if missing:
        return False, (f"live router missing backend(s) {sorted(missing)} "
                       f"— healthy unit(s) the proxy is not serving")
    dead = live_in_fleet - set(healthy_backends)
    if dead:
        return False, (f"live router points at unhealthy backend(s) {sorted(dead)} "
                       f"— load is being sent to a dead worker")
    return True, (f"live router matches {len(healthy_backends)} healthy "
                  f"backend(s)")


def _proc_cmdline(pid):
    """The command line of `pid` (stripped), or None if unreadable.

    Seam for the proxy heal paths: any signal we send must be vetted against
    what the process actually IS, not against a PID number.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5).stdout
        return out.strip() or None
    except Exception:
        return None


def _proxy_listener():
    """(pid, cmdline) of the process LISTENING on PROXY_PORT, or (None, None).

    Same lsof+ps technique as hscc-api/gateway_bridge._listener_cmdline: the
    port owner is what actually serves, so it is the only thing worth trusting.
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{PROXY_PORT}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5).stdout
        pids = [int(p) for p in out.split() if p.strip().isdigit()]
        if not pids:
            return None, None
        return pids[0], _proc_cmdline(pids[0])
    except Exception:
        return None, None


def _proxy_tracked_pid():
    """PID sparkrun believes owns the proxy (from its state file), or None.

    Read through sparkrun's own `proxy status --json` rather than by parsing its
    state file — sparkrun owns that file's format and we must not fork it.
    """
    res = run_cmd(sparkrun_bin.argv("proxy", "status", "--json"), timeout=30)
    if not res.get("ok"):
        return None
    try:
        data = json.loads(res.get("output") or "{}")
    except Exception:
        return None
    pid = data.get("pid")
    return int(pid) if isinstance(pid, int) and pid > 0 else None


def _reap_orphan_proxy_listener(tracked_pid):
    """SIGTERM a listener on PROXY_PORT that sparkrun does not track.

    Guards, in order, all required before any signal is sent:
      1. no listener -> nothing to do (returns True, port is free).
      2. the listener IS the tracked PID -> not an orphan, do NOT touch it
         (that is sparkrun's own process and it can stop it).
      3. the cmdline must mention litellm -> we kill only the thing we mean.
         A foreign listener (a second hscc-api, a dev server) is reported and
         left alone, exactly like the 2.8.0 orphaned-serve heal's contract.

    Returns True when the port ends up free.
    """
    pid, cmdline = _proxy_listener()
    if pid is None:
        return True
    if tracked_pid is not None and pid == tracked_pid:
        log(f"proxy listener on :{PROXY_PORT} is the tracked PID {pid} — "
            f"not an orphan, leaving it to sparkrun")
        return False
    if not cmdline or "litellm" not in cmdline.lower():
        log(f"proxy listener on :{PROXY_PORT} is PID {pid} but its cmdline is "
            f"not litellm ({cmdline!r}) — refusing to kill an unrecognised "
            "process", "WARN")
        return False
    log(f"reaping ORPHAN proxy listener PID {pid} on :{PROXY_PORT} "
        f"(sparkrun tracks {tracked_pid}): {cmdline[:120]}", "WARN")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception as e:
        log(f"could not SIGTERM orphan proxy PID {pid}: {e}", "ERROR")
        return False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if _proxy_listener()[0] is None:
            return True
        time.sleep(0.2)
    log(f"orphan proxy PID {pid} survived SIGTERM — escalating to SIGKILL", "WARN")
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception as e:
        log(f"could not SIGKILL orphan proxy PID {pid}: {e}", "ERROR")
        return False
    time.sleep(0.5)
    return _proxy_listener()[0] is None


def _proxy_repair_orphan(relaunch_hosts):
    """Full orphan repair: stop, reap any untracked litellm listener, start.

    `proxy start --restart` was PROVEN unable to fix the incident state: the
    orphan was not the tracked PID, so sparkrun SIGTERMs nothing that matters
    and the new instance silently binds another port (:1708) while the orphan
    keeps :4000. So the order here is deliberate —
      1. `sparkrun proxy stop` (the polite path, when sparkrun does track it),
      2. reap a listener sparkrun does NOT track, cmdline-vetted first,
      3. `sparkrun proxy start --hosts <fleet>` — plain start, no --restart:
         after 1+2 the port is genuinely free, and --restart's own state-file
         assumptions are what failed us.
      4. verify the port is now owned by the tracked PID.
    """
    tracked_before = _proxy_tracked_pid()
    run_cmd(sparkrun_bin.argv("proxy", "stop"), timeout=60)
    reaped = _reap_orphan_proxy_listener(tracked_before)
    if not reaped:
        owner, cmd = _proxy_listener()
        if owner is not None:
            log(f"proxy listener on :{PROXY_PORT} still present after reap "
                f"(PID {owner}, {cmd!r}) — starting anyway; the drift check "
                "will re-detect it next tick", "WARN")
    args = ["proxy", "start", "--cluster", serving.HSCC_CLUSTER,
            "--hosts", ",".join(relaunch_hosts), "--port", str(PROXY_PORT)]
    res = run_cmd(sparkrun_bin.argv(*args), timeout=90)
    # VERIFY, do not trust exit code: exit 0 over a proxy that bound a
    # different port is exactly how this incident stayed invisible.
    # BOUNDED WAIT first: `proxy start` returns after sparkrun's own 2 s
    # startup grace, and litellm binds the port seconds later — the 2026-10-18
    # 01:41:42 "repair UNVERIFIED — port owner None" line was this race. Poll
    # only while the port is unbound; a visible owner that disagrees is an
    # immediate verdict, not something to wait on.
    deadline = time.time() + max(0.0, PROXY_REPAIR_VERIFY_WAIT_S)
    owner, owner_cmd = _proxy_listener()
    while owner is None and time.time() < deadline:
        time.sleep(1.0)
        owner, owner_cmd = _proxy_listener()
    tracked_after = _proxy_tracked_pid()
    verified = (owner is not None and tracked_after is not None
                and owner == tracked_after)
    if not verified:
        log(f"proxy repair UNVERIFIED on :{PROXY_PORT} — port owner {owner}, "
            f"sparkrun tracks {tracked_after} ({owner_cmd!r})", "WARN")
    else:
        log(f"proxy repaired on :{PROXY_PORT}, owned by tracked PID {owner}")
    return {"ok": bool(res.get("ok")) and verified,
            "port_owner": owner, "tracked_pid": tracked_after,
            "verified": verified, "output": res.get("output", "")}


def _router_drift_probe():
    """Seam: compare proxy.yaml router intent vs the live litellm_config.

    Thin wrapper over :func:`hscc_daemon.proxy_router.router_drift` so tests
    (and nothing else) can stub it — health tests must never read the
    operator's live ~/.config/sparkrun (ambient-state discipline, same as the
    serving.load_serving stub in TestCheckProxy._setup).
    """
    from . import proxy_router
    return proxy_router.router_drift()


def _proxy_kill_hollow_tracked(reason):
    """SIGTERM (then SIGKILL) a tracked proxy PID that is alive but not serving.

    THE HOLLOW-PID DEFECT (t_43b6e245): sparkrun restarts signal only the
    tracked PID, which for ``uvx litellm`` is a *wrapper* — the real socket
    owner is a child. A SIGTERM'd wrapper can leave the child orphaned on the
    port, and the replacement wrapper can stay alive without ever binding
    (EADDRINUSE). ``is_running()`` (state-file PID + ``os.kill(pid, 0)``) then
    reads "running" forever and every plain ``proxy start`` refuses. The only
    hscc-side way out is to retire the hollow PID itself, so sparkrun's state
    stops pinning the stream.

    Guards, same contract as :func:`_reap_orphan_proxy_listener`:
      1. no tracked PID -> nothing to do.
      2. the process must exist (a dead PID is sparkrun's own stale-state case,
         not ours to signal).
      3. the cmdline must mention litellm — we kill only the thing we mean;
         PID reuse by a foreign process is refused and reported.

    Returns True when the PID is gone (killed or already exited).
    """
    pid = _proxy_tracked_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except Exception as e:
        log(f"proxy escalation: could not inspect tracked PID {pid}: {e}",
            "ERROR")
        return False
    cmd = _proc_cmdline(pid) or ""
    if "litellm" not in cmd.lower():
        log(f"proxy escalation: tracked PID {pid} is alive but its cmdline is "
            f"not litellm ({cmd!r}) — refusing to kill an unrecognised "
            "process", "ERROR")
        return False
    log(f"proxy escalation: killing HOLLOW tracked proxy PID {pid} "
        f"({reason}): {cmd[:120]}", "WARN")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception as e:
        log(f"proxy escalation: could not SIGTERM tracked PID {pid}: {e}",
            "ERROR")
        return False
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.25)
    log(f"proxy escalation: hollow tracked PID {pid} survived SIGTERM — "
        "escalating to SIGKILL", "WARN")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception as e:
        log(f"proxy escalation: could not SIGKILL tracked PID {pid}: {e}",
            "ERROR")
        return False
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def _proxy_escalated_repair(relaunch_hosts, streak, last_error, reason):
    """The post-threshold repair: retire the hollow PID, then the proven path.

    Runs only after PROXY_RELAUNCH_MAX_CONSECUTIVE consecutive failed
    attempts, still under the heal lease and breaker gate. Order matters:
    killing the hollow tracked PID FIRST (so sparkrun's state stops refusing
    starts) -> `proxy stop` inside _proxy_repair_orphan clears that stale
    state -> reap of any orphaned listener holding the port -> plain start ->
    port-owner verification. Never `--restart`: it SIGTERMs the same wrapper
    that is the problem.
    """
    log(f"proxy relaunch STALLED after {streak} consecutive failures — "
        f"escalating to orphan repair on :{PROXY_PORT} "
        f"(last error: {(last_error or reason or '')[:200]})", "ERROR")
    _proxy_kill_hollow_tracked(f"{streak} consecutive failed relaunches")
    return _proxy_repair_orphan(relaunch_hosts)


def _proxy_stall_alert(relaunch_hosts, streak, last_error, reason):
    """Loud operator alert for a stalled relaunch loop — throttled.

    Desktop notify (priority high) + a durable events.jsonl row, exactly the
    F5 breaker's alerting shape: a direct notify rather than a trigger rule,
    because the live ~/.hscc/triggers.json only gains a rule on a fresh
    bootstrap and a critical alert that silently depends on a missing rule is
    the 2026-10-08 NAS class of failure. Throttle lives in the stream state
    (`stall_alerted_at`), so it survives a daemon restart: the 3h incident
    alerts every PROXY_STALL_NOTIFY_COOLDOWN_S, never once per 5-min tick.
    """
    since = _seconds_since_stamp(_proxy_stall_stamp())
    if since is not None and since < PROXY_STALL_NOTIFY_COOLDOWN_S:
        return False, None
    msg = (f"Proxy :{PROXY_PORT} down and {streak} relaunches failed — "
           f"escalated repair in progress. Reason: {reason}. "
           f"Last error: {(last_error or 'none')[:300]}. "
           f"If this repeats, sparkrun's tracked PID is hollow: "
           f"`sparkrun proxy stop` then `sparkrun proxy start --hosts "
           f"{','.join(relaunch_hosts or [])} --port {PROXY_PORT}`.")
    send_macos_notification("HSCC: proxy relaunch stalled", msg,
                            priority="high")
    emit_event("proxy.relaunch.stalled", {
        "port": PROXY_PORT, "streak": streak, "reason": reason,
        "last_error": (last_error or "")[:400],
        "hosts": list(relaunch_hosts or []),
    }, severity="error")
    return True, now_iso()


def check_proxy():
    """Keep the sparkrun LiteLLM worker proxy alive (worker load-balancer).

    Role-worker profiles + orchestrator subagents reach the worker GPUs through
    this proxy (http://localhost:PROXY_PORT). If it dies, all worker work falls
    back onto the orchestrator. Health-check it and relaunch via `sparkrun proxy
    start` if it's down. No-op when there are no keep-alive workers configured.

    Health is NOT mere reachability (2026-10-11 outage): a litellm proxy can
    answer 200 with an EMPTY model catalogue, which serves nobody. Healthy
    requires the aliases serving.json says the fleet advertises FOR THE HOSTS
    THE PROXY IS POINTED AT (PR #22 review) — non-uniform fleets reach only
    keep-alive workers, so orchestrator-model is not required there; uniform
    pools require both role aliases; >=1 model when serving.json is absent.
    Relaunch hosts follow :func:`_proxy_relaunch_hosts` — under a uniform_pool
    they MUST include the orchestrator node, or the relaunched LB can never
    recover the orchestrator's endpoint.
    Relaunch attempts are throttled by a DURABLE cooldown
    (:func:`_proxy_relaunch_cooldown_state`, HSCC_PROXY_RELAUNCH_COOLDOWN_S
    default 300s, stamped in the proxy state file): EVERY attempt stamps the
    window — `proxy start` exiting 0 over still-empty discovery (the
    2026-10-11 shape) must not relaunch every tick. The healthy branch
    rewrites the state WITHOUT the stamp, so a recovered proxy clears the
    cooldown and a later genuine down-proxy recovers on its next tick.
    """
    from .state import write_state

    log("Running proxy check")
    nodes = sorted(serving.KEEPALIVE_NODES)
    if not nodes:
        write_state("proxy", {"ok": True, "message": "no workers — proxy not needed",
                              "last_check": now_iso()})
        return True

    serving_data = serving.load_serving()
    url = f"http://localhost:{PROXY_PORT}/v1/models"
    # Router-settings drift (dep-bump guard, WARN-only): does the generated
    # litellm_config still carry what ~/.config/sparkrun/proxy.yaml asks for?
    # Computed BEFORE the healthy branch so BOTH state writes record it — a
    # drift must survive on the healthy path too (the exact 2026-10-15 failure
    # shape is a proxy that serves fine yet routes blind after a bump dropped
    # the fork branch). Pure-read (proxy_router.router_drift); never writes,
    # never restarts; a raising probe must never take the proxy check down.
    try:
        drift = _router_drift_probe()
    except Exception as exc:  # pragma: no cover - defensive, check never raises
        drift = {"status": "unverified", "warn": False,
                 "detail": f"router drift probe raised {type(exc).__name__}"}
    # Host set FIRST: it is what `proxy start --hosts` points the LB at, so
    # the expected-alias requirement below must be scoped to it (PR #22 review
    # — expecting an alias the host set can never serve = relaunch forever).
    relaunch_hosts = _proxy_relaunch_hosts(serving_data)
    probe = _proxy_models_probe(url, timeout=5)
    expected = _proxy_expected_aliases(serving_data, relaunch_hosts)
    healthy, reason = _proxy_models_healthy(probe, expected)
    # PER-UNIT ALIAS COVERAGE (t_be4b94ca addendum). The catalogue verdict
    # above is proxy-level: one correctly-launched unit masks its siblings'
    # missing --served-model-name aliases (2026-10-22: two units served only
    # the base name; the proxy stayed "healthy" and all aliased traffic
    # funneled to the third node for ~2h). Ask each unit DIRECTLY, then
    # report — do NOT flip proxy health: the proxy really is healthy in that
    # shape, and a relaunch fixes nothing. Pure-read + never raises; the
    # WARN names each unit and its missing aliases.
    try:
        unit_alias = _unit_alias_check_fn(serving_data=serving_data)
    except Exception as exc:  # pragma: no cover - defensive, check never raises
        unit_alias = {"ok": None, "findings": [], "unverified": [],
                      "message": f"unit alias check raised {type(exc).__name__}"}
    unit_alias_warn = bool(unit_alias.get("findings"))
    if unit_alias_warn:
        for f in unit_alias["findings"]:
            log(f"unit alias coverage: {f}", "WARN")
    elif unit_alias.get("unverified"):
        log(f"unit alias coverage: {unit_alias['message']}", "WARN")
    # LIVE-vs-HEALTHY (F4). A catalogue that serves every expected alias can
    # still be routing ALL of it through ONE backend — the incident shape — and
    # the alias check above cannot see that, because /v1/models lists ids and
    # never reveals api_base. Ask the running proxy what it actually routes to.
    backend_drift = False
    if healthy:
        live = _proxy_backend_probe(timeout=5)
        if not live.get("ok"):
            # Could not ask the proxy. The catalogue probe just succeeded, so
            # this is unusual (endpoint disabled/changed); it is NOT evidence of
            # drift, and failing closed here would restart a working LB.
            log(f"proxy backend drift check skipped — /v1/model/info "
                f"unavailable: {live.get('error')}", "WARN")
        else:
            # timeout 2s, not the 5s the workers check uses: this is a
            # consistency check, and a unit that cannot answer /health in 2s is
            # not a backend we should DEMAND from the router. Being conservative
            # about what we demand is what keeps a slow-but-alive unit from
            # triggering a needless proxy restart; the authoritative down
            # verdict (with load grace + debounce) stays with check_workers.
            healthy_units, fleet_hp = _fleet_unit_backends(
                serving_data, probe=lambda u, timeout=5: http_check(u, timeout=2))
            drift_ok, drift_reason = _proxy_backend_drift(
                live.get("backends") or set(), healthy_units, fleet_hp)
            if not drift_ok:
                backend_drift = True
                healthy, reason = False, drift_reason
    if healthy:
        write_state("proxy", {"ok": True, "port": PROXY_PORT,
                              "last_check": now_iso(), "message": reason,
                              "router_drift": drift["detail"],
                              "router_drift_warn": bool(drift["warn"]),
                              "unit_alias_warn": unit_alias_warn,
                              "models": sorted(probe.get("models") or [])})
        if drift["warn"]:
            log(f"proxy router drift ({drift['detail']})", "WARN")
        return True

    log(f"Worker proxy unhealthy on :{PROXY_PORT} ({reason})")
    # Durable cooldown (PR #22 review): a persistently-missing alias must not
    # hammer `sparkrun proxy start` — every attempt runs a full SSH endpoint
    # discovery sweep and spams WARN once per check tick even when it is a
    # no-op (plain start REFUSES a running proxy; sparkrun engine.py start()
    # `if self.is_running(): return 1`, cli exits 1 on ProxyAlreadyRunning —
    # it only SIGTERMs the live proxy under --restart).
    # The stamp lives in the proxy stream state file, NOT in memory, so a
    # daemon restart cannot reset it.
    restart = _proxy_needs_restart(probe)
    since = _proxy_relaunch_cooldown_state()
    r = {"ok": False, "skipped": "cooldown"}
    suppressed = False
    deferred = False
    attempted = False
    if since is not None and since < PROXY_RELAUNCH_COOLDOWN_S:
        suppressed = True
        wait = int(PROXY_RELAUNCH_COOLDOWN_S - since)
        log(f"proxy relaunch suppressed — attempted {int(since)}s ago "
            f"(cooldown {PROXY_RELAUNCH_COOLDOWN_S}s); retry in {wait}s", "WARN")
        message = f"relaunch cooldown — retry in {wait}s"
    else:
        # ONE REPAIR AT A TIME (F2, HEAL_CASCADE 2026-10-14). The proxy relaunch
        # was one of the four paths that fired on a single wedged unit, and the
        # loudest of them: `sparkrun proxy start --hosts <all 4> --restart`
        # SIGTERMs the live LB while three other mechanisms are mid-repair. Take
        # the durable heal lease before acting; if another repair holds it, log
        # what is in flight and back off THIS tick.
        #
        # Claimed only here (not before the cooldown test) so a cooldown tick
        # neither claims nor releases anything, and a lease is held only for the
        # duration of an actual `proxy start`.
        lease_ok, holder = heal_lease.acquire(
            "proxy-relaunch", f"worker proxy on :{PROXY_PORT}")
        if not lease_ok:
            deferred = True
            message = f"deferred — heal in progress: {heal_lease.describe(holder)}"
            log(f"proxy relaunch deferred — {message}", "WARN")
        else:
            # F5 breaker gate, AFTER the lease so a lease-deferred tick (no
            # action attempted) never counts a strike — same semantics as the
            # worker sites. Keyed on the host the relaunch leads with: this path
            # issues ONE command against the whole fleet, so one strike per
            # storm-on-a-node is the faithful count, and tripping any single
            # host correctly stops the fleet-wide hammering.
            allowed, note = _note_heal_action(
                (relaunch_hosts or ["?"])[0], PROXY_PORT, "proxy-relaunch",
                f"proxy:{PROXY_PORT}")
            if not allowed:
                deferred = True
                message = note
                log(f"proxy relaunch {note}", "WARN")
            else:
                # LOUD + ESCALATING (t_43b6e245): the 2026-10-18 incident ran
                # ~36 plain `sparkrun proxy start` attempts across 3h09m while
                # a hollow tracked PID made every one refuse, and the ONLY
                # trace was `relaunching with [...]` — the command's stderr
                # ("already running ... Use --restart") was discarded.
                # The streak counts CONSECUTIVE TICKS THAT ATTEMPTED A REPAIR
                # WITHOUT RESTORING SERVICE (this branch only runs when the
                # probes already failed) and lives in the state file, so it —
                # like the alert throttle and the F5 ledger — survives a daemon
                # restart. Only the healthy branch clears it: an exit code is
                # not success (rc-0-over-still-empty-discovery is the
                # 2026-10-11 shape).
                attempted = True
                prev_streak = _proxy_relaunch_streak()
                # This tick is attempt number prev_streak+1. The MAX-th
                # consecutive stalled attempt escalates (two polite plain
                # starts, then the repair that clears the hollow PID) and the
                # same tick raises the stall alarm.
                escalated = (prev_streak + 1) >= PROXY_RELAUNCH_MAX_CONSECUTIVE
                try:
                    if backend_drift:
                        # F4: the catalogue is fine but the live router disagrees with
                        # what is healthy, so `proxy start` alone cannot help — plain
                        # start REFUSES a running proxy (sparkrun returns 1 on
                        # ProxyAlreadyRunning) and --restart only SIGTERMs the PID in
                        # sparkrun's OWN state file, which is precisely what was wrong
                        # (the orphan kept :4000 while the new instance bound :1708).
                        # Needs stop + cmdline-vetted reap + start + verification.
                        log(f"repairing proxy backend drift on :{PROXY_PORT} — {reason}",
                            "WARN")
                        r = _proxy_repair_orphan(relaunch_hosts)
                        message = ("repaired" if r.get("ok")
                                   else "repair incomplete (unverified)")
                    elif escalated:
                        # Carry the PREVIOUS attempt's surfaced error into the
                        # escalation banner — the streak that triggered this
                        # repair came from those attempts, not from this one.
                        from .state import read_state as _rs
                        _prev = _rs("proxy") or {}
                        r = _proxy_escalated_repair(
                            relaunch_hosts, prev_streak,
                            _prev.get("relaunch_error") if isinstance(_prev, dict) else "",
                            reason)
                        message = ("escalated repair" if r.get("ok")
                                   else "escalated repair incomplete (unverified)")
                    else:
                        log(f"relaunching with {relaunch_hosts}"
                            f"{' (--restart: running proxy serves nothing)' if restart else ''}"
                            f" (attempt {prev_streak + 1} of "
                            f"{PROXY_RELAUNCH_MAX_CONSECUTIVE} before stall alarm)", "WARN")
                        args = ["proxy", "start", "--cluster", serving.HSCC_CLUSTER,
                                "--hosts", ",".join(relaunch_hosts), "--port", str(PROXY_PORT)]
                        if restart:
                            args.append("--restart")
                        r = run_cmd(sparkrun_bin.argv(*args), timeout=90)
                        message = "relaunched" if r.get("ok") else "relaunch failed"
                finally:
                    heal_lease.release("proxy-relaunch")
    ok = bool(r.get("ok"))
    # A relaunch/repair that came back failed must SURFACE the underlying
    # command output — every failure of this path in the 2026-10-18 window was
    # logged as bare text without the reason ("already running" etc.), and
    # nothing downstream could alarm on it.
    if not ok and not suppressed and not deferred:
        log(f"proxy repair on :{PROXY_PORT} FAILED ({message}): "
            f"{(r.get('output') or r.get('stderr') or 'no command output')[:400]}",
            "ERROR")
    new_streak = _proxy_relaunch_streak() + (1 if attempted else 0)
    alert_stamp = None
    # Alert when a repair attempt FAILED and the stream has now been
    # unrecoverable for MAX consecutive attempts. `attempted` keeps
    # cooldown/lease/breaker-deferred ticks (which did nothing) out of the
    # count and out of the alarm; `not ok` keeps the moment an escalated
    # repair SUCCEEDS from raising a false alarm on its own tick. The
    # intentional-autodown window never alerts.
    if attempted and not ok and new_streak >= PROXY_RELAUNCH_MAX_CONSECUTIVE \
            and not _intentional_window():
        alerted, alert_stamp = _proxy_stall_alert(
            relaunch_hosts, new_streak, r.get("output") or "", reason)
        if alerted:
            message = (message + f" — STALLED after {new_streak} attempts, "
                                 "operator notified")
    proxy_data = {"ok": ok, "port": PROXY_PORT, "relaunched": ok,
                  "last_check": now_iso(),
                  "reason": reason,
                  "restart": restart,
                  "backend_drift": backend_drift,
                  "relaunch_hosts": relaunch_hosts,
                  # A deferral is not a FAILED relaunch — nothing was attempted.
                  # Same treatment as `suppressed`, so a deferred tick cannot
                  # raise a "relaunch failed" alert.
                  "relaunch_failed": (not ok and not suppressed and not deferred),
                  "deferred": deferred,
                  "router_drift": drift["detail"],
                  "router_drift_warn": bool(drift["warn"]),
                  "unit_alias_warn": unit_alias_warn,
                  "message": message}
    if not ok and not suppressed and not deferred:
        proxy_data["relaunch_error"] = (r.get("output") or r.get("stderr") or "")[:400]
    if new_streak:
        # NOT cleared by a merely-successful exit code: only the healthy
        # branch (probes pass) clears the streak. `sparkrun proxy start`
        # exiting 0 over a dead/empty proxy is a documented shape (10-11).
        proxy_data["relaunch_streak"] = new_streak
    if suppressed or deferred:
        # Carry the ORIGINAL attempt stamp forward verbatim. Re-stamping on a
        # suppressed/deferred tick would push the window out every tick and make
        # the cooldown perpetual (never retries, never recovers).
        stamp = _proxy_relaunch_stamp()
        if stamp:
            proxy_data["last_relaunch"] = stamp
        # Same rule for the stall-alert throttle: re-stamping it on a tick that
        # alerted nothing would push the notify window out forever and the
        # operator would never hear about a 3-hour outage.
        if not alert_stamp:
            alert_stamp = _proxy_stall_stamp()
    else:
        # EVERY attempt stamps the cooldown — success or failure (PR #22
        # round 2). `sparkrun proxy start` exiting 0 does NOT mean the fleet
        # is served: the exact 2026-10-11 shape is a start that succeeds
        # while discovery is still empty, so the NEXT probe is still
        # unhealthy — stamping only failures left that path relaunching
        # every tick all over again. The healthy branch rewrites the state
        # WITHOUT the stamp, so a genuinely recovered proxy clears the
        # cooldown on its own and stays fast to recover later.
        proxy_data["last_relaunch"] = now_iso()
    if alert_stamp:
        proxy_data["stall_alerted_at"] = alert_stamp
    elif not ok:
        # Throttled or suppressed-by-intentional-window alert: carry the
        # EXISTING stamp forward on every unhealthy write. Dropping it on a
        # throttled attempt tick would disarm the throttle entirely — the
        # next 5-min tick would notify again (the 2026-10-08 NAS 178-notification
        # storm is that exact class).
        prev_alert = _proxy_stall_stamp()
        if prev_alert:
            proxy_data["stall_alerted_at"] = prev_alert
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
    spark_r = run_cmd(sparkrun_bin.argv("--version"), timeout=3)
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


# ── Worker container-age state (startup grace) — persisted ────────────────
# The startup grace keys on container creation time + a has-served flag, both
# PERSISTED so a still-loading unit is never auto-healed across a daemon
# restart (the debounce resets; container age does not).

def _load_worker_container_state():
    """Load persisted per-unit container-age state from disk (best-effort).

    Entries whose created_ts is None are DROPPED: they carry no usable age.
    They were written by the inert pre-sparkrun grace (which could never
    resolve a remote container's age) and by old tests (placeholder nodes).
    The fail-closed path re-derives a real created_ts for any unit that is
    genuinely down, so dropping a null entry loses no signal and the file no
    longer carries dead rows. If any are dropped the cleaned dict is rewritten
    to disk once.
    """
    dropped = False
    try:
        with open(_WORKER_CONTAINER_STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            cleaned = {}
            for key_str, rec in data.items():
                try:
                    parts = key_str.strip("()").split(",")
                    key = (parts[0], int(parts[1]))
                except (ValueError, IndexError):
                    continue
                created_ts = rec.get("created_ts")
                if created_ts is None:
                    dropped = True          # stale null entry — drop
                    continue
                cleaned[key] = {
                    "created_ts": created_ts,
                    "has_served": bool(rec.get("has_served")),
                }
            _worker_container_state.update(cleaned)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    if dropped:
        _save_worker_container_state()


def _save_worker_container_state():
    """Persist per-unit container-age state to disk (best-effort)."""
    try:
        serializable = {
            f"({k[0]},{k[1]})": v for k, v in _worker_container_state.items()
        }
        tmp = _WORKER_CONTAINER_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(serializable, f)
        os.replace(tmp, _WORKER_CONTAINER_STATE_FILE)
    except OSError:
        pass


# ── Worker container-age helper (startup grace) ───────────────────────────
# Injectable (like _autoheal_worker_fn) so tests can control the age signal
# without touching a real node/docker. The real implementation resolves the
# unit's container age through SPARKRUN's structured status (the sanctioned
# path — HSCC never shells to docker/ssh directly): the (host, port) → launch
# epoch map below. A sparkrun sweep costs up to ~25s, so the map is cached
# module-wide with a short TTL; the down-threshold path only refreshes when
# stale, so a slow/unreachable node never stalls the health cycle.

# (host, port) -> launch-epoch cache built from sparkrun's status to_dict().
_WORKER_CREATED_CACHE: dict = {}
_WORKER_CREATED_CACHE_TTL = 30.0           # seconds; one sweep per check cycle
_WORKER_CREATED_CACHE_AT = 0.0
# check_workers and check_dgx run on separate daemon threads and share this
# cache; the lock keeps a refresh from racing (a redundant ~25s sweep, or a
# partial clear/update) when both see it stale at once.
_WORKER_CREATED_LOCK = threading.Lock()


def _sparkrun_created_map():
    """Return {(host, port): launch_epoch} from sparkrun's corrected status.

    The launch epoch (meta.started_at from job metadata) is rewritten by a
    fresh ``sparkrun run`` — which is exactly what a force-recreate does — so
    it is a faithful proxy for "this unit's container was (re)created at T".
    Built from ClusterStatusResult.to_dict(): groups[*].containers[*].host +
    meta.port → meta.started_at, and solo_entries[*].host + meta.port →
    meta.started_at. Cached with _WORKER_CREATED_CACHE_TTL. Best-effort: any
    failure returns the existing cache (stale data beats none on the down
    path) — never {} overwriting populated entries.
    """
    now = time.time()
    global _WORKER_CREATED_CACHE_AT
    if now - _WORKER_CREATED_CACHE_AT < _WORKER_CREATED_CACHE_TTL:
        return _WORKER_CREATED_CACHE
    with _WORKER_CREATED_LOCK:
        # Re-check after acquiring — the other thread may have refreshed.
        if now - _WORKER_CREATED_CACHE_AT < _WORKER_CREATED_CACHE_TTL:
            return _WORKER_CREATED_CACHE
        venv_py = _sparkrun_venv_python()
        if not venv_py:
            return _WORKER_CREATED_CACHE
        try:
            res = run_cmd([venv_py, "-c", _SPARKRUN_STATUS_SCRIPT], timeout=25)
            if not (res.get("ok") and res.get("output")):
                return _WORKER_CREATED_CACHE
            data = json.loads(res["output"])
        except Exception:
            return _WORKER_CREATED_CACHE
        mapping = {}
        for cid, group in (data.get("groups") or {}).items():
            meta = group.get("meta") or {}
            port = meta.get("port")
            started = meta.get("started_at")
            if port is None:
                continue
            for c in group.get("containers") or []:
                host = c.get("host")
                if host:
                    mapping[(host, int(port))] = started
        for entry in data.get("solo_entries") or []:
            meta = entry.get("meta") or {}
            port = meta.get("port")
            started = meta.get("started_at")
            host = entry.get("host")
            if host and port is not None:
                mapping[(host, int(port))] = started
        _WORKER_CREATED_CACHE.clear()
        _WORKER_CREATED_CACHE.update(mapping)
        _WORKER_CREATED_CACHE_AT = now
        return _WORKER_CREATED_CACHE


def _default_worker_container_created_ts(node, port):
    """Epoch-seconds when the unit's container on `node` was (re)created, or
    None if it cannot be determined. Resolved through SPARKRUN's structured
    status (see _sparkrun_created_map) — HSCC never shells to docker/ssh
    directly. The launch epoch is rewritten by a fresh ``sparkrun run``, which
    is exactly what a force-recreate does, so it stays faithful across a
    recreate. Best-effort: unknown (no entry / query failure) returns None and
    the caller decides fail-open vs fail-closed.
    """
    return _sparkrun_created_map().get((node, int(port)))


_worker_container_created_ts = _default_worker_container_created_ts


# --------------------------------------------------------------------------
# A2 — one-vLLM-per-host launch guard (HEAL_CASCADE 2026-10-14, F7 follow-up)
#
# Policy lives in launch_guard.py (pure, unit-testable). What lives here is
# only the I/O: ONE cached sparkrun status fetch shared by every launch site,
# so four heal paths do not each pay a fresh fleet scan, and the alert glue.
# --------------------------------------------------------------------------

_LAUNCH_STATUS_CACHE = {"at": 0.0, "status": None}
_LAUNCH_STATUS_TTL = float(os.environ.get("HSCC_LAUNCH_GUARD_TTL_S", "20"))
_LAUNCH_STATUS_LOCK = threading.Lock()

# (host, port) -> last alert epoch. The refusal returns every heal tick; the
# NOTIFY must not (2026-10-08 NAS: 178 notifications in 11 minutes). The
# refusal itself still logs every time — the alert cooldown is on the push
# channel only, never on the ERROR line.
_LAUNCH_ALERT_AT = {}
_LAUNCH_ALERT_COOLDOWN_S = float(os.environ.get("HSCC_LAUNCH_GUARD_ALERT_COOLDOWN_S", "1800"))


def _sparkrun_cluster_status(force=False):
    """Structured sparkrun status dict, or launch_guard.NO_STATUS on failure.

    Deliberately a DIFFERENT cache from _sparkrun_created_map: that one returns
    a (host,port)->epoch projection and returns a stale cache on failure, which
    is right for age checks but WRONG here — A2's whole point is that "could
    not look" must not read as "the host is free". So a failed fetch returns
    the NO_STATUS sentinel and the caller refuses, and there is no stale-data
    fallback path to fail open through.
    """
    now = time.time()
    if not force and now - _LAUNCH_STATUS_CACHE["at"] < _LAUNCH_STATUS_TTL:
        return _LAUNCH_STATUS_CACHE["status"]
    with _LAUNCH_STATUS_LOCK:
        if not force and now - _LAUNCH_STATUS_CACHE["at"] < _LAUNCH_STATUS_TTL:
            return _LAUNCH_STATUS_CACHE["status"]
        status = launch_guard.NO_STATUS
        venv_py = _sparkrun_venv_python()
        if venv_py:
            try:
                res = run_cmd([venv_py, "-c", _SPARKRUN_STATUS_SCRIPT], timeout=25)
                if res.get("ok") and res.get("output"):
                    status = json.loads(res["output"])
            except Exception as e:
                log(f"launch guard: sparkrun status query failed ({e})", "WARN")
        _LAUNCH_STATUS_CACHE["at"] = now
        _LAUNCH_STATUS_CACHE["status"] = status
        return status


# Tests monkeypatch this name (the seam) rather than the transport.
_launch_guard_status_fn = _sparkrun_cluster_status


def _guard_launch_or_refuse(host, port, span, actor, unit, status=None,
                            declared=None):
    """A2 gate. True = may launch; False = refused (caller must not launch).

    Call sites pass the `span` their command builds (the unit's declared
    `nodes`) and their own `actor` label. `status` lets a caller that already
    fetched (cluster up / autoup fetch once for the whole plan) avoid a second
    scan; None means "fetch it yourself (cached)". `declared` is the
    serving.json footprint (see launch_guard.declared_footprint); None means
    "resolve from the serving module" — a caller holding its own serving dict
    (autoup) should pass it so planner and guard read the SAME document.

    On refusal this function has already logged ERROR, notified (deduped) and
    emitted the events.jsonl row — callers just skip the launch and record the
    skip, like every other gate in this file (F2 lease, F5 breaker).
    """
    if status is None:
        status = _launch_guard_status_fn()
    if declared is None:
        declared = launch_guard.declared_footprint(serving.load_serving())
    ok, reason, detail = launch_guard.evaluate(status, host, port, span,
                                               declared=declared)
    if ok:
        return True
    key = (host, port, reason)
    last = _LAUNCH_ALERT_AT.get(key, 0.0)
    if time.time() - last >= _LAUNCH_ALERT_COOLDOWN_S:
        _LAUNCH_ALERT_AT[key] = time.time()
        # Pass OUR module-level names explicitly: launch_guard's own deferred
        # desktop import would bypass tests that patch health.send_macos_
        # notification / health.emit_event (the F5 alert tests set that
        # precedent), and an unpatched desktop path fires real osascript.
        launch_guard.alert_refusal(actor, unit, reason, detail,
                                   notify=send_macos_notification,
                                   emit=emit_event)
    else:
        # Suppressed push, but the decision must still be visible in the log.
        log(f"launch guard: refused {unit} on {host}:{port} ({reason}: "
            f"{detail.get('error')}) — alert suppressed (cooldown)", "ERROR")
    return False


def _note_heal_action(node, port, actor, label):
    """Count one heal ACTION against `node` and honour the per-node breaker.

    Returns (allowed, note). When the breaker trips or is already suspended,
    the caller must NOT act — it logs `note` and moves on, exactly like the
    F2 lease deferral.

    WHY A BREAKER ON TOP OF THE LEASE (F5, HEAL_CASCADE 2026-10-14): the lease
    makes repairs *sequential*; it does not make them *finite*. The incident's
    killer was four sequential-but-unbounded repair paths firing every ~60s for
    ~35 min against degrading hardware, until two nodes hard-hung and needed a
    manual power-cycle. Counting per-node actions and STOPPING after HEAL_
    BREAKER_MAX is what converts "relentless" into "relentless for a while".

    Counting is durable (heal_breaker writes under ~/.hscc), so — unlike the
    in-memory _worker_down_streak a crash-looping daemon reset every restart —
    the count survives the very restarts healing itself can cause.
    """
    remaining = heal_breaker.suspended(node)
    if remaining > 0:
        return False, (f"node {node} heal-suspended for another {int(remaining)}s "
                       f"after {heal_breaker.strike_count(node)} repair actions — "
                       f"NOT attempting {actor} for {label}")
    res = heal_breaker.note_heal(node)
    if res["tripped"]:
        # LATCH ONCE (heal_breaker.should_alert dedupes; the 2026-10-08 NAS
        # rule fired 178 notifications in 11 min by getting this wrong). A tick
        # that arrives while already suspended returns via `suspended()` above
        # WITHOUT adding a strike — the suspension is self-sustaining (it lasts
        # until the oldest strike ages out of the window), so re-counting would
        # only extend it indefinitely and never let the node recover.
        if heal_breaker.should_alert(node):
            msg = (f"Stopped auto-healing node {node} after {res['count']} "
                   f"repair actions in "
                   f"{int(heal_breaker.HEAL_BREAKER_WINDOW_S)}s — the cluster is "
                   f"not self-healing, it is being destroyed. Refusing further "
                   f"heals for {int(res['suspended_s'])}s. Investigate the node "
                   f"(dmesg/ping) before clearing: "
                   f"hscc heal-clear {node}")
            log(msg, "ERROR")
            # Notify DIRECTLY, like autodown/watchdog/escalate_watcher do. The
            # trigger engine could carry this, but the live ~/.hscc/triggers.json
            # only gets a new rule on a fresh bootstrap — a critical alert that
            # silently depends on a rule that may not exist is the same silent
            # failure that left the 2026-10-08 NAS outage un-noticed.
            try:
                send_macos_notification("HSCC: heal breaker tripped", msg,
                                        priority="high")
            except Exception:
                pass
            # Ledger record regardless (the events.jsonl trail + any rule that
            # does exist still see the trip).
            emit_event("heal_breaker.tripped", {
                "node": node, "strikes": res["count"],
                "window_s": heal_breaker.HEAL_BREAKER_WINDOW_S,
                "suspend_s": res["suspended_s"], "actor": actor, "unit": label,
                "severity": "critical",
            }, source="health")
        return False, (f"node {node} just tripped the heal breaker "
                       f"({res['count']} actions) — NOT attempting {actor} "
                       f"for {label}; operator alerted")
    return True, ""


def _unit_is_still_loading(key, node, port, now_wall, *, fail_closed=False):
    """True if the (node, port) unit is still LOADING, not dead: its container
    is younger than WORKER_AUTOHEAL_LOAD_GRACE AND has never served.

    SHARED PREDICATE (F1, HEAL_CASCADE 2026-10-14): BOTH down-repair paths in
    check_workers consult this — the debounced auto-heal (force-recreate) AND
    the plain gentle relaunch ("Worker X down — relaunching"). Before the
    cascade only the auto-heal path asked, so check_workers destroyed loads in
    progress that other paths (wedge recovery, `cluster up`, template apply)
    had started: the age key here is the CONTAINER, so a unit created
    elsewhere is recognised as loading too. The relaunch path's own guard
    (`_worker_relaunch_at`) only knew about relaunches IT had issued.

    `fail_closed` decides what UNKNOWN age means, and the two callers differ
    on purpose because their actions differ in cost:
      * auto-heal passes True — a force-recreate is destructive and expensive,
        so with no age evidence we assume loading and shield for the window.
      * the gentle relaunch passes False — when we have no age AND no stored
        record, there is no container to destroy (the created-ts map returns a
        stale cache rather than nothing on query failure, so a genuinely
        missing entry means missing container), and shielding would strand a
        dead unit unrepaired for the whole grace window.

    Both facts are durable: container age comes from sparkrun (re-resolved
    here on the down-threshold path, so a recreate is detected and has_served
    resets for the fresh container's load), and the has-served flag is
    persisted in worker_container_state.json. So the shield holds ACROSS a
    daemon restart — a still-loading unit never gets a fresh in-memory
    3-strike countdown into a force-recreate.

    A unit that HAS served and then stopped is exempt (has_served=True) and
    heals promptly.

    FAIL-CLOSED (bounded, `fail_closed=True` only): assume the unit is still
    loading rather than dead — store created_ts = now_wall and shield it for
    the grace window. Rationale: a repair costs a full multi-minute model
    reload and can loop (the incident this grace exists to kill), which is
    strictly worse than a bounded grace-window of suppressed healing. The
    fresh-assumption is durable, so it holds across a daemon restart, and it
    EXPIRES after WORKER_AUTOHEAL_LOAD_GRACE — a genuinely dead unit still
    heals once the window elapses. A unit that HAS served and then stopped is
    exempt regardless (has_served=True → heals promptly).
    """
    rec = _worker_container_state.get(key)
    stored_ts = rec["created_ts"] if rec else None
    has_served = bool(rec and rec["has_served"])
    live_ts = _worker_container_created_ts(node, port)
    # Recreate detection: the live container differs from the one we recorded →
    # a freshly-recreated, still-loading container must get a fresh grace.
    # Compare with a tolerance: created_ts round-trips through JSON (float), so
    # a few-microsecond drift must not look like a new container.
    if (live_ts is not None and stored_ts is not None
            and abs(live_ts - stored_ts) > 1.0):
        has_served = False
    created_ts = live_ts if live_ts is not None else stored_ts
    if created_ts is None:
        if not fail_closed:
            # Gentle-relaunch caller: no container evidence at all, so there
            # is no in-progress load to destroy — let the repair proceed
            # instead of stranding a dead unit for the whole grace window.
            return False
        # Fail-closed: cannot determine age and have never recorded one —
        # assume this is a freshly (re)created, still-loading container and
        # shield it for the grace window. Durable so a restart doesn't reset
        # the window; expires after the grace so a genuinely dead unit heals.
        created_ts = now_wall
    if (created_ts, has_served) != (stored_ts, bool(rec and rec["has_served"])):
        _worker_container_state[key] = {
            "created_ts": created_ts, "has_served": has_served}
        _save_worker_container_state()
    if has_served:
        return False                       # has served -> genuine failure, heal
    if now_wall - created_ts < WORKER_AUTOHEAL_LOAD_GRACE:
        return True                        # young container, never served -> loading
    return False


def _record_container_served(key, node, port):
    """Persist that the current container for (node, port) HAS served — the
    durable exemption from the startup grace. Called when /health answers OK.

    Only the has-served flag is stored/updated here. The healthy path must stay
    side-effect-free (no docker shell-out) — that invariant is pinned by the
    suite. If created_ts is unknown it stays None and is resolved later on the
    down-threshold path where docker is legitimately consulted. Cheap in the
    steady state: no-op once already recorded served."""
    rec = _worker_container_state.get(key)
    if rec and rec["has_served"]:
        return            # already recorded — nothing to do (no shell-out)
    _worker_container_state[key] = {
        "created_ts": rec["created_ts"] if rec else None,
        "has_served": True}
    _save_worker_container_state()


# ── Worker auto-heal helper ────────────────────────────────────────────────
# The heal ACTION is injectable (``_autoheal_worker_fn``) so tests can stub it;
# production recreates ONLY the down unit through the sanctioned sparkrun stop +
# run path (``serving._unit_run_cmd``), never a whole-template apply — see
# ``_default_autoheal_worker`` for why (F3, HEAL_CASCADE 2026-10-14).

def _autoheal_find_unit(node, port):
    """The serving.json unit owning (node, port), or None.

    Uses the same ``keepalive_units`` view check_workers iterates, so the heal
    acts on the very entry the check just declared down (same ``nodes`` span,
    recipe, port, tp and served names).
    """
    try:
        units = serving.keepalive_units(serving.load_serving())
    except Exception as e:  # unreadable serving.json -> caller reports, no heal
        log(f"Auto-heal: cannot read serving.json ({e})", "ERROR")
        return None
    for u in units or []:
        if u.get("node") == node and int(u.get("port") or 0) == int(port):
            return u
    return None


def _default_autoheal_worker(key, label, node, port):
    """REAL auto-heal: recreate ONLY the down unit — never the whole template.

    F3 (HEAL_CASCADE 2026-10-14). This used to re-apply the applied template
    with ``--force-recreate``, i.e. stop and re-run EVERY unit in the fleet to
    heal ONE. At 07:21Z and 07:43Z that tore down .246 and .248 — both healthy,
    each mid-way through a ~3 minute model load — and the daemon then saw them
    "down", which fed the cascade that hung .244 and .247.

    A whole-template apply is also not what the wedge needs: the wedge is one
    unit whose vLLM never answers. So heal exactly that unit:

      1. stop ITS span (``sparkrun stop <recipe> --hosts <span>`` — scoped to
         the span, not ``--all``, so a co-located sibling on another port
         survives), then
      2. re-issue the unit AS DEFINED via the shared ``serving._unit_run_cmd``
         (the single source of truth shared with ``fleet_up_plan``), with
         ``--ensure`` STRIPPED — ``--ensure`` no-ops on a container that is
         ``Up`` but wedged, which is precisely the failure class this path
         exists for. Stopping first plus no ``--ensure`` is what makes the
         recreate real; a leftover container then collides LOUDLY in the heal
         log instead of being silently skipped.

    The launch is detached (``start_new_session``) into
    ``~/.hscc/heal-<node>-<port>.log`` because the recreate re-stages weights
    for minutes; blocking the health check for that would stall every other
    stream. The relaunch timestamp is recorded so the F1 load grace protects
    the container this just created from the gentle relaunch.

    Injectable for tests via monkeypatching ``_autoheal_worker_fn``; a test
    that exercises THIS function patches ``run_cmd``/``Popen`` to prove only
    the down unit is touched, never a real node or docker.
    """
    unit = _autoheal_find_unit(node, port)
    if unit is None:
        log(f"Auto-heal {label}: no serving.json unit for {node}:{port} — "
            f"nothing to recreate", "WARN")
        return {"ok": False, "error": f"no serving.json unit for {node}:{port}"}

    recipe = unit.get("recipe")
    if not recipe:
        log(f"Auto-heal {label}: unit has no recipe — cannot recreate", "WARN")
        return {"ok": False, "error": "unit has no recipe in serving.json"}

    span = [n for n in (unit.get("nodes") or [node]) if n] or [node]
    run_argv = serving._unit_run_cmd(unit)
    # --ensure would no-op on an Up-but-wedged container (see docstring).
    recreate_argv = [a for a in run_argv if a != "--ensure"]

    # A2 (before ANY destructive step): if sparkrun reports a vLLM workload on
    # this host outside this unit's declared span, this recreate would
    # over-commit the GPU — and the stop below would take the unit's own
    # healthy span down first to do it. Refuse, alert, leave everything alone.
    if not _guard_launch_or_refuse(node, port, span, "autoheal-recreate", label):
        return {"ok": False, "error": "refused by launch guard (A2)",
                "reason": "foreign_workload_or_no_status"}

    log(f"Auto-heal {label}: recreating ONLY this unit "
        f"({recipe} on {'+'.join(span)}:{port}) — template untouched")
    # Stop the whole span (not just this node) so two workloads can never bind
    # the same host:port, exactly as the gentle relaunch does.
    stop_result = run_cmd(sparkrun_bin.argv(
        "stop", recipe, "--hosts", ",".join(span)), timeout=60)
    if not stop_result.get("ok"):
        log(f"Auto-heal {label}: stop reported failure "
            f"({stop_result.get('output', '')}) — recreating anyway; a leftover "
            f"container will collide loudly in the heal log", "WARN")

    # Record BEFORE launching so we never thrash even if Popen raises, and so
    # the shared load grace (F1) shields the container we are about to create.
    if key is not None:
        _worker_relaunch_at[key] = time.time()
        _save_worker_relaunch_timestamps()

    log_path = os.path.expanduser(f"~/.hscc/heal-{node}-{port}.log")
    try:
        with open(log_path, "a") as log_file:
            subprocess.Popen(
                _sparkrun_exec_argv(recreate_argv),
                stdout=log_file, stderr=log_file,
                start_new_session=True,
            )
    except Exception as e:
        log(f"Auto-heal {label}: failed to launch recreate: {e}", "ERROR")
        return {"ok": False, "error": f"recreate launch failed: {e}"}

    return {"ok": True, "status": "recreating", "unit": label,
            "recipe": recipe, "hosts": span, "port": port,
            "note": f"single-unit recreate detached to {log_path}"}


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

    # Load persisted grace timestamps + container-age state on each check
    # (both survive daemon restarts).
    _load_worker_relaunch_timestamps()
    _load_worker_container_state()

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
            # Durable: this container HAS served. Exempts it from the startup
            # grace so a later stop is a genuine failure healed promptly (the
            # has-served flag persists across daemon restarts).
            _record_container_served(key, node, port)
            continue
        # DEBOUNCED AUTO-HEAL (WD1): the unit is DOWN. Increment its
        # consecutive-down streak and, once it crosses the debounce threshold
        # AND is out of this unit's cooldown, recreate THAT unit via the
        # sanctioned sparkrun stop+run path. This is what fixes a unit
        # that stays "Up" in docker but whose vLLM never answers — a plain
        # sparkrun --ensure relaunch no-ops on it (--ensure sees the container
        # already running). Debounce/cooldown are configurable, not hardcoded.
        # The streak counts REGARDLESS of the gentle-relaunch grace below:
        # mid-load a unit is still not-serving, but the debounce (3 checks
        # ≈ 90s) is far shorter than a load (minutes). The STARTUP GRACE below
        # (container-age + has-served) is what shields a still-loading unit; a
        # genuinely wedged unit with an old container and no serve history
        # still auto-heals.
        streak = _worker_down_streak.get(key, 0) + 1
        _worker_down_streak[key] = streak
        cooldown_s = WORKER_AUTOHEAL_COOLDOWN_MINUTES * 60
        if (streak >= WORKER_AUTOHEAL_DEBOUNCE
                and now_wall - _worker_last_autoheal.get(key, 0.0) >= cooldown_s):
            # STARTUP GRACE (durable): a unit whose container is younger than
            # the grace AND has never served is still LOADING, not dead — do
            # NOT force-recreate it. Keyed on container age (sparkrun status) +
            # a persisted has-served flag, so this holds ACROSS a daemon
            # restart (the in-memory debounce resets; container age does not).
            # A unit that HAS served and then stopped is exempt and heals
            # promptly. The unit still counts as down (not online).
            if _unit_is_still_loading(key, node, port, now_wall, fail_closed=True):
                log(f"Auto-heal: worker {label} ({node}:{port}) down {streak}x "
                    f"but container <{WORKER_AUTOHEAL_LOAD_GRACE}s old and has "
                    f"never served — still loading, NOT auto-healing")
                down.append(label)
                continue
            # ONE REPAIR AT A TIME (F2, HEAL_CASCADE 2026-10-14): this is the
            # most destructive of the four paths — it recreates units while
            # others are acting. Back off a tick rather than join the pile-up.
            lease_ok, holder = heal_lease.acquire("auto-heal", f"{node}:{port}")
            if not lease_ok:
                log(f"Auto-heal: {label} deferred — heal in progress: "
                    f"{heal_lease.describe(holder)}", "WARN")
                down.append(label)
                continue
            try:
                # F5 breaker gate, INSIDE the lease and inside the try so the
                # `continue` below runs the finally-release (a lease-deferred
                # tick earlier attempts nothing and counts no strike). This is
                # the most destructive path — a whole-template force-recreate —
                # so its strikes matter most.
                allowed, note = _note_heal_action(node, port, "auto-heal", label)
                if not allowed:
                    log(f"Auto-heal: {note}", "WARN")
                    down.append(label)
                    continue
                log(f"Auto-heal: worker {label} ({node}:{port}) down {streak}x "
                    f"consecutively — recreating this unit only")
                _worker_last_autoheal[key] = now_wall
                _worker_down_streak[key] = 0  # finalise this debounce round
                heal_result = _autoheal_worker_fn(key, label, node, port)
            finally:
                heal_lease.release("auto-heal")
            heal_note = (heal_result.get("status") or heal_result.get("ok")
                         or heal_result.get("error") or "?")
            log(f"Auto-heal result for {label}: {heal_note} "
                f"{heal_result.get('note') or ''}".strip())
            down.append(label)
            continue
        # Down. Respect the grace window after a relaunch (mid-load == not dead).
        last = _worker_relaunch_at.get(key)
        if last and now_wall - last < grace_secs:
            down.append(label)  # still within load grace — leave it
            continue
        # SHARED LOAD GRACE (F1, HEAL_CASCADE 2026-10-14): the guard above only
        # knows about relaunches THIS path issued. A container started by wedge
        # recovery, `cluster up` or a template apply is invisible to it, so the
        # cascade could relaunch (destroying a ~3 min model load) every check.
        # The durable container-age + never-served predicate sees loads started
        # by ANY path, which is exactly what a model still loading needs.
        # Fail-open here (no container evidence -> repair proceeds) because the
        # alternative strands a dead unit; the destructive force-recreate path
        # keeps fail-closed.
        if _unit_is_still_loading(key, node, port, now_wall, fail_closed=False):
            down.append(label)  # a container is loading (started by any path)
            log(f"Worker {label} ({node}:{port}) down but container "
                f"<{WORKER_AUTOHEAL_LOAD_GRACE}s old and has never served — "
                f"still loading, NOT relaunching")
            continue
        if not recipe:
            down.append(label)
            log(f"Worker {label} down but no recipe in serving.json — skipping", "WARN")
            continue
        # ONE REPAIR AT A TIME (F2, HEAL_CASCADE 2026-10-14). This path fired on
        # a 60s tick against containers three other mechanisms were creating.
        # Take the lease for the stop+re-issue; back off one tick if held.
        lease_ok, holder = heal_lease.acquire("worker-relaunch", f"{node}:{port}")
        if not lease_ok:
            log(f"Worker {label} relaunch deferred — heal in progress: "
                f"{heal_lease.describe(holder)}", "WARN")
            down.append(label)
            continue
        try:
            # F5 breaker gate, inside the lease/try so `continue` runs the
            # finally-release. Per-UNIT relaunch has its own load-grace cooldown,
            # which cannot see this: a degrading node hammered across several of
            # its units. The breaker is the per-NODE count that closes that gap.
            allowed, note = _note_heal_action(node, port, "worker-relaunch", label)
            if not allowed:
                log(f"Worker {label}: {note}", "WARN")
                down.append(label)
                continue
            log(f"Worker {label} down — relaunching ({recipe}) on :{port}")
            # Re-issue the unit AS DEFINED in serving.json, using the SAME shared
            # command builder as fleet_up_plan (_unit_run_cmd), so sparkrun derives
            # the SAME cluster_id as the declared workload. keepalive_units() now
            # carries the unit's full span (`nodes`), tp, model and role on every
            # per-node entry, so a TP unit is relaunched as a whole group
            # (head + workers) WITH `--served-model-name`, exactly like
            # fleet_up_plan. Passing --served-model-name is essential: sparkrun
            # folds it into the intent hash, so omitting it (the pre-fix behaviour)
            # derived a DIFFERENT cluster_id and `--ensure` then launched a
            # PARALLEL DUPLICATE instead of re-issuing the declared workload
            # (t_e1ff0e8e). For a plain single-node unit (tp<=1, no model) this
            # reduces to the same single-host, no-`--tp` command as before.
            span = u.get("nodes") or [node]
            relaunch_cmd = serving._unit_run_cmd(u)
            # A2 (before the stop below): a launch that would land on a host
            # whose GPU already holds a foreign vLLM is refused + alerted; the
            # unit stays down and its healthy span members stay up.
            if not _guard_launch_or_refuse(node, port, span,
                                           "worker-relaunch", label):
                down.append(label)
                continue
            # Stop the WHOLE span first (not just this node) so requirement #3 holds:
            # never leave two workloads bound to the same host:port. Stopping the
            # full span clears the surviving pair members on the peer(s) before the
            # re-provisioned group binds :port. --hosts <span> scopes the stop to the
            # span (not --all), so an unrelated co-located sibling on another port
            # survives.
            stop_result = run_cmd(sparkrun_bin.argv(
                "stop", recipe, "--hosts", ",".join(span)), timeout=60)
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
                        _sparkrun_exec_argv(relaunch_cmd),
                        stdout=log_file, stderr=log_file,
                        start_new_session=True,
                    )
                relaunched.append(label)
            except Exception as e:
                log(f"ERROR: failed to launch worker {label}: {e}", "ERROR")
                down.append(label)
        finally:
            heal_lease.release("worker-relaunch")

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
# Wall-clock budget for the /metrics busy-signal check (GET + parse). Must stay
# MUCH smaller than ENGINE_WEDGE_TIMEOUT (default 10s) so a loaded unit whose
# probe already burns the full 10s is not made worse by an additional slow
# metrics fetch. Best-effort: if /metrics cannot answer within this window we
# treat the signal as unavailable and fall back to the per-unit
# throughput-derived bound (scope-2 path).
ENGINE_WEDGE_BUSY_CHECK_TIMEOUT = int(
    os.environ.get("HSCC_ENGINE_WEDGE_BUSY_CHECK_TIMEOUT", "2"))
# How many recent successful probe round-trips each unit retains to derive its
# own per-unit probe bound (scope-2 fallback when no /metrics busy signal is
# reachable). Small window: throughput varies over minutes; we only want the
# recent norm, not a multi-hour average.
_ENGINE_WEDGE_LATENCY_WINDOW = 16
# Multiplier over a unit's own median successful probe latency used to derive
# its per-unit probe bound in the no-busy-signal fallback. 3x the observed
# median: a healthy unit that usually streams in <1s still gets caught fast if
# it wedges (bound stays ~10s), while a unit that routinely takes ~8s under
# load gets ~24s of headroom instead of a flat 10s.
_ENGINE_WEDGE_TIMEOUT_MULTIPLIER = 3.0
# ── One probe result per tick, SHARED across callers (t_e37a74c7) ─────────
# On 2026-10-24 the live fleet ran TWO full daemon loops in TWO processes
# (launchd's supervised `start-daemon` plus an orphaned `hscc start` fork —
# both writers held ~/.hscc/daemon.log open). Each loop probes every unit on
# its own 60 s tick ~2 s apart, so one "tick" hit every engine with TWO
# streaming probes and two independent streak memories: contradictory
# verdicts 1.6 s apart, and a single transient blip counted TWICE — enough
# to cross the 2-tick wedge debounce by itself. An in-process memo cannot
# fix that (the callers are different processes), so each probe RESULT is
# recorded under the state dir and any caller whose own last probe of that
# unit is older than a recorded sibling result inside this TTL consumes it
# instead of re-probing. Well below the 60 s cadence, so a real next tick
# (and every test that drives ticks) still probes fresh; a CLI
# `hscc check engine_wedge` run (persist_disabled) neither reads nor writes
# the share — it probes fresh, as an explicit operator request should.
ENGINE_WEDGE_PROBE_SHARE_TTL = int(
    os.environ.get("HSCC_ENGINE_WEDGE_PROBE_SHARE_TTL", "20"))
# Sustained INCONCLUSIVE probes required before a unit starts accruing wedge
# credit (see _probe_unit_generation's "closed_early" classification). Bounds
# how long a unit that only ever closes the probe stream early can hide from
# the wedge signal, while making isolated early-close blips — the actual
# 2026-10-24 false-positive signature — individually harmless.
ENGINE_WEDGE_INCONCLUSIVE_THRESHOLD = int(
    os.environ.get("HSCC_ENGINE_WEDGE_INCONCLUSIVE_THRESHOLD", "3"))
# Ceiling on waiting for a sibling caller's in-flight probe result before
# deciding to probe ourselves. MUST stay strictly below ENGINE_WEDGE_TIMEOUT
# so one tick's worst-case stall (a sibling wedged mid-probe) still fits
# inside the stream's own 60 s cadence — one bounded pause per unit, never
# a cascade (every probe path is independently bounded).
ENGINE_WEDGE_PROBE_WAIT_CAP = 5

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


def _unit_busy(node, port):
    """Busy signal for a unit via its vLLM /metrics endpoint.

    A probe that answers HTTP 200 with ZERO tokens is ambiguous: the engine
    may be wedged (idle, never generating) or merely BUSY (saturated serving
    other requests, so OUR probe waits in the queue past the flat bound).
    vLLM exposes ``num_requests_running`` / ``num_requests_waiting`` on
    /metrics — the real busy signal. Reuses ``throughput.fetch_node_metrics``
    so we use the SAME Prometheus parser the G/T/E streams and ``verify``
    already trust — no second, divergent /metrics implementation.

    Returns:
        True  — engine demonstrably serving other requests
                (num_requests_running + num_requests_waiting > 0)
        False — /metrics reachable but idle (both are zero)
        None  — /metrics unreachable/absent ⇒ no busy signal available
    """
    from . import throughput
    m = throughput.fetch_node_metrics(
        "http://%s:%s/metrics" % (node, port),
        timeout=ENGINE_WEDGE_BUSY_CHECK_TIMEOUT)
    if m is None:
        return None
    return (int(m.get("running") or 0) + int(m.get("waiting") or 0)) > 0


def _unit_probe_timeout(st):
    """Per-unit probe bound derived from the unit's OWN observed throughput.

    Scope-2 fallback: when no /metrics busy signal is reachable we cannot tell
    \"busy-but-slow\" from \"wedged\" under a flat wall-clock bound, so we bound
    the probe with a per-unit value derived from the unit's own observed
    successful probe latencies instead of the flat ``ENGINE_WEDGE_TIMEOUT``:

        bound = max(ENGINE_WEDGE_TIMEOUT, 3 x median(recent successful latencies))

    A unit whose healthy history is fast keeps a TIGHT bound (a genuine wedge
    is still caught quickly); a unit that historically runs hot under load
    gets headroom proportional to its own observed throughput. Never below
    ``ENGINE_WEDGE_TIMEOUT`` — this does NOT raise the global default and does
    NOT weaken the idle-wedge detection; it only widens the bound for units
    that have demonstrated they need it.
    """
    lats = st.get("latencies") or []
    if not lats:
        return float(ENGINE_WEDGE_TIMEOUT)
    median = float(sorted(lats)[len(lats) // 2])
    return max(float(ENGINE_WEDGE_TIMEOUT),
               _ENGINE_WEDGE_TIMEOUT_MULTIPLIER * median)


# ── Cross-process probe share (t_e37a74c7) ────────────────────────────────
# ONE probe result per unit per tick, shared by every caller. The 2026-10-24
# false positives came from two full daemon loops in TWO processes (an
# orphaned `hscc start` fork alongside launchd's `start-daemon`) each probing
# every unit on its own 60 s tick ~2 s apart: two streaming probes per tick,
# two streak memories, and one transient blip counted twice — enough on its
# own to cross the 2-tick wedge debounce. An in-process memo cannot cover two
# processes, so the result itself is recorded in the state dir (deliberately
# NOT a .json file: `hscc status` and `verify` glob state/*.json and must
# never mistake a probe share for a stream).
#
# Consumption rule: a caller consumes a recorded result IFF that result is
# NEWER than its own last probe of the unit and inside the TTL. A caller's
# own result is therefore never "newer than its own last probe" (both stamp
# the same ts), so consecutive ticks in ONE process still probe fresh — the
# multi-tick debounce keeps its meaning. The .claim file (O_EXCL) marks an
# in-flight probe: a caller that finds a live claim WAITS (bounded) for the
# sibling result instead of firing a second concurrent probe at the same
# engine; a claim whose holder died is stale, gets stolen, and the unit is
# probed normally — sharing can never silence the check.

# A claim is stale (holder presumed dead) once it is older than the longest
# a probe legitimately needs plus our own bounded wait, with slack.
def _engine_wedge_claim_horizon():
    return ENGINE_WEDGE_TIMEOUT + ENGINE_WEDGE_PROBE_WAIT_CAP + 10


def _engine_wedge_probe_share_enabled():
    """Sharing is on when the TTL is positive and we are NOT an ad-hoc CLI
    check (`hscc check engine_wedge` runs under persist_disabled and must
    probe fresh on an explicit operator request — and publish nothing)."""
    if ENGINE_WEDGE_PROBE_SHARE_TTL <= 0:
        return False
    try:
        from .state import persist_disabled_active
        return not persist_disabled_active()
    except Exception:
        return True


def _engine_wedge_probe_share_path(node, port):
    from . import state
    digest = hashlib.sha1(("%s:%s" % (node, port)).encode()).hexdigest()[:16]
    return os.path.join(state.STATE_DIR,
                        f"engine-wedge-probe-{digest}.probe")


def _engine_wedge_share_read(path):
    """Read a recorded sibling probe result → dict or None. Never raises."""
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        ts = data.get("ts")
        if not isinstance(result, dict) or not isinstance(ts, (int, float)):
            return None
        return data
    except Exception:
        return None


def _engine_wedge_share_write(path, ts, probe_timeout, result, elapsed):
    """Atomically record this caller's probe result. Never raises."""
    entry = {"ts": ts, "pid": os.getpid(),
             "probe_timeout": float(probe_timeout),
             "elapsed": round(float(elapsed), 3), "result": result}
    tmp = "%s.%s.%s.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(entry, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _engine_wedge_claim_try(path):
    """Acquire the in-flight probe claim atomically (O_EXCL). True if ours."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps({"pid": os.getpid(),
                                     "ts": time.time()}).encode())
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # A filesystem we cannot even write must NOT disable the check:
        # fall through to the unshared probe (old behaviour).
        return True


def _engine_wedge_claim_release(path):
    """Release OUR claim (pid-checked, so a stolen-then-ours race cannot let
    us delete a live sibling's claim). Never raises."""
    try:
        with open(path) as f:
            data = json.load(f)
        if data.get("pid") != os.getpid():
            return
        os.unlink(path)
    except Exception:
        pass


def _engine_wedge_claim_age(path, now):
    """Seconds since the claim was taken; None if absent/unreadable; a
    corrupt claim counts as ancient (steal it)."""
    try:
        with open(path) as f:
            data = json.load(f)
        ts = data.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        return now - ts
    except FileNotFoundError:
        return None
    except Exception:
        return float("inf")


def _engine_wedge_fresh_share(path, st, now):
    """A recorded result newer than this caller's own last probe and inside
    the TTL → the sibling's probe for THIS tick. Returns entry or None."""
    entry = _engine_wedge_share_read(path)
    if entry is None:
        return None
    ts = entry["ts"]
    if ts <= st.get("last_probe_ts", 0.0):
        return None
    if now - ts > ENGINE_WEDGE_PROBE_SHARE_TTL:
        return None
    return entry


def _engine_wedge_probe_shared(node, port, probe_timeout, st):
    """Probe one unit with the per-tick result shared across caller
    processes (see the block comment above). Returns (result, elapsed).

    NEVER waits longer than ENGINE_WEDGE_PROBE_WAIT_CAP for a sibling: with
    every probe itself bounded by probe_timeout, the per-unit worst case
    stays inside the stream's 60 s cadence — one bounded pause, never a
    cascade, and never a silent stream (stale claims are stolen)."""
    path = _engine_wedge_probe_share_path(node, port)
    claim = path + ".claim"
    now = time.time()

    entry = _engine_wedge_fresh_share(path, st, now)
    if entry is not None:
        st["last_probe_ts"] = entry["ts"]
        return entry["result"], float(entry.get("elapsed") or 0.0)

    acquired = _engine_wedge_claim_try(claim)
    if not acquired:
        # A sibling is probing this unit right now. If its claim is stale the
        # holder died — steal it and take over. Otherwise wait (bounded) for
        # its result to land.
        age = _engine_wedge_claim_age(claim, now)
        if age is not None and age > _engine_wedge_claim_horizon():
            try:
                os.unlink(claim)
            except OSError:
                pass
            acquired = _engine_wedge_claim_try(claim)
        if not acquired:
            deadline = time.monotonic() + ENGINE_WEDGE_PROBE_WAIT_CAP
            while time.monotonic() < deadline:
                time.sleep(0.1)
                entry = _engine_wedge_fresh_share(path, st, time.time())
                if entry is not None:
                    st["last_probe_ts"] = entry["ts"]
                    return entry["result"], float(entry.get("elapsed") or 0.0)
                if _engine_wedge_claim_age(claim, time.time()) is None:
                    # Claim vanished (sibling finished or died) without a
                    # result we can use — take the claim ourselves.
                    if _engine_wedge_claim_try(claim):
                        acquired = True
                        break
    if not acquired:
        # Either the sibling's claim was stolen-and-lost or the bounded wait
        # expired (sibling wedged mid-probe). Probe ourselves rather than
        # skip the unit — the fresh result replaces whatever the stale claim
        # left behind. (No release below: this probe is not claim-guarded.)
        t0 = time.monotonic()
        result = _probe_unit_generation(node, port, timeout=probe_timeout)
        elapsed = time.monotonic() - t0
        ts = time.time()
        st["last_probe_ts"] = ts
        _engine_wedge_share_write(path, ts, probe_timeout, result, elapsed)
        return result, elapsed

    try:
        t0 = time.monotonic()
        result = _probe_unit_generation(node, port, timeout=probe_timeout)
        elapsed = time.monotonic() - t0
        ts = time.time()
        st["last_probe_ts"] = ts
        _engine_wedge_share_write(path, ts, probe_timeout, result, elapsed)
        return result, elapsed
    finally:
        if acquired:
            _engine_wedge_claim_release(claim)


def _engine_wedge_prune_shares(units_now):
    """Delete probe-share/claim files for units no longer configured, and
    any file older than the horizon (a crash leftover). Bounded: one small
    dir listing, throttled to once a minute per process. Never raises —
    sharing must never be able to break the check that owns it."""
    now = time.time()
    if now - getattr(health_prune_state, "last", 0.0) < 60:
        return
    health_prune_state.last = now
    try:
        from . import state
        keep = set()
        for node, port in units_now:
            digest = hashlib.sha1(
                ("%s:%s" % (node, port)).encode()).hexdigest()[:16]
            keep.add(f"engine-wedge-probe-{digest}.probe")
        horizon = _engine_wedge_claim_horizon()
        for fn in os.listdir(state.STATE_DIR):
            if not fn.startswith("engine-wedge-probe-"):
                continue
            path = os.path.join(state.STATE_DIR, fn)
            base = fn[:-6] if fn.endswith(".claim") else fn
            if base in keep and fn.endswith(".claim"):
                # Live unit: still age out a claim whose holder died.
                age = _engine_wedge_claim_age(path, now)
                if age is not None and age > horizon:
                    os.unlink(path)
                continue
            if base in keep:
                continue
            try:
                if now - os.stat(path).st_mtime > horizon:
                    os.unlink(path)
            except OSError:
                pass
    except Exception:
        pass


health_prune_state = threading.local()


def _read_sse_content_until(resp, stop_event, out_box, ended_box=None):
    """Drain an OpenAI-compatible SSE stream on a helper thread.

    Accumulates ``choices[0].delta.content`` text into ``out_box`` (a plain
    list) until [DONE], EOF, or ``stop_event`` is set. Runs off the caller's
    stack so the caller can enforce an OVERALL outer deadline on the streaming
    probe with a bounded ``join(timeout=...)`` regardless of per-read socket
    timeouts. Best-effort: any parse/transport error just stops accumulating;
    the resulting (possibly empty) text is what the caller judges.

    ``ended_box`` (t_e37a74c7): when given, a one-element list set to True IFF
    the stream ENDED ON ITS OWN (EOF/peer-close, [DONE], or a transport error)
    before the caller gave up. This separates the zero-token shapes the old
    code conflated: "the engine held the connection open and never generated"
    (a genuine wedge) vs "the stream terminated inside the window with no
    tokens" (a transport blip, a handshake-only close, or the documented RST
    race where the server's close beats the body bytes — NOT the wedge
    signature, and the shape that produced the 2026-10-24 false positives).
    The caller still checks the elapsed budget so a socket timeout AT the
    bound stays the wedge signal. Exits caused by ``stop_event`` (caller
    deadline) never set it.
    """
    buf = b""
    fp = getattr(resp, "fp", None)
    try:
        while not stop_event.is_set():
            if fp is None:
                if ended_box is not None:
                    ended_box[0] = True
                break
            chunk = fp.read(256)
            if stop_event.is_set():
                break
            if not chunk:
                # EOF: the peer closed the stream on its own.
                if ended_box is not None:
                    ended_box[0] = True
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    if ended_box is not None:
                        ended_box[0] = True
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
        # Transport error mid-stream (ConnectionResetError from the server's
        # close, socket.timeout AT the bound, ...). It IS "the stream ended"
        # — the caller's elapsed-budget check decides which shape it was:
        # ended well inside the bound = blip (inconclusive); ended at the
        # bound = the stall signature (wedged).
        if ended_box is not None:
            ended_box[0] = True


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

    Core signal (the wedge signature): the unit returns HTTP 200 and HOLDS the
    streaming connection open for the whole ``timeout`` window producing NO
    content deltas ⇒ ``ok: False`` with a "wedged" classification. HTTP/
    transport errors ⇒ ``ok: False`` with a "down" error — unreachable, a
    distinct condition from "answering but never generating". The caller
    separates the two in the stream's details.

    Third shape (t_e37a74c7): HTTP 200 but the stream ENDED ON ITS OWN well
    inside the window with zero tokens (peer closed / [DONE] without content)
    ⇒ ``ok: False`` with a "closed_early" classification. A stalled-to-death
    engine holds the socket; a server that closes cleanly is answering — a
    transient transport blip or a handshake-only close, NOT the wedge
    signature. On 2026-10-24 this shape (probes returning in 2.2-4.0 s across
    all 3 units, while a true stall burns the full >=10 s bound each) was
    counted as wedge credit and — doubled by a second daemon loop re-probing
    seconds later — crossed the debounce and false-alarmed healthy units.
    """
    import urllib.request
    import urllib.error
    import threading as _threading

    timeout = timeout or ENGINE_WEDGE_TIMEOUT
    max_tokens = max_tokens or ENGINE_WEDGE_MAX_TOKENS
    _started = time.monotonic()
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
            end_box = [False]
            stop = _threading.Event()
            t = _threading.Thread(
                target=_read_sse_content_until,
                args=(resp, stop, out_box, end_box),
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
        # (t_e37a74c7) Two zero-token shapes, judged by WHO ended the stream
        # and WHEN. A stalled engine HOLDS the socket until the caller
        # abandons it: ended (by transport error or EOF) only at/after the
        # budget ran out — that stays the wedge signature. A stream that
        # terminated ON ITS OWN well inside the budget (clean close, RST
        # race, early EOF) is a transport blip, not a stall.
        elapsed = time.monotonic() - _started
        if end_box[0] and elapsed < timeout * 0.9:
            return {"ok": False, "status": status, "error": "closed_early"}
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
      * LOAD-AWARE verdict: a probe that answers 200 with ZERO tokens is
        ambiguous. Before counting a wedge failure we consult the unit's vLLM
        /metrics (num_requests_running / num_requests_waiting) — the real busy
        signal. A unit demonstrably serving other requests is reported
        "busy" (a saturated-but-healthy engine), NOT a wedge. A 200-with-zero-
        tokens probe while /metrics is IDLE remains the genuine wedge
        signature and is still caught. When /metrics is unreachable (no busy
        signal), we fall back to a per-unit probe bound derived from that
        unit's own observed successful throughput (``_unit_probe_timeout``)
        rather than the flat ``ENGINE_WEDGE_TIMEOUT``.

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
    wedged, loading, down, ok_units, busy = [], [], [], [], []
    live_endpoints = []

    for u in units:
        nodes = [n for n in (u.get("nodes") or []) if n]
        if not nodes:
            continue
        key = _engine_wedge_unit_key(u)
        node, port = nodes[0], u.get("port") or serving.serving_port(serving_data)
        label = u.get("id") or f"{node}:{port}"

        st = _engine_wedge_units.setdefault(key, {
            "first_seen": now_wall, "consecutive_failures": 0,
            "last_success": 0.0, "latencies": [],
            # t_e37a74c7: per-tick probe-share bookkeeping — wall clock of
            # the last probe THIS caller ran for the unit (a shared result
            # is consumed only when newer), and the run-length of
            # inconclusive (closed-early) probes before wedge credit accrues.
            "last_probe_ts": 0.0, "inconclusive": 0,
        })

        # A unit we've only just met may still be loading weights (minutes);
        # a loading unit is NOT a wedged unit. Skip it for the load grace and
        # report "loading" so the stream tells the truth without alarming.
        if now_wall - st["first_seen"] < ENGINE_WEDGE_LOAD_GRACE:
            loading.append({"unit": label, "node": node, "port": port,
                            "status": "loading"})
            continue

        # Per-unit probe bound: the flat ENGINE_WEDGE_TIMEOUT unless the unit
        # has demonstrated (via its own observed successful throughput) that
        # it needs more headroom and no /metrics busy signal is reachable
        # (scope-2 fallback — see _unit_probe_timeout).
        probe_timeout = _unit_probe_timeout(st)
        if _engine_wedge_probe_share_enabled():
            # ONE probe result per unit per tick across ALL caller processes
            # (t_e37a74c7): consume a sibling caller's in-flight/just-landed
            # result instead of firing a second streaming probe at the same
            # engine seconds apart.
            probe, _probe_elapsed = _engine_wedge_probe_shared(
                node, port, probe_timeout, st)
        else:
            _probe_start = time.monotonic()
            probe = _probe_unit_generation(node, port, timeout=probe_timeout)
            _probe_elapsed = time.monotonic() - _probe_start
            st["last_probe_ts"] = time.time()
        live_endpoints.append((node, port))

        if probe["ok"]:
            # Observed successful probe latency feeds the per-unit bound.
            lats = st.setdefault("latencies", [])
            lats.append(_probe_elapsed)
            if len(lats) > _ENGINE_WEDGE_LATENCY_WINDOW:
                del lats[:-_ENGINE_WEDGE_LATENCY_WINDOW]
            st["consecutive_failures"] = 0
            st["inconclusive"] = 0
            st["last_success"] = now_wall
            ok_units.append({"unit": label, "node": node, "port": port,
                             "status": "ok"})
            continue

        if probe["error"] == "closed_early":
            # (t_e37a74c7) 200 answered and the SSE stream ENDED ON ITS OWN
            # well inside the bound with zero tokens. A genuinely stalled
            # engine HOLDS the socket until the probe abandons it; a server
            # that closes cleanly is answering — a transport blip or a
            # handshake-only close. This shape (probes returning in 2.2-4.0 s
            # while a true stall burns >=10 s each) produced every one of the
            # 19 false WEDGED ERRORs on 2026-10-24, because the twin daemon
            # loop re-probing ~2 s later turned ONE blip into TWO counted
            # failures and crossed the wedge debounce by itself. An isolated
            # blip is now inconclusive: no wedge credit, reported honestly.
            # Only a SUSTAINED run of them (the engine refuses every probe
            # stream) starts accruing wedge credit, and then the ordinary
            # debounce applies — a real wedge is still caught.
            st["inconclusive"] = st.get("inconclusive", 0) + 1
            escalated = st["inconclusive"] >= ENGINE_WEDGE_INCONCLUSIVE_THRESHOLD
            if escalated:
                st["consecutive_failures"] += 1
            note = ("stream closed early with no tokens (inconclusive %d/%d)"
                    % (st["inconclusive"], ENGINE_WEDGE_INCONCLUSIVE_THRESHOLD))
            if escalated and st["consecutive_failures"] >= ENGINE_WEDGE_THRESHOLD:
                # About to declare a wedge — same corroboration the stall path
                # gets (no regression of the busy-vs-wedged distinction): a
                # congested engine can reset probe streams while it is busy
                # serving other requests. /metrics settles it.
                if _unit_busy(node, port):
                    st["consecutive_failures"] = 0
                    st["inconclusive"] = 0
                    st["last_success"] = now_wall
                    busy.append({"unit": label, "node": node, "port": port,
                                 "status": "busy",
                                 "message": "probe stream closed early "
                                            "repeatedly but vLLM /metrics "
                                            "shows it serving other requests"})
                    continue
                stalled_s = (now_wall - st["last_success"]) if st["last_success"] else None
                wedged.append({
                    "unit": label, "node": node, "port": port,
                    "status": "wedged",
                    "message": "HTTP 200 with no generated tokens on %d "
                               "consecutive checks (stream closed early each "
                               "time)" % st["consecutive_failures"],
                    "last_success": st["last_success"] or None,
                    "stalled_for_s": round(stalled_s) if stalled_s else None})
            else:
                loading.append({"unit": label, "node": node, "port": port,
                                "status": "checking",
                                "consecutive_failures": st["consecutive_failures"],
                                "note": note})
            continue

        if probe["error"] == "wedged":
            # A 200-with-zero-tokens probe is ambiguous: the engine may be
            # wedged (idle) or merely BUSY (saturated serving other requests,
            # so OUR probe waits in queue past the bound). Consult the unit's
            # vLLM /metrics for a REAL busy signal before counting a failure.
            busy_signal = _unit_busy(node, port)
            if busy_signal:
                # Demonstrably serving other requests → a loaded-but-healthy
                # engine, NOT a wedge. Report "busy" (positive liveness
                # evidence) and do NOT count a wedge failure. Reset the
                # streak: a busy unit is genuinely healthy.
                st["consecutive_failures"] = 0
                st["last_success"] = now_wall
                busy.append({"unit": label, "node": node, "port": port,
                             "status": "busy",
                             "message": "answered 200 with zero tokens but "
                                        "vLLM /metrics shows it serving other "
                                        "requests"})
                continue
            if busy_signal is None:
                # No /metrics busy signal reachable. Fall back to a per-unit
                # bound derived from THIS unit's own observed throughput
                # (already applied as probe_timeout above) instead of the flat
                # ENGINE_WEDGE_TIMEOUT. Count toward the streak so a genuinely
                # wedged unit is still caught; the note says which bound was
                # used so the report is honest about the degraded mode.
                st["consecutive_failures"] += 1
                checking = {"unit": label, "node": node, "port": port,
                            "status": "checking",
                            "consecutive_failures": st["consecutive_failures"],
                            "note": "no /metrics busy signal — probe bound "
                                    "%ss (per-unit)" % round(probe_timeout, 1)}
                if st["consecutive_failures"] >= ENGINE_WEDGE_THRESHOLD:
                    # Even without a busy signal, the per-unit bound was
                    # derived from the unit's own throughput; crossing the
                    # debounced threshold means it stalled that bound too.
                    # It is genuinely wedged (200-with-zero-tokens beyond its
                    # own observed norm).
                    stalled_s = (now_wall - st["last_success"]) if st["last_success"] else None
                    wedged.append({
                        "unit": label, "node": node, "port": port,
                        "status": "wedged",
                        "message": "HTTP 200 but no generated tokens within "
                                   "%ss (per-unit bound, no /metrics busy "
                                   "signal)" % round(probe_timeout, 1),
                        "last_success": st["last_success"] or None,
                        "stalled_for_s": round(stalled_s) if stalled_s else None})
                else:
                    # Debouncing: one (or a few) slow/empty responses is not
                    # yet a declared wedge — report transparently.
                    loading.append(checking)
                continue

            # busy_signal is False: /metrics reachable AND idle, but the unit
            # answered 200 with ZERO tokens within the bound. THIS is the
            # genuine wedge signature — "answered 200 with ZERO tokens while
            # idle" — and the existing detection is preserved unchanged.
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
        st["inconclusive"] = 0
        down.append({"unit": label, "node": node, "port": port,
                     "status": "down",
                     "message": probe.get("error") or "unreachable"})

    _engine_wedge_prune_shares(live_endpoints)

    ok = not wedged
    msg_parts = [f"{len(ok_units)}/{len(units)} streaming ok"]
    if busy:
        msg_parts.append(f"{len(busy)} busy (loaded, not wedged)")
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
        "wedged": wedged, "loading": loading, "down": down, "busy": busy,
        "last_check": now_iso(), "message": ", ".join(msg_parts),
    })
    log(f"Engine-wedge check: {', '.join(msg_parts)}")
    return ok
