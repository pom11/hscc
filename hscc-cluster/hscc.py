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
  down [--dry-run] Stop ALL sparkrun workloads across the fleet
  up   [--dry-run] Start every unit in serving.json (orch + keepalive)
  info             Detailed cluster configuration
  orch inject <p> "<msg>" [--mode auto|plain|queue|steer] [--dry-run] [--json]
                   Inject a message into a running Hermes REPL pane (repl_inject)
"""

import sys
import json
import subprocess
import os
import argparse

import _theme  # guarded themed render helper (peer cli_theme + neutral fallback)

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
    """Run a command and return structured output.

    Timeout maps to returncode 124 (the `timeout(1)` convention clusterlib
    .run_cmd also uses) and carries whatever partial output the child had
    written — a per-unit launch report needs that tail as evidence, not an
    empty dict (t_be4b94ca).
    """
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
    except subprocess.TimeoutExpired as e:
        partial = (e.output or b"").decode("utf-8", "replace") if isinstance(e.output, bytes) else (e.output or "")
        partial_err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        resp = {"success": False, "returncode": 124,
                "error": f"command timed out after {timeout}s",
                "output": partial.strip(), "command": args}
        if partial_err.strip():
            resp["error"] = f"{resp['error']}\n{partial_err.strip()}"
        return resp
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

def _job_host_ip(stripped):
    """Return the node IP of a Job-block host line, or None.

    Host lines under a ``Job:`` block carry a label column then the address:
    ``node_0     10.0.0.1     Up 12 hours    <image>`` (or ``solo`` for a
    single-container job). The idle-section lines (``  192.0.2.11``) have no
    label column, so requiring a dot-free first token and a dotted second
    token separates the two classes. (t_db6a32da)
    """
    parts = stripped.split()
    if len(parts) < 2:
        return None
    label, addr = parts[0], parts[1]
    if "." in label or "." not in addr:
        return None
    return addr


def cmd_cluster_status():
    """Show running workloads and idle hosts on the DGX Spark cluster."""
    result = run_cmd([SPARKRUN, "status"], timeout=15)
    output = result.get("output", "")
    
    workloads = []
    idle_hosts = []
    total_hosts = 0
    # Host lines of the ``Job:`` block currently being read (None outside a
    # block). The SAME loop that yields name/tp/pp/container_id pairs each
    # job with the nodes it runs on — the unit→node edge clients cannot get
    # from anywhere else (t_db6a32da). The list object is shared with the
    # workload dict, so later host lines of the block land in its ``nodes``.
    current_nodes = None
    
    for line in output.split("\n"):
        stripped = line.strip()
        if not stripped:
            current_nodes = None  # a blank line closes the Job block
            continue
        if stripped.startswith("Idle"):
            current_nodes = None  # the idle section is never inside a block
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
            current_nodes = []
            workloads.append({
                "name": name,
                "tp": tp,
                "pp": pp,
                "container_id": container_id,
                # Optional/additive (t_db6a32da): the host lines of this
                # job's block, in order, de-duplicated. A tp=2 job
                # legitimately lists BOTH nodes.
                "nodes": current_nodes,
            })
        # Solo host line: "  solo       192.0.2.10                           Up 26 minutes             sparkrun-eugr-vllm"
        elif "solo" in stripped and "." in stripped and ("Up " in stripped or "since" in stripped.lower()):
            parts = stripped.split()
            for i, p in enumerate(parts):
                if p == "solo" and i+1 < len(parts):
                    ip = parts[i+1]
                    total_hosts += 1
                    if current_nodes is not None and ip not in current_nodes:
                        current_nodes.append(ip)
                    break
        # "  192.0.2.11" — check raw line for indentation
        elif line.startswith("  ") and stripped.count(".") >= 3 and not stripped.startswith(("logs:", "stop:", "solo")):
            idle_hosts.append(stripped)
            total_hosts += 1
            if current_nodes is not None:
                ip = _job_host_ip(stripped)
                if ip is not None and ip not in current_nodes:
                    current_nodes.append(ip)
        # Total line: "Total: 1 container(s) across 4 host(s)"
        elif stripped.startswith("Total:"):
            current_nodes = None
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
    """Stop a running sparkrun workload by container ID.

    Refuses (without shelling out) when the id is missing or looks like a
    flag: `--help`, `-h`, `--dry-run`, an empty string. B6 (audit
    t_7b0f65c8, card t_875c5019) measured `hscc cluster stop --help` reaching
    this function with `--help` as the container id and executing
    `sparkrun stop --help` — a help request invoking a MUTATING verb. The
    unified CLI now intercepts `--help` before dispatch, but this is the
    second line of defence and the only one that also covers the standalone
    `hscc-cluster stop <id>` entry: a destructive verb runs only on an
    argument that can actually name a container.
    """
    cid = (container_id or "").strip()
    if not cid or cid.startswith("-"):
        return {
            "success": False,
            "error": (f"a container id is required, got {container_id!r}"
                      if cid else "a container id is required"),
            "usage": "hscc cluster stop <container-id> "
                     "(ids: hscc cluster status)",
        }
    return run_cmd([SPARKRUN, "stop", cid], timeout=30)


def _load_daemon_serving():
    """Import the daemon's ``serving`` module (hscc_daemon/serving.py).

    ``serving`` owns serving.json topology + the fleet down/up builders. It is
    a sibling plugin (hscc_daemon) of this one; when this plugin is driven as
    a library by the daemon CLI, ``hscc_daemon`` is already on ``sys.path``.
    When driven standalone, add the repo root so the sibling package resolves.
    Returns the module, or None (the callers abort) if it cannot be imported.
    """
    try:
        from hscc_daemon import serving
        return serving
    except Exception:
        # Standalone fallback: add the repo root (parent of hscc_daemon) to path.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        try:
            from hscc_daemon import serving
            return serving
        except Exception as e:
            return None


def _fleet_op_timeout_seconds():
    """Generous subprocess timeout for a full fleet start/stop.

    A fleet `sparkrun run` on real hardware takes far longer than a quick
    status call: the launch phase alone is 40s+ (before the model even begins
    loading) and a model load can take minutes. run_cmd's default timeout=30 is
    for quick status calls and would kill a fleet start every time. Size the
    subprocess timeout against the daemon's model-load grace window
    (lifecycle.VLLM_LOAD_GRACE_MINUTES, default 20 ⇒ 1200s) — reusing that
    natural knob rather than inventing a new one. Falls back to 1200s if the
    daemon can't be imported (standalone mode).

    This is the budget for WHOLE-FLEET operations (`cluster down`). The
    per-unit LAUNCH phase of `cluster up` uses _launch_phase_timeout_seconds()
    instead — a `--no-follow --ensure` launch must RETURN in seconds, and
    reusing this 1200s window is what made the 2026-10-22 hung launch look
    "forever" (t_be4b94ca).
    """
    try:
        from hscc_daemon import lifecycle
        return getattr(lifecycle, "VLLM_LOAD_GRACE_MINUTES", 20) * 60
    except Exception:
        return 1200


# t_be4b94ca (2026-10-22 cooldown): `sparkrun run ... --no-follow --ensure`
# must RETURN once the containers are up — the model load happens afterwards
# and is watched by the daemon, not by this CLI. The operator observed a
# launch stuck 19 min / 8 min on a unit that ALREADY answered /v1/models 200.
# A dedicated launch-phase budget, sized to the slowest honest launch
# (operator's hand-run parallel launches returned in ~3 min worst case), keeps
# one hung unit from eating the fleet window. Overridable for slow fleets.
LAUNCH_PHASE_TIMEOUT_DEFAULT_S = 180


def _launch_phase_timeout_seconds():
    """Per-unit budget for the LAUNCH phase of `cluster up` (t_be4b94ca).

    Deliberately NOT the model-load grace: this covers the `sparkrun run
    --no-follow --ensure` call returning, not the weights loading. Env
    HSCC_CLUSTER_UP_LAUNCH_TIMEOUT overrides (garbage falls back to the
    default rather than disabling the bound).
    """
    raw = os.environ.get("HSCC_CLUSTER_UP_LAUNCH_TIMEOUT", "")
    try:
        value = int(raw) if raw else LAUNCH_PHASE_TIMEOUT_DEFAULT_S
    except ValueError:
        value = LAUNCH_PHASE_TIMEOUT_DEFAULT_S
    return value if value > 0 else LAUNCH_PHASE_TIMEOUT_DEFAULT_S


def models_probe_fn(url, timeout=2):
    """GET + parse one unit's own /v1/models (t_be4b94ca, fix 2).

    Delegates to the daemon's proven body-parsing probe (health
    ._proxy_models_probe — "a bare reachability check is not health").
    Returns {"ok", "models", "error"}; NEVER raises. Tests monkeypatch this
    module-level name (the seam), exactly like _launch_guard_status_fn.
    """
    try:
        from hscc_daemon import health as _h
        return _h._proxy_models_probe(url, timeout=timeout)
    except Exception as e:  # daemon unimportable / probe blew up: not serving
        return {"ok": False, "models": [], "error": str(e)}


# Per-node probe budget for the already-serving skip. A live vLLM answers in
# milliseconds; the point is a FAST pre-flight, not a health verdict — on any
# error we launch anyway (fail-open: --ensure makes the launch itself safe).
_UNIT_PROBE_TIMEOUT_S = 2


def _unit_already_serving(entry):
    """(True, served_models) iff EVERY node in the unit's span answers its own
    /v1/models with a parseable NON-EMPTY catalogue; else (False, []).

    Every span node, not just the head: a TP group only serves on the head
    once ALL peers joined — a lone responder is mid-formation, not serving.
    Non-empty body: HTTP 200 + {"data": []} is the hollow-catalogue shape
    (2026-10-11) — reachable and serving nothing is NOT already-up.
    """
    served = []
    for node in entry["nodes"]:
        url = f"http://{node}:{entry['port']}/v1/models"
        try:
            res = models_probe_fn(url, timeout=_UNIT_PROBE_TIMEOUT_S)
        except Exception:
            return False, []
        if not res.get("ok") or not (res.get("models") or []):
            return False, []
        served.extend(res.get("models") or [])
    return True, sorted(set(served))


def _launch_unit(entry, dry_run, guard_status, launch_timeout, daemon_health):
    """One plan entry -> its issued-report dict (guard, skip decision made by
    the caller; this function REFUSES or LAUNCHES, never both).

    A2 launch guard first (same single prefetched status for every unit),
    then the launch under the launch-phase timeout. The report ALWAYS names
    its unit and carries the combined output tail — a timeout (run_cmd ->
    returncode 124) is reported as a failed unit, not a swallowed one.
    """
    unit_id = entry["unit_id"]
    cmd = entry["cmd"]
    if not dry_run:
        for host in entry["nodes"]:
            if not daemon_health._guard_launch_or_refuse(
                    host, entry["port"], entry["nodes"],
                    "cluster-up", unit_id, status=guard_status):
                out = (f"launch guard refused {unit_id} on {host} "
                       f"(see daemon log; alert sent)")
                return {"success": False, "refused": True, "unit_id": unit_id,
                        "cmd": cmd, "command": cmd, "output": out}
    res = _run_or_print(cmd, dry_run, timeout=launch_timeout)
    if dry_run:
        return res
    res["unit_id"] = unit_id
    res.setdefault("command", cmd)
    if res.get("returncode") == 124:
        res["timed_out"] = True
    # One report tail covering BOTH streams (run_cmd captured them separately):
    # what an operator pastes into the incident channel.
    combined = "\n".join(p for p in (res.get("output", ""), res.get("error", ""))
                         if p)
    res["output"] = combined[-2000:]
    return res


def _run_or_print(cmd, dry_run, timeout=None):
    """Print the command; execute it unless ``dry_run``. Returns a result dict.

    ``cmd`` is a list of argv tokens. Always prints the exact command line.
    With ``dry_run`` prints ``--dry-run: not executing`` and returns
    ``{"dry_run": True, "command": cmd, "issued": []}`` — nothing is run. The
    real path runs via ``run_cmd`` (which captures BOTH stdout and stderr, so a
    failure is diagnosable without a manual probe). ``timeout`` defaults to a
    generous fleet-op timeout — these call sites issue `sparkrun run`/`stop`
    for the whole fleet, which take far longer than the 30s default meant for
    quick status calls.
    """
    print(f"Will run: {' '.join(cmd)}")
    if dry_run:
        print("  --dry-run: not executing")
        return {"dry_run": True, "command": cmd, "issued": []}
    res = run_cmd(cmd, timeout=timeout if timeout is not None
                  else _fleet_op_timeout_seconds())
    return {
        "success": bool(res.get("success", res.get("ok", False))),
        "returncode": res.get("returncode"),
        "command": cmd,
        "output": res.get("output", ""),
        "error": res.get("error", ""),
    }


def _post_boot_module():
    """Import the daemon's post_boot module, or None when unavailable.

    Best-effort on purpose: the expected_up marker is an ENHANCEMENT to the
    fleet verbs — a post_boot import failure must never break `cluster up`
    or `cluster down` (same tolerance the daemon gives its own startup hooks).
    """
    try:
        from hscc_daemon import post_boot
        return post_boot
    except Exception:
        try:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if repo_root not in sys.path:
                sys.path.insert(0, repo_root)
            from hscc_daemon import post_boot
            return post_boot
        except Exception:
            return None


def cmd_cluster_down(dry_run=False):
    """Stop ALL sparkrun workloads across the fleet. ``[--dry-run]``.

    The single source of truth for a full fleet down: issues ONE
    ``sparkrun stop --all`` built by ``serving.fleet_down_cmd()``, scoped to
    the configured cluster. Prints the exact command; ``--dry-run`` prints
    without executing. autodown.teardown() uses this same builder.

    t_576964d5: a real (non-dry-run) down ALSO clears the post-boot
    expected_up marker — this verb IS the operator's "stay down" intent, so
    after it the post-boot pass must never relaunch anything (the fail-safe
    direction: the marker is cleared even when the stop itself failed,
    because "down, no relaunch" is always the safe reading of a deliberate
    `cluster down`). ``--dry-run`` touches nothing.
    """
    serving = _load_daemon_serving()
    if serving is None:
        return {"error": "cannot import hscc_daemon.serving (fleet down "
                         "requires the daemon's serving module)"}
    cmd = serving.fleet_down_cmd()
    res = _run_or_print(cmd, dry_run)
    if not dry_run:
        pb = _post_boot_module()
        if pb is not None:
            try:
                pb.clear_expected_up("hscc cluster down")
            except Exception as e:
                print(f"WARN: expected_up marker clear failed: {e}")
    return res


def cmd_cluster_up(dry_run=False, nodes=None):
    """Start EVERY unit in serving.json (orchestrator AND keepalive workers).

    ``[--dry-run]`` prints the commands without executing. The single source
    of truth for a full fleet up: builds every unit's start command via
    ``serving.fleet_up_plan()`` (reusing the existing ``VLLM_START_CMD`` form
    / ``orchestrator_recipe``) and runs them. autodown.autoup() uses this same
    builder.

    ``nodes`` (t_576964d5) narrows the launch to the plan entries whose span
    TOUCHES one of the given hosts — the scoping the post-boot relaunch path
    (hscc_daemon/post_boot.py) needs to relaunch only the units on a node
    that rebooted, through THIS surface rather than a second start path.
    ``None`` (the default, every existing caller) is the full fleet-up plan,
    byte-identical to the previous behaviour.

    Hardened per t_be4b94ca (2026-10-22: an already-serving unit's launch hung
    ~19 min under the 1200s model-load grace, and the SERIAL loop starved the
    remaining units while it hung):

      1. each launch is bounded by the LAUNCH-PHASE timeout (seconds-to-
         minutes, HSCC_CLUSTER_UP_LAUNCH_TIMEOUT) — not the model-load grace;
         a timeout reports that unit failed with the output tail;
      2. a unit whose every span node already answers a parseable non-empty
         /v1/models is reported already-up and sparkrun is NOT invoked;
      3. launches run CONCURRENTLY (units are independent hosts) while the
         per-unit report stays plan-ordered; one hung/failed unit cannot
         prevent the others.

    The A2 launch guard keeps its SINGLE status fetch for the whole plan
    (prefetched here, passed into every unit worker).

    t_576964d5 marker hook (post-boot relaunch), fail-soft — a marker
    problem must never break fleet up:

      * a REAL (non-dry-run) up records the full fleet-up footprint as the
        expected_up set regardless of ``nodes`` scoping (this verb is the
        operator claiming "I want the fleet running"). No post-success stamp:
        a later post-boot pass re-probes unit health, so a unit this call
        brought up is simply "already serving" (no candidate), while one the
        host then took down again correctly stays eligible for relaunch.
    """
    serving = _load_daemon_serving()
    if serving is None:
        return {"error": "cannot import hscc_daemon.serving (fleet up "
                         "requires the daemon's serving module)"}
    plan = serving.fleet_up_plan()
    if nodes:
        # Scope to units whose span touches a named host (a TP unit launches
        # as its whole group, so membership is span-intersection, not head
        # equality). Keeps plan ORDER. Dry-run shows the scoped plan.
        wanted = {str(n) for n in nodes if n}
        plan = [e for e in plan
                if wanted & {str(n) for n in (e.get("nodes") or [])}]
    if not plan:
        return {"error": "no serving units to start (serving.json missing or "
                         "empty?)", "units": 0}
    # A2 launch guard (HEAL_CASCADE 2026-10-14): one status fetch for the whole
    # plan, then every unit is checked host-by-host before its start is issued.
    # A refused unit is reported, never silently skipped; dry-run shows the
    # plan regardless (nothing is launched, so nothing can over-commit).
    from hscc_daemon import health as _daemon_health  # deferred: CLI stays light
    guard_status = (None if dry_run
                    else _daemon_health._launch_guard_status_fn())

    # Record the operator's "expected up" claim BEFORE launching (t_576964d5):
    # the marker says what the fleet SHOULD be running even if this command's
    # own launches then fail or are interrupted. Full footprint, always.
    pb = None if dry_run else _post_boot_module()
    if pb is not None:
        try:
            pb.record_expected_up(serving.fleet_up_plan(),
                                  reason="hscc cluster up")
        except Exception as e:
            print(f"WARN: expected_up marker record failed: {e}")

    if dry_run:
        # Nothing is launched and nothing is probed — print the plan serially
        # so the output stays readable (this is the form operators copy).
        issued = [_run_or_print(e["cmd"], True) for e in plan]
    else:
        launch_timeout = _launch_phase_timeout_seconds()
        issued: list = [None] * len(plan)
        to_launch = []
        # Fix 2: cheap pre-flight FIRST. Sequential on purpose: every probe
        # is bounded at _UNIT_PROBE_TIMEOUT_S and connection failures return
        # in milliseconds; the expensive phase (the launches) is what runs
        # concurrently.
        for i, entry in enumerate(plan):
            serving_now, models = _unit_already_serving(entry)
            if serving_now:
                print(f"Unit {entry['unit_id']} already serving on "
                      f"{','.join(entry['nodes'])}:{entry['port']} "
                      f"({len(models)} model id(s)) — skipping launch")
                issued[i] = {"success": True, "skipped": "already_serving",
                             "unit_id": entry["unit_id"],
                             "command": entry["cmd"],
                             "served_models": models}
            else:
                to_launch.append(i)
        # Fix 3: concurrent launches. Threads because the work is a blocking
        # subprocess per unit; results are stored by PLAN INDEX so the report
        # order is deterministic regardless of completion order.
        if to_launch:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(
                    max_workers=min(8, len(to_launch))) as pool:
                futures = {
                    pool.submit(_launch_unit, plan[i], False, guard_status,
                                launch_timeout, _daemon_health): i
                    for i in to_launch}
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        issued[i] = fut.result()
                    except Exception as exc:
                        # A raising worker must not sink the fleet report.
                        issued[i] = {
                            "success": False, "unit_id": plan[i]["unit_id"],
                            "command": plan[i]["cmd"],
                            "output": f"launcher raised "
                                      f"{type(exc).__name__}: {exc}"}
        issued = [r for r in issued if r is not None]
    return {
        "success": not dry_run and all(i.get("success") for i in issued),
        "dry_run": bool(dry_run),
        "units": len(plan),
        "plan": [
            {"kind": e["kind"], "nodes": e["nodes"], "port": e["port"],
             "unit_id": e["unit_id"], "cmd": e["cmd"],
             "keepalive": e["keepalive"]}
            for e in plan
        ],
        "issued": issued,
    }


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


# ── Orchestrator inject (thin CLI wrapper over hscc-api/repl_inject) ────────

def _load_repl_inject():
    """Import ``repl_inject`` from the sibling hscc-api plugin dir.

    ``repl_inject`` owns pane discovery, the exactly-one-pane rule, the mode
    vocabulary and delivery — this plugin must not re-implement any of it (and
    the unified ``hscc orch inject`` verb reaches here the same way the daemon
    reaches ``serving``: a sibling plugin dir on ``sys.path``). Returns the
    module, or None if hscc-api is not deployed next to this plugin.
    """
    try:
        import repl_inject
        return repl_inject
    except ImportError:
        pass
    api_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hscc-api")
    if os.path.isdir(api_dir) and api_dir not in sys.path:
        sys.path.insert(0, api_dir)
    try:
        import repl_inject
        return repl_inject
    except ImportError:
        return None


class _InjectParser(argparse.ArgumentParser):
    """argparse that refuses by raising, not by SystemExit.

    main()'s error path owns rendering (raw JSON with ``--json``, themed panel
    otherwise, exit 1); a bare parser's SystemExit would bypass that contract
    and print argparse's own stderr instead of the CLI's error surface.
    """

    def error(self, message):
        raise ValueError(f"{self.prog}: {message}")


def cmd_orch_inject(argv=None, repl=None):
    """``hscc orch inject <profile> "<message>"`` — inject a running Hermes REPL.

    A THIN wrapper over ``repl_inject.inject`` (t_75f17c83): the pane
    resolution, busy read and mode->verb decision all stay in that module, so
    the CLI's ``--dry-run`` cannot drift from what a real send does. ``--mode``
    is passed straight through (repl_inject owns the vocabulary
    ``auto|plain|queue|steer``, plus the accepted legacy alias ``interrupt``);
    this function contains no prefix logic and reads no screen of its own.

    Result dict (machine path — ``--json`` dumps it raw): the receipt from
    ``inject()`` — ``pane``, ``target``, ``profile``, ``busy``, ``mode``,
    ``verb``, ``interrupts`` (and ``sent_text`` on a real send, ``text``/
    ``chars`` on a dry run) — plus, on a dry run, the human target ``line``
    (``format_plan``) proving nothing was sent.

    Refusals raise (``repl_inject.InjectError`` for no/ambiguous pane, empty
    text or unknown mode; ``ValueError`` for bad argv), which main() renders as
    its error path (JSON dump with ``--json``, themed panel otherwise; exit 1).

    ``repl=`` injects a stub module for tests; ``argv`` defaults to the real
    tail (``sys.argv[2:]`` on the standalone entry).
    """
    repl = repl or _load_repl_inject()
    if repl is None:
        raise RuntimeError(
            "repl_inject not importable — the hscc-api plugin must be "
            "deployed next to hscc-cluster for `hscc orch inject`")
    ap = _InjectParser(
        prog="hscc orch inject",
        description="Inject a message into the operator's RUNNING Hermes REPL "
                    "pane for <profile> (tmux bracketed paste; exactly one "
                    "pane for the profile or refuse).")
    ap.add_argument("profile", help="profile whose REPL pane receives the message")
    ap.add_argument("message", help="message text to deliver")
    ap.add_argument("--mode", default="auto",
                    help="auto (queue while busy, plain when idle) | plain | "
                         "queue | steer (legacy alias: interrupt) [default: auto]")
    ap.add_argument("--pane", default=None,
                    help="optional tmux pane id/target to pin the target to")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="resolve + report the plan (target, busy, verb); send nothing")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help=argparse.SUPPRESS)  # rendering is main()'s; accept, ignore
    ns = ap.parse_args(argv if argv is not None else sys.argv[2:])
    receipt = dict(repl.inject(ns.profile, ns.message, mode=ns.mode,
                               pane_override=ns.pane, dry_run=ns.dry_run))
    # The card's --json contract: pane, idle/busy, verb, chars. inject()'s
    # dry-run receipt already carries chars (the rstripped text); a real-send
    # receipt gets the message length here — display metadata, not delivery.
    receipt.setdefault("chars", len(ns.message))
    if ns.dry_run:
        # The reference tool's own one-line target rendering, so a dry run
        # prints exactly what the reference tool prints for the same pane.
        receipt["line"] = repl.format_plan(receipt)
    return receipt


def cmd_orch(argv=None):
    """``hscc orch <verb>`` group router. Only ``inject`` exists today.

    The card-title surface is `hscc orch inject ...`; COMMANDS also maps
    `orch-inject`/`inject` directly, so every spelling reaches cmd_orch_inject.
    """
    argv = list(sys.argv[2:] if argv is None else argv)
    if argv and argv[0] == "inject":
        return cmd_orch_inject(argv[1:])
    raise ValueError(f"unknown orch subcommand: {argv[0] if argv else '(none)'} "
                     "(available: inject)")


# ── Command Map ───────────────────────────────────────────────────────────

COMMANDS = {
    "cluster-status": cmd_cluster_status,
    "hosts": cmd_hosts,
    "monitor": cmd_monitor,
    "jobs": cmd_jobs,
    "stop": cmd_stop,
    "down": cmd_cluster_down,
    "up": cmd_cluster_up,
    "info": cmd_info,
    "cluster-template": cmd_cluster_template,
    "profile-status": cmd_profile_status,
    "orch": cmd_orch,
    "orch-inject": cmd_orch_inject,
    "inject": cmd_orch_inject,
}


# ── Themed human rendering (back-compat standalone entry) ─────────────────

def _emit_result(cmd: str, result, as_json: bool = False) -> None:
    """Render a command result the way the standalone ``hscc-cluster`` entry.

    When ``as_json`` is set (machine path — ``--json`` flag) the result is
    dumped raw, BYTE-IDENTICAL to the pre-themed output (daemon / scripts / the
    iOS console parse it). Otherwise a themed human view is rendered through the
    shared ``_theme`` layer (guard: when ``hscc_daemon.cli_theme`` is absent the
    ``_theme`` helper falls back to a plain Console, so output stays plain).
    """
    if as_json:
        print(json.dumps(result, indent=2, default=str))
        return
    console = _theme.make_console()
    emit_err = getattr(result, "error", None) if isinstance(result, dict) else None
    if emit_err is not None:
        body = _theme.escape(str(emit_err))
        usage = result.get("usage") if isinstance(result, dict) else None
        if usage:
            body = f"{body}\n\nusage: {_theme.escape(str(usage))}"
        console.print(_theme.status_panel(body, status="error", title=cmd))
    else:
        lines = _human_lines(cmd, result)
        console.print(_theme.panel(cmd, "\n".join(lines)))


def _human_lines(cmd: str, result) -> list:
    """Build the themed human view lines for a command's result dict.

    The standalone entry point is a back-compat wrapper around engine functions
    that return dicts; the unified ``hscc`` CLI owns the full table renderers
    (hscc_daemon/cluster_render.py). Here we keep a concise, readable themed
    summary — enough to be useful on its own without duplicating that renderer.
    ``escape`` is applied to every value-derived line so literal Rich markup in
    content (e.g. a ``[PRUNE]``-like token) renders literally.
    """
    if not isinstance(result, dict):
        return [_theme.escape(str(result))]
    esc = _theme.escape
    lines = []

    if cmd in ("cluster-status",):
        workloads = result.get("workloads") or []
        lines.append(f"[label]workloads[/label]  {len(workloads)}")
        for w in workloads:
            lines.append(
                f"  {esc(str(w.get('name', '?'))):<48} "
                f"tp={esc(str(w.get('tp', '?')))} pp={esc(str(w.get('pp', '?')))} "
                f"id={esc(str(w.get('container_id', '?')))}")
        idle = result.get("idle_hosts") or []
        total = result.get("total_hosts", 0)
        lines.append(f"[label]total hosts[/label]  {total}")
        lines.append(f"[label]idle hosts[/label]   {len(idle)}")
        if idle:
            lines.append("[dim]" + ", ".join(esc(str(h)) for h in idle) + "[/dim]")
        return lines

    if cmd in ("down", "up", "stop"):
        ok = bool(result.get("success", result.get("ok")))
        tag = "OK" if ok else "ERROR"
        lines.append(f"[{'ok' if ok else 'error'}]{tag}[/]")
        if result.get("command"):
            cmdline = result["command"]
            lines.append("[label]command[/label]  "
                         + (esc(cmdline) if isinstance(cmdline, str)
                            else " ".join(esc(str(x)) for x in cmdline)))
        if result.get("dry_run"):
            lines.append("[dim]--dry-run: nothing executed[/dim]")
        for key in ("output", "error"):
            if result.get(key):
                lines.append(f"[label]{key}[/label]")
                for sub in str(result[key]).split("\n"):
                    lines.append(f"  {esc(sub)}")
        for i, item in enumerate(result.get("issued") or [], 1):
            lines.append(f"[dim]unit {i}[/dim]  "
                         f"{'ok' if item.get('success') else 'error'}")
        if result.get("units"):
            lines.append(f"[label]units[/label]  {result['units']}")
        return lines or ["[dim]command completed[/dim]"]

    if cmd == "profile-status":
        counts = result.get("counts") or {}
        lines.append(f"[label]running tasks[/label]  "
                     f"{result.get('total_running', sum(counts.values()))}")
        for profile, n in sorted(counts.items()):
            lines.append(f"  {esc(str(profile)):<24} {n}")
        return lines

    if cmd in ("orch", "orch-inject", "inject"):
        # The inject receipt: one target line, plus what was/wasn't sent.
        if result.get("line"):
            lines.append(esc(str(result["line"])))
        if result.get("dry_run"):
            lines.append(f"[dim]--dry-run: nothing sent "
                         f"({result.get('chars', '?')} chars)[/dim]")
        elif result.get("target"):
            lines.append(
                f"[ok]injected[/] {esc(str(result.get('mode', '')))} "
                f"-> {esc(str(result['target']))}")
        return lines or ["[dim]command completed[/dim]"]

    # Generic fallback: flatten nested dicts/lists into indented, escaped lines.
    def _flatten(value, depth=0):
        pad = "  " * depth
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{pad}{esc(str(k))}:")
                    _flatten(v, depth + 1)
                else:
                    lines.append(f"{pad}{esc(str(k))}: {esc(str(v))}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    _flatten(item, depth + 1)
                else:
                    lines.append(f"{pad}- {esc(str(item))}")
        else:
            lines.append(f"{pad}{esc(str(value))}")
    _flatten(result)
    return lines


# ── Entry Point ───────────────────────────────────────────────────────────

def main():
    # These commands are merged into the single `hscc` CLI. This entry stays
    # for back-compat (and as the library the merged CLI imports), but point
    # users at the unified command.
    print("note: `hscc-cluster <cmd>` is merged into the main CLI — "
          "use `hscc cluster <cmd>` / `hscc template <cmd>` / `hscc profiles`.",
          file=sys.stderr)
    as_json = "--json" in sys.argv[1:]
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
  down [--dry-run]    Stop ALL sparkrun workloads fleet-wide
  up   [--dry-run]    Start every unit in serving.json (orch + keepalive)
  info                Detailed cluster configuration
  cluster-template    Manage cluster templates (list|preview|apply)
  profile-status      Show running kanban task counts per profile
  orch inject <p> "<msg>" [--mode auto|plain|queue|steer] [--dry-run] [--json]
                      Inject a message into a running Hermes REPL pane
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
        elif cmd in ("down", "up"):
            dry_run = "--dry-run" in sys.argv[2:]
            result = fn(dry_run=dry_run)
        elif cmd in ("orch", "orch-inject", "inject"):
            # The unified `hscc orch inject <profile> "msg" ...` reaches the
            # same engine verb with its tail; cmd_orch_inject owns the flags.
            result = fn(sys.argv[2:])
        elif cmd == "cluster-template":
            # Pass remaining args through to template CLI
            result = fn()
        else:
            result = fn()

        _emit_result(cmd, result, as_json=as_json)
    except Exception as e:
        # Exception path: the machine ``--json`` contract is a raw error JSON
        # dump; otherwise a themed error panel (non-TTY stays plain).
        if as_json:
            print(json.dumps({"error": str(e)}))
        else:
            _theme.make_console().print(
                _theme.status_panel(_theme.escape(str(e)),
                                    status="error", title=cmd))
        sys.exit(1)


if __name__ == "__main__":
    main()
