"""Parity: every widget/Live-Activity cluster route == its CLI command.

Card t_8743f98c's rule is that a route may not re-implement a CLI read. These
tests execute BOTH sides on the SAME fixture state and compare the numbers/rows
the widget receives with what the CLI command returns:

  * ``hscc cluster status``   vs ``GET /v1/cluster/status``
  * ``hscc cluster monitor``  vs ``GET /v1/cluster/monitor``
  * ``hscc autodown status``  vs ``GET /v1/autodown/status``

The cluster fixture is a neutral 192.0.2.x sparkrun status/monitor payload; no
live host, tenant or token is read.
"""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

import api_server
import routes_cluster
from test_api import RunningServer

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


SPARKRUN_STATUS = """Cluster: hscc (default) — 2 host(s)

Job: qwen3.8-flash  (tp=1, pp=1)  [abc123_def456]  (1 container(s))
  solo       192.0.2.10 (ib: 192.0.2.110)       Up 9 days                 image-a
  logs: sparkrun logs abc123_def456
  stop: sparkrun stop abc123_def456

Job: qwen3.8-flash  (tp=1, pp=1)  [fed789_cba654]  (1 container(s))
  solo       192.0.2.11 (ib: 192.0.2.111)       Up 9 days                 image-a
  logs: sparkrun logs fed789_cba654
  stop: sparkrun stop fed789_cba654

Total: 2 container(s) across 2 host(s)
"""

MONITOR_FRAME = {
    "timestamp": 1795000000.0,
    "hosts": [
        {"host": "192.0.2.10", "error": None,
         "sample": {"hostname": "host-a", "cpu_usage_pct": "12.7",
                    "mem_used_pct": "85.0", "gpu_util_pct": "96",
                    "gpu_temp_c": "68", "gpu_power_w": "42.23"},
         "workloads": [{"workload_id": "sparkrun_abc123_def456",
                        "containers": [{"name": "qwen_solo",
                                        "status": "Up 9 days"}]}],
         "used_slots": 1, "free_slots": 0},
        {"host": "192.0.2.11", "error": "collect failed", "sample": None,
         "workloads": [], "used_slots": 0, "free_slots": 1},
    ],
}


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def engine(monkeypatch):
    """The real hscc-cluster engine, loaded once and used by BOTH sides."""
    eng = routes_cluster._load_cluster_engine()
    assert eng is not None
    monkeypatch.setattr(routes_cluster, "_load_cluster_engine", lambda: eng)
    monkeypatch.setattr(
        routes_cluster, "_backing_serving_snapshot",
        lambda: {"rows": [{"node": "192.0.2.10", "port": "8000",
                           "model": "qwen3.8-flash", "running": 2,
                           "waiting": 1, "kv": 0.17, "gen_tps": 12.5}],
                 "proxy_error": None})
    return eng


def _request(running, token, path):
    return running.request(token=token, path=path)


def test_cluster_status_api_matches_cli(engine, running, token, monkeypatch):
    monkeypatch.setattr(
        engine, "run_cmd",
        lambda args, timeout=30, as_json=False: {
            "success": True, "returncode": 0, "output": SPARKRUN_STATUS})

    cli = engine.cmd_cluster_status()
    status, api = _request(running, token, "/v1/cluster/status")
    assert status == 200
    assert api["workloads"] == cli["workloads"]
    assert api["idle_hosts"] == cli["idle_hosts"]
    assert api["total_hosts"] == cli["total_hosts"] == 2
    assert [w["container_id"] for w in api["workloads"]] == \
        ["abc123_def456", "fed789_cba654"]
    assert [w["nodes"] for w in api["workloads"]] == \
        [["192.0.2.10"], ["192.0.2.11"]]


def test_cluster_monitor_api_matches_cli(engine, running, token, monkeypatch):
    line = json.dumps(MONITOR_FRAME)
    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **k: types.SimpleNamespace(
            stdout=line, stderr="", returncode=0),
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(engine, "subprocess", fake_subprocess)

    cli = engine.cmd_monitor()
    status, api = _request(running, token, "/v1/cluster/monitor")
    assert status == 200
    assert api["success"] is True
    assert api["json"] == cli["json"]
    assert len(api["json"]["hosts"]) == len(cli["json"]["hosts"]) == 2
    # Every CLI host row (up + errored) is forwarded, and the serving section
    # is the same shape the CLI renderer consumes.
    assert [h["host"] for h in api["json"]["hosts"]] == \
        ["192.0.2.10", "192.0.2.11"]
    assert api["serving"][0]["model"] == "qwen3.8-flash"


