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


def load_serving(path=None):
    """Parse serving.json. Return the dict, or None on missing/malformed.

    ``path`` defaults to the module-level SERVING_JSON read at CALL time (not a
    default-arg bound at definition), so monkeypatching ``serving.SERVING_JSON``
    or changing it at runtime actually takes effect.
    """
    if path is None:
        path = SERVING_JSON
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


def _worker_recipe_for(node, serving):
    """Resolve the sparkrun recipe for a worker node from serving.json.

    Each keep-alive worker unit carries its own ``recipe`` — authoritative,
    because the global VLLM_RECIPE is the ORCHESTRATOR's recipe (may differ from
    what a worker should serve). Returns None if no worker unit names this node,
    so the caller skips relaunch rather than provisioning the wrong model.
    """
    if not isinstance(serving, dict):
        return None
    for u in (serving.get("units", []) or []):
        if u.get("role") == "worker" and node in (u.get("nodes") or []):
            recipe = u.get("recipe")
            if recipe:
                return os.path.expanduser(recipe)
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


def keepalive_units(serving):
    """Unit-keyed keep-alive workers for multi-model-per-node supervision (G1).

    Returns a list of {node, port, recipe, id} — one per keep-alive worker unit.
    A node may appear multiple times on different ports (co-located models), so
    the daemon health-checks + relaunches each (node,port) independently. Port
    comes from the unit (v2 schema), else the serving-level port / VLLM_PORT.
    """
    out = []
    default_port = serving_port(serving) if isinstance(serving, dict) else VLLM_PORT
    seen = set()
    if isinstance(serving, dict):
        for u in (serving.get("units", []) or []):
            if u.get("role") != "worker" or not u.get("keepalive"):
                continue
            recipe = u.get("recipe")
            port = int(u.get("port") or default_port)
            for node in (u.get("nodes") or []):
                key = (node, port)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"node": node, "port": port,
                            "recipe": os.path.expanduser(recipe) if recipe else None,
                            "id": u.get("id") or f"{node}:{port}"})
    for node in _env_keepalive_nodes():
        key = (node, default_port)
        if key not in seen:
            seen.add(key)
            out.append({"node": node, "port": default_port, "recipe": None,
                        "id": f"{node}:{default_port}"})
    return out


def fleet_down_cmd():
    """`sparkrun stop --all` — stop ALL sparkrun workloads fleet-wide.

    The SINGLE fleet-down command. The raw sparkrun stop string for a full
    fleet down is built HERE and here only, then consumed by BOTH the
    ``hscc cluster down`` CLI wrapper (hscc-cluster/hscc.py) and
    ``autodown.teardown()`` — autodown no longer constructs its own raw
    per-unit stop strings. ``--all`` stops every sparkrun container on the
    configured cluster, so a full teardown powers the ENTIRE serving layer
    down (orchestrator AND keepalive workers) — the whole point of the feature.
    """
    return ["sparkrun", "stop", "--all", "--cluster", HSCC_CLUSTER]


def _validate_unit_serve_cmd(unit):
    """Validates a unit's recorded ``serve_cmd`` against its OWN ``nodes`` /
    ``port`` / ``recipe`` fields. Returns a list of human-readable violations;
    empty means the serve_cmd agrees with the unit (or is absent).

    A ``serve_cmd`` is the start command stamped into serving.json at provision
    time (see cluster_template._render_serve_cmd). It is data from the past —
    transcribed from the unit's fields — so it must never be TRUSTED to override
    the unit's own fields. If it disagrees (hosts outside the node list, a port
    that isn't the unit's, a recipe that isn't the unit's), it is bogus fixture
    data and MUST NOT be executed: an autoup/start path that trusts a stale
    serve_cmd could launch a model on hosts that are not this cluster
    (t_501fb7f1 — a leaked test serve_cmd naming 10.0.0.1/9000 landed in live
    ~/.hscc/serving.json).

    Normalization: the serve_cmd stores the recipe path-expanded and nodes
    comma-joined and port as a string, while the unit keeps recipe possibly
    tilde'd, nodes as a list, port as an int. We compare node SETS (order
    doesn't matter), ports as ints, and recipes after expanduser on both sides.
    """
    if not isinstance(unit, dict):
        return []
    serve_cmd = unit.get("serve_cmd")
    if not serve_cmd:
        return []
    if not isinstance(serve_cmd, list):
        return [f"unit {unit.get('id')}: serve_cmd is not a list ({serve_cmd!r})"]

    nodes = [n for n in (unit.get("nodes") or []) if n]
    port = unit.get("port")
    recipe = unit.get("recipe")

    cmd_nodes = None
    cmd_port = None
    cmd_recipe = None
    i = 0
    argv = list(serve_cmd)
    while i < len(argv):
        tok = argv[i]
        if tok == "--hosts" and i + 1 < len(argv):
            cmd_nodes = [n for n in str(argv[i + 1]).split(",") if n]
            i += 2
        elif tok == "--port" and i + 1 < len(argv):
            try:
                cmd_port = int(argv[i + 1])
            except (TypeError, ValueError):
                cmd_port = None
            i += 2
        elif tok.startswith("-"):
            i += 2 if (i + 1 < len(argv) and not str(argv[i + 1]).startswith("-")) else 1
        else:
            # first non-flag token after "sparkrun run" is the recipe
            if cmd_recipe is None and tok not in ("sparkrun", "run"):
                cmd_recipe = tok
            i += 1

    problems = []
    unit_id = unit.get("id") or ",".join(nodes) or "?"

    def _norm(path):
        return os.path.normpath(os.path.expanduser(path)) if path else path

    if nodes and cmd_nodes is not None and set(cmd_nodes) != set(nodes):
        problems.append(
            f"unit {unit_id}: serve_cmd --hosts {sorted(cmd_nodes)} disagrees "
            f"with unit nodes {sorted(nodes)} — REFUSING: a start command "
            f"targeting hosts outside the unit's node list must never be executed")
    if port is not None and cmd_port is not None and cmd_port != int(port):
        problems.append(
            f"unit {unit_id}: serve_cmd --port {cmd_port} disagrees with unit "
            f"port {port}")
    if recipe and cmd_recipe is not None and _norm(cmd_recipe) != _norm(recipe):
        problems.append(
            f"unit {unit_id}: serve_cmd recipe {cmd_recipe!r} disagrees with unit "
            f"recipe {recipe!r}")
    return problems


