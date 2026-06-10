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
    """check_workers() checks worker node health."""

    def test_no_workers_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        def fake_expanduser(p):
            if p == "~/.hscc/workers.json":
                return str(tmp_hfcc_dir / "nonexistent.json")
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_workers()
        assert result is True  # No workers configured -> OK

    def test_workers_online(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        workers_file = tmp_hfcc_dir / "workers.json"
        workers_file.write_text(json.dumps({
            "workers": [
                {"node": "10.0.0.1", "status": "online"},
                {"node": "10.0.0.2", "status": "offline"},
            ]
        }))

        def fake_expanduser(p):
            if p == "~/.hscc/workers.json":
                return str(workers_file)
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_workers()
        assert result is True  # At least 1 online

    def test_all_workers_offline(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        workers_file = tmp_hfcc_dir / "workers.json"
        workers_file.write_text(json.dumps({
            "workers": [
                {"node": "10.0.0.1", "status": "offline"},
            ]
        }))

        def fake_expanduser(p):
            if p == "~/.hscc/workers.json":
                return str(workers_file)
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_workers()
        assert result is False  # No online workers

    def test_malformed_workers_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import health
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        workers_file = tmp_hfcc_dir / "workers.json"
        workers_file.write_text("{bad json")

        def fake_expanduser(p):
            if p == "~/.hscc/workers.json":
                return str(workers_file)
            return p

        monkeypatch.setattr(os.path, "expanduser", fake_expanduser)
        result = health.check_workers()
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
