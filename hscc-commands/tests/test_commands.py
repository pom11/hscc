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


def test_status_tp_peer_worker_never_false_down(monkeypatch):
    """A tp-peer worker has vllm_healthy False (its model lives on the span's
    primary) — /status must render it as 'tp peer', NEVER ❌/unhealthy."""
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: {
        "10.0.0.247": {"role": "worker", "unit_id": "family-reasoning-247-8000",
                       "model": "m", "tp_peer": False, "primary_of_unit": True},
        "10.0.0.248": {"role": "worker", "unit_id": "family-reasoning-247-8000",
                       "model": "m", "tp_peer": True, "primary_of_unit": False},
    })
    monkeypatch.setattr(cmdlib, "discovery_snapshot", lambda probe=True: {
        "ok": True, "source": "live",
        "orchestrator": {"ip": "10.0.0.244", "name": "gw"},
        "workers": [
            {"ip": "10.0.0.247", "vram_free_gb": 20.0, "power_draw_w": 300.0,
             "idle": False, "vllm_healthy": True},
            {"ip": "10.0.0.248", "vram_free_gb": 30.0, "power_draw_w": 310.0,
             "idle": False, "vllm_healthy": False},   # peer: False is NORMAL
        ],
    })
    monkeypatch.setattr(cmdlib, "proxy_health", lambda: True)
    monkeypatch.setattr(cmdlib, "applied_template", lambda: {"template": "hscc-live"})
    monkeypatch.setattr(cmdlib, "autonomy_flag", lambda: "on")
    out = plugin.cmd_status("")
    # the peer renders as tp peer with its span, never ❌ / unhealthy
    assert "🔗 tp peer (member of family-reasoning-247-8000/10.0.0.247 span) 10.0.0.248" in out
    assert "❌" not in out
    # healthy primary still shows ✅ worker
    assert "✅ worker 10.0.0.247" in out
    # free-VRAM line kept
    assert "30.0GB free" in out


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
    """cmd_cluster renders every node from the engine, distinct states, tp peer
    never down. Read-only — no mutation, no live endpoints hit."""
    hosts, score, nodes = _four_node_engine()
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: hosts)
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: score)
    monkeypatch.setattr(cmdlib, "enumerate_cluster_nodes", lambda *a, **k: nodes)
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {})  # no live sparkrun

    out = plugin.cmd_cluster("")
    assert "Orchestrator" in out and "10.0.0.244" in out
    # tp peer rendered as tp peer, NEVER as down/❌
    assert "🔗 tp peer" in out and "tp peer" in out
    assert "serving" in out
    assert "idle" in out


def test_cluster_status_shows_metrics(monkeypatch):
    """Metrics attach per-node from cluster_metrics, incl. the VRAM-used line."""
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: [
        {"ip": "10.0.0.1", "name": "gw", "id": "gw", "role": "gateway"},
        {"ip": "10.0.0.2", "name": "w1", "id": "w1", "role": "worker"},
    ])
    monkeypatch.setattr(cmdlib, "read_units",
                        lambda: [{"id": "u1", "role": "worker", "model": "m",
                                  "nodes": ["10.0.0.1"]}])
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: {
        "10.0.0.1": {"role": "worker", "unit_id": "u1", "model": "m",
                     "tp_peer": False, "primary_of_unit": True},
        "10.0.0.2": {"role": "worker", "unit_id": "u1", "model": "m",
                     "tp_peer": True, "primary_of_unit": False},
    })
    monkeypatch.setattr(cmdlib, "enumerate_cluster_nodes", lambda *a, **k: [
        {"ip": "10.0.0.1", "state": "serving", "model": "m", "name": "gw",
         "id": "gw", "role": "gateway", "detail": ""},
        {"ip": "10.0.0.2", "state": "tp_peer", "model": None, "name": "w1",
         "id": "w1", "role": "worker", "detail": "tp peer of a multi-node span"},
    ])
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {
        "10.0.0.1": {"cpu_usage_pct": "12", "cpu_temp_c": "55",
                     "cpu_load_1m": "0.5", "mem_used_pct": "40",
                     "mem_available_mb": "90000", "gpu_name": "GB10",
                     "gpu_util_pct": "30", "gpu_temp_c": "60", "gpu_power_w": "40",
                     "gpu_mem_used_pct": "80"}})
    out = plugin.cmd_cluster("")
    assert "GPU GB10" in out and "30% util" in out
    assert "VRAM: 80% used by model" in out   # loaded-but-idle is unambiguous


