"""t_be4b94ca — `hscc cluster up` launch hardening (TDD-first).

Three behaviors pinned here, all against fakes (no live host is contacted;
the /v1/models probe seam, the run_cmd seam and the guard-status seam are
each monkeypatched BEFORE the command can dial out):

1. per-unit LAUNCH-PHASE timeout — a `sparkrun run ... --no-follow --ensure`
   that never returns is cut off at a launch-scale budget (default 180s,
   env HSCC_CLUSTER_UP_LAUNCH_TIMEOUT), NOT the 1200s model-load grace that
   made the 2026-10-22 hang look "forever". The unit is reported failed with
   the output tail and the fleet up continues.
2. already-serving SKIP — a unit whose own /v1/models answers a parseable
   non-empty catalogue on EVERY span node is reported already-up and sparkrun
   is NEVER invoked for it (operator evidence: /v1/models answered 200 WHILE
   the launch hung). Fail-open: any probe error launches anyway.
3. PARALLEL launches — plan units are independent hosts; they must launch
   concurrently (barrier-proved), while `issued` stays plan-ordered with a
   per-unit report (unit_id on every entry).

Address doctrine: sanctioned 10.0.0.x placeholder range only
(scripts/address_guard.py); fixtures are fabricated.
"""

import importlib.util
import os
import sys
import threading

import pytest

# The daemon package lives at the repo root; hscc-cluster's conftest only
# puts the plugin dir on sys.path. Add the root so `import hscc_daemon` works
# from this leg too (same as test_launch_guard.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hscc_daemon import daemon_ops, health, serving  # noqa: E402

HOST_ORCH = "10.0.0.244"
HOST_WK1 = "10.0.0.247"
HOST_WK2 = "10.0.0.248"
BASE_MODEL = "local-lab/Qwen-Flash-NVFP4"


def _units():
    """Three independent solo units — the 2026-10-22 fleet shape (orch on
    .244, two workers on .247/.248)."""
    return [
        {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
         "port": 8000, "recipe": "~/recipes/orch.yaml", "tp": 1},
        {"id": "wk1", "role": "worker", "nodes": [HOST_WK1],
         "port": 8001, "recipe": "~/recipes/wk1.yaml", "tp": 1},
        {"id": "wk2", "role": "worker", "nodes": [HOST_WK2],
         "port": 8002, "recipe": "~/recipes/wk2.yaml", "tp": 1},
    ]


class _FakeServing:
    """Stand-in for the serving module as the CLI uses it. Mirrors the real
    fleet_up_plan entry shape including the `--no-follow --ensure` form."""

    def __init__(self, units):
        self._units = units

    def load_serving(self):
        return {"port": 8000, "units": self._units}

    def fleet_up_plan(self, serving=None):
        return [{"kind": u["role"], "nodes": list(u["nodes"]),
                 "port": u["port"], "unit_id": u["id"], "keepalive": False,
                 "cmd": ["sparkrun", "run", u["recipe"],
                         "--hosts", ",".join(u["nodes"]),
                         "--port", str(u["port"]),
                         "--no-follow", "--ensure"]}
                for u in self._units]


def _ok_result(cmd=None):
    return {"success": True, "returncode": 0, "output": "launched", "error": ""}


