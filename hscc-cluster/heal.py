"""hscc-cluster mutating self-heal handlers. Shared with the daemon (autonomous path)."""
try:
    from . import clusterlib as cl  # package context (runtime)
except ImportError:
    import clusterlib as cl  # direct import context (tests)


def _running_containers():
    """Live: [{node, recipe}] for sparkrun containers across the cluster."""
    out = []
    for node in cl.NODES + [cl.HEAD]:
        r = cl.ssh_cmd(node, "docker ps --format '{{.Names}}' | grep -i sparkrun", timeout=12)
        for name in [l for l in r["stdout"].splitlines() if l.strip()]:
            out.append({"node": node, "recipe": name.strip()})
    return out


def _serving_nodes():
    nodes = set()
    for u in cl.read_serving_units():
        for n in u.get("nodes", []):
            nodes.add(n)
    return nodes


def restart_model(args, **kwargs):
    """Shared restart primitive for both the agent toolset and the daemon.

    Agent/worker path: restart_model({recipe, node}) -> hard `stop` then `run`,
    HEAD-refused unless force. (No optional flags passed -> behavior unchanged.)

    Daemon/orchestrator path: the head vLLM needs its sparkrun launch flags
    (--cluster for the NAS cache_dir, --port, --no-follow, --ensure). Passing
    ensure=True selects launch-only semantics (NO hard stop) so a transient
    health blip can't thrash a model that is still loading.
    """
    recipe, node = args["recipe"], args["node"]
    if node == cl.HEAD and not args.get("force", False):
        return {"refused": True, "executed": False,
                "reason": f"{node} is the orchestrator/head node; refusing to restart it. Pass force=true to override."}
    cluster = args.get("cluster")
    port = args.get("port")
    ensure = bool(args.get("ensure", False))
    no_follow = bool(args.get("no_follow", False))
    mode = "ensure-up" if ensure else "stop then run"
    action = f"restart_model {recipe} on {node} ({mode} via sparkrun)"
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    run = [cl.SPARKRUN, "run", recipe, "--hosts", node]
    if cluster:
        run += ["--cluster", cluster]
    if port:
        run += ["--port", str(port)]
    if no_follow:
        run.append("--no-follow")
    if ensure:
        run.append("--ensure")  # launch-only: skip the stop, no thrash
    else:
        cl.run_cmd([cl.SPARKRUN, "stop", recipe, "--hosts", node], timeout=120)
    r = cl.run_cmd(run, timeout=900)
    base_port = int(port) if port else 8000
    return {"ok": r["ok"], "executed": True, "node": node,
            "base_url": f"http://{node}:{base_port}/v1", "stdout_tail": r["stdout"][-2000:]}


def remount_nas(args, **kwargs):
    node = args["node"]
    action = f"remount_nas on {node} (umount -l /mnt/nas then mount)"
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    r = cl.ssh_cmd(node, "sudo umount -l /mnt/nas; sudo mount /mnt/nas && ls /mnt/nas | head",
                   timeout=40)
    return {"ok": r["ok"], "executed": True, "node": node, "out": r["stdout"][-1000:]}


def repair_nas_export(args, **kwargs):
    action = ("repair_nas_export on QNAP .249: backup /etc/exports, restore export line, "
              "exportfs -ra (HIGH-RISK)")
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    bk = cl.ssh_cmd(cl.NAS_HOST,
                    "sudo cp /etc/exports /etc/exports.bak.$(date +%s) 2>/dev/null; "
                    "sudo exportfs -ra 2>&1; sudo exportfs -v 2>&1", timeout=30)
    return {"ok": bk["ok"], "executed": True, "out": bk["stdout"][-2000:]}


def reap_orphans(args, **kwargs):
    running = _running_containers()
    serving = _serving_nodes()
    # never target the head/orchestrator node, regardless of serving.json state
    orphans = [c for c in running if c["node"] not in serving and c["node"] != cl.HEAD]
    action = f"reap_orphans: stop {[ (o['node'],o['recipe']) for o in orphans]}"
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    reaped = []
    failed = []
    for o in orphans:
        r = cl.run_cmd([cl.SPARKRUN, "stop", o["recipe"], "--hosts", o["node"]], timeout=120)
        if r["ok"]:
            reaped.append({"node": o["node"], "recipe": o["recipe"]})
        else:
            failed.append({"node": o["node"], "recipe": o["recipe"],
                           "error": r.get("stderr", "unknown")[:200]})
    return {"ok": len(failed) == 0, "executed": True, "reaped": reaped,
            "failed": failed if failed else None}
