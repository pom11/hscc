"""No-ANSI regression for event_driven.py's human CLI commands.

The standalone event_driven CLI's human command views (install / uninstall /
status) were converted to render through ``hscc_daemon.cli_theme``. When
stdout is not a tty, Rich must emit NO ANSI escape codes; these tests pin
that per command. This module has no machine ``--json`` path (the events it
drives are internal kqueue/launchd), so every command here is a human view.

``run_tests()`` is the module's own self-test harness, not a themed command
result — its pass/fail output is intentionally left plain (documented on the
card), so it is not asserted here.
"""

import io
from contextlib import redirect_stdout

import pytest

from hscc_daemon import event_driven as ed


def _no_ansi(fn):
    """Run ``fn()`` with stdout captured to a non-tty StringIO and assert the
    captured output contains no ANSI escape sequence."""
    f = io.StringIO()
    with redirect_stdout(f):
        rc = fn()
    out = f.getvalue()
    assert "\x1b[" not in out, f"ANSI escape found in piped output: {out!r}"
    return out, rc


class TestNoAnsiEventDrivenCli:
    def _patch_paths(self, monkeypatch, tmp_path):
        # Redirect the real LaunchAgents + ~/.hscc paths to tmp so the CLI
        # commands can never touch the live host (launchd jobs stay uncreated).
        monkeypatch.setattr(ed, "PLIST_DIR", str(tmp_path / "LaunchAgents"))
        monkeypatch.setattr(ed, "HSCC_DIR", str(tmp_path / "hscc"))
        monkeypatch.setattr(ed, "STATE_DIR", str(tmp_path / "hscc" / "state"))

    def test_install(self, monkeypatch, tmp_path, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        self._patch_paths(monkeypatch, tmp_path)
        _no_ansi(ed.cmd_install_event_driven)

    def test_uninstall(self, monkeypatch, tmp_path, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        self._patch_paths(monkeypatch, tmp_path)
        _no_ansi(ed.cmd_uninstall_event_driven)

    def test_status(self, monkeypatch, tmp_path, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        self._patch_paths(monkeypatch, tmp_path)
        # No daemon.log in the redirected HSCC_DIR -> the watchers branch
        # prints "No daemon log found" (still no ANSI).
        _no_ansi(ed.cmd_event_status)

    def test_status_with_daemon_log(self, monkeypatch, tmp_path,
                                    fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        self._patch_paths(monkeypatch, tmp_path)
        (tmp_path / "hscc").mkdir(parents=True, exist_ok=True)
        (tmp_path / "hscc" / "daemon.log").write_text(
            "KqueueWatcher started\nKqueueWatcher on dgx\n")
        _no_ansi(ed.cmd_event_status)

    def test_help(self, monkeypatch, tmp_path, fake_subprocess):
        """main()'s help path (len(sys.argv) < 2) must not emit ANSI either.
        It calls sys.exit(0), so we capture SystemExit."""
        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(ed.sys, "argv", ["event_driven.py"])
        f = io.StringIO()
        with redirect_stdout(f):
            with pytest.raises(SystemExit):
                ed.main()
        out = f.getvalue()
        assert "\x1b[" not in out, f"ANSI escape found in help output: {out!r}"
