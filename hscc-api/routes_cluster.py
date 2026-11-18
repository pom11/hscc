"""HSCC HTTP API — Phase A2: cluster + fleet READ endpoints.

Registers read-only endpoints on ``api_server.ROUTES`` (see docs/DESIGN-api.md
§A Cluster + §A Fleet 7 health, and §B for the ``speak`` field). Every
response carries a first-class ``speak`` string derived from the actual data
in that response — never hardcoded, never fabricated.

Backing (libraries, never CLI text-parsing): the hscc-cluster engine is loaded
exactly like ``hscc_daemon.hscc._load_cluster_engine()`` — insert
``hscc-cluster/`` on sys.path, import its ``hscc.py`` as a module, call the
``cmd_*`` functions directly. Fleet/verify/stats/throughput/autoscale are the
``hscc_daemon`` package modules.

This module exposes :func:`load`, which appends to ``api_server.ROUTES``.
``api_server.py`` imports and calls it last (after ``ROUTES`` / ``ApiError``
exist), so there is no circular import: ``load`` imports ``api_server`` itself,
and importing this module on its own has no side effects. Tests import
``api_server`` (via conftest), which pulls this module in and registers the
routes.

Test seam: every backing call goes through one of the ``_backing_*`` module
functions so tests can monkeypatch them without touching real SSH/sparkrun/GPU
nodes or a live cluster.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

# Resolve the repo root: hscc-api is a sibling of hscc-cluster and hscc_daemon.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Why the last engine load failed. The handlers include this in their error
# payload/speak instead of the old bare "unavailable", which hid the real
# failure (t_8743f98c). Kept module-level so a handler can report it without
# re-running the load.
_LAST_ENGINE_ERROR = ""


# ---------------------------------------------------------------------------
# Backing library loading (mirrors hscc_daemon/hscc.py:_load_cluster_engine)
# ---------------------------------------------------------------------------

def _ensure_repo_root_on_path():
    """Put a root that can import ``hscc_daemon`` on sys.path.

    The repo root is first (worktree/tests). If this API was launched from a
    Homebrew Cellar directory that an upgrade deleted, that root is gone; the
    deployed Hermes plugins directory still contains ``hscc_daemon`` and is
    the same fallback the cluster-engine loader uses.
    """
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    plugins = str(Path.home() / ".hermes" / "plugins")
    if plugins not in sys.path:
        sys.path.append(plugins)
    return root


def _cluster_engine_candidates():
    """Ordered hscc-cluster locations, independent of this module's path.

    The live 2026-11-18 failure was a Homebrew upgrade: the API process had
    been launched from ``/opt/homebrew/Cellar/hscc/2.15.4/...`` and Homebrew
    deleted that Cellar directory while the process kept running. Resolving
    the engine only as ``Path(__file__).parent.parent / "hscc-cluster"`` then
    returned None forever, and every cluster route answered "unavailable".

    A shipped API can therefore be started from a directory that no longer
    exists. Keep the sibling lookup first (repo/worktree tests), then fall
    back to locations that survive an upgrade: the package next to an
    already-imported ``hscc_daemon``, the deployed Hermes plugin, the stable
    Homebrew opt link, and an explicit ``HSCC_CLUSTER_DIR`` override.
    """
    candidates = []
    override = os.environ.get("HSCC_CLUSTER_DIR")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(_REPO_ROOT / "hscc-cluster")
    try:
        import hscc_daemon
        daemon_root = Path(hscc_daemon.__file__).resolve().parent.parent
        candidates.append(daemon_root / "hscc-cluster")
    except Exception:
        pass
    candidates.append(Path.home() / ".hermes" / "plugins" / "hscc-cluster")
    candidates.append(Path("/opt/homebrew/opt/hscc/libexec/hscc/hscc-cluster"))
    seen = set()
    ordered = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def _load_cluster_engine():
    """Load hscc-cluster/hscc.py as a module (mirrors _load_cluster_engine).

    Returns the loaded module, or None if the plugin cannot be found/loaded.
    The engine's own submodules import each other by bare name, so the
    containing directory must be on sys.path while we call into it.
    """
    global _LAST_ENGINE_ERROR
    candidates = _cluster_engine_candidates()
    load_errors = []
    for cluster_dir in candidates:
        cluster_hscc = cluster_dir / "hscc.py"
        if not cluster_hscc.is_file():
            continue
        if str(cluster_dir) not in sys.path:
            sys.path.insert(0, str(cluster_dir))
        try:
            spec = importlib.util.spec_from_file_location(
                "hscc_cluster_engine", str(cluster_hscc))
            if spec is None or spec.loader is None:
                load_errors.append(f"{cluster_hscc}: no import spec")
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _LAST_ENGINE_ERROR = ""
            return mod
        except Exception as exc:  # report the real cause, try the next path
            load_errors.append(f"{cluster_hscc}: {type(exc).__name__}: {exc}")
    looked = ", ".join(str(p) for p in candidates)
    _LAST_ENGINE_ERROR = (
        "hscc-cluster engine not found (looked in: "
        f"{looked}); load errors: {'; '.join(load_errors) or 'none'}")
    return None


# ---------------------------------------------------------------------------
# Backing-call seam (monkeypatch these in tests)
# ---------------------------------------------------------------------------

def _backing_cluster_status():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_cluster_status()


def _backing_cluster_hosts():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_hosts()


def _backing_cluster_monitor():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_monitor()


def _backing_cluster_jobs():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_jobs()


def _backing_cluster_info():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_info()


def _backing_serving_snapshot():
    """The CLI monitor's serving section: one ``ServingPoller`` sample.

    ``cluster_render.render_monitor`` builds exactly this object for
    ``hscc cluster monitor`` (hscc_daemon/cluster_render.py:140-151). Returning
    the rows alongside the sparkrun frame is what gives a widget the model
    served per node, running/waiting, KV, and token totals/rates — the parts of
    the CLI view that the raw sparkrun frame does not carry.
    """
    _ensure_repo_root_on_path()
    from hscc_daemon import cluster_live
    poller = cluster_live.ServingPoller()
    try:
        rows = poller.poll()
    finally:
        poller.close()
    return {"rows": rows, "proxy_error": poller.proxy_error}


def _backing_verify():
    from hscc_daemon import verify
    return verify.run_all()


def _backing_stats(days):
    from hscc_daemon import stats
    return stats.compute_stats(since_days=days)


def _backing_throughput():
    from hscc_daemon import throughput
    return throughput.compute_throughput()


def _backing_usage():
    from hscc_daemon import usage
    return usage.compute_usage()


def _backing_streams():
    from hscc_daemon import state
    return state.read_all_states()


def _backing_autoscale():
    from hscc_daemon import throughput, autoscale
    tp = throughput.compute_throughput()
    current = tp.get("fleet", {}).get("nodes_ok", 0) or len(tp.get("by_node", {}))
    return autoscale.decide_scale(tp, current_workers=current)


def _is_error_dict(data) -> bool:
    """True when a backing call returned a failure-shaped dict."""
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return True
    # run_cmd-style results: success False with no usable payload.
    if data.get("success") is False and not data.get("json"):
        return True
    return False


def _one_line(value, limit=500) -> str:
    """Collapse a multi-line command error into one readable sentence."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _failure_detail(data=None, exc=None) -> str:
    """A specific reason for a degraded route — never the word "unavailable".

    The old handlers swallowed every cause into "… unavailable", which is why
    the live 2026-11-18 outage (a Homebrew upgrade deleted the directory the
    API process was launched from) was undiagnosable from the wire. The detail
    is the backing function's own ``error``/exit status, the engine-load
    reason, or the exception class + message. It is command/stderr text, never
    the auth token.
    """
    if exc is not None:
        return f"{type(exc).__name__}: {_one_line(exc)}"
    if isinstance(data, dict):
        if data.get("error"):
            return _one_line(data["error"])
        rc = data.get("returncode")
        output = data.get("output")
        if rc is not None:
            detail = f"command exited {rc}"
            if output:
                detail += f": {_one_line(output)}"
            return detail
        if output:
            return _one_line(output)
    if data is None and _LAST_ENGINE_ERROR:
        return _one_line(_LAST_ENGINE_ERROR)
    return "backing command returned no data"


