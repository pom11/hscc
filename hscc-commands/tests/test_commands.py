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
         "recipe": "~/r/27b.yaml", "nodes": ["10.0.0.2"], "keepalive": True},
        {"id": "worker-3", "role": "worker", "model": "Qwen/27B",
         "recipe": "~/r/27b.yaml", "nodes": ["10.0.0.3"], "keepalive": True},
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


# cluster-restart now prefers TEMPLATE re-apply (D14); the unit-restart path is
# the fallback when no template is recorded. These tests pin "no template".

def test_cluster_restart_preview_lists_all(monkeypatch):
    monkeypatch.setattr(cmdlib, "applied_template", lambda: None)   # no template
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    fired = []
    monkeypatch.setattr(cmdlib, "restart_one", lambda u: fired.append(u) or {})
    out = plugin.cmd_cluster_restart("")
    assert "FULL cluster restart" in out
    assert out.count("•") == 3                # orch + 2 workers
    assert fired == []


def test_cluster_restart_executes_all_with_confirm(monkeypatch):
    monkeypatch.setattr(cmdlib, "applied_template", lambda: None)
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    fired = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: fired.append(cmdlib.unit_node(u)) or
                        {"ok": True, "unit": u["id"], "node": cmdlib.unit_node(u)})
    out = plugin.cmd_cluster_restart("confirm")
    assert fired == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert "3/3 launched" in out


def test_cluster_restart_reports_failures(monkeypatch):
    monkeypatch.setattr(cmdlib, "applied_template", lambda: None)
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


# ── cluster-restart TEMPLATE recovery path (D14) ──────────────────────────────

def test_cluster_restart_reapplies_template_preview(monkeypatch):
    monkeypatch.setattr(cmdlib, "applied_template", lambda: {"template": "hscc-live"})
    called = []
    monkeypatch.setattr(cmdlib, "reapply_template",
                        lambda confirm: called.append(confirm) or {"ok": True})
    out = plugin.cmd_cluster_restart("")
    assert "re-apply template" in out.lower() and "hscc-live" in out
    assert called == []                        # preview must not execute


def test_cluster_restart_reapplies_template_confirm(monkeypatch):
    monkeypatch.setattr(cmdlib, "applied_template", lambda: {"template": "hscc-live"})
    called = []
    monkeypatch.setattr(cmdlib, "reapply_template",
                        lambda confirm: called.append(confirm) or {"ok": True})
    out = plugin.cmd_cluster_restart("confirm")
    assert called == [True]
    assert "re-applied template" in out.lower() and "hscc-live" in out


# ── /status, /heal, /template ─────────────────────────────────────────────────

def test_status_renders_topology_and_vram(monkeypatch):
    monkeypatch.setattr(cmdlib, "discovery_snapshot", lambda probe=True: {
        "ok": True, "source": "live",
        "orchestrator": {"ip": "10.0.0.1", "name": "gw"},
        "workers": [{"ip": "10.0.0.2", "vram_free_gb": 90.0, "power_draw_w": 12.0,
                     "idle": True, "vllm_healthy": True}],
        "nas": {"ip": "10.0.0.9"},
    })
    monkeypatch.setattr(cmdlib, "proxy_health", lambda: True)
    monkeypatch.setattr(cmdlib, "applied_template", lambda: {"template": "hscc-live"})
    monkeypatch.setattr(cmdlib, "autonomy_flag", lambda: "on")
    out = plugin.cmd_status("")
    assert "live" in out and "10.0.0.2" in out and "90.0GB free" in out
    assert "hscc-live" in out and "on" in out


def test_status_handles_discovery_failure(monkeypatch):
    monkeypatch.setattr(cmdlib, "discovery_snapshot", lambda probe=True: {})
    monkeypatch.setattr(cmdlib, "proxy_health", lambda: False)
    monkeypatch.setattr(cmdlib, "applied_template", lambda: None)
    monkeypatch.setattr(cmdlib, "autonomy_flag", lambda: None)
    out = plugin.cmd_status("")
    assert "discovery unavailable" in out and "down" in out


def test_heal_reports_unhealthy_and_gates(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "_curl_model",
                        lambda node: None if node == "10.0.0.2" else "m")
    fired = []
    monkeypatch.setattr(cmdlib, "restart_one", lambda u: fired.append(u) or {"ok": True})
    out = plugin.cmd_heal("")
    assert "10.0.0.2" in out and "/heal confirm" in out
    assert fired == []                          # gated


def test_heal_restarts_on_confirm(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "_curl_model",
                        lambda node: None if node == "10.0.0.2" else "m")
    fired = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: fired.append(cmdlib.unit_node(u)) or
                        {"ok": True, "unit": u["id"], "node": cmdlib.unit_node(u)})
    out = plugin.cmd_heal("confirm")
    assert fired == ["10.0.0.2"]


def test_heal_advises_cluster_restart_on_orch_wedge(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "_curl_model", lambda node: None)  # all down incl orch
    monkeypatch.setattr(cmdlib, "restart_one", lambda u: {"ok": True})
    out = plugin.cmd_heal("")
    assert "/cluster-restart" in out and "wedged" in out.lower()


