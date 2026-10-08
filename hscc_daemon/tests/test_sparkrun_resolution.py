"""Regression tests for t_b543e530 — sparkrun resolution must not depend on PATH.

Reproduces the service-supervised environment measured live on 2026-10-08:
a launchd-started daemon whose PATH (from the plist EnvironmentVariables) is

    <home>/.hermes/hermes-agent/venv/bin:/opt/homebrew/bin:<home>/bin:
    /usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin

— no ``~/.local/bin``, where the sparkrun CLI actually lives. Everything here
is hermetic: the CLI and its venv python are FAKE files in a per-test HOME,
``shutil.which``/``subprocess`` are monkeypatched, and nothing ever shells out
to sparkrun, the venv python, or the fleet. No assertion depends on live state.
"""
import json
import os
import sys
import subprocess

import pytest

# The PATH the live launchd job actually had (measured with `ps eww <daemon pid>`),
# expressed against the per-test fake HOME. Note what is NOT in it: ~/.local/bin.
SERVICE_PATH_FMT = (
    "{home}/.hermes/hermes-agent/venv/bin:/opt/homebrew/bin:{home}/bin:"
    "/usr/local/bin:/System/Cryptexes/App/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin")


def _service_env(tmp_path, monkeypatch, cli_shebang=None, make_cli=True):
    """Fake HOME with ~/.local/bin/sparkrun + the sanitized service PATH.

    Returns (home, cli_path, venv_py). ``cli_shebang`` lets a test exercise the
    direct (``#!/abs/python``) vs env (``#!/usr/bin/env python3``) form.
    """
    from hscc_daemon import sparkrun_bin

    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    # The daemon's state dir has to exist for the relaunch log open
    # (``~/.hscc/relaunch-*.log``) — the real host always has it.
    (home / ".hscc").mkdir(parents=True, exist_ok=True)
    venv_bin = home / "sparkrun" / ".venv-sparkrun-py313" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)

    # sparkrun's OWN venv python — a file that EXISTS (that is all the shebang
    # check needs; we never exec it).
    venv_py = venv_bin / "python3.13"
    venv_py.write_text("# fake sparkrun venv interpreter\n")

    cli = local_bin / "sparkrun"
    if make_cli:
        shebang = cli_shebang or f"#!{venv_py}"
        cli.write_text(f"{shebang}\nimport sys\nfrom sparkrun.cli import main\n")
        cli.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", SERVICE_PATH_FMT.format(home=home))
    # Drop the conftest's hermetic override so the candidate-list probe is what
    # is under test (PATH has no sparkrun by construction here).
    monkeypatch.delenv("HSCC_SPARKRUN_BIN", raising=False)
    sparkrun_bin.reset_cache()
    return home, cli, venv_py


