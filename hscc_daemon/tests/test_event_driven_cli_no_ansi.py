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
    # NO per-test path patching in this class, deliberately (t_35d9f6ea).
    # These commands reach ``os.makedirs(PLIST_DIR)`` plus a plist write
    # (cmd_install_event_driven) and an ``os.remove`` per stream
    # (cmd_uninstall_event_driven -> LaunchdJobGenerator.uninstall_job). This
    # file used to carry its own ``_patch_paths`` helper, and that is exactly
    # the shape that leaves the NEXT event-driven test file unprotected: the
    # suite-wide seal was never load-bearing here, so deleting it would have
    # broken nothing. Every path these commands touch is now redirected by the
    # autouse ``conftest._isolate_hscc`` seal, whose values are the same ones
    # that helper set (``<tmp>/Library/LaunchAgents``, ``<tmp>/hscc``,
    # ``<tmp>/hscc/state``), so these five commands run against the seal
    # itself and the seal is what keeps the operator's real LaunchAgents
    # intact. A test that patched its own path would still win (monkeypatch
    # LIFO teardown) — patching is unnecessary here, not forbidden.
    def test_install(self, monkeypatch, tmp_path, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        _no_ansi(ed.cmd_install_event_driven)

    def test_uninstall(self, monkeypatch, tmp_path, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        _no_ansi(ed.cmd_uninstall_event_driven)

    def test_status(self, monkeypatch, tmp_path, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        # No daemon.log in the redirected HSCC_DIR -> the watchers branch
        # prints "No daemon log found" (still no ANSI).
        _no_ansi(ed.cmd_event_status)

    def test_status_with_daemon_log(self, monkeypatch, tmp_path,
                                    fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        (tmp_path / "hscc").mkdir(parents=True, exist_ok=True)
        (tmp_path / "hscc" / "daemon.log").write_text(
            "KqueueWatcher started\nKqueueWatcher on dgx\n")
        _no_ansi(ed.cmd_event_status)

    def test_help(self, monkeypatch, tmp_path, fake_subprocess):
        """main()'s help path (len(sys.argv) < 2) must not emit ANSI either.
        It calls sys.exit(0), so we capture SystemExit."""
        monkeypatch.setattr(ed.sys, "argv", ["event_driven.py"])
        f = io.StringIO()
        with redirect_stdout(f):
            with pytest.raises(SystemExit):
                ed.main()
        out = f.getvalue()
        assert "\x1b[" not in out, f"ANSI escape found in help output: {out!r}"
