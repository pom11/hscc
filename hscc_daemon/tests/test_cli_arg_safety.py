"""F6 (HEAL_CASCADE 2026-10-14) — CLI argument safety.

Two defects, both of the "the CLI guessed where it should have refused" class:

  1. ``hscc cluster up --help`` STARTED THE FLEET. The sub-command parsers read
     only ``"--dry-run" in cluster_args`` and ignored every other token, so any
     typo, any unsupported flag, and even ``--help`` fell through to the
     destructive branch. A mistyped ``--hosts`` silently upped all four nodes.
  2. ``hscc start`` left the caller's stdin/stdout/stderr attached to the
     daemonised grandchild, so the child could write into the caller's
     terminal — and, worse, keep the caller's pipe open. A caller that blocks
     until its stdout closes (``hscc start | cat``, an editor shell-integration,
     a tool capturing output) hangs forever even though ``hscc start`` printed
     and returned. This also leaks the operator's terminal file descriptors into
     a long-lived daemon.

Both are pinned here: flags are parsed against an explicit allow-list and an
unknown one is an error, and the daemonised child must have redirected all
three standard streams before it runs any loop.
"""

import io
import os

import pytest

from hscc_daemon import hscc as unified


# ── 1. cluster up/down flag parsing ────────────────────────────────────────

class TestClusterFlagParsing:
    """``_parse_cluster_flags`` is the gate: it decides whether a destructive
    cluster command runs at all."""

    def test_dry_run_is_recognised(self):
        dry, err = unified._parse_cluster_flags(["up", "--dry-run"], "up")
        assert err is None
        assert dry is True

    def test_bare_command_is_not_dry_run(self):
        dry, err = unified._parse_cluster_flags(["up"], "up")
        assert err is None and dry is False

    def test_unknown_flag_is_an_error_not_a_silent_up(self):
        """The incident bug: --help (or any typo) was ignored and the fleet was
        started. An unrecognised flag must be REFUSED."""
        for bad in ("--help", "-h", "--hosts", "--dry", "--dryrun",
                    "--dry-run=true", "--json"):
            dry, err = unified._parse_cluster_flags(["up", bad], "up")
            assert err is not None, f"{bad} was accepted (dry_run={dry})"
            assert dry is False, f"{bad} still reached the destructive branch"
            assert "up" in err and bad in err

    def test_unknown_flag_error_names_the_offending_token_and_the_fix(self):
        _, err = unified._parse_cluster_flags(["up", "--host"], "up")
        assert "--host" in err
        # The operator must be told what IS supported, or they will guess again.
        assert "--dry-run" in err

    def test_unexpected_positional_is_refused(self):
        """`hscc cluster up somegarbage` used to up the fleet too."""
        dry, err = unified._parse_cluster_flags(["up", "3node-coding"], "up")
        assert err is not None and dry is False
        assert "3node-coding" in err

    def test_repeated_dry_run_is_fine(self):
        dry, err = unified._parse_cluster_flags(
            ["down", "--dry-run", "--dry-run"], "down")
        assert err is None and dry is True

    def test_theme_flag_is_still_tolerated(self):
        """--theme is stripped by the caller BEFORE this parser runs; passing
        the already-stripped list must not error."""
        dry, err = unified._parse_cluster_flags(["up", "--dry-run"], "up")
        assert err is None and dry is True