class TestPathIndependentResolution:
    """sparkrun_bin resolves the CLI + interpreter with no PATH entry for it."""

    def test_which_would_fail_under_the_service_path(self, tmp_path, monkeypatch):
        """Negative control: the pre-fix mechanism really is broken here.

        If shutil.which ever FOUND sparkrun under this PATH the reproduction is
        invalid and every other test in this file would prove nothing.
        """
        import shutil as _shutil
        _service_env(tmp_path, monkeypatch)
        assert _shutil.which("sparkrun") is None

    def test_cli_resolves_from_candidate_list(self, tmp_path, monkeypatch):
        from hscc_daemon import sparkrun_bin
        home, cli, _venv = _service_env(tmp_path, monkeypatch)
        resolved = sparkrun_bin.sparkrun_cli()
        assert resolved is not None
        assert os.path.realpath(resolved) == os.path.realpath(cli)
        assert resolved == str(home / ".local" / "bin" / "sparkrun")

    def test_venv_python_resolves_under_sanitized_path(self, tmp_path, monkeypatch):
        """The card's core assertion: with PATH lacking ~/.local/bin, the
        structured-status interpreter still resolves — no venv_py=None."""
        from hscc_daemon import health, sparkrun_bin
        _home, _cli, venv_py = _service_env(tmp_path, monkeypatch)
        assert health._sparkrun_venv_python() == str(venv_py)
        assert sparkrun_bin.sparkrun_venv_python() == str(venv_py)

    def test_env_shebang_prefers_the_venv_sibling_over_path_python(
            self, tmp_path, monkeypatch):
        """``#!/usr/bin/env python3`` must resolve to the CLI's venv SIBLING.

        The service PATH's ``python3`` is the Hermes/host python, which cannot
        ``import sparkrun``; picking it would silently re-break the structured
        query with an ImportError every tick.
        """
        from hscc_daemon import sparkrun_bin
        home, _cli, venv_py = _service_env(
            tmp_path, monkeypatch, cli_shebang="#!/usr/bin/env python3")
        sibling = home / ".local" / "bin" / "python3"
        sibling.write_text("# fake venv python3\n")
        # PATH does contain /usr/bin and /opt/homebrew/bin; a PATH-first
        # implementation would answer with one of those instead.
        assert sparkrun_bin.sparkrun_venv_python() == str(sibling)

    def test_direct_shebang_with_missing_interpreter_is_not_used(
            self, tmp_path, monkeypatch):
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch, cli_shebang="#!/nope/gone/python")
        assert sparkrun_bin.sparkrun_venv_python() is None

    def test_absent_everywhere_returns_none(self, tmp_path, monkeypatch):
        """A genuinely sparkrun-less host: None, not a fabricated path."""
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch, make_cli=False)
        assert sparkrun_bin.sparkrun_cli() is None
        assert sparkrun_bin.sparkrun_venv_python() is None

    def test_path_entry_still_wins(self, tmp_path, monkeypatch):
        """An operator's deliberate PATH install is never overridden by the
        candidate list (PATH-first ordering)."""
        from hscc_daemon import sparkrun_bin
        home, _cli, _venv = _service_env(tmp_path, monkeypatch)
        path_cli = home / ".local" / "bin" / "sparkrun"   # not on PATH
        other = home / "opt" / "sparkrun"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("#!/bin/sh\nexit 0\n")
        other.chmod(0o755)
        monkeypatch.setenv("PATH", f"{other.parent}:{os.environ['PATH']}")
        sparkrun_bin.reset_cache()
        assert sparkrun_bin.sparkrun_cli() == os.path.realpath(str(other))
        assert path_cli.exists()  # (sanity: the candidate is present too)

    def test_override_env_wins(self, tmp_path, monkeypatch):
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch)
        custom = tmp_path / "custom-sparkrun"
        custom.write_text("#!/bin/sh\nexit 0\n")
        custom.chmod(0o755)
        monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(custom))
        sparkrun_bin.reset_cache()
        assert sparkrun_bin.sparkrun_cli() == str(custom)

    def test_override_pointing_at_nothing_means_absent(self, tmp_path, monkeypatch):
        """An override that names no usable file is 'not installed' — it must
        not silently fall through to a different sparkrun."""
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch)
        monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(tmp_path / "nope"))
        sparkrun_bin.reset_cache()
        assert sparkrun_bin.sparkrun_cli() is None


class TestExecArgvRewrite:
    """The single execution chokepoint that makes argv PATH-independent."""

    def test_bare_name_rewritten_only_when_path_cannot_exec_it(
            self, tmp_path, monkeypatch):
        from hscc_daemon import sparkrun_bin
        home, _cli, _venv = _service_env(tmp_path, monkeypatch)
        argv = sparkrun_bin.exec_argv(["sparkrun", "stop", "r.yaml", "--hosts", "10.0.0.2"])
        assert argv[0] == str(home / ".local" / "bin" / "sparkrun")
        assert argv[1:] == ["stop", "r.yaml", "--hosts", "10.0.0.2"]

    def test_healthy_path_resolves_to_the_same_file(self, tmp_path, monkeypatch):
        """When PATH CAN exec sparkrun, the rewrite is purely mechanical: the
        absolute path is the SAME executable PATH would have chosen (realpath),
        so a well-configured environment sees zero behavioural drift."""
        from hscc_daemon import sparkrun_bin
        home, _cli, _venv = _service_env(tmp_path, monkeypatch)
        monkeypatch.setenv("PATH", f"{home}/.local/bin:{os.environ['PATH']}")
        sparkrun_bin.reset_cache()
        out = sparkrun_bin.exec_argv(["sparkrun", "status"])
        assert out[0] == os.path.realpath(str(home / ".local" / "bin" / "sparkrun"))
        assert out[1:] == ["status"]

    def test_unresolvable_cli_passes_through_loud(self, tmp_path, monkeypatch):
        """No sparkrun anywhere → argv unchanged → subprocess raises [Errno 2].
        A silent no-op here would be the fail-open this card exists to kill."""
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch, make_cli=False)
        assert sparkrun_bin.exec_argv(["sparkrun", "status"]) == ["sparkrun", "status"]

    def test_other_programs_and_absolute_paths_untouched(self, tmp_path, monkeypatch):
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch)
        assert sparkrun_bin.exec_argv(["docker", "info"]) == ["docker", "info"]
        assert sparkrun_bin.exec_argv(["/opt/sparkrun", "x"]) == ["/opt/sparkrun", "x"]
        assert sparkrun_bin.exec_argv([]) == []


