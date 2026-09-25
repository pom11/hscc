"""Unit tests for cli.py - CLI commands.

Tests cmd_status, cmd_check, cmd_triggers, cmd_notify, cmd_log,
cmd_start_daemon, and event-driven placeholders.
"""
import io
import json
import os
import pytest
import sys
from contextlib import redirect_stdout
from pathlib import Path


class TestCmdStatus:
    """cmd_status() shows daemon status and check results."""

    def test_status_output(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))

        # Capture stdout
        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_status()

        output = f.getvalue()
        assert "HSCC Daemon Status" in output
        assert "STOPPED" in output  # no PID file

    def test_status_with_state(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))

        # Write some state
        (state_dir / "dgx.json").write_text(json.dumps({"ok": True, "timestamp": "2026-06-09T12:00:00"}))
        (state_dir / "gateway.json").write_text(json.dumps({"ok": False, "timestamp": "2026-06-09T12:01:00"}))

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_status()

        output = f.getvalue()
        assert "Check Streams" in output or "dgx" in output.lower() or "gateway" in output.lower()


class TestCmdStatusLiveness:
    """cmd_status() gates RUNNING on the durable daemon_liveness() signal:
    pid present AND alive AND fresh heartbeat. It must NEVER report "RUNNING
    alive" when the pid is gone or the heartbeat is stale.

    Hermetic: we monkeypatch ``daemon_ops.daemon_liveness`` (the function
    cmd_status imports at call time) to return curated state dicts matching the
    real helper's keys, and pin PID_FILE/HEARTBEAT_FILE/STATE_DIR to tmp paths
    — no real daemon, no os.kill against a live ~/.hscc.
    """

    @staticmethod
    def _run(tmp_hfcc_dir, monkeypatch, liv):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(daemon_ops, "HEARTBEAT_FILE", str(tmp_hfcc_dir / "heartbeat"))
        monkeypatch.setattr(daemon_ops, "daemon_liveness", lambda: liv)

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_status()
        return f.getvalue()

    def _liv(self, state, pid=None, pid_file_present=True, alive=True,
             last_heartbeat=None, heartbeat_present=True, stale=False):
        return {
            "pid": pid,
            "pid_file_present": pid_file_present,
            "alive": alive,
            "last_heartbeat": last_heartbeat,
            "heartbeat_present": heartbeat_present,
            "stale": stale,
            "state": state,
        }

    def test_pid_alive_fresh_heartbeat_reports_running(self, tmp_hfcc_dir, monkeypatch):
        out = self._run(
            tmp_hfcc_dir, monkeypatch,
            self._liv("running-fresh", pid=4242, last_heartbeat="2026-09-25T15:00:00+00:00"),
        )
        assert "RUNNING" in out
        assert "alive (PID 4242)" in out
        # Regression: the healthy path still carries the exact "RUNNING alive" text.
        assert "RUNNING alive (PID 4242)" in out

    def test_pid_alive_stale_heartbeat_not_running(self, tmp_hfcc_dir, monkeypatch):
        out = self._run(
            tmp_hfcc_dir, monkeypatch,
            self._liv("running-stale-heartbeat", pid=4242, stale=True,
                      last_heartbeat="2026-09-25T01:00:00+00:00"),
        )
        assert "RUNNING alive" not in out
        assert "STOPPED" in out
        # The unexpected-exit note is surfaced on its own distinct line.
        assert "possible unexpected exit / stale heartbeat" in out
        # The last-heartbeat timestamp is present (line wraps, so assert the
        # unbroken timestamp token, not a phrase spanning a wrap boundary).
        assert "2026-09-25T01:00:00+00:00" in out
        assert "4242" in out

    def test_pid_present_process_gone_not_running(self, tmp_hfcc_dir, monkeypatch):
        out = self._run(
            tmp_hfcc_dir, monkeypatch,
            self._liv("pid-gone", pid=4242, alive=False, heartbeat_present=False),
        )
        assert "RUNNING alive" not in out
        assert "STOPPED" in out
        assert "stale PID file" in out
        assert "that process is not alive" in out

    def test_pid_file_missing_clean_stop(self, tmp_hfcc_dir, monkeypatch):
        out = self._run(
            tmp_hfcc_dir, monkeypatch,
            self._liv("pid-gone", pid=None, pid_file_present=False, alive=False,
                      heartbeat_present=False),
        )
        assert "RUNNING alive" not in out
        assert "STOPPED" in out
        # Clean stop: no stale/unexpected-exit note, just the plain status.
        assert "stale heartbeat" not in out
        assert "stale PID file" not in out

    def test_pid_alive_no_heartbeat_not_running(self, tmp_hfcc_dir, monkeypatch):
        # Pid alive but no durable heartbeat written yet — the pid file alone
        # cannot confirm the daemon, so status errs safe and reports NOT-running.
        out = self._run(
            tmp_hfcc_dir, monkeypatch,
            self._liv("running-no-heartbeat", pid=4242, heartbeat_present=False),
        )
        assert "RUNNING alive" not in out
        assert "STOPPED" in out
        assert "no durable heartbeat" in out