def _degraded(lead, data=None, exc=None):
    detail = _failure_detail(data, exc)
    return {"error": detail, "speak": f"{lead}: {detail}"}


# ---------------------------------------------------------------------------
# speak helpers (pure — take the computed dict, return the sentence)
# ---------------------------------------------------------------------------

def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _speak_cluster_status(data):
    """§B: "{total} hosts up. {n} workload(s) running, {i} idle."."""
    total = data.get("total_hosts", 0)
    workloads = data.get("workloads", [])
    idle = data.get("idle_hosts", [])
    return (
        f"{total} hosts up. "
        f"{len(workloads)} workload{'s' if len(workloads) != 1 else ''} running, "
        f"{len(idle)} idle."
    )


def _count_saved_clusters(saved):
    """Count saved clusters defensively (list -> len; run_cmd dict -> best-effort)."""
    if isinstance(saved, list):
        return len(saved)
    if isinstance(saved, dict):
        # run_cmd result carrying parsed json (e.g. {"name": ..., "hosts": [...]}).
        parsed = saved.get("json")
        if isinstance(parsed, list):
            return len(parsed)
        if isinstance(parsed, dict) and parsed:
            return 1
    return None


def _speak_cluster_hosts(data):
    """§B: "{len(hosts)} hosts registered. {N} cluster(s) saved."."""
    hosts = data.get("hosts", [])
    n = _count_saved_clusters(data.get("saved_clusters"))
    base = f"{len(hosts)} hosts registered."
    if n is not None:
        return f"{base} {_plural(n, 'cluster')} saved."
    return base


