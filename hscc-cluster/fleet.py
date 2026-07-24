"""Read-only fleet tools: verify, throughput, stats, autoscale advice."""
import importlib
import os
import sys


def _daemon_mod(name):
    """Import a module from the sibling hscc_daemon plugin, path-robustly."""
    plugins = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    idx = None
    if plugins not in sys.path:
        sys.path.insert(0, plugins)
        idx = 0
    try:
        return importlib.import_module(f"hscc_daemon.{name}")
    finally:
        if idx is not None:
            sys.path.pop(idx)


# ── Schemas (OpenAI function-parameters style) ──

CLUSTER_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

CLUSTER_THROUGHPUT_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

FLEET_STATS_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "Number of days to look back (default 7)",
            "default": 7,
        },
    },
    "additionalProperties": False,
}

AUTOSCALE_ADVICE_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


# ── Handlers ──

def cluster_verify(args, **kwargs):
    """Full compatibility/health smoke-test (plugins, multiplex, streams, proxy, config)."""
    try:
        v = _daemon_mod("verify")
        return v.run_all()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cluster_throughput(args, **kwargs):
    """vLLM token throughput + per-node queue depth across the fleet."""
    try:
        t = _daemon_mod("throughput")
        return t.compute_throughput()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fleet_stats(args, **kwargs):
    """Fleet activity: task completions + tool usage over N days (default 7)."""
    try:
        st = _daemon_mod("stats")
        days = int(args.get("days", 7) or 7)
        return st.compute_stats(since_days=days)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def autoscale_advice(args, **kwargs):
    """Advisory scale up/down/none decision from current queue depth (does NOT scale)."""
    try:
        t = _daemon_mod("throughput")
        a = _daemon_mod("autoscale")
        tp = t.compute_throughput()
        fleet = tp.get("fleet", {})
        current = fleet.get("nodes_ok", 0) or len(tp.get("by_node", {}))
        return {
            "throughput": fleet,
            "decision": a.decide_scale(tp, current_workers=current),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
