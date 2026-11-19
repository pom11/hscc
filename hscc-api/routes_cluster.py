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
import time
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
# GET /v1/cluster/history — the recorder's stored series, READ-ONLY
# ---------------------------------------------------------------------------
#
# WHY THIS ROUTE DOES NOT LOOK AT THE FLEET. Every other route here runs a live
# read (`cmd_monitor` ssh-es to every host). This one answers "what did the
# fleet look like at 14:05", and the only thing that knows is the recorder
# (``hscc_daemon/metrics_history.py``). So the entire data path is the
# recorder's OWN read API — ``MetricsStore.host_rows`` / ``engine_rows`` for
# tiers 1-2, ``MetricsRecorder.tier0_points`` for tier 0 — and nothing else: no
# sampling, no ssh, no sparkrun, and deliberately no ``get_sampler()``, which
# constructs a Sampler on first use and would spawn a
# ``sparkrun cluster monitor`` child from a GET.
# tests/test_cluster_history_parity.py pins that three ways (AST sweep of this
# handler, every live-backing seam poisoned, and a missing store left missing).
#
# THE TIER IS PART OF THE ANSWER, NOT AN IMPLEMENTATION DETAIL. The three tiers
# have different resolutions (2 s / 1 min / 5 min) and different columns (a
# peak only exists after a roll-up), so a response that just said "here is your
# series" would leave a chart guessing whether a point is 2 seconds or 5 minutes
# wide. Every response therefore states `tier`, `resolution_s`, `resolution`,
# `tier_summary` and `max_fields` — the same fields for every window.

#: A host name as the recorder keys it: sparkrun host, DNS name, or a
#: tailnet/IPv6 address. Anything with a space, a slash or a `?` is a client
#: bug rather than a host, and gets a 400 naming the rule.
_HISTORY_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,253}")

#: the window a client may ask for, mapped to the recorder's own (tier,
#: bucket_s, span_s). Built from ``metrics_history``'s constants rather than
#: literals so this route cannot drift from the cadence that produced the data:
#: a client asking for 1 h is being served tier 1's 1-minute buckets because
#: that is what tier 1 IS.
def _history_windows(mh) -> dict:
    return {
        "15m": (0, mh.TIER0_INTERVAL_S, mh.TIER0_WINDOW_S),
        "1h": (1, mh.TIER1_BUCKET_S, 60 * 60),
        "6h": (1, mh.TIER1_BUCKET_S, 6 * 60 * 60),
        "24h": (1, mh.TIER1_BUCKET_S, mh.TIER1_WINDOW_S),
        "7d": (2, mh.TIER2_BUCKET_S, mh.TIER2_WINDOW_S),
    }


#: Keys that identify a row and are never dropped by `metrics=`.
_HISTORY_HOST_IDENTITY = ("tier", "ts", "host", "res", "n", "error")
_HISTORY_ENGINE_IDENTITY = ("tier", "ts", "node", "port", "model", "res", "n",
                            "error")


def _metrics_history():
    """The recorder module, imported on first use (never at import time)."""
    _ensure_repo_root_on_path()
    from hscc_daemon import metrics_history
    return metrics_history


def _history_now() -> float:
    """The wall clock for a window's edge — the seam the parity test pins."""
    return time.time()


def _history_vocab(mh, tier: int) -> tuple:
    """(host field names, engine field names) this tier actually stores.

    Tier 0 holds raw samples, so it has no ``<field>_max`` column: a peak only
    exists once a bucket has been rolled up. Asking for `gpu_temp_c_max` at
    window=15m is therefore a 400, not an empty column.
    """
    host_vocab = tuple(mh.HOST_KEYS) if tier == 0 else tuple(mh.HOST_COLUMNS)
    return host_vocab, tuple(mh.ENGINE_KEYS)


def _history_tier_summary(mh, tier: int, bucket_s: int) -> str:
    """What the served tier is, in the client's own terms."""
    if tier == 0:
        return (f"raw {bucket_s} s samples from the recorder's in-memory ring — "
                "no averaging, and no peak columns")
    step = f"{bucket_s // 60}-minute" if bucket_s % 60 == 0 else f"{bucket_s} s"
    peaks = ", ".join(f"{k}_max" for k in mh.MAX_KEYS)
    return f"{step} averages, with peaks for {peaks}"


