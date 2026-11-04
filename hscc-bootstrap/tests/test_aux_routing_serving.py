"""t_417610e6 — auxiliary/* compaction endpoints must follow the serving plan.

The generator bug these tests pin: `COMPACT_URL` used to default to
`http://{ORCH_MODEL_HOST}:8000/v1` — a hardcoded LAN node — and BOTH auxiliary
writers (`_ensure_compaction`, `_ensure_text_auxiliaries`) stamp that constant
into `auxiliary.*.base_url`. On a uniform_pool fleet the gateway cannot route to
a node port at all, so the orchestrator profile's 9 auxiliary endpoints
(compression + the 8 text tasks) all pointed at an unreachable address while
model/delegation/goal_judge — routed from WORKER_PROXY_URL — kept working.
And because both writers are fill-empty, the wrong value survived every later
bootstrap and every `doctor --fix`.

Every test here uses synthetic addresses from RFC 5737 documentation space
(192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and a tmp serving.json; nothing
reads or writes the operator's live state.
"""

import json
import os

import pytest
import yaml

import enable_plugins


# Synthetic topology (RFC 5737 documentation space — never a live address).
NODE_A = "192.0.2.44"
NODE_B = "192.0.2.47"
PROXY = enable_plugins.WORKER_PROXY_URL  # http://localhost:4000/v1


def _serving(tmp_path, *, uniform=True, orch_node=NODE_A, port=8000):
    p = tmp_path / "serving.json"
    data = {
        "version": 2,
        "units": [
            {"id": "orch", "role": "orchestrator", "model": "m",
             "nodes": [orch_node], "port": port},
            {"id": "w1", "role": "worker", "model": "m",
             "nodes": [NODE_B], "port": 8001},
        ],
    }
    if uniform:
        data["uniform_pool"] = True
    p.write_text(json.dumps(data))
    return str(p)


def _wire_pool(tmp_path, monkeypatch, *, uniform=True):
    """Point the module at a tmp pool serving.json and re-derive the constants
    exactly as import-time would."""
    serving = _serving(tmp_path, uniform=uniform)
    monkeypatch.setattr(enable_plugins, "SERVING_JSON", serving)
    url, is_orch, hosts = enable_plugins._serving_compact_route(serving)
    monkeypatch.setattr(enable_plugins, "_SERVING_URL", url)
    monkeypatch.setattr(enable_plugins, "_SERVING_IS_ORCH", is_orch)
    monkeypatch.setattr(enable_plugins, "FLEET_NODE_HOSTS", hosts)
    monkeypatch.setattr(enable_plugins, "_DEFAULT_COMPACT_URL", url)
    monkeypatch.setattr(enable_plugins, "_DEFAULT_IS_ORCH", is_orch)
    monkeypatch.setattr(enable_plugins, "_POOL_REPAIR", bool(url) and not is_orch)
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", url)
    monkeypatch.setattr(enable_plugins, "COMPACT_MODEL",
                        enable_plugins.ORCH_MODEL if is_orch
                        else enable_plugins.WORKER_MODEL)
    # The compaction probe is a real urlopen; keep these tests off the network.
    monkeypatch.setattr(enable_plugins, "_probe_compaction_endpoint",
                        lambda *a, **k: None)
    return serving


def _write(p, data):
    p.write_text(yaml.safe_dump(data))
    return str(p)


def _aux(cfg):
    return cfg.get("auxiliary", {})


# ── derivation ───────────────────────────────────────────────────────────────

def test_pool_plan_routes_aux_to_proxy_not_node(tmp_path):
    """uniform_pool -> the proxy URL, and is_orchestrator False."""
    serving = _serving(tmp_path, uniform=True)
    url, is_orch, hosts = enable_plugins._serving_compact_route(serving)
    assert url == PROXY
    assert is_orch is False
    assert NODE_A in hosts and NODE_B in hosts


def test_pinned_plan_keeps_direct_orchestrator(tmp_path):
    """A pinned fleet keeps the historical direct node endpoint (role split real)."""
    serving = _serving(tmp_path, uniform=False)
    url, is_orch, hosts = enable_plugins._serving_compact_route(serving)
    assert url == f"http://{NODE_A}:8000/v1"
    assert is_orch is True


def test_missing_serving_falls_back_to_none(tmp_path):
    url, is_orch, hosts = enable_plugins._serving_compact_route(
        str(tmp_path / "nope.json"))
    assert url is None and is_orch is None and hosts == set()


def test_pinned_unit_port_respected(tmp_path):
    serving = _serving(tmp_path, uniform=False, port=8123)
    url, _is_orch, _hosts = enable_plugins._serving_compact_route(serving)
    assert url == f"http://{NODE_A}:8123/v1"


def test_pool_model_is_worker_alias(tmp_path, monkeypatch):
    """On a pool the aux default target is the proxy, so COMPACT_MODEL must be
    the worker alias — the orchestrator alias is not resolvable at a proxy that
    only fronts worker units (a mismatch 404s every compaction call)."""
    _wire_pool(tmp_path, monkeypatch, uniform=True)
    assert enable_plugins.COMPACT_MODEL == enable_plugins.WORKER_MODEL


