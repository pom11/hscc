import debug

def test_nas_diagnose_classifies_estale(monkeypatch):
    def fake_ssh(host, cmd, timeout=30):
        return {"ok": False, "stdout": "", "stderr": "Stale file handle", "code": 32}
    monkeypatch.setattr(debug.cl, "ssh_cmd", fake_ssh)
    out = debug.nas_diagnose({"node": "192.0.2.11"})
    assert out["verdict"] == "stale"

def test_nas_diagnose_healthy(monkeypatch):
    def fake_ssh(host, cmd, timeout=30):
        return {"ok": True, "stdout": "probe-ok", "stderr": "", "code": 0}
    monkeypatch.setattr(debug.cl, "ssh_cmd", fake_ssh)
    out = debug.nas_diagnose({"node": "192.0.2.11"})
    assert out["verdict"] == "healthy"

def test_node_diagnostics_flags_oom(monkeypatch):
    def fake_ssh(host, cmd, timeout=30):
        return {"ok": True, "stdout": "Out of memory: Killed process 1234", "stderr": "", "code": 0}
    monkeypatch.setattr(debug.cl, "ssh_cmd", fake_ssh)
    out = debug.node_diagnostics({"node": "192.0.2.11"})
    assert out["oom_detected"] is True