class TestClusterUpHelpDoesNotExecute:
    """End-to-end through main(): the actual incident repro."""

    def _run(self, monkeypatch, argv):
        import sys
        from contextlib import redirect_stdout
        calls = []
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster"] + argv)
        # A fake engine whose up/down RECORD that they were called. If the
        # parser lets an unknown flag through, this fires and the test fails.
        class FakeEng:
            def cmd_cluster_up(self, dry_run=False):
                calls.append(("up", dry_run))
                # Match render_up's real contract (cluster_render.render_up
                # reads success/units/issued, and returns 1 without `success`)
                # so rc here measures the PARSER, not the renderer.
                return {"success": True, "dry_run": dry_run, "units": 1,
                        "issued": [{"success": True}]}

            def cmd_cluster_down(self, dry_run=False):
                calls.append(("down", dry_run))
                return {"success": True, "dry_run": dry_run, "units": 1,
                        "stopped": [{"success": True}]}
        monkeypatch.setattr(unified, "_load_cluster_engine", lambda: FakeEng())
        out = io.StringIO()
        code = None
        try:
            with redirect_stdout(out):
                code = unified._handle_cluster()
        except SystemExit as e:
            code = e.code
        return out.getvalue(), code, calls

    def test_up_help_does_not_start_the_fleet(self, monkeypatch):
        text, code, calls = self._run(monkeypatch, ["up", "--help"])
        assert calls == [], f"cluster up --help EXECUTED: {calls}"
        assert code not in (None, 0), f"rc={code}: an ignored flag returned OK"
        assert "dry-run" in text.lower()

    def test_up_typo_flag_does_not_start_the_fleet(self, monkeypatch):
        text, code, calls = self._run(monkeypatch, ["up", "--host=10.0.0.2"])
        assert calls == [], f"cluster up --host=... EXECUTED: {calls}"
        assert code != 0

    def test_up_dry_run_still_works(self, monkeypatch):
        text, code, calls = self._run(monkeypatch, ["up", "--dry-run"])
        assert calls == [("up", True)]
        assert code == 0

    def test_bare_up_still_performs_the_up(self, monkeypatch):
        """Regression guard the other way: tightening the parser must not break
        the normal case, or the fix itself becomes an outage."""
        text, code, calls = self._run(monkeypatch, ["up"])
        assert calls == [("up", False)]
        assert code == 0

    def test_down_help_does_not_stop_the_fleet(self, monkeypatch):
        text, code, calls = self._run(monkeypatch, ["down", "--help"])
        assert calls == []
        assert code != 0

    def test_dangling_theme_does_not_start_the_fleet(self, monkeypatch):
        """Second instance of the same bug class, found while writing the fix.

        ``_strip_theme_arg`` silently DROPS a ``--theme`` with no value, so
        ``hscc cluster up --theme`` looked to the parser like a clean bare
        ``up`` — and started the fleet. A stripped flag must not be able to
        erase the evidence that the operator typed something wrong.
        """
        text, code, calls = self._run(monkeypatch, ["up", "--theme"])
        assert calls == [], f"dangling --theme EXECUTED cluster up: {calls}"
        assert code != 0
        assert "theme" in text.lower()

    def test_theme_with_a_value_still_works(self, monkeypatch):
        """The fix must not break the supported form."""
        text, code, calls = self._run(monkeypatch,
                                      ["up", "--theme", "dark"])
        assert calls == [("up", False)], calls
        assert code == 0

    def test_theme_equals_value_still_works(self, monkeypatch):
        text, code, calls = self._run(monkeypatch, ["up", "--theme=dark"])
        assert calls == [("up", False)]
        assert code == 0

    def test_theme_followed_by_dry_run_still_works(self, monkeypatch):
        text, code, calls = self._run(
            monkeypatch, ["up", "--theme", "dark", "--dry-run"])
        assert calls == [("up", True)], calls
        assert code == 0


# ── 2. `hscc start` must not keep the caller's stdio ───────────────────────

