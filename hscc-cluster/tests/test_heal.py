import heal

def test_reap_orphans_spares_serving_units(monkeypatch):
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.12", "recipe": "orch"},
        {"node": "192.0.2.13", "recipe": "stray"},
    ])
    monkeypatch.setattr(heal.cl, "read_serving_units", lambda: [
        {"name": "orch", "nodes": ["192.0.2.12"]},
    ])
    out = heal.reap_orphans({"confirm": True})
    assert out["reaped"] == [{"node": "192.0.2.13", "recipe": "stray"}]

def test_reap_orphans_requires_confirm(monkeypatch):
    monkeypatch.setattr(heal, "_running_containers", lambda: [])
    monkeypatch.setattr(heal.cl, "read_serving_units", lambda: [])
    out = heal.reap_orphans({})
    assert out["preview"] is True

def test_repair_nas_export_requires_confirm():
    out = heal.repair_nas_export({})
    assert out["preview"] is True and "exports" in out["would_do"].lower()

def test_restart_model_requires_confirm():
    out = heal.restart_model({"recipe": "r", "node": "192.0.2.12"})
    assert out["preview"] is True

def test_reap_never_touches_head(monkeypatch):
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": heal.cl.HEAD, "recipe": "orch-27b"},
        {"node": "192.0.2.13", "recipe": "stray"},
    ])
    monkeypatch.setattr(heal.cl, "read_serving_units", lambda: [])  # empty/corrupt serving.json
    out = heal.reap_orphans({"confirm": True})
    assert {"node": heal.cl.HEAD, "recipe": "orch-27b"} not in out["reaped"]
    assert out["reaped"] == [{"node": "192.0.2.13", "recipe": "stray"}]

def test_restart_refuses_head_without_force():
    out = heal.restart_model({"recipe": "orch-27b", "node": "192.0.2.10"})
    assert out.get("refused") is True and out["executed"] is False
