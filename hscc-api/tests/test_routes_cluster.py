"""Unit tests for hscc-api Phase A2: cluster + fleet READ endpoints.

Hermetic: all backing engine calls are monkeypatched (``routes_cluster``'s
``_backing_*`` seam) so no test hits real SSH/sparkrun/GPU nodes or depends on
a live cluster. Servers bind loopback port 0 (ephemeral); the suite runs in
isolation via scripts/run_tests.sh.

Covers, per endpoint: 200 + expected shape + a non-empty ``speak`` derived
from the stubbed data; auth still enforced (401 without a token); and a
backing-engine failure degrades gracefully (200 with a sane ``speak``, no
traceback leak). The pure ``_speak_*`` helpers are also tested directly.
"""

import json

import pytest

import api_server
import routes_cluster
from test_api import RunningServer  # reuse A1's hermetic server helper


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _request(running, token, path):
    """GET a path on the live server, returning (status, payload)."""
    return running.request(token=token, path=path)


# ---------------------------------------------------------------------------
# Fixtures: stub the backing calls with controlled data
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_cluster(monkeypatch):
    """Stub all five cluster-engine backing calls with plain dicts."""

    def install(status=None, hosts=None, monitor=None, jobs=None, info=None,
                serving=None):
        monkeypatch.setattr(routes_cluster, "_backing_cluster_status", lambda: status)
        monkeypatch.setattr(routes_cluster, "_backing_cluster_hosts", lambda: hosts)
        monkeypatch.setattr(routes_cluster, "_backing_cluster_monitor", lambda: monitor)
        monkeypatch.setattr(routes_cluster, "_backing_cluster_jobs", lambda: jobs)
        monkeypatch.setattr(routes_cluster, "_backing_cluster_info", lambda: info)
        monkeypatch.setattr(
            routes_cluster, "_backing_serving_snapshot",
            lambda: serving if serving is not None
            else {"rows": [], "proxy_error": None})

    return install


@pytest.fixture
def stub_fleet(monkeypatch):
    """Stub the verify/stats/throughput/streams/autoscale backing calls."""

    def install(verify=None, stats=None, throughput=None, streams=None, autoscale=None):
        monkeypatch.setattr(routes_cluster, "_backing_verify", lambda: verify)
        monkeypatch.setattr(routes_cluster, "_backing_stats", lambda days: stats)
        monkeypatch.setattr(routes_cluster, "_backing_throughput", lambda: throughput)
        monkeypatch.setattr(routes_cluster, "_backing_streams", lambda: streams)
        monkeypatch.setattr(routes_cluster, "_backing_autoscale", lambda: autoscale)

    return install


# ---------------------------------------------------------------------------
# Cluster endpoints
# ---------------------------------------------------------------------------

def test_cluster_status_shape(running, token, stub_cluster):
    stub_cluster(status={
        "workloads": [{"name": "qwen", "tp": "1", "pp": "1", "container_id": "abc"}],
        "idle_hosts": ["192.0.2.11"],
        "total_hosts": 4,
        "raw_output": "...",
    })
    status, payload = _request(running, token, "/v1/cluster/status")
    assert status == 200
    assert set(payload) == {"workloads", "idle_hosts", "total_hosts", "speak"}
    assert len(payload["workloads"]) == 1
    assert payload["workloads"][0]["name"] == "qwen"
    assert payload["idle_hosts"] == ["192.0.2.11"]
    assert payload["total_hosts"] == 4
    assert payload["speak"] == "4 hosts up. 1 workload running, 1 idle."
    assert isinstance(payload["speak"], str) and payload["speak"]


def test_cluster_status_workload_nodes_passthrough(running, token, stub_cluster):
    """t_db6a32da: the per-workload `nodes` list (unit→node edge) is forwarded
    verbatim like the other workload keys — `handle_cluster_status` must not
    whitelist its way into dropping it. A tp=2 job legitimately lists BOTH
    nodes; speak must not change because of the new key."""
    stub_cluster(status={
        "workloads": [{"name": "ds4", "tp": "2", "pp": "1",
                       "container_id": "cid_1",
                       "nodes": ["192.0.2.244", "192.0.2.246"]}],
        "idle_hosts": [],
        "total_hosts": 2,
        "raw_output": "...",
    })
    status, payload = _request(running, token, "/v1/cluster/status")
    assert status == 200
    assert payload["workloads"][0]["nodes"] == ["192.0.2.244", "192.0.2.246"]
    assert payload["speak"] == "2 hosts up. 1 workload running, 0 idle."


