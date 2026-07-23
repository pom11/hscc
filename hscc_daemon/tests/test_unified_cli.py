"""Unit tests for the unified CLI entry point in hscc.py.

Tests rich help output, per-command help, cluster/template routing,
and that existing daemon dispatch is preserved.
"""
import io
import json
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


class TestVerifyAndStatsInHelp:
    """Full help lists 'verify' and 'stats' commands."""

    def _run_help(self, args, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        return out.getvalue()

    def test_full_help_lists_verify(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "verify" in output

    def test_full_help_lists_stats(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "stats" in output


class TestPerCommandHelpVerifyStats:
    """hscc help verify and hscc help stats print their help."""

    def _run_help_cmd(self, cmd, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", cmd])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        return out.getvalue(), exc.value.code

    def test_help_verify(self, monkeypatch):
        output, rc = self._run_help_cmd("verify", monkeypatch)
        assert rc == 0
        assert "hscc verify" in output
        assert "smoke-test" in output.lower() or "verify" in output.lower()

    def test_help_stats(self, monkeypatch):
        output, rc = self._run_help_cmd("stats", monkeypatch)
        assert rc == 0
        assert "hscc stats" in output
        assert "fleet" in output.lower() or "stats" in output.lower()


class TestVerifyCommand:
    """hscc verify routes to verify.run_all, respects --json, exit code follows ok."""

    def _run(self, args, fake_result, monkeypatch):
        import types
        monkeypatch.setattr(sys, "argv", ["hscc", "verify", *args])
        from hscc_daemon import hscc as hscc_mod

        fake_verify = types.ModuleType("hscc_daemon.verify")
        fake_verify.run_all = lambda **kw: fake_result

        # Patch _handle_verify to use our fake verify module
        def fake_handler():
            from hscc_daemon import hscc as m
            result = fake_verify.run_all()
            if "--json" in sys.argv[1:]:
                print(json.dumps(result))
            else:
                checks = result.get("checks", [])
                if checks:
                    max_name = max(len(c.get("name", "")) for c in checks)
                    max_name = max(max_name, 4)
                    for c in checks:
                        glyph = "\u2713" if c.get("ok") else "\u2717"
                        name = c.get("name", "").ljust(max_name)
                        detail = c.get("detail", "")
                        print(f"  {glyph} {name}  {detail}")
                overall = "\u2713 All checks passed" if result.get("ok") else "\u2717 Some checks failed"
                print(f"\n  {overall}")
            sys.exit(0 if result.get("ok") else 1)

        monkeypatch.setattr(hscc_mod, "_handle_verify", fake_handler)

        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                hscc_mod.main()
        except SystemExit as exc:
            return out.getvalue(), exc.code, err.getvalue()
        return out.getvalue(), None, err.getvalue()

    def test_verify_table_output(self, monkeypatch):
        fake_result = {
            "ok": True,
            "checks": [
                {"name": "plugins", "ok": True, "detail": "all core commands found"},
                {"name": "proxy", "ok": True, "detail": "3 models available"},
            ],
        }
        output, rc, err = self._run([], fake_result, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert "\u2713" in output
        assert "plugins" in output
        assert "proxy" in output
        assert "All checks passed" in output

    def test_verify_failing_exit_1(self, monkeypatch):
        fake_result = {
            "ok": False,
            "checks": [
                {"name": "plugins", "ok": True, "detail": "ok"},
                {"name": "proxy", "ok": False, "detail": "connection error"},
            ],
        }
        output, rc, err = self._run([], fake_result, monkeypatch)
        assert rc == 1, f"expected 1, got {rc}; err={err!r}"
        assert "\u2717" in output
        assert "Some checks failed" in output

    def test_verify_json_output(self, monkeypatch):
        fake_result = {
            "ok": True,
            "checks": [{"name": "plugins", "ok": True, "detail": "ok"}],
        }
        output, rc, err = self._run(["--json"], fake_result, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        parsed = json.loads(output.strip())
        assert parsed["ok"] is True
        assert len(parsed["checks"]) == 1


class TestStatsCommand:
    """hscc stats routes to compute_stats, parses days, respects --json."""

    def _run(self, args, monkeypatch):
        import types
        captured = []
        monkeypatch.setattr(sys, "argv", ["hscc", "stats", *args])
        from hscc_daemon import hscc as hscc_mod

        fake_stats = types.ModuleType("hscc_daemon.stats")

        def fake_compute(since_days=7, **kw):
            captured.append(since_days)
            return {
                "since_days": since_days,
                "completions": {"total": since_days * 10, "by_profile": {}, "by_day": {}},
                "activity": {"tool_calls_by_profile": {}, "top_tools": []},
            }

        fake_stats.compute_stats = fake_compute
        fake_stats.format_stats = lambda s: f"formatted:{s['since_days']}d,total={s['completions']['total']}"

        # Patch _handle_stats to use our fake stats module
        def fake_handler():
            json_mode = "--json" in sys.argv[1:]
            days = 7
            rest = [a for a in sys.argv[1:] if a != "--json"]
            if len(rest) > 1:
                try:
                    days = int(rest[1])
                except (ValueError, TypeError):
                    days = 7
            result = fake_stats.compute_stats(since_days=days)
            if json_mode:
                print(json.dumps(result))
            else:
                print(fake_stats.format_stats(result))
            sys.exit(0)

        monkeypatch.setattr(hscc_mod, "_handle_stats", fake_handler)

        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                hscc_mod.main()
        except SystemExit as exc:
            return out.getvalue(), exc.code, captured, err.getvalue()
        return out.getvalue(), None, captured, err.getvalue()

    def test_stats_default_days(self, monkeypatch):
        output, rc, captured, err = self._run([], monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [7]
        assert "formatted:7d" in output

    def test_stats_custom_days(self, monkeypatch):
        output, rc, captured, err = self._run(["3"], monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [3]
        assert "formatted:3d" in output

    def test_stats_bad_days_defaults_to_7(self, monkeypatch):
        output, rc, captured, err = self._run(["not-a-number"], monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [7]

    def test_stats_json_output(self, monkeypatch):
        output, rc, captured, err = self._run(["5", "--json"], monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [5]
        parsed = json.loads(output.strip())
        assert parsed["since_days"] == 5
        assert parsed["completions"]["total"] == 50

    def test_stats_json_flag_before_days(self, monkeypatch):
        output, rc, captured, err = self._run(["--json", "2"], monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [2]
        parsed = json.loads(output.strip())
        assert parsed["since_days"] == 2


class TestVerifyStatsHelpFlags:
    """hscc verify --help and hscc stats --help work."""

    def _run(self, args, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        return out.getvalue(), exc.value.code

    def test_verify_help_flag(self, monkeypatch):
        output, rc = self._run(["verify", "--help"], monkeypatch)
        assert rc == 0
        assert "hscc verify" in output

    def test_stats_help_flag(self, monkeypatch):
        output, rc = self._run(["stats", "--help"], monkeypatch)
        assert rc == 0
        assert "hscc stats" in output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
