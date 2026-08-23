"""Unit tests for daemon_ops.py - PID management, logging, stream watcher.

All tests isolated: PID_FILE, LOG_FILE, STATE_DIR are monkeypatched to tmp_path.
"""
import json
import os
import threading
import time

import pytest
from pathlib import Path


class TestAutodownLoop:
    """run_autodown_loop / _autodown_tick — the Phase 2 idle timer thread.

    Tests never sleep the real 30s cadence: the interval is injected, and the
    per-tick logic (_autodown_tick) is exercised directly with a monkeypatched
    config + cycle so the real ~/.hscc and ~/.hermes are never touched.
    """

    def _patch_enabled(self, monkeypatch, enabled):
        """Point autodown.load_config at a fake config; return the counter."""
        from hscc_daemon import autodown
        calls = {"n": 0}

        def fake_load_config():
            return {"enabled": enabled}

        def fake_cycle():
            calls["n"] += 1

        monkeypatch.setattr(autodown, "load_config", fake_load_config)
        monkeypatch.setattr(autodown, "cycle", fake_cycle, raising=False)
        return calls

    def test_tick_disabled_never_calls_cycle(self, monkeypatch):
        """Disabled config ⇒ cycle() is never invoked."""
        from hscc_daemon import daemon_ops
        calls = self._patch_enabled(monkeypatch, enabled=False)
        daemon_ops._autodown_tick()   # must not raise, must not call cycle
        assert calls["n"] == 0

    def test_tick_enabled_calls_cycle(self, monkeypatch):
        """Enabled config ⇒ cycle() is invoked once per tick."""
        from hscc_daemon import daemon_ops
        calls = self._patch_enabled(monkeypatch, enabled=True)
        daemon_ops._autodown_tick()
        daemon_ops._autodown_tick()
        assert calls["n"] == 2

    def test_tick_missing_cycle_is_noop(self, monkeypatch):
        """Enabled config but no cycle() yet (Phase 3) ⇒ silent no-op, no raise."""
        from hscc_daemon import autodown, daemon_ops
        monkeypatch.setattr(autodown, "load_config",
                            lambda: {"enabled": True})
        # cycle does not exist yet — getattr returns None, tick must not raise.
        daemon_ops._autodown_tick()

    def test_raising_cycle_survives_and_ticks_again(self, monkeypatch):
        """A raising cycle() is caught; the loop keeps ticking."""
        from hscc_daemon import autodown, daemon_ops
        calls = {"n": 0}
        monkeypatch.setattr(autodown, "load_config",
                            lambda: {"enabled": True})

        def flaky_cycle():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom in cycle")
        monkeypatch.setattr(autodown, "cycle", flaky_cycle, raising=False)

        # First tick: cycle raises inside _autodown_tick — must not propagate.
        daemon_ops._autodown_tick()
        # Second tick: loop survives and cycle runs again.
        daemon_ops._autodown_tick()
        assert calls["n"] == 2

    def test_loop_starts_and_stops_cleanly(self, monkeypatch):
        """run_autodown_loop exits cleanly once stop_event is set."""
        from hscc_daemon import autodown, daemon_ops
        monkeypatch.setattr(autodown, "load_config",
                            lambda: {"enabled": False})

        stop_event = threading.Event()
        t = threading.Thread(
            target=daemon_ops.run_autodown_loop,
            args=(stop_event,),
            kwargs={"interval": 0.005},
            daemon=True,
        )
        t.start()
        assert t.is_alive()
        time.sleep(0.03)  # let it tick a few times
        stop_event.set()
        t.join(timeout=2)
        assert not t.is_alive()  # exited cleanly