def test_cluster_metrics_empty_on_failure(monkeypatch):
    monkeypatch.setattr(cmdlib, "_run", lambda *a, **k: (False, "", "boom"))
    assert cmdlib.cluster_metrics() == {}


def _four_node_engine():
    """4-node, live-topology-shaped engine result.

    - .244 orchestrator primary (serving)
    - .246 orchestrator tp peer
    - .247 worker primary (serving)
    - .248 worker idle (reachable, no model)

    Returns (hosts_from_cluster_json, scoreboard, engine_nodes)."""
    hosts = [
        {"ip": "10.0.0.244", "name": "GX10 Gateway", "id": "gx10-gateway", "role": "gateway"},
        {"ip": "10.0.0.246", "name": "GX10 #1", "id": "gx10-worker-1", "role": "worker"},
        {"ip": "10.0.0.247", "name": "GX10 #2", "id": "gx10-worker-2", "role": "worker"},
        {"ip": "10.0.0.248", "name": "GX10 #3", "id": "gx10-worker-3", "role": "worker"},
    ]
    score = {
        "10.0.0.244": {"role": "orchestrator", "unit_id": "orch", "model": "m",
                       "tp_peer": False, "primary_of_unit": True},
        "10.0.0.246": {"role": "worker", "unit_id": "orch", "model": "m",
                       "tp_peer": True, "primary_of_unit": False},
        "10.0.0.247": {"role": "worker", "unit_id": "family-reasoning-247-8000",
                       "model": "m", "tp_peer": False, "primary_of_unit": True},
        "10.0.0.248": {"role": "worker", "unit_id": "family-reasoning-247-8000",
                       "model": "m", "tp_peer": False, "primary_of_unit": True},
    }
    nodes = [
        {"ip": "10.0.0.244", "state": "serving", "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
         "name": "GX10 Gateway", "id": "gx10-gateway", "role": "gateway", "detail": ""},
        {"ip": "10.0.0.246", "state": "tp_peer", "model": None,
         "name": "GX10 #1", "id": "gx10-worker-1", "role": "worker",
         "detail": "tp peer of a multi-node span"},
        {"ip": "10.0.0.247", "state": "serving", "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
         "name": "GX10 #2", "id": "gx10-worker-2", "role": "worker", "detail": ""},
        {"ip": "10.0.0.248", "state": "idle", "model": None,
         "name": "GX10 #3", "id": "gx10-worker-3", "role": "worker", "detail": "reachable, no model served"},
    ]
    return hosts, score, nodes


def test_cluster_renders_all_nodes_with_distinct_states(monkeypatch):
    """4-node live-topology-shaped engine output → all 4 IPs, count is 4,
    tp peers shown as 'tp peer' (never down), states distinguish serving vs
    idle vs unreachable."""
    hosts, score, nodes = _four_node_engine()
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: hosts)
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: score)
    monkeypatch.setattr(cmdlib, "enumerate_cluster_nodes", lambda *a, **k: nodes)
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {})

    out = plugin.cmd_cluster("")
    for ip in ("10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"):
        assert ip in out, f"missing node {ip}"
    assert "*Nodes (4)*" in out             # header counts NODES, not units
    assert "source: cluster.json" in out    # authoritative source, labeled
    # tp peers distinct & never down
    assert "🔗 tp peer (member of orch/10.0.0.244 span) @ 10.0.0.246" in out
    assert "✅ serving `deepseek-ai/DeepSeek-V4-Flash-0731` @ 10.0.0.247" in out
    # serving vs idle states distinguishable
    assert "○ idle @ 10.0.0.248  (model not loaded)" in out