def test_template_routes_to_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(cmdlib, "template_cli", lambda argv: seen.append(argv) or {"templates": []})
    plugin.cmd_template("list")
    plugin.cmd_template("apply hscc-live confirm")
    assert seen[0] == ["x", "list"]
    assert seen[1] == ["x", "apply", "hscc-live", "--confirm"]


def test_template_usage_on_bad_subcommand():
    out = plugin.cmd_template("frobnicate")
    assert "Usage:" in out


# ── /cluster read-only ───────────────────────────────────────────────────────

def test_cluster_status_never_mutates(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "_curl_model",
                        lambda node: "nvidia/Qwen3.6-35B-A3B-NVFP4"
                        if node == "10.0.0.1" else None)
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {})  # no live sparkrun
    out = plugin.cmd_cluster("")
    assert "Orchestrator" in out and "10.0.0.1" in out
    assert "❌" in out                          # a down worker shows


def test_cluster_status_shows_metrics(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "_curl_model", lambda node: "m")
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {
        "10.0.0.1": {"cpu_usage_pct": "12", "cpu_temp_c": "55",
                     "cpu_load_1m": "0.5", "mem_used_pct": "40",
                     "mem_available_mb": "90000", "gpu_name": "GB10",
                     "gpu_util_pct": "30", "gpu_temp_c": "60", "gpu_power_w": "40"}})
    out = plugin.cmd_cluster("")
    assert "GPU GB10" in out and "30% util" in out


def test_cluster_metrics_empty_on_failure(monkeypatch):
    monkeypatch.setattr(cmdlib, "_run", lambda *a, **k: (False, "", "boom"))
    assert cmdlib.cluster_metrics() == {}


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


# ── registration + topology-free ──────────────────────────────────────────────

def test_register_exposes_all_commands():
    names = []

    class Ctx:
        def register_command(self, **kw):
            names.append(kw["name"])
    plugin.register(Ctx())
    assert set(names) == {"cluster", "status", "orch-restart",
                          "cluster-restart", "heal", "template",
                          "cluster-reboot", "cluster-down",
                          "cluster-docker-prune", "cluster-apt-upgrade",
                          "cluster-prune", "workers-up"}


def test_no_hardcoded_ips_in_source():
    import re
    import os as _os
    here = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    for fn in ("__init__.py", "cmdlib.py"):
        text = open(_os.path.join(here, fn)).read()
        assert not re.search(r"\b192\.168\.\d{1,3}\.\d{1,3}\b", text), fn


# ── /workers-up ────────────────────────────────────────────────────────────

def test_workers_up_skips_online_workers(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "check_unit_health", lambda node: True)
    restarted = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: restarted.append(u) or {"ok": True})
    out = plugin.cmd_workers_up("")
    assert restarted == []
    assert "already online" in out
    assert "10.0.0.2" in out and "10.0.0.3" in out


def test_workers_up_launches_down_workers(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "check_unit_health",
                        lambda node: node != "10.0.0.2")  # worker-2 down
    restarted = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: restarted.append(cmdlib.unit_node(u)) or
                        {"ok": True, "unit": u["id"], "node": cmdlib.unit_node(u)})
    out = plugin.cmd_workers_up("")
    assert restarted == ["10.0.0.2"]
    assert "already online" in out
    assert "launched" in out


def test_workers_up_never_restarts_orchestrator(monkeypatch):
    """Orchestrator is never in keepalive list — even if it were down."""
    units_with_orch_keepalive = {
        "version": 1, "port": 8000,
        "units": [
            {"id": "orch-a3b", "role": "orchestrator",
             "model": "x", "recipe": "~/r/a3b.yaml",
             "nodes": ["10.0.0.1"], "keepalive": True},
            {"id": "worker-2", "role": "worker", "model": "Qwen/27B",
             "recipe": "~/r/27b.yaml", "nodes": ["10.0.0.2"], "keepalive": True},
        ],
    }
    monkeypatch.setattr(cmdlib, "read_units", lambda: units_with_orch_keepalive["units"])
    monkeypatch.setattr(cmdlib, "check_unit_health", lambda node: False)
    restarted = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: restarted.append(u.get("role")) or
                        {"ok": True, "unit": u["id"], "node": cmdlib.unit_node(u)})
    out = plugin.cmd_workers_up("")
    assert "orchestrator" not in restarted
    assert restarted == ["worker"]


def test_workers_up_reports_failure(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "check_unit_health", lambda node: False)
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: {
                            "ok": False,
                            "unit": u["id"],
                            "node": cmdlib.unit_node(u),
                            "error": "ssh refused",
                        })
    out = plugin.cmd_workers_up("")
    assert "failed" in out and "ssh refused" in out


def test_workers_up_no_keepalive_workers(monkeypatch):
    units = {
        "version": 1, "port": 8000,
        "units": [
            {"id": "w1", "role": "worker", "model": "m",
             "recipe": "~/r/m.yaml", "nodes": ["10.0.0.2"]},  # no keepalive
        ],
    }
    monkeypatch.setattr(cmdlib, "read_units", lambda: units["units"])
    out = plugin.cmd_workers_up("")
    assert "nothing to check" in out
