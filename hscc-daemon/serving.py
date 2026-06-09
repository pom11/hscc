"""Serving configuration and cluster topology resolution."""

import json
import os
import re
import subprocess
import sys
import datetime


# Module-level globals (set by resolve_cluster_config at import)
PRIMARY_NODE = "192.0.2.10"
ORCH_NODES = {"192.0.2.10"}
KEEPALIVE_NODES = set()
VLLM_HEALTH_URL = ""
VLLM_STOP_CMD = ""
VLLM_START_CMD = ""
VLLM_RECIPE = ""
VLLM_PORT = 8000
HSCC_CLUSTER = "hscc"
NAS_HOST = "192.0.2.20"
CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")
SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
PROFILES_DIR = os.path.expanduser("~/.hermes/profiles")
ORCH_ENDPOINT_STATE = os.path.expanduser("~/.hscc/.daemon_orch_endpoint")


def _serving_warn(msg):
    """Loud warning that is safe to call at import time (before log() exists)."""
    fn = globals().get("log")
    if callable(fn):
        fn(msg, "ERROR")
    else:
        print(f"[ERROR] {msg}", file=sys.stderr)


def load_serving(path=SERVING_JSON):
    """Parse serving.json. Return the dict, or None on missing/malformed."""
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            _serving_warn(f"{path} is not a JSON object — using fallback topology")
            return None
        return data
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, TypeError) as e:
        _serving_warn(f"{path} present but unparseable ({e}) — using fallback "
                      f"topology (orchestrator exempt set may be incomplete)")
        return None


def _orchestrator_units(serving):
    if not isinstance(serving, dict):
        return []
    return [u for u in (serving.get("units", []) or [])
            if u.get("role") == "orchestrator" and (u.get("nodes") or [])]


def orchestrator_nodes(serving):
    """Set of every node belonging to an orchestrator unit (reaper exempt set)."""
    nodes = set()
    for u in _orchestrator_units(serving):
        nodes.update(u.get("nodes") or [])
    return nodes


def orchestrator_head(serving):
    """Head endpoint host (nodes[0]) of the first orchestrator unit, or None."""
    units = _orchestrator_units(serving)
    return (units[0].get("nodes") or [None])[0] if units else None


def orchestrator_recipe(serving):
    """Sparkrun recipe of the first orchestrator unit (expanded), or None.

    Authoritative for relaunching the orchestrator vLLM: the hardcoded
    VLLM_RECIPE is only a fallback for when serving.json is absent.
    """
    units = _orchestrator_units(serving)
    if not units:
        return None
    recipe = units[0].get("recipe")
    return os.path.expanduser(recipe) if recipe else None


def serving_port(serving):
    """Top-level serving port, or the hardcoded VLLM_PORT default."""
    try:
        return int((serving or {}).get("port") or VLLM_PORT)
    except (TypeError, ValueError):
        return VLLM_PORT


def orchestrator_endpoint(serving):
    """The single orchestrator base_url (http://head:port/v1), or None."""
    head = orchestrator_head(serving)
    if not head:
        return None
    return f"http://{head}:{serving_port(serving)}/v1"


def compute_base_url_change(current, old_endpoint, new_endpoint):
    """Decide a managed profile's new base_url, or None for no change.

    Rule: a profile that points at the OLD orchestrator endpoint follows it to
    the NEW one. Worker profiles point at their own node (never the orchestrator
    endpoint), so they are never matched — the model split is preserved.
    """
    if old_endpoint == new_endpoint:
        return None
    if current == old_endpoint and current != new_endpoint:
        return new_endpoint
    return None


def _rebuild_vllm_cmds():
    """Rebuild the vLLM health URL + control commands from the current PRIMARY_NODE.

    Must be called after any change to PRIMARY_NODE.
    """
    global VLLM_HEALTH_URL, VLLM_STOP_CMD, VLLM_START_CMD
    VLLM_HEALTH_URL = f"http://{PRIMARY_NODE}:{VLLM_PORT}/health"
    VLLM_STOP_CMD = ["sparkrun", "stop", "--hosts", PRIMARY_NODE]
    VLLM_START_CMD = ["sparkrun", "run", VLLM_RECIPE,
                      "--cluster", HSCC_CLUSTER, "--hosts", PRIMARY_NODE,
                      "--port", str(VLLM_PORT), "--no-follow", "--ensure"]


def _env_keepalive_nodes():
    """Parse HSCC_KEEPALIVE_NODES (comma/space separated IPs) into a set."""
    raw = os.environ.get("HSCC_KEEPALIVE_NODES", "")
    return {tok for tok in re.split(r"[,\s]+", raw) if tok}


def keepalive_nodes(serving):
    """Set of worker nodes flagged keep-alive in serving.json ∪ env override."""
    nodes = set(_env_keepalive_nodes())
    if isinstance(serving, dict):
        for u in (serving.get("units", []) or []):
            if u.get("role") == "worker" and u.get("keepalive"):
                nodes.update(u.get("nodes") or [])
    return nodes


def _resolve_serving_overlay():
    """Overlay serving.json onto PRIMARY_NODE + ORCH_NODES.

    Returns True when topology was applied, False when serving.json is
    absent/invalid. No logging here: this runs at import before log() is defined.
    """
    global PRIMARY_NODE, ORCH_NODES, KEEPALIVE_NODES, VLLM_RECIPE
    serving = load_serving()
    if serving is None:
        ORCH_NODES = {PRIMARY_NODE}
        KEEPALIVE_NODES = _env_keepalive_nodes()
        return False
    head = orchestrator_head(serving)
    if head:
        PRIMARY_NODE = head
    orch_recipe = orchestrator_recipe(serving)
    if orch_recipe:
        VLLM_RECIPE = orch_recipe
    ORCH_NODES = orchestrator_nodes(serving) or {PRIMARY_NODE}
    KEEPALIVE_NODES = keepalive_nodes(serving) - ORCH_NODES
    return True


def resolve_cluster_config():
    """Resolve gateway/workers/NAS from cluster.json, falling back to sparkrun."""
    global NAS_HOST, PRIMARY_NODE, VLLM_HEALTH_URL, ORCH_NODES, KEEPALIVE_NODES
    try:
        with open(CLUSTER_JSON) as f:
            config = json.load(f)
        gateway = config.get("gateway", {})
        workers = config.get("workers", [])
        nas_devices = config.get("nasDevices", [])

        # Primary node = gateway
        if gateway:
            PRIMARY_NODE = gateway.get("ip", PRIMARY_NODE)
        elif workers:
            PRIMARY_NODE = workers[0].get("ip", PRIMARY_NODE)

        # NAS
        if nas_devices:
            NAS_HOST = nas_devices[0].get("ip", NAS_HOST)

        # serving.json overlay
        _resolve_serving_overlay()
        _rebuild_vllm_cmds()
        return

    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, KeyError) as e:
        _serving_warn(f"cluster.json present but unparseable ({e}) — trying "
                      f"sparkrun fallback")

    # Fallback: resolve the default cluster's primary host from sparkrun.
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
                    break
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError,
            subprocess.SubprocessError, OSError) as e:
        _serving_warn(f"sparkrun cluster fallback failed ({e})")

    # serving.json overlay applies on every path
    applied = _resolve_serving_overlay()
    if not applied:
        _serving_warn(f"no serving.json overlay; using PRIMARY_NODE="
                      f"{PRIMARY_NODE} (cluster.json/sparkrun or hardcoded "
                      f"default), ORCH_NODES={sorted(ORCH_NODES)}")
    _rebuild_vllm_cmds()


# Resolve cluster config at import time
resolve_cluster_config()