class TestNoLiveHsccLeak:
    """RELEASE BLOCKER regression — the suite must never touch real ~/.hscc.

    The historical bug: patching ``autodown.load_config`` to ``{"enabled":
    True}`` without patching ``save_config``/``AUTODOWN_FILE`` let Phase 6's
    activity probes drive ``record_activity()`` → ``save_config()`` straight
    into the operator's REAL ``~/.hscc/autodown.json``, arming idle-teardown.

    These tests replay that EXACT path (patched loader returning a partial
    3-key dict + real record_activity/save_config/cycle) and assert the real
    file is untouched. Green because the autouse ``_isolate_hscc`` fixture
    redirects ``AUTODOWN_FILE`` (and the activity/telegram state paths) to a
    tmp dir for every test. If that isolation is ever removed or weakened,
    these FAIL.
    """

    @staticmethod
    def _real_state_path():
        import os as _os
        return _os.path.expanduser("~/.hscc/autodown.json")

    @staticmethod
    def _real_hash_or_absent(path):
        import hashlib
        try:
            with open(path, "rb") as f:
                return ("present", hashlib.sha256(f.read()).hexdigest())
        except FileNotFoundError:
            # Untouched means: if it didn't exist before, it must not exist now.
            return ("absent", None)

    def test_patched_loader_record_activity_does_not_touch_real_file(
            self, monkeypatch):
        """Replay the documented leak: patched loader + real record_activity."""
        from hscc_daemon import autodown
        real = self._real_state_path()
        before = self._real_hash_or_absent(real)
        # The historical partial 3-key dict a patched loader returns.
        monkeypatch.setattr(autodown, "load_config",
                            lambda: {"enabled": True})
        # Force the leak path through the REAL record_activity → save_config.
        autodown.record_activity("http")
        autodown.record_activity("kanban")
        after = self._real_hash_or_absent(real)
        assert before == after, (
            "TEST SUITE WROTE TO REAL ~/.hscc/autodown.json! "
            "before=%r after=%r" % (before, after)
        )

    def test_cycle_with_default_probes_does_not_touch_real_file(
            self, monkeypatch, tmp_path):
        """A full enabled cycle() with default probes must stay contained."""
        from hscc_daemon import autodown
        real = self._real_state_path()
        before = self._real_hash_or_absent(real)
        # Enabled, up, long-idle config with a fake kanban board that reports
        # live work — the exact conditions under which the leak fired.
        monkeypatch.setattr(autodown, "load_config",
                            lambda: {"enabled": True, "state": "up",
                                     "last_activity_iso": "2000-01-01T00:00:00+00:00"})
        # Inject an in-memory kanban lib so probe_kanban_activity fires and
        # stamps (reaching record_activity + save_config like the real cycle).
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO tasks (id, status) VALUES ('t-1', 'running')")
        conn.commit()

        class _Kb:
            from contextlib import contextmanager
            @contextmanager
            def connect_closing(self):
                yield conn

        # stop() is never reached because active work blocks teardown, but the
        # probes + record_activity run first — the leak surface.
        autodown.cycle(kanban_db=_Kb(), agents_file=str(tmp_path / "agents.json"),
                       probes=None)
        after = self._real_hash_or_absent(real)
        assert before == after, (
            "TEST SUITE WROTE TO REAL ~/.hscc/autodown.json! "
            "before=%r after=%r" % (before, after)
        )


class TestAutodownStartupRecovery:
    """Phase 8 wiring — a broken autodown must never stop the daemon booting.

    ``run_daemon_loop`` calls ``autodown.resume_from_restart_defensive()`` at
    startup (daemon_ops.py:173-183). That wrapper is the daemon's boot
    guarantee: even if ``resume_from_restart`` raises (corrupt config, I/O
    error, anything), the daemon proceeds. The autouse ``_isolate_hscc``
    fixture redirects the real ~/.hscc, and these tests call the wrapper
    directly (the full ``run_daemon_loop`` is a monolithic thread-spawning
    function not practically testable in-process).
    """

    def test_startup_wrapper_swallows_raise(self, monkeypatch):
        """A raising resume_from_restart does NOT propagate — the daemon can
        still boot (daemon_ops's startup hook contract)."""
        from hscc_daemon import autodown
        # resume raising (e.g. corrupt autodown.json blew up load/save).
        monkeypatch.setattr(autodown, "resume_from_restart",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # The wrapper must swallow it — this is what run_daemon_loop calls.
        autodown.resume_from_restart_defensive()  # must not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


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