def _speak_cluster_monitor(data):
    """§B: compact aggregate from the JSON, else "fleet monitor unavailable"."""
    parsed = data.get("json") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or not parsed:
        return "fleet monitor unavailable"
    # Prefer a host/node count if the sample names them; else count metrics.
    for key in ("nodes", "hosts"):
        val = parsed.get(key)
        if isinstance(val, (list, dict)) and val:
            return f"Fleet snapshot: {len(val)} {'host' if len(val) == 1 else 'hosts'} sampled."
    return f"Fleet snapshot: {len(parsed)} metric{'s' if len(parsed) != 1 else ''} reported."


def _speak_cluster_jobs(data):
    """§B: "{count} job(s) running." from the output, else "job list unavailable"."""
    output = data.get("output") if isinstance(data, dict) else None
    if output is None:
        return "job list unavailable"
    count = 0
    for line in str(output).splitlines():
        line = line.strip()
        if not line:
            continue
        if "Total:" in line and "job" in line.lower():
            # e.g. "Total: 3 job(s) across ..." — extract the leading integer.
            tokens = line.split()
            if tokens and tokens[0].lstrip("-").isdigit():
                count = int(tokens[0])
                break
        elif line.startswith(("Job:", "Pending:", "Running:", "Queued:")):
            count += 1
    return _plural(count, "job") + " running."


def _speak_cluster_info(data):
    """§B: "Cluster configuration loaded." / "cluster info unavailable"."""
    if not isinstance(data, dict):
        return "cluster info unavailable"
    return "Cluster configuration loaded."


def _speak_health(data):
    """§B: ok ? "All checks passed." : "{N} of {total} checks have problems."."""
    checks = data.get("checks", [])
    ok = data.get("ok")
    if ok:
        return "All checks passed."
    failed = [c for c in checks if not c.get("ok")]
    total = len(checks)
    names = ", ".join(str(c.get("name", "?")) for c in failed)
    sentence = f"{len(failed)} of {total} checks have problems."
    if names:
        sentence += f" ({names})"
    return sentence


def _speak_stats(data):
    """§B: "Last {days} days: {key stat}." — the single most useful number."""
    days = data.get("since_days", 7)
    total = (data.get("completions") or {}).get("total", 0)
    return f"About {total} work items across the last {days} days."


def _speak_throughput(data):
    """§B: "{nodes_ok} of {nodes_total} nodes healthy". """
    fleet = data.get("fleet", {})
    ok = fleet.get("nodes_ok", 0)
    total = fleet.get("nodes_total", 0)
    return f"{ok} of {total} nodes healthy."