def _parse_history_window(query, mh) -> tuple:
    """?window= -> (window, tier, bucket_s, span_s). 400 on anything else."""
    from api_server import ApiError
    windows = _history_windows(mh)
    raw = (query.get("window") or "1h").strip()
    if raw not in windows:
        raise ApiError(
            400, "bad_request",
            f"window must be one of {', '.join(windows)} (got {raw!r}); each "
            "serves a different tier, and the response states which one",
            "window must be 15m, 1 hour, 6 hours, 24 hours or 7 days.",
        )
    tier, bucket_s, span_s = windows[raw]
    return raw, tier, bucket_s, span_s


def _parse_history_node(query) -> str:
    """?node= -> a host name, or 'all'. 400 on an empty or impossible name."""
    from api_server import ApiError
    node = (query.get("node") or "all").strip()
    if not node:
        raise ApiError(400, "bad_request",
                       "node must be 'all' or one host name (an empty value is "
                       "not a request for every host)",
                       "node must be all or one host name.")
    if node != "all" and not _HISTORY_NODE_RE.fullmatch(node):
        raise ApiError(
            400, "bad_request",
            f"node {node!r} is not a host name: use 'all' or a single sparkrun "
            "host / DNS name / tailnet address (letters, digits, . - _ :)",
            "node must be all or a single host name.",
        )
    return node


def _parse_history_metrics(query, mh, tier: int):
    """?metrics=a,b -> the ordered, de-duplicated names to keep, or None for all.

    The vocabulary is the recorder's own (``HOST_KEYS`` / ``HOST_COLUMNS`` /
    ``ENGINE_KEYS``) rather than a list copied into this module, so a rename in
    the recorder shows up here as a rejected name rather than as a silently
    missing column — and a client that asks for a peak at tier 0 is told why,
    instead of receiving a hole.
    """
    from api_server import ApiError
    raw = query.get("metrics")
    if raw is None:
        return None
    names: list = []
    for part in str(raw).split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise ApiError(
            400, "bad_request",
            "metrics must name at least one field (e.g. "
            "metrics=gpu_util_pct,gpu_temp_c); an empty list is not a request "
            "for all of them",
            "metrics must name at least one field.",
        )
    host_vocab, engine_vocab = _history_vocab(mh, tier)
    valid = ", ".join(sorted(set(host_vocab) | set(engine_vocab)))
    for name in names:
        if name in host_vocab or name in engine_vocab:
            continue
        if name in mh.HOST_COLUMNS:      # a peak, valid only once rolled up
            raise ApiError(
                400, "bad_request",
                f"{name} is not stored for a 15 m window: tier 0 keeps raw 2 s "
                "samples, so a peak column exists only after a roll-up "
                "(window=1h, 6h, 24h or 7d)",
                f"{name} is only available for rolled-up windows.",
            )
        raise ApiError(
            400, "bad_request",
            f"unknown metric {name!r} for this window; the recorder stores: "
            f"{valid}",
            f"{name} is not a metric the recorder stores.",
        )
    return names


def _history_host_rows_from_points(points, node: str) -> list:
    """Tier-0 records -> per-node rows, key names unchanged.

    One ring record is one 2 s tick carrying EVERY host, while tiers 1 and 2
    store one row per (tier, ts, host). A chart cannot consume two shapes, so
    the ring is transposed into the row shape — and it is only a transpose:
    every metric key and value comes out as the recorder holds it (a ``None``
    stays ``None``, nothing is averaged, summed or otherwise re-derived), with
    `tier`/`ts`/`n` from the same record.
    """
    rows = []
    for p in points or []:
        for host, metrics in (p.get("hosts") or {}).items():
            if node != "all" and host != node:
                continue
            row = {"tier": 0, "ts": p.get("ts"), "res": p.get("res")}
            row.update(metrics)
            row["host"] = host
            row.setdefault("n", p.get("n", 1))
            rows.append(row)
    return rows