class TestStartDetachesStdio:
    """``_detach_stdio`` is what stands between a daemon and the operator's
    terminal. Redirecting to DEVNULL is the whole point of daemonising."""

    def test_detach_helper_redirects_all_three_streams(self, monkeypatch):
        from hscc_daemon import cli
        seen = {}

        def fake_dup2(fd, target, *a, **k):
            seen[target] = fd
            return target

        monkeypatch.setattr(os, "dup2", fake_dup2)
        opened = []
        real_open = open

        class _Devnull:
            fileno_val = 77

            def fileno(self):
                return self.fileno_val

        def fake_open(path, mode="r", *a, **k):
            opened.append((path, mode))
            return _Devnull()

        monkeypatch.setattr("builtins.open", fake_open)
        cli._detach_stdio()
        # stdin, stdout, stderr all replaced by a device.
        assert set(seen) >= {0, 1, 2}, f"streams left attached: {seen}"
        assert all("/dev/null" in p for p, _ in opened), opened

    def test_detach_never_raises_if_open_fails(self, monkeypatch):
        """The child runs after fork, before the loop. Raising here would kill
        the daemon at startup; a leaked fd is the lesser harm and must be
        logged, not fatal."""
        from hscc_daemon import cli
        monkeypatch.setattr(os, "dup2", lambda *a, **k: 0)

        def boom(*a, **k):
            raise OSError("no /dev/null here")

        monkeypatch.setattr("builtins.open", boom)
        cli._detach_stdio()          # must not raise

    def test_daemon_child_calls_detach_before_running_the_loop(self, monkeypatch,
                                                               tmp_hfcc_dir):
        """Placement matters: redirect AFTER the second fork (so the parent has
        already printed) and BEFORE run_daemon_loop (so the loop can never
        write to the caller's terminal)."""
        from hscc_daemon import cli, daemon_ops
        from hscc_daemon import state as state_mod

        order = []
        # cmd_start does `from .daemon_ops import run_daemon_loop` INSIDE the
        # function, so patch the SOURCE MODULE (the local name is re-resolved
        # from the module on each call); patching cli.run_daemon_loop would
        # silently do nothing. Same pattern as test_cli.py's fork emulation.
        monkeypatch.setattr(state_mod, "STATE_DIR", str(tmp_hfcc_dir / "state"))
        monkeypatch.setattr(state_mod, "ensure_state_dir", lambda: None)
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(daemon_ops, "get_pid", lambda: None)
        monkeypatch.setattr(daemon_ops, "write_stopped",
                            lambda: order.append("stopped"))
        monkeypatch.setattr(daemon_ops, "save_pid",
                            lambda: order.append("save_pid"))
        monkeypatch.setattr(daemon_ops, "run_daemon_loop",
                            lambda: order.append("loop"))
        monkeypatch.setattr(cli, "_detach_stdio",
                            lambda: order.append("detach"))

        # fork -> child (0), second fork -> grandchild (0): the parent branch is
        # skipped and the grandchild (the daemonising one) is exercised.
        forks = iter([0, 0])
        monkeypatch.setattr(cli.os, "fork", lambda: next(forks))
        monkeypatch.setattr(cli.os, "setsid", lambda: None)
        monkeypatch.setattr(cli.os, "chdir", lambda p: None)
        # The first child's os._exit must be intercepted or this test dies.
        monkeypatch.setattr(cli.os, "_exit", lambda code=0: order.append("exit"))
        import signal as _sig
        monkeypatch.setattr(_sig, "signal", lambda *a, **k: None)
        cli.cmd_start()
        assert "detach" in order, order
        assert order.index("detach") < order.index("loop"), order


class TestStartDoesNotHoldTheCallersPipe:
    """The observable symptom: a caller that waits for stdout to close hangs.

    We cannot exercise a real double-fork here, so the property is pinned at
    the level that matters — the child branch must not retain fd 1, and the
    parent branch must exit having closed nothing of its own that the child
    still needs.
    """

    def test_streams_point_at_a_device_and_only_the_source_fd_is_closed(
            self, monkeypatch):
        """The DEVNULL-vs-close(1) rationale, pinned.

        Redirecting to a CLOSED fd (0-close) is a classic daemon bug, verified
        empirically while writing this: after ``os.close(1)`` the next
        ``open()`` in the process silently gets fd 1 and its file content is
        then written into the caller's pipe — strictly worse than leaving it
        attached. So every stream must be dup2'd from a REAL open descriptor,
        and the only descriptor the helper may close is its own source.
        """
        from hscc_daemon import cli
        dupes, closed = {}, []
        DEVNULL_FD = 42

        monkeypatch.setattr(os, "open", lambda *a, **k: DEVNULL_FD)
        monkeypatch.setattr(os, "dup2",
                            lambda fd, target, *a, **k: dupes.setdefault(
                                target, fd) or target)
        monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))

        cli._detach_stdio()

        assert set(dupes) == {0, 1, 2}, dupes
        assert all(fd == DEVNULL_FD for fd in dupes.values()), dupes
        # Only the helper's own source fd is closed — never stdin/stdout/stderr.
        assert closed == [DEVNULL_FD], closed
