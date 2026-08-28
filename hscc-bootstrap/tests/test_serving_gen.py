import serving_gen


def test_orchestrator_plus_keepalive_workers():
    cluster = {"hosts": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="10.0.0.1",
        recipe="~/r/qwen.yaml", model="Qwen/X", port=8000, keepalive=True)
    assert s["version"] == 1
    assert s["port"] == 8000
    units = s["units"]
    orch = [u for u in units if u["role"] == "orchestrator"]
    workers = [u for u in units if u["role"] == "worker"]
    assert len(orch) == 1 and orch[0]["nodes"] == ["10.0.0.1"]
    assert {tuple(w["nodes"]) for w in workers} == {("10.0.0.2",), ("10.0.0.3",)}
    assert all(w.get("keepalive") is True for w in workers)
    assert all(u["model"] == "Qwen/X" and u["recipe"] == "~/r/qwen.yaml" for u in units)


def test_single_node_no_workers():
    cluster = {"hosts": ["10.0.0.1"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="10.0.0.1",
        recipe="~/r.yaml", model="M", port=8000, keepalive=True)
    assert len([u for u in s["units"] if u["role"] == "worker"]) == 0
    assert len([u for u in s["units"] if u["role"] == "orchestrator"]) == 1


def test_keepalive_false_omits_flag():
    cluster = {"hosts": ["1.1.1.1", "2.2.2.2"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="1.1.1.1",
        recipe="r", model="m", port=8000, keepalive=False)
    workers = [u for u in s["units"] if u["role"] == "worker"]
    assert all("keepalive" not in w for w in workers)


def test_same_last_octet_different_subnet_no_id_collision():
    """Two hosts sharing a last octet on different subnets (CX7 dual-subnet)
    must get DISTINCT unit ids (regression: ids were derived from last octet)."""
    # Three DISTINCT subnets that all share last octet .10 — the point of the
    # regression. (An IP scrub once collapsed two of these into the same address,
    # silently destroying the premise and failing the test.)
    cluster = {"hosts": ["10.0.0.10", "10.1.0.10", "172.16.0.10"]}
    s = serving_gen.build_serving(
        cluster, orchestrator="10.0.0.10",
        recipe="r", model="m", port=8000, keepalive=True)
    ids = [u["id"] for u in s["units"]]
    assert len(ids) == len(set(ids)), f"duplicate unit ids: {ids}"
    # workers are the two non-orchestrator .10 hosts
    workers = [u for u in s["units"] if u["role"] == "worker"]
    assert len({u["id"] for u in workers}) == 2