def _history_engine_rows_from_points(points, node: str) -> list:
    """Tier-0 records -> per-engine rows, keyed (node, port) as the recorder keys them."""
    rows = []
    for p in points or []:
        for e in p.get("engines") or []:
            if node != "all" and e.get("node") != node:
                continue
            row = {"tier": 0, "ts": p.get("ts"), "res": p.get("res")}
            row.update(e)
            row.setdefault("n", p.get("n", 1))
            rows.append(row)
    return rows


def _history_project(rows, identity, keep):
    """Keep the row's identity keys plus the requested metrics (None = all)."""
    if keep is None:
        return rows
    allowed = set(identity) | set(keep)
    return [{k: v for k, v in row.items() if k in allowed} for row in rows]


def _backing_history_tier0(node):
    """Tier 0 — the in-memory ring, but ONLY if this process owns it.

    None means "this process is not the sampler's" (the deployed case: the API
    runs as its own ``python -m api_server serve``, so the daemon's ring is
    unreachable from here) and an empty list means "this process does sample and
    holds nothing yet" (the stated post-restart state). The route reports both
    distinctly — an empty list answered as if it were data is how a chart draws
    "nothing happened" over "we cannot see".
    """
    recorder = _metrics_history().resident_recorder()
    if recorder is None:
        return None
    return recorder.tier0_points(None if node == "all" else node)


def _backing_history_rows(hscc_dir, tier, since, until, node):
    """Tiers 1 and 2 — straight out of the recorder's store.

    Returns ``(host_rows, engine_rows, store_info)``. The store is opened only
    when the file already exists: the recorder owns that file, and a widget poll
    against a fresh install must not leave an empty database behind (an empty
    file looks like a data source; a missing one says what it means).
    """
    mh = _metrics_history()
    store = mh.MetricsStore(hscc_dir=hscc_dir)
    exists = os.path.exists(store.path)
    info = {"path": store.path, "exists": exists,
            "db_bytes": os.path.getsize(store.path) if exists else 0}
    if not exists:
        return [], [], info
    name = None if node == "all" else node
    try:
        host_rows = store.host_rows(tier, since=since, until=until, host=name)
        engine_rows = store.engine_rows(tier, since=since, until=until, node=name)
    finally:
        store.close()
    info["error"] = store.error
    return host_rows, engine_rows, info


def _speak_cluster_history(payload) -> str:
    """One sentence naming the window, the resolution and how much is there."""
    if payload.get("error"):
        return f"Cluster history failed: {payload['error']}"
    if payload["tier"] == 0 and not payload["store"]["tier0_resident"]:
        return ("The 15 m window is the daemon's in-memory ring and this server "
                "process does not own it. Ask for window=1h for 1-minute points.")
    counts = payload["counts"]
    return (f"Last {payload['window']} at {payload['resolution']} resolution: "
            f"{counts['hosts']} node point{'s' if counts['hosts'] != 1 else ''}, "
            f"{counts['engines']} engine point{'s' if counts['engines'] != 1 else ''}.")


