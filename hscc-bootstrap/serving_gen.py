"""Generate a serving.json structure from a detected cluster + choices."""


def build_serving(cluster, *, orchestrator, recipe, model, port=8000, keepalive=True):
    """Build the serving.json dict: one orchestrator unit + a worker unit per
    other host. Workers get keepalive=True when ``keepalive`` is set.
    """
    hosts = list(cluster.get("hosts") or [])

    def _ipslug(ip, idx):
        # Full IP with dots->dashes, so two hosts that share a last octet on
        # different subnets (e.g. CX7 dual-subnet) get DISTINCT ids. Falls back
        # to the index if the value isn't a dotted address.
        s = str(ip).replace(".", "-")
        return s if s else str(idx)

    units = [{
        "id": f"orch-{_ipslug(orchestrator, 0)}",
        "role": "orchestrator",
        "model": model,
        "recipe": recipe,
        "nodes": [orchestrator],
    }]
    seen = set()
    for i, host in enumerate(h for h in hosts if h != orchestrator):
        uid = f"worker-{_ipslug(host, i)}"
        # Guarantee uniqueness even if two hosts somehow slug-collide.
        if uid in seen:
            uid = f"{uid}-{i}"
        seen.add(uid)
        unit = {
            "id": uid,
            "role": "worker",
            "model": model,
            "recipe": recipe,
            "nodes": [host],
        }
        if keepalive:
            unit["keepalive"] = True
        units.append(unit)
    return {"version": 1, "port": port, "units": units}