def test_cluster_renders_unreachable_distinctly(monkeypatch):
    """An unreachable node shows as ❌ unreachable, distinct from idle/serving."""
    hosts, score, nodes = _four_node_engine()
    # make 248 unreachable instead of idle
    for n in nodes:
        if n["ip"] == "10.0.0.248":
            n["state"], n["model"], n["detail"] = "unreachable", None, "no response on reachability probe"
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: hosts)
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: score)
    monkeypatch.setattr(cmdlib, "enumerate_cluster_nodes", lambda *a, **k: nodes)
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {})
    out = plugin.cmd_cluster("")
    assert "❌ unreachable @ 10.0.0.248" in out


def test_cluster_falls_back_to_legacy_when_engine_empty(monkeypatch):
    """Engine returns [] (no cluster.json AND no serving.json units) → the old
    serving.json-primary rendering is used, clearly labeled, no crash."""
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "enumerate_cluster_nodes", lambda *a, **k: [])
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: {})
    monkeypatch.setattr(cmdlib, "_curl_model",
                        lambda node: "nvidia/Qwen3.6-35B-A3B-NVFP4"
                        if node == "10.0.0.1" else None)
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {})
    out = plugin.cmd_cluster("")
    assert "legacy serving.json-primary rendering" in out  # clearly labeled
    assert "Orchestrator" in out and "10.0.0.1" in out
    assert "Workers" in out and "10.0.0.2" in out and "10.0.0.3" in out


def test_cluster_loaded_but_idle_label(monkeypatch):
    """A serving node reading 0% GPU util is labeled 'model loaded, idle between
    requests' — the label is the fix, never a tampered 0% number (2b)."""
    hosts, score, nodes = _four_node_engine()
    # keep 247 serving and give IT the 0%-util metric
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: hosts)
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    monkeypatch.setattr(cmdlib, "serving_unit_scoreboard", lambda: score)
    monkeypatch.setattr(cmdlib, "enumerate_cluster_nodes", lambda *a, **k: nodes)
    monkeypatch.setattr(cmdlib, "cluster_metrics", lambda: {
        "10.0.0.247": {"gpu_name": "GB10", "gpu_util_pct": "0", "gpu_temp_c": "60",
                       "gpu_power_w": "40", "gpu_mem_used_pct": "85",
                       "cpu_usage_pct": "1", "cpu_temp_c": "50", "cpu_load_1m": "0.1",
                       "mem_used_pct": "70", "mem_available_mb": "30000"}})
    out = plugin.cmd_cluster("")
    # state line already says serving + the loaded-but-idle note appears
    assert "✅ serving `deepseek-ai/DeepSeek-V4-Flash-0731` @ 10.0.0.247" in out
    assert "(model loaded, idle between requests)" in out
    # 0% util is kept as the true number, not tampered
    assert "0% util" in out
    assert "VRAM: 85% used by model" in out


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


def test_restart_one_stops_only_target_recipe(monkeypatch):
    """restart_one must stop the unit's recipe, not --all (which kills siblings)."""
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args)
        return True, "", ""
    monkeypatch.setattr(cmdlib, "_run", fake_run)
    unit = {"id": "w1", "role": "worker", "recipe": "~/r/27b.yaml",
            "nodes": ["10.0.0.2"], "port": 8001}
    cmdlib.restart_one(unit)
    stop_call = calls[0]
    assert "--all" not in stop_call, "must not use --all"
    assert "~/r/27b.yaml" in stop_call or "/Users" in stop_call[2]  # recipe is the target
    assert stop_call[1] == "stop"
    assert stop_call[2] != "--all"


