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
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        assert health.check_workers() is True    # relaunched -> ok
        # stop then run, both targeting the worker node + its recipe
        assert any(a[:2] == ["sparkrun", "stop"] for a in ran)
        run_cmd = next(a for a in ran if a[:2] == ["sparkrun", "run"])
        assert "10.0.0.2" in run_cmd
        assert any("27b" in str(x) for x in run_cmd)

    def test_grace_window_skips_relaunch(self, tmp_hfcc_dir, monkeypatch):
        import datetime as dt
        health, _ = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": False})
        # pretend we just relaunched it -> within grace, must NOT relaunch again.
        # Grace is keyed per UNIT (node, port); units w/o explicit port → :8000.
        health._worker_relaunch_at[("10.0.0.2", 8000)] = dt.datetime.now(dt.timezone.utc)
        ran = []
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: ran.append(args) or {"ok": True})
        health.check_workers()
        assert ran == []                         # grace window respected

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
        health.check_workers()
        # only b (:8001, ~/r/b.yaml) relaunched; a's recipe never touched
        run_cmds = [a for a in ran if a[:2] == ["sparkrun", "run"]]
        assert len(run_cmds) == 1
        assert any("b.yaml" in str(x) for x in run_cmds[0])
        assert "8001" in run_cmds[0]
        # stop targeted b's recipe, not --all (sibling a survives)
        stop_cmds = [a for a in ran if a[:2] == ["sparkrun", "stop"]]
        assert all("--all" not in a for a in stop_cmds)
        assert all(not any("a.yaml" in str(x) for x in a) for a in run_cmds)


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
            elif args[0] == "sparkrun":
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
