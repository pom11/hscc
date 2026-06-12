"""Unit tests for daemon_ops.py - PID management, logging, stream watcher.

All tests isolated: PID_FILE, LOG_FILE, STATE_DIR are monkeypatched to tmp_path.
"""
import json
import os
import pytest
from pathlib import Path


class TestGetPid:
    """get_pid() reads PID from file and verifies process."""

    def test_no_pid_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        assert daemon_ops.get_pid() is None

    def test_stale_pid(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        (tmp_hfcc_dir / "pid").write_text("99999")  # non-existent process
        # Should return None since process 99999 doesn't exist
        result = daemon_ops.get_pid()
        # In test env, os.kill(99999, 0) raises OSError -> returns None
        assert result is None

    def test_current_pid(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        (tmp_hfcc_dir / "pid").write_text(str(os.getpid()))
        result = daemon_ops.get_pid()
        assert result == os.getpid()

    def test_invalid_pid_content(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        (tmp_hfcc_dir / "pid").write_text("not_a_number")
        assert daemon_ops.get_pid() is None

    def test_empty_pid_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        (tmp_hfcc_dir / "pid").write_text("")
        assert daemon_ops.get_pid() is None


class TestSavePid:
    """save_pid() writes current PID to file."""

    def test_writes_pid(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        daemon_ops.save_pid()
        content = (tmp_hfcc_dir / "pid").read_text()
        assert int(content) == os.getpid()

    def test_creates_parent_dir(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        target = tmp_hfcc_dir / "nested" / "pid"
        target.parent.mkdir(parents=True)
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(target))
        daemon_ops.save_pid()  # should not raise
        assert target.exists()


class TestWriteStopped:
    """write_stopped() removes PID file."""

    def test_removes_pid_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        (tmp_hfcc_dir / "pid").write_text("123")
        daemon_ops.write_stopped()
        assert not (tmp_hfcc_dir / "pid").exists()

    def test_noop_when_missing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        daemon_ops.write_stopped()  # should not raise


class TestLog:
    """log() writes timestamped log lines."""

    def test_writes_log_line(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        pid_file = tmp_hfcc_dir / "pid"
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(pid_file))
        log_file = tmp_hfcc_dir / "log"
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(log_file))
        # No PID file -> foreground mode, also prints
        daemon_ops.log("test message")
        content = log_file.read_text()
        assert "test message" in content
        assert "INFO" in content
        # Has timestamp
        assert "[" in content and "T" in content

    def test_log_level(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        pid_file = tmp_hfcc_dir / "pid"
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(pid_file))
        log_file = tmp_hfcc_dir / "log"
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(log_file))
        daemon_ops.log("error occurred", "ERROR")
        content = log_file.read_text()
        assert "ERROR" in content
        assert "error occurred" in content

    def test_append_mode(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        pid_file = tmp_hfcc_dir / "pid"
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(pid_file))
        log_file = tmp_hfcc_dir / "log"
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(log_file))
        daemon_ops.log("first")
        daemon_ops.log("second")
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]


class TestGetDaemonLogTail:
    """get_daemon_log_tail() reads last N lines from log."""

    def test_no_log_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
        assert daemon_ops.get_daemon_log_tail() == []

    def test_reads_lines(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
        log_file = tmp_hfcc_dir / "log"
        log_file.write_text("line1\nline2\nline3\nline4\nline5\n")
        lines = daemon_ops.get_daemon_log_tail(3)
        assert len(lines) == 3
        assert "line3" in lines[0]
        assert "line5" in lines[2]

    def test_all_lines_when_fewer_than_limit(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
        log_file = tmp_hfcc_dir / "log"
        log_file.write_text("a\nb\n")
        lines = daemon_ops.get_daemon_log_tail(50)
        assert len(lines) == 2

    def test_default_50_lines(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
        log_file = tmp_hfcc_dir / "log"
        log_file.write_text("\n".join(f"line{i}" for i in range(100)))
        lines = daemon_ops.get_daemon_log_tail()
        assert len(lines) == 50


class TestEnsureStateDir:
    """ensure_state_dir() creates state directory."""

    def test_creates_directory(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "STATE_DIR", str(tmp_hfcc_dir / "state"))
        daemon_ops.ensure_state_dir()
        assert (tmp_hfcc_dir / "state").is_dir()


class TestPruneDeadFiles:
    """prune_dead_files() removes .corrupt-*/.stale + caps .bak.* groups."""

    def test_removes_corrupt_and_stale(self, tmp_path):
        from hscc_daemon import daemon_ops
        (tmp_path / "serving.json.corrupt-112313").write_text("x")
        (tmp_path / "models.json.corrupt-112344").write_text("x")
        (tmp_path / "autonomy.stale").write_text("on")
        (tmp_path / "watchdog_block.json.stale").write_text("{}")
        (tmp_path / "serving.json").write_text("{}")        # live file — keep
        res = daemon_ops.prune_dead_files(str(tmp_path))
        assert res["removed_dead"] == 4
        assert not list(tmp_path.glob("*.corrupt-*"))
        assert not list(tmp_path.glob("*.stale"))
        assert (tmp_path / "serving.json").exists()         # live untouched

    def test_caps_bak_groups(self, tmp_path):
        import os
        from hscc_daemon import daemon_ops
        for stem in ("serving.json", "models.json"):
            for i in range(9):
                f = tmp_path / f"{stem}.bak.{1000+i}"
                f.write_text("{}")
                os.utime(f, (1000 + i, 1000 + i))
        res = daemon_ops.prune_dead_files(str(tmp_path))
        assert len(list(tmp_path.glob("serving.json.bak.*"))) == 5
        assert len(list(tmp_path.glob("models.json.bak.*"))) == 5
        assert res["pruned_bak"] == 8                       # 2 groups × (9-5)... =8

    def test_idempotent_and_safe_on_empty(self, tmp_path):
        from hscc_daemon import daemon_ops
        res = daemon_ops.prune_dead_files(str(tmp_path))
        assert res == {"removed_dead": 0, "pruned_bak": 0}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