def test_restart_one_uses_unit_port(monkeypatch):
    """restart_one must use the unit's real port, not the default PORT=8000."""
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args)
        return True, "", ""
    monkeypatch.setattr(cmdlib, "_run", fake_run)
    unit = {"id": "w1", "role": "worker", "recipe": "~/r/27b.yaml",
            "nodes": ["10.0.0.2"], "port": 8001}
    cmdlib.restart_one(unit)
    run_call = calls[1]
    port_idx = run_call.index("--port")
    assert run_call[port_idx + 1] == "8001"


def test_restart_one_falls_back_to_default_port(monkeypatch):
    """When unit has no port key, restart_one defaults to PORT=8000."""
    calls = []

    def fake_run(args, timeout=0):
        calls.append(args)
        return True, "", ""
    monkeypatch.setattr(cmdlib, "_run", fake_run)
    unit = {"id": "w1", "role": "worker", "recipe": "~/r/27b.yaml",
            "nodes": ["10.0.0.2"]}  # no port
    cmdlib.restart_one(unit)
    run_call = calls[1]
    port_idx = run_call.index("--port")
    assert run_call[port_idx + 1] == "8000"


# ── registration + topology-free ──────────────────────────────────────────────

def test_register_exposes_all_commands():
    names = []

    class Ctx:
        def register_command(self, **kw):
            names.append(kw["name"])
    plugin.register(Ctx())
    assert set(names) == {"cluster", "status", "orch-restart",
                          "cluster-restart", "cluster-reboot",
                          "cluster-down", "cluster-docker-prune",
                          "cluster-apt-upgrade", "cluster-prune",
                          "heal", "template", "workers-up"}


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
    monkeypatch.setattr(cmdlib, "check_unit_health", lambda node, port=None: True)
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
                        lambda node, port=None: node != "10.0.0.2")  # worker-2 down
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
    monkeypatch.setattr(cmdlib, "check_unit_health", lambda node, port=None: False)
    restarted = []
    monkeypatch.setattr(cmdlib, "restart_one",
                        lambda u: restarted.append(u.get("role")) or
                        {"ok": True, "unit": u["id"], "node": cmdlib.unit_node(u)})
    out = plugin.cmd_workers_up("")
    assert "orchestrator" not in restarted
    assert restarted == ["worker"]


def test_workers_up_reports_failure(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING["units"])
    monkeypatch.setattr(cmdlib, "check_unit_health", lambda node, port=None: False)
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


# ── cluster.json node-state engine ────────────────────────────────────────────
# Cluster.json is the authoritative host list; serving.json decides tp roles.
# These tests use fake 10.0.0.x addresses so real endpoints are never probed.

CLUSTER_LIVE = {
    "name": "hscc",
    "gateway": {"ip": "10.0.0.244", "name": "GX10 Gateway",
                "sshUser": "spark", "id": "gx10-gateway", "role": "gateway"},
    "workers": [
        {"ip": "10.0.0.246", "name": "GX10 #1", "sshUser": "spark",
         "id": "gx10-worker-1", "role": "worker"},
        {"ip": "10.0.0.247", "name": "GX10 #2", "sshUser": "spark",
         "id": "gx10-worker-2", "role": "worker"},
        {"ip": "10.0.0.248", "name": "GX10 #3", "sshUser": "spark",
         "id": "gx10-worker-3", "role": "worker"},
    ],
}

SERVING_LIVE = {
    "version": 2,
    "units": [
        {"id": "orch", "role": "orchestrator",
         "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
         "recipe": "~/r/v4.yaml",
         "nodes": ["10.0.0.244", "10.0.0.246"], "port": 8000},
        {"id": "family-reasoning-247-8000", "role": "worker", "keepalive": True,
         "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
         "recipe": "~/r/v4.yaml",
         "nodes": ["10.0.0.247", "10.0.0.248"], "port": 8000, "tp": 2, "pp": 1},
    ],
}


# read_cluster_json ────────────────────────────────────────────────────────────

