"""Unit tests for install.py - service management (launchd/systemd).

Tests generate_plist, generate_systemd_unit, _daemon_path_env, _service_manager.
"""
import os
import pytest
from pathlib import Path


class TestDaemonPathEnv:
    """_daemon_path_env() constructs a minimal PATH for the daemon."""

    def test_contains_venv_bin(self):
        from hscc_daemon.install import _daemon_path_env
        hmdir = "/home/user"
        path = _daemon_path_env(hmdir)
        assert "/home/user/.hermes/hermes-agent/venv/bin" in path

    def test_contains_local_bin(self):
        from hscc_daemon.install import _daemon_path_env
        path = _daemon_path_env("/home/user")
        assert "/home/user/.local/bin" in path

    def test_contains_system_paths(self):
        from hscc_daemon.install import _daemon_path_env
        path = _daemon_path_env("/home/user")
        assert "/usr/bin" in path
        assert "/bin" in path
        assert "/usr/sbin" in path


class TestServiceManager:
    """_service_manager() picks the right auto-start mechanism."""

    def test_darwin_returns_launchd(self, monkeypatch):
        from hscc_daemon import install
        import sys
        monkeypatch.setattr(sys, "platform", "darwin")
        assert install._service_manager() == "launchd"

    def test_linux_with_systemctl(self, monkeypatch):
        from hscc_daemon import install
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/systemctl" if x == "systemctl" else None)
        assert install._service_manager() == "systemd"

    def test_linux_without_systemctl(self, monkeypatch):
        from hscc_daemon import install
        import sys
        import shutil
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(shutil, "which", lambda x: None)
        assert install._service_manager() == "none"


class TestGeneratePlist:
    """generate_plist() produces valid launchd plist XML."""

    def test_contains_label(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "com.hermes.hscc_daemon" in plist

    def test_contains_program_arguments(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "python3" in plist or "Python" in plist or "<string>" in plist
        assert "hscc_daemon" in plist
        assert "start-daemon" in plist

    def test_contains_working_directory(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "WorkingDirectory" in plist

    def test_contains_log_paths(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "daemon.log" in plist

    def test_contains_keepalive(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "KeepAlive" in plist

    def test_python_path_is_real_not_hardcoded(self):
        """C1: the interpreter must be a real executable, never a blindly
        hardcoded /usr/local/bin/python3 (absent on Homebrew-only/Spark hosts)."""
        import os
        from hscc_daemon.install import _resolve_python
        p = _resolve_python()
        assert os.path.isfile(p) and os.access(p, os.X_OK)

    def test_resolver_prefers_venv(self, monkeypatch, tmp_path):
        import os
        from hscc_daemon import install
        venv = tmp_path / ".hermes/hermes-agent/venv/bin"
        venv.mkdir(parents=True)
        py = venv / "python"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        assert install._resolve_python() == str(py)

    def test_contains_run_at_load(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "RunAtLoad" in plist

    def test_contains_watch_paths(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert "WatchPaths" in plist
        assert "events.jsonl" in plist

    def test_valid_xml_structure(self):
        from hscc_daemon.install import generate_plist
        plist = generate_plist()
        assert plist.startswith("<?xml")
        assert plist.strip().endswith("</plist>")
        assert "<dict>" in plist
        assert "</dict>" in plist


class TestGenerateSystemdUnit:
    """generate_systemd_unit() produces valid systemd unit file."""

    def test_contains_unit_section(self):
        from hscc_daemon.install import generate_systemd_unit
        unit = generate_systemd_unit()
        assert "[Unit]" in unit
        assert "Description" in unit

    def test_contains_service_section(self):
        from hscc_daemon.install import generate_systemd_unit
        unit = generate_systemd_unit()
        assert "[Service]" in unit
        assert "ExecStart" in unit

    def test_contains_install_section(self):
        from hscc_daemon.install import generate_systemd_unit
        unit = generate_systemd_unit()
        assert "[Install]" in unit
        assert "WantedBy" in unit

    def test_hscc_daemon_in_exec_start(self):
        from hscc_daemon.install import generate_systemd_unit
        unit = generate_systemd_unit()
        assert "hscc_daemon" in unit

    def test_restart_on_failure(self):
        from hscc_daemon.install import generate_systemd_unit
        unit = generate_systemd_unit()
        assert "Restart=on-failure" in unit

    def test_contains_environment(self):
        from hscc_daemon.install import generate_systemd_unit
        unit = generate_systemd_unit()
        assert "PYTHONPATH" in unit
        assert "PATH" in unit


class TestWriteStopped:
    """_write_stopped() removes PID file safely."""

    def test_removes_pid(self, tmp_path, monkeypatch):
        from hscc_daemon import install
        pid_file = tmp_path / "daemon.pid"
        pid_file.write_text("1234")
        monkeypatch.setattr(install, "PID_FILE", str(pid_file))
        install._write_stopped()
        assert not pid_file.exists()

    def test_noop_missing(self, tmp_path, monkeypatch):
        from hscc_daemon import install
        monkeypatch.setattr(install, "PID_FILE", str(tmp_path / "daemon.pid"))
        install._write_stopped()  # should not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
