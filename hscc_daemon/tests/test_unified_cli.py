"""Unit tests for the unified CLI entry point in hscc.py.

Tests rich help output, per-command help, cluster/template routing,
and that existing daemon dispatch is preserved.
"""
import io
import sys
import pytest
from contextlib import redirect_stdout, redirect_stderr


class TestFullHelp:
    """Full help is printed for no args, -h, --help, help."""

    def _run_help(self, args, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        return out.getvalue()

    def test_no_args_prints_help(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "Daemon control" in output
        assert "Cluster & templates" in output
        assert "template apply" in output
        assert "profiles" in output

    def test_h_flag_prints_help(self, monkeypatch):
        output = self._run_help(["-h"], monkeypatch)
        assert "HSCC" in output

    def test_help_flag_prints_help(self, monkeypatch):
        output = self._run_help(["--help"], monkeypatch)
        assert "HSCC" in output

    def test_help_subcommand_prints_help(self, monkeypatch):
        output = self._run_help(["help"], monkeypatch)
        assert "Daemon control" in output

    def test_help_contains_section_headers(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "Daemon control" in output
        assert "Health & monitoring" in output
        assert "Cluster & templates" in output
        assert "Utility" in output
        assert "Examples" in output

    def test_help_contains_version(self, monkeypatch):
        import re
        output = self._run_help([], monkeypatch)
        # A version token, not a pinned literal — survives release bumps.
        assert re.search(r"\bv\d+\.\d+\.\d+\b", output)

    def test_help_contains_advanced_footer(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "hscc help advanced" in output


class TestPerCommandHelp:
    """hscc help <command> prints short usage."""

    def _run_help_cmd(self, cmd, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", cmd])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        return out.getvalue(), exc.value.code

    def test_help_start(self, monkeypatch):
        output, rc = self._run_help_cmd("start", monkeypatch)
        assert rc == 0
        assert "hscc start" in output

    def test_help_cluster(self, monkeypatch):
        output, rc = self._run_help_cmd("cluster", monkeypatch)
        assert rc == 0
        assert "hscc cluster" in output

    def test_help_template(self, monkeypatch):
        output, rc = self._run_help_cmd("template", monkeypatch)
        assert rc == 0
        assert "hscc template" in output

    def test_help_unknown_command(self, monkeypatch):
        output, rc = self._run_help_cmd("nonexistent", monkeypatch)
        assert rc == 1
        assert "no help for" in output

    def test_help_advanced_lists_start_daemon(self, monkeypatch):
        output, rc = self._run_help_cmd("advanced", monkeypatch)
        assert rc == 0
        assert "start-daemon" in output
        assert "ed-status" in output

    def test_help_advanced_lists_ed_commands(self, monkeypatch):
        output, rc = self._run_help_cmd("advanced", monkeypatch)
        assert rc == 0
        assert "ed-install" in output
        assert "ed-uninstall" in output


class TestClusterSubcommand:
    """hscc cluster with no sub prints list; unknown sub exits 1."""

    def test_cluster_no_sub_exits_0(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster"])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        output = out.getvalue()
        assert "cluster status" in output or "status" in output

    def test_cluster_unknown_sub_exits_1(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "cluster", "bogus"])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 1


class TestTemplateSubcommand:
    """hscc template with no sub prints list; unknown sub exits 1."""

    def test_template_no_sub_exits_0(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "template"])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0

    def test_template_unknown_sub_exits_1(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "template", "bogus"])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 1


class _FakeEngine:
    """Stand-in for the hscc-cluster engine module; records which cmd_* ran."""

    def __init__(self):
        self.calls = []

    def _rec(self, name):
        def fn(*args):
            self.calls.append((name, list(args)))
            return {"ok": True, "cmd": name, "args": list(args)}
        return fn

    def __getattr__(self, name):
        # Any cmd_* attribute becomes a recording callable.
        if name.startswith("cmd_"):
            return self._rec(name)
        raise AttributeError(name)


class TestClusterRouting:
    """cluster/profiles route to the engine's cmd_* functions directly."""

    def _run(self, args, monkeypatch):
        eng = _FakeEngine()
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(hscc_mod, "_load_cluster_engine", lambda: eng)
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit:
            pass
        return eng.calls

    def test_cluster_status_routing(self, monkeypatch):
        assert self._run(["cluster", "status"], monkeypatch) == [("cmd_cluster_status", [])]

    def test_cluster_hosts_routing(self, monkeypatch):
        assert self._run(["cluster", "hosts"], monkeypatch) == [("cmd_hosts", [])]

    def test_cluster_monitor_routing(self, monkeypatch):
        assert self._run(["cluster", "monitor"], monkeypatch) == [("cmd_monitor", [])]

    def test_cluster_jobs_routing(self, monkeypatch):
        assert self._run(["cluster", "jobs"], monkeypatch) == [("cmd_jobs", [])]

    def test_cluster_info_routing(self, monkeypatch):
        assert self._run(["cluster", "info"], monkeypatch) == [("cmd_info", [])]

    def test_cluster_stop_routing(self, monkeypatch):
        assert self._run(["cluster", "stop", "abc"], monkeypatch) == [("cmd_stop", ["abc"])]

    def test_profiles_routing(self, monkeypatch):
        assert self._run(["profiles"], monkeypatch) == [("cmd_profile_status", [])]


class TestTemplateRouting:
    """template routes to cluster_template_cli.cmd_cluster_template([...])."""

    def _run(self, args, monkeypatch):
        import types
        captured = []
        fake = types.ModuleType("cluster_template_cli")
        fake.cmd_cluster_template = lambda a: (captured.append(list(a)) or {"ok": True})
        monkeypatch.setitem(sys.modules, "cluster_template_cli", fake)
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit:
            pass
        return captured

    def test_template_list_routing(self, monkeypatch):
        assert self._run(["template", "list"], monkeypatch) == [["list"]]

    def test_template_status_routing(self, monkeypatch):
        assert self._run(["template", "status"], monkeypatch) == [["status"]]

    def test_template_preview_routing(self, monkeypatch):
        assert self._run(["template", "preview", "my-template"], monkeypatch) == [["preview", "my-template"]]

    def test_template_validate_routing(self, monkeypatch):
        assert self._run(["template", "validate", "my-template"], monkeypatch) == [["validate", "my-template"]]

    def test_template_apply_with_confirm(self, monkeypatch):
        assert self._run(["template", "apply", "X", "--confirm"], monkeypatch) == [["apply", "X", "--confirm"]]


class TestDaemonDispatchPreserved:
    """Existing daemon commands still route to cmd_* functions."""

    def test_status_routes_to_cmd_status(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import hscc as hscc_mod
        from hscc_daemon import cli
        from hscc_daemon import daemon_ops
        from hscc_daemon import state as state_mod

        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))

        called = []
        original = cli.cmd_status
        def fake_status():
            called.append(True)
        monkeypatch.setattr(cli, "cmd_status", fake_status)

        monkeypatch.setattr(sys, "argv", ["hscc", "status"])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit:
            pass
        assert len(called) == 1

    def test_start_routes_to_cmd_start(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import hscc as hscc_mod
        from hscc_daemon import cli

        called = []
        def fake_start():
            called.append(True)
        monkeypatch.setattr(cli, "cmd_start", fake_start)

        monkeypatch.setattr(sys, "argv", ["hscc", "start"])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit:
            pass
        assert len(called) == 1

    def test_check_routes_to_cmd_check(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import hscc as hscc_mod
        from hscc_daemon import cli

        called = []
        def fake_check(stream=None):
            called.append(stream)
        monkeypatch.setattr(cli, "cmd_check", fake_check)

        monkeypatch.setattr(sys, "argv", ["hscc", "check", "gateway"])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit:
            pass
        assert called == ["gateway"]

    def test_stop_routes_to_cmd_stop(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import hscc as hscc_mod
        from hscc_daemon import cli

        called = []
        def fake_stop():
            called.append(True)
        monkeypatch.setattr(cli, "cmd_stop", fake_stop)

        monkeypatch.setattr(sys, "argv", ["hscc", "stop"])
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit:
            pass
        assert len(called) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