def test_read_cluster_json_all_hosts(tmp_path, monkeypatch):
    f = tmp_path / "cluster.json"
    f.write_text(json.dumps(CLUSTER_LIVE))
    monkeypatch.setattr(cmdlib, "CLUSTER_JSON", str(f))
    hosts = cmdlib.read_cluster_json()
    ips = [h["ip"] for h in hosts]
    assert ips == ["10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
    assert hosts[0]["name"] == "GX10 Gateway"
    assert hosts[0]["id"] == "gx10-gateway"


def test_read_cluster_json_excludes_nas(tmp_path, monkeypatch):
    d = dict(CLUSTER_LIVE)
    d["nasDevices"] = [{"ip": "10.0.0.249", "name": "nas"}]
    f = tmp_path / "cluster.json"
    f.write_text(json.dumps(d))
    monkeypatch.setattr(cmdlib, "CLUSTER_JSON", str(f))
    hosts = cmdlib.read_cluster_json()
    ips = [h["ip"] for h in hosts]
    assert ips == ["10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
    assert not any(h["ip"] == "10.0.0.249" for h in hosts)


def test_read_cluster_json_empty_on_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cmdlib, "CLUSTER_JSON", str(tmp_path / "nope.json"))
    assert cmdlib.read_cluster_json() == []


def test_read_cluster_json_empty_on_corrupt(tmp_path, monkeypatch):
    f = tmp_path / "cluster.json"
    f.write_text("{ not json")
    monkeypatch.setattr(cmdlib, "CLUSTER_JSON", str(f))
    assert cmdlib.read_cluster_json() == []


# serving_unit_scoreboard ──────────────────────────────────────────────────────

def test_serving_unit_scoreboard_marks_tp_peers(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    score = cmdlib.serving_unit_scoreboard()
    assert sorted(score) == ["10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
    assert score["10.0.0.246"]["tp_peer"] is True
    assert score["10.0.0.248"]["tp_peer"] is True
    assert score["10.0.0.244"]["primary_of_unit"] is True
    assert score["10.0.0.247"]["primary_of_unit"] is True
    assert score["10.0.0.246"]["primary_of_unit"] is False
    assert score["10.0.0.244"]["role"] == "orchestrator"
    assert score["10.0.0.247"]["role"] == "worker"


def test_scoreboard_primary_dominates_peer(monkeypatch):
    """A node primary in one unit and peer in another stays primary."""
    units = [
        {"id": "u1", "role": "worker", "model": "m",
         "nodes": ["10.0.0.2", "10.0.0.3"], "tp": 2},       # 10.0.0.3 is peer
        {"id": "u2", "role": "worker", "model": "other",
         "nodes": ["10.0.0.3", "10.0.0.4"], "tp": 2},       # 10.0.0.3 primary here
    ]
    monkeypatch.setattr(cmdlib, "read_units", lambda: units)
    score = cmdlib.serving_unit_scoreboard()
    assert score["10.0.0.3"]["primary_of_unit"] is True
    assert score["10.0.0.3"]["tp_peer"] is False
    assert score["10.0.0.3"]["model"] == "other"            # dominant unit's lineage


def test_scoreboard_tp1_single_node_not_peer(monkeypatch):
    monkeypatch.setattr(cmdlib, "read_units",
                        lambda: [{"id": "w1", "role": "worker", "model": "m",
                                  "nodes": ["10.0.0.2"], "tp": 1}])
    score = cmdlib.serving_unit_scoreboard()
    s = score["10.0.0.2"]
    assert s["tp_peer"] is False and s["primary_of_unit"] is True


# classify_node ────────────────────────────────────────────────────────────────

def test_classify_serving_when_probe_returns_model():
    res = cmdlib.classify_node("10.0.0.1", {}, probe_fn=lambda ip: "Qwen/27B")
    assert res["state"] == "serving" and res["model"] == "Qwen/27B"


def test_classify_tp_peer_never_down():
    """tp_peer honored even when probe returns None — a tp peer is never down."""
    score = {"10.0.0.6": {"tp_peer": True, "primary_of_unit": False}}
    res = cmdlib.classify_node("10.0.0.6", score,
                               probe_fn=lambda ip: None,          # no model at :8000
                               reachability_fn=lambda ip: False)  # unreachable too
    assert res["state"] == "tp_peer"


def test_classify_idle_when_reachable_probe_none():
    res = cmdlib.classify_node("10.0.0.7", {}, probe_fn=lambda ip: None,
                               reachability_fn=lambda ip: True)
    assert res["state"] == "idle"


def test_classify_unreachable_when_probe_fails():
    res = cmdlib.classify_node("10.0.0.8", {}, probe_fn=lambda ip: None,
                               reachability_fn=lambda ip: False)
    assert res["state"] == "unreachable"


# enumerate_cluster_nodes ──────────────────────────────────────────────────────

def test_enumerate_cluster_nodes_all_present(monkeypatch):
    """probe serves only primaries → states [serving, tp_peer, serving, tp_peer]."""
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: [
        {"ip": "10.0.0.244", "name": "GX10 Gateway", "id": "gx10-gateway",
         "role": "gateway"},
        {"ip": "10.0.0.246", "name": "GX10 #1", "id": "gx10-worker-1",
         "role": "worker"},
        {"ip": "10.0.0.247", "name": "GX10 #2", "id": "gx10-worker-2",
         "role": "worker"},
        {"ip": "10.0.0.248", "name": "GX10 #3", "id": "gx10-worker-3",
         "role": "worker"},
    ])
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])

    def probe(ip):
        return "deepseek-ai/DeepSeek-V4-Flash-0731" if ip in ("10.0.0.244", "10.0.0.247") else None

    nodes = cmdlib.enumerate_cluster_nodes(probe_fn=probe,
                                           reachability_fn=lambda ip: True)
    ips = [n["ip"] for n in nodes]
    assert ips == ["10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
    assert [n["state"] for n in nodes] == ["serving", "tp_peer", "serving", "tp_peer"]
    assert nodes[0]["model"] == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert nodes[1]["model"] is None
    assert nodes[0]["name"] == "GX10 Gateway"
    assert nodes[1]["role"] == "worker"


def test_enumerate_falls_back_to_serving_nodes(monkeypatch):
    """Empty cluster.json → fall back to serving.json unit nodes, classified."""
    monkeypatch.setattr(cmdlib, "read_cluster_json", lambda: [])
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    nodes = cmdlib.enumerate_cluster_nodes(probe_fn=lambda ip: None,
                                           reachability_fn=lambda ip: True)
    ips = [n["ip"] for n in nodes]
    assert sorted(ips) == ["10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
    assert [n["state"] for n in nodes] == ["idle", "tp_peer", "idle", "tp_peer"]


def test_enumerate_excludes_nas_from_node_list(tmp_path, monkeypatch):
    """nasDevices in cluster.json must NOT surface as a failed node (no NAS row)."""
    d = dict(CLUSTER_LIVE)
    d["nasDevices"] = [{"ip": "10.0.0.249", "name": "nas"}]
    f = tmp_path / "cluster.json"
    f.write_text(json.dumps(d))
    monkeypatch.setattr(cmdlib, "CLUSTER_JSON", str(f))
    monkeypatch.setattr(cmdlib, "read_units", lambda: SERVING_LIVE["units"])
    nodes = cmdlib.enumerate_cluster_nodes(probe_fn=lambda ip: None,
                                           reachability_fn=lambda ip: True)
    ips = [n["ip"] for n in nodes]
    assert ips == ["10.0.0.244", "10.0.0.246", "10.0.0.247", "10.0.0.248"]
    assert "10.0.0.249" not in ips           # NAS never rendered as a node
    assert [n["state"] for n in nodes] == ["idle", "tp_peer", "idle", "tp_peer"]