def fleet_up_plan(serving=None):
    """Ordered start plan for EVERY serving.json unit (orchestrator FIRST, then
    workers sorted by id) — the full fleet-up set.

    **Fleet-up restores the WHOLE serving layer**, not just the non-keepalive
    non-orchestrator units: orchestrator AND keepalive AND non-keepalive
    workers all come back up. This is the exact reverse of ``fleet_down_cmd``
    (which takes everything down). Kept alive: there is no C4 exemption on the
    way up.

    Start commands reuse the existing start path (no new mechanism): the
    ``serving.VLLM_START_CMD`` form (serving.py:150-153) — ``sparkrun run
    <recipe> --cluster hscc --hosts <nodes> --port <port> --no-follow
    --ensure`` — with the unit's OWN recipe (``orchestrator_recipe`` for the
    orchestrator, ``u.recipe`` for a worker), falling back to the hardcoded
    ``VLLM_RECIPE`` (the same form autodown's per-unit start used before the
    fleet wrapper absorbed it).

    Each returned entry: ``{"kind", "nodes", "port", "unit_id", "cmd",
    "keepalive"}`` (``keepalive`` True for a keepalive worker). Empty/missing
    ``serving`` ⇒ ``[]`` (nothing to start). Used by BOTH ``hscc cluster up``
    and ``autodown.autoup()``.

    SAFETY (t_501fb7f1): before planning, every unit's recorded ``serve_cmd``
    is validated against that unit's OWN ``nodes``/``port``/``recipe``. A
    ``serve_cmd`` that disagrees (e.g. hosts outside the node list) is leaked
    fixture data, and any start derived from it could launch a model on hosts
    that are not this cluster. When any unit carries a disagreeing ``serve_cmd``
    we REFUSE: log loudly and return ``[]`` (start nothing), so no consumer —
    now or a future autoup that trusts serve_cmd — ever executes a bogus
    command.
    """
    if serving is None:
        serving = load_serving()
    if not isinstance(serving, dict):
        return []
    # REFUSE to plan a start when any unit's recorded serve_cmd disagrees
    # with its own recipe/nodes/port (leaked fixture data must never drive a
    # launch on hosts outside the unit's node list — t_501fb7f1).
    violations = []
    for u in (serving.get("units", []) or []):
        if isinstance(u, dict):
            violations.extend(_validate_unit_serve_cmd(u))
    if violations:
        for v in violations:
            _serving_warn(f"serve_cmd validation REFUSED start: {v}")
        return []
    orch = []
    workers = []
    for u in (serving.get("units", []) or []):
        if not isinstance(u, dict):
            continue
        nodes = [n for n in (u.get("nodes") or []) if n]
        if not nodes:
            continue
        role = u.get("role")
        unit_id = u.get("id") or ",".join(nodes)
        port = u.get("port") or serving_port(serving)
        if role == "orchestrator":
            recipe = orchestrator_recipe(serving) or VLLM_RECIPE
            orch.append({"kind": "orchestrator", "nodes": nodes, "port": port,
                         "unit_id": unit_id, "recipe": recipe,
                         "keepalive": False})
        elif role == "worker":
            # BOTH keepalive and non-keepalive workers come up on fleet-up.
            recipe = u.get("recipe") or VLLM_RECIPE
            recipe = os.path.expanduser(recipe) if recipe else None
            workers.append({"kind": "worker", "nodes": nodes, "port": port,
                            "unit_id": unit_id, "recipe": recipe,
                            "keepalive": bool(u.get("keepalive"))})
    workers.sort(key=lambda e: e["unit_id"])
    plan = orch + workers
    cmds = []
    for e in plan:
        cmd = ["sparkrun", "run", e["recipe"], "--cluster", HSCC_CLUSTER,
               "--hosts", ",".join(e["nodes"]), "--port", str(e["port"]),
               "--no-follow", "--ensure"]
        cmds.append({"kind": e["kind"], "nodes": e["nodes"], "port": e["port"],
                     "unit_id": e["unit_id"], "cmd": cmd,
                     "keepalive": e["keepalive"]})
    return cmds


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
        if not isinstance(config, dict):
            raise ValueError("cluster.json is not a JSON object")
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
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as e:
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


