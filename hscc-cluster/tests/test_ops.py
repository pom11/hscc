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
    assert "192.0.2.24" in out["would_do"]   # preview names a chosen node

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
