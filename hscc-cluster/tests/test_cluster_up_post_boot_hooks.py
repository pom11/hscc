"""t_576964d5 — the fleet verbs' post-boot expected_up marker hooks.

Pins, on the CLI side (the daemon-side adjudication is pinned in
hscc_daemon/tests/test_post_boot.py):

1. a REAL `cluster up` records the FULL fleet-up footprint as the expected_up
   marker even when the launch itself is scoped with nodes= (the verb is the
   operator claiming "I want the fleet running");
2. a scoped `cluster up(nodes=...)` launches ONLY the units whose span
   touches a named host — the exact scoping the post-boot relaunch path
   relies on instead of a second start surface;
3. `cluster up --dry-run` writes NOTHING (no sparkrun, no marker);
4. a real `cluster down` clears the marker — even when the stop itself
   failed (fail-safe direction: a deliberate down means "stay down");
5. `cluster down --dry-run` leaves the marker untouched.

The marker file itself is redirected to the per-test tmp dir by the suite's
autouse `_pin_expected_up_marker_to_tmp` fixture (defense-in-depth against
the live ~/.hscc — the t_501fb7f1 doctrine).

Address doctrine: sanctioned 10.0.0.x placeholder range only.
"""

import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hscc_daemon import post_boot  # noqa: E402

from test_cluster_up_launch import (  # noqa: E402
    HOST_ORCH, HOST_WK1, HOST_WK2, _load_cli, _units,
)


@pytest.fixture
def marker_path(tmp_path, monkeypatch):
    """The autouse fixture already redirects EXPECTED_UP_FILE to a per-test
    tmp path; capture the exact path for assertions."""
    return post_boot.EXPECTED_UP_FILE


class TestUpRecordsMarker:
    def test_real_up_records_full_footprint(self, monkeypatch, marker_path):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is True
        m = json.load(open(marker_path))
        ids = {u["unit_id"] for u in m["units"]}
        assert ids == {"orch", "wk1", "wk2"}          # the FULL footprint
        assert m["reason"] == "hscc cluster up"
        orch = [u for u in m["units"] if u["unit_id"] == "orch"][0]
        assert orch["nodes"] == [HOST_ORCH] and orch["port"] == 8000

    def test_scoped_up_records_full_footprint_too(self, monkeypatch,
                                                  marker_path):
        """nodes= scopes the LAUNCH, never the claim of what should run."""
        mod, calls, _ = _load_cli(monkeypatch, _units())
        mod.cmd_cluster_up(dry_run=False, nodes=[HOST_WK1])
        m = json.load(open(marker_path))
        assert {u["unit_id"] for u in m["units"]} == {"orch", "wk1", "wk2"}
        # but the launches were scoped to wk1's host alone
        assert all(HOST_ORCH not in " ".join(c) for c in calls)
        assert all(HOST_WK2 not in " ".join(c) for c in calls)
        assert any(HOST_WK1 in " ".join(c) for c in calls)

    def test_scoped_up_touching_host_matches(self, monkeypatch):
        """nodes= matches by SPAN membership (a TP unit would launch whole)."""
        units = _units() + [{"id": "tp", "role": "worker",
                             "nodes": [HOST_WK1, HOST_WK2],
                             "port": 8002, "recipe": "~/recipes/tp.yaml",
                             "tp": 2}]
        mod, calls, _ = _load_cli(monkeypatch, units)
        mod.cmd_cluster_up(dry_run=False, nodes=[HOST_WK2])
        joined = " ".join(" ".join(c) for c in calls)
        assert HOST_WK2 in joined          # its own host
        assert HOST_WK1 in joined          # the co-span member (whole group)
        assert HOST_ORCH not in joined     # untouched host never launched

    def test_dry_run_writes_no_marker_and_launches_nothing(
            self, monkeypatch, marker_path):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        mod.cmd_cluster_up(dry_run=True)
        assert calls == []
        assert not os.path.exists(marker_path)

    def test_marker_record_failure_never_breaks_the_up(self, monkeypatch,
                                                       marker_path):
        """The hook is fail-soft: a marker that cannot be written prints a
        WARN and the launches still proceed."""
        mod, calls, _ = _load_cli(monkeypatch, _units())

        def boom(plan, reason, path=None):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(post_boot, "record_expected_up", boom)
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is True
        assert calls                                    # launches happened


class TestDownClearsMarker:
    def _wire_down(self, monkeypatch):
        import importlib.util
        path = os.path.join(_REPO_ROOT, "hscc-cluster", "hscc.py")
        spec = importlib.util.spec_from_file_location(
            "hscc_cluster_cli_down_hooks", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        calls = []

        def sink(cmd, timeout=30, as_json=False):
            calls.append(list(cmd))
            return {"success": True, "returncode": 0, "output": "stopped",
                    "error": ""}
        monkeypatch.setattr(mod, "run_cmd", sink)
        fake = type("S", (), {"fleet_down_cmd": staticmethod(
            lambda: ["sparkrun", "stop", "--all", "--cluster", "hscc"])})
        monkeypatch.setattr(mod, "_load_daemon_serving", lambda: fake)
        monkeypatch.setattr(post_boot, "log", lambda *a, **k: None)
        return mod, calls

    def test_real_down_clears(self, monkeypatch, marker_path):
        mod, calls = self._wire_down(monkeypatch)
        post_boot.record_expected_up(
            [{"kind": "orchestrator", "nodes": [HOST_ORCH], "port": 8000,
              "unit_id": "orch"}], "hscc cluster up", path=marker_path)
        mod.cmd_cluster_down(dry_run=False)
        assert json.load(open(marker_path))["units"] == []
        assert calls                                    # stop was issued

    def test_down_clears_even_when_stop_fails(self, monkeypatch, marker_path):
        """Fail-safe direction: the operator's deliberate `cluster down`
        means stay-down even if sparkrun's stop failed."""
        mod, calls = self._wire_down(monkeypatch)

        def failing_sink(cmd, timeout=30, as_json=False):
            calls.append(list(cmd))
            return {"success": False, "returncode": 1, "output": "",
                    "error": "sparkrun died"}
        monkeypatch.setattr(mod, "run_cmd", failing_sink)
        post_boot.record_expected_up(
            [{"kind": "worker", "nodes": [HOST_WK1], "port": 8001,
              "unit_id": "wk1"}], "hscc cluster up", path=marker_path)
        mod.cmd_cluster_down(dry_run=False)
        assert json.load(open(marker_path))["units"] == []

    def test_dry_run_down_keeps_marker(self, monkeypatch, marker_path):
        mod, calls = self._wire_down(monkeypatch)
        post_boot.record_expected_up(
            [{"kind": "worker", "nodes": [HOST_WK1], "port": 8001,
              "unit_id": "wk1"}], "hscc cluster up", path=marker_path)
        mod.cmd_cluster_down(dry_run=True)
        assert calls == []
        assert json.load(open(marker_path))["units"], "dry-run must not clear"

    def test_marker_clear_failure_never_breaks_the_down(self, monkeypatch,
                                                        marker_path):
        mod, calls = self._wire_down(monkeypatch)

        def boom(reason, path=None):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(post_boot, "clear_expected_up", boom)
        res = mod.cmd_cluster_down(dry_run=False)
        assert res["success"] is True
        assert calls
