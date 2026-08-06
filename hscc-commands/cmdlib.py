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
CLUSTER_JSON = os.path.expanduser("~/.hscc/cluster.json")
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


def keepalive_worker_units(units):
    """Return workers with keepalive=true (never includes orchestrator)."""
    return [u for u in units if u.get("role") == "worker"
            and u.get("keepalive") is True and unit_node(u)]


def check_unit_health(node, port=None):
    """HTTP GET http://<node>:<port>/health → True if 2xx, else False."""
    port = port or PORT
    ok, _, _ = _run(
        ["ssh", "-o", "ConnectTimeout=6", f"spark@{node}",
         f"curl -sf -o /dev/null --max-time {HEALTH_TIMEOUT} "
         f"http://localhost:{port}/health"],
        timeout=20,
    )
    return bool(ok)


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
    port = unit.get("port") or PORT
    _run([SPARKRUN, "stop", recipe, "--hosts", node], timeout=60)
    ok, _out, err = _run(
        [SPARKRUN, "run", recipe, "--cluster", CLUSTER, "--hosts", node,
         "--port", str(port), "--no-follow", "--ensure"],
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


# ── cluster.json node-state engine ───────────────────────────────────────────
# The /cluster command enumerates nodes, not just unit primaries. These helpers
# read the authoritative 4-node cluster.json and classify EVERY node into
# serving / tp_peer / idle / unreachable. tp classification comes from the
# serving.json scoreboard (a node that is a peer in a multi-node span is never
# probed for a model at its own :8000 — it is a tp peer, period).


def read_cluster_json():
    """All hosts from ~/.hscc/cluster.json as a list of dicts.

    The gateway entry plus each worker plus each nasDevice (if present). Each
    entry carries ip/name/id/role when present in the file. Missing or corrupt
    file → [] (callers fall back to serving.json unit nodes). Never drops a
    node.
    """
    try:
        with open(CLUSTER_JSON) as fh:
            d = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(d, dict):
        return []
    hosts = []
    gw = d.get("gateway")
    if isinstance(gw, dict) and gw.get("ip"):
        hosts.append(gw)
    for w in d.get("workers") or []:
        if isinstance(w, dict) and w.get("ip"):
            hosts.append(w)
    for n in d.get("nasDevices") or []:
        if isinstance(n, dict) and n.get("ip"):
            hosts.append(n)
    return hosts


def serving_unit_scoreboard():
    """Map every node named in serving.json to its serving-lineage info.

    {node_ip: {"role": <orchestrator|worker>, "unit_id", "model", "port",
               "tp_peer", "primary_of_unit"}}
    For a unit with >1 node (or tp>1) the FIRST node is the primary and the
    REST are tp_peer. A node can only be primary once: if a node is primary in
    one unit and a peer in another, the primary role wins (dominant). Every
    unit node appears exactly once.
    """
    score = {}
    for u in read_units():
        nodes = u.get("nodes") or []
        tp = int(u.get("tp") or 1)
        multi = len(nodes) > 1 or tp > 1
        role = "orchestrator" if u.get("role") == "orchestrator" else "worker"
        base = {
            "role": role,
            "unit_id": u.get("id"),
            "model": u.get("model"),
            "port": u.get("port") or PORT,
        }
        for i, ip in enumerate(nodes):
            primary = (i == 0)          # first entry is this unit's primary
            tp_peer = multi and not primary
            prev = score.get(ip)
            if prev is None:
                entry = dict(base)
                entry["tp_peer"] = tp_peer
                entry["primary_of_unit"] = primary
                score[ip] = entry
                continue
            # Already seen. Primary is dominant: never downgrade a primary, and
            # upgrade a peer to primary if it is primary in this unit.
            if prev["primary_of_unit"]:
                continue                 # keep the primary unit's lineage
            if primary:
                entry = dict(base)
                entry["tp_peer"] = False
                entry["primary_of_unit"] = True
                score[ip] = entry
            else:
                prev["tp_peer"] = True   # peer in another unit too
    return score


def _node_reachable(node_ip):
    """Reachability probe: local gateway node always reachable, else SSH echo."""
    if gateway_runs_on_node(node_ip):
        return True
    ok, _o, _e = ssh_exec(node_ip, "echo ready", timeout=8)
    return bool(ok)


def classify_node(node_ip, scoreboard, probe_fn=_curl_model, reachability_fn=None):
    """Classify one node into serving / tp_peer / idle / unreachable.

    Returns {"ip", "state", "model", "detail"} with state ∈ the four classes.
    Precedence: tp_peer > serving > unreachable > idle. A tp peer is NEVER
    down — its state is "tp_peer" even if its own :8000 doesn't answer (its
    model lives on the span's primary). Serving is decided by the endpoint
    probe (a loaded-but-idle vLLM whose /v1/models answers is still serving);
    the reachability probe only distinguishes idle from unreachable.
    """
    reachability = reachability_fn if reachability_fn is not None else _node_reachable
    s = (scoreboard or {}).get(node_ip) or {}
    if s.get("tp_peer"):
        return {"ip": node_ip, "state": "tp_peer", "model": None,
                "detail": "tp peer of a multi-node span"}
    model = probe_fn(node_ip)
    if model:
        return {"ip": node_ip, "state": "serving", "model": model, "detail": ""}
    if not reachability(node_ip):
        return {"ip": node_ip, "state": "unreachable", "model": None,
                "detail": "no response on reachability probe"}
    return {"ip": node_ip, "state": "idle", "model": None,
            "detail": "reachable, no model served"}


def enumerate_cluster_nodes(*, probe_fn=_curl_model, reachability_fn=None):
    """Enumerate every cluster.json node, each classified. cluster.json-driven.

    The authoritative host list from cluster.json, each classified against the
    serving.json scoreboard via classify_node. If cluster.json is empty/corrupt,
    fall back to the serving.json unit nodes (each classified). NEVER omits a
    node present in cluster.json. Every returned dict carries the classification
    {"ip","state","model","detail"} plus the node's cluster.json name/id/role
    when present.
    """
    hosts = read_cluster_json()
    score = serving_unit_scoreboard()
    if not hosts:
        # Fallback: enumerate the serving.json unit nodes instead.
        for ip in score:
            hosts.append({"ip": ip, "name": None, "id": None, "role": None})
    out = []
    for h in hosts:
        ip = h.get("ip")
        if not ip:
            continue
        node = classify_node(ip, score, probe_fn=probe_fn,
                             reachability_fn=reachability_fn)
        node["name"] = h.get("name")
        node["id"] = h.get("id")
        node["role"] = h.get("role")
        out.append(node)
    return out
