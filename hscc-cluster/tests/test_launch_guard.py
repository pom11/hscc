"""A2 launch guard on the `hscc cluster up` path (HEAL_CASCADE 2026-10-14).

cmd_cluster_up must check every plan entry against sparkrun status BEFORE
issuing its `sparkrun run`, refuse a unit whose host holds a vLLM outside the
declared span, and REPORT the refusal in its result (never silently drop the
unit — the incident's lesson is that silent skips hide the failure). Dry-run
is untouched: nothing is launched, so nothing can over-commit.

Policy + alerts are tested where they live (hscc_daemon/tests/test_launch_
guard.py). This file pins only the CLI seam, against the REAL guard: the CLI
is patched only at its two injection points (command sink, serving module)
plus the health module's alert sinks, exactly as the daemon's own conftest
does — so the real launch_guard.evaluate runs on every assertion here.
"""

import importlib.util
import json
import os
import sys

import pytest

# The daemon package lives at the repo root; hscc-cluster's conftest only
# puts the plugin dir on sys.path. Add the root so `import hscc_daemon` works
# from this leg too (the CLI's own _load_daemon_serving does the same).
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hscc_daemon import daemon_ops, health, serving  # noqa: E402

HOST_ORCH = "192.0.2.10"
HOST_WK1 = "192.0.2.11"
HOST_TP2 = "192.0.2.12"


def _solo(host, port, recipe="~/r/foreign-vllm.yaml"):
    return {"cluster_id": "sparkrun_x", "host": host, "status": "Up",
            "image": "img", "meta": {"port": port, "recipe": recipe}}


def _status(poisoned):
    """sparkrun-shaped status where `poisoned` hosts hold a vLLM on :9100
    (a port NO serving.json declares)."""
    return {"groups": {}, "solo_entries": [_solo(h, 9100) for h in poisoned],
            "errors": {}, "idle_hosts": [], "preparing_hosts": [],
            "pending_ops": [], "total_containers": len(poisoned),
            "host_count": 3}


def _units(one=False, multi=False):
    units = [
        {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
         "port": 8000, "recipe": "~/recipes/orch.yaml", "tp": 1},
        {"id": "wk1", "role": "worker", "nodes": [HOST_WK1],
         "port": 8001, "recipe": "~/recipes/wk1.yaml", "tp": 1},
    ]
    if multi:
        units = [{"id": "tp", "role": "worker",
                  "nodes": [HOST_WK1, HOST_TP2], "port": 8001,
                  "recipe": "~/recipes/tp.yaml", "tp": 2}]
    return units


class _FakeServing:
    """Stand-in for the serving module as the CLI uses it: planner only.
    fleet_up_plan mirrors the real builder's entry shape."""

    def __init__(self, units):
        self._units = units

    def load_serving(self):
        return {"port": 8000, "units": self._units}

    def fleet_up_plan(self, serving=None):
        return [{"kind": u["role"], "nodes": list(u["nodes"]),
                 "port": u["port"], "unit_id": u["id"], "keepalive": False,
                 "cmd": ["sparkrun", "run", u["recipe"],
                         "--hosts", ",".join(u["nodes"]),
                         "--port", str(u["port"])]}
                for u in self._units]