# ── writer behaviour ─────────────────────────────────────────────────────────

def test_fresh_config_pool_aux_all_on_proxy(tmp_path, monkeypatch):
    """A freshly wired config on a pool fleet carries NO node address in any
    auxiliary entry — the core regression this card exists for."""
    _wire_pool(tmp_path, monkeypatch, uniform=True)
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"]})
    enable_plugins.enable(path)
    aux = _aux(yaml.safe_load(open(path)))
    tasks = ["compression"] + list(enable_plugins._LOCAL_TEXT_AUX_TASKS)
    assert len(tasks) == 9                      # the 9 the operator lost
    for task in tasks:
        url = aux[task]["base_url"]
        assert url == PROXY, f"auxiliary.{task} escaped the proxy: {url}"
        assert NODE_A not in url and NODE_B not in url
    assert "192.0.2." not in yaml.safe_dump(aux)


def test_poisoned_config_is_repaired(tmp_path, monkeypatch):
    """The exact poison class is repaired: provider=custom + model alias +
    node-direct base_url (the fingerprint in the operator's backup) is
    re-aimed at the proxy instead of being preserved as a 'choice'."""
    serving = _wire_pool(tmp_path, monkeypatch, uniform=True)
    node_url = f"http://{NODE_A}:8000/v1"
    aux = {t: {"provider": "custom", "base_url": node_url,
               "model": enable_plugins.ORCH_MODEL, "api_key": "sk-sparkrun"}
           for t in enable_plugins._LOCAL_TEXT_AUX_TASKS}
    aux["compression"] = {"base_url": node_url,
                          "model": enable_plugins.ORCH_MODEL,
                          "provider": "custom", "api_key": "sk-sparkrun",
                          "timeout": 90}
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]},
                   "toolsets": ["kanban"], "auxiliary": aux})
    res = enable_plugins.enable(path)
    out = _aux(yaml.safe_load(open(path)))
    for task in ["compression"] + list(enable_plugins._LOCAL_TEXT_AUX_TASKS):
        assert out[task]["base_url"] == PROXY, task
    # The repair is reported, never silent.
    assert any("aux" in k for k in res["compaction"] + res["text_aux"])
    assert serving  # sanity: the fixture is what drove the decision


def test_repair_is_idempotent(tmp_path, monkeypatch):
    _wire_pool(tmp_path, monkeypatch, uniform=True)
    node_url = f"http://{NODE_A}:8000/v1"
    aux = {t: {"provider": "custom", "base_url": node_url,
               "model": enable_plugins.ORCH_MODEL}
           for t in enable_plugins._LOCAL_TEXT_AUX_TASKS}
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]},
                   "toolsets": ["kanban"], "auxiliary": aux})
    enable_plugins.enable(path)
    first = yaml.safe_load(open(path))
    res = enable_plugins.enable(path)
    assert res["text_aux"] == []                # nothing left to fix
    assert res["compaction"] == []
    assert yaml.safe_load(open(path)) == first


def test_operator_cloud_provider_never_rewritten(tmp_path, monkeypatch):
    """The fill-empty philosophy survives: a deliberate NON-node provider is an
    operator choice and must survive byte-for-byte."""
    _wire_pool(tmp_path, monkeypatch, uniform=True)
    cloud = "https://api.example.invalid/v1"
    path = _write(tmp_path / "config.yaml", {
        "plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"],
        "auxiliary": {
            "kanban_decomposer": {"provider": "openrouter", "base_url": cloud,
                                  "model": "someone/else"},
            "curator": {"provider": "custom", "base_url": "http://localhost:9999/v1",
                        "model": "local-tool"},
        },
    })
    enable_plugins.enable(path)
    out = _aux(yaml.safe_load(open(path)))
    assert out["kanban_decomposer"] == {"provider": "openrouter",
                                        "base_url": cloud,
                                        "model": "someone/else"}
    assert out["curator"]["base_url"] == "http://localhost:9999/v1"
    assert out["curator"]["model"] == "local-tool"


def test_pinned_fleet_node_aux_preserved(tmp_path, monkeypatch):
    """On a PINNED fleet the node endpoint is legitimate and routable — no
    repair, no finding. The repair is pool-scoped by construction."""
    _wire_pool(tmp_path, monkeypatch, uniform=False)
    node_url = f"http://{NODE_A}:8000/v1"
    path = _write(tmp_path / "config.yaml", {
        "plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"],
        "auxiliary": {"kanban_decomposer": {
            "provider": "custom", "base_url": node_url,
            "model": enable_plugins.ORCH_MODEL}},
    })
    res = enable_plugins.enable(path)
    out = _aux(yaml.safe_load(open(path)))
    assert out["kanban_decomposer"]["base_url"] == node_url
    assert "aux.kanban_decomposer.base_url" not in res["text_aux"]


