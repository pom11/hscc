import json, os, tempfile
import clusterlib as cl  # run pytest from the plugin dir

def test_read_serving_units_empty(tmp_path, monkeypatch):
    f = tmp_path / "serving.json"
    f.write_text(json.dumps({"units": []}))
    monkeypatch.setattr(cl, "SERVING_JSON", str(f))
    assert cl.read_serving_units() == []

def test_read_serving_units_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "SERVING_JSON", str(tmp_path / "nope.json"))
    assert cl.read_serving_units() == []   # tolerate missing, never raise

def test_read_serving_units_wrong_shape(tmp_path, monkeypatch):
    f = tmp_path / "serving.json"
    f.write_text(json.dumps(["not", "a", "dict"]))  # valid JSON, wrong shape
    monkeypatch.setattr(cl, "SERVING_JSON", str(f))
    assert cl.read_serving_units() == []   # tolerate corrupt shape, never raise

def test_confirm_preview_blocks_without_confirm():
    out = cl.confirm_gate(False, action="provision_model qwen on .246")
    assert out["preview"] is True and out["executed"] is False
    assert "provision_model qwen on .246" in out["would_do"]

def test_confirm_gate_passes_with_confirm():
    out = cl.confirm_gate(True, action="x")
    assert out is None   # None means "proceed to execute"
