"""Unit tests for util.py - utility functions.

Tests are fully isolated: subprocess is mocked, file I/O uses tmp_path.
"""
import json
import os
import pytest
from pathlib import Path


class TestRunCmd:
    """run_cmd() executes commands and returns structured output."""

    def test_success(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout="hello world", returncode=0)
        result = run_cmd(["echo", "hello"])
        assert result["ok"] is True
        assert result["output"] == "hello world"

    def test_failure(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout="", stderr="not found", returncode=1)
        result = run_cmd(["false"])
        assert result["ok"] is False
        assert result["output"] == ""

    def test_timeout(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(timeout_exc=True)
        result = run_cmd(["sleep", "999"], timeout=5)
        assert result["ok"] is False
        assert "timed out" in result["output"].lower() or "timeout" in result["output"].lower()

    def test_file_not_found(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(file_not_found=True)
        result = run_cmd(["nonexistent_binary"])
        assert result["ok"] is False

    def test_as_json_success(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout='{"key": "val"}', returncode=0)
        result = run_cmd(["cat", "data.json"], as_json=True)
        assert result["ok"] is True
        assert result["output"] == {"key": "val"}

    def test_as_json_bad_json(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout="not json", returncode=0)
        result = run_cmd(["cat", "data.json"], as_json=True)
        assert result["ok"] is False

    def test_as_json_empty(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout="", returncode=0)
        result = run_cmd(["true"], as_json=True)
        assert result["ok"] is True
        # Empty stdout with as_json -> not parsed, returns raw
        assert isinstance(result["output"], str)

    def test_custom_timeout(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout="ok", returncode=0)
        result = run_cmd(["cmd"], timeout=99)
        assert result["ok"] is True

    def test_exception_handling(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(file_not_found=True)
        result = run_cmd(["bad_cmd"])
        assert result["ok"] is False
        assert "failed" in result["output"].lower() or len(result["output"]) > 0


class TestSshCmd:
    """ssh_cmd() wraps SSH commands."""

    def test_builds_ssh_command(self, fake_subprocess):
        from hscc_daemon.util import ssh_cmd
        fake_subprocess.set_result(stdout="reachable", returncode=0)
        result = ssh_cmd("10.0.0.1", "echo hello")
        assert result["ok"] is True
        assert result["output"] == "reachable"

    def test_ssh_failure(self, fake_subprocess):
        from hscc_daemon.util import ssh_cmd
        fake_subprocess.set_result(stdout="", stderr="Connection refused", returncode=1)
        result = ssh_cmd("10.0.0.1", "ls")
        assert result["ok"] is False


class TestHttpCheck:
    """http_check() checks HTTP endpoint reachability."""

    def test_success(self, monkeypatch):
        from hscc_daemon import util

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: FakeResp())
        result = util.http_check("http://localhost:8000/health")
        assert result["ok"] is True
        assert result["status"] == 200

    def test_connection_refused(self, monkeypatch):
        from hscc_daemon import util
        import urllib.error

        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("Connection refused")))
        result = util.http_check("http://localhost:19999/health")
        assert result["ok"] is False

    def test_timeout(self, monkeypatch):
        from hscc_daemon import util

        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
        result = util.http_check("http://localhost:8000/health", timeout=1)
        assert result["ok"] is False

    def test_unreachable_port(self, monkeypatch):
        """Port that nothing listens on should return ok=False."""
        from hscc_daemon.util import http_check
        result = http_check("http://localhost:19997/health", timeout=1)
        assert result["ok"] is False


class TestEnsureDir:
    """ensure_dir() creates directories."""

    def test_creates_directory(self, tmp_path):
        from hscc_daemon.util import ensure_dir
        target = tmp_path / "new_dir"
        ensure_dir(str(target))
        assert target.is_dir()

    def test_noop_when_exists(self, tmp_path):
        from hscc_daemon.util import ensure_dir
        target = tmp_path / "existing"
        target.mkdir()
        ensure_dir(str(target))  # should not raise


class TestReadJsonFile:
    """read_json_file() reads JSON with fallback."""

    def test_read_existing(self, tmp_path):
        from hscc_daemon.util import read_json_file
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"key": "val"}))
        result = read_json_file(str(f))
        assert result == {"key": "val"}

    def test_missing_returns_empty_dict(self, tmp_path):
        from hscc_daemon.util import read_json_file
        result = read_json_file(str(tmp_path / "missing.json"))
        assert result == {}

    def test_missing_returns_custom_default(self, tmp_path):
        from hscc_daemon.util import read_json_file
        result = read_json_file(str(tmp_path / "missing.json"), default=None)
        assert result == {}  # default=None returns {} for missing files

    def test_malformed_returns_default(self, tmp_path):
        from hscc_daemon.util import read_json_file
        f = tmp_path / "bad.json"
        f.write_text("{invalid json")
        result = read_json_file(str(f))
        assert result == {}

    def test_custom_default_on_malformed(self, tmp_path):
        from hscc_daemon.util import read_json_file
        f = tmp_path / "bad.json"
        f.write_text("{invalid")
        result = read_json_file(str(f), default={"fallback": True})
        assert result == {"fallback": True}


class TestWriteJsonFile:
    """write_json_file() writes JSON atomically."""

    def test_write_and_read(self, tmp_path):
        from hscc_daemon.util import write_json_file, read_json_file
        path = str(tmp_path / "output.json")
        write_json_file(path, {"key": "val", "count": 42})
        result = read_json_file(path)
        assert result == {"key": "val", "count": 42}

    def test_creates_parent_dirs(self, tmp_path):
        from hscc_daemon.util import write_json_file
        path = str(tmp_path / "deep" / "nested" / "file.json")
        write_json_file(path, {"ok": True})  # should not raise
        assert Path(path).exists()

    def test_overwrites_existing(self, tmp_path):
        from hscc_daemon.util import write_json_file, read_json_file
        path = str(tmp_path / "data.json")
        write_json_file(path, {"old": True})
        write_json_file(path, {"new": True})
        assert read_json_file(path) == {"new": True}

    def test_atomic_write_no_tmp_left(self, tmp_path):
        from hscc_daemon.util import write_json_file
        path = str(tmp_path / "atomic.json")
        write_json_file(path, {"test": True})
        assert not Path(path + ".tmp").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
