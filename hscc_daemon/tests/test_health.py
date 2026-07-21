"""Unit tests for health.py - health check functions.

Tests isolate I/O by mocking run_cmd, ssh_cmd, http_check, and file reads.
"""
import json
import os
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
        health._worker_relaunch_at.clear()
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
        # stop via run_cmd still fires
        assert any(t == "run_cmd" and a[:2] == ["sparkrun", "stop"] for t, a in ran)
        # Popen called for sparkrun run (detached)
        assert len(popen_calls) == 1
        popen_args = popen_calls[0][0][0]
        assert popen_args[:2] == ["sparkrun", "run"]
        assert "10.0.0.2" in popen_args
        assert any("27b" in str(x) for x in popen_args)
        # Popen kwargs: detached + log file
        kwargs = popen_calls[0][1]
        assert kwargs.get("start_new_session") is True

    def test_grace_window_skips_relaunch(self, tmp_hfcc_dir, monkeypatch):
        import time as _time
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        # pretend we just relaunched it -> within grace, must NOT relaunch again.
        # Grace is keyed per UNIT (node, port); units w/o explicit port → :8000.
        health._worker_relaunch_at[("10.0.0.2", 8000)] = _time.monotonic()
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
        stop_cmds = [a for a in ran if a[:2] == ["sparkrun", "stop"]]
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
        """Asserts grace period from lifecycle.py is respected (20 min default)."""
        import time as _time
        from hscc_daemon import lifecycle
        # Confirm default is 20
        monkeypatch.setattr(lifecycle, "VLLM_LOAD_GRACE_MINUTES", 20)

        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True})

        # Set relaunch time to 19 minutes ago (within 20-min grace)
        health._worker_relaunch_at[("10.0.0.2", 8000)] = _time.monotonic() - 19 * 60
        popen_calls = []
        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            raise RuntimeError("should not launch")
        monkeypatch.setattr(health.subprocess, "Popen", fake_popen)

        health.check_workers()
        assert popen_calls == []  # within 20-min grace, no relaunch

        # Set relaunch time to 21 minutes ago (past grace) — relaunch fires
        health._worker_relaunch_at[("10.0.0.2", 8000)] = _time.monotonic() - 21 * 60
        popen_calls.clear()
        health.check_workers()
        assert len(popen_calls) == 1  # past grace, relaunch fires


class TestCheckProxy:
    """check_proxy() keeps the worker load-balancer alive."""

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", set(nodes))
        return health

    def test_no_workers_noop(self, tmp_hfcc_dir, monkeypatch):
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=[])
        assert health.check_proxy() is True

    def test_proxy_healthy(self, tmp_hfcc_dir, monkeypatch):
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True})
        ran = []
        monkeypatch.setattr(health, "run_cmd", lambda *a, **k: ran.append(a) or {"ok": True})
        assert health.check_proxy() is True
        assert ran == []                         # healthy -> no relaunch

    def test_proxy_down_relaunched(self, tmp_hfcc_dir, monkeypatch):
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2", "10.0.0.3"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_proxy() is True
        cmd = ran[0]
        assert cmd[:3] == ["sparkrun", "proxy", "start"]
        assert "10.0.0.2,10.0.0.3" in cmd


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

    # --- gateway_state.json parse error ---
    def test_gateway_state_parse_error(self, tmp_path, monkeypatch):
        health, profiles_dir, config_yaml, gw_state = self._setup(tmp_path, monkeypatch)

        config_yaml.write_text("multiplex_profiles: true\n")
        gw_state.write_text("{invalid json")
        profiles_dir.mkdir(parents=True)

        result = health._check_multiplex_profiles()
        assert result["ok"] is True
        assert "skipped" in result["message"].lower() or "parse" in result["message"].lower()

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