class TestUtilRunCmdRouting:
    """util.run_cmd (the daemon package's one subprocess funnel) does the swap."""

    def test_sparkrun_argv_executes_the_absolute_path(self, tmp_path, monkeypatch):
        from hscc_daemon import util, sparkrun_bin
        home, _cli, _venv = _service_env(tmp_path, monkeypatch)
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

        monkeypatch.setattr(util.subprocess, "run", fake_run)
        res = util.run_cmd(["sparkrun", "stop", "r.yaml", "--hosts", "10.0.0.2"])
        assert res["ok"] is True
        assert seen["args"][0] == str(home / ".local" / "bin" / "sparkrun")

    def test_non_sparkrun_argv_untouched(self, tmp_path, monkeypatch):
        from hscc_daemon import util
        _service_env(tmp_path, monkeypatch)
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(util.subprocess, "run", fake_run)
        util.run_cmd(["pgrep", "-f", "hscc_daemon"])
        assert seen["args"] == ["pgrep", "-f", "hscc_daemon"]

    def test_shell_strings_are_not_rewritten(self, tmp_path, monkeypatch):
        from hscc_daemon import util
        _service_env(tmp_path, monkeypatch)
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(util.subprocess, "run", fake_run)
        util.run_cmd("timeout 2 sparkrun cluster list --json", shell=True)
        assert seen["args"] == "timeout 2 sparkrun cluster list --json"


class TestAutoHealRelaunchUsesAbsolutePath:
    """End-to-end service-environment reproduction of the broken relaunch.

    The pre-fix failure the operator measured: ``failed to launch worker
    family-coding-…: [Errno 2] No such file or directory: 'sparkrun'``.
    """

    def test_relaunch_and_stop_argv_are_executable_without_path_entry(
            self, tmp_path, monkeypatch):
        from hscc_daemon import health, serving, sparkrun_bin

        home, _cli, _venv = _service_env(tmp_path, monkeypatch)
        # The structured path is deliberately NOT available here (the worst
        # case): only the CLI-based relaunch/stop paths are under test.
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)
        monkeypatch.setattr(health, "log", lambda *a, **k: None)
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": False})
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        from hscc_daemon import state as state_mod
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(
            serving, "load_serving",
            lambda path=None: {"version": 2, "units": [
                {"id": "w1", "role": "worker", "keepalive": True,
                 "nodes": ["10.0.0.2"], "port": 8000, "recipe": "/r/w.yaml",
                 "model": "W"}]})
        monkeypatch.setattr(health, "_tp_peer_nodes", lambda: set())
        # Fresh-daemon bookkeeping (same as test_health._setup): a relaunch
        # timestamp leaked by an earlier test in the suite would put this unit
        # inside the load-grace window and skip the gentle relaunch entirely.
        health._worker_relaunch_at.clear()
        health._worker_down_streak.clear()
        health._worker_last_autoheal.clear()
        # Keep the auto-heal/action path out of the way: we are testing the
        # gentle relaunch shell-outs, not the force-recreate action.
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 10_000)

        ran = []
        popened = []

        # Stub at the SUBPROCESS layer (health.subprocess IS util.subprocess —
        # one module object), so the real `util.run_cmd` executes and the
        # resolver's argv[0] rewrite is genuinely exercised, not stubbed away.
        def fake_subproc_run(args, **kwargs):
            ran.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(health.subprocess, "run", fake_subproc_run)
        monkeypatch.setattr(health.subprocess, "Popen",
                            lambda *a, **k: popened.append(a) or None)

        health.check_workers()

        stop_cmds = [a for a in ran if a[1:2] == ["stop"]]
        assert stop_cmds, "expected the pre-relaunch sparkrun stop"
        assert popened, "expected the detached sparkrun run"
        for argv in [stop_cmds[0]] + [a[0] for a in popened]:
            assert argv[0] == str(home / ".local" / "bin" / "sparkrun"), (
                f"argv[0] {argv[0]!r} is not the absolute sparkrun path — under "
                "a service PATH without ~/.local/bin this is [Errno 2]")
        sparkrun_bin.reset_cache()


