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
        # This tick reaches the REAL cycle(), whose DEFAULT probes include
        # probe_prci_activity → the live `gh` PR/CI screen. Stub the SCREEN (not
        # _has_active_pr_ci — the probe is a separate consumer) so the no-op
        # assertion cannot depend on this host's GitHub reachability (t_18318fd9:
        # the GH PROBE FENCE now flags an unstubbed probe instead of letting it
        # silently run 12 live gh calls on every run).
        monkeypatch.setattr(autodown, "_screen_prci",
                            lambda *a, **k: {"open_prs": 0, "active_runs": 0,
                                             "repos": []})
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
    redirects ``AUTODOWN_FILE`` (and the activity state paths) to a
    tmp dir for every test. If that isolation is ever removed or weakened,
    these FAIL.
    """

    @staticmethod
    def _real_state_path():
        import os as _os
        # Bypass the autouse fixture's os.path.expanduser redirect: expand
        # "~" (NOT under ~/.hscc, so delegated to the real home dir) and join
        # the literal ".hscc/autodown.json" to REACH the operator's real file.
        # The fixture redirects "~/.hscc/..." via expanduser; this must not.
        return _os.path.join(_os.path.expanduser("~"), ".hscc/autodown.json")

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
        # The DEFAULT probes stay in place (that is this test's subject), but the
        # PR/CI screen is stubbed: its default implementation fires 12 live `gh`
        # probes, so an unstubbed run made this test's outcome depend on this
        # host's GitHub reachability (t_18318fd9 — the GH PROBE FENCE now flags
        # it). Clear screen ⇒ no "prci" stamp; the kanban probe still stamps, so
        # record_activity + save_config still run and the leak path is intact.
        monkeypatch.setattr(autodown, "_screen_prci",
                            lambda *a, **k: {"open_prs": 0, "active_runs": 0,
                                             "repos": []})
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


class TestSupervisedPeriodic:
    """_run_supervised_periodic — a periodic check thread that dies must be
    detected and restarted, never left dark (the "NAS check silently stops"
    bug class). Module-level so it is directly testable without booting the
    full monolithic run_daemon_loop."""

    @staticmethod
    def _start_supervisor(monkeypatch, check_fn, interval=0.005):
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "log", lambda *a, **kw: None)
        stop = threading.Event()
        t = threading.Thread(
            target=daemon_ops._run_supervised_periodic,
            args=(stop, check_fn, interval, "nas"),
            daemon=True,
        )
        t.start()
        return stop, t

    def test_dead_thread_is_restarted_and_runs_again(self, monkeypatch):
        """A check that DIES (raises a BaseException, which the old
        `except Exception` silently let out) is detected by the supervisor and
        restarted: the check runs a second time instead of the stream going
        dark forever."""
        calls = {"n": 0}

        def flaky_check():
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate the abnormal death the old loop missed: only
                # `except BaseException` catches this; the old `except
                # Exception` let it kill the thread with no one watching.
                raise SystemExit("simulated thread death")

        stop, t = self._start_supervisor(monkeypatch, flaky_check)
        # Wait until the supervisor has respawned the thread at least once.
        deadline = time.time() + 5
        while calls["n"] < 2 and time.time() < deadline:
            time.sleep(0.005)
        stop.set()
        t.join(timeout=3)
        assert calls["n"] >= 2, (
            f"check was NOT restarted after dying; ran {calls['n']}x")

    def test_supervisor_exits_on_stop_even_if_check_raises_every_time(
            self, monkeypatch):
        """A check that dies on EVERY call must not spin the supervisor into an
        unbounded tight loop: once stop_event is set the supervisor unwinds.
        The respawn loop is gated on stop_event, so setting it terminates the
        supervisor cleanly."""
        calls = {"n": 0}

        def always_dying_check():
            calls["n"] += 1
            raise RuntimeError("ordinary exception — kept alive, not a death")

        stop, t = self._start_supervisor(monkeypatch, always_dying_check,
                                         interval=0.002)
        # Let it run a few cycles (ordinary exceptions never kill the thread),
        # then stop.
        deadline = time.time() + 3
        while calls["n"] < 3 and time.time() < deadline:
            time.sleep(0.005)
        stop.set()
        t.join(timeout=3)
        assert not t.is_alive()  # supervisor exited cleanly on stop

    def test_ordinary_exception_does_not_kill_thread(self, monkeypatch):
        """A check raising a NORMAL Exception (the common case) must NOT be
        treated as a death: `_run_periodic` catches it, logs, and keeps the
        SAME thread alive — no restart churn."""
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "log", lambda *a, **kw: None)
        calls = {"n": 0}

        def flaky_check():
            calls["n"] += 1
            raise ValueError("transient blip")

        stop = threading.Event()
        # Run _run_periodic DIRECTLY (not via the supervisor) — this is what a
        # living check thread runs. An ordinary exception must not make it
        # return, so the check keeps running on the same thread.
        t = threading.Thread(
            target=daemon_ops._run_periodic,
            args=(stop, flaky_check, 0.002, "nas"),
            daemon=True,
        )
        t.start()
        deadline = time.time() + 3
        while calls["n"] < 3 and time.time() < deadline:
            time.sleep(0.005)
        assert calls["n"] >= 3, f"thread died on an ordinary exception ({calls['n']})"
        stop.set()
        t.join(timeout=2)


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


class TestTouchHeartbeat:
    """touch_heartbeat() writes a parseable UTC ISO timestamp to HEARTBEAT_FILE
    and updates it on successive calls."""

    def test_writes_parseable_iso_and_advances(self, tmp_hfcc_dir, monkeypatch):
        import datetime
        from hscc_daemon import daemon_ops
        hb = tmp_hfcc_dir / "heartbeat"
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(hb))

        first = daemon_ops.touch_heartbeat()
        assert first is not None, "touch_heartbeat returned None"
        ts1 = datetime.datetime.fromisoformat(first)
        assert ts1.tzinfo is not None

        # A successive call writes a NEW (>=) timestamp — the file advances.
        second = daemon_ops.touch_heartbeat()
        ts2 = datetime.datetime.fromisoformat(second)
        assert ts2 >= ts1
        # And the on-disk content matches what was reported + parses.
        disk = (tmp_hfcc_dir / "heartbeat").read_text().strip()
        assert datetime.datetime.fromisoformat(disk).tzinfo is not None

    def test_creates_parent_dir(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        target = tmp_hfcc_dir / "nested" / "heartbeat"
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(target))
        daemon_ops.touch_heartbeat()  # must not raise
        assert target.exists()

    def test_never_raises_on_bad_path(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        # Simulate a failure by making the target a directory that cannot be
        # opened as a file — the daemon must not die because a heartbeat write
        # failed (same guarantee as log()).
        d = tmp_hfcc_dir / "adir"
        d.mkdir()
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(d))
        assert daemon_ops.touch_heartbeat() is None


class TestRunHeartbeatLoop:
    """run_heartbeat_loop() (the daemon's main-loop body) advances the
    heartbeat while running and stops advancing when the loop exits."""

    def test_advances_while_running_stops_on_exit(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import daemon_ops
        hb = tmp_hfcc_dir / "heartbeat"
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(hb))

        stop_event = threading.Event()
        t = threading.Thread(
            target=daemon_ops.run_heartbeat_loop,
            args=(stop_event,),
            kwargs={"interval": 0.01},
            daemon=True,
        )
        t.start()
        # Let it run a while — the heartbeat must be written and re-advance.
        time.sleep(0.06)
        assert hb.exists(), "heartbeat not written while loop running"
        content_while_running = hb.read_text().strip()
        assert content_while_running, "heartbeat file empty while running"

        # Stop the loop: it exits, and the file freezes (no longer advances).
        stop_event.set()
        t.join(timeout=5)
        assert not t.is_alive(), "heartbeat loop did not exit on stop"
        frozen = hb.read_text().strip()
        time.sleep(0.05)
        assert hb.read_text().strip() == frozen, (
            "heartbeat kept advancing after the loop exited")

    def test_staleness_floor_constant(self):
        """HEARTBEAT_STALE_AFTER is derived from the constant, not hardcoded:
        max(2x cadence, 120 floor). Changing the cadence changes staleness."""
        from hscc_daemon import daemon_ops
        assert daemon_ops.HEARTBEAT_INTERVAL == 300
        assert daemon_ops.HEARTBEAT_STALE_AFTER == max(
            2 * daemon_ops.HEARTBEAT_INTERVAL, 120)
        assert daemon_ops.HEARTBEAT_STALE_AFTER == 600


class TestDaemonLiveness:
    """daemon_liveness() combines pid-file pid + aliveness + heartbeat
    freshness into one structured answer."""

    def _liveness(self, monkeypatch, tmp_hfcc_dir, pid_content=None,
                  heartbeat_content=None):
        """Point PID_FILE + HEARTBEAT_FILE at tmp files with given content."""
        from hscc_daemon import daemon_ops
        pid_file = tmp_hfcc_dir / "daemon.pid"
        hb_file = tmp_hfcc_dir / "heartbeat"
        if pid_content is not None:
            pid_file.write_text(str(pid_content))
        if heartbeat_content is not None:
            hb_file.write_text(str(heartbeat_content))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(pid_file))
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(hb_file))
        return daemon_ops.daemon_liveness()

    def test_pid_file_missing(self, monkeypatch, tmp_hfcc_dir):
        liv = self._liveness(monkeypatch, tmp_hfcc_dir)
        assert liv["pid"] is None
        assert liv["pid_file_present"] is False
        assert liv["alive"] is False
        assert liv["state"] == "pid-gone"

    def test_pid_present_process_gone(self, monkeypatch, tmp_hfcc_dir):
        liv = self._liveness(monkeypatch, tmp_hfcc_dir, pid_content=99999)
        assert liv["pid"] == 99999
        assert liv["pid_file_present"] is True
        assert liv["alive"] is False
        assert liv["state"] == "pid-gone"

    def test_pid_alive_no_heartbeat(self, monkeypatch, tmp_hfcc_dir):
        liv = self._liveness(monkeypatch, tmp_hfcc_dir, pid_content=os.getpid())
        assert liv["alive"] is True
        assert liv["last_heartbeat"] is None
        assert liv["heartbeat_present"] is False
        assert liv["stale"] is False   # no heartbeat ⇒ not stale, just absent
        assert liv["state"] == "running-no-heartbeat"

    def test_pid_alive_heartbeat_fresh(self, monkeypatch, tmp_hfcc_dir):
        import datetime
        from hscc_daemon import daemon_ops
        fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
        liv = self._liveness(monkeypatch, tmp_hfcc_dir, pid_content=os.getpid(),
                             heartbeat_content=fresh)
        assert liv["alive"] is True
        assert liv["heartbeat_present"] is True
        assert liv["stale"] is False
        assert liv["state"] == "running-fresh"

    def test_pid_alive_heartbeat_stale(self, monkeypatch, tmp_hfcc_dir):
        import datetime
        from hscc_daemon import daemon_ops
        # Older than the threshold (derived from the constant, not hardcoded).
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(
                   seconds=daemon_ops.HEARTBEAT_STALE_AFTER + 5)).isoformat()
        liv = self._liveness(monkeypatch, tmp_hfcc_dir, pid_content=os.getpid(),
                             heartbeat_content=old)
        assert liv["alive"] is True
        assert liv["heartbeat_present"] is True
        assert liv["stale"] is True
        assert liv["state"] == "running-stale-heartbeat"

    def test_just_within_threshold_not_stale(self, monkeypatch, tmp_hfcc_dir):
        import datetime
        from hscc_daemon import daemon_ops
        # Just inside the threshold (healthy-but-slow slip) — NOT stale, so we
        # never cry wolf. Boundary computed from the constant.
        age = daemon_ops.HEARTBEAT_STALE_AFTER - 10
        recent = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(seconds=age)).isoformat()
        liv = self._liveness(monkeypatch, tmp_hfcc_dir, pid_content=os.getpid(),
                             heartbeat_content=recent)
        assert liv["stale"] is False
        assert liv["state"] == "running-fresh"

    def test_corrupt_heartbeat_treated_absent_not_alive(self, monkeypatch,
                                                        tmp_hfcc_dir):
        liv = self._liveness(monkeypatch, tmp_hfcc_dir, pid_content=os.getpid(),
                             heartbeat_content="not-a-timestamp")
        assert liv["heartbeat_present"] is False
        assert liv["last_heartbeat"] is None
        assert liv["stale"] is False
        assert liv["state"] == "running-no-heartbeat"


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

    def test_foreground_echo_goes_to_stderr_not_stdout(
            self, capsys, tmp_hfcc_dir, monkeypatch):
        """The foreground-mode echo must NOT touch stdout (t_18318fd9).

        stdout is the machine channel for every HSCC CLI: `--json` is a raw
        `json.dumps` printed there. While PID_FILE was absent the echo went to
        stdout, so a single WARN could splice itself in front of a JSON
        document and break the caller's `json.loads` — that is exactly the
        intermittent `hscc autodown status --json` failure this pins closed.
        """
        from hscc_daemon import daemon_ops
        pid_file = tmp_hfcc_dir / "pid"          # never created -> foreground
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(pid_file))
        log_file = tmp_hfcc_dir / "log"
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(log_file))

        daemon_ops.log("interlock unevaluable", "WARN")

        captured = capsys.readouterr()
        assert captured.out == "", (
            f"log() wrote to stdout; a --json payload would be corrupted: "
            f"{captured.out!r}")
        assert "[ WARN] interlock unevaluable" in captured.err
        # Visibility is not lost — the file write is unconditional.
        assert "[ WARN] interlock unevaluable" in log_file.read_text()

    def test_daemon_mode_prints_nothing(self, capsys, tmp_hfcc_dir,
                                        monkeypatch):
        """With a live PID file (daemon mode) there is no echo at all — only
        the file. Guards against a fix that printed unconditionally."""
        from hscc_daemon import daemon_ops
        pid_file = tmp_hfcc_dir / "pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(pid_file))
        log_file = tmp_hfcc_dir / "log"
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(log_file))

        daemon_ops.log("quiet please", "WARN")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        assert "quiet please" in log_file.read_text()


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


class TestLogRotation:
    """daemon_ops.append_rotating() size-caps + gzips rolled generations.

    The daemon log (~/.hscc/daemon.log) had NO rotation and grew unbounded
    (289 MiB / 104.6 days ≈ 2.8 MiB/day average, 3.5-3.8 MiB/day recent).
    append_rotating() routes every line through a stdlib RotatingFileHandler
    so the live file is size-capped and each rolled generation is gzipped.
    These tests exercise that on temp files with a tiny cap.
    """

    @pytest.fixture(autouse=True)
    def _fresh_handlers(self):
        """Reset the module-level handler cache around each test.

        _get_logger() reads LOG_MAX_BYTES / LOG_BACKUP_COUNT ONCE when it
        first sees a path, caching the handler forever. Clearing the cache lets
        each test (re)bind the small cap without leaking handlers between
        tests or paths.
        """
        import hscc_daemon.daemon_ops as d
        d._handlers.clear()
        yield
        d._handlers.clear()

    def test_caps_file_size(self, tmp_path):
        from hscc_daemon import daemon_ops as d
        d.LOG_MAX_BYTES = 500
        d.LOG_BACKUP_COUNT = 2
        log_file = str(tmp_path / "daemon.log")
        # Write well past the cap so at least one rollover must happen.
        for i in range(50):
            d.append_rotating(log_file, f"line {i} " + "x" * 60)
        assert os.path.getsize(log_file) <= 500

    def test_rolls_gzipped_generations(self, tmp_path):
        import gzip as gz
        from hscc_daemon import daemon_ops as d
        d.LOG_MAX_BYTES = 500
        d.LOG_BACKUP_COUNT = 3
        log_file = str(tmp_path / "daemon.log")
        for i in range(80):
            d.append_rotating(log_file, f"line {i} " + "y" * 60)
        gens = sorted(tmp_path.glob("daemon.log.*.gz"))
        assert len(gens) <= 3
        # Each rolled generation is valid gzip and retains its content.
        data = gz.open(gens[0], "rt").read()
        assert "line" in data                       # content preserved
        assert data[:20].rstrip().endswith("y")     # verbatim, no mangling

    def test_retention_capped(self, tmp_path):
        from hscc_daemon import daemon_ops as d
        d.LOG_MAX_BYTES = 300
        d.LOG_BACKUP_COUNT = 2
        log_file = str(tmp_path / "daemon.log")
        for i in range(120):
            d.append_rotating(log_file, f"line {i} " + "z" * 60)
        # Only maxBytes + backupCount generations survive on disk at once.
        leftovers = sorted(tmp_path.glob("daemon.log.*.gz"))
        assert len(leftovers) <= 2
        assert all(str(p).endswith(".gz") for p in leftovers)

    def test_existing_large_file_untouched_until_cap(self, tmp_path):
        """append_rotating must NOT truncate an existing large file.

        The operator's 289 MiB ~/.hscc/daemon.log must keep its contents and
        only rotate after the cap is crossed going forward — never truncate or
        delete it as part of the change.
        """
        from hscc_daemon import daemon_ops as d
        d.LOG_MAX_BYTES = 10 ** 12      # effectively no rotation this run
        d.LOG_BACKUP_COUNT = 3
        log_file = tmp_path / "daemon.log"
        log_file.write_text("[2026-01-01T00:00:00+00:00] [ INFO] big old line\n" * 50)
        before = log_file.read_bytes()
        d.append_rotating(str(log_file), "a new line going forward")
        after = log_file.read_bytes()
        assert after.startswith(before)             # old contents preserved
        assert b"a new line going forward" in after  # new line appended
        assert not list(tmp_path.glob("daemon.log.*.gz"))  # no premature rollover


class TestWatchdogTriggerCadenceSot:
    """GAP-B-6 (t_2039a319): the watchdog + trigger-engine cadences live in
    PERIODIC_INTERVALS, the same SOT the CHECKS threads and `hscc verify`'s
    per-stream staleness math read.

    Before the fix `run_daemon_loop` started both dedicated threads with
    `stop_event.wait(30)` / `wait(15)` written inline. Both loops DO stamp a
    stream state file every tick (watchdog.json from lifecycle.pipeline_watchdog,
    triggers.json from trigger.trigger_engine), so they were streams in every
    sense verify cares about — yet verify could not see their cadence and fell
    back to the flat window. These tests pin the three things that must stay
    true, each from a different angle:

      * the dict carries both keys at the values the loops always used;
      * the loop BODIES hold no numeric `stop_event.wait()` argument at all and
        read the SOT instead (AST-scanned — a re-hardcoded literal fails, and so
        does a `wait(0.5)` retry that a naive "no 30/15" grep would pass);
      * the cadence actually FLOWS from the dict (re-point the dict and the
        thread-start log line follows), and neither name spawns a second
        supervised check thread (a CHECKS row would double-tick the same
        machinery — that would be the behaviour change the card forbids).
    """

    @staticmethod
    def _loop_fn_src(name):
        """The source text of one nested loop function in daemon_ops.py."""
        import ast
        import inspect
        from hscc_daemon import daemon_ops
        src = inspect.getsource(daemon_ops.run_daemon_loop)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(src, node) or ""
        raise AssertionError(f"{name} is no longer a function inside run_daemon_loop")

    @staticmethod
    def _wait_args(name):
        """Every stop_event.wait(...) arg node inside one loop function."""
        import ast
        from hscc_daemon import daemon_ops
        import inspect
        tree = ast.parse(inspect.getsource(daemon_ops.run_daemon_loop))
        out = []
        for fn in (n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name):
            for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
                f = call.func
                if (isinstance(f, ast.Attribute) and f.attr == "wait"
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "stop_event"):
                    out.append(call.args[0] if call.args else None)
        return out

    # ── the SOT itself ────────────────────────────────────────────────────
    def test_both_cadences_are_sot_entries(self):
        from hscc_daemon import daemon_ops
        assert daemon_ops.PERIODIC_INTERVALS["watchdog"] == 30
        assert daemon_ops.PERIODIC_INTERVALS["triggers"] == 15

    def test_declaring_the_cadences_changes_nothing_about_the_ticks(self):
        """The cadence VALUES are the ones the hardcoded literals used, so no
        tick frequency moves — the fix is where they are declared, not how
        often the loops run."""
        from hscc_daemon import daemon_ops
        assert (daemon_ops.PERIODIC_INTERVALS["watchdog"],
                daemon_ops.PERIODIC_INTERVALS["triggers"]) == (30, 15)

    # ── no literal cadence left at the sites ──────────────────────────────
    @pytest.mark.parametrize("fn_name,stream",
                             [("run_watchdog_loop", "watchdog"),
                              ("run_trigger_loop", "triggers")])
    def test_loop_body_has_no_numeric_wait_literal_and_reads_the_sot(
            self, fn_name, stream):
        import ast
        src = self._loop_fn_src(fn_name)
        args = self._wait_args(fn_name)
        assert args, f"{fn_name} no longer waits on stop_event"
        for a in args:
            assert a is not None, f"{fn_name} waits with no interval"
            # A literal here — 30, 15, or any new number — is the regression.
            assert not isinstance(a, ast.Constant), (
                f"{fn_name} still passes a hardcoded interval: {ast.dump(a)}")
            # The interval must be the local that reads the SOT key.
            assert isinstance(a, ast.Name), (
                f"{fn_name} must wait on a variable holding "
                f"PERIODIC_INTERVALS[{stream!r}], got {type(a).__name__}")
        assert f'STREAMS["{stream}"]' in src, (
            f"{fn_name} no longer reads its cadence from the SOT dict")

    # ── the cadence really flows from the dict ────────────────────────────
    def _run_loop_capturing(self, monkeypatch, intervals):
        """Boot run_daemon_loop with the supervision + tick bodies stubbed.

        Returns (log_lines, supervised_stream_names). `intervals` is the dict
        PERIODIC_INTERVALS is re-pointed to, so a value that shows up in the
        start-up log can only have come from the dict.
        """
        from hscc_daemon import daemon_ops, daemon_singleton as ds
        logs, seen = [], []

        def fake_supervised(stop_event, check_fn, interval, stream_name):
            seen.append(stream_name)
            stop_event.set()

        for path in ("hscc_daemon.autodown.resume_from_restart_defensive",
                     "hscc_daemon.serving.resolve_cluster_config",
                     "hscc_daemon.sparkrun_bin.ensure_on_path",
                     "hscc_daemon.recover.recover_engine_wedge",
                     "hscc_daemon.dispatcher_wedge.recover_dispatcher_wedge",
                     "hscc_daemon.lifecycle.pipeline_watchdog",
                     "hscc_daemon.trigger.trigger_engine"):
            monkeypatch.setattr(path, lambda *a, **k: None)
        monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                            lambda **k: (True, ""))
        monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: logs.append(a[0]))
        monkeypatch.setattr(daemon_ops, "run_autodown_loop", lambda ev, **k: None)
        monkeypatch.setattr(daemon_ops, "_run_supervised_periodic",
                            fake_supervised)
        monkeypatch.setattr(daemon_ops, "run_heartbeat_loop",
                            lambda ev, **k: ev.wait(2))
        monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
        monkeypatch.setattr(daemon_ops, "prune_dead_files",
                            lambda *a, **k: {"removed_dead": 0, "pruned_bak": 0})
        monkeypatch.setattr(daemon_ops, "PERIODIC_INTERVALS", intervals)
        daemon_ops.run_daemon_loop()
        return logs, seen

    def test_start_log_reports_the_sot_value_not_a_literal(self, monkeypatch):
        """A hardcoded log string would keep saying 30s/15s after the dict moves
        — proving the line prints the dict value is what ties the two together."""
        base = dict(__import__("hscc_daemon.daemon_ops", fromlist=["x"])
                    .PERIODIC_INTERVALS)
        base.update({"watchdog": 7, "triggers": 3})
        logs, _ = self._run_loop_capturing(monkeypatch, base)
        joined = "\n".join(logs)
        assert "Started watchdog thread (interval=7s)" in joined, joined
        assert "Started trigger engine thread (interval=3s)" in joined, joined
        assert "Started watchdog thread (interval=30s)" not in joined, joined
        assert "Started trigger engine thread (interval=15s)" not in joined, joined

    def test_the_two_streams_do_not_also_start_a_check_thread(self, monkeypatch):
        """Declaring the cadence in the SOT must NOT route the tick through the
        CHECKS table too — that would run pipeline_watchdog / trigger_engine on
        two threads at once (the behaviour change the card forbids)."""
        base = dict(__import__("hscc_daemon.daemon_ops", fromlist=["x"])
                    .PERIODIC_INTERVALS)
        _, seen = self._run_loop_capturing(monkeypatch, base)
        assert "watchdog" not in seen, seen
        assert "triggers" not in seen, seen
        # Sanity: the supervision loop really did run for the other streams.
        assert "orch_waker" in seen and "dgx" in seen, seen

    # ── verify's per-stream math now covers them ──────────────────────────
    def test_verify_sees_both_streams(self):
        from hscc_daemon import daemon_ops, verify
        intervals = verify._live_stream_intervals()
        assert intervals.get("watchdog") == daemon_ops.PERIODIC_INTERVALS["watchdog"]
        assert intervals.get("triggers") == daemon_ops.PERIODIC_INTERVALS["triggers"]

    def test_verify_staleness_uses_the_per_stream_rule_for_both(self, tmp_path):
        """A stream whose cadence verify knows gets ``max(max_age,
        2*interval+90)``; one it does not know gets the flat ``max_age`` only.

        With a tightened window the difference is observable, which is the whole
        point of the fix: a watchdog tick 140 s old is healthy (its own limit is
        2*30+90 = 150 s) but an unknown stream of the same age is stale at the
        120 s flat window. Before this card the watchdog stream WAS that unknown
        stream, so a tightened window cried wolf on a healthy daemon.
        """
        import json
        from datetime import datetime, timedelta, timezone
        from hscc_daemon.verify import check_daemon_streams
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        def stamp(stream, age_s):
            ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
            (state_dir / f"{stream}.json").write_text(json.dumps(
                {"ok": True, "stream": stream, "last_check": ts, "timestamp": ts}))

        stamp("watchdog", 140)     # inside its own 150 s per-stream limit
        stamp("unknowable", 140)   # same age, but no cadence => flat 120 s limit

        res = check_daemon_streams(state_dir=str(state_dir), max_age_s=120)
        assert res["ok"] is False, res["detail"]
        assert "unknowable.json: stale" in res["detail"], res["detail"]
        # The control proves the pass is caused by the cadence being declared,
        # not by the window being loose for everybody.
        assert "watchdog.json" not in res["detail"], res["detail"]

        # And a watchdog that misses MORE than ~2 of its own cycles is still
        # caught — declaring a cadence must not hide a genuinely dead stream.
        stamp("watchdog", 160)     # 160 > 150
        res = check_daemon_streams(state_dir=str(state_dir), max_age_s=120)
        assert "watchdog.json: stale" in res["detail"], res["detail"]

        # Fresh stamps for both real streams => clean (and triggers is covered
        # by the same per-stream rule at its own 15 s cadence).
        stamp("watchdog", 20)
        stamp("triggers", 20)
        (state_dir / "unknowable.json").unlink()
        res = check_daemon_streams(state_dir=str(state_dir), max_age_s=120)
        assert res["ok"] is True, res["detail"]

    def test_per_stream_limit_math_is_the_unchanged_rule(self):
        """The card pins the math: max(600, 2*interval+90). Both new streams are
        fast, so both land on the 600 s floor like every other fast stream —
        the rule was not adjusted for them."""
        _JITTER_S, _EXTRA_CYCLES, FLOOR = 90, 2, 600
        from hscc_daemon import daemon_ops
        for stream in ("watchdog", "triggers"):
            interval = daemon_ops.PERIODIC_INTERVALS[stream]
            assert max(FLOOR, _EXTRA_CYCLES * interval + _JITTER_S) == 600


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
