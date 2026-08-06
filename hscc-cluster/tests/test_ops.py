import inspect, json
import ops, debug, heal

def test_handlers_accept_dispatch_kwargs(monkeypatch):
    # Hermes registry.dispatch calls handlers as handler(args, task_id=..., user_task=...).
    # Every registered handler must absorb those kwargs or it crashes at runtime.
    monkeypatch.setattr(ops, "_running_by_node", lambda: {})
    out = ops.cluster_status({}, task_id="t1", user_task="do x")
    assert "head" in out

def test_all_registered_handlers_have_var_keyword():
    handlers = [ops.cluster_status, ops.list_recipes, ops.pick_node,
                ops.provision_model, ops.stop_model, ops.model_health,
                debug.vllm_logs, debug.node_diagnostics, debug.nas_diagnose,
                heal.restart_model, heal.remount_nas, heal.repair_nas_export,
                heal.reap_orphans]
    for h in handlers:
        kinds = [p.kind for p in inspect.signature(h).parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD in kinds, f"{h.__name__} lacks **kwargs"

_STATUS_SAMPLE = """Job: @local-official/qwen3.6-27b-fp8-vllm  (tp=1, pp=1)  [294ec31919c6]  (1 container(s))
  solo       192.0.2.10                           Up About an hour          sparkrun-eugr-vllm
  logs: sparkrun logs 294ec31919c6
  stop: sparkrun stop 294ec31919c6

Idle hosts (no sparkrun containers):
  192.0.2.11
  192.0.2.12
  192.0.2.13

Total: 1 container(s) across 4 host(s)
"""

def test_running_by_node_excludes_idle_section(monkeypatch):
    monkeypatch.setattr(ops.cl, "run_cmd",
                        lambda a, timeout=30: {"ok": True, "stdout": _STATUS_SAMPLE,
                                               "stderr": "", "code": 0})
    m = ops._running_by_node()
    # only the head is running; idle hosts must NOT appear as running
    assert m == {"192.0.2.10": "@local-official/qwen3.6-27b-fp8-vllm"}


def test_running_by_node_no_substring_ip_match(monkeypatch):
    """Regression: '10.0.0.1' must NOT match a line containing '10.0.0.10'."""
    sample = """Job: @local-official/qwen3.6-27b-fp8-vllm  (tp=1, pp=1)  [abc123]  (1 container(s))
  solo       10.0.0.10                          Up About an hour          sparkrun-eugr-vllm
  logs: sparkrun logs abc123

Total: 1 container(s) across 2 host(s)
"""
    monkeypatch.setattr(ops.cl, "NODES", ["10.0.0.1"])
    monkeypatch.setattr(ops.cl, "HEAD", "10.0.0.1")
    monkeypatch.setattr(ops.cl, "run_cmd",
                        lambda a, timeout=30: {"ok": True, "stdout": sample,
                                               "stderr": "", "code": 0})
    m = ops._running_by_node()
    # 10.0.0.1 should NOT match the line for 10.0.0.10
    assert "10.0.0.1" not in m
    assert len(m) == 0


def test_running_by_node_exact_match_longer_ip(monkeypatch):
    """'10.0.0.10' must match a line containing '10.0.0.10' exactly."""
    sample = """Job: @local-official/qwen3.6-27b-fp8-vllm  (tp=1, pp=1)  [abc123]  (1 container(s))
  solo       10.0.0.100                         Up About an hour          sparkrun-eugr-vllm
  logs: sparkrun logs abc123

Total: 1 container(s) across 2 host(s)
"""
    monkeypatch.setattr(ops.cl, "NODES", ["10.0.0.10"])
    monkeypatch.setattr(ops.cl, "HEAD", "10.0.0.10")
    monkeypatch.setattr(ops.cl, "run_cmd",
                        lambda a, timeout=30: {"ok": True, "stdout": sample,
                                               "stderr": "", "code": 0})
    m = ops._running_by_node()
    # 10.0.0.10 should NOT match 10.0.0.100 either
    assert "10.0.0.10" not in m

def test_cluster_status_idle_and_units(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.cl, "run_cmd",
                        lambda a, timeout=30: {"ok": True, "stdout": _STATUS_SAMPLE,
                                               "stderr": "", "code": 0})
    f = tmp_path / "serving.json"
    f.write_text(json.dumps({"units": [{"id": "orch-27b", "nodes": ["192.0.2.10"]}]}))
    monkeypatch.setattr(ops.cl, "SERVING_JSON", str(f))
    out = ops.cluster_status({})
    assert set(out["idle_nodes"]) == {"192.0.2.11", "192.0.2.12", "192.0.2.13"}
    assert out["serving_units"] == ["orch-27b"]

def test_pick_node_prefers_idle(monkeypatch):
    monkeypatch.setattr(ops, "_running_by_node", lambda: {"192.0.2.11": "qwenX"})
    node = ops.pick_node({})["node"]
    assert node in ("192.0.2.12", "192.0.2.13")

def test_pick_node_none_when_all_busy(monkeypatch):
    monkeypatch.setattr(ops, "_running_by_node", lambda: {
        "192.0.2.11": "a", "192.0.2.12": "b", "192.0.2.13": "c"})
    out = ops.pick_node({})
    assert out["node"] is None and "no idle" in out["reason"].lower()

def test_provision_requires_confirm(monkeypatch):
    monkeypatch.setattr(ops, "_running_by_node", lambda: {})
    out = ops.provision_model({"recipe": "qwen3.6-27b-fp8-vllm", "node": "192.0.2.12"})
    assert out["preview"] is True and out["executed"] is False

def test_provision_auto_node_uses_pick(monkeypatch):
    monkeypatch.setattr(ops, "_running_by_node", lambda: {"192.0.2.11": "x"})
    out = ops.provision_model({"recipe": "r", "node": "auto"})
    # .11 busy -> first idle worker is .12 (pick_node returns idle[0])
    assert "192.0.2.12" in out["would_do"]   # preview names the chosen idle node

def test_provision_executes_with_confirm(monkeypatch):
    monkeypatch.setattr(ops, "_running_by_node", lambda: {})
    calls = {}
    def fake_run(args, timeout=30):
        calls["args"] = args
        return {"ok": True, "stdout": "Serve command", "stderr": "", "code": 0}
    monkeypatch.setattr(ops.cl, "run_cmd", fake_run)
    out = ops.provision_model({"recipe": "r", "node": "192.0.2.12", "confirm": True})
    assert out["executed"] is True
    assert out["base_url"] == "http://192.0.2.12:8000/v1"
    assert "run" in calls["args"] and "--hosts" in calls["args"]


def test_provision_uses_correct_invocation(monkeypatch):
    """H2: provision must pass --cluster (NAS cache), --port, --ensure and
    expand ~ in the recipe path — not the old bare `run <recipe> --hosts`."""
    import os
    monkeypatch.setattr(ops, "_running_by_node", lambda: {})
    monkeypatch.setattr(ops, "_cluster_name", lambda: "hscc")
    calls = {}
    def fake_run(args, timeout=30):
        calls["args"] = args
        return {"ok": True, "stdout": "", "stderr": "", "code": 0}
    monkeypatch.setattr(ops.cl, "run_cmd", fake_run)
    out = ops.provision_model({"recipe": "~/r/a.yaml", "node": "192.0.2.12",
                               "port": 8001, "confirm": True})
    a = calls["args"]
    assert "--cluster" in a and "hscc" in a
    assert "--port" in a and "8001" in a
    assert "--ensure" in a
    assert os.path.expanduser("~/r/a.yaml") in a   # ~ expanded
    assert out["base_url"] == "http://192.0.2.12:8001/v1"


def test_stop_requires_confirm():
    out = ops.stop_model({"recipe": "r", "node": "192.0.2.12"})
    assert out["preview"] is True

def test_stop_refuses_head_without_force():
    out = ops.stop_model({"recipe": "orch-27b", "node": "192.0.2.10"})
    assert out.get("refused") is True and out["executed"] is False

def test_stop_head_with_force_executes(monkeypatch):
    monkeypatch.setattr(ops.cl, "run_cmd",
                        lambda a, timeout=30: {"ok": True, "stdout": "", "stderr": "", "code": 0})
    out = ops.stop_model({"recipe": "orch-27b", "node": "192.0.2.10",
                          "confirm": True, "force": True})
    assert out["executed"] is True


# ── tp-peer handling (Card 3: v1.5.1 cluster status) ───────────────────────

_LIVE_TOPOLOGY_UNITS = [
    # orch: head (.244) primary, .246 is the tp peer
    {"id": "orch", "nodes": ["10.0.0.244", "10.0.0.246"]},
    # reasoning family: .247 primary, .248 is the tp peer (tp=2 span)
    {"id": "family-reasoning-DeepSeek-V4-Flash-0731-247-8000",
     "nodes": ["10.0.0.247", "10.0.0.248"], "tp": 2, "pp": 1},
]

def test_tp_peer_nodes_live_topology():
    # The live-topology-shaped serving.json must yield exactly {.246, .248}.
    assert ops._tp_peer_nodes(_LIVE_TOPOLOGY_UNITS) == {"10.0.0.246", "10.0.0.248"}

def test_tp_peer_nodes_empty_when_no_span():
    assert ops._tp_peer_nodes([{"id": "a", "nodes": ["1.2.3.4"]},
                               {"id": "b", "nodes": ["5.6.7.8"], "tp": 1}]) == set()
    assert ops._tp_peer_nodes([]) == set()
    assert ops._tp_peer_nodes(None) == set()
    # tp>1 with a single resolved node has no non-primary member -> no peer
    assert ops._tp_peer_nodes([{"id": "a", "nodes": ["1.2.3.4"], "tp": 2}]) == set()

def test_tp_peer_nodes_ignores_units_without_nodes():
    assert ops._tp_peer_nodes([{"id": "a"}, {"id": "b", "tp": 4}]) == set()

# Pinned test topology (conftest): HEAD=192.0.2.10, NODES=[.11, .12, .13].
# orch spans .10(head)+.11(peer); reasoning spans .12(primary)+.13(peer,tp=2).
_TP_UNITS_PINNED = [
    {"id": "orch", "nodes": ["192.0.2.10", "192.0.2.11"]},
    {"id": "family-reasoning-27b-12-8000",
     "nodes": ["192.0.2.12", "192.0.2.13"], "tp": 2, "pp": 1},
]

def test_cluster_status_exposes_tp_peer_nodes(monkeypatch):
    # sparkrun status shows every node idle, but .11/.13 hold tp-peer roles
    # in spans -> they must not read as plain idle workers.
    monkeypatch.setattr(ops, "_running_by_node", lambda: {})
    monkeypatch.setattr(ops.cl, "read_serving_units", lambda: _TP_UNITS_PINNED)
    out = ops.cluster_status({})
    assert set(out["tp_peer_nodes"]) == {"192.0.2.11", "192.0.2.13"}
    assert "192.0.2.11" not in out["idle_nodes"]
    assert "192.0.2.13" not in out["idle_nodes"]
    assert out["idle_nodes"] == ["192.0.2.12"]
    # existing result keys preserved (ADD don't remove)
    for k in ("head", "running", "idle_nodes", "serving_units", "source"):
        assert k in out

def test_pick_node_never_targets_tp_peer(monkeypatch):
    # .11/.13 are tp peers but sparkrun shows them idle; auto-pick must not
    # provision onto them (would collide with / corrupt the tp span).
    monkeypatch.setattr(ops, "_running_by_node", lambda: {})
    monkeypatch.setattr(ops.cl, "read_serving_units", lambda: _TP_UNITS_PINNED)
    node = ops.pick_node({})["node"]
    assert node == "192.0.2.12"
    assert node not in ("192.0.2.11", "192.0.2.13")

def test_pick_node_tp_peer_reserved_even_when_idle(monkeypatch):
    # .13 looks idle in sparkrun but is a tp peer -> still excluded from idle
    monkeypatch.setattr(ops, "_running_by_node",
                        lambda: {"192.0.2.11": "orch-recipe"})
    monkeypatch.setattr(ops.cl, "read_serving_units", lambda: _TP_UNITS_PINNED)
    assert ops.pick_node({})["node"] == "192.0.2.12"

def test_pick_node_no_tp_span_behaves_as_before(monkeypatch):
    # no serving units / no tp spans -> tp peers empty, pick_node unchanged
    monkeypatch.setattr(ops, "_running_by_node", lambda: {"192.0.2.11": "qwenX"})
    monkeypatch.setattr(ops.cl, "read_serving_units", lambda: [])
    assert ops.pick_node({})["node"] in ("192.0.2.12", "192.0.2.13")