class TestDegradedFleetVisibility:
    """The double-failure must surface, not read as a healthy empty fleet."""

    def _dgx_setup(self, tmp_path, monkeypatch):
        from hscc_daemon import health, serving, state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **k: None)
        monkeypatch.setattr(health, "ssh_cmd",
                            lambda *a, **k: {"ok": True, "output": "reachable"})
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": True, "status": 200})
        monkeypatch.setattr(serving, "PRIMARY_NODE", "10.0.0.2")
        monkeypatch.setattr(serving, "VLLM_HEALTH_URL", "http://10.0.0.2:8000/health")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        return health, state_dir

    def test_both_paths_dead_publishes_degraded_not_ok(self, tmp_path, monkeypatch):
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)
        # The legacy shell-out ALSO fails — the real [Errno 2] shape.
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {
            "ok": False,
            "output": "Command failed: [Errno 2] No such file or directory: 'sparkrun'"})

        ok = health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())

        assert entry["ok"] is False, (
            "an empty workload list we could NOT verify must not publish ok=True")
        assert entry["details"]["fleet_visibility"] == "degraded"
        assert entry["details"]["workload_count"] == 0
        assert "VISIBILITY" in entry["message"].upper()
        # Never excused as an intentional autodown: we do not know the fleet
        # state, so we must not claim it is down by design.
        assert entry.get("intentional") != "autodown"
        # The RETURN value stays the serving-layer verdict: a status-polling
        # outage must not make the watchdog restart a healthy vLLM.
        assert ok is True

    def test_double_failure_logs_warn(self, tmp_path, monkeypatch):
        health, _state_dir = self._dgx_setup(tmp_path, monkeypatch)
        logs = []
        monkeypatch.setattr(health, "log", lambda msg, level="INFO": logs.append((level, msg)))
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": False, "output": "boom"})

        workloads, degraded = health._sparkrun_workloads()
        assert workloads == [] and degraded is True
        assert any(lvl == "WARN" and "VISIBILITY" in msg.upper() for lvl, msg in logs)

    def test_textparse_working_is_not_degraded(self, tmp_path, monkeypatch):
        """A fleet that really IS empty (fallback answered 0 Jobs) is healthy —
        degraded must mean 'could not look', not 'nothing to see'."""
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": True, "output": ""})

        ok = health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert ok is True
        assert entry["ok"] is True
        assert entry["details"]["fleet_visibility"] == "ok"

    def test_structured_path_published_when_resolvable(self, tmp_path, monkeypatch):
        """Acceptance: workloads come from query_cluster_status().to_dict()."""
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python",
                            lambda: "/fake/sparkrun-venv/bin/python")
        payload = {"groups": {"cid_a": {"meta": {"recipe": "w"},
                                        "containers": [{"host": "10.0.0.2"}]}},
                   "solo_entries": []}
        calls = []

        def fake_run(args, **k):
            calls.append(args)
            return {"ok": True, "output": json.dumps(payload)}

        monkeypatch.setattr(health, "run_cmd", fake_run)
        assert health.check_dgx() is True
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert entry["ok"] is True
        assert entry["details"]["fleet_visibility"] == "ok"
        assert entry["details"]["workloads"] == [{"name": "w (cid_a)", "container": "cid_a"}]
        assert calls[0][0] == "/fake/sparkrun-venv/bin/python"   # structured path used
        assert health._SPARKRUN_STATUS_SCRIPT in calls[0]

    def test_structured_query_error_then_dead_cli_is_degraded(
            self, tmp_path, monkeypatch):
        """Structured path raises AND the fallback cannot exec → degraded."""
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python",
                            lambda: "/fake/sparkrun-venv/bin/python")

        def fake_run(args, **k):
            if args[0].endswith("/python"):
                raise RuntimeError("query exploded")
            return {"ok": False, "output": "[Errno 2] No such file or directory: 'sparkrun'"}

        monkeypatch.setattr(health, "run_cmd", fake_run)
        health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert entry["ok"] is False
        assert entry["details"]["fleet_visibility"] == "degraded"


