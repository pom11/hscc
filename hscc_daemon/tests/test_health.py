"""Unit tests for health.py - health check functions.

Tests isolate I/O by mocking run_cmd, ssh_cmd, http_check, and file reads.
"""
import json
import os
import types
import pytest
from pathlib import Path


class TestGatewayJobAlive:
    """_gateway_job_alive() checks if Hermes gateway is running."""

    def test_launchctl_found(self, fake_subprocess, monkeypatch):
        from hscc_daemon import health
        import sys
        monkeypatch.setattr(sys, "platform", "darwin")
        fake_subprocess.set_result(stdout="    0    ai.hermes.gateway\n", returncode=0)
        assert health._gateway_job_alive() is True

    def test_launchctl_not_found(self, fake_subprocess, monkeypatch):
        from hscc_daemon import health
        import sys
        monkeypatch.setattr(sys, "platform", "darwin")
        fake_subprocess.set_result(stdout="", stderr="does not exist", returncode=1)
        # Falls back to pgrep
        fake_subprocess.set_result(stdout="", returncode=1)
        assert health._gateway_job_alive() is False

    def test_systemctl_active(self, fake_subprocess, monkeypatch):
        from hscc_daemon import health
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemctl" if x == "systemctl" else None)
        fake_subprocess.set_result(stdout="active\n", returncode=0)
        assert health._gateway_job_alive() is True

    def test_systemctl_inactive(self, fake_subprocess, monkeypatch):
        from hscc_daemon import health
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemctl" if x == "systemctl" else None)
        fake_subprocess.set_result(stdout="inactive\n", returncode=3)
        # Falls back to pgrep
        fake_subprocess.set_result(stdout="", returncode=1)
        assert health._gateway_job_alive() is False

    def test_pgrep_fallback(self, fake_subprocess, monkeypatch):
        from hscc_daemon import health
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: None)  # no systemctl
        # pgrep finds process
        fake_subprocess.set_result(stdout="12345\n", returncode=0)
        assert health._gateway_job_alive() is True

    def test_exception_handling(self, fake_subprocess, monkeypatch):
        from hscc_daemon import health
        import sys
        monkeypatch.setattr(sys, "platform", "darwin")
        # launchctl fails with exception
        fake_subprocess.set_result(timeout_exc=True)
        # pgrep fallback also fails — but run_cmd catches TimeoutExpired and returns
        # {"ok": False, "output": "Command timed out..."}. Since pgrep output is
        # truthy on timeout, we need to simulate a clean failure where pgrep returns
        # exit code 1 with empty output.
        fake_subprocess.set_result(stdout="", returncode=1)
        assert health._gateway_job_alive() is False


class TestCheckIdleMonitor:
    """check_idle_monitor() checks for stale idle agents."""

    def test_no_agents_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        # Expanduser mock
        monkeypatch.setitem(os.environ, "HOME", str(tmp_hfcc_dir))

        result = health.check_idle_monitor()
        assert result is False  # FileNotFoundError

    def test_idle_agents_ok(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        agents_file = tmp_hfcc_dir / "agents.json"
        agents_file.write_text(json.dumps({
            "agents": [
                {"id": "a1", "status": "idle"},
                {"id": "a2", "status": "working"},
            ]
        }))

        def fake_expanduser(p):
            if p == "~/.hscc/agents.json":
                return str(agents_file)
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_idle_monitor()
        assert result is True  # < 100 idle agents

    def test_too_many_idle_agents(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        agents_file = tmp_hfcc_dir / "agents.json"
        agents_file.write_text(json.dumps({
            "agents": [{"id": f"a{i}", "status": "idle"} for i in range(101)]
        }))

        def fake_expanduser(p):
            if p == "~/.hscc/agents.json":
                return str(agents_file)
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_idle_monitor()
        assert result is False  # >= 100 idle agents

    def test_malformed_agents_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        agents_file = tmp_hfcc_dir / "agents.json"
        agents_file.write_text("{invalid json")

        def fake_expanduser(p):
            if p == "~/.hscc/agents.json":
                return str(agents_file)
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_idle_monitor()
        assert result is False


class TestCheckWorkers:
    """check_workers() health-checks serving.json keep-alive workers and
    relaunches crashed ones."""

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes, recipe="~/r/27b.yaml"):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        # serving.json with one orchestrator + the given keep-alive workers
        units = [{"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
                  "recipe": "~/r/orch.yaml", "model": "M"}]
        for n in nodes:
            units.append({"id": f"w-{n}", "role": "worker", "keepalive": True,
                          "nodes": [n], "recipe": recipe, "model": "W"})
        monkeypatch.setattr(serving, "ORCH_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving",
                            lambda: {"version": 1, "units": units})
        # Clear in-memory state and point persistence file at tmp dir so
        # _load_worker_relaunch_timestamps() does not pick up real timestamps.
        health._worker_relaunch_at.clear()
        # WD1: each test is a FRESH daemon — reset the debounce/cooldown auto-heal
        # bookkeeping too, or a streak left by an earlier test can fire the
        # auto-heal prematurely (a premature auto-heal `continue`s and skips the
        # gentle relaunch this class's other tests assert on).
        health._worker_down_streak.clear()
        health._worker_last_autoheal.clear()
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                            str(tmp_hfcc_dir / "worker_relaunch.json"))
        return health, serving

    def test_no_keepalive_workers(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=[])
        assert health.check_workers() is True   # nothing to watch -> ok

    def test_all_workers_online(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True})
        calls = []
        monkeypatch.setattr(health, "run_cmd", lambda *a, **k: calls.append(a) or {"ok": True})
        assert health.check_workers() is True
        assert calls == []                      # healthy -> no relaunch

    def test_intentional_autodown_suppresses_relaunch(self, tmp_hfcc_dir, monkeypatch):
        """Fix 1 (SAFETY BLOCKER): while autodown has DELIBERATELY torn the
        fleet down (watchdog block intentional=autodown + autodown state=down),
        check_workers must NOT relaunch the keep-alive unit it stopped. This is
        the third resurrection vector (after pipeline_watchdog + trigger_engine)."""
        import time as _time
        from hscc_daemon import lifecycle as _lc
        from hscc_daemon import autodown as _ad
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        # Worker is DOWN (would normally be relaunched).
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise AssertionError("must NOT relaunch under intentional autodown")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)
        # Intentional autodown is in effect: watchdog block latched intentional +
        # autodown confirmed DOWN — the exact live-state that preceded the 23s
        # resurrection (12:02:42 recorded state=down, 12:03:05 check_workers
        # relaunched).
        block_file = str(tmp_hfcc_dir / "watchdog-block.json")
        monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE", block_file)
        _lc.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                 "reason": "autodown: intentional idle teardown"})
        ad_file = str(tmp_hfcc_dir / "autodown.json")
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
        _ad.save_config({**_ad.DEFAULT_CONFIG, "enabled": True,
                         "state": "down", "down_since": "2026-01-01T00:00:00+00:00"})
        monkeypatch.setattr(health, "time", _time)

        ok = health.check_workers()

        # Deliberately-down unit NOT relaunched — no stop, no Popen run.
        assert popen_calls == []
        assert not any(os.path.basename(a[0]) == "sparkrun" and
                       a[1:2] == ["stop"] for a in ran)
        # The check reports ok (it is correctly doing nothing, not failing).
        assert ok is True

    def test_intentional_autodown_helper_true_only_when_confirmed_down(
            self, tmp_hfcc_dir, monkeypatch):
        """_intentional_autodown_down() consults classify(): True only when the
        block is intentional AND autodown state is confirmed down. Any other
        state (no block, waking, up) ⇒ False so supervision proceeds."""
        from hscc_daemon import lifecycle as _lc
        from hscc_daemon import autodown as _ad
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        block_file = str(tmp_hfcc_dir / "watchdog-block.json")
        monkeypatch.setattr(_lc, "WATCHDOG_BLOCK_FILE", block_file)
        ad_file = str(tmp_hfcc_dir / "autodown.json")
        monkeypatch.setattr(_ad, "AUTODOWN_FILE", ad_file)
        monkeypatch.setattr(health, "_tp_peer_nodes", lambda: set())

        def write_cfg(**over):
            _ad.save_config({**_ad.DEFAULT_CONFIG, **over})

        # No block at all ⇒ False (ordinary supervision).
        assert health._intentional_autodown_down() is False
        # Intentional block but NOT down (blocked-intentional + state up ⇒
        # should_be_up) ⇒ False.
        _lc.save_watchdog_block({"blocked": True, "intentional": "autodown"})
        write_cfg(enabled=True, state="up")
        assert health._intentional_autodown_down() is False
        # Intentional block + state waking ⇒ False (transition in progress).
        write_cfg(enabled=True, state="waking")
        assert health._intentional_autodown_down() is False
        # Intentional block + state DOWN ⇒ True (the resurrection guard).
        write_cfg(enabled=True, state="down")
        assert health._intentional_autodown_down() is True
        # Block latched but AUTODOWN not disabled-state... state down + intent
        # ⇒ True even if enabled flag irrelevant to classify. Confirmed above.

    def test_crashed_worker_relaunched(self, tmp_hfcc_dir, monkeypatch):
        import time as _time
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(("run_cmd", args)) or {"ok": True})
        # Track Popen calls (the detached relaunch path)
        popen_calls = []
        real_popen = health.subprocess.Popen
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return real_popen.__class__.__new__(real_popen.__class__)
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(health, "time", _time)

        # With ok = not down, and relaunched workers not in down, this should be True
        # because the worker goes into relaunched list (not down)
        ok = health.check_workers()
        # stop via run_cmd still fires (argv[0] is the resolved absolute
        # sparkrun CLI, so match on basename + subcommand)
        assert any(t == "run_cmd" and os.path.basename(a[0]) == "sparkrun"
                   and a[1:2] == ["stop"] for t, a in ran)
        # Popen called for sparkrun run (detached)
        assert len(popen_calls) == 1
        popen_args = popen_calls[0][0][0]
        # t_b543e530: the detached relaunch MUST be executable under a service
        # PATH without ~/.local/bin — argv[0] is the resolved ABSOLUTE sparkrun
        # path (basename stays "sparkrun"), never the bare name.
        import os as _os
        assert _os.path.basename(popen_args[0]) == "sparkrun"
        assert _os.path.isabs(popen_args[0])
        assert popen_args[1] == "run"
        assert "10.0.0.2" in popen_args
        assert any("27b" in str(x) for x in popen_args)
        # Popen kwargs: detached + log file
        kwargs = popen_calls[0][1]
        assert kwargs.get("start_new_session") is True

    def test_relaunch_reuses_declared_cluster_id(self, tmp_hfcc_dir, monkeypatch):
        """t_e1ff0e8e: the gentle relaunch MUST re-issue the unit AS DEFINED,
        carrying EVERY input sparkrun folds into the intent hash (recipe, port,
        --served-model-name, --tp). Before the fix the relaunch dropped
        --served-model-name, so sparkrun derived a DIFFERENT cluster_id for the
        same hosts and `--ensure` launched a PARALLEL DUPLICATE instead of
        re-issuing the declared workload. The relaunch command must therefore
        equal fleet_up_plan's command for the same TP worker."""
        import time as _time
        health, serving = self._setup(tmp_hfcc_dir, monkeypatch, nodes=[])
        from hscc_daemon import serving as serving_mod
        # A TP=2 keep-alive worker with a declared model (so --served-model-name
        # must be carried through, exactly as fleet_up_plan does).
        monkeypatch.setattr(
            serving_mod, "load_serving",
            lambda: {"version": 2, "units": [
                {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
                 "recipe": "~/r/orch.yaml", "model": "M"},
                {"id": "w-tp", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.247", "10.0.0.248"], "port": 8000,
                 "recipe": "~/r/27b.yaml", "model": "W", "tp": 2},
            ]})
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(("run_cmd", args)) or {"ok": True})
        popen_calls = []
        real_popen = health.subprocess.Popen
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return real_popen.__class__.__new__(real_popen.__class__)
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(health, "time", _time)

        health.check_workers()
        assert len(popen_calls) >= 1
        # Both span members may be probed independently (the test env has no
        # live serving.json for _tp_peer_nodes), but EVERY relaunch must
        # re-issue the SAME defined unit — no divergence.
        plan = serving_mod.fleet_up_plan()
        wk = next(p["cmd"] for p in plan if p["unit_id"] == "w-tp")
        for (args, _kwargs) in popen_calls:
            relaunch = args[0]
            # Same full-span hosts + --tp as the declared worker…
            assert "--hosts" in relaunch
            assert relaunch[relaunch.index("--hosts") + 1] == "10.0.0.247,10.0.0.248"
            assert "--tp" in relaunch
            assert relaunch[relaunch.index("--tp") + 1] == "2"
            # …AND the --served-model-name that fleet_up_plan carries (the
            # missing piece that previously changed the intent hash →
            # duplicate cluster_id).
            assert "--served-model-name" in relaunch
            assert (relaunch[relaunch.index("--served-model-name") + 1]
                    == "W worker-model")
            # Identical to fleet_up_plan's command for the same unit from the
            # recipe onwards, so sparkrun derives the SAME cluster_id and
            # --ensure re-issues the declared workload instead of duplicating
            # it (t_e1ff0e8e). argv[0] is deliberately NOT compared verbatim:
            # the relaunch resolves sparkrun to an ABSOLUTE path at exec time
            # (t_b543e530 — a service PATH without ~/.local/bin cannot exec the
            # bare name) while fleet_up_plan emits the logical command. Same
            # program, same intent-hash inputs.
            import os as _os
            assert _os.path.basename(relaunch[0]) == wk[0]
            assert _os.path.isabs(relaunch[0])
            assert relaunch[1:] == wk[1:]

    def test_grace_window_skips_relaunch(self, tmp_hfcc_dir, monkeypatch):
        import time as _time
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        # pretend we just relaunched it -> within grace, must NOT relaunch again.
        # Grace is keyed per UNIT (node, port); units w/o explicit port → :8000.
        # Uses wall-clock time.time() (not monotonic).
        health._worker_relaunch_at[("10.0.0.2", 8000)] = _time.time()
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("should not launch")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)
        health.check_workers()
        assert ran == []                         # grace window respected
        assert popen_calls == []                 # no Popen within grace

    def test_colocated_units_supervised_per_port(self, tmp_hfcc_dir, monkeypatch):
        """G1: two models co-located on one node (distinct ports) are each
        health-checked + relaunched independently; a healthy sibling is NOT
        killed when the other is down."""
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"; state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        units = [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
             "recipe": "~/r/orch.yaml", "model": "M"},
            {"id": "a", "role": "worker", "keepalive": True, "nodes": ["10.0.0.2"],
             "port": 8000, "recipe": "~/r/a.yaml", "model": "A"},
            {"id": "b", "role": "worker", "keepalive": True, "nodes": ["10.0.0.2"],
             "port": 8001, "recipe": "~/r/b.yaml", "model": "B"},
        ]
        monkeypatch.setattr(serving, "ORCH_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving", lambda: {"version": 2, "units": units})
        health._worker_relaunch_at.clear()
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                            str(tmp_hfcc_dir / "worker_relaunch.json"))
        # :8000 healthy, :8001 down
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": ":8000/" in url})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("mock")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)
        health.check_workers()
        # only b (:8001, ~/r/b.yaml) relaunched; a's recipe never touched
        assert len(popen_calls) == 1
        popen_args = popen_calls[0][0][0]
        assert any("b.yaml" in str(x) for x in popen_args)
        assert "8001" in popen_args
        # stop targeted b's recipe, not --all (sibling a survives)
        stop_cmds = [a for a in ran
                     if os.path.basename(a[0]) == "sparkrun" and a[1:2] == ["stop"]]
        assert all("--all" not in a for a in stop_cmds)
        assert not any("a.yaml" in str(x) for x in popen_args)

    def test_detached_relaunch_log_file(self, tmp_hfcc_dir, monkeypatch):
        """Asserts log-file wiring: file opened in append mode at expected path."""
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True})
        # Track open() calls via builtins — only intercept relaunch log path
        opened_files = []
        _real_open = open
        def fake_open(path, mode="r", *args, **kwargs):
            if isinstance(path, str) and "relaunch-" in path:
                opened_files.append((path, mode))
                class FakeFile:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                    def write(self, *a): pass
                    def close(self): pass
                return FakeFile()
            return _real_open(path, mode, *args, **kwargs)
        monkeypatch.setattr("builtins.open", fake_open)
        # Track Popen calls
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("mock")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)
        # Expanduser for log path
        def fake_expanduser(p):
            if p.startswith("~/.hscc/"):
                return str(tmp_hfcc_dir / p[1:])
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)

        health.check_workers()
        # open() called with append mode at correct path
        assert len(opened_files) >= 1
        log_path, log_mode = opened_files[0]
        assert "relaunch-10.0.0.2-8000.log" in log_path
        assert log_mode == "a"

    def test_popen_exception_logged(self, tmp_hfcc_dir, monkeypatch):
        """On Popen exception, log ERROR with exception text."""
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True})
        log_calls = []
        monkeypatch.setattr(health, "log", lambda msg, level="INFO": log_calls.append((msg, level)))
        def fake_popen(*args, **kwargs):
            raise PermissionError("spawn denied")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        health.check_workers()
        # ERROR log with exception text
        error_logs = [m for m, l in log_calls if l == "ERROR"]
        assert len(error_logs) == 1
        assert "spawn denied" in error_logs[0]
        assert "w-10.0.0.2" in error_logs[0]

    def test_popen_failure_counts_worker_down(self, tmp_hfcc_dir, monkeypatch):
        """A relaunch whose Popen raises is a FAILED launch: the worker goes to
        down (ok=False), not to relaunched — a worker we could not even start
        must not look healthy. The grace timestamp is still recorded so the
        next cycle does not thrash."""
        import time as _time
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True})
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("mock")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        ok = health.check_workers()
        # Popen raised — worker goes to down, ok = not down = False
        assert ok is False
        # Grace timestamp still recorded (no thrash on the next cycle)
        assert ("10.0.0.2", 8000) in health._worker_relaunch_at
        ts = health._worker_relaunch_at[("10.0.0.2", 8000)]
        assert isinstance(ts, float)

    def test_grace_period_uses_lifecycle_default_20min(self, tmp_hfcc_dir, monkeypatch):
        """Asserts grace period from lifecycle.py is respected (20 min default). Uses
        wall-clock time (time.time())."""
        import time as _time
        from hscc_daemon import lifecycle
        # Confirm default is 20
        monkeypatch.setattr(lifecycle, "VLLM_LOAD_GRACE_MINUTES", 20)

        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True})

        # Set relaunch time to 19 minutes ago (within 20-min grace)
        health._worker_relaunch_at[("10.0.0.2", 8000)] = _time.time() - 19 * 60
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("should not launch")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        health.check_workers()
        assert popen_calls == []  # within 20-min grace, no relaunch

        # Set relaunch time to 21 minutes ago (past grace) — relaunch fires
        health._worker_relaunch_at[("10.0.0.2", 8000)] = _time.time() - 21 * 60
        popen_calls.clear()
        health.check_workers()
        assert len(popen_calls) == 1  # past grace, relaunch fires

    def test_persisted_grace_window_survives_reload(self, tmp_hfcc_dir, monkeypatch):
        """Grace timestamps persisted to disk survive a module reload."""
        from hscc_daemon import health
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"; state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        relaunch_file = tmp_hfcc_dir / "worker_relaunch.json"
        # Persist a timestamp from 10 minutes ago
        import time as _time
        old_ts = _time.time() - 10 * 60
        relaunch_file.write_text(json.dumps({"(10.0.0.2,8000)": old_ts}))
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE", str(relaunch_file))

        # Clear in-memory dict to simulate fresh process
        health._worker_relaunch_at.clear()

        # Load persisted timestamps
        health._load_worker_relaunch_timestamps()

        assert ("10.0.0.2", 8000) in health._worker_relaunch_at
        ts = health._worker_relaunch_at[("10.0.0.2", 8000)]
        assert abs(ts - old_ts) < 1  # loaded correctly

    def test_persisted_grace_window_corrupt_file(self, tmp_hfcc_dir, monkeypatch):
        """Corrupt persisted file is treated as empty (no crash)."""
        from hscc_daemon import health

        relaunch_file = tmp_hfcc_dir / "worker_relaunch.json"
        relaunch_file.write_text("{bad json content")
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE", str(relaunch_file))

        health._worker_relaunch_at.clear()
        health._load_worker_relaunch_timestamps()  # must not raise
        assert health._worker_relaunch_at == {}

    def test_persisted_grace_window_missing_file(self, tmp_hfcc_dir, monkeypatch):
        """Missing persisted file is treated as empty (no crash)."""
        from hscc_daemon import health

        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                            str(tmp_hfcc_dir / "nonexistent.json"))
        health._worker_relaunch_at.clear()
        health._load_worker_relaunch_timestamps()  # must not raise
        assert health._worker_relaunch_at == {}

    def test_save_worker_relaunch_timestamps(self, tmp_hfcc_dir, monkeypatch):
        """_save_worker_relaunch_timestamps() persists to disk."""
        from hscc_daemon import health

        relaunch_file = tmp_hfcc_dir / "worker_relaunch.json"
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE", str(relaunch_file))

        import time as _time
        ts = _time.time()
        health._worker_relaunch_at[("10.0.0.2", 8000)] = ts
        health._save_worker_relaunch_timestamps()

        assert relaunch_file.exists()
        data = json.loads(relaunch_file.read_text())
        assert "(10.0.0.2,8000)" in data
        assert abs(data["(10.0.0.2,8000)"] - ts) < 1

    def test_sparkrun_stop_failure_logged(self, tmp_hfcc_dir, monkeypatch):
        """When sparkrun stop fails, a WARN is logged but relaunch still proceeds."""
        import time as _time
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})

        log_calls = []
        monkeypatch.setattr(health, "log", lambda msg, level="INFO": log_calls.append((msg, level)))

        # First call: sparkrun stop fails. No more calls needed (Popen not reached)
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": False, "output": "stop error"})

        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("should not reach Popen if run_cmd is mocked")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        health.check_workers()

        # WARN logged for stop failure
        warn_logs = [m for m, l in log_calls if l == "WARN"]
        assert any("stop" in m.lower() and "stop error" in m for m in warn_logs)

    def _setup_span(self, tmp_hfcc_dir, monkeypatch):
        """Setup a keep-alive worker unit spanning TWO nodes (tp=2):
        10.0.0.2 = primary (binds the endpoint), 10.0.0.3 = tp peer (no endpoint
        of its own). Returns (health, serving, state_dir)."""
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        units = [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
             "recipe": "~/r/orch.yaml", "model": "M"},
            {"id": "w-span", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2", "10.0.0.3"], "tp": 2,
             "recipe": "~/r/span.yaml", "model": "S"},
        ]
        monkeypatch.setattr(serving, "ORCH_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving",
                            lambda: {"version": 2, "units": units})
        health._worker_relaunch_at.clear()
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                            str(tmp_hfcc_dir / "worker_relaunch.json"))
        # Redirect relaunch-log write into tmp so the test never touches the
        # real ~/.hscc on this machine.
        def fake_expanduser(p):
            if p.startswith("~/.hscc/"):
                return str(tmp_hfcc_dir / p[len("~/.hscc/"):])
            return p
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        return health, serving, state_dir

    def test_tp_peer_with_no_endpoint_report_ok_never_down(self, tmp_hfcc_dir, monkeypatch):
        """A tp-peer span member has NO endpoint of its own but must be reported
        online, never down, and never relaunched as a solo unit. The span's
        PRIMARY answers its own :8000; the peer's :8000 does not."""
        health, _, state_dir = self._setup_span(tmp_hfcc_dir, monkeypatch)
        # Scoreboard marks 10.0.0.3 the tp peer of the 10.0.0.2 span
        monkeypatch.setattr(health, "_tp_peer_nodes", lambda: {"10.0.0.3"})
        # Primary .2 answers; peer .3 has no endpoint (http_check to .3 -> False)
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": ".2:8000" in url})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        popen_calls = []
        def fake_popen(*a, **k):
            popen_calls.append(a)
            raise RuntimeError("should not relaunch a tp peer")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        ok = health.check_workers()
        state = json.loads((state_dir / "workers.json").read_text())
        assert ok is True
        # both primary and peer reported online (peer's online count = 2 total)
        assert state["online"] == 2
        assert state["down"] == []
        assert state["relaunched"] == []
        assert popen_calls == []

    def test_span_member_relaunched_as_group_not_solo(self, tmp_hfcc_dir, monkeypatch):
        """A down span is relaunched as the WHOLE TP group, not a solo on the head.

        The card t_4c9dd6d4 regression: the gentle relaunch in check_workers used
        to drop to a single-host, no-`--tp` `sparkrun run --hosts <head>` — the
        `_SOLO` that wedged the unit. Now the down primary .2 is relaunched as the
        full span (--hosts 10.0.0.2,10.0.0.3 --tp 2), and the peer .3 is never
        solo-provisioned as a separate single-host run.
        """
        health, _, state_dir = self._setup_span(tmp_hfcc_dir, monkeypatch)
        monkeypatch.setattr(health, "_tp_peer_nodes", lambda: {"10.0.0.3"})
        # Both endpoints down — spans pair must be re-provisioned together
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        popen_calls = []
        def fake_popen(*a, **k):
            popen_calls.append(a)
            return None  # success — the code never uses the Popen handle
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        health.check_workers()
        state = json.loads((state_dir / "workers.json").read_text())
        # exactly ONE sparkrun run (one GROUP relaunch, not one per node / not a solo)
        assert len(popen_calls) == 1
        cmd = popen_calls[0][0]   # first positional arg to Popen = the argv list
        # t_b543e530: resolved to an absolute sparkrun path at exec time (the
        # conftest pins a hermetic fake CLI), never the bare name.
        import os as _os
        assert _os.path.basename(cmd[0]) == "sparkrun" and _os.path.isabs(cmd[0])
        assert cmd[1] == "run"
        # the full span is in --hosts (comma-joined head,peer)
        i = cmd.index("--hosts")
        assert cmd[i + 1] == "10.0.0.2,10.0.0.3"
        # --tp 2 carried so the pair re-binds host-network as a TP group, not a solo
        j = cmd.index("--tp")
        assert cmd[j + 1] == "2"
        # --served-model-name not required here (unit w/o model alias path); the
        # span command is what matters. peer:8000 never health-hit standalone.
        assert state["down"] == []                # peer never counted down
        assert state["relaunched"] != []          # the group relaunch logs a relaunch
        # requirement #3: the stop also targets the FULL span (hosts .2,.3), so
        # no two workloads are ever left bound to the same host:port.
        stop_cmds = [a for a in ran
                     if os.path.basename(a[0]) == "sparkrun" and a[1:2] == ["stop"]]
        assert any(a[a.index("--hosts") + 1] == "10.0.0.2,10.0.0.3" for a in stop_cmds)

    def test_genuinely_down_primary_still_relaunched(self, tmp_hfcc_dir, monkeypatch):
        """Regression: real self-heal must be preserved — a genuinely down
        PRIMARY (single-node unit, not a span peer) is still relaunched."""
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "_tp_peer_nodes", lambda: set())  # no peers
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True})
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            return None  # success — never uses the Popen handle
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        health.check_workers()
        # solo single-node primary IS relaunched (self-heal intact)
        assert len(popen_calls) == 1
        popen_args = popen_calls[0][0][0]
        assert "10.0.0.2" in popen_args


