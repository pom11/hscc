"""t_417610e6 (verify leg) — the endpoint guard must not treat a fleet NODE as
a valid hermes route when the applied fleet is a uniform_pool.

`check_profile_endpoints` builds its allowed set from serving.json. Before this
card it always added the orchestrator endpoint, so on a pool — where serving.json
legitimately lists the orchestrator's node — a node-direct `auxiliary.*.base_url`
compared equal to a "served" origin and passed silently. That is the hole that
let the orchestrator profile carry 9 unreachable auxiliary endpoints through
every verify run: a valid SERVING address is not a valid ROUTE when the gateway
host has no path to a node port.
"""

import json

import yaml

from hscc_daemon.verify import check_profile_endpoints


ORCH_NODE = "192.0.2.44"
WORKER_NODE = "192.0.2.47"
ORCH_PORT = 8000
PROXY = "http://localhost:4000"


def _serving(tmp_path, *, uniform):
    p = tmp_path / "serving.json"
    p.write_text(json.dumps({
        "version": 2,
        "units": [
            {"role": "orchestrator", "nodes": [ORCH_NODE], "port": ORCH_PORT},
            {"role": "worker", "nodes": [WORKER_NODE], "port": 8001},
        ],
        **({"uniform_pool": True} if uniform else {}),
    }))
    return str(p)


def _profiles(tmp_path, cfg):
    d = tmp_path / "profiles"
    (d / "hscc-orch").mkdir(parents=True)
    (d / "hscc-orch" / "config.yaml").write_text(yaml.safe_dump(cfg))
    return str(d)


_NODE_AUX = {"auxiliary": {"kanban_decomposer": {
    "provider": "custom", "base_url": f"http://{ORCH_NODE}:{ORCH_PORT}/v1",
    "model": "orchestrator-model"}}}


def test_node_direct_aux_named_on_pool(tmp_path):
    """The regression: on a pool this exact value must be named, key path and
    host included, and the finding must say the fleet is a pool."""
    result = check_profile_endpoints(
        serving_path=_serving(tmp_path, uniform=True),
        profiles_dir=_profiles(tmp_path, _NODE_AUX))
    assert result["ok"] is False
    assert "auxiliary.kanban_decomposer.base_url" in result["detail"]
    assert ORCH_NODE in result["detail"]
    assert "uniform_pool" in result["detail"]


def test_proxy_aux_passes_on_pool(tmp_path):
    result = check_profile_endpoints(
        serving_path=_serving(tmp_path, uniform=True),
        profiles_dir=_profiles(tmp_path, {
            "auxiliary": {"kanban_decomposer": {
                "base_url": f"{PROXY}/v1", "model": "worker-model"}}}))
    assert result["ok"] is True, result["detail"]


def test_all_9_aux_entries_are_named(tmp_path):
    """The operator's actual shape: 9 node-direct auxiliary entries (compression
    + the 8 text tasks). Every one is reported, not just the first."""
    tasks = ["compression", "kanban_decomposer", "triage_specifier",
             "profile_describer", "curator", "title_generation", "skills_hub",
             "approval", "mcp"]
    cfg = {"model": {"base_url": f"{PROXY}/v1"},
           "auxiliary": {t: {"provider": "custom",
                             "base_url": f"http://{ORCH_NODE}:{ORCH_PORT}/v1",
                             "model": "orchestrator-model"} for t in tasks}}
    result = check_profile_endpoints(
        serving_path=_serving(tmp_path, uniform=True),
        profiles_dir=_profiles(tmp_path, cfg))
    assert result["ok"] is False
    assert result["detail"].startswith("9 offending url(s)")
    for t in tasks:
        assert f"auxiliary.{t}.base_url" in result["detail"]


def test_pinned_fleet_allows_direct_orchestrator(tmp_path):
    """Non-uniform role split: the node endpoint stays legitimate — including
    the orchestrator's `model.base_url`, which is the historical form."""
    cfg = {"model": {"base_url": f"http://{ORCH_NODE}:{ORCH_PORT}/v1"},
           "auxiliary": {"compression": {
               "base_url": f"http://{ORCH_NODE}:{ORCH_PORT}/v1"}}}
    result = check_profile_endpoints(
        serving_path=_serving(tmp_path, uniform=False),
        profiles_dir=_profiles(tmp_path, cfg))
    assert result["ok"] is True, result["detail"]


def test_pinned_fleet_still_flags_unknown_host(tmp_path):
    """Tightening the pool case must not loosen the pinned case: a host no unit
    serves is still a finding."""
    result = check_profile_endpoints(
        serving_path=_serving(tmp_path, uniform=False),
        profiles_dir=_profiles(tmp_path, {
            "model": {"base_url": "http://198.51.100.9:8000/v1"}}))
    assert result["ok"] is False
    assert "198.51.100.9" in result["detail"]


def test_pool_without_orchestrator_unit_is_not_unverified(tmp_path):
    """A pool declares the proxy as the only route, so a serving.json whose
    units are all workers still gives a decidable answer (previously: an
    unconditional "unverified"). Workers may serve the orchestrator alias."""
    p = tmp_path / "serving.json"
    p.write_text(json.dumps({"units": [
        {"role": "worker", "nodes": [WORKER_NODE], "port": 8001}],
        "uniform_pool": True}))
    result = check_profile_endpoints(
        serving_path=str(p),
        profiles_dir=_profiles(tmp_path, {
            "auxiliary": {"compression": {"base_url": f"{PROXY}/v1"}}}))
    assert result["ok"] is True, result["detail"]


def test_pool_finding_names_worker_node_too(tmp_path):
    """Not just the orchestrator node: any unit's node is unreachable from the
    gateway on a pool."""
    result = check_profile_endpoints(
        serving_path=_serving(tmp_path, uniform=True),
        profiles_dir=_profiles(tmp_path, {
            "model": {"base_url": f"http://{WORKER_NODE}:8001/v1"}}))
    assert result["ok"] is False
    assert WORKER_NODE in result["detail"]
