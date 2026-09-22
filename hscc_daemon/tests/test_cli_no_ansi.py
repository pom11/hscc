"""No-ANSI regression: converted Rich commands must degrade to plain text.

MANDATORY regression for the hscc-cli rich card (t_48b71e12): every command
converted to render through ``hscc_daemon.cli_theme`` must emit NO ANSI escape
codes when stdout is NOT a tty (piped / captured). Rich's Console handles this
automatically via colour-system detection — these tests pin it per command so
a regression (e.g. a stray ``print``/forced colour) is caught in CI.

Every command below is run with stdout redirected to an ``io.StringIO`` (a
non-tty file, exactly like piping ``hscc <cmd> | ...``) and asserts the output
contains no ``\\x1b[`` escape sequence.

Excluded by design (documented on the card):
  - ``cmd_watch`` — delegates to ``daemon_ops.stream_watcher`` whose live-loop
    output is NOT restyled (daemon streaming decision) and which never returns
    (infinite loop) so it cannot be captured on a piped stdout.
  - ``cmd_start_daemon`` — supervised entry point; its only output is the
    daemon's own ``log()`` background lines, not command output (not restyled).
  - ``cmd_ed_*`` — inert placeholders, not restyled.
"""

import io
import json
import signal
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from hscc_daemon import cli
from hscc_daemon import daemon_ops
from hscc_daemon import state as state_mod
from hscc_daemon import install as install_mod


def _no_ansi(fn):
    """Run ``fn()`` with stdout captured to a non-tty StringIO and assert the
    captured output contains no ANSI escape sequence."""
    f = io.StringIO()
    with redirect_stdout(f):
        fn()
    out = f.getvalue()
    assert "\x1b[" not in out, f"ANSI escape found in piped output: {out!r}"
    return out


def _patch_state_dir(monkeypatch, tmp_path):
    """Patch the state + pid/log paths onto a tmp dir and return the state dir."""
    state_dir = tmp_path / "hscc" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_path / "pid"))
    monkeypatch.setattr(daemon_ops, "STATE_DIR", str(state_dir))
    return state_dir


