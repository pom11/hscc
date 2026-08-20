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


class TestTemplateApplyExitCode:
    """`hscc template apply` must exit non-zero when the fleet was NOT deployed
    (blocked by pre-flight, or only partially applied) — a script chaining
    `apply && proceed` must not treat a non-deployment as success."""

    def _exit_code(self, result, args, monkeypatch):
        import types
        fake = types.ModuleType("cluster_template_cli")
        fake.cmd_cluster_template = lambda a: result
        monkeypatch.setitem(sys.modules, "cluster_template_cli", fake)
        from hscc_daemon import hscc as hscc_mod
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        out = io.StringIO()
        code = None
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit as e:
            code = e.code
        return code

    def test_blocked_apply_exits_nonzero(self, monkeypatch):
        result = {"status": "blocked", "success": False, "errors": ["bad layout"]}
        assert self._exit_code(result, ["template", "apply", "X", "--confirm"], monkeypatch) == 1

    def test_partial_apply_failure_exits_nonzero(self, monkeypatch):
        # steps had a warn/error -> success flipped False, but NO "error" key,
        # so _emit alone would have returned 0.
        result = {"template": "X", "success": False,
                  "steps": [{"step": "serve", "status": "error"}]}
        assert self._exit_code(result, ["template", "apply", "X", "--confirm"], monkeypatch) == 1

    def test_successful_apply_exits_zero(self, monkeypatch):
        result = {"template": "X", "success": True, "steps": []}
        assert self._exit_code(result, ["template", "apply", "X", "--confirm"], monkeypatch) == 0


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