def _load_cli(monkeypatch, units, *, probe=None, run_cmd_fn=None):
    """Fresh CLI module wired to hermetic fakes.

    ``probe`` replaces the /v1/models probe seam entirely (default: always
    NOT-serving, so the skip path cannot fire unless a test opts in — and no
    test can reach a real socket). ``run_cmd_fn`` replaces the subprocess
    sink; the default succeeds and records (cmd, timeout).

    Returns (mod, calls, timeouts) where calls/timeouts are shared lists the
    fakes append to.
    """
    path = os.path.join(_REPO_ROOT, "hscc-cluster", "hscc.py")
    spec = importlib.util.spec_from_file_location("hscc_cluster_cli_launch", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    calls, timeouts = [], []

    def _sink(cmd, timeout=30, as_json=False):
        calls.append(list(cmd))
        timeouts.append(timeout)
        return _ok_result()

    monkeypatch.setattr(mod, "run_cmd", run_cmd_fn or _sink)
    monkeypatch.setattr(mod, "_load_daemon_serving", lambda: _FakeServing(units))
    monkeypatch.setattr(mod, "models_probe_fn",
                        probe or (lambda url, timeout=5:
                                  {"ok": False, "models": [], "error": "stub: not serving"}))
    # Launch-phase budget must not depend on the ambient operator env. An
    # EMPTY override resolves to the built-in default (see
    # _launch_phase_timeout_seconds); a test that wants a different budget
    # monkeypatch.setenv AFTER this helper, which wins.
    monkeypatch.setenv("HSCC_CLUSTER_UP_LAUNCH_TIMEOUT", "")

    # Guard: clean status, hermetic alert/log sinks (as test_launch_guard.py).
    monkeypatch.setattr(health, "_launch_guard_status_fn",
                        lambda force=False: {"groups": {}, "solo_entries": [],
                                             "errors": {}, "idle_hosts": [],
                                             "preparing_hosts": [], "pending_ops": []})
    monkeypatch.setattr(serving, "load_serving",
                        lambda: {"port": 8000, "units": units})
    monkeypatch.setattr(health, "send_macos_notification", lambda *a, **k: True)
    monkeypatch.setattr(health, "emit_event", lambda t, p, **k: {})
    monkeypatch.setattr(health, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(health, "_LAUNCH_ALERT_AT", {})
    return mod, calls, timeouts


class TestAlreadyServingSkip:
    """Fix 2 — probe FIRST, never invoke sparkrun for a unit that answers."""

    def test_serving_unit_skipped_without_sparkrun(self, monkeypatch):
        calls = []

        def probe(url, timeout=5):
            if HOST_WK1 in url:
                return {"ok": True, "models": [BASE_MODEL, "worker-model"],
                        "error": ""}
            return {"ok": False, "models": [], "error": "refused"}

        def sink(cmd, timeout=30, as_json=False):
            calls.append(list(cmd))
            return _ok_result()

        mod, _, _ = _load_cli(monkeypatch, _units(), probe=probe,
                              run_cmd_fn=sink)
        res = mod.cmd_cluster_up(dry_run=False)
        # wk1: reported already-up, and NO sparkrun call names its host.
        by_unit = {i["unit_id"]: i for i in res["issued"]}
        assert by_unit["wk1"]["skipped"] == "already_serving"
        assert by_unit["wk1"]["success"] is True
        assert BASE_MODEL in by_unit["wk1"]["served_models"]
        assert all(HOST_WK1 not in " ".join(c) for c in calls)
        # The other two units still launched normally.
        assert any(HOST_ORCH in " ".join(c) for c in calls)
        assert any(HOST_WK2 in " ".join(c) for c in calls)
        assert res["success"] is True

    def test_empty_catalogue_is_not_serving(self, monkeypatch):
        """200 + {"data": []} is the litellm hollow-catalogue shape
        (2026-10-11): parseable but serving nothing is NOT an already-up."""
        def probe(url, timeout=5):
            return {"ok": True, "models": [], "error": ""}

        mod, calls, _ = _load_cli(monkeypatch, _units(), probe=probe)
        res = mod.cmd_cluster_up(dry_run=False)
        assert all("skipped" not in i for i in res["issued"])
        assert len(calls) == 3

    def test_tp_unit_skips_only_when_every_span_node_answers(self, monkeypatch):
        """A TP group answers on the head only once ALL peers have joined; a
        lone responder mid-formation must not be treated as serving."""
        peer = HOST_WK2

        def probe(url, timeout=5):
            if HOST_WK1 in url:
                return {"ok": True, "models": [BASE_MODEL, "worker-model"],
                        "error": ""}
            if peer in url:
                return {"ok": False, "models": [], "error": "refused"}
            return {"ok": True, "models": [BASE_MODEL, "orchestrator-model"],
                    "error": ""}

        units = [
            {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
             "port": 8000, "recipe": "~/recipes/orch.yaml", "tp": 1},
            {"id": "tp", "role": "worker", "nodes": [HOST_WK1, HOST_WK2],
             "port": 8001, "recipe": "~/recipes/tp.yaml", "tp": 2},
        ]
        mod, calls, _ = _load_cli(monkeypatch, units, probe=probe)
        res = mod.cmd_cluster_up(dry_run=False)
        by_unit = {i["unit_id"]: i for i in res["issued"]}
        assert by_unit["orch"]["skipped"] == "already_serving"
        assert "skipped" not in by_unit["tp"]          # peer silent -> launch
        assert any(HOST_WK1 in " ".join(c) and HOST_WK2 in " ".join(c)
                   for c in calls)

    def test_probe_failure_fails_open_to_launch(self, monkeypatch):
        """The probe is a fast path, never an authority: an exception or a
        malformed probe result must launch anyway (--ensure stays safe)."""
        def boom(url, timeout=5):
            raise RuntimeError("probe exploded")

        mod, calls, _ = _load_cli(monkeypatch, _units(), probe=boom)
        res = mod.cmd_cluster_up(dry_run=False)
        assert len(calls) == 3
        assert res["success"] is True


class TestLaunchPhaseTimeout:
    """Fix 1 — a hung launch is cut off at launch scale, reported, and does
    not prevent the other units from launching."""

    def test_launch_uses_launch_scale_timeout_not_model_grace(self, monkeypatch):
        from hscc_daemon import lifecycle
        grace = getattr(lifecycle, "VLLM_LOAD_GRACE_MINUTES", 20) * 60
        mod, calls, timeouts = _load_cli(monkeypatch, _units())
        res = mod.cmd_cluster_up(dry_run=False)
        assert len(timeouts) == 3
        assert all(t == 180 for t in timeouts), \
            "every unit launch must be bounded at the launch-phase default"
        assert 180 < grace, "the launch budget must stay below the load grace"

    def test_launch_timeout_env_override(self, monkeypatch):
        mod, calls, timeouts = _load_cli(monkeypatch, _units())
        monkeypatch.setenv("HSCC_CLUSTER_UP_LAUNCH_TIMEOUT", "7")
        mod.cmd_cluster_up(dry_run=False)
        assert timeouts and all(t == 7 for t in timeouts)

    def test_hung_unit_reported_others_still_launch(self, monkeypatch):
        def sink(cmd, timeout=30, as_json=False):
            calls_inner.append(list(cmd))
            if HOST_WK1 in " ".join(cmd):
                # What run_cmd returns when it cuts a hung launch off:
                # code 124, captured tail of both streams.
                return {"success": False, "returncode": 124,
                        "output": "Waiting for server readiness...\nlast log line",
                        "error": "command timed out after 180s"}
            return _ok_result()

        calls_inner = []
        mod, _, _ = _load_cli(monkeypatch, _units(), run_cmd_fn=sink)
        res = mod.cmd_cluster_up(dry_run=False)
        by_unit = {i["unit_id"]: i for i in res["issued"]}
        hung = by_unit["wk1"]
        assert hung["success"] is False
        assert hung["timed_out"] is True
        assert "command timed out" in hung["output"]
        assert "last log line" in hung["output"]
        # The hang did NOT starve the fleet: all three units were attempted.
        assert len(calls_inner) == 3
        assert by_unit["orch"]["success"] and by_unit["wk2"]["success"]
        assert res["success"] is False, "a timed-out unit fails the up verdict"

    def test_run_cmd_timeout_carries_124_and_output_tail(self, monkeypatch):
        """run_cmd itself: expiry -> code 124 (clusterlib convention) plus
        the partial output tail, so the per-unit report has evidence."""
        path = os.path.join(_REPO_ROOT, "hscc-cluster", "hscc.py")
        spec = importlib.util.spec_from_file_location("hscc_run_cmd_tmo", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        res = mod.run_cmd(
            [sys.executable, "-c",
             "import time; print('partial-out', flush=True); time.sleep(30)"],
            timeout=1)
        assert res["success"] is False
        assert res["returncode"] == 124
        assert "timed out" in res["error"]
        assert "partial-out" in res["output"]


class TestParallelLaunch:
    """Fix 3 — independent hosts launch concurrently, report stays plan-ordered."""

    def test_units_launch_concurrently(self, monkeypatch):
        """A barrier across all units passes ONLY if the launches overlap;
        a serial loop breaks it (and each wait times out after 3s)."""
        units = _units()
        bar = threading.Barrier(len(units), timeout=3)
        calls = []

        def sink(cmd, timeout=30, as_json=False):
            bar.wait()
            calls.append(list(cmd))
            return _ok_result()

        mod, _, _ = _load_cli(monkeypatch, units, run_cmd_fn=sink)
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is True, \
            f"serial launch broke the barrier: {res['issued']}"
        assert len(calls) == 3

    def test_issued_stays_plan_ordered_with_unit_ids(self, monkeypatch):
        """Completion order is nondeterministic (variable sleeps); issued[i]
        must still correspond 1:1 to plan[i] and name its unit."""
        import random
        rnd = random.Random(7)

        def sink(cmd, timeout=30, as_json=False):
            import time
            time.sleep(rnd.uniform(0.001, 0.02))
            return _ok_result()

        units = _units()
        mod, _, _ = _load_cli(monkeypatch, units, run_cmd_fn=sink)
        res = mod.cmd_cluster_up(dry_run=False)
        plan = res["plan"]
        assert len(res["issued"]) == len(plan) == 3
        for entry, issued in zip(plan, res["issued"]):
            assert issued["unit_id"] == entry["unit_id"]
            assert set(entry["nodes"]) <= set(" ".join(issued["command"]).split(" --hosts ")[1].split(" --port ")[0].split(","))

    def test_raising_unit_does_not_sink_siblings(self, monkeypatch):
        calls_inner = []

        def sink(cmd, timeout=30, as_json=False):
            calls_inner.append(list(cmd))
            if HOST_WK1 in " ".join(cmd):
                raise RuntimeError("executor blew up")
            return _ok_result()

        mod, _, _ = _load_cli(monkeypatch, _units(), run_cmd_fn=sink)
        res = mod.cmd_cluster_up(dry_run=False)
        by_unit = {i["unit_id"]: i for i in res["issued"]}
        assert by_unit["wk1"]["success"] is False
        assert "executor blew up" in by_unit["wk1"]["output"]
        assert by_unit["orch"]["success"] and by_unit["wk2"]["success"]
        assert len(calls_inner) == 3


class TestDryRunUnchanged:
    def test_dry_run_prints_plan_without_probe_or_launch(self, monkeypatch):
        probed, called = [], []

        def probe(url, timeout=5):
            probed.append(url)
            return {"ok": True, "models": [BASE_MODEL], "error": ""}

        def sink(cmd, timeout=30, as_json=False):
            called.append(list(cmd))
            return _ok_result()

        mod, _, _ = _load_cli(monkeypatch, _units(), probe=probe,
                              run_cmd_fn=sink)
        res = mod.cmd_cluster_up(dry_run=True)
        assert res["dry_run"] is True
        assert called == [] and probed == []