class TestServiceEnvironmentDeclaration:
    """The service environment must also be able to SEE the CLI (belt to the
    PATH-independent resolution) — every generator/template in the repo."""

    def test_install_plist_path_includes_local_bin(self):
        from hscc_daemon import install
        path = install._daemon_path_env("/Users/example")
        assert "/Users/example/.local/bin" in path

    def test_install_rendered_plist_and_systemd_unit_include_local_bin(self):
        """Pin the RENDERED service definitions, not just the helper — the
        plist PATH string is a static template ({PATH_ENV} substitution), and
        a fresh install through install.py must never reproduce the broken
        environment (t_b543e530, operator requirement 2)."""
        from hscc_daemon import install
        home = os.path.expanduser("~")
        plist = install.generate_plist()
        assert f"{home}/.local/bin" in plist
        unit = install.generate_systemd_unit()
        assert f"Environment=PATH={home}/.local/bin" in unit or \
            f"{home}/.local/bin" in unit

    def test_flightdeck_daemon_install_path_env_includes_local_bin(self):
        """flightdeck's daemon_install is the other plist emitter — same belt.
        Resolved as a sibling of the hscc_daemon package dir, which holds in
        both the repo layout (<root>/hscc_daemon + <root>/hscc-project) and
        the deployed layout (~/.hermes/plugins/*)."""
        import hscc_daemon
        plugins_dir = os.path.dirname(os.path.dirname(
            os.path.abspath(hscc_daemon.__file__)))
        src = os.path.join(plugins_dir, "hscc-project", "flightdeck",
                           "commands", "daemon_install.py")
        text = open(src).read()
        assert 'os.path.join(home, ".local/bin")' in text, (
            "flightdeck daemon_install._path_env lost ~/.local/bin — a fresh "
            "flightdeck-installed daemon would reproduce t_b543e530")

    def test_daemon_launchd_template_includes_local_bin(self):
        """The template that generated the live plist — this was the actual
        regression: it omitted ~/.local/bin while install.py included it."""
        from hscc_daemon import sparkrun_bin  # noqa: F401  (import guard)
        import hscc_daemon
        tpl = os.path.join(os.path.dirname(os.path.abspath(hscc_daemon.__file__)),
                           "com.hermes.hscc_daemon.plist.template")
        text = open(tpl).read()
        path_line = [ln for ln in text.splitlines()
                     if "__HOME__/.hermes/hermes-agent/venv/bin" in ln]
        assert path_line, "template PATH line not found"
        assert "__HOME__/.local/bin" in path_line[0]

    def test_daemon_startup_puts_sparkrun_on_path(self, tmp_path, monkeypatch):
        """shell=True sparkrun call sites need the ENVIRONMENT fixed, not argv."""
        from hscc_daemon import sparkrun_bin
        home, _cli, _venv = _service_env(tmp_path, monkeypatch)
        assert "sparkrun" not in os.environ["PATH"]
        assert sparkrun_bin.ensure_on_path() == str(home / ".local" / "bin") \
            and os.environ["PATH"].startswith(str(home / ".local" / "bin"))
        # Idempotent + additive: a second call does not duplicate or reorder.
        again = os.environ["PATH"]
        sparkrun_bin.ensure_on_path()
        assert os.environ["PATH"] == again

    def test_ensure_on_path_is_a_noop_when_cli_missing(self, tmp_path, monkeypatch):
        from hscc_daemon import sparkrun_bin
        _service_env(tmp_path, monkeypatch, make_cli=False)
        before = os.environ["PATH"]
        assert sparkrun_bin.ensure_on_path() is None
        assert os.environ["PATH"] == before


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
