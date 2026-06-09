"""Generate a serving.json structure from a detected cluster + choices."""


def build_serving(cluster, *, orchestrator, recipe, model, port=8000, keepalive=True):
    """Build the serving.json dict: one orchestrator unit + a worker unit per
    other host. Workers get keepalive=True when ``keepalive`` is set.
    """
    hosts = list(cluster.get("hosts") or [])

    def _octet(ip, idx):
        tail = ip.rsplit(".", 1)[-1]
        return tail if tail.isdigit() else str(idx)

    units = [{
        "id": f"orch-{_octet(orchestrator, 0)}",
        "role": "orchestrator",
        "model": model,
        "recipe": recipe,
        "nodes": [orchestrator],
    }]
    for i, host in enumerate(h for h in hosts if h != orchestrator):
        unit = {
            "id": f"worker-{_octet(host, i)}",
            "role": "worker",
            "model": model,
            "recipe": recipe,
            "nodes": [host],
        }
        if keepalive:
            unit["keepalive"] = True
        units.append(unit)
    return {"version": 1, "port": port, "units": units}
