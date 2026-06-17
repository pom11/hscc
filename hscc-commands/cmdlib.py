"""Cluster incident-response logic for the operator slash commands.

Self-contained on purpose: these handlers run in the gateway process and must
work even when the orchestrator LLM is wedged, so they shell out to ``sparkrun``
directly and read ``~/.hscc/serving.json`` for topology — no cross-plugin import,
no LLM round-trip.

serving.json is the authoritative source of what should be running where: each
unit carries its own ``role`` (orchestrator|worker), ``recipe``, and ``nodes``.
"""
import json
import os
import subprocess

SERVING_JSON = os.path.expanduser("~/.hscc/serving.json")
APPLIED_TEMPLATE = os.path.expanduser("~/.hscc/applied_template.json")
AUTONOMY_FLAG = os.path.expanduser("~/.hscc/autonomy")
SPARKRUN = "sparkrun"
CLUSTER = "hscc"
PORT = 8000
PROXY_PORT = 4000
HEALTH_TIMEOUT = 6
RUN_TIMEOUT = 240

# The hscc-cluster plugin dir (sibling of this plugin in ~/.hermes/plugins or
# ~/dev/hscc). Used to import the template engine + discovery by path.
_PLUGINS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLUSTER_DIR = os.path.join(_PLUGINS_DIR, "hscc-cluster")


def read_units():
    """Units from serving.json, or [] when missing/corrupt."""
    try:
        with open(SERVING_JSON) as fh:
            data = json.load(fh)
        return data.get("units", []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def unit_node(unit):
    nodes = unit.get("nodes") or []
    return nodes[0] if nodes else None


def orchestrator_unit(units):
    for u in units:
        if u.get("role") == "orchestrator" and unit_node(u):
            return u
    return None


def worker_units(units):
    return [u for u in units if u.get("role") == "worker" and unit_node(u)]


def _run(args, timeout=RUN_TIMEOUT):
    """Run a subprocess, never raise — return (ok, stdout, stderr)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return False, "", f"command not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except OSError as e:
        return False, "", str(e)


def _curl_model(node):
    """Return the served model id on a node, or None if unreachable/empty."""
    ok, out, _ = _run(
        ["ssh", "-o", "ConnectTimeout=6", f"spark@{node}",
         f"curl -s --max-time {HEALTH_TIMEOUT} http://localhost:{PORT}/v1/models"],
        timeout=20,
    )
    if not ok or not out:
        return None
    try:
        return json.loads(out)["data"][0]["id"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


def cluster_metrics():
    """One-shot snapshot of CPU/GPU/mem/power metrics across all nodes.
    Returns dict keyed by IP, or empty dict on failure."""
    # sparkrun --json streams continuously — pipe through head -1
    ok, out, err = _run(
        ["bash", "-c", "sparkrun cluster monitor --simple --json 2>/dev/null | head -1"],
        timeout=30,
    )
    if not ok or not out:
        return {}
    try:
        return json.loads(out).get("hosts", {})
    except Exception:
        return {}


def _import_cluster_module(name):
    """Import a module from the hscc-cluster plugin dir by path (best-effort)."""
    import importlib.util
    path = os.path.join(_CLUSTER_DIR, f"{name}.py")
    if not os.path.isfile(path):
        return None
    try:
        import sys as _sys
        modname = f"_hscc_{name}"
        spec = importlib.util.spec_from_file_location(modname, path)
        mod = importlib.util.module_from_spec(spec)
        if _CLUSTER_DIR not in _sys.path:
            _sys.path.insert(0, _CLUSTER_DIR)  # so sibling imports resolve
        # Register BEFORE exec: @dataclass resolves cls.__module__ via sys.modules.
        _sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def applied_template():
    """Read ~/.hscc/applied_template.json → the recorded template dict, or None."""
    try:
        with open(APPLIED_TEMPLATE) as fh:
            data = json.load(fh)
        return data or None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def reapply_template(confirm):
    """Re-apply the active template (D14 recovery contract). Returns a dict.

    {"ok", "mode": "template"|"none", "template"?, "result"?|"error"?}"""
    state = applied_template()
    name = (state or {}).get("template")
    if not name:
        return {"ok": False, "mode": "none",
                "error": "no applied template recorded (~/.hscc/applied_template.json)"}
    ct = _import_cluster_module("cluster_template")
    if ct is None or not hasattr(ct, "apply_template"):
        return {"ok": False, "mode": "template", "template": name,
                "error": "cluster_template engine unavailable"}
    try:
        result = ct.apply_template(name, confirm=confirm)
    except Exception as e:
        return {"ok": False, "mode": "template", "template": name, "error": str(e)[:300]}
    ok = bool(result.get("success")) if confirm else True
    return {"ok": ok, "mode": "template", "template": name, "result": result}


def discovery_snapshot(probe=True):
    """Best-effort live topology map for /status. Never raises → {} on failure."""
    disc = _import_cluster_module("discovery")
    if disc is None:
        return {}
    try:
        return disc.discovery_status({"probe": probe})
    except Exception:
        return {}


def autonomy_flag():
    """Read the ~/.hscc/autonomy flag value ('on'/'off'), or None."""
    try:
        with open(AUTONOMY_FLAG) as fh:
            return fh.read().strip() or None
    except (FileNotFoundError, OSError):
        return None


def proxy_health():
    """Is the sparkrun LiteLLM proxy on :PROXY_PORT answering? bool."""
    ok, out, _ = _run(
        ["curl", "-s", "--max-time", str(HEALTH_TIMEOUT),
         f"http://localhost:{PROXY_PORT}/v1/models"], timeout=12)
    return bool(ok and out)


def template_cli(argv):
    """Route a /template subcommand through the cluster_template CLI. Returns a
    result dict (or {'error': ...})."""
    cli = _import_cluster_module("cluster_template_cli")
    if cli is None or not hasattr(cli, "cmd_cluster_template"):
        return {"error": "cluster_template CLI unavailable"}
    try:
        return cli.cmd_cluster_template(argv)
    except Exception as e:
        return {"error": str(e)[:300]}


def restart_one(unit):
    """Hard stop+run a unit's model on its node. Returns a result dict."""
    node = unit_node(unit)
    recipe = unit.get("recipe")
    label = unit.get("id") or unit.get("role") or node
    if not node or not recipe:
        return {"unit": label, "ok": False, "error": "missing node or recipe"}
    recipe = os.path.expanduser(recipe)
    _run([SPARKRUN, "stop", "--all", "--hosts", node], timeout=60)
    ok, _out, err = _run(
        [SPARKRUN, "run", recipe, "--cluster", CLUSTER, "--hosts", node,
         "--port", str(PORT), "--no-follow", "--ensure"],
        timeout=RUN_TIMEOUT,
    )
    return {"unit": label, "node": node, "ok": ok,
            "error": None if ok else (err or "sparkrun run failed")[:200]}


def ssh_exec(node, remote_cmd, timeout=120):
    """Run a shell command on a remote node via SSH. Returns (ok, stdout, stderr)."""
    return _run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
         f"spark@{node}", remote_cmd],
        timeout=timeout,
    )


