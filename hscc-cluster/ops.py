"""hscc-cluster read+mutating ops handlers."""
try:
    from . import clusterlib as cl  # package context (runtime)
except ImportError:
    import clusterlib as cl  # direct import context (tests)


def _running_by_node():
    """Live: parse `sparkrun status` -> {node_ip: recipe}. Empty on failure.

    `sparkrun status` groups output into `Job:` blocks (with container lines that
    name running hosts) and an `Idle hosts (...)` section. Hosts in the idle
    section are NOT running, even though their IP appears on the line."""
    r = cl.run_cmd([cl.SPARKRUN, "status"], timeout=20)
    mapping = {}
    if not r["ok"]:
        return mapping
    in_idle = False
    recipe = "unknown"
    for line in r["stdout"].splitlines():
        stripped = line.strip()
        if stripped.startswith("Idle hosts"):
            in_idle = True
            continue
        if stripped.startswith("Job:"):
            in_idle = False
            parts = stripped.split()
            recipe = parts[1] if len(parts) > 1 else "unknown"
            continue
        if in_idle:
            continue
        import re as _re
        for ip in cl.NODES + [cl.HEAD]:
            if _re.search(r'(?<![0-9.])' + _re.escape(str(ip)) + r'(?![0-9.])', line):
                mapping[ip] = recipe
    return mapping


def cluster_status(args, **kwargs):
    running = _running_by_node()
    units = cl.read_serving_units()
    idle = [n for n in cl.NODES if n not in running]
    result = {"head": cl.HEAD, "running": running, "idle_nodes": idle,
              "serving_units": [u.get("id") or u.get("name") for u in units]}
    # Surface discovery source + free-VRAM when available (best-effort; never
    # fail cluster_status if discovery/probe is unavailable).
    try:
        from . import discovery as _disc  # package context
    except ImportError:
        try:
            import discovery as _disc  # direct context
        except ImportError:
            _disc = None
    if _disc is not None:
        try:
            topo = _disc.discover()
            result["source"] = topo.source
            result["free_vram_gb"] = {
                n.ip: n.vram_free_gb for n in topo.workers if n.vram_free_gb is not None
            }
        except Exception:
            pass
    return result


def list_recipes(args, **kwargs):
    r = cl.run_cmd([cl.SPARKRUN, "recipes"], timeout=20)
    return {"ok": r["ok"], "recipes_raw": r["stdout"], "error": r["stderr"] or None}


def pick_node(args, **kwargs):
    running = _running_by_node()
    idle = [n for n in cl.NODES if n not in running]
    if not idle:
        return {"node": None, "reason": "no idle worker nodes available"}
    return {"node": idle[0], "reason": f"idle nodes: {idle}"}


def _cluster_name():
    """Saved sparkrun cluster name (for --cluster, which gives the NAS cache).
    Reads ~/.hscc/cluster.json 'name', else 'hscc'."""
    import json as _j
    try:
        with open(cl.CLUSTER_JSON) as fh:
            return (_j.load(fh).get("name") or "hscc")
    except (FileNotFoundError, ValueError, OSError):
        return "hscc"


def provision_model(args, **kwargs):
    import os as _os
    recipe = args["recipe"]
    node = args.get("node", "auto")
    port = int(args.get("port", 8000))
    if node == "auto":
        picked = pick_node({})
        if not picked["node"]:
            return {"ok": False, "error": picked["reason"]}
        node = picked["node"]
    # Correct invocation (H2): --cluster gives the NAS weight cache; --port sets
    # the vLLM port (supports >1 model/node); --ensure no-ops if already up;
    # expanduser so a ~/... recipe path resolves.
    recipe_path = _os.path.expanduser(recipe)
    cluster = _cluster_name()
    argv = [cl.SPARKRUN, "run", recipe_path, "--cluster", cluster,
            "--hosts", node, "--port", str(port), "--no-follow", "--ensure"]
    action = (f"provision_model recipe={recipe} on {node}:{port} "
              f"(sparkrun run --cluster {cluster} --hosts {node} "
              f"--port {port} --ensure)")
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    r = cl.run_cmd(argv, timeout=900)
    return {"ok": r["ok"], "executed": True, "node": node, "port": port,
            "base_url": f"http://{node}:{port}/v1",
            "stdout_tail": r["stdout"][-2000:], "error": r["stderr"] or None}


def stop_model(args, **kwargs):
    recipe = args["recipe"]
    node = args["node"]
    if node == cl.HEAD and not args.get("force", False):
        return {"refused": True, "executed": False,
                "reason": f"{node} is the orchestrator/head node; refusing to stop it. Pass force=true to override."}
    action = f"stop_model recipe={recipe} on {node} (sparkrun stop {recipe} --hosts {node})"
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    r = cl.run_cmd([cl.SPARKRUN, "stop", recipe, "--hosts", node], timeout=120)
    return {"ok": r["ok"], "executed": True, "node": node,
            "stdout_tail": r["stdout"][-2000:], "error": r["stderr"] or None}


def model_health(args, **kwargs):
    node = args.get("node", cl.HEAD)
    r = cl.run_cmd(["curl", "-s", "--max-time", "8",
                    f"http://{node}:8000/v1/models"], timeout=12)
    served = None
    if r["ok"] and r["stdout"].strip():
        import json as _j
        try:
            served = _j.loads(r["stdout"])["data"][0]["id"]
        except Exception:
            served = None
    return {"node": node, "reachable": r["ok"] and served is not None,
            "served_model": served}