def test_cluster_status_nodes_absent_still_decodes(running, token, stub_cluster):
    """Back-compat half: a pre-field engine (no `nodes` anywhere) yields the
    exact pre-change payload — clients must treat the key as optional."""
    stub_cluster(status={
        "workloads": [{"name": "qwen", "tp": "1", "pp": "1", "container_id": "abc"}],
        "idle_hosts": [],
        "total_hosts": 1,
        "raw_output": "...",
    })
    status, payload = _request(running, token, "/v1/cluster/status")
    assert status == 200
    assert "nodes" not in payload["workloads"][0]
    assert payload["workloads"][0] == {"name": "qwen", "tp": "1", "pp": "1",
                                       "container_id": "abc"}


def test_cluster_status_failure_names_the_cause(running, token, stub_cluster):
    stub_cluster(status={"error": "sparkrun status exited 1: ssh unreachable"})
    status, payload = _request(running, token, "/v1/cluster/status")
    assert status == 200
    assert payload["error"] == "sparkrun status exited 1: ssh unreachable"
    assert payload["speak"] == \
        "Cluster status failed: sparkrun status exited 1: ssh unreachable"
    # Degraded payload keeps the widget-safe shape.
    assert payload["workloads"] == []
    assert payload["total_hosts"] == 0


def test_cluster_status_failure_on_exception(running, token, stub_cluster, monkeypatch):
    stub_cluster(status={"error": "x"})
    monkeypatch.setattr(routes_cluster, "_backing_cluster_status", lambda: (_ for _ in ()).throw(RuntimeError("secret ssh failure")))
    status, payload = _request(running, token, "/v1/cluster/status")
    assert status == 200
    assert payload["error"] == "RuntimeError: secret ssh failure"
    assert payload["speak"].startswith("Cluster status failed: RuntimeError:")
    assert "traceback" not in json.dumps(payload).lower()


def test_cluster_hosts_shape(running, token, stub_cluster):
    stub_cluster(hosts={
        "hosts": [{"id": "1", "name": "gw", "ip": "192.0.2.244", "role": "gateway"}],
        "saved_clusters": [{"name": "hscc"}],
        "live_status": {"success": True, "returncode": 0, "output": "Job: x"},
    })
    status, payload = _request(running, token, "/v1/cluster/hosts")
    assert status == 200
    assert set(payload) == {"hosts", "saved_clusters", "live_status", "speak"}
    assert len(payload["hosts"]) == 1
    assert payload["speak"] == "1 hosts registered. 1 cluster saved."


def test_cluster_hosts_unavailable(running, token, stub_cluster):
    stub_cluster(hosts=None)
    status, payload = _request(running, token, "/v1/cluster/hosts")
    assert status == 200
    assert payload["speak"].startswith("Host list failed:")
    assert payload["error"]


def test_cluster_monitor_shape(running, token, stub_cluster):
    stub_cluster(
        monitor={"success": True, "output": "{}", "json": {"nodes": [1, 2, 3]}},
        serving={"rows": [{"node": "192.0.2.10", "port": "8000",
                           "model": "m", "running": 1}],
                 "proxy_error": None},
    )
    status, payload = _request(running, token, "/v1/cluster/monitor")
    assert status == 200
    assert payload["success"] is True
    assert payload["speak"] == "Fleet snapshot: 3 hosts sampled."
    # The serving section is the same one `hscc cluster monitor` renders.
    assert payload["serving"][0]["model"] == "m"
    assert payload["serving_proxy_error"] is None


def test_cluster_monitor_failure_names_the_cause(running, token, stub_cluster):
    stub_cluster(monitor={"success": False, "error": "no monitor output"})
    status, payload = _request(running, token, "/v1/cluster/monitor")
    assert status == 200
    assert payload["error"] == "no monitor output"
    assert payload["speak"] == "Fleet monitor failed: no monitor output"


