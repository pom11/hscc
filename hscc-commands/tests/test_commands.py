import json

import cmdlib
import __init__ as plugin


SERVING = {
    "version": 1, "port": 8000,
    "units": [
        {"id": "orch-a3b", "role": "orchestrator",
         "model": "nvidia/Qwen3.6-35B-A3B-NVFP4",
         "recipe": "~/r/a3b.yaml", "nodes": ["10.0.0.1"]},
        {"id": "worker-2", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["10.0.0.2"]},
        {"id": "worker-3", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["10.0.0.3"]},
    ],
}


# ── parsing ────────────────────────────────────────────────────────────────

def test_read_units_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cmdlib, "SERVING_JSON", str(tmp_path / "nope.json"))
    assert cmdlib.read_units() == []


def test_orchestrator_and_workers(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    units = cmdlib.read_units()
    assert cmdlib.orchestrator_unit(units)["id"] == "orch-a3b"
    assert [cmdlib.unit_node(w) for w in cmdlib.worker_units(units)] == \
        ["10.0.0.2", "10.0.0.3"]


def test_no_orchestrator():
    assert cmdlib.orchestrator_unit(
        [{"role": "worker", "nodes": ["10.0.0.2"]}]) is None


# ── confirm gating ───────────────────────────────────────────────────────────

def test_confirmed_words():
    assert plugin._confirmed("confirm")
    assert plugin._confirmed("yes please")
    assert plugin._confirmed("y")
    assert not plugin._confirmed("")
    assert not plugin._confirmed("maybe later")


def test_orch_restart_previews_without_confirm(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    called = []
    monkeypatch.setattr(cmdlib, "restart_one", lambda u: called.append(u) or {})
    out = plugin.cmd_orch_restart("")
    assert "Confirm" in out and "10.0.0.1" in out
    assert called == []                       # must NOT execute on preview


def test_orch_restart_executes_with_confirm(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    seen = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: seen.append(u["id"]) or {"ok": True, "unit": u["id"]})
    out = plugin.cmd_orch_restart("confirm")
    assert seen == ["orch-a3b"]
    assert "launched" in out.lower()


def test_cluster_restart_preview_lists_all(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    fired = []
    monkeypatch.setattr(cmdlib, "restart_one", lambda u: fired.append(u) or {})
    out = plugin.cmd_cluster_restart("")
    assert "FULL cluster restart" in out
    assert out.count("•") == 3                # orch + 2 workers
    assert fired == []


def test_cluster_restart_executes_all_with_confirm(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    fired = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: fired.append(cmdlib.unit_node(u)) or
                        {"ok": True, "unit": u["id"], "node": cmdlib.unit_node(u)})
    out = plugin.cmd_cluster_restart("confirm")
    assert fired == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert "3/3 launched" in out


def test_cluster_restart_reports_failures(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])

    def fake(u):
        node = cmdlib.unit_node(u)
        ok = node != "10.0.0.2"
        return {"ok": ok, "unit": u["id"], "node": node,
                "error": None if ok else "boom"}
    monkeypatch.setattr(cmdlib, "restart_one", fake)
    out = plugin.cmd_cluster_restart("confirm")
    assert "2/3 launched" in out
    assert "boom" in out


# ── /cluster read-only ───────────────────────────────────────────────────────

def test_cluster_status_never_mutates(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "_curl_model",
                        lambda node: "nvidia/Qwen3.6-35B-A3B-NVFP4"
                        if node == "10.0.0.1" else None)
    out = plugin.cmd_cluster("")
    assert "Orchestrator" in out and "10.0.0.1" in out
    assert "❌" in out                          # a down worker shows


# ── restart_one shells out correctly ─────────────────────────────────────────

def test_restart_one_missing_recipe():
    res = cmdlib.restart_one({"id": "x", "role": "worker", "nodes": ["10.0.0.9"]})
    assert res["ok"] is False and "recipe" in res["error"]


def test_restart_one_calls_stop_then_run(monkeypatch):
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args)
        return True, "", ""
    monkeypatch.setattr(cmdlib, "_run", fake_run)
    res = cmdlib.restart_one(SERVING["units"][0])
    assert res["ok"] is True
    assert calls[0][:2] == [cmdlib.SPARKRUN, "stop"]
    assert calls[1][:2] == [cmdlib.SPARKRUN, "run"]