class TestCmdCheck:
    """cmd_check() runs a single check cycle."""

    def test_check_no_stream_runs_dgx(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import state as state_mod
        from hscc_daemon import daemon_ops
        from hscc_daemon import health
        from hscc_daemon import lifecycle
        from hscc_daemon import trigger

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))

        # Mock the check functions in their actual modules
        def mock_check():
            return True

        monkeypatch.setattr(health, "check_dgx", mock_check)
        monkeypatch.setattr(health, "check_gateway", mock_check)
        monkeypatch.setattr(health, "check_idle_monitor", mock_check)
        monkeypatch.setattr(health, "check_workers", mock_check)
        monkeypatch.setattr(lifecycle, "pipeline_watchdog", mock_check)
        monkeypatch.setattr(trigger, "trigger_engine", mock_check)

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_check()

        output = f.getvalue()
        assert "DGX" in output or "dgx" in output.lower() or "Result" in output


class TestCmdCheckDoesNotClobberDaemonState:
    """Regression: an ad-hoc `hscc check` must not overwrite the daemon's
    shared stream state, or a CLI-side failure fakes a fleet-wide FAIL.

    Bug (reproduced 2026-09-21): `hscc check` and the daemon both call the
    same check_* functions, which write ~/.hscc/state/<stream>.json. A CLI
    failure 74ms after the daemon's green result won the file, and `hscc
    status` then reported the fleet as FAIL while both TP pairs were up.
    Fix: the CLI prints only — it never writes the stream state. `hscc
    status` keeps meaning "what the daemon observes".
    """

    def test_cli_fail_does_not_flip_status(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        from hscc_daemon import state as state_mod
        from hscc_daemon import health

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))

        # Daemon's own (green) result is already on disk.
        daemon_entry = {"ok": True, "message": "daemon sees fleet healthy",
                        "timestamp": "2026-09-21T08:52:56"}
        (state_dir / "dgx.json").write_text(json.dumps(daemon_entry))

        # A real CLI-side LAN/TCC failure, indistinguishable from the daemon's
        # thread EXCEPT that it runs under cmd_check (persist_disabled). It
        # returns False and calls write_state with ok=False, as the real
        # check_dgx does on failure.
        def failing_check():
            state_mod.write_state("dgx", {"ok": False, "message": "No route to host"})
            return False

        monkeypatch.setattr(health, "check_dgx", failing_check)

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_check("dgx")
        output = f.getvalue()
        # The CLI still tells the operator its own result.
        assert "Result: FAIL" in output

        # But the daemon's on-disk state is untouched — still green.
        on_disk = json.loads((state_dir / "dgx.json").read_text())
        assert on_disk["ok"] is True

        # And `hscc status` still reports the daemon's observation (OK, not FAIL).
        f2 = io.StringIO()
        with redirect_stdout(f2):
            cli.cmd_status()
        status_out = f2.getvalue()
        assert "dgx" in status_out or "dgx" in status_out.lower()
        dgx_line = [l for l in status_out.splitlines() if l.strip().startswith("dgx")]
        assert dgx_line, "dgx stream should be listed"
        assert "OK" in dgx_line[0] and "FAIL" not in dgx_line[0]

    def test_status_still_shows_daemon_green_after_cli_check(self, tmp_hfcc_dir, monkeypatch):
        # Same guarantee expressed via read_state the way the daemon readers
        # consume it: state on disk is unchanged by a suppressed write.
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        # Simulate the full bug shape: daemon wrote ok=True, then the CLI tried
        # to write ok=False. With the fix the on-disk file must stay ok=True.
        (state_dir / "dgx.json").write_text(json.dumps({"ok": True}))
        with state_mod.persist_disabled():
            state_mod.write_state("dgx", {"ok": False})
            # Inside the block the read reflects the CLI's own run...
            inside = state_mod.read_state("dgx")
            assert inside is not None and inside["ok"] is False
        # ...but the file on disk is still the daemon's green result.
        after = state_mod.read_state("dgx")
        assert after is not None and after["ok"] is True
        assert json.loads((state_dir / "dgx.json").read_text())["ok"] is True

    def test_daemon_write_unaffected_outside_block(self, tmp_hfcc_dir, monkeypatch):
        # The suppression must not leak: writes outside a persist_disabled()
        # block (i.e. the daemon's normal path) still persist normally.
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        with state_mod.persist_disabled():
            state_mod.write_state("dgx", {"ok": False})
        assert state_mod.read_state("dgx") is None  # nothing written

        state_mod.write_state("dgx", {"ok": True})
        assert json.loads((state_dir / "dgx.json").read_text())["ok"] is True