def _speak_usage(data):
    """§B: budget/activity sentence from the real usage dict.

    Prefers a cost sentence when cost is actually tracked; otherwise reports
    the real token activity across bots and projects. Never fabricated.
    """
    budget = data.get("budget") or {}
    total = data.get("total") or {}
    spent = budget.get("spent_usd", 0.0)
    configured = budget.get("configured", False)
    n_bots = len(data.get("per_bot", {}))
    n_projects = len(data.get("per_project", {}))
    activity = f"{n_bots} bots across {n_projects} projects"
    if data.get("cost_tracked"):
        pct = budget.get("pct", 0.0)
        if budget.get("exceeded"):
            return f"Budget exceeded: ${spent:.2f} over budget ({pct:.0f}%). {activity}."
        if configured:
            return (f"{activity}: ${spent:.2f} spent "
                    f"({pct:.0f}% of ${budget.get('budget_usd', 0):.2f} budget).")
        return f"{activity}: ${spent:.2f} tracked spend."
    # Cost is not tracked — report the real token activity honestly.
    total_tokens = total.get("total_tokens", 0)
    if total_tokens:
        return (f"{activity}: {total_tokens:,} tokens used; cost not tracked "
                "on this cluster.")
    return f"{activity}; cost not tracked on this cluster."


def _speak_streams(data):
    """§B: "Daemon streams: all ok." / enumerate blocked/failed ones."""
    if not isinstance(data, dict):
        return "Daemon streams: status unavailable."
    blocked = [
        name for name, s in data.items()
        if isinstance(s, dict) and s.get("ok") is not True
    ]
    if not blocked:
        return "Daemon streams: all ok."
    return f"Daemon streams: {len(blocked)} blocked: {', '.join(blocked)}."


def _speak_autoscale(data):
    """§B: humanize the decision."""
    action = data.get("action") if isinstance(data, dict) else None
    if action == "scale_up":
        tgt = data.get("target")
        if tgt is not None:
            return f"Autoscale suggests scaling up to {tgt} workers."
        return "Autoscale suggests scaling up."
    if action == "scale_down":
        tgt = data.get("target")
        if tgt is not None:
            return f"Autoscale suggests scaling down to {tgt} workers."
        return "Autoscale suggests scaling down."
    if action == "none":
        return "Autoscale: nothing to change."
    return "Autoscale: no decision available."


# ---------------------------------------------------------------------------
# Handlers (read-only; 200 with degraded speak on a backing failure)
# ---------------------------------------------------------------------------

def handle_cluster_status(server, ctx, query, body):
    try:
        data = _backing_cluster_status()
    except Exception as exc:
        return 200, {
            "workloads": [], "idle_hosts": [], "total_hosts": 0,
            **_degraded("Cluster status failed", exc=exc),
        }
    if data is None or _is_error_dict(data):
        return 200, {
            "workloads": [], "idle_hosts": [], "total_hosts": 0,
            **_degraded("Cluster status failed", data),
        }
    return 200, {
        "workloads": data.get("workloads", []),
        "idle_hosts": data.get("idle_hosts", []),
        "total_hosts": data.get("total_hosts", 0),
        "speak": _speak_cluster_status(data),
    }


def handle_cluster_hosts(server, ctx, query, body):
    try:
        data = _backing_cluster_hosts()
    except Exception as exc:
        return 200, _degraded("Host list failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Host list failed", data)
    payload = {
        "hosts": data.get("hosts", []),
        "saved_clusters": data.get("saved_clusters", {}),
        "live_status": data.get("live_status", {}),
    }
    payload["speak"] = _speak_cluster_hosts(payload)
    return 200, payload


def handle_cluster_monitor(server, ctx, query, body):
    try:
        data = _backing_cluster_monitor()
    except Exception as exc:
        return 200, _degraded("Fleet monitor failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Fleet monitor failed", data)
    payload = {**data, "speak": _speak_cluster_monitor(data)}
    # Add the same serving rows `hscc cluster monitor` prints beside the host
    # table (model per node, run/wait, KV, token totals and rates). Additive:
    # older clients decode only `json`, new ones may read `serving`.
    try:
        serving = _backing_serving_snapshot()
    except Exception as exc:
        payload["serving"] = []
        payload["serving_error"] = _failure_detail(exc=exc)
    else:
        payload["serving"] = serving.get("rows", [])
        payload["serving_proxy_error"] = serving.get("proxy_error")
    return 200, payload


