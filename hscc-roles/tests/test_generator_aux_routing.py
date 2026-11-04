"""t_417610e6 (generator leg) — a freshly generated profile config must carry
only proxy-relative endpoints, never a node address from the serving plan.

Two facts this pins:

1. The `orchestrator` role's generated config has NO `auxiliary` block at all
   (see generator.generate_profile: an orch gets `model` + `compression` only).
   So `hscc-roles generate` could never have been the writer that left the
   orchestrator profile's 9 node-direct auxiliary endpoints on disk — that was
   hscc-bootstrap/enable_plugins.py. If this test ever starts seeing an
   `auxiliary` key on a generated orch profile, the generator has grown the
   same bug and the pin below is what catches it.

2. A worker role's generated `auxiliary.compression.base_url` follows the
   serving plan: the proxy on a uniform_pool, the orchestrator node on a pinned
   fleet. Under a pool a node address is unreachable from the gateway.

Synthetic RFC 5737 addresses only; profiles are generated into tmp_path.
"""

import json
import os
import sys

import yaml

import generator


NODE_A = "192.0.2.44"
NODE_B = "192.0.2.47"
PROXY = generator.WORKER_PROXY_BASE_URL  # http://localhost:4000/v1

_ORCH_SPEC = {"name": "orchestrator", "identity": "You orchestrate.\n",
              "preload_skills": []}
_WORKER_SPEC = {"name": "coder", "identity": "You build.\n",
                "preload_skills": []}


def _prepare(tmp_path, monkeypatch, *, uniform):
    """Generate into tmp_path and aim the endpoint constants at the plan shape.

    The module constants (STRONG_URL / COMPACT_BASE_URL) bind at import from
    serving.json, so a test that wants a specific plan pins them explicitly —
    the pattern the existing strong-tier test uses. The derivation from
    serving.json itself is covered by test_generator.py's
    test_live_orchestrator_endpoint_* cases.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    serving = tmp_path / "serving.json"
    serving.write_text(json.dumps({
        "version": 2,
        "units": [
            {"id": "orch", "role": "orchestrator", "model": "m",
             "nodes": [NODE_A], "port": 8000},
            {"id": "w1", "role": "worker", "model": "m",
             "nodes": [NODE_B], "port": 8001},
        ],
        **({"uniform_pool": True} if uniform else {}),
    }))
    monkeypatch.setattr(generator, "STRONG_URL",
                        PROXY if uniform else f"http://{NODE_A}:8000/v1")
    monkeypatch.setattr(generator, "COMPACT_BASE_URL",
                        PROXY if uniform else f"http://{NODE_A}:8000/v1")
    monkeypatch.setattr(generator, "COMPACT_MODEL",
                        generator.WORKER_MODEL if uniform else generator.STRONG_MODEL)
    return str(serving)


def _read(tmp_path, name):
    with open(os.path.join(str(tmp_path / "profiles" / name), "config.yaml")) as f:
        return yaml.safe_load(f)


def _load_verify():
    import importlib
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("hscc_daemon.verify")


def test_generated_orch_has_no_auxiliary_block(tmp_path, monkeypatch):
    """The generator never writes an auxiliary block for an orchestrator role —
    so it cannot be the writer that produced the node-direct auxiliaries."""
    _prepare(tmp_path, monkeypatch, uniform=True)
    generator.generate_profile(dict(_ORCH_SPEC), base_identity="BASE")
    cfg = _read(tmp_path, "orchestrator")
    assert "auxiliary" not in cfg, (
        "the orch profile grew an auxiliary block — re-check it against the "
        "serving plan before trusting this test")
    assert cfg["model"]["base_url"] == PROXY


def test_generated_orch_passes_endpoint_pin_pool(tmp_path, monkeypatch):
    """The card's contract: a freshly generated orch config passes the endpoint
    guard on a uniform_pool fleet."""
    serving = _prepare(tmp_path, monkeypatch, uniform=True)
    generator.generate_profile(dict(_ORCH_SPEC), base_identity="BASE")
    verify = _load_verify()
    result = verify.check_profile_endpoints(
        serving_path=serving,
        profiles_dir=str(tmp_path / "profiles"))
    assert result["ok"] is True, result["detail"]


def test_generated_worker_compaction_follows_pool(tmp_path, monkeypatch):
    """Worker compaction aux lands on the proxy on a pool — never on a node
    address from the plan's node list."""
    _prepare(tmp_path, monkeypatch, uniform=True)
    generator.generate_profile(dict(_WORKER_SPEC), base_identity="BASE")
    cfg = _read(tmp_path, "coder")
    url = cfg["auxiliary"]["compression"]["base_url"]
    assert url == PROXY
    dumped = yaml.safe_dump(cfg)
    assert NODE_A not in dumped and NODE_B not in dumped
    assert cfg["auxiliary"]["compression"]["model"] == generator.WORKER_MODEL


def test_generated_worker_compaction_pinned_fleet_is_direct(tmp_path, monkeypatch):
    """A pinned fleet keeps the direct orchestrator endpoint — legitimate there
    (the role split is real), and it must pass the same guard."""
    serving = _prepare(tmp_path, monkeypatch, uniform=False)
    generator.generate_profile(dict(_WORKER_SPEC), base_identity="BASE")
    cfg = _read(tmp_path, "coder")
    assert cfg["auxiliary"]["compression"]["base_url"] == f"http://{NODE_A}:8000/v1"
    verify = _load_verify()
    result = verify.check_profile_endpoints(
        serving_path=serving,
        profiles_dir=str(tmp_path / "profiles"))
    assert result["ok"] is True, result["detail"]


def test_a_node_direct_aux_would_fail_the_pool_pin(tmp_path, monkeypatch):
    """Teeth check: hand the same guard a generated profile whose aux was
    node-direct and it must name it. Without this the passing tests above could
    be passing for the wrong reason."""
    serving = _prepare(tmp_path, monkeypatch, uniform=True)
    generator.generate_profile(dict(_ORCH_SPEC), base_identity="BASE")
    p = tmp_path / "profiles" / "orchestrator" / "config.yaml"
    cfg = yaml.safe_load(p.read_text())
    cfg["auxiliary"] = {"compression": {
        "provider": "custom", "base_url": f"http://{NODE_A}:8000/v1",
        "model": generator.STRONG_MODEL}}
    p.write_text(yaml.safe_dump(cfg))
    verify = _load_verify()
    result = verify.check_profile_endpoints(
        serving_path=serving,
        profiles_dir=str(tmp_path / "profiles"))
    assert result["ok"] is False
    assert "auxiliary.compression.base_url" in result["detail"]
    assert NODE_A in result["detail"]