def test_cluster_monitor_run_cmd_error_dict_degrades(running, token, stub_cluster):
    """A plain {"error": "..."} (not the run_cmd shape) reports the cause."""
    stub_cluster(monitor={"error": "cluster engine command failed"})
    status, payload = _request(running, token, "/v1/cluster/monitor")
    assert status == 200
    assert payload["error"] == "cluster engine command failed"
    assert payload["speak"] == \
        "Fleet monitor failed: cluster engine command failed"


def test_cluster_jobs_shape(running, token, stub_cluster):
    stub_cluster(jobs={"success": True, "returncode": 0, "output": "Job: a\nJob: b\nTotal: 2 job(s) across 2 host(s)"})
    status, payload = _request(running, token, "/v1/cluster/jobs")
    assert status == 200
    assert payload["success"] is True
    assert payload["speak"] == "2 jobs running."


def test_cluster_jobs_unavailable(running, token, stub_cluster):
    stub_cluster(jobs={"error": "cluster engine command failed"})
    status, payload = _request(running, token, "/v1/cluster/jobs")
    assert status == 200
    assert payload["error"] == "cluster engine command failed"
    assert payload["speak"] == \
        "Job list failed: cluster engine command failed"


def test_cluster_jobs_run_cmd_error_dict_degrades(running, token, stub_cluster):
    """A run_cmd-shaped error (success False, no json) reports the cause."""
    stub_cluster(jobs={"success": False, "error": "no job output"})
    status, payload = _request(running, token, "/v1/cluster/jobs")
    assert status == 200
    assert payload["error"] == "no job output"
    assert payload["speak"] == "Job list failed: no job output"


def test_cluster_info_shape(running, token, stub_cluster):
    stub_cluster(info={"cluster_config": {"name": "hscc"}, "default_cluster": {}, "cluster_files": {}})
    status, payload = _request(running, token, "/v1/cluster/info")
    assert status == 200
    assert payload["cluster_config"]["name"] == "hscc"
    assert payload["speak"] == "Cluster configuration loaded."


def test_cluster_info_unavailable(running, token, stub_cluster):
    stub_cluster(info={"error": "boom"})
    status, payload = _request(running, token, "/v1/cluster/info")
    assert status == 200
    assert payload["error"] == "boom"
    assert payload["speak"] == "Cluster info failed: boom"


# ---------------------------------------------------------------------------
# Fleet endpoints
# ---------------------------------------------------------------------------

def test_health_ok_shape(running, token, stub_fleet):
    stub_fleet(verify={
        "ok": True,
        "checks": [
            {"name": "plugins", "ok": True, "detail": "ok"},
            {"name": "multiplex", "ok": True, "detail": "ok"},
            {"name": "streams", "ok": True, "detail": "ok"},
            {"name": "proxy", "ok": True, "detail": "ok"},
            {"name": "config_wiring", "ok": True, "detail": "ok"},
        ],
    })
    status, payload = _request(running, token, "/v1/health")
    assert status == 200
    assert payload["ok"] is True
    assert len(payload["checks"]) == 5
    assert payload["speak"] == "All checks passed."
    assert isinstance(payload["speak"], str) and payload["speak"]
    # t_7babf634: this test's tmp dir has never held a snapshot and the
    # in-process cache is reset per test, so the stubbed verify must be the
    # FIRST (and only) one — the inline path. Every later poll in the same
    # process would be answered from the cache instead; test_health_cache.py
    # pins that half.
    assert payload["source"] == "inline"


def test_health_partial_shape(running, token, stub_fleet):
    stub_fleet(verify={
        "ok": False,
        "checks": [
            {"name": "plugins", "ok": True, "detail": "ok"},
            {"name": "multiplex", "ok": False, "detail": "bad"},
            {"name": "streams", "ok": True, "detail": "ok"},
        ],
    })
    status, payload = _request(running, token, "/v1/health")
    assert status == 200
    assert payload["ok"] is False
    assert payload["speak"].startswith("1 of 3 checks have problems")
    assert "multiplex" in payload["speak"]


def test_health_unavailable(running, token, stub_fleet):
    stub_fleet(verify=None)
    status, payload = _request(running, token, "/v1/health")
    assert status == 200
    assert payload["speak"] == "health check unavailable"