def ssh_exec_parallel(nodes, remote_cmd, timeout=120):
    """Run the same command on multiple nodes in parallel. Returns
    {node: (ok, stdout, stderr)}."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        futures = {pool.submit(ssh_exec, n, remote_cmd, timeout): n for n in nodes}
        for fut in futures:
            n = futures[fut]
            try:
                out[n] = fut.result(timeout=timeout + 30)
            except Exception as e:  # noqa: BLE001
                out[n] = (False, "", str(e)[:200])
    return out


def wait_for_ssh_back(node, max_wait=180, probe_interval=5):
    """Poll SSH on node until it answers or max_wait elapsed. Returns bool."""
    import time
    deadline = time.time() + max_wait
    while time.time() < deadline:
        ok, _o, _e = ssh_exec(node, "echo ready", timeout=8)
        if ok:
            return True
        time.sleep(probe_interval)
    return False


REBOOT_REQUIRED_FILE = "/var/run/reboot-required"


def _local_ips():
    """Return set of IPv4 addresses bound to local interfaces."""
    import socket
    ips = set()
    try:
        # Hostname-based lookup
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    # Fallback: parse `ifconfig` / `ip addr` for any IPv4
    ok, out, _ = _run(["bash", "-c", "ifconfig 2>/dev/null || ip -4 addr"], timeout=5)
    if ok and out:
        import re
        for m in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", out):
            ips.add(m)
    return ips


def gateway_runs_on_node(node_ip):
    """True if the gateway (this process) runs on the same host as ``node_ip``.
    Used to decide whether a node reboot would kill the gateway."""
    if not node_ip:
        return False
    locals_ = _local_ips()
    return node_ip in locals_ or node_ip == "127.0.0.1" or node_ip == "localhost"


def gateway_on_cluster():
    """True if the gateway shares a host with ANY unit in serving.json."""
    for u in read_units():
        if gateway_runs_on_node(unit_node(u)):
            return True
    return False