class TestCmdTriggers:
    """cmd_triggers() shows trigger engine status."""

    def test_triggers_output(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_triggers()

        output = f.getvalue()
        assert "Trigger Engine" in output or "trigger" in output.lower()


class TestCmdNotify:
    """cmd_notify() sends a manual notification."""

    def test_notify_output(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import state as state_mod
        from hscc_daemon import desktop

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        notified = []
        def mock_notify(title, body, priority="normal"):
            notified.append({"title": title, "body": body})
            return True

        monkeypatch.setattr(desktop, "send_macos_notification", mock_notify)

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_notify("Hello World")

        output = f.getvalue()
        assert "notification" in output.lower() or "Sent" in output
        assert len(notified) == 1


class TestCmdLog:
    """cmd_log() shows daemon log output."""

    def test_log_empty(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_log()

        output = f.getvalue()
        assert "No daemon log" in output or "No log" in output or "No" in output

    def test_log_with_entries(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
        (tmp_hfcc_dir / "log").write_text("[2026-01-01] INFO test message\n")

        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_log()

        output = f.getvalue()
        assert "test message" in output


class TestCmdStartDaemon:
    """cmd_start_daemon() runs the daemon loop (service-supervised mode)."""

    def test_starts_daemon_loop(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))

        loop_called = []
        def mock_loop():
            loop_called.append(True)

        monkeypatch.setattr(daemon_ops, "run_daemon_loop", mock_loop)
        monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
        monkeypatch.setattr(daemon_ops, "save_pid", lambda: None)
        monkeypatch.setattr(daemon_ops, "ensure_state_dir", lambda: None)

        cli.cmd_start_daemon()
        assert len(loop_called) == 1


class TestEventDrivenCommands:
    """Event-driven commands are placeholders."""

    def test_ed_status(self):
        from hscc_daemon import cli
        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_ed_status()
        output = f.getvalue()
        assert "Event-driven" in output or "event" in output.lower()

    def test_ed_install(self):
        from hscc_daemon import cli
        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_ed_install()
        output = f.getvalue()
        assert "Event-driven" in output or "event" in output.lower()

    def test_ed_uninstall(self):
        from hscc_daemon import cli
        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_ed_uninstall()
        output = f.getvalue()
        assert "Event-driven" in output or "event" in output.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