def _load_cli(monkeypatch, units, poisoned_hosts):
    """Fresh CLI module + real guard wired to fakes. Returns
    (cli_mod, issued, alerts, checks)."""
    path = os.path.join(_REPO_ROOT, "hscc-cluster", "hscc.py")
    spec = importlib.util.spec_from_file_location("hscc_cluster_cli_a2", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    issued = []
    monkeypatch.setattr(
        mod, "run_cmd",
        lambda cmd, timeout=30, as_json=False: issued.append(list(cmd)) or
        {"success": True, "ok": True, "output": "", "returncode": 0})
    monkeypatch.setattr(mod, "_load_daemon_serving",
                        lambda: _FakeServing(units))

    # Guard inputs/outputs: fake sparkrun status + hermetic alert/log sinks
    # (mirrors the daemon conftest; these tests must never write the real
    # ~/.hscc or fire real notifications).
    monkeypatch.setattr(health, "_launch_guard_status_fn",
                        lambda force=False: _status(poisoned_hosts))
    monkeypatch.setattr(serving, "load_serving",
                        lambda: {"port": 8000, "units": units})
    alerts, checks = [], []
    monkeypatch.setattr(health, "send_macos_notification",
                        lambda *a, **k: alerts.append(("note", a)) or True)
    monkeypatch.setattr(health, "emit_event",
                        lambda t, p, **k: alerts.append(("event", t, p)) or {})
    monkeypatch.setattr(health, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(health, "_LAUNCH_ALERT_AT", {})

    real_guard = health._guard_launch_or_refuse

    def spy_guard(host, port, span, actor, unit, status=None, declared=None):
        checks.append((host, port, actor, unit))
        return real_guard(host, port, span, actor, unit, status=status,
                          declared=declared)
    monkeypatch.setattr(health, "_guard_launch_or_refuse", spy_guard)
    return mod, issued, alerts, checks


class TestClusterUpGuard:
    def test_clean_status_starts_every_unit(self, monkeypatch):
        """Positive control: nothing foreign anywhere => both starts issued,
        both hosts consulted first. Proves the guard is not a blanket refusal."""
        mod, issued, alerts, checks = _load_cli(monkeypatch, _units(), [])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is True and res["units"] == 2
        assert len(issued) == 2
        assert {h for h, _, _, _ in checks} == {HOST_ORCH, HOST_WK1}
        assert alerts == []

    def test_refused_unit_is_reported_not_silently_dropped(self, monkeypatch):
        """A foreign vLLM on wk1's host => wk1's start is NEVER issued, the
        result says so explicitly, and the alert fired. The clean orch unit
        still starts (per-unit refusal, not all-or-nothing)."""
        mod, issued, alerts, checks = _load_cli(monkeypatch, _units(),
                                                [HOST_WK1])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is False, "a refused unit must fail the up"
        refused = [i for i in res["issued"] if i.get("refused")]
        assert len(refused) == 1 and "wk1" in refused[0]["output"]
        assert len(issued) == 1 and HOST_ORCH in " ".join(issued[0])
        assert all(HOST_WK1 not in " ".join(c) for c in issued)
        events = [a for a in alerts if a[0] == "event"]
        assert any(a[1] == "launch_guard.refused" for a in events), alerts

    def test_dry_run_never_consults_the_guard(self, monkeypatch):
        """dry-run launches nothing => nothing to over-commit, and it must not
        depend on status availability (a dry-run during a status outage still
        prints the plan)."""
        mod, issued, alerts, checks = _load_cli(monkeypatch, _units(),
                                                [HOST_WK1])
        res = mod.cmd_cluster_up(dry_run=True)
        assert res["dry_run"] is True
        assert checks == [] and issued == []

    def test_refusal_on_any_span_host_refuses_the_whole_unit(self, monkeypatch):
        """A TP unit lands on ALL span hosts; a foreign workload on the peer
        (not the host being launched) refuses the whole launch — the group
        would over-commit the peer's GPU either way."""
        mod, issued, alerts, checks = _load_cli(monkeypatch,
                                                _units(multi=True),
                                                [HOST_TP2])
        res = mod.cmd_cluster_up(dry_run=False)
        assert HOST_TP2 in [h for h, _, _, _ in checks]
        assert any(i.get("refused") for i in res["issued"])
        assert issued == []                   # span refused as a whole

    def test_declared_colocation_does_not_block_the_up(self, monkeypatch):
        """A workload at a DECLARED (host, port) is co-location the operator
        chose (a gateway + embed pair on one host). cluster up must still
        issue the start — refusing declared workloads would deadlock the CLI
        against a working deployment."""
        units = _units()
        # wk1 declares 192.0.2.11:8001; status shows exactly that running.
        mod, issued, alerts, checks = _load_cli(monkeypatch, units, [])
        # Override the fake status: live wk1 at its OWN declared port.
        monkeypatch.setattr(health, "_launch_guard_status_fn",
                            lambda force=False: {
                                "groups": {}, "errors": {},
                                "solo_entries": [_solo(HOST_WK1, 8001)]})
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is True and len(issued) == 2