def handle_cluster_jobs(server, ctx, query, body):
    try:
        data = _backing_cluster_jobs()
    except Exception as exc:
        return 200, _degraded("Job list failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Job list failed", data)
    return 200, {**data, "speak": _speak_cluster_jobs(data)}


def handle_cluster_info(server, ctx, query, body):
    try:
        data = _backing_cluster_info()
    except Exception as exc:
        return 200, _degraded("Cluster info failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Cluster info failed", data)
    return 200, {**data, "speak": _speak_cluster_info(data)}


def handle_health(server, ctx, query, body):
    try:
        data = _backing_verify()
    except Exception:
        return 200, {"speak": "health check unavailable"}
    if not isinstance(data, dict):
        return 200, {"speak": "health check unavailable"}
    return 200, {
        "ok": data.get("ok", False),
        "checks": data.get("checks", []),
        "speak": _speak_health(data),
    }


def _parse_days(query):
    """parse ?days=N mirroring _handle_stats: default 7, int, clamped >= 0."""
    raw = query.get("days")
    if raw is None:
        return 7
    try:
        days = int(raw)
    except (ValueError, TypeError):
        return 7
    return max(days, 0)


def handle_fleet_stats(server, ctx, query, body):
    days = _parse_days(query)
    try:
        data = _backing_stats(days)
    except Exception:
        return 200, {"speak": "fleet stats unavailable"}
    if not isinstance(data, dict):
        return 200, {"speak": "fleet stats unavailable"}
    return 200, {**data, "speak": _speak_stats(data)}


def handle_fleet_throughput(server, ctx, query, body):
    try:
        data = _backing_throughput()
    except Exception as exc:
        return 200, _degraded("Fleet throughput failed", exc=exc)
    if not isinstance(data, dict) or data.get("fleet") is None:
        return 200, _degraded("Fleet throughput failed", data)
    return 200, {**data, "speak": _speak_throughput(data)}


def handle_fleet_usage(server, ctx, query, body):
    try:
        data = _backing_usage()
    except Exception:
        return 200, {"speak": "fleet usage unavailable"}
    if not isinstance(data, dict):
        return 200, {"speak": "fleet usage unavailable"}
    return 200, {**data, "speak": _speak_usage(data)}


def handle_fleet_streams(server, ctx, query, body):
    try:
        data = _backing_streams()
    except Exception:
        return 200, {"speak": "Daemon streams: status unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Daemon streams: status unavailable."}
    return 200, {"streams": data, "speak": _speak_streams(data)}


def handle_autoscale(server, ctx, query, body):
    try:
        data = _backing_autoscale()
    except Exception:
        return 200, {"speak": "Autoscale: no decision available."}
    if not isinstance(data, dict):
        return 200, {"speak": "Autoscale: no decision available."}
    return 200, {**data, "speak": _speak_autoscale(data)}


# ---------------------------------------------------------------------------
# Route registration (import side-effect; loaded by api_server.py)
# ---------------------------------------------------------------------------

def load():
    """Register all cluster + fleet read routes on api_server.ROUTES.

    Imported last by api_server.py (after ROUTES / ApiError exist); api_server
    is imported here so this module never creates a circular import when
    imported on its own. Each route is ``(method, compiled path regex,
    handler)`` — handlers are plain functions
    ``(server, ctx, query, body) -> (status, payload_dict)``.
    """
    import api_server

    routes = [
        ("GET", r"^/v1/cluster/status$", handle_cluster_status),
        ("GET", r"^/v1/cluster/hosts$", handle_cluster_hosts),
        ("GET", r"^/v1/cluster/monitor$", handle_cluster_monitor),
        ("GET", r"^/v1/cluster/jobs$", handle_cluster_jobs),
        ("GET", r"^/v1/cluster/info$", handle_cluster_info),
        ("GET", r"^/v1/health$", handle_health),
        ('GET', r'^/v1/fleet/stats$', handle_fleet_stats),
        ('GET', r'^/v1/fleet/throughput$', handle_fleet_throughput),
        ('GET', r'^/v1/fleet/usage$', handle_fleet_usage),
        ('GET', r'^/v1/fleet/streams$', handle_fleet_streams),
        ("GET", r"^/v1/autoscale$", handle_autoscale),
    ]
    for method, pattern, handler in routes:
        api_server.ROUTES.append((method, re.compile(pattern), handler))
