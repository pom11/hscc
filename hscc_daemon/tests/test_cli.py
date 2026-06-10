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