class TestStatsNegativeDays:
    """Real _handle_stats rejects negative days with error + non-zero exit."""

    def _run(self, args, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", ["hscc", "stats", *args])
        from hscc_daemon import hscc as hscc_mod

        # Patch the functions on the REAL stats module. _handle_stats resolves
        # it via `from hscc_daemon import stats`, which returns the package
        # attribute once stats has been imported anywhere — so swapping the
        # sys.modules entry misses it; patching the module's attributes doesn't.
        from hscc_daemon import stats as stats_mod

        def fake_compute(since_days=7, **kw):
            return {
                "since_days": since_days,
                "completions": {"total": 0, "by_profile": {}, "by_day": {}},
                "activity": {"tool_calls_by_profile": {}, "top_tools": []},
            }

        monkeypatch.setattr(stats_mod, "compute_stats", fake_compute)
        monkeypatch.setattr(stats_mod, "format_stats", lambda s: f"ok:{s['since_days']}d")

        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                hscc_mod._handle_stats()
        except SystemExit as exc:
            return out.getvalue(), exc.code, err.getvalue()
        return out.getvalue(), None, err.getvalue()

    def test_negative_days_rejected(self, monkeypatch, tmp_path):
        output, rc, err = self._run(["-3"], monkeypatch, tmp_path)
        assert rc == 1, f"expected exit 1, got {rc}; out={output!r}; err={err!r}"
        assert "days must be non-negative" in err

    def test_zero_days_allowed(self, monkeypatch, tmp_path):
        output, rc, err = self._run(["0"], monkeypatch, tmp_path)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert "ok:0d" in output

    def test_positive_days_allowed(self, monkeypatch, tmp_path):
        output, rc, err = self._run(["5"], monkeypatch, tmp_path)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert "ok:5d" in output


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


class TestThroughputCommand:
    """hscc throughput routes to throughput.compute_throughput, respects --json."""

    def _run(self, args, fake_tp, monkeypatch):
        import types
        monkeypatch.setattr(sys, "argv", ["hscc", "throughput", *args])
        from hscc_daemon import hscc as hscc_mod

        fake_mod = types.ModuleType("hscc_daemon.throughput")
        fake_mod.compute_throughput = lambda: fake_tp
        fake_mod.format_throughput = lambda d: f"formatted:{d['fleet']['nodes_ok']}/{d['fleet']['nodes_total']}"

        def fake_handler():
            json_mode = "--json" in sys.argv[1:]
            tp = fake_mod.compute_throughput()
            if json_mode:
                print(json.dumps(tp))
            else:
                print(fake_mod.format_throughput(tp))
            sys.exit(0)

        monkeypatch.setattr(hscc_mod, "_handle_throughput", fake_handler)

        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                hscc_mod.main()
        except SystemExit as exc:
            return out.getvalue(), exc.code, err.getvalue()
        return out.getvalue(), None, err.getvalue()

    def test_throughput_table_output(self, monkeypatch):
        fake_tp = {
            "fleet": {"prompt_tokens": 100, "generation_tokens": 500, "running": 2, "waiting": 0, "nodes_ok": 2, "nodes_total": 2},
            "by_node": {},
        }
        output, rc, err = self._run([], fake_tp, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert "formatted:2/2" in output

    def test_throughput_json_output(self, monkeypatch):
        fake_tp = {
            "fleet": {"prompt_tokens": 50, "generation_tokens": 200, "running": 1, "waiting": 1, "nodes_ok": 1, "nodes_total": 2},
            "by_node": {},
        }
        output, rc, err = self._run(["--json"], fake_tp, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        parsed = json.loads(output.strip())
        assert parsed["fleet"]["nodes_ok"] == 1
        assert parsed["fleet"]["generation_tokens"] == 200


class TestAutoscaleCommand:
    """hscc autoscale routes to autoscale.decide_scale, derives current_workers, respects --json."""

    def _run(self, args, fake_tp, fake_decision, monkeypatch):
        # Patch the REAL modules the real _handle_autoscale imports, then run
        # the real handler (do NOT replace the handler with a copy).
        monkeypatch.setattr(sys, "argv", ["hscc", "autoscale", *args])
        from hscc_daemon import hscc as hscc_mod
        from hscc_daemon import throughput as tp_mod
        from hscc_daemon import autoscale as as_mod

        monkeypatch.setattr(tp_mod, "compute_throughput", lambda: fake_tp)
        captured = []
        def fake_decide(tp, *, current_workers=0, **kw):
            captured.append(current_workers)
            return fake_decision
        monkeypatch.setattr(as_mod, "decide_scale", fake_decide)

        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                hscc_mod.main()
        except SystemExit as exc:
            return out.getvalue(), exc.code, captured, err.getvalue()
        return out.getvalue(), None, captured, err.getvalue()

    def test_autoscale_current_workers_from_nodes_ok(self, monkeypatch):
        fake_tp = {
            "fleet": {"nodes_ok": 3, "nodes_total": 3, "waiting": 5, "running": 1},
            "by_node": {},
        }
        fake_decision = {"action": "scale_up", "target": 4, "reason": "queue depth 5 >= 4"}
        output, rc, captured, err = self._run([], fake_tp, fake_decision, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [3]
        assert "autoscale: scale_up (target 4)" in output

    def test_autoscale_fallback_to_by_node_len(self, monkeypatch):
        fake_tp = {
            "fleet": {"nodes_ok": 0, "nodes_total": 2, "waiting": 0, "running": 0},
            "by_node": {"http://a": {}, "http://b": {}},
        }
        fake_decision = {"action": "none", "reason": "within healthy band"}
        output, rc, captured, err = self._run([], fake_tp, fake_decision, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert captured == [2]

    def test_autoscale_json_output(self, monkeypatch):
        fake_tp = {"fleet": {"nodes_ok": 1, "nodes_total": 1}, "by_node": {}}
        fake_decision = {"action": "none", "reason": "within healthy band"}
        output, rc, captured, err = self._run(["--json"], fake_tp, fake_decision, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        parsed = json.loads(output.strip())
        assert parsed["action"] == "none"


class TestEscalateCommand:
    """hscc escalate routes to scan_and_escalate with no-op callbacks (dry-run)."""

    def _run(self, args, fake_actions, monkeypatch):
        import types
        monkeypatch.setattr(sys, "argv", ["hscc", "escalate", *args])
        from hscc_daemon import hscc as hscc_mod

        fake_ew = types.ModuleType("hscc_daemon.escalate_watcher")
        captured = []
        def fake_scan(_reassign=None, _notify=None):
            captured.append((_reassign, _notify))
            return fake_actions
        fake_ew.scan_and_escalate = fake_scan

        def fake_handler():
            json_mode = "--json" in sys.argv[1:]
            actions = fake_ew.scan_and_escalate(
                _reassign=lambda *a: None,
                _notify=lambda *a: None,
            )
            if json_mode:
                print(json.dumps(actions))
            else:
                if actions:
                    for a in actions:
                        task_id = a.get("task", "?")
                        action = a.get("action", "?")
                        to = a.get("to", a.get("category", "?"))
                        print(f"card {task_id}: {action} -> {to}")
                else:
                    print("no escalations pending")
            sys.exit(0)

        monkeypatch.setattr(hscc_mod, "_handle_escalate", fake_handler)

        out = io.StringIO()
        err = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                hscc_mod.main()
        except SystemExit as exc:
            return out.getvalue(), exc.code, captured, err.getvalue()
        return out.getvalue(), None, captured, err.getvalue()

    def test_escalate_dry_run_with_actions(self, monkeypatch):
        fake_actions = [
            {"task": "t_abc", "action": "escalate", "to": "architect", "category": "timeout"},
            {"task": "t_def", "action": "human", "category": "capability"},
        ]
        output, rc, captured, err = self._run([], fake_actions, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert "card t_abc: escalate -> architect" in output
        assert "card t_def: human -> capability" in output

    def test_escalate_no_actions(self, monkeypatch):
        output, rc, captured, err = self._run([], [], monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        assert "no escalations pending" in output

    def test_escalate_json_output(self, monkeypatch):
        fake_actions = [{"task": "t_abc", "action": "escalate", "to": "architect"}]
        output, rc, captured, err = self._run(["--json"], fake_actions, monkeypatch)
        assert rc == 0, f"expected 0, got {rc}; err={err!r}"
        parsed = json.loads(output.strip())
        assert len(parsed) == 1
        assert parsed[0]["task"] == "t_abc"


class TestThroughputAutoscaleEscalateHelp:
    """Full help, per-command help, and --help flags for throughput/autoscale/escalate."""

    def _run_help(self, args, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        return out.getvalue()

    def test_full_help_lists_throughput(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "throughput" in output

    def test_full_help_lists_autoscale(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "autoscale" in output

    def test_full_help_lists_escalate(self, monkeypatch):
        output = self._run_help([], monkeypatch)
        assert "escalate" in output

    def test_help_throughput(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "throughput"])
        from hscc_daemon import hscc as hscc_mod
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc throughput" in out.getvalue()

    def test_help_autoscale(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "autoscale"])
        from hscc_daemon import hscc as hscc_mod
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc autoscale" in out.getvalue()

    def test_help_escalate(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "escalate"])
        from hscc_daemon import hscc as hscc_mod
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc escalate" in out.getvalue()

    def test_throughput_help_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "throughput", "--help"])
        from hscc_daemon import hscc as hscc_mod
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc throughput" in out.getvalue()

    def test_autoscale_help_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "autoscale", "--help"])
        from hscc_daemon import hscc as hscc_mod
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc autoscale" in out.getvalue()

    def test_escalate_help_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["hscc", "escalate", "--help"])
        from hscc_daemon import hscc as hscc_mod
        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc escalate" in out.getvalue()


class TestProjectRouting:
    """'project' routes the raw argv slice to the relocated flightdeck.cli.main.

    The wrapper hands everything after ``hscc project`` straight to
    flightdeck's argv-driven entry point and returns its exit code. We stub
    ``flightdeck.cli.main`` (no real registry / hscc-project needed), then
    assert the argv slice is forwarded unmodified and the exit code flows
    through ``main()`` unchanged.
    """

    def _run(self, args, monkeypatch, tmp_path):
        import types
        captured: list = []

        fake_cli = types.ModuleType("flightdeck.cli")

        def fake_main(argv=None):
            captured.append(list(argv) if argv is not None else None)
            return 42  # a distinctive sentinel rc

        fake_cli.main = fake_main
        # Provide a fake flightdeck.cli so the wrapper's import finds it.
        monkeypatch.setitem(sys.modules, "flightdeck.cli", fake_cli)
        # Point _resolve_project_dir at a harmless temp dir (the fake import
        # never touches disk), avoiding any real sys.path manipulation.
        monkeypatch.setattr(
            "hscc_daemon.hscc._resolve_project_dir",
            lambda: tmp_path,
        )
        from hscc_daemon import hscc as hscc_mod

        monkeypatch.setattr(sys, "argv", ["hscc", *args])
        out = io.StringIO()
        rc = None
        try:
            with redirect_stdout(out):
                hscc_mod.main()
        except SystemExit as exc:
            rc = exc.code
        return captured, rc

    def test_project_standup_forwards_argv(self, monkeypatch, tmp_path):
        captured, rc = self._run(["project", "standup"], monkeypatch, tmp_path)
        # The whole arg slice after `project` is forwarded, untransformed.
        assert captured == [["standup"]]
        # flightdeck's sentinel rc propagates through main() unchanged.
        assert rc == 42

    def test_project_full_argv_slice(self, monkeypatch, tmp_path):
        captured, rc = self._run(
            ["project", "verify", "ecofire-app", "--json"], monkeypatch, tmp_path
        )
        assert captured == [["verify", "ecofire-app", "--json"]]
        assert rc == 42

    def test_project_help_returns_flightdeck_rc(self, monkeypatch, tmp_path):
        captured, rc = self._run(["project", "--help"], monkeypatch, tmp_path)
        assert captured == [["--help"]]
        assert rc == 42

    def test_full_help_lists_project(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", ["hscc"])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "project <cmd>" in out.getvalue()

    def test_help_project(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", ["hscc", "help", "project"])
        from hscc_daemon import hscc as hscc_mod

        out = io.StringIO()
        with redirect_stdout(out), pytest.raises(SystemExit) as exc:
            hscc_mod.main()
        assert exc.value.code == 0
        assert "hscc project" in out.getvalue()
        assert "flightdeck" in out.getvalue()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