class TestCheckWorkersAutoHeal:
    """WD1: check_workers() debounced auto-heal of a down worker unit.

    A unit down across N CONSECUTIVE checks (default 3 ≈ 90s) is recreated — the
    unit ONLY, never the whole template (F3); a single blip or a
    flapping (down→up→down) unit does NOT trigger; a cooldown stops the same unit
    from being re-healed within the window even if it stays down. The down-check
    history and the heal call are injected — never touches a real node/docker.
    Debounce + cooldown are read from config, not hardcoded.
    """

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes, recipe="~/r/27b.yaml"):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        units = [{"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
                  "recipe": "~/r/orch.yaml", "model": "M"}]
        for n in nodes:
            units.append({"id": f"w-{n}", "role": "worker", "keepalive": True,
                          "nodes": [n], "recipe": recipe, "model": "W"})
        monkeypatch.setattr(serving, "ORCH_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving",
                            lambda: {"version": 1, "units": units})
        # Reset module-level auto-heal + relaunch state so a test starts clean.
        health._worker_relaunch_at.clear()
        health._worker_down_streak.clear()
        health._worker_last_autoheal.clear()
        health._worker_container_state.clear()
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                            str(tmp_hfcc_dir / "worker_relaunch.json"))
        monkeypatch.setattr(health, "_WORKER_CONTAINER_STATE_FILE",
                            str(tmp_hfcc_dir / "worker_container_state.json"))
        # These tests exercise debounce/cooldown mechanics, not the startup
        # grace. Default the container-age probe to an OLD container (older
        # than the grace) so the unit is a genuine wedge that heals promptly —
        # the fail-closed startup grace (unknown age ⇒ shield) must not mask
        # the auto-heal path these tests pin.
        import time as _t
        _old = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: _old)
        return health, serving

    def _down(self, health, monkeypatch):
        """Force every probe down; stub the gentle sparkrun relaunch so only the
        auto-heal path under test is observable."""
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": True})
        monkeypatch.setattr(health.subprocess, "Popen", lambda *a, **k: None)

    def test_single_down_check_does_not_trigger(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        self._down(health, monkeypatch)
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        health.check_workers()          # 1 down-check < debounce
        assert heals == []             # no auto-heal on a single blip

    def test_n_consecutive_down_checks_triggers(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        self._down(health, monkeypatch)
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        # up to N-1 consecutive downs: debounce not yet reached
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE - 1):
            health.check_workers()
        assert heals == []
        # the Nth consecutive down fires the auto-heal FOR THIS UNIT
        health.check_workers()
        assert len(heals) == 1
        key, label, node, port = heals[0]
        assert key == ("10.0.0.2", 8000)   # unit identity (node, port)
        assert node == "10.0.0.2"
        assert port == 8000
        assert "w-10.0.0.2" in label

    def test_keepalive_healing_proceeds_while_intentional(self, tmp_hfcc_dir, monkeypatch):
        """§5 C2: keepalive/other-unit healing must continue while an
        intentional autodown is in effect. The fork is "don't resurrect the
        intentionally-down orchestrator", NOT "do nothing" — a keepalive worker
        that autodown did NOT stop must still be auto-healed when it crashes.
        This exercises the REAL check_workers (the function responsible for
        keepalive supervision) with an intentional watchdog block present."""
        from hscc_daemon import health, serving, lifecycle
        # Set up a keepalive worker unit (never a teardown target).
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        self._down(health, monkeypatch)
        # An intentional autodown block is latched (orchestrator deliberately
        # down by autodown) — the exact state during an idle teardown.
        block_file = str(tmp_hfcc_dir / "watchdog-block.json")
        monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        lifecycle.save_watchdog_block({"blocked": True, "intentional": "autodown",
                                       "reason": "autodown: intentional idle teardown"})
        # keepalive unit down across the debounce ⇒ auto-heal still fires
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert len(heals) == 1
        assert heals[0][2] == "10.0.0.2"    # keepalive unit healed despite intentional

    def test_flapping_resets_consecutive_count(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": True})
        monkeypatch.setattr(health.subprocess, "Popen", lambda *a, **k: None)
        # down (streak 1)
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        health.check_workers()
        # up (streak reset)
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True})
        health.check_workers()
        # down again (streak restarts at 1), then down (2) — STILL < debounce
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        health.check_workers()
        health.check_workers()
        # 3 total downs across a flap, but never 3 CONSECUTIVE -> no auto-heal
        assert heals == []

    def test_cooldown_blocks_second_heal(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        self._down(health, monkeypatch)
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        # fire the first auto-heal (N consecutive downs)
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert len(heals) == 1
        # rebuild the streak within the cooldown window (still down) — must NOT re-fire
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE * 2):
            health.check_workers()
        assert len(heals) == 1          # cooldown prevented a second auto-heal

    def test_action_logged_distinctly(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        self._down(health, monkeypatch)
        log_calls = []
        monkeypatch.setattr(health, "log",
                            lambda msg, level="INFO": log_calls.append(msg))
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: {"status": "ok", "template": "candy"})
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert any("Auto-heal" in m for m in log_calls)   # auditable distinct action
        # F3: the log must say it recreates THIS unit only — the old wording
        # promised a whole-template force-recreate, which is what hung the fleet.
        assert any("recreating this unit only" in m for m in log_calls)

    def test_config_values_are_read_not_hardcoded(self, tmp_hfcc_dir, monkeypatch):
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        self._down(health, monkeypatch)
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        # Override the debounce threshold to 2 (from config) — NOT hardcoded 3.
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 2)
        health.check_workers()          # streak 1 < 2 -> no heal
        assert heals == []
        health.check_workers()          # streak 2 -> fires
        assert len(heals) == 1

    def _heal_isolation(self, health, monkeypatch, tmp_hfcc_dir):
        """Neutralize every side effect _default_autoheal_worker can have, so a
        heal test can never touch a real node, docker, or the REAL ~/.hscc:
        silence the log, record the stop + the detached launch, and redirect
        the heal log / relaunch-state paths into the tmp dir.

        Returns (stops, launches) — argv lists of what the heal issued.
        """
        stops, launches = [], []
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        monkeypatch.setattr(
            health, "run_cmd",
            lambda args, **k: stops.append(list(args)) or {"ok": True, "output": ""})
        monkeypatch.setattr(
            health.subprocess, "Popen",
            lambda argv, **k: launches.append(list(argv)) or None)
        # Resolve the sparkrun CLI without touching a real install.
        monkeypatch.setattr(health.sparkrun_bin, "argv", lambda *a: list(a))
        monkeypatch.setattr(health, "_sparkrun_exec_argv", lambda argv: list(argv))
        # Redirect ~/.hscc (heal log) into the tmp dir — never the real one.
        (tmp_hfcc_dir / ".hscc").mkdir(parents=True, exist_ok=True)
        _real_expanduser = os.path.expanduser

        def fake_expanduser(p):
            if isinstance(p, str) and p.startswith("~/.hscc/"):
                return str(tmp_hfcc_dir / ".hscc" / p[len("~/.hscc/"):])
            return _real_expanduser(p)
        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        return stops, launches

    def _one_unit_serving(self, monkeypatch, node="10.0.0.2", port=8000,
                          recipe="~/r/a.yaml", model="A"):
        from hscc_daemon import serving
        monkeypatch.setattr(
            serving, "load_serving",
            lambda: {"version": 1, "units": [
                {"id": f"w-{node}", "role": "worker", "keepalive": True,
                 "nodes": [node], "recipe": recipe, "model": model,
                 "tp": 1, "port": port}]})

    def test_default_heal_recreates_only_the_down_unit(self, tmp_hfcc_dir, monkeypatch):
        """F3 (HEAL_CASCADE 2026-10-14): healing ONE unit must NOT re-apply the
        whole template. The old path ran `template apply <applied> --confirm
        --force-recreate`, which stopped and re-ran EVERY unit — at 07:21Z and
        07:43Z that destroyed two healthy units mid-load. The heal must instead
        stop the down unit's OWN span and re-issue its OWN run command."""
        from hscc_daemon import health, serving
        stops, launches = self._heal_isolation(health, monkeypatch, tmp_hfcc_dir)
        # serving.json: the unit under heal + a healthy sibling on another node.
        monkeypatch.setattr(serving, "load_serving", lambda: {"version": 1, "units": [
            {"id": "w-a", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "recipe": "~/r/a.yaml", "model": "A",
             "tp": 1, "port": 8000},
            {"id": "w-b", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.3"], "recipe": "~/r/b.yaml", "model": "B",
             "tp": 1, "port": 8001},
        ]})

        res = health._default_autoheal_worker(
            ("10.0.0.2", 8000), "w-1", "10.0.0.2", 8000)

        # exactly one stop, scoped to THIS unit's recipe + span — never --all
        assert len(stops) == 1
        assert stops[0][0] == "stop"
        assert stops[0][1].endswith("r/a.yaml")
        assert "--hosts" in stops[0] and "10.0.0.2" in stops[0]
        assert "10.0.0.3" not in stops[0]                 # sibling host untouched
        assert "--all" not in stops[0]
        # exactly one launch, for THIS unit only
        assert len(launches) == 1
        launched = " ".join(launches[0])
        assert "r/a.yaml" in launched and "8000" in launched
        assert "r/b.yaml" not in launched                 # sibling not relaunched
        assert "template" not in launched and "apply" not in launched
        # --ensure must be stripped, or an Up-but-wedged container would no-op
        assert "--ensure" not in launches[0]
        assert res.get("ok") is True and res.get("status") == "recreating"

    def test_default_heal_recreates_a_tp_span_as_a_group(self, tmp_hfcc_dir, monkeypatch):
        """A TP unit's heal must stop and re-run its WHOLE span (head + worker
        nodes) with the unit's --tp, exactly like the gentle relaunch — never a
        single-host solo, which would bind the wrong topology."""
        from hscc_daemon import health, serving
        stops, launches = self._heal_isolation(health, monkeypatch, tmp_hfcc_dir)
        monkeypatch.setattr(serving, "load_serving", lambda: {"version": 1, "units": [
            {"id": "w-tp", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2", "10.0.0.3"], "recipe": "~/r/tp.yaml",
             "model": "TP", "tp": 2, "port": 8000},
        ]})

        res = health._default_autoheal_worker(
            ("10.0.0.2", 8000), "w-tp", "10.0.0.2", 8000)

        assert len(stops) == 1 and len(launches) == 1
        assert stops[0][stops[0].index("--hosts") + 1] == "10.0.0.2,10.0.0.3"
        launched = " ".join(launches[0])
        assert "--hosts 10.0.0.2,10.0.0.3" in launched
        assert "--tp 2" in launched
        assert res.get("ok") is True

    def test_default_heal_never_touches_the_template_apply_path(
            self, tmp_hfcc_dir, monkeypatch):
        """Regression guard for the incident's most destructive actor: no code
        path in the heal may import or call cmd_cluster_template. If someone
        reintroduces the whole-template apply, this fails at once."""
        import sys as _sys
        from hscc_daemon import health
        stops, launches = self._heal_isolation(health, monkeypatch, tmp_hfcc_dir)

        def boom(argv):
            raise AssertionError(f"heal must NEVER apply a template: {argv}")

        cli_mod = types.ModuleType("cluster_template_cli")
        cli_mod.cmd_cluster_template = boom          # type: ignore[attr-defined]
        tpl_mod = types.ModuleType("cluster_template")
        tpl_mod.applied_status = lambda: {"applied": {"template": "candy"}}  # type: ignore[attr-defined]
        monkeypatch.setitem(_sys.modules, "cluster_template_cli", cli_mod)
        monkeypatch.setitem(_sys.modules, "cluster_template", tpl_mod)
        self._one_unit_serving(monkeypatch)

        res = health._default_autoheal_worker(
            ("10.0.0.2", 8000), "w-1", "10.0.0.2", 8000)
        assert res.get("ok") is True                  # healed without applying
        assert len(stops) == 1 and len(launches) == 1

    def test_default_heal_records_grace_for_the_container_it_creates(
            self, tmp_hfcc_dir, monkeypatch):
        """The recreate re-stages weights for minutes, so it must register the
        relaunch timestamp BEFORE launching — that is what lets the F1 load
        grace shield the new container from the gentle relaunch."""
        from hscc_daemon import health
        stops, launches = self._heal_isolation(health, monkeypatch, tmp_hfcc_dir)
        key = ("10.0.0.2", 8000)
        health._worker_relaunch_at.pop(key, None)
        self._one_unit_serving(monkeypatch)

        health._default_autoheal_worker(key, "w-1", "10.0.0.2", 8000)
        assert key in health._worker_relaunch_at

    def test_default_heal_reports_without_recreating_when_unit_unknown(
            self, tmp_hfcc_dir, monkeypatch):
        """No serving.json entry for (node, port) => no stop, no launch, and a
        loud 'no serving.json unit' error instead of a guess."""
        from hscc_daemon import health
        stops, launches = self._heal_isolation(health, monkeypatch, tmp_hfcc_dir)
        from hscc_daemon import serving
        monkeypatch.setattr(serving, "load_serving",
                            lambda: {"version": 1, "units": []})

        res = health._default_autoheal_worker(
            ("10.0.0.9", 8000), "w-x", "10.0.0.9", 8000)
        assert stops == [] and launches == []
        assert res.get("ok") is False
        assert "no serving.json unit" in res.get("error", "")