def test_no_serving_means_no_repair(tmp_path, monkeypatch):
    """Without serving.json the pool invariant is unproven, so an existing
    node-shaped value is preserved — never repaired on a guess."""
    monkeypatch.setattr(enable_plugins, "SERVING_JSON",
                        str(tmp_path / "absent-serving.json"))
    monkeypatch.setattr(enable_plugins, "_POOL_REPAIR", False)
    monkeypatch.setattr(enable_plugins, "FLEET_NODE_HOSTS", set())
    monkeypatch.setattr(enable_plugins, "_probe_compaction_endpoint",
                        lambda *a, **k: None)
    node_url = f"http://{NODE_A}:8000/v1"
    path = _write(tmp_path / "config.yaml", {
        "plugins": {"enabled": ["hscc-cluster"]}, "toolsets": ["kanban"],
        "auxiliary": {"kanban_decomposer": {
            "provider": "custom", "base_url": node_url, "model": "orchestrator-model"}},
    })
    enable_plugins.enable(path)
    out = _aux(yaml.safe_load(open(path)))
    assert out["kanban_decomposer"]["base_url"] == node_url


def test_node_direct_helper_parity():
    """_node_direct_aux_url host parsing covers the shapes configs actually
    carry (path, port, missing scheme) and nothing else."""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(enable_plugins, "_POOL_REPAIR", True)
        monkeypatch.setattr(enable_plugins, "FLEET_NODE_HOSTS", {NODE_A, NODE_B})
        assert enable_plugins._node_direct_aux_url(f"http://{NODE_A}:8000/v1")
        assert enable_plugins._node_direct_aux_url(f"http://{NODE_B}:8001")
        assert enable_plugins._node_direct_aux_url(NODE_A)
        assert not enable_plugins._node_direct_aux_url(
            "https://api.example.invalid/v1")
        assert not enable_plugins._node_direct_aux_url("http://localhost:4000/v1")
        assert not enable_plugins._node_direct_aux_url("")
        assert not enable_plugins._node_direct_aux_url(None)
    finally:
        monkeypatch.undo()


# ── the pin: a wired config must pass the endpoint guard ─────────────────────

def _load_verify():
    """Import hscc_daemon.verify from the repo root (cross-leg import, same
    pattern hscc-cluster/tests/test_cluster_up_launch.py uses)."""
    import importlib
    import sys
    root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("hscc_daemon.verify")


def test_wired_pool_config_passes_endpoint_pin(tmp_path, monkeypatch):
    """The contract in one line: the config bootstrap WRITES on a pool fleet is
    the config the endpoint guard ACCEPTS. Uses the real guard, not a mock."""
    serving = _wire_pool(tmp_path, monkeypatch, uniform=True)
    # bootstrap writes the ROUTE (which host:port); doctor --fix writes the
    # model id. Both live in the same file, so both are seeded here.
    node_url = f"http://{NODE_A}:8000/v1"
    poison = {t: {"provider": "custom", "base_url": node_url,
                  "model": enable_plugins.ORCH_MODEL}
              for t in enable_plugins._LOCAL_TEXT_AUX_TASKS}
    path = _write(tmp_path / "config.yaml",
                  {"plugins": {"enabled": ["hscc-cluster"]},
                   "toolsets": ["kanban"], "auxiliary": poison})
    enable_plugins.enable(path)                       # the ROUTE half
    cfg = yaml.safe_load(open(path))
    for t, ent in cfg["auxiliary"].items():            # the MODEL half (doctor)
        if ent.get("model") == enable_plugins.ORCH_MODEL:
            ent["model"] = enable_plugins.WORKER_MODEL
    cfg["model"] = {"base_url": PROXY, "default": enable_plugins.WORKER_MODEL,
                    "provider": "custom", "api_key": "sk-sparkrun"}
    from pathlib import Path
    Path(path).write_text(yaml.safe_dump(cfg))

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "hscc-orch").mkdir()
    (profiles_dir / "hscc-orch" / "config.yaml").write_text(open(path).read())

    verify = _load_verify()
    result = verify.check_profile_endpoints(
        serving_path=serving, profiles_dir=str(profiles_dir))
    assert result["ok"] is True, result["detail"]


def test_poisoned_config_would_have_failed_the_pin(tmp_path):
    """The same guard, same fixture, WITHOUT the fix applied: the node-direct
    auxiliary is named. Proves the pin actually has teeth on this class."""
    serving = _serving(tmp_path, uniform=True)
    profiles_dir = tmp_path / "profiles"
    (profiles_dir / "hscc-orch").mkdir(parents=True)
    (profiles_dir / "hscc-orch" / "config.yaml").write_text(yaml.safe_dump({
        "model": {"base_url": f"http://{NODE_A}:8000/v1"},
        "auxiliary": {t: {"provider": "custom",
                          "base_url": f"http://{NODE_A}:8000/v1",
                          "model": "orchestrator-model"}
                      for t in enable_plugins._LOCAL_TEXT_AUX_TASKS},
    }))
    verify = _load_verify()
    result = verify.check_profile_endpoints(
        serving_path=serving, profiles_dir=str(profiles_dir))
    assert result["ok"] is False
    assert "auxiliary.kanban_decomposer.base_url" in result["detail"]
    assert NODE_A in result["detail"]