def handle_cluster_history(server, ctx, query, body):
    """GET /v1/cluster/history?node=<host|all>&metrics=a,b&window=15m|1h|6h|24h|7d

    The recorder's stored per-node and per-engine series, read-only: a request
    here never triggers a sample, an ssh call or a sparkrun child. `window`
    picks the tier (15m -> tier 0's 2 s ring; 1h/6h/24h -> tier 1's 1-minute
    roll-ups; 7d -> tier 2's 5-minute roll-ups) and the response says which tier
    and resolution it served, plus which peak columns it carries.

    Rows come back with the recorder's own field names and its ``None``s
    intact — ``None`` means "not reported", and coercing it to 0 would draw a
    node that never answered as an idle one.
    """
    from api_server import ApiError
    mh = _metrics_history()
    for unsupported in sorted(set(query) - {"node", "metrics", "window"}):
        raise ApiError(
            400, "bad_request",
            f"'{unsupported}' is not supported by /v1/cluster/history: it "
            "accepts 'node', 'metrics' and 'window' only",
            f"{unsupported} is not supported by the cluster history route.",
        )
    window, tier, bucket_s, span_s = _parse_history_window(query, mh)
    node = _parse_history_node(query)
    metrics = _parse_history_metrics(query, mh, tier)

    until = int(_history_now())
    since = until - span_s
    host_vocab, engine_vocab = _history_vocab(mh, tier)
    keep_host = keep_engine = None
    if metrics is not None:
        keep_host = [m for m in metrics if m in host_vocab]
        keep_engine = [m for m in metrics if m in engine_vocab]
    # A series with no requested field is REPORTED as excluded rather than
    # returned as an empty list, so a client cannot read "no engine points" as
    # "the fleet served nothing".
    hosts_included = keep_host is None or bool(keep_host)
    engines_included = keep_engine is None or bool(keep_engine)

    payload = {
        "window": window,
        "window_s": span_s,
        "tier": tier,
        "resolution_s": bucket_s,
        "resolution": mh.RES_TIER0 if tier == 0 else f"{bucket_s}s",
        "tier_summary": _history_tier_summary(mh, tier, bucket_s),
        "max_fields": [c for c in host_vocab
                       if c.endswith("_max")
                       and (metrics is None or c in metrics)],
        "since": since,
        "until": until,
        "node": node,
        "metrics": list(metrics) if metrics is not None else list(host_vocab) + list(engine_vocab),
        "hosts_included": hosts_included,
        "engines_included": engines_included,
    }

    # `tier0_resident` is a MEASUREMENT, not a default. It answers exactly one
    # question — "could THIS process read the 2 s ring?" — and that question only
    # exists when tier 0 is the tier being served. So it starts as None
    # ("nobody measured") and is written only by the tier-0 branch. It used to
    # start as True, which let a `window=6h`/`7d` payload claim a resident ring in
    # a process that hosts no recorder at all (measured on #413's fixtures: the
    # tier-1 and tier-2 bytes said `true`, the `window=15m` bytes from the same
    # generator in the same process said `false`). `note`/`speak` stay the honest
    # signal for an empty tier-0 answer; this key only says what its name says,
    # and only where it was actually asked.
    tier0_resident = None
    error = None
    try:
        if tier == 0:
            points = _backing_history_tier0(node)
            tier0_resident = points is not None
            host_rows = _history_host_rows_from_points(points, node)
            engine_rows = _history_engine_rows_from_points(points, node)
            store_info = {"path": mh.db_path(ctx.hscc_dir),
                          "exists": os.path.exists(mh.db_path(ctx.hscc_dir)),
                          "db_bytes": 0}
            store_info["db_bytes"] = (os.path.getsize(store_info["path"])
                                      if store_info["exists"] else 0)
        else:
            host_rows, engine_rows, store_info = _backing_history_rows(
                ctx.hscc_dir, tier, since, until, node)
    except Exception as exc:
        host_rows, engine_rows = [], []
        store_info = {"path": mh.db_path(ctx.hscc_dir), "exists": False,
                      "db_bytes": 0}
        error = _failure_detail(exc=exc)

    if not hosts_included:
        host_rows = []
    if not engines_included:
        engine_rows = []
    host_rows = _history_project(host_rows, _HISTORY_HOST_IDENTITY, keep_host)
    engine_rows = _history_project(engine_rows, _HISTORY_ENGINE_IDENTITY,
                                   keep_engine)

    store_info["tier0_resident"] = tier0_resident
    payload["store"] = store_info
    payload["hosts"] = host_rows
    payload["engines"] = engine_rows
    payload["counts"] = {"hosts": len(host_rows), "engines": len(engine_rows)}
    if error:
        payload["error"] = error
    payload["note"] = _history_note(tier, tier0_resident, store_info, error)
    payload["speak"] = _speak_cluster_history(payload)
    return 200, payload


def _history_note(tier, tier0_resident, store_info, error) -> str:
    """What the payload says about its own emptiness, so a gap explains itself."""
    if error:
        return f"history read failed: {error}"
    if tier == 0 and not tier0_resident:
        return ("tier 0 is the sampler's in-memory ring and lives in the daemon "
                "process; this server process cannot read it, so no 2 s points "
                "are served here. Use window=1h for the durable 1-minute tier.")
    if tier != 0 and not store_info.get("exists"):
        return ("the recorder has not written its store yet "
                f"({store_info['path']}); the durable tiers are empty until it "
                "closes its first minute")
    return ""


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
        ("GET", r"^/v1/cluster/history$", handle_cluster_history),
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