class TestWorkersStartupGrace:
    """WD2: auto-heal startup grace keyed on container age + has-served.

    A unit whose container is younger than WORKER_AUTOHEAL_LOAD_GRACE AND has
    never served is still LOADING, not dead — it must NOT be auto-healed, and
    that must hold ACROSS a daemon restart (container age + has-served are
    durable; the in-memory debounce resets). A unit that HAS served and then
    stopped is a genuine failure and heals promptly. No real node/docker is
    touched — the container-age probe is injected (like _autoheal_worker_fn).
    """

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"]):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        units = [{"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
                  "recipe": "~/r/orch.yaml", "model": "M"}]
        for n in nodes:
            units.append({"id": f"w-{n}", "role": "worker", "keepalive": True,
                          "nodes": [n], "recipe": "~/r/27b.yaml", "model": "W"})
        monkeypatch.setattr(serving, "ORCH_NODES", {"10.0.0.1"})
        monkeypatch.setattr(serving, "load_serving",
                            lambda: {"version": 1, "units": units})
        health._worker_relaunch_at.clear()
        health._worker_down_streak.clear()
        health._worker_last_autoheal.clear()
        health._worker_container_state.clear()
        monkeypatch.setattr(health, "_WORKER_RELATCH_FILE",
                            str(tmp_hfcc_dir / "worker_relaunch.json"))
        monkeypatch.setattr(health, "_WORKER_CONTAINER_STATE_FILE",
                            str(tmp_hfcc_dir / "worker_container_state.json"))
        # Unit is DOWN the whole time; stub the gentle relaunch so only the
        # auto-heal path is observable.
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": True})
        monkeypatch.setattr(health.subprocess, "Popen", lambda *a, **k: None)
        return health

    def _heals_recorder(self, health, monkeypatch):
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        return heals

    def test_young_never_served_no_autoheal(self, tmp_hfcc_dir, monkeypatch):
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        # Container was JUST created (young) and has no serve history.
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: _t.time())
        # Many consecutive down-checks — well past the debounce — must NOT
        # auto-heal a still-loading unit.
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE * 4):
            health.check_workers()
        assert heals == []

    def test_young_never_served_survives_daemon_restart(self, tmp_hfcc_dir, monkeypatch):
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: _t.time())
        created_ts = _t.time()
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": created_ts, "has_served": False}
        health._save_worker_container_state()  # durable on disk
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: created_ts)
        # Simulate a daemon restart: in-memory debounce + container state are
        # wiped, ONLY the on-disk container-age record survives.
        health._worker_down_streak.clear()
        health._worker_container_state.clear()
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert heals == []           # still loading — never auto-healed

    def test_old_never_served_is_genuine_wedge_and_heals(self, tmp_hfcc_dir, monkeypatch):
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        # Container created LONG ago (older than the grace) and never served —
        # that is a genuine wedge, not a load. Auto-heal must fire.
        old = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": old, "has_served": False}
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: old)
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert len(heals) == 1
        assert heals[0][2] == "10.0.0.2"

    def test_served_then_stopped_still_heals(self, tmp_hfcc_dir, monkeypatch):
        """A unit that HAS served and then stopped is a genuine failure — it
        must auto-heal promptly regardless of its container's age."""
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        # The unit served before (persisted has_served=True) and its container
        # is young — but it IS a real crash, so it must still be healed.
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": _t.time(), "has_served": True}
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: _t.time())
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert len(heals) == 1
        assert heals[0][2] == "10.0.0.2"

    def test_recreated_container_gets_fresh_grace(self, tmp_hfcc_dir, monkeypatch):
        """A container that was recreated (new created_ts) resets the serve
        history, so its fresh load is shielded again."""
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        old_ts = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 600
        # The OLD container served before (recorded with the old ts).
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": old_ts, "has_served": True}
        # It was recreated just now — a brand-new container, younger than grace.
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: _t.time())
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert heals == []           # fresh container loading — no auto-heal

    def test_unknown_age_fails_closed_shields_unit(self, tmp_hfcc_dir, monkeypatch):
        """Age cannot be determined (live sparkrun age unknown AND no stored
        created_ts) → fail CLOSED: assume still-loading, shield for the grace
        window (bounded). No docker/ssh fallback — the unknown age is the
        actual remote-exec result via the injected seam."""
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        # Sparkrun cannot tell us the container's age (no entry / query fail).
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: None)
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert heals == []           # unknown age → assumed loading → no heal
        # The fresh-assumption was persisted (durable) for the down unit.
        rec = health._worker_container_state.get(("10.0.0.2", 8000))
        assert rec is not None and rec["created_ts"] is not None
        assert rec["has_served"] is False

    def test_unknown_age_survives_daemon_restart_bounded(self, tmp_hfcc_dir, monkeypatch):
        """Fail-closed fresh-assumption is DURABLE: after a restart the unit is
        still shielded (no fresh in-memory debounce into a force-recreate), and
        the shield EXPIRES after the grace so a genuinely dead unit heals."""
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        # First sight: unknown age → assume fresh, persisted.
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: None)
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert heals == []
        created_ts = health._worker_container_state[("10.0.0.2", 8000)]["created_ts"]
        # Simulate a daemon restart: in-memory debounce + container state wiped,
        # ONLY the on-disk durable record survives. Probe still unknown.
        health._worker_down_streak.clear()
        health._worker_container_state.clear()
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert heals == []           # still bounded-shielded after restart
        # Bounded: once the youthful window elapses, a genuinely dead unit heals.
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        expired = created_ts - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": expired, "has_served": False}
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: expired)
        health._worker_down_streak.clear()
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert len(heals) == 1       # grace elapsed → genuine failure → heal
        assert heals[0][2] == "10.0.0.2"

    # ── F1 (HEAL_CASCADE 2026-10-14): the GENTLE relaunch path shares the grace ──
    #
    # The pre-fix code had the load grace ONLY on the force-recreate path. The
    # plain `Worker X down — relaunching` path was guarded solely by
    # `_worker_relaunch_at`, which records relaunches THAT path issued — so a
    # container created by engine-wedge recovery, `cluster up` or a template
    # apply was invisible to it and got destroyed mid-load, every check. The
    # incident log shows exactly that: template apply recreated all 4 units at
    # 07:21:45 and 07:24:29 check_workers relaunched a unit created 2m44s
    # earlier, with a ~3 min model load ahead of it.

    def _relaunch_recorder(self, health, monkeypatch):
        """Record the gentle relaunches (stop+Popen) without touching a node."""
        relaunches = []
        monkeypatch.setattr(
            health.subprocess, "Popen",
            lambda argv, **k: relaunches.append(list(argv)) or None)
        return relaunches

    def test_relaunch_shares_grace_for_container_started_by_another_path(
            self, tmp_hfcc_dir, monkeypatch):
        """A unit created by ANOTHER path (age known, never served) must not be
        relaunched by check_workers, even though this path never relaunched it.
        This is the cascade bug: `_worker_relaunch_at` is empty, so the pre-fix
        guard let the relaunch through and destroyed the load."""
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        relaunches = self._relaunch_recorder(health, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        # Container is 60s old (well inside the grace) and has never served.
        young = _t.time() - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: young)
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": young, "has_served": False}
        for _ in range(3):                      # several 60s checks
            health.check_workers()
        assert relaunches == []                 # mid-load -> no destroy
        assert heals == []                      # and still no force-recreate
        # The unit is honestly reported down (not silently "online").
        from hscc_daemon import state as state_mod
        state = state_mod.read_state("workers") or {}
        assert "w-10.0.0.2" in state.get("down", [])

    def test_relaunch_still_fires_once_grace_expires(self, tmp_hfcc_dir, monkeypatch):
        """The grace is bounded: an OLD never-served container is a genuine
        failure and the gentle path must still repair it (no stranding)."""
        import time as _t
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        relaunches = self._relaunch_recorder(health, monkeypatch)
        monkeypatch.setattr(health, "_autoheal_worker_fn", lambda *a: {"status": "ok"})
        old = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: old)
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": old, "has_served": False}
        health.check_workers()
        assert len(relaunches) == 1

    def test_relaunch_not_blocked_when_no_container_evidence(
            self, tmp_hfcc_dir, monkeypatch):
        """The gentle path is fail-OPEN on unknown age (unlike the destructive
        force-recreate path): with no container at all there is no load to
        destroy, and shielding would strand a dead unit for 20 minutes."""
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        relaunches = self._relaunch_recorder(health, monkeypatch)
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: None)
        health.check_workers()
        assert len(relaunches) == 1

    def test_force_recreate_path_still_fails_closed(
            self, tmp_hfcc_dir, monkeypatch):
        """The destructive path keeps its fail-CLOSED behaviour after F1's
        rename/flag split: unknown age → no force-recreate."""
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        self._relaunch_recorder(health, monkeypatch)
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: None)
        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE * 2):
            health.check_workers()
        assert heals == []

    # ── F2 (HEAL_CASCADE 2026-10-14): both check_workers repair paths honour
    #    the heal lease ──────────────────────────────────────────────────────

    def test_relaunch_defers_when_another_repair_holds_the_lease(
            self, tmp_hfcc_dir, monkeypatch):
        """Path (b) must stand down while, say, engine-wedge recovery is running
        `hscc cluster up` — the incident had both acting on one unit."""
        from hscc_daemon import heal_lease
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        relaunches = self._relaunch_recorder(health, monkeypatch)
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: None)     # dead, no container
        assert heal_lease.acquire(
            "engine-wedge-recovery", "10.0.0.2:8000")[0] is True

        health.check_workers()
        assert relaunches == []
        assert heal_lease.held_by("engine-wedge-recovery") is True

    def test_autoheal_defers_when_another_repair_holds_the_lease(
            self, tmp_hfcc_dir, monkeypatch):
        """Path (d) — the whole-template force-recreate, the most destructive of
        the four — must not fire while another repair is in flight."""
        from hscc_daemon import heal_lease
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        heals = self._heals_recorder(health, monkeypatch)
        self._relaunch_recorder(health, monkeypatch)
        import time as _t
        old = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: old)
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": old, "has_served": False}
        assert heal_lease.acquire("proxy-relaunch", "proxy :4000")[0] is True

        for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
            health.check_workers()
        assert heals == []

    def test_lease_released_after_each_repair(self, tmp_hfcc_dir, monkeypatch):
        """Neither path may leak its lease: a leaked one suppresses every other
        repair for the whole TTL."""
        from hscc_daemon import heal_lease
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        self._relaunch_recorder(health, monkeypatch)
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: None)
        health.check_workers()
        assert heal_lease.held_by() is False

    def test_lease_released_when_the_heal_action_raises(self, tmp_hfcc_dir, monkeypatch):
        """Release sits in a ``finally`` around the injectable heal call."""
        from hscc_daemon import heal_lease
        health = self._setup(tmp_hfcc_dir, monkeypatch)
        self._relaunch_recorder(health, monkeypatch)
        import time as _t
        old = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: old)
        health._worker_container_state[("10.0.0.2", 8000)] = {
            "created_ts": old, "has_served": False}

        def boom(*a, **k):
            raise RuntimeError("template apply exploded")

        monkeypatch.setattr(health, "_autoheal_worker_fn", boom)
        with pytest.raises(RuntimeError):
            for _ in range(health.WORKER_AUTOHEAL_DEBOUNCE):
                health.check_workers()
        assert heal_lease.held_by() is False

    def test_sparkrun_created_map_builds_host_port_launch(self, monkeypatch):
        """_sparkrun_created_map() turns a ClusterStatusResult.to_dict()-shaped
        dict into a (host, port) → launch-epoch map — the signal the grace keys
        on. Groups span multiple hosts (one shared launch epoch) and solo
        entries map via their own host."""
        import time as _t
        from hscc_daemon import health
        now = _t.time()
        fake = {
            "groups": {
                "cid_a": {
                    "meta": {"port": 8000, "started_at": now - 100},
                    "containers": [
                        {"host": "10.0.0.2", "role": "node_0", "status": "Up",
                         "image": "img"},
                        {"host": "10.0.0.3", "role": "node_1", "status": "Up",
                         "image": "img"},
                    ],
                },
            },
            "solo_entries": [
                {"cluster_id": "cid_b", "host": "10.0.0.4",
                 "meta": {"port": 8001, "started_at": now - 50},
                 "status": "Up", "image": "img"},
            ],
        }
        monkeypatch.setattr(health, "_WORKER_CREATED_CACHE_AT", 0.0)
        monkeypatch.setattr(health, "_WORKER_CREATED_CACHE", {})
        # Stub the sweep so the map is built purely from the injected JSON.
        monkeypatch.setattr(health, "_SPARKRUN_STATUS_SCRIPT", "stubbed")
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: "/py")
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True, "output": json.dumps(fake)})
        m = health._sparkrun_created_map()
        assert m[("10.0.0.2", 8000)] == now - 100   # group span member
        assert m[("10.0.0.3", 8000)] == now - 100   # shared launch epoch
        assert m[("10.0.0.4", 8001)] == now - 50    # solo entry
        assert ("10.0.0.2", 8001) not in m          # wrong port doesn't match

    def test_default_created_ts_resolves_remote_via_sparkrun(self, monkeypatch):
        """The REAL _default_worker_container_created_ts resolves the remote
        unit's container age through sparkrun (never local docker) — the exact
        inert-grace regression the card reports. Injecting the (host,port)→
        launch map must feed the lookup directly."""
        import time as _t
        from hscc_daemon import health
        now = _t.time()
        monkeypatch.setattr(health, "_WORKER_CREATED_CACHE_AT", 0.0)
        monkeypatch.setattr(
            health, "_WORKER_CREATED_CACHE",
            {("10.0.0.7", 8000): now - 30, ("10.0.0.8", 8000): now - 5})
        monkeypatch.setattr(health, "_sparkrun_created_map",
                            lambda: health._WORKER_CREATED_CACHE)
        ts = health._default_worker_container_created_ts("10.0.0.7", 8000)
        assert ts == now - 30
        # A node the map doesn't know → unknown → None (caller fails closed).
        assert health._default_worker_container_created_ts("10.0.0.9", 8000) is None