class TestNoAnsiStatus:
    def test_status_stopped(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        _no_ansi(cli.cmd_status)

    def test_status_with_streams(self, monkeypatch, tmp_path):
        state_dir = _patch_state_dir(monkeypatch, tmp_path)
        (state_dir / "dgx.json").write_text(json.dumps(
            {"ok": True, "timestamp": "2026-09-21T08:52:56"}))
        (state_dir / "gateway.json").write_text(json.dumps(
            {"ok": False, "timestamp": "2026-09-21T08:53:00"}))
        _no_ansi(cli.cmd_status)


class TestNoAnsiCheck:
    def test_check_dgx(self, monkeypatch, tmp_path):
        state_dir = _patch_state_dir(monkeypatch, tmp_path)
        from hscc_daemon import health
        monkeypatch.setattr(health, "check_dgx", lambda: True)
        _no_ansi(lambda: cli.cmd_check("dgx"))

    def test_check_all(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        from hscc_daemon import health, lifecycle, trigger
        ok = lambda: True
        monkeypatch.setattr(health, "check_dgx", ok)
        monkeypatch.setattr(health, "check_gateway", ok)
        monkeypatch.setattr(health, "check_local", ok)
        monkeypatch.setattr(health, "check_heartbeat", ok)
        monkeypatch.setattr(health, "check_nas", ok)
        monkeypatch.setattr(health, "check_idle_monitor", ok)
        monkeypatch.setattr(health, "check_workers", ok)
        monkeypatch.setattr(health, "check_engine_wedge", ok)
        monkeypatch.setattr(lifecycle, "pipeline_watchdog", ok)
        monkeypatch.setattr(trigger, "trigger_engine", ok)
        _no_ansi(lambda: cli.cmd_check("all"))


class TestNoAnsiTriggers:
    def test_triggers_empty(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "TRIGGERS_FILE",
                            str(tmp_path / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE",
                            str(tmp_path / "cooldowns.json"))
        _no_ansi(cli.cmd_triggers)

    def test_triggers_with_rules(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "TRIGGERS_FILE",
                            str(tmp_path / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE",
                            str(tmp_path / "cooldowns.json"))
        Path(tmp_path / "triggers.json").write_text(json.dumps({"rules": [
            {"id": "auto_heal", "enabled": True, "cooldown_seconds": 300},
            {"id": "quota_warn", "enabled": False, "cooldown_seconds": 60},
        ]}))
        _no_ansi(cli.cmd_triggers)


class TestNoAnsiNotify:
    def test_notify(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        from hscc_daemon import desktop
        monkeypatch.setattr(desktop, "send_macos_notification",
                            lambda t, b, priority="normal": True)
        _no_ansi(lambda: cli.cmd_notify("hello world"))


class TestNoAnsiLog:
    def test_log_empty(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        _no_ansi(cli.cmd_log)

    def test_log_with_entries(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_ops, "LOG_FILE",
                            str(tmp_path / "daemon.log"))
        Path(tmp_path / "daemon.log").write_text(
            "[2026-01-01] INFO test message\n[2026-01-01] WARN another\n")
        _no_ansi(cli.cmd_log)


class TestNoAnsiStart:
    def test_start(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        # Avoid real fork / daemon spawn: get_pid -> None so it proceeds to
        # fork; fork returns a parent-side pid so it prints "started" and
        # returns. save_pid is a no-op.
        monkeypatch.setattr(daemon_ops, "get_pid", lambda: None)
        monkeypatch.setattr(daemon_ops, "save_pid", lambda: None)
        monkeypatch.setattr(cli.os, "fork", lambda: 12345)
        monkeypatch.setattr(cli.os, "kill", lambda *a, **k: None)
        _no_ansi(cli.cmd_start)


class TestNoAnsiStop:
    def test_stop_not_running(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_ops, "get_pid", lambda: None)
        monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
        _no_ansi(cli.cmd_stop)

    def test_stop_running(self, monkeypatch, tmp_path):
        _patch_state_dir(monkeypatch, tmp_path)
        monkeypatch.setattr(daemon_ops, "get_pid", lambda: 4321)
        monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
        # Make the graceful-stop poll exit on the first probe: the ``kill(pid,
        # 0)`` liveness check raises OSError immediately -> "stopped".
        def fake_kill(p, sig):
            if sig == 0:
                raise OSError("no such process")
        monkeypatch.setattr(cli.os, "kill", fake_kill)
        _no_ansi(cli.cmd_stop)


class TestNoAnsiInstall:
    """install/uninstall/plist confirmations — disk + subprocess patched to tmp."""

    def _patch_install_paths(self, monkeypatch, tmp_path):
        monkeypatch.setattr(install_mod, "PLIST_DIR", str(tmp_path / "plists"))
        monkeypatch.setattr(install_mod, "PLIST_FILE",
                            str(tmp_path / "plists" / "com.hermes.hscc_daemon.plist"))
        monkeypatch.setattr(install_mod, "SYSTEMD_USER_DIR",
                            tmp_path / "systemd")
        monkeypatch.setattr(install_mod, "SYSTEMD_UNIT_FILE",
                            tmp_path / "systemd" / "com.hermes.hscc_daemon.service")

    def test_install_launchd(self, monkeypatch, tmp_path, fake_subprocess):
        self._patch_install_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(install_mod, "_service_manager", lambda: "launchd")
        fake_subprocess.set_result(returncode=0)
        _no_ansi(install_mod.cmd_install)

    def test_uninstall_launchd(self, monkeypatch, tmp_path, fake_subprocess):
        self._patch_install_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(install_mod, "PLIST_FILE",
                            str(tmp_path / "plists" / "com.hermes.hscc_daemon.plist"))
        (tmp_path / "plists").mkdir(parents=True, exist_ok=True)
        Path(tmp_path / "plists" / "com.hermes.hscc_daemon.plist").write_text("x")
        monkeypatch.setattr(install_mod, "_service_manager", lambda: "launchd")
        fake_subprocess.set_result(returncode=0)
        _no_ansi(install_mod.cmd_uninstall)

    def test_plist_launchd(self, monkeypatch, tmp_path):
        self._patch_install_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(install_mod, "_service_manager", lambda: "launchd")
        _no_ansi(install_mod.cmd_plist)

    def test_plist_systemd(self, monkeypatch, tmp_path):
        self._patch_install_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(install_mod, "_service_manager", lambda: "systemd")
        _no_ansi(install_mod.cmd_plist)