# ── Orchestrator follower helpers ─────────────────────────────────────────

ORCH_ENDPOINT_STATE = os.path.expanduser("~/.hscc/orch-endpoint")
PROFILES_DIR = os.path.expanduser("~/.hermes/profiles")


def _endpoint_healthy(endpoint, timeout=5):
    """True iff GET <endpoint>/models returns HTTP 200."""
    try:
        res = subprocess.run(
            ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), f"{endpoint}/models"],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        return res.returncode == 0 and res.stdout.strip() == "200"
    except Exception:
        return False


_BASE_URL_RE = re.compile(
    r'^(?P<indent>\s*)base_url:\s*(?P<q>["\']?)(?P<url>\S+?)(?P=q)\s*$')


def _read_prev_orch_endpoint():
    try:
        with open(ORCH_ENDPOINT_STATE) as f:
            return f.read().strip() or None
    except (FileNotFoundError, OSError):
        return None


def _write_prev_orch_endpoint(endpoint):
    from .health import log  # lazy to avoid circular import
    try:
        tmp = ORCH_ENDPOINT_STATE + ".tmp"
        with open(tmp, "w") as f:
            f.write(endpoint)
        os.replace(tmp, ORCH_ENDPOINT_STATE)
    except OSError as e:
        log(f"base_url follower: could not persist orch endpoint: {e}", "WARN")


def update_orchestrator_followers():
    """Repoint managed profiles that track the OLD orchestrator endpoint to the
    NEW one when serving.json re-maps the orchestrator.

    Only profiles whose base_url == the previously-applied orchestrator endpoint
    are rewritten. Worker profiles point at their own node and never match,
    so the model split is preserved. The new endpoint is health-validated
    before any file is touched. No-op on first run.
    """
    from .health import log  # circular import guard
    
    serving = load_serving()
    new_endpoint = orchestrator_endpoint(serving)
    if not new_endpoint:
        return
    old_endpoint = _read_prev_orch_endpoint()
    if old_endpoint == new_endpoint:
        return
    if old_endpoint is None:
        _write_prev_orch_endpoint(new_endpoint)
        return
    
    if not _endpoint_healthy(new_endpoint):
        log(f"base_url follower: new orchestrator {new_endpoint} not healthy "
            f"(/models != 200); deferring profile rewrites", "WARN")
        return

    if not os.path.isdir(PROFILES_DIR):
        _write_prev_orch_endpoint(new_endpoint)
        return

    changed = 0
    had_failure = False
    for name in sorted(os.listdir(PROFILES_DIR)):
        cfg = os.path.join(PROFILES_DIR, name, "config.yaml")
        if not os.path.isfile(cfg):
            continue
        try:
            with open(cfg) as f:
                lines = f.readlines()
        except OSError as e:
            log(f"base_url follower: cannot read {cfg}: {e}", "WARN")
            had_failure = True
            continue
        dirty = False
        for i, line in enumerate(lines):
            m = _BASE_URL_RE.match(line.rstrip("\n"))
            if not m:
                continue
            repl = compute_base_url_change(m.group("url"), old_endpoint,
                                           new_endpoint)
            if repl:
                lines[i] = f'{m.group("indent")}base_url: {repl}\n'
                dirty = True
        if not dirty:
            continue
        try:
            tmp = cfg + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.replace(tmp, cfg)
            changed += 1
            log(f"base_url follower: {name} -> {new_endpoint}")
        except OSError as e:
            log(f"base_url follower: cannot write {cfg}: {e}", "WARN")
            had_failure = True

    if had_failure:
        log(f"base_url follower: {old_endpoint} -> {new_endpoint}, "
            f"{changed} updated but some profiles FAILED; endpoint state NOT "
            f"advanced — will retry next tick", "ERROR")
        return
    log(f"base_url follower: {old_endpoint} -> {new_endpoint}, "
        f"{changed} profile(s) updated")
    _write_prev_orch_endpoint(new_endpoint)


# Resolve cluster config at import time
resolve_cluster_config()