class TestCheckProxy:
    """check_proxy() keeps the worker load-balancer alive.

    t_proxy_outage_20261011: health is a BODY verdict, not a status code.
    These stub _proxy_models_probe (never the live :4000) and stub
    serving.load_serving (never the operator's live serving.json — the same
    ambient-state discipline as test_doctor's _HOME_ENV guard).

    F4 added a SECOND live probe (_proxy_backend_probe -> /v1/model/info, plus
    a /health sweep of the fleet). _setup stubs BOTH by default so no test in
    this class can reach the operator's running proxy or nodes: the backend
    probe defaults to "could not ask the proxy", which is the documented
    fail-open (drift check skipped) and preserves every pre-F4 expectation
    exactly. Tests that exercise drift pass `backend=` explicitly.
    """

    _SERVING_NO_UNITS = {"version": 2, "units": []}

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes, probe=None,
               serving_data=_SERVING_NO_UNITS, backend=None,
               healthy_units=None, fleet_hp=None):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", set(nodes))
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: serving_data)
        if probe is not None:
            monkeypatch.setattr(health, "_proxy_models_probe",
                                lambda url, timeout=5: dict(probe))
        # F4: never probe the live proxy or the real nodes from a test.
        if backend is None:
            backend = {"ok": False, "backends": set(),
                       "error": "stubbed — drift check skipped"}
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: dict(backend))
        if healthy_units is not None:
            monkeypatch.setattr(
                health, "_fleet_unit_backends",
                lambda serving_data=None, probe=None:
                    (set(healthy_units), set(fleet_hp or healthy_units)))
        return health

    def test_no_workers_noop(self, tmp_hfcc_dir, monkeypatch):
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=[])
        assert health.check_proxy() is True

    # ── F4 (HEAL_CASCADE 2026-10-14): live router vs. actually-healthy units ──

    def test_backend_probe_parses_live_api_base_set(self):
        """_proxy_backend_probe must read the LIVE router out of
        /v1/model/info's litellm_params.api_base — /v1/models cannot show this,
        which is exactly why the orphan routing everything to one node looked
        healthy for two hours."""
        from hscc_daemon import health
        body = {
            "data": [
                {"model_name": "worker-model",
                 "litellm_params": {"api_base": "http://10.0.0.2:8001/v1"}},
                {"model_name": "orchestrator-model",
                 "litellm_params": {"api_base": "http://10.0.0.2:8001"}},
                {"model_name": "no-params"},
            ]}
        import io
        class _FakeResp:
            def __init__(self, b): self._b = b
            def read(self): return self._b        # urlopen().read() -> bytes
            def __enter__(self): return self
            def __exit__(self, *a): return False
        payload = json.dumps(body).encode()
        monkey_fake = lambda url, timeout=5: _FakeResp(payload)
        orig = health.urllib.request.urlopen
        health.urllib.request.urlopen = monkey_fake
        try:
            res = health._proxy_backend_probe("http://x/v1/model/info")
        finally:
            health.urllib.request.urlopen = orig
        assert res["ok"] is True
        assert res["backends"] == {"10.0.0.2:8001"}

    def test_backend_probe_failure_is_not_drift(self):
        """"Could not ask the proxy" must NOT be read as drift — failing closed
        here would restart a working LB on any endpoint change."""
        from hscc_daemon import health
        assert health._proxy_backend_drift(set(), {"10.0.0.2:8001"},
                                           {"10.0.0.2:8001"})[0] is True

    def test_drift_missing_backend_is_unhealthy(self):
        """THE INCIDENT SHAPE: the catalogue serves every alias, but all of it
        routes through ONE backend while three units are healthy."""
        from hscc_daemon import health
        ok, reason = health._proxy_backend_drift(
            {"10.0.0.2:8001"},                              # live router
            {"10.0.0.2:8001", "10.0.0.3:8002", "10.0.0.4:8003"},  # healthy
            {"10.0.0.2:8001", "10.0.0.3:8002", "10.0.0.4:8003"})
        assert ok is False
        assert "10.0.0.3:8002" in reason and "10.0.0.4:8003" in reason

    def test_drift_dead_backend_is_unhealthy(self):
        """A router pointing at a unit that is NOT healthy is sending load to a
        dead worker — unhealthy even when every healthy unit is covered."""
        from hscc_daemon import health
        ok, reason = health._proxy_backend_drift(
            {"10.0.0.2:8001", "10.0.0.9:8099"},
            {"10.0.0.2:8001"},
            {"10.0.0.2:8001", "10.0.0.9:8099"})
        assert ok is False and "10.0.0.9:8099" in reason

    def test_drift_ignores_backends_hscc_does_not_supervise(self):
        """The proxy may legitimately serve endpoints outside serving.json. They
        must never make us relaunch the LB forever."""
        from hscc_daemon import health
        ok, _ = health._proxy_backend_drift(
            {"10.0.0.2:8001", "10.9.9.9:9000"},
            {"10.0.0.2:8001"},
            {"10.0.0.2:8001"})
        assert ok is True

    def test_drift_matches_is_healthy(self):
        from hscc_daemon import health
        hp = {"10.0.0.2:8001", "10.0.0.3:8002"}
        ok, _ = health._proxy_backend_drift(hp, hp, hp)
        assert ok is True

    def test_healthy_aliases_but_single_backend_triggers_repair(
            self, tmp_hfcc_dir, monkeypatch):
        """check_proxy must treat catalogue-healthy + single-backend as
        UNHEALTHY and take the repair path, not the plain `proxy start` that
        refuses a running proxy."""
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"],
            probe={"ok": True, "models": ["worker-model"], "error": ""},
            serving_data={"version": 2, "units": [
                {"id": "w1", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.2"], "port": 8001, "recipe": "r"},
                {"id": "w2", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.3"], "port": 8002, "recipe": "r"},
            ]},
            backend={"ok": True, "backends": {"10.0.0.2:8001"}, "error": ""},
            healthy_units={"10.0.0.2:8001", "10.0.0.3:8002"},
            fleet_hp={"10.0.0.2:8001", "10.0.0.3:8002"})
        calls = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda a, **k: calls.append(list(a)) or {"ok": True})
        monkeypatch.setattr(health, "_proxy_repair_orphan",
                            lambda hosts: calls.append(["REPAIR", list(hosts)])
                            or {"ok": True, "verified": True})
        assert health.check_proxy() is True
        assert any(c and c[0] == "REPAIR" for c in calls), \
            f"drift must take the repair path, got {calls}"
        from hscc_daemon import state as state_mod
        assert (state_mod.read_state("proxy") or {}).get("backend_drift") is True

    def test_drift_repair_does_not_use_plain_start(self, tmp_hfcc_dir, monkeypatch):
        """The repair must never fall back to plain `proxy start`: sparkrun
        REFUSES a running proxy (rc 1), which is how the incident persisted."""
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"],
            probe={"ok": True, "models": ["worker-model"], "error": ""},
            backend={"ok": True, "backends": {"10.0.0.2:8001"}, "error": ""},
            healthy_units={"10.0.0.2:8001", "10.0.0.3:8002"})
        started = []
        monkeypatch.setattr(health, "_proxy_repair_orphan",
                            lambda hosts: {"ok": True, "verified": True})
        monkeypatch.setattr(
            health, "run_cmd",
            lambda a, **k: started.append(list(a)) or {"ok": True})
        health.check_proxy()
        for c in started:
            assert not ("start" in c and "--restart" not in c
                        and "stop" not in c and "status" not in c), \
            f"plain `proxy start` used despite drift: {c}"

    # ── F4b: node reboot -> proxy MUST re-add the returned unit ──────────────

    def test_returned_unit_is_readded_within_one_sweep(
            self, tmp_hfcc_dir, monkeypatch):
        """THE OPERATOR'S PATH: power-cycle a node while the proxy runs. Its unit
        leaves (proxy drops it), comes back healthy, and the RUNNING proxy never
        re-adds it — sparkrun's auto-discover rewrites the config file but skips
        the restart because is_running() only checks its own state-file PID.
        One check_proxy sweep must detect that and repair."""
        healthy_units = {"10.0.0.2:8001", "10.0.0.3:8002"}
        # Sweep 1: node .3 is down for reboot -> proxy legitimately has 1 backend.
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"],
            probe={"ok": True, "models": ["worker-model"], "error": ""},
            backend={"ok": True, "backends": {"10.0.0.2:8001"}, "error": ""},
            healthy_units={"10.0.0.2:8001"})
        monkeypatch.setattr(health, "run_cmd", lambda a, **k: {"ok": True})
        monkeypatch.setattr(health, "_proxy_repair_orphan", lambda hosts: {"ok": True})
        assert health.check_proxy() is True          # no demand on a rebooting node

        # Sweep 2: .3 is back and healthy, but the live router still has only .2.
        health2 = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"],
            probe={"ok": True, "models": ["worker-model"], "error": ""},
            backend={"ok": True, "backends": {"10.0.0.2:8001"}, "error": ""},
            healthy_units=healthy_units)
        repaired = []
        monkeypatch.setattr(health2, "_proxy_repair_orphan",
                            lambda hosts: repaired.append(list(hosts))
                            or {"ok": True, "verified": True})
        monkeypatch.setattr(health2, "run_cmd", lambda a, **k: {"ok": True})
        health2.check_proxy()
        assert repaired, "returned unit was not re-added within one sweep"

    # ── F4: orphan reap guards ───────────────────────────────────────────────

    def _reap_setup(self, monkeypatch, health, listener, tracked, killed):
        monkeypatch.setattr(health, "_proxy_listener", lambda: listener)
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: tracked)
        monkeypatch.setattr(health.os, "kill",
                            lambda pid, sig: killed.append((pid, sig)))

    def test_reap_refuses_tracked_pid(self, tmp_hfcc_dir, monkeypatch):
        """The tracked PID is sparkrun's own proxy — never kill it here."""
        from hscc_daemon import health
        killed = []
        self._reap_setup(monkeypatch, health, (555, "litellm --config x"), 555, killed)
        assert health._reap_orphan_proxy_listener(555) is False
        assert killed == []

    def test_reap_refuses_non_litellm_cmdline(self, tmp_hfcc_dir, monkeypatch):
        """Blast-radius contract: an unrecognised listener on :4000 is reported
        and left alone, same rule as the orphaned-serve heal."""
        from hscc_daemon import health
        killed = []
        self._reap_setup(monkeypatch, health, (777, "/usr/bin/some-other-server"),
                         None, killed)
        assert health._reap_orphan_proxy_listener(None) is False
        assert killed == []

    def test_reap_kills_untracked_litellm_orphan(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        killed = []
        state = {"listener": (999, "python -m litellm --config /tmp/x.yaml")}
        monkeypatch.setattr(health, "_proxy_listener", lambda: state["listener"])
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: 111)
        def fake_kill(pid, sig):
            killed.append((pid, sig))
            state["listener"] = (None, None)      # dies on first signal
        monkeypatch.setattr(health.os, "kill", fake_kill)
        assert health._reap_orphan_proxy_listener(111) is True
        assert killed and killed[0][0] == 999

    def test_repair_verifies_port_owner_is_tracked_pid(
            self, tmp_hfcc_dir, monkeypatch):
        """exit 0 is not success. `proxy start --restart` exited fine during the
        incident while the orphan kept :4000 and the new instance bound :1708."""
        from hscc_daemon import health
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: 222)
        monkeypatch.setattr(health, "_proxy_listener", lambda: (333, "litellm"))
        monkeypatch.setattr(health, "_reap_orphan_proxy_listener", lambda t: True)
        monkeypatch.setattr(health, "run_cmd", lambda a, **k: {"ok": True})
        res = health._proxy_repair_orphan(["10.0.0.2"])
        assert res["verified"] is False and res["ok"] is False

    def test_repair_orders_stop_then_reap_then_start(
            self, tmp_hfcc_dir, monkeypatch):
        """Stop before reaping, verify after starting — the order that makes
        `--restart` unnecessary (and which --restart could never satisfy).

        Sequence labels are derived from the SUBCOMMAND TOKEN inside each argv
        (`sparkrun_bin.argv` prefixes the resolved binary path, so argv[0] is
        never "stop"/"start" — asserting on argv[0] would pass vacuously).
        """
        from hscc_daemon import health
        seq = []
        monkeypatch.setattr(health, "_proxy_tracked_pid",
                            lambda: seq.append("tracked") or 222)
        monkeypatch.setattr(
            health, "run_cmd",
            lambda a, **k: seq.append(list(a)) or {"ok": True})
        monkeypatch.setattr(
            health, "_reap_orphan_proxy_listener",
            lambda t: seq.append("reap") or True)
        monkeypatch.setattr(health, "_proxy_listener",
                            lambda: seq.append("listen") or (222, "litellm"))
        res = health._proxy_repair_orphan(["10.0.0.2", "10.0.0.3"])
        assert res["ok"] is True and res["verified"] is True

        kinds = []
        for c in seq:
            if c == "reap":
                kinds.append("reap")
            elif isinstance(c, list) and "proxy" in c:
                if "stop" in c:
                    kinds.append("stop")
                elif "start" in c:
                    kinds.append("start")
        assert kinds == ["stop", "reap", "start"], \
            f"repair sequence must be stop -> reap -> start, got {kinds}"
        start = [c for c in seq if isinstance(c, list) and "start" in c][0]
        assert "--restart" not in start, \
            "--restart must not be used: it only SIGTERMs sparkrun's own tracked PID"
        assert "10.0.0.2,10.0.0.3" in start

    def test_proxy_healthy(self, tmp_hfcc_dir, monkeypatch):
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"],
            probe={"ok": True, "models": ["worker-model"], "error": ""})
        ran = []
        monkeypatch.setattr(health, "run_cmd", lambda *a, **k: ran.append(a) or {"ok": True})
        assert health.check_proxy() is True
        assert ran == []                         # healthy -> no relaunch

    # ── t_proxy_outage_20261011 PR #22 review: durable relaunch cooldown ───

    def _unhealthy_setup(self, tmp_hfcc_dir, monkeypatch):
        """Running proxy (probe ok) whose reachable worker alias is missing —
        the shape that would otherwise relaunch-spam every 60s tick. The stub
        run_cmd models reality: plain `sparkrun proxy start` REFUSES a running
        proxy (engine.py is_running -> rc 1), so attempts come back failed."""
        serving_data = {"version": 2, "units": [
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"], probe={
                "ok": True, "models": ["some-model"], "error": ""},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": False})
        return health, ran

    def test_failed_relaunch_holds_cooldown(self, tmp_hfcc_dir, monkeypatch):
        """Tick 1 attempts + stamps; tick 2 within the window must NOT invoke
        sparkrun again, and the state must say why (retry-in), not re-attempt.
        The stamp lives in the STATE FILE — a daemon restart reads the same
        file (durability requirement: no in-memory cooldown)."""
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        assert health.check_proxy() is False     # attempt 1 refused (rc 1)
        assert len(ran) == 1
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        stamp1 = state["last_relaunch"]
        assert stamp1
        assert health.check_proxy() is False     # attempt 2 = suppressed
        assert len(ran) == 1                     # NO second sparkrun call
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert "cooldown" in state["message"]
        # A suppressed tick must NOT re-stamp: extending the window every tick
        # would make the cooldown perpetual (never retries, never recovers).
        assert state["last_relaunch"] == stamp1

    def test_cooldown_survives_state_reload(self, tmp_hfcc_dir, monkeypatch):
        """Fresh health-module semantics only via the state file: helper call
        after a simulated daemon restart (state file kept, no process-level
        memory) must still see the stamp."""
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        health.check_proxy()
        assert len(ran) == 1
        since = health._proxy_relaunch_cooldown_state()
        assert since is not None and 0 <= since < 60

    def test_cooldown_expires_and_retry_proceeds(self, tmp_hfcc_dir, monkeypatch):
        """Backdate the stamp past the window -> next tick attempts again."""
        import datetime
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        health.check_proxy()
        assert len(ran) == 1
        state_path = tmp_hfcc_dir / "state" / "proxy.json"
        state = json.loads(state_path.read_text())
        assert state["last_relaunch"]            # attempt 1 stamped
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=2 * health.PROXY_RELAUNCH_COOLDOWN_S + 5)
                 ).isoformat()
        state["last_relaunch"] = stale
        state_path.write_text(json.dumps(state))
        assert health.check_proxy() is False
        assert len(ran) == 2                     # window elapsed -> attempt again

    # ── F2 (HEAL_CASCADE 2026-10-14): the proxy relaunch honours the heal lease ──

    def test_relaunch_deferred_while_another_repair_holds_lease(
            self, tmp_hfcc_dir, monkeypatch):
        """A repair in flight elsewhere (here: auto-heal) must stop check_proxy
        from SIGTERMing the LB underneath it — the incident's path (c)."""
        from hscc_daemon import heal_lease
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        assert heal_lease.acquire("auto-heal", "10.0.0.2:8001")[0] is True

        assert health.check_proxy() is False
        assert ran == []                         # sparkrun NEVER invoked

        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert state["deferred"] is True
        # A deferral is not a failed relaunch: no alert may fire off it.
        assert state["relaunch_failed"] is False
        assert "auto-heal" in state["message"]   # says what is in flight

    def test_deferral_does_not_extend_the_cooldown(self, tmp_hfcc_dir, monkeypatch):
        """Same rule as a suppressed tick: re-stamping on every deferral would
        make the cooldown perpetual, so a blocked proxy would never recover."""
        from hscc_daemon import heal_lease
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        health.check_proxy()                     # attempt 1, stamps
        assert len(ran) == 1
        state_path = tmp_hfcc_dir / "state" / "proxy.json"
        stamp1 = json.loads(state_path.read_text())["last_relaunch"]
        assert stamp1

        # Backdate past the window so the cooldown is NOT what stops tick 2,
        # then hold the lease: the deferral must not re-stamp.
        import datetime
        stale = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(seconds=2 * health.PROXY_RELAUNCH_COOLDOWN_S + 5)
                 ).isoformat()
        state = json.loads(state_path.read_text())
        state["last_relaunch"] = stale
        state_path.write_text(json.dumps(state))
        assert heal_lease.acquire("worker-relaunch", "10.0.0.2:8001")[0] is True

        health.check_proxy()
        assert len(ran) == 1                     # no NEW sparkrun call (still 1)
        assert json.loads(state_path.read_text())["last_relaunch"] == stale

    def test_lease_released_after_relaunch_attempt(self, tmp_hfcc_dir, monkeypatch):
        """The lease must not outlive the attempt it guarded — a leaked lease
        would suppress every other repair for the TTL."""
        from hscc_daemon import heal_lease
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        health.check_proxy()
        assert len(ran) == 1
        assert heal_lease.held_by() is False

    def test_lease_released_even_when_the_relaunch_raises(
            self, tmp_hfcc_dir, monkeypatch):
        """The release lives in a ``finally``: an exception from run_cmd must
        not leak the lease."""
        from hscc_daemon import heal_lease
        health, _ = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("sparkrun exploded")

        monkeypatch.setattr(health, "run_cmd", boom)
        with pytest.raises(RuntimeError):
            health.check_proxy()
        assert heal_lease.held_by() is False

    def test_successful_relaunch_still_stamps_and_healthy_clears(self, tmp_hfcc_dir, monkeypatch):
        """PR #22 round 2 semantics: a relaunch whose `proxy start` EXITS 0
        still stamps the window (an ok exit over still-empty discovery is the
        10-11 shape). The cooldown is cleared by the HEALTHY branch: once a
        probe comes back good, its state rewrite drops the stamp, so a later
        genuine down-proxy recovers on its next tick."""
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True      # start exited 0
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert state["relaunched"] is True
        assert state["last_relaunch"]            # EVERY attempt stamps
        # tick 2 still unhealthy -> suppressed (the round-2 gap closed)
        assert health.check_proxy() is False
        assert len(ran) == 1
        # tick 3: probe recovered -> healthy rewrite clears the stamp
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": True, "models": ["worker-model"], "error": ""})
        assert health.check_proxy() is True
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert "last_relaunch" not in state
        assert health._proxy_relaunch_cooldown_state() is None

    def test_ok_start_still_unhealthy_next_tick_is_suppressed(self, tmp_hfcc_dir, monkeypatch):
        """PR #22 round 2 (the exact 2026-10-11 shape): `proxy start` returns
        ok, but discovery is still empty so the NEXT probe is still unhealthy.
        The second tick must be SUPPRESSED by the cooldown, not relaunch —
        stamping only failures let this loop run every tick."""
        health, ran = self._unhealthy_setup(tmp_hfcc_dir, monkeypatch)
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        # tick 1: unhealthy -> attempt -> start ok (stamp written regardless)
        assert health.check_proxy() is True
        assert len(ran) == 1
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert state["last_relaunch"]
        # tick 2: probe STILL unhealthy (empty discovery) -> cooldown suppresses
        assert health.check_proxy() is False
        assert len(ran) == 1                     # NO second start
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert "cooldown" in state["message"]
        # the suppressed tick did not extend the window
        assert state["last_relaunch"]

    def test_cooldown_state_reader_tolerates_garbage(self):
        from hscc_daemon import health
        import hscc_daemon.state as st
        orig = st.read_state
        try:
            st.read_state = lambda name: {"last_relaunch": "not-a-timestamp"}
            assert health._proxy_relaunch_cooldown_state() is None
            st.read_state = lambda name: None
            assert health._proxy_relaunch_cooldown_state() is None
            st.read_state = lambda name: "not a dict"
            assert health._proxy_relaunch_cooldown_state() is None
        finally:
            st.read_state = orig

    def test_proxy_down_relaunched(self, tmp_hfcc_dir, monkeypatch):
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"],
            probe={"ok": False, "models": [], "error": "connection refused"})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True
        cmd = ran[0]
        # argv[0] is the resolved absolute sparkrun CLI (t_b543e530) — match on
        # basename + subcommands, not the literal bare name.
        assert os.path.basename(cmd[0]) == "sparkrun"
        assert cmd[1:3] == ["proxy", "start"]
        assert "10.0.0.2,10.0.0.3" in cmd

    # ── t_proxy_outage_20261011 bug 1: HTTP 200 + empty catalogue ──────────

    def test_empty_catalogue_is_unhealthy_and_relaunches(self, tmp_hfcc_dir, monkeypatch):
        """THE outage shape: proxy answers 200 with data: [] — must relaunch."""
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"],
            probe={"ok": True, "models": [], "error": ""})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True
        assert len(ran) == 1                     # empty body = down
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert "empty" in state["reason"]

    def _relaunch_args(self, tmp_hfcc_dir, monkeypatch, probe, serving_data=None):
        kw = {} if serving_data is None else {"serving_data": serving_data}
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"],
                             probe=probe, **kw)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(list(args)) or {"ok": True})
        health.check_proxy()
        assert len(ran) == 1
        return ran[0]

    def test_empty_catalogue_relaunch_passes_restart(self, tmp_hfcc_dir, monkeypatch):
        """A LIVE proxy serving nothing: plain `proxy start` refuses a running
        proxy (exit 1), so without --restart the outage shape is detected but
        never healed. Empty catalogue = no traffic to drop -> --restart."""
        args = self._relaunch_args(tmp_hfcc_dir, monkeypatch,
                                   {"ok": True, "models": [], "error": ""})
        assert "--restart" in args
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert state["restart"] is True

    def test_partial_catalogue_relaunch_never_restarts(self, tmp_hfcc_dir, monkeypatch):
        """One alias missing but others served: a restart would drop live
        traffic, so the relaunch stays a plain start."""
        serving_data = {"version": 2, "units": [
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
        ]}
        args = self._relaunch_args(
            tmp_hfcc_dir, monkeypatch,
            {"ok": True, "models": ["some-model"], "error": ""}, serving_data)
        assert "--restart" not in args

    def test_unreachable_proxy_relaunch_never_restarts(self, tmp_hfcc_dir, monkeypatch):
        """Unreachable = not running: plain start brings it up."""
        args = self._relaunch_args(
            tmp_hfcc_dir, monkeypatch,
            {"ok": False, "models": [], "error": "connection refused"})
        assert "--restart" not in args

    def test_non_uniform_worker_only_catalogue_is_healthy(self, tmp_hfcc_dir, monkeypatch):
        """PR #22 review (blocking): non-uniform fleets point `proxy start
        --hosts` at keep-alive WORKER nodes only, so orchestrator-model is NOT
        something the proxy can serve — a healthy worker-model catalogue must
        be HEALTHY and must NOT relaunch. Requiring orchestrator-model here
        made every check tick relaunch a proxy that could never satisfy it."""
        serving_data = {"version": 2, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"], probe={
                "ok": True, "models": ["some-model", "worker-model"], "error": ""},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True
        assert ran == []                         # NO relaunch — proxy serves all it can
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert state["ok"] is True

    def test_non_uniform_reachable_worker_alias_missing_relaunches(self, tmp_hfcc_dir, monkeypatch):
        """The genuine version of the old test: the worker unit IS reachable
        (its node is in the host set) and its alias is missing → unhealthy →
        relaunch. The bug was never 'orchestrator alias missing'; it is any
        REACHABLE unit's alias missing."""
        serving_data = {"version": 2, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"], probe={
                "ok": True, "models": ["some-model"], "error": ""},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True      # attempt made and (stub) succeeded
        assert len(ran) == 1                     # relaunch WAS invoked
        state = json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())
        assert "worker-model" in state["reason"]
        assert state["relaunched"] is True

    def test_uniform_pool_requires_both_aliases(self, tmp_hfcc_dir, monkeypatch):
        serving_data = {"version": 2, "uniform_pool": True, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"], probe={
                "ok": True, "models": ["m", "orchestrator-model"], "error": ""},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True
        assert len(ran) == 1                     # worker-model missing -> relaunch

    def test_both_aliases_served_is_healthy(self, tmp_hfcc_dir, monkeypatch):
        serving_data = {"version": 2, "uniform_pool": True, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"], probe={
                "ok": True, "models": ["m", "worker-model", "orchestrator-model"],
                "error": ""},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd", lambda *a, **k: ran.append(a) or {"ok": True})
        assert health.check_proxy() is True
        assert ran == []

    # ── t_proxy_outage_20261011 bug 2: uniform-pool relaunch hosts ─────────

    def test_relaunch_includes_orchestrator_under_uniform_pool(self, tmp_hfcc_dir, monkeypatch):
        """KEEPALIVE_NODES excludes the orchestrator node BY DESIGN; under a
        uniform pool the relaunch must union it back, or the LB can never
        re-add the orchestrator endpoint (the 2026-10-11 outage)."""
        serving_data = {"version": 2, "uniform_pool": True, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
            {"id": "w2", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.3"], "port": 8002},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"],
            probe={"ok": False, "models": [], "error": "down"},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        health.check_proxy()
        cmd = ran[0]
        assert "10.0.0.1,10.0.0.2,10.0.0.3" in cmd   # orchestrator node included

    def test_uniform_relaunch_includes_nonkeepalive_workers(self, tmp_hfcc_dir, monkeypatch):
        """ALL serving nodes means ALL: a uniform pool may legitimately carry
        keepalive:false workers; they serve either role and the LB must know
        about them (KEEPALIVE_NODES alone would drop them)."""
        serving_data = {"version": 2, "uniform_pool": True, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
            {"id": "w2", "role": "worker", "keepalive": False,
             "nodes": ["10.0.0.4"], "port": 8003},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"],   # keepalive set is w1 only
            probe={"ok": False, "models": [], "error": "down"},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        health.check_proxy()
        cmd = ran[0]
        assert cmd[cmd.index("--hosts") + 1] == "10.0.0.1,10.0.0.2,10.0.0.4"

    def test_relaunch_non_uniform_excludes_orchestrator(self, tmp_hfcc_dir, monkeypatch):
        """Non-uniform behaviour UNCHANGED: worker nodes only (historical)."""
        serving_data = {"version": 2, "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"], "port": 8000},
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.2"], "port": 8001},
        ]}
        health = self._setup(
            tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"],
            probe={"ok": False, "models": [], "error": "down"},
            serving_data=serving_data)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        health.check_proxy()
        cmd = ran[0]
        assert cmd[cmd.index("--hosts") + 1] == "10.0.0.2"   # no orchestrator union

    # ── pure-function units (the health VERDICT itself) ─────────────────────

    def test_relaunch_hosts_units_union_and_env_override(self):
        """Direct unit: uniform pool = every unit node ∪ KEEPALIVE_NODES (so an
        env-only HSCC_KEEPALIVE_NODES with no serving.json still reaches the
        fleet); non-uniform = KEEPALIVE_NODES exactly."""
        from hscc_daemon import health, serving
        original = serving.KEEPALIVE_NODES
        serving.KEEPALIVE_NODES = {"10.0.0.2", "10.0.0.9"}
        try:
            assert health._proxy_relaunch_hosts(
                {"uniform_pool": True, "units": [
                    {"role": "orchestrator", "nodes": ["10.0.0.1"]},
                    {"role": "worker", "nodes": ["10.0.0.2"]}]}) == \
                ["10.0.0.1", "10.0.0.2", "10.0.0.9"]
            # non-uniform: env/keepalive set only, unit nodes NOT unioned
            assert health._proxy_relaunch_hosts(
                {"units": [{"role": "orchestrator", "nodes": ["10.0.0.1"]}]}) == \
                ["10.0.0.2", "10.0.0.9"]
        finally:
            serving.KEEPALIVE_NODES = set()

    def test_models_healthy_verdicts(self):
        from hscc_daemon.health import _proxy_models_healthy
        # no aliases configured: >=1 model required, empty is unhealthy
        assert _proxy_models_healthy({"ok": True, "models": []}, set())[0] is False
        assert _proxy_models_healthy({"ok": True, "models": ["x"]}, set())[0] is True
        # failed probe never healthy regardless of models
        assert _proxy_models_healthy({"ok": False, "models": ["x"]}, {"worker-model"})[0] is False
        # alias requirement is exact-union, subset is unhealthy
        ok, reason = _proxy_models_healthy(
            {"ok": True, "models": ["a", "worker-model"]},
            {"worker-model", "orchestrator-model"})
        assert ok is False and "orchestrator-model" in reason
        ok, _ = _proxy_models_healthy(
            {"ok": True, "models": ["a", "worker-model", "orchestrator-model"]},
            {"worker-model", "orchestrator-model"})
        assert ok is True

    def test_expected_aliases_from_serving(self, monkeypatch):
        from hscc_daemon import health, serving
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: None)
        assert health._proxy_expected_aliases(None) == set()
        # PR #22 review: alias expectation is scoped to the relaunch host set
        # (what `proxy start --hosts` points the proxy at). Worker on a host
        # the proxy reaches -> worker-model expected; orchestrator-only unit
        # on a host it cannot reach -> orchestrator-model NOT expected.
        assert health._proxy_expected_aliases(
            {"units": [{"role": "worker", "nodes": ["10.0.0.2"]},
                       {"role": "orchestrator", "nodes": ["10.0.0.1"]}]},
            relaunch_hosts=["10.0.0.2"]) == {"worker-model"}
        # when the orchestrator node IS in the host set (e.g. tp-peer union or
        # env override), its alias IS expected again
        assert health._proxy_expected_aliases(
            {"units": [{"role": "worker", "nodes": ["10.0.0.2"]},
                       {"role": "orchestrator", "nodes": ["10.0.0.1"]}]},
            relaunch_hosts=["10.0.0.1", "10.0.0.2"]) == \
            {"worker-model", "orchestrator-model"}
        # a unit with no node info is expected wherever the proxy points
        assert health._proxy_expected_aliases(
            {"units": [{"role": "worker"}]}, relaunch_hosts=["10.0.0.2"]) == \
            {"worker-model"}
        # uniform pool demands BOTH regardless of configured roles
        assert health._proxy_expected_aliases(
            {"uniform_pool": True, "units": [{"role": "worker"}]}) == \
            {"worker-model", "orchestrator-model"}

    def test_probe_parses_models_body(self, monkeypatch):
        """The probe must PARSE the body — a 200 whose JSON has no model ids
        is not health (that is the whole outage)."""
        import json as _json
        from hscc_daemon import health

        class _Resp:
            def __init__(self, payload):
                self._b = _json.dumps(payload).encode()

            def read(self):
                return self._b

        monkeypatch.setattr(
            health.urllib.request, "urlopen",
            lambda url, timeout=5: _Resp({"data": [{"id": "worker-model"}, {"id": "x"}]}))
        out = health._proxy_models_probe("http://localhost:4000/v1/models")
        assert out["ok"] is True and sorted(out["models"]) == ["worker-model", "x"]

        monkeypatch.setattr(
            health.urllib.request, "urlopen", lambda url, timeout=5: _Resp({"data": []}))
        out = health._proxy_models_probe("http://localhost:4000/v1/models")
        assert out["ok"] is True and out["models"] == []   # verdict lives in _healthy

        def boom(url, timeout=5):
            raise OSError("connection refused")
        monkeypatch.setattr(health.urllib.request, "urlopen", boom)
        out = health._proxy_models_probe("http://localhost:4000/v1/models")
        assert out["ok"] is False and "refused" in out["error"]


