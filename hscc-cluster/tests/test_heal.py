import heal

# Reaper topology comes from conftest's _pin_topology: HEAD 192.0.2.10, workers
# 192.0.2.11/.12/.13 — TEST-NET-1 only, no fleet is contacted by this file.
# serving.json is planted as a real fake FILE (the `fake_serving` fixture) rather
# than by stubbing a reader, because for this tool "orphan" IS that file's content
# and the F13 guard branches on the file's STATE (absent/corrupt/unit-less).


def test_reap_orphans_spares_serving_units(monkeypatch, fake_serving):
    fake_serving([{"name": "orch", "nodes": ["192.0.2.12"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.12", "recipe": "orch"},
        {"node": "192.0.2.13", "recipe": "stray"},
    ])
    monkeypatch.setattr(heal.cl, "run_cmd", lambda args, timeout=30:
                        {"ok": True, "stdout": "", "stderr": "", "code": 0})
    out = heal.reap_orphans({"confirm": True})
    assert out["reaped"] == [{"node": "192.0.2.13", "recipe": "stray"}]

def test_reap_orphans_requires_confirm(monkeypatch, fake_serving):
    fake_serving([{"name": "orch", "nodes": ["192.0.2.12"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [])
    out = heal.reap_orphans({})
    assert out["preview"] is True

def test_repair_nas_export_requires_confirm():
    out = heal.repair_nas_export({})
    assert out["preview"] is True and "exports" in out["would_do"].lower()

def test_restart_model_requires_confirm():
    out = heal.restart_model({"recipe": "r", "node": "192.0.2.12"})
    assert out["preview"] is True

def test_reap_never_touches_head(monkeypatch, fake_serving):
    """HEAD stays off the orphan list even though serving.json says nothing about
    it — the exemption is unconditional, not a property of the file. The
    absent/corrupt-file half of that shape is now the guard's refusal, pinned in
    test_heal_reap_serving_guard_t_b21a939e.py; here serving.json is healthy so
    this test says only what its name says."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": heal.cl.HEAD, "recipe": "orch-27b"},
        {"node": "192.0.2.13", "recipe": "stray"},
    ])
    monkeypatch.setattr(heal.cl, "run_cmd", lambda args, timeout=30:
                        {"ok": True, "stdout": "", "stderr": "", "code": 0})
    out = heal.reap_orphans({"confirm": True})
    assert {"node": heal.cl.HEAD, "recipe": "orch-27b"} not in out["reaped"]
    assert out["reaped"] == [{"node": "192.0.2.13", "recipe": "stray"}]

def test_restart_refuses_head_without_force():
    out = heal.restart_model({"recipe": "orch-27b", "node": "192.0.2.10"})
    assert out.get("refused") is True and out["executed"] is False


def test_reap_orphans_tracks_failed_stops(monkeypatch, fake_serving):
    """reap_orphans should check run_cmd result and only reap on success."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "good-orphan"},
        {"node": "192.0.2.14", "recipe": "bad-orphan"},
    ])

    def fake_run_cmd(args, timeout=30):
        recipe = args[2]  # [SPARKRUN, "stop", recipe, "--hosts", node]
        if recipe == "good-orphan":
            return {"ok": True, "stdout": "", "stderr": "", "code": 0}
        else:
            return {"ok": False, "stdout": "", "stderr": "host unreachable", "code": 22}

    monkeypatch.setattr(heal.cl, "run_cmd", fake_run_cmd)
    out = heal.reap_orphans({"confirm": True})

    assert out["ok"] is False
    assert len(out["reaped"]) == 1
    assert out["reaped"][0]["recipe"] == "good-orphan"
    assert len(out["failed"]) == 1
    assert out["failed"][0]["recipe"] == "bad-orphan"
    assert "host unreachable" in out["failed"][0]["error"]


def test_reap_orphans_ok_when_all_succeed(monkeypatch, fake_serving):
    """When all stops succeed, ok=True and failed is None."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "orphan1"},
    ])
    monkeypatch.setattr(heal.cl, "run_cmd", lambda args, timeout=30:
                        {"ok": True, "stdout": "", "stderr": "", "code": 0})

    out = heal.reap_orphans({"confirm": True})

    assert out["ok"] is True
    assert len(out["reaped"]) == 1
    assert out["failed"] is None


def test_reap_orphans_all_fail(monkeypatch, fake_serving):
    """When all stops fail, reaped is empty and ok=False."""
    fake_serving([{"name": "worker-a", "nodes": ["192.0.2.11"]}])
    monkeypatch.setattr(heal, "_running_containers", lambda: [
        {"node": "192.0.2.13", "recipe": "orphan1"},
    ])
    monkeypatch.setattr(heal.cl, "run_cmd", lambda args, timeout=30:
                        {"ok": False, "stdout": "", "stderr": "ssh refused", "code": 255})

    out = heal.reap_orphans({"confirm": True})

    assert out["ok"] is False
    assert out["reaped"] == []
    assert len(out["failed"]) == 1
    assert "ssh refused" in out["failed"][0]["error"]