def test_fleet_stats_shape(running, token, stub_fleet):
    stub_fleet(stats={
        "since_days": 7,
        "completions": {"total": 1234, "by_profile": {}, "by_day": {}},
        "activity": {"tool_calls_by_profile": {}, "top_tools": []},
    })
    status, payload = _request(running, token, "/v1/fleet/stats")
    assert status == 200
    assert payload["since_days"] == 7
    assert payload["completions"]["total"] == 1234
    assert payload["speak"] == "About 1234 work items across the last 7 days."


def test_fleet_stats_days_query(running, token, monkeypatch):
    seen = {}
    def capture(days):
        seen["days"] = days
        return {"since_days": days, "completions": {"total": 5}}
    monkeypatch.setattr(routes_cluster, "_backing_stats", capture)
    status, payload = _request(running, token, "/v1/fleet/stats?days=14")
    assert status == 200
    assert seen["days"] == 14
    assert payload["speak"].endswith("last 14 days.")


def test_fleet_stats_negative_days_clamped(running, token, monkeypatch):
    seen = {}
    def capture(days):
        seen["days"] = days
        return {"since_days": days, "completions": {"total": 0}}
    monkeypatch.setattr(routes_cluster, "_backing_stats", capture)
    status, _ = _request(running, token, "/v1/fleet/stats?days=-3")
    assert status == 200
    assert seen["days"] == 0


def test_fleet_stats_invalid_days_defaults(running, token, monkeypatch):
    seen = {}
    def capture(days):
        seen["days"] = days
        return {"since_days": days, "completions": {"total": 0}}
    monkeypatch.setattr(routes_cluster, "_backing_stats", capture)
    status, _ = _request(running, token, "/v1/fleet/stats?days=abc")
    assert status == 200
    assert seen["days"] == 7


def test_fleet_throughput_shape(running, token, stub_fleet):
    stub_fleet(throughput={
        "by_node": {"http://n:8000/metrics": {"running": 2, "waiting": 1}},
        "fleet": {"nodes_ok": 3, "nodes_total": 4, "running": 2, "waiting": 1,
                  "prompt_tokens": 100, "generation_tokens": 200},
    })
    status, payload = _request(running, token, "/v1/fleet/throughput")
    assert status == 200
    assert payload["fleet"]["nodes_ok"] == 3
    assert payload["speak"] == "3 of 4 nodes healthy."


def test_engine_loader_falls_back_after_cellar_disappears(
        tmp_path, monkeypatch):
    """2026-11-18 root cause: the API process was launched from a Homebrew
    Cellar dir that an upgrade deleted. The sibling-only lookup then returned
    None forever. HSCC_CLUSTER_DIR (and the deployed-plugin/opt-link
    fallbacks) must still find an engine."""
    cluster_dir = tmp_path / "hscc-cluster"
    cluster_dir.mkdir()
    (cluster_dir / "hscc.py").write_text(
        "def cmd_cluster_status():\n"
        "    return {'workloads': [], 'idle_hosts': [], 'total_hosts': 1}\n")
    monkeypatch.setenv("HSCC_CLUSTER_DIR", str(cluster_dir))
    monkeypatch.setattr(routes_cluster, "_REPO_ROOT", tmp_path / "gone")
    eng = routes_cluster._load_cluster_engine()
    assert eng is not None
    assert eng.cmd_cluster_status()["total_hosts"] == 1


def test_engine_loader_failure_names_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setattr(routes_cluster, "_REPO_ROOT", tmp_path / "gone")
    monkeypatch.setattr(routes_cluster, "_cluster_engine_candidates",
                        lambda: [tmp_path / "gone" / "hscc-cluster"])
    assert routes_cluster._load_cluster_engine() is None
    assert "hscc-cluster engine not found" in routes_cluster._LAST_ENGINE_ERROR
    assert str(tmp_path / "gone" / "hscc-cluster") in \
        routes_cluster._LAST_ENGINE_ERROR


def test_fleet_throughput_unavailable(running, token, stub_fleet):
    stub_fleet(throughput=None)
    status, payload = _request(running, token, "/v1/fleet/throughput")
    assert status == 200
    assert payload["speak"].startswith("Fleet throughput failed:")
    assert payload["error"]