def test_autodown_status_api_matches_cli(running, token, monkeypatch,
                                         capsys):
    from hscc_daemon import autodown, autodown_cli, lifecycle

    stale = {
        "enabled": False, "idle_minutes": 120, "state": "down",
        "last_activity_iso": "2026-11-05T19:27:52+00:00",
        "down_since": "2026-11-05T19:22:52+00:00",
        "wake_source": "cli", "reason": "autodown: intentional idle teardown",
        "force_armed": False, "force_armed_overrides": [],
    }
    monkeypatch.setattr(autodown, "load_config", lambda: dict(stale))
    monkeypatch.setattr(
        lifecycle, "load_watchdog_block",
        lambda: {"blocked": False, "intentional": None, "reason": ""})
    monkeypatch.setattr(
        autodown, "_status_interlock_context",
        lambda: {"blocked_by": None, "kanban_ok": True, "kanban_reason": "",
                 "prci_active": False, "prci_ok": True, "prci_reason": "",
                 "prci_signal": None, "active_cron_cpu_only": [],
                 "active_cron_model": []})
    # The fleet is UP despite the stale file: status must report reality.
    monkeypatch.setattr(autodown, "_serving_actually_up", lambda: True)

    rc = autodown_cli._cmd_status([], True)
    assert rc == 0
    cli = json.loads(capsys.readouterr().out)

    status, api = _request(running, token, "/v1/autodown/status")
    assert status == 200
    assert cli["state"] == api["state"] == "up"
    assert cli["state_recorded"] == api["state_recorded"] == "down"
    assert cli["down_since"] is None and api["down_since"] is None
    assert {k: v for k, v in api.items() if k != "speak"} == cli


def test_alerts_api_matches_notify_list(running, token, monkeypatch):
    """`GET /v1/alerts` is the feed `hscc notify list` presents, row for row.

    The route answers ascending by id; the CLI reverses that page so an operator
    reads newest first. The "Needs me" widget mirrors the same newest-first order
    and keeps only rows whose ``acked`` is false. This test pins the shared half:
    the route and the CLI must agree on the rows and their fields before any
    client-side filter can be trusted.
    """
    from hscc_daemon import alerts, notify_cli

    store = running.server.ctx.hscc_dir
    monkeypatch.setenv("HSCC_HOME", store)
    first = alerts.post_alert(title="Example one", body="Example body one",
                              level="info", key="fixture/one", hscc_dir=store)
    second = alerts.post_alert(title="Example two", body="Example body two",
                               level="action", key="fixture/two",
                               hscc_dir=store)
    third = alerts.post_alert(title="Example three", body="Example body three",
                              level="critical", key="fixture/three",
                              hscc_dir=store)
    alerts.ack_alert(second["alert"]["id"], hscc_dir=store)

    status, api = _request(running, token, "/v1/alerts?limit=100")
    assert status == 200
    assert [a["id"] for a in api["alerts"]] == [
        first["alert"]["id"], second["alert"]["id"], third["alert"]["id"],
    ]

    opts, error = notify_cli.parse_notify_args(["list", "--limit", "100"])
    assert error is None
    status, cli = notify_cli.list_alerts(
        {"api": f"http://{running.host}:{running.port}",
         "since": None, "limit": 100}, hscc_dir=store)
    assert status == 200
    # Newest first, exactly the order a "Needs me" tile reads.
    assert [a["id"] for a in cli["alerts"]] == [
        third["alert"]["id"], second["alert"]["id"], first["alert"]["id"],
    ]
    by_id = {a["id"]: a for a in api["alerts"]}
    for row in cli["alerts"]:
        assert row == by_id[row["id"]]
    assert by_id[second["alert"]["id"]]["acked"] is True
    assert by_id[first["alert"]["id"]]["acked"] is False
