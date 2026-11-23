"""Unit tests for util.py - utility functions.

Tests are fully isolated: subprocess is mocked, file I/O uses tmp_path.
"""
import json
import os
import sys
import time

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
        # stderr is folded into output so failure diagnostics are never empty
        # (sparkrun writes its errors to stderr — see run_cmd docstring).
        assert "not found" in result["output"]

    def test_failure_folds_stdout_and_stderr(self, fake_subprocess):
        from hscc_daemon.util import run_cmd
        fake_subprocess.set_result(stdout="partial", stderr="boom", returncode=1)
        result = run_cmd(["false"])
        assert result["ok"] is False
        assert "partial" in result["output"]
        assert "boom" in result["output"]

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
        result = ssh_cmd("192.0.2.1", "echo hello")
        assert result["ok"] is True
        assert result["output"] == "reachable"

    def test_ssh_failure(self, fake_subprocess):
        from hscc_daemon.util import ssh_cmd
        fake_subprocess.set_result(stdout="", stderr="Connection refused", returncode=1)
        result = ssh_cmd("192.0.2.1", "ls")
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

    def test_lan_errno65_gets_macos_hint(self, monkeypatch):
        """errno 65 (EHOSTUNREACH) to a LAN/private IP should carry the macOS
        Local Network hint — this is the daemon-keeps-failing footgun where a
        denied app gets 'No route to host' for a reachable fleet endpoint."""
        from hscc_daemon import util
        import urllib.error

        def _throw(*a, **kw):
            raise urllib.error.URLError(
                OSError(65, "No route to host", "192.0.2.10"))

        monkeypatch.setattr("urllib.request.urlopen", _throw)
        result = util.http_check("http://192.0.2.10:8000/health", timeout=5)
        assert result["ok"] is False
        assert "Local Network" in result["output"]
        assert "No route to host" in result["output"]

    def test_lan_errno65_on_public_ip_no_hint(self, monkeypatch):
        """Same errno to a public IP should NOT get the LAN hint."""
        from hscc_daemon import util
        import urllib.error

        def _throw(*a, **kw):
            raise urllib.error.URLError(OSError(65, "No route to host", "1.1.1.1"))

        monkeypatch.setattr("urllib.request.urlopen", _throw)
        result = util.http_check("http://1.1.1.1/health", timeout=5)
        assert result["ok"] is False
        assert "Local Network" not in result["output"]

    def test_non_host_unreach_error_no_hint(self, monkeypatch):
        """A refusal to a private IP (different errno) should not get the hint."""
        from hscc_daemon import util
        import urllib.error

        def _throw(*a, **kw):
            raise urllib.error.URLError(
                OSError(61, "Connection refused", "192.0.2.10"))

        monkeypatch.setattr("urllib.request.urlopen", _throw)
        result = util.http_check("http://192.0.2.10:8000/health", timeout=5)
        assert result["ok"] is False
        assert "Local Network" not in result["output"]


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


class TestRunCmdUntilReady:
    """``run_cmd(ready=…)``: the bounded wait that ends when the WORKLOAD is up.

    t_09708c17 DEFECT 3. A ``sparkrun run`` CLIENT can sit on a child ssh
    session for tens of minutes after the container it launched is serving, so
    "the client returned" is not a success signal a resume may trust. These run
    REAL children (no fake_subprocess): the point is that the wait is bounded,
    that the child is really signalled, and that the outcome is reported as
    "verified by the probe, client detached" rather than as a hang or a failure.
    """

    #: A child that starts, says so, and then never returns on its own.
    WEDGE = "import sys, time; print('client up', flush=True); time.sleep(600)"

    def _wedged_child(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        script = (f"import os, time\n"
                  f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                  f"print('client up', flush=True)\n"
                  f"time.sleep(600)\n")
        path = tmp_path / "wedged_client.py"
        path.write_text(script)
        return [sys.executable, str(path)], pid_file

    def test_a_child_that_becomes_ready_is_cut_loose_and_reports_ok(self):
        from hscc_daemon.util import run_cmd
        started = time.monotonic()
        res = run_cmd([sys.executable, "-c", self.WEDGE], timeout=120,
                      ready=lambda: True)
        assert res["ok"] is True
        assert res["detached"] is True and res["timed_out"] is False
        assert res["verified_by"] == "health probe"
        # The probe ended the wait, not the bound.
        assert time.monotonic() - started < 15, res

    def test_the_cut_loose_client_is_really_gone(self, tmp_path):
        """A detach that leaves an orphaned client is a leak, not a detach."""
        import errno

        from hscc_daemon.util import run_cmd
        argv, pid_file = self._wedged_child(tmp_path)
        # "Ready" = the client got far enough to record its own pid and is still
        # sitting there: the detach happens mid-flight, not after an exit.
        res = run_cmd(argv, timeout=120, ready=pid_file.exists)
        assert res["detached"] is True and res["ok"] is True
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError as e:
                # EPERM would mean "still alive, not ours"; only "no such
                # process" counts as reaped.
                assert e.errno == errno.ESRCH, e
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"client {pid} outlived the detach")

    def test_the_deadline_alone_also_cuts_loose_and_says_timed_out(self):
        from hscc_daemon.util import run_cmd
        started = time.monotonic()
        res = run_cmd([sys.executable, "-c", self.WEDGE], timeout=1,
                      ready=lambda: False)
        assert res["ok"] is False
        assert res["detached"] is True and res["timed_out"] is True
        assert "timed out after 1s" in res["output"]
        assert time.monotonic() - started < 15, res

    def test_a_child_that_exits_on_its_own_is_not_a_detach(self, tmp_path):
        """The ordinary path: ``--ensure`` returns and nothing was signalled.
        stdout/stderr keep the shape ``run_cmd`` has always returned."""
        from hscc_daemon.util import run_cmd
        script = tmp_path / "quick.py"
        script.write_text("import sys\n"
                          "print('ensured')\n"
                          "sys.stderr.write('a warning\\n')\n"
                          "sys.exit(0)\n")
        res = run_cmd([sys.executable, str(script)], timeout=30,
                      ready=lambda: False)
        assert res["ok"] is True and res["detached"] is False
        assert res["output"] == "ensured" and res["stderr"] == "a warning"
        assert "verified_by" not in res

    def test_a_failing_child_still_reports_its_folded_stderr(self, tmp_path):
        from hscc_daemon.util import run_cmd
        script = tmp_path / "fails.py"
        script.write_text("import sys\n"
                          "sys.stderr.write('Recipe not found\\n')\n"
                          "sys.exit(3)\n")
        res = run_cmd([sys.executable, str(script)], timeout=30,
                      ready=lambda: False)
        assert res["ok"] is False and res["detached"] is False
        assert "Recipe not found" in res["output"] and "Recipe" in res["stderr"]

    def test_a_probe_that_raises_is_answered_as_not_ready(self):
        """A refusing endpoint is 'not up yet', not an error that aborts a
        launch already in flight."""
        from hscc_daemon.util import run_cmd

        def angry():
            raise ConnectionRefusedError("connection refused")

        res = run_cmd([sys.executable, "-c", self.WEDGE], timeout=1,
                      ready=angry)
        assert res["ok"] is False and res["timed_out"] is True

    def test_a_command_that_cannot_start_is_loud_not_a_hang(self, tmp_path):
        from hscc_daemon.util import run_cmd
        res = run_cmd([str(tmp_path / "no-such-binary")], timeout=5,
                      ready=lambda: True)
        assert res["ok"] is False
        assert "Command failed" in res["output"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