def test_fleet_streams_shape(running, token, stub_fleet):
    stub_fleet(streams={"dgx": {"ok": True, "timestamp": "t"}, "gateway": {"ok": True}})
    status, payload = _request(running, token, "/v1/fleet/streams")
    assert status == 200
    assert payload["streams"]["dgx"]["ok"] is True
    assert payload["speak"] == "Daemon streams: all ok."


def test_fleet_streams_blocked(running, token, stub_fleet):
    stub_fleet(streams={"dgx": {"ok": True}, "gateway": {"ok": False}, "proxy": {}})
    status, payload = _request(running, token, "/v1/fleet/streams")
    assert status == 200
    assert payload["speak"] == "Daemon streams: 2 blocked: gateway, proxy."


def test_fleet_streams_unavailable(running, token, stub_fleet):
    stub_fleet(streams=None)
    status, payload = _request(running, token, "/v1/fleet/streams")
    assert status == 200
    assert payload["speak"] == "Daemon streams: status unavailable."


def test_autoscale_shape(running, token, stub_fleet):
    stub_fleet(autoscale={"action": "scale_up", "target": 3, "reason": "queue depth 4 >= 4"})
    status, payload = _request(running, token, "/v1/autoscale")
    assert status == 200
    assert payload["action"] == "scale_up"
    assert payload["target"] == 3
    assert payload["speak"] == "Autoscale suggests scaling up to 3 workers."


def test_autoscale_none(running, token, stub_fleet):
    stub_fleet(autoscale={"action": "none", "reason": "within healthy band"})
    status, payload = _request(running, token, "/v1/autoscale")
    assert status == 200
    assert payload["speak"] == "Autoscale: nothing to change."


def test_autoscale_unavailable(running, token, stub_fleet):
    stub_fleet(autoscale=None)
    status, payload = _request(running, token, "/v1/autoscale")
    assert status == 200
    assert payload["speak"] == "Autoscale: no decision available."


# ---------------------------------------------------------------------------
# Auth enforcement across all new routes
# ---------------------------------------------------------------------------

def test_all_cluster_routes_require_auth(running):
    for path in ("/v1/cluster/status", "/v1/cluster/hosts", "/v1/cluster/monitor",
                 "/v1/cluster/jobs", "/v1/cluster/info", "/v1/health",
                 "/v1/fleet/stats", "/v1/fleet/throughput", "/v1/fleet/streams",
                 "/v1/autoscale"):
        status, payload = _request(running, None, path)
        assert status == 401, path
        assert payload["error"]["code"] == "unauthorized", path


@pytest.mark.parametrize("path", [
    "/v1/cluster/status", "/v1/cluster/hosts", "/v1/cluster/monitor",
    "/v1/cluster/jobs", "/v1/cluster/info", "/v1/health",
    "/v1/fleet/stats", "/v1/fleet/throughput", "/v1/fleet/streams",
    "/v1/autoscale",
])
def test_routes_are_get_only(running, token, path):
    """POST on a GET-only route -> 405 (design: reads are GET-only)."""
    status, payload = running.request(method="POST", path=path, token=token)
    assert status == 405, path
    assert payload["error"]["code"] == "method_not_allowed"


# ---------------------------------------------------------------------------
# Pure speak helpers (no I/O)
# ---------------------------------------------------------------------------

def test_speak_cluster_status_degrades_on_error():
    help1 = routes_cluster._speak_cluster_status({"total_hosts": 4, "workloads": [1], "idle_hosts": [1]})
    assert help1 == "4 hosts up. 1 workload running, 1 idle."


def test_speak_health_names_failures():
    data = {
        "ok": False,
        "checks": [
            {"name": "plugins", "ok": True},
            {"name": "proxy", "ok": False},
            {"name": "streams", "ok": False},
        ],
    }
    s = routes_cluster._speak_health(data)
    assert s.startswith("2 of 3 checks have problems")
    assert "proxy" in s and "streams" in s


def test_speak_stats_uses_total():
    s = routes_cluster._speak_stats({"since_days": 7, "completions": {"total": 42}})
    assert s == "About 42 work items across the last 7 days."