class TestProxyRelaunchStall:
    """t_43b6e245 — the relaunch loop must never no-op silently for hours.

    Incident (docs/audits/PROXY_RELAUNCH_NOOP_2026-10-18.md): sparkrun's own
    restart SIGTERMs only the state-file wrapper PID, orphaning the real
    litellm socket owner; the replacement wrapper stayed alive without ever
    binding :4000. `is_running()` (state-file PID + os.kill) then read
    "running" forever and every plain `sparkrun proxy start` refused —
    "Error: proxy is already running (PID ...) Use --restart" — while
    `check_proxy` logged only `relaunching with [...]` and discarded the
    output. ~36 attempts, 3h09m, zero ERROR lines, zero notifications.

    RED ON OLD: with the pre-fix health.py the loop below emits N
    `relaunching with…` lines and NOTHING else — no ERROR, no notify, no
    recovery — which is exactly what the incident log shows.
    """

    _SERVING = {"version": 2, "units": [
        {"id": "w1", "role": "worker", "keepalive": True,
         "nodes": ["10.0.0.2"], "port": 8001},
    ]}
    REFUSAL = ("Discovering inference endpoints...\n"
               "Error: Proxy is already running (PID 12477) on port 4000. "
               "Use --restart to apply new settings, or 'sparkrun proxy stop' first.")

    def _incident_setup(self, tmp_hfcc_dir, monkeypatch, *, repair_ok=True):
        """Sandboxed replay of the incident steady state: probe refused, a
        hollow tracked PID alive, every plain `proxy start` refused, the repair
        path available. Nothing here touches the live fleet."""
        from hscc_daemon import health
        from hscc_daemon import serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", {"10.0.0.2"})
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: self._SERVING)
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": False, "models": [],
                                                    "error": "Connection refused"})
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: {"ok": False, "backends": set(),
                                                          "error": "refused"})
        calls = []
        # Plain `proxy start` is REFUSED while the hollow tracked PID lives —
        # the incident's refusal verbatim.
        def fake_run(args, **k):
            calls.append(list(args))
            return {"ok": False, "output": self.REFUSAL, "stderr": self.REFUSAL}
        monkeypatch.setattr(health, "run_cmd", fake_run)
        # The escalation repair: succeeds (recovers) or fails per repair_ok.
        repairs = []
        def fake_repair(hosts):
            repairs.append(list(hosts))
            return ({"ok": True, "verified": True, "output": "repaired"}
                    if repair_ok else
                    {"ok": False, "verified": False, "output": self.REFUSAL})
        monkeypatch.setattr(health, "_proxy_repair_orphan", fake_repair)
        # Hollow tracked PID: alive, cmdline vetted (os.kill/ps stubbed —
        # never the operator's real process table). raising=False so the
        # RED-ON-OLD run reaches the behavioural assertions instead of dying
        # on a missing attribute the old module never had.
        kills = []
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: 12477)
        monkeypatch.setattr(health, "_proc_cmdline",
                            lambda pid: "/x/bin/python /x/bin/litellm --config c.yaml",
                            raising=False)
        # The hollow PID DIES on the first real signal (no 0-signal probes are
        # recorded, and the death is immediate so no test sleeps on the
        # SIGTERM->SIGKILL ladder).
        alive = {"v": True}
        def fake_kill(pid, sig):
            if not sig:
                if not alive["v"]:
                    raise ProcessLookupError()
                return
            kills.append((pid, sig))
            alive["v"] = False
            raise ProcessLookupError()          # dies right now
        monkeypatch.setattr(health.os, "kill", fake_kill)
        # Alert sinks: record, never notify the operator's desktop.
        notes, events = [], []
        monkeypatch.setattr(health, "send_macos_notification",
                            lambda title, body, priority="normal", **kw:
                                notes.append((title, body, priority)) or True)
        monkeypatch.setattr(health, "emit_event",
                            lambda t, p, severity="info", **kw:
                                events.append((t, p, severity)) or True)
        return health, calls, repairs, kills, notes, events

    def _state(self, tmp_hfcc_dir):
        return json.loads((tmp_hfcc_dir / "state" / "proxy.json").read_text())

    def _force_attempt(self, tmp_hfcc_dir):
        """Backdate the durable cooldown stamp so the NEXT check_proxy tick
        actually attempts (the 300 s cooldown is correct production behaviour
        — a test loop must not be suppressed by it)."""
        import datetime
        from hscc_daemon import health
        p = tmp_hfcc_dir / "state" / "proxy.json"
        st = json.loads(p.read_text())
        st["last_relaunch"] = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=2 * health.PROXY_RELAUNCH_COOLDOWN_S + 5)
        ).isoformat()
        p.write_text(json.dumps(st))

    def _attempts(self, tmp_hfcc_dir, n):
        """Run N check_proxy ticks with the cooldown forced open (the incident
        cadence: one real attempt every PROXY_RELAUNCH_COOLDOWN_S)."""
        from hscc_daemon import health
        for _ in range(n):
            health.check_proxy()
            self._force_attempt(tmp_hfcc_dir)

    def test_old_loop_would_log_forever_new_alarms_after_n(self, tmp_hfcc_dir, monkeypatch):
        """THE card's red-on-old: N consecutive refused relaunches. OLD code:
        every attempt is a bare `relaunching with…` WARN, no ERROR, no notify.
        NEW: each failure logs ERROR with the refusal text, and at the
        threshold exactly one throttled notify + a durable event fire."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch, repair_ok=False)
        errs = []
        monkeypatch.setattr(health, "log",
                            lambda msg, lvl="INFO": errs.append((lvl, msg)))
        self._attempts(tmp_hfcc_dir, health.PROXY_RELAUNCH_MAX_CONSECUTIVE)
        # Loud: every failed attempt surfaced the command output as ERROR.
        fail_lines = [m for lvl, m in errs if lvl == "ERROR"
                      and "proxy repair on" in m and "FAILED" in m]
        assert len(fail_lines) >= health.PROXY_RELAUNCH_MAX_CONSECUTIVE
        assert "already running" in fail_lines[0]
        # Durable streak in the state file (survives a daemon restart).
        assert self._state(tmp_hfcc_dir)["relaunch_streak"] == \
            health.PROXY_RELAUNCH_MAX_CONSECUTIVE
        # Alert fired exactly once, loud, with a durable event.
        assert len(notes) == 1 and notes[0][2] == "high"
        assert "already running" in notes[0][1]
        assert len(events) == 1 and events[0][0] == "proxy.relaunch.stalled"
        assert self._state(tmp_hfcc_dir).get("stall_alerted_at")

    def test_alert_is_throttled_across_ticks(self, tmp_hfcc_dir, monkeypatch):
        """One notify per notify-cooldown, not one per 5-min attempt: the 3h
        incident is ~36 attempts and must not become 36 desktop notifications
        (the 2026-10-08 NAS 178-notification lesson)."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch, repair_ok=False)
        self._attempts(tmp_hfcc_dir, 2 * health.PROXY_RELAUNCH_MAX_CONSECUTIVE)
        # Every tick attempted something: MAX-1 plain starts, then escalated
        # repairs (the plain-start refusal is never re-issued while pinned).
        assert len(calls) >= health.PROXY_RELAUNCH_MAX_CONSECUTIVE - 1
        assert len(repairs) >= health.PROXY_RELAUNCH_MAX_CONSECUTIVE
        # Exactly one alert: the throttle stamp is CARRIED FORWARD on later
        # unhealthy writes, so a throttled tick can never re-arm the notify.
        assert len(notes) == 1
        assert len(events) == 1
        streak = self._state(tmp_hfcc_dir)["relaunch_streak"]
        assert streak >= 2 * health.PROXY_RELAUNCH_MAX_CONSECUTIVE

    def test_third_attempt_escalates_to_the_repair_path(self, tmp_hfcc_dir, monkeypatch):
        """Asymmetry killer: once the streak hits the threshold the relaunch
        stops issuing the command sparkrun provably refuses and runs the
        stop -> reap -> verified-start repair instead — under the same heal
        lease. A plain `proxy start` may not be invoked while pinned."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch)
        self._attempts(tmp_hfcc_dir, health.PROXY_RELAUNCH_MAX_CONSECUTIVE)
        # Attempts 1..MAX-1 were plain starts; attempt MAX escalated.
        starts = [c for c in calls if c[1:3] == ["proxy", "start"] and "--restart" not in c]
        assert len(starts) == health.PROXY_RELAUNCH_MAX_CONSECUTIVE - 1
        assert len(repairs) == 1 and repairs[0] == ["10.0.0.2"]
        # The hollow tracked PID was retired first (vetted cmdline stubbed).
        assert kills and kills[0][0] == 12477
        assert self._state(tmp_hfcc_dir)["relaunched"] is True

    def test_escalation_never_uses_restart_and_respects_the_guards(
            self, tmp_hfcc_dir, monkeypatch):
        """The escalation must refuse a non-litellm tracked PID (PID reuse)
        and must not invent a plain `--restart` invocation as a side door."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch, repair_ok=False)
        monkeypatch.setattr(health, "_proc_cmdline", lambda pid: "/sbin/launchd")
        self._attempts(tmp_hfcc_dir, health.PROXY_RELAUNCH_MAX_CONSECUTIVE)
        assert kills == []                       # foreign cmdline: refused
        for c in calls:
            assert "--restart" not in c, f"--restart side door used: {c}"
        assert len(repairs) == 1                 # repair still attempted
        assert health.check_proxy() is not True

    def test_healthy_check_clears_the_streak(self, tmp_hfcc_dir, monkeypatch):
        """Recovery defuses the alarm: once the probes pass, the state rewrite
        drops streak + alert stamp, so a LATER outage alerts afresh."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch)
        for _ in range(health.PROXY_RELAUNCH_MAX_CONSECUTIVE):
            health.check_proxy()
        assert self._state(tmp_hfcc_dir).get("relaunch_streak")
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": True,
                                                    "models": ["worker-model"],
                                                    "error": ""})
        assert health.check_proxy() is True
        st = self._state(tmp_hfcc_dir)
        assert "relaunch_streak" not in st
        assert "stall_alerted_at" not in st
        assert health._proxy_relaunch_streak() == 0

    def test_rc0_over_dead_proxy_does_not_defuse_the_streak(
            self, tmp_hfcc_dir, monkeypatch):
        """Exit code is not success: sparkrun can exit 0 while litellm never
        comes up (the 10-11 shape). The streak must keep climbing to the
        threshold — an rc-0 relaunch must not reset the alarm — and the
        stall alert must fire anyway."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch, repair_ok=False)

        def fake_run(args, **k):
            calls.append(list(args))
            return {"ok": True, "output": "Proxy started", "stderr": ""}
        monkeypatch.setattr(health, "run_cmd", fake_run)
        for _ in range(health.PROXY_RELAUNCH_MAX_CONSECUTIVE):
            health.check_proxy()
            p = tmp_hfcc_dir / "state" / "proxy.json"
            st = json.loads(p.read_text())
            import datetime
            st["last_relaunch"] = (datetime.datetime.now(
                datetime.timezone.utc)
                - datetime.timedelta(seconds=2 * health.PROXY_RELAUNCH_COOLDOWN_S + 5)
            ).isoformat()
            p.write_text(json.dumps(st))
        assert self._state(tmp_hfcc_dir)["relaunch_streak"] == \
            health.PROXY_RELAUNCH_MAX_CONSECUTIVE
        assert len(notes) == 1                   # rc-0-then-die still alarms

    def test_suppressed_and_deferred_ticks_never_count(self, tmp_hfcc_dir, monkeypatch):
        """Cooldown-suppressed and lease-deferred ticks attempt nothing and
        must not raise a streak that could fire a false alarm (same rule as
        `relaunch_failed`)."""
        from hscc_daemon import heal_lease
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch)
        assert health.check_proxy() is False     # attempt 1, streak 1
        assert health.check_proxy() is False     # suppressed (cooldown)
        st = self._state(tmp_hfcc_dir)
        assert st["relaunch_streak"] == 1        # suppressed did NOT count
        assert "last_relaunch" in st and "stall_alerted_at" not in st
        # Lease-deferred tick.
        p = tmp_hfcc_dir / "state" / "proxy.json"
        st = json.loads(p.read_text())
        import datetime
        st["last_relaunch"] = (datetime.datetime.now(
            datetime.timezone.utc)
            - datetime.timedelta(seconds=2 * health.PROXY_RELAUNCH_COOLDOWN_S + 5)
        ).isoformat()
        p.write_text(json.dumps(st))
        assert heal_lease.acquire("worker-relaunch", "10.0.0.2:8001")[0] is True
        assert health.check_proxy() is False
        assert self._state(tmp_hfcc_dir)["relaunch_streak"] == 1
        assert notes == []

    def test_streak_survives_daemon_restart(self, tmp_hfcc_dir, monkeypatch):
        """Durability: the alert threshold must be reachable across a restart
        (the incident's counters were file-based; an in-memory streak would
        reset exactly when a crash-looping daemon needs it most)."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch, repair_ok=False)
        self._attempts(tmp_hfcc_dir, health.PROXY_RELAUNCH_MAX_CONSECUTIVE - 1)
        # Simulated restart: module state gone, state file kept.
        assert health._proxy_relaunch_streak() == \
            health.PROXY_RELAUNCH_MAX_CONSECUTIVE - 1
        assert notes == []
        health.check_proxy()                     # attempt MAX after "restart"
        assert len(notes) == 1

    def test_repair_verification_waits_for_the_port_to_bind(
            self, tmp_hfcc_dir, monkeypatch):
        """The 01:41:42 `repair UNVERIFIED — port owner None` was a RACE:
        `proxy start` returns after sparkrun's 2 s grace and litellm binds
        seconds later. Verification must poll while the port is unbound."""
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: 999)
        monkeypatch.setattr(health, "_reap_orphan_proxy_listener", lambda t: True)
        monkeypatch.setattr(health, "run_cmd", lambda a, **k: {"ok": True})
        ticks = {"n": 0}
        def flaky_listener():
            ticks["n"] += 1
            if ticks["n"] <= 3:
                return None, None                # not bound yet
            return 999, "litellm --config c.yaml"
        monkeypatch.setattr(health, "_proxy_listener", flaky_listener)
        monkeypatch.setattr(health.time, "sleep", lambda s: None)
        res = health._proxy_repair_orphan(["10.0.0.2"])
        assert res["verified"] is True and res["ok"] is True

    def test_kill_hollow_refuses_foreign_and_dead_pids(self, tmp_hfcc_dir, monkeypatch):
        """Blast-radius contract: only a living, litellm-cmdline tracked PID
        may be signalled."""
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        kills = []
        # sig 0 is the liveness probe, not a signal — record only real ones.
        monkeypatch.setattr(
            health.os, "kill",
            lambda pid, sig: kills.append((pid, sig)) if sig else None)
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: None)
        assert health._proxy_kill_hollow_tracked("test") is False and kills == []
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: 555)
        monkeypatch.setattr(health, "_proc_cmdline", lambda pid: "/usr/sbin/httpd")
        assert health._proxy_kill_hollow_tracked("test") is False and kills == []
        monkeypatch.setattr(health, "_proc_cmdline",
                            lambda pid: "litellm --config c.yaml")
        # os.kill(pid, 0) raises -> already gone, no signal needed.
        def gone(pid, sig):
            raise ProcessLookupError()
        monkeypatch.setattr(health.os, "kill", gone)
        assert health._proxy_kill_hollow_tracked("test") is True

    def test_intentional_autodown_window_never_alerts(self, tmp_hfcc_dir, monkeypatch):
        """Fleet deliberately down (autodown window): the streak may build but
        no operator alert may fire — same suppression verify.py expects."""
        health, calls, repairs, kills, notes, events = self._incident_setup(
            tmp_hfcc_dir, monkeypatch)
        monkeypatch.setattr(health, "_intentional_window", lambda: True)
        for _ in range(health.PROXY_RELAUNCH_MAX_CONSECUTIVE):
            health.check_proxy()
        assert notes == [] and events == []



class TestCheckMultiplexProfiles:
    """_check_multiplex_profiles() verifies that all Hermes profiles are served
    when multiplex is enabled."""

    def _setup(self, tmp_path, monkeypatch):
        """Create tmp dirs and monkeypatch the module-level path constants."""
        hermes_dir = tmp_path / "hermes"
        hermes_dir.mkdir(parents=True)
        profiles_dir = hermes_dir / "profiles"
        config_yaml = hermes_dir / "config.yaml"
        gw_state = hermes_dir / "gateway_state.json"

        from hscc_daemon import health
        monkeypatch.setattr(health, "_HERMES_CONFIG_YAML", str(config_yaml))
        monkeypatch.setattr(health, "_HERMES_GATEWAY_STATE", str(gw_state))
        monkeypatch.setattr(health, "_HERMES_PROFILES_DIR", str(profiles_dir))
        return health, profiles_dir, config_yaml, gw_state

    # --- multiplex enabled, served_profiles missing ---
    def test_multiplex_true_no_gateway_state(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        profiles_dir.mkdir(parents=True)
        # gw_state does not exist

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "gateway_state.json" in result["message"]

    # --- multiplex enabled, served_profiles empty ---
    def test_multiplex_true_served_profiles_empty(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text(json.dumps({"served_profiles": []}))
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "empty" in result["message"].lower() or "no profiles" in result["message"].lower()

    # --- multiplex enabled, served_profiles missing key ---
    def test_multiplex_true_served_profiles_missing_key(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text(json.dumps({}))
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "empty" in result["message"].lower() or "no profiles" in result["message"].lower()

    # --- multiplex enabled, profiles missing from served ---
    def test_multiplex_true_partial_profiles(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text(json.dumps({"served_profiles": ["backend-engineer"]}))
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "backend-engineer").mkdir()
        (profiles_dir / "orchestrator").mkdir()
        (profiles_dir / "researcher").mkdir()

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "orchestrator" in result["message"]
        assert "researcher" in result["message"]
        assert "backend-engineer" not in result["message"]  # served, not missing

    # --- multiplex enabled, all profiles served ---
    def test_multiplex_true_all_profiles_served(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text(json.dumps({"served_profiles": ["backend-engineer", "orchestrator"]}))
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "backend-engineer").mkdir()
        (profiles_dir / "orchestrator").mkdir()

        result = health._check_multiplex_profiles()
        assert result["ok"] is True
        assert "served" in result["message"]

    # --- multiplex disabled ---
    def test_multiplex_false(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: false\n")
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is True
        assert "disabled" in result["message"].lower() or "absent" in result["message"].lower()

    # --- multiplex key absent in config ---
    def test_multiplex_key_absent(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("something_else: true\n")
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is True
        assert "disabled" in result["message"].lower() or "absent" in result["message"].lower()

    # --- config.yaml missing entirely ---
    def test_config_yaml_missing(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        profiles_dir.mkdir(parents=True)
        # config_yaml does not exist

        result = health._check_multiplex_profiles()
        assert result["ok"] is True
        assert "skipped" in result["message"].lower()

    # --- gateway_state.json parse error (present but corrupt) ---
    def test_gateway_state_parse_error(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text("{invalid json")
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "corrupt" in result["message"].lower()

    # --- gateway_state.json present but contains non-dict (e.g. array) ---
    def test_gateway_state_non_dict(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text("[1, 2, 3]")
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is False
        assert "corrupt" in result["message"].lower()

    # --- profiles dir missing when multiplex enabled ---
    def test_profiles_dir_missing_multiplex_true(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text(json.dumps({"served_profiles": ["default"]}))
        # profiles_dir does not exist

        result = health._check_multiplex_profiles()
        assert result["ok"] is True
        assert "skipped" in result["message"].lower()


class TestCheckGatewayWithMultiplex:
    """check_gateway() integrates multiplex check with gateway health."""

    def test_gateway_ok_mux_ok(self, tmp_hfcc_dir, monkeypatch, fake_subprocess):
        from hscc_daemon import health
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)

        # Gateway job alive
        fake_subprocess.set_result(stdout="12345\n", returncode=0)
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True})

        # Multiplex disabled — informational
        hermes_dir = tmp_hfcc_dir / "hermes"
        (hermes_dir / "profiles").mkdir(parents=True)
        (hermes_dir / "config.yaml").write_text("multiplex_profiles: false\n")
        monkeypatch.setattr(health, "_HERMES_CONFIG_YAML", str(hermes_dir / "config.yaml"))
        monkeypatch.setattr(health, "_HERMES_GATEWAY_STATE", str(hermes_dir / "gateway_state.json"))
        monkeypatch.setattr(health, "_HERMES_PROFILES_DIR", str(hermes_dir / "profiles"))

        result = health.check_gateway()
        assert result is True

        # Verify state was written with multiplex fields
        state = state_mod.read_state("gateway")
        assert state is not None
        assert "multiplex_ok" in state
        assert "multiplex_message" in state

    def test_gateway_ok_but_mux_fails(self, tmp_hfcc_dir, monkeypatch, fake_subprocess):
        from hscc_daemon import health
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)

        # Gateway job alive
        fake_subprocess.set_result(stdout="12345\n", returncode=0)
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True})

        # Multiplex enabled, all profiles missing
        hermes_dir = tmp_hfcc_dir / "hermes"
        (hermes_dir / "profiles").mkdir(parents=True)
        (hermes_dir / "profiles" / "backend-engineer").mkdir()
        (hermes_dir / "config.yaml").write_text("multiplex_profiles: true\n")
        # No gateway_state.json
        monkeypatch.setattr(health, "_HERMES_CONFIG_YAML", str(hermes_dir / "config.yaml"))
        monkeypatch.setattr(health, "_HERMES_GATEWAY_STATE", str(hermes_dir / "gateway_state.json"))
        monkeypatch.setattr(health, "_HERMES_PROFILES_DIR", str(hermes_dir / "profiles"))

        result = health.check_gateway()
        assert result is False  # multiplex failure makes gateway check fail

        state = state_mod.read_state("gateway")
        assert state is not None
        assert state["multiplex_ok"] is False
        assert state["gateway_job"] is True
        assert state["vllm_healthy"] is True
class TestCheckLocal:
    """check_local() — informational by default, required via HSCC_LOCAL_REQUIRE."""

    def _mock(self, monkeypatch, tmp_hfcc_dir, docker_ok=False, ollama_ok=False,
              pg_ok=False, hscc_ok=False):
        """Set up mocks for check_local(). Returns (health module, state_calls list)."""
        from hscc_daemon import health
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        # Capture write_state calls
        state_calls = []
        def fake_write_state(name, data):
            state_calls.append((name, data))
        monkeypatch.setattr(health, "write_state", fake_write_state)

        # Control run_cmd responses
        def fake_run_cmd(args, **kwa):
            if args[0] == "docker" and "info" in args:
                return {"ok": docker_ok, "output": ""}
            elif args[0] == "docker" and "ps" in args:
                return {"ok": pg_ok, "output": "Up 5 hours" if pg_ok else ""}
            elif args[0] == "pgrep":
                return {"ok": hscc_ok, "output": "12345" if hscc_ok else ""}
            elif args[0] == "node":
                return {"ok": True, "output": "v18.0.0"}
            elif args[0] == "npm":
                return {"ok": True, "output": "9.0.0"}
            elif os.path.basename(args[0]) == "sparkrun":
                return {"ok": False, "output": ""}
            return {"ok": False, "output": ""}
        monkeypatch.setattr(health, "run_cmd", fake_run_cmd)

        # Control http_check (Ollama)
        def fake_http_check(url, **kwa):
            if "11434" in url:
                return {"ok": ollama_ok}
            return {"ok": False}
        monkeypatch.setattr(health, "http_check", fake_http_check)

        return health, state_calls

    # -- default mode (HSCC_LOCAL_REQUIRE not set) --

    def test_default_docker_absent_ok(self, tmp_hfcc_dir, monkeypatch):
        monkeypatch.delenv("HSCC_LOCAL_REQUIRE", raising=False)
        health, state_calls = self._mock(monkeypatch, tmp_hfcc_dir)
        result = health.check_local()
        assert result is True
        _, data = state_calls[-1]
        assert data["ok"] is True
        assert "docker" in data["message"]

    def test_default_all_services_present_ok(self, tmp_hfcc_dir, monkeypatch):
        monkeypatch.delenv("HSCC_LOCAL_REQUIRE", raising=False)
        health, state_calls = self._mock(
            monkeypatch, tmp_hfcc_dir,
            docker_ok=True, ollama_ok=True, pg_ok=True, hscc_ok=True
        )
        result = health.check_local()
        assert result is True
        _, data = state_calls[-1]
        assert data["ok"] is True
        assert "all services available" in data["message"]

    def test_default_all_absent_ok(self, tmp_hfcc_dir, monkeypatch):
        """Default mode: everything missing is still informational ok=True."""
        monkeypatch.delenv("HSCC_LOCAL_REQUIRE", raising=False)
        health, state_calls = self._mock(monkeypatch, tmp_hfcc_dir)
        result = health.check_local()
        assert result is True
        _, data = state_calls[-1]
        assert data["ok"] is True
        assert "informational" in data["message"]

    # -- required mode --

    def test_required_docker_absent_fail(self, tmp_hfcc_dir, monkeypatch):
        monkeypatch.setenv("HSCC_LOCAL_REQUIRE", "docker,ollama")
        health, state_calls = self._mock(monkeypatch, tmp_hfcc_dir)
        result = health.check_local()
        assert result is False
        _, data = state_calls[-1]
        assert data["ok"] is False
        assert "docker" in data["message"]
        assert "required" in data["message"]

    def test_required_all_required_present_non_required_absent_ok(self, tmp_hfcc_dir, monkeypatch):
        monkeypatch.setenv("HSCC_LOCAL_REQUIRE", "docker,ollama")
        health, state_calls = self._mock(
            monkeypatch, tmp_hfcc_dir,
            docker_ok=True, ollama_ok=True, pg_ok=False, hscc_ok=False
        )
        result = health.check_local()
        assert result is True
        _, data = state_calls[-1]
        assert data["ok"] is True
        assert "all required services running" in data["message"]

    def test_required_one_present_one_absent(self, tmp_hfcc_dir, monkeypatch):
        """Docker present, Ollama absent — only Ollama listed as missing."""
        monkeypatch.setenv("HSCC_LOCAL_REQUIRE", "docker,ollama")
        health, state_calls = self._mock(
            monkeypatch, tmp_hfcc_dir,
            docker_ok=True, ollama_ok=False, pg_ok=False, hscc_ok=False
        )
        result = health.check_local()
        assert result is False
        _, data = state_calls[-1]
        assert data["ok"] is False
        assert "ollama" in data["message"]
        assert "docker" not in data["message"]  # docker is present, not listed

    def test_required_whitespace_stripped(self, tmp_hfcc_dir, monkeypatch):
        """HSCC_LOCAL_REQUIRE with spaces around names still works."""
        monkeypatch.setenv("HSCC_LOCAL_REQUIRE", " docker , Ollama ")
        health, state_calls = self._mock(monkeypatch, tmp_hfcc_dir)
        result = health.check_local()
        assert result is False  # both docker and ollama absent
        _, data = state_calls[-1]
        assert "docker" in data["message"].lower()
        assert "ollama" in data["message"].lower()

    def test_services_dict_populated(self, tmp_hfcc_dir, monkeypatch):
        """services dict has correct running flags regardless of mode."""
        monkeypatch.delenv("HSCC_LOCAL_REQUIRE", raising=False)
        health, state_calls = self._mock(
            monkeypatch, tmp_hfcc_dir,
            docker_ok=True, ollama_ok=False, pg_ok=True, hscc_ok=False
        )
        health.check_local()
        _, data = state_calls[-1]
        svc = data["services"]
        assert svc["docker"]["running"] is True
        assert svc["ollama"]["running"] is False
        assert svc["postgresql"]["running"] is True
        assert svc["hscc_daemon"]["running"] is False


class TestCheckNas:
    """check_nas() must verify the NAS path is a REAL NFS mount, not just an
    existing directory (silent-failure bug fix: a bare local dir left behind
    when the NFS export/mount is lost must report ok=False, and local disk
    figures must never be surfaced as NAS figures)."""

    def _check(self, monkeypatch, tmp_hfcc_dir, mount_entries, *, nas_subpath="nas",
               probe=None):
        """Run check_nas with a controlled mount table and a tmp NAS dir.

        Returns (health_module, state_calls_list, nas_mount_path).
        """
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        monkeypatch.setattr(health, "_mount_table", lambda: mount_entries)
        state_calls = []
        monkeypatch.setattr(
            health, "write_state",
            lambda name, data: state_calls.append((name, data)))
        if probe is not None:
            monkeypatch.setattr(health, "_probe_mount", probe)
        nas_mount = tmp_hfcc_dir / nas_subpath
        nas_mount.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(health, "NAS_MOUNT", str(nas_mount))
        monkeypatch.setattr(health, "NAS_PROBE_TIMEOUT", 5.0)
        return health, state_calls, nas_mount

    def test_exists_not_a_mount_reports_false(self, tmp_hfcc_dir, monkeypatch):
        """Bare existing directory NOT in the mount table => ok False, and the
        message says not-mounted (the silent-failure bug)."""
        health, state_calls, nas_mount = self._check(
            monkeypatch, tmp_hfcc_dir, mount_entries=[])
        assert health.check_nas() is False
        _, data = state_calls[-1]
        assert data["ok"] is False
        assert "not an NFS mount" in data["details"]["message"]
        assert data["details"]["in_mount_table"] is False
        assert data["details"]["mount_is_distinct_device"] is False

    def test_not_a_mount_surfaces_no_local_disk_figures(self, tmp_hfcc_dir, monkeypatch):
        """No LOCAL disk figures may be reported as NAS figures when unmounted
        (df on an unmounted path silently resolves to the parent filesystem)."""
        health, state_calls, nas_mount = self._check(
            monkeypatch, tmp_hfcc_dir, mount_entries=[])
        health.check_nas()
        _, data = state_calls[-1]
        details = data["details"]
        for key in ("disk_total", "disk_used", "disk_avail", "disk_pct"):
            assert key not in details, f"{key} must not appear when unmounted"

    def test_real_mount_with_expected_content_reports_true(self, tmp_hfcc_dir, monkeypatch):
        """Path listed in the mount table with expected content => ok True."""
        health, state_calls, nas_mount = self._check(
            monkeypatch, tmp_hfcc_dir, mount_entries=[])
        monkeypatch.setattr(health, "_mount_table",
                            lambda: [(str(nas_mount), "nfs", "//nas/export")])
        # Provide expected content marker
        (nas_mount / "hub").mkdir(parents=True, exist_ok=True)
        assert health.check_nas() is True
        _, data = state_calls[-1]
        assert data["ok"] is True
        assert data["details"]["has_hub"] is True
        assert data["details"]["in_mount_table"] is True

    def test_real_mount_without_content_distinguished(self, tmp_hfcc_dir, monkeypatch):
        """A genuinely-mounted-but-EMPTY NAS is a different condition from "not
        mounted": it is still a real mount => ok True, with a note that the
        expected content markers are absent."""
        health, state_calls, nas_mount = self._check(
            monkeypatch, tmp_hfcc_dir, mount_entries=[])
        monkeypatch.setattr(health, "_mount_table",
                            lambda: [(str(nas_mount), "nfs", "//nas/export")])
        assert health.check_nas() is True
        _, data = state_calls[-1]
        assert data["ok"] is True
        assert "empty NAS" in data["details"]["message"]

    def test_missing_directory_reports_false(self, tmp_hfcc_dir, monkeypatch):
        """Directory missing entirely => ok False."""
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_calls = []
        monkeypatch.setattr(
            health, "write_state",
            lambda name, data: state_calls.append((name, data)))
        monkeypatch.setattr(health, "NAS_MOUNT",
                            str(tmp_hfcc_dir / "does-not-exist"))
        monkeypatch.setattr(health, "NAS_PROBE_TIMEOUT", 5.0)
        assert health.check_nas() is False
        _, data = state_calls[-1]
        assert data["ok"] is False
        assert "missing" in data["details"]["message"]

    def test_probe_timeout_reports_false_promptly(self, tmp_hfcc_dir, monkeypatch):
        """A hung filesystem probe (wedged NFS handle) must time out, report
        ok False, and return promptly — the test does NOT actually sleep."""
        import time as _time

        def hanging_probe(path):            # blocks forever (daemon-thread bound)
            while True:
                _time.sleep(1)

        nas_mount = tmp_hfcc_dir / "nas"
        nas_mount.mkdir(parents=True, exist_ok=True)
        health, state_calls, nas_mount = self._check(
            monkeypatch, tmp_hfcc_dir,
            mount_entries=[(str(nas_mount), "nfs", "//nas/export")],
            probe=hanging_probe)
        monkeypatch.setattr(health, "NAS_PROBE_TIMEOUT", 0.05)  # don't wait 5s
        start = _time.monotonic()
        assert health.check_nas() is False
        elapsed = _time.monotonic() - start
        assert elapsed < 3.0, f"check_nas took {elapsed:.2f}s on a hung probe"
        _, data = state_calls[-1]
        assert data["ok"] is False
        assert "timed out" in data["details"]["message"]
        # No figures must be surfaced from a timed-out probe
        details = data["details"]
        for key in ("disk_total", "disk_used", "disk_avail", "disk_pct"):
            assert key not in details

    def test_hung_existence_probe_does_not_freeze_thread(self, tmp_hfcc_dir,
                                                         monkeypatch):
        """REGRESSION: the existence gate at the TOP of check_nas must also be
        timeout-bounded. Before the fix, os.path.exists(NAS_MOUNT) /
        os.path.isdir(NAS_MOUNT) ran directly on the daemon's periodic thread,
        so a wedged NFS handle froze the NAS check forever (the "NAS check
        silently stops running" root cause). A hanging _probe_exists must time
        out, report ok False, and return promptly — never hang the thread."""
        import time as _time

        def hanging_exists(path):           # blocks forever (bounded daemon thread)
            while True:
                _time.sleep(1)

        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        monkeypatch.setattr(health, "_probe_exists", hanging_exists)
        monkeypatch.setattr(health, "NAS_MOUNT",
                            str(tmp_hfcc_dir / "nas"))
        monkeypatch.setattr(health, "NAS_PROBE_TIMEOUT", 0.05)
        state_calls = []
        monkeypatch.setattr(
            health, "write_state",
            lambda name, data: state_calls.append((name, data)))

        start = _time.monotonic()
        assert health.check_nas() is False
        elapsed = _time.monotonic() - start
        assert elapsed < 3.0, \
            f"check_nas hung {elapsed:.2f}s on a blocking existence probe"
        _, data = state_calls[-1]
        assert data["ok"] is False
        assert "timed out" in data["details"]["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
