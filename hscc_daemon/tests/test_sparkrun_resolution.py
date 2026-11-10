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
import time

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
        argv = sparkrun_bin.exec_argv(["sparkrun", "stop", "r.yaml", "--hosts", "192.0.2.2"])
        assert argv[0] == str(home / ".local" / "bin" / "sparkrun")
        assert argv[1:] == ["stop", "r.yaml", "--hosts", "192.0.2.2"]

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
        res = util.run_cmd(["sparkrun", "stop", "r.yaml", "--hosts", "192.0.2.2"])
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
                 "nodes": ["192.0.2.2"], "port": 8000, "recipe": "/r/w.yaml",
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
        monkeypatch.setattr(serving, "PRIMARY_NODE", "192.0.2.2")
        monkeypatch.setattr(serving, "VLLM_HEALTH_URL", "http://192.0.2.2:8000/health")
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

        workloads, degraded, _visibility = health._sparkrun_workloads()
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
                                        "containers": [{"host": "192.0.2.2"}]}},
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


class TestSweepBudgetAlignment:
    """t_614f4cac — the daemon's caps must sit ABOVE the sweep's own budget.

    Audit §c3/§c8-1: `query_status_for_cluster` runs TWO sequential host-scope
    rounds, each capped at `ssh_kwargs.get("timeout", 15)` — a 30 s worst case
    against a 25 s daemon cap, and the legacy `sparkrun status` leg ran under a
    10 s cap (under ONE round). So a merely SLOW node — no fault anywhere —
    produced `FLEET VISIBILITY LOST`.

    These tests drive the REAL `util.run_cmd` timeout branch with a subprocess
    stand-in that reproduces `TimeoutExpired` when the caller's cap is shorter
    than the sweep needs, so the verdict comes from the code path, not from an
    assertion about a constant. The negative control lowers the cap to the old
    value and asserts the alarm DOES fire — without it, every test here would
    pass even if the constants were reverted.
    """

    SLOW_SWEEP_SECONDS = 20  # "merely slow node" per the audit's acceptance case

    _PAYLOAD = json.dumps({"groups": {"cid_a": {
        "meta": {"recipe": "w"}, "containers": [{"host": "192.0.2.2"}]}},
        "solo_entries": []})
    _JOBS_TEXT = "Job: w [cid_a]\n"

    def _dgx_setup(self, tmp_path, monkeypatch):
        from hscc_daemon import health, serving, state as state_mod
        monkeypatch.setattr(health, "log", lambda *a, **k: None)
        monkeypatch.setattr(health, "ssh_cmd",
                            lambda *a, **k: {"ok": True, "output": "reachable"})
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": True, "status": 200})
        monkeypatch.setattr(serving, "PRIMARY_NODE", "192.0.2.2")
        monkeypatch.setattr(serving, "VLLM_HEALTH_URL", "http://192.0.2.2:8000/health")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        return health, state_dir

    def _slow_sweep(self, monkeypatch, slow_seconds=None):
        """Make every sparkrun status shell-out need `slow_seconds` to finish.

        Replaces `subprocess.run` (what `util.run_cmd` calls) with one that
        raises `TimeoutExpired` exactly when the caller's cap is below the
        sweep's need — the same branch the real timeout takes.
        """
        import subprocess as _sp
        from pathlib import Path
        slow = self.SLOW_SWEEP_SECONDS if slow_seconds is None else slow_seconds
        calls = []

        class _Completed:
            def __init__(self, out):
                self.returncode, self.stdout, self.stderr = 0, out, ""

        def fake_run(args, **kw):
            cap = kw.get("timeout")
            calls.append({"argv0": str(args[0]), "cap": cap})
            if cap is None:
                raise AssertionError(
                    "sparkrun status shell-outs must pass an explicit timeout "
                    f"(audit §c3): {str(args)[:120]}")
            if slow > cap:
                raise _sp.TimeoutExpired(cmd=list(args), timeout=cap)
            name = Path(str(args[0])).name
            out = self._PAYLOAD if name.startswith("python") else self._JOBS_TEXT
            return _Completed(out)

        monkeypatch.setattr(_sp, "run", fake_run)
        return calls

    # ── the acceptance case ────────────────────────────────────────────────
    def test_slow_structured_sweep_is_not_visibility_lost(self, tmp_path, monkeypatch):
        """A 20 s structured sweep (slow node) yields ok=True, not LOST."""
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        calls = self._slow_sweep(monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python",
                            lambda: "/fake/sparkrun-venv/bin/python")
        warns = []
        monkeypatch.setattr(health, "log",
                            lambda msg, level="INFO": warns.append((level, msg)))

        ok = health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())

        assert calls and calls[0]["cap"] >= self.SLOW_SWEEP_SECONDS, (
            "the structured leg must be given a cap above the sweep's own "
            f"worst case; got {calls[0]['cap'] if calls else None}")
        assert entry["details"]["fleet_visibility"] == "ok"
        assert entry["ok"] is True
        assert entry["details"]["workloads"] == [{"name": "w (cid_a)",
                                                  "container": "cid_a"}]
        assert ok is True
        assert not any(lvl == "WARN" and "VISIBILITY" in msg.upper()
                       for lvl, msg in warns), warns

    def test_slow_legacy_sweep_is_not_visibility_lost(self, tmp_path, monkeypatch):
        """The text-parse leg gets the same budget — a 20 s sweep is not LOST.

        The old leg capped at 10 s, i.e. below a single sweep round, so it was
        the leg that printed `FLEET VISIBILITY LOST`.
        """
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        calls = self._slow_sweep(monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)  # legacy only
        warns = []
        monkeypatch.setattr(health, "log",
                            lambda msg, level="INFO": warns.append((level, msg)))

        health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())

        assert calls and calls[0]["cap"] >= self.SLOW_SWEEP_SECONDS, (
            f"legacy leg cap is {calls[0]['cap'] if calls else None}")
        assert entry["details"]["fleet_visibility"] == "ok"
        # The text-parse shape: name from the `Job:` token, container from [..].
        assert entry["details"]["workloads"] == [{"name": "w", "container": "cid_a"}]
        assert not any("VISIBILITY" in msg.upper() for _, msg in warns), warns

    def test_old_caps_would_have_failed_this_same_sweep(self, tmp_path, monkeypatch):
        """NEGATIVE CONTROL: with the old 10 s cap the same slow sweep DOES
        publish degraded + LOST. Proves the two tests above discriminate on the
        cap rather than passing because the stub is friendly.
        """
        health, state_dir = self._dgx_setup(tmp_path, monkeypatch)
        self._slow_sweep(monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python",
                            lambda: "/fake/sparkrun-venv/bin/python")
        monkeypatch.setattr(health, "SPARKRUN_STATUS_TIMEOUT", 10)  # pre-fix cap
        warns = []
        monkeypatch.setattr(health, "log",
                            lambda msg, level="INFO": warns.append((level, msg)))

        health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert entry["details"]["fleet_visibility"] == "degraded"
        assert any("VISIBILITY" in msg.upper() for _, msg in warns)

    # ── the arithmetic that keeps the two halves from drifting ─────────────
    def test_cap_sits_above_the_sweeps_own_worst_case(self, monkeypatch):
        """cap > 2 rounds x per-host timeout, so a self-limited sweep fits."""
        from hscc_daemon import health
        assert health.SPARKRUN_SWEEP_ROUNDS == 2, (
            "sparkrun runs the docker + local host-scope executors sequentially "
            "(orchestration/executor.py:170) — if that changes, re-derive this")
        assert health.SPARKRUN_SWEEP_WORST_CASE == (
            health.SPARKRUN_SWEEP_ROUNDS * health.SPARKRUN_SWEEP_HOST_TIMEOUT)
        assert health.SPARKRUN_STATUS_TIMEOUT > health.SPARKRUN_SWEEP_WORST_CASE, (
            "the daemon cap must be ABOVE the operation it wraps (audit §c3)")
        # Concrete numbers the card asked for: 10 s/host self-limit, 40 s cap.
        assert health.SPARKRUN_SWEEP_HOST_TIMEOUT == 10
        assert health.SPARKRUN_STATUS_TIMEOUT == 40

    def test_sweep_script_self_limits_under_the_cap(self):
        """The structured script must pass ssh_kwargs['timeout'] — without it
        sparkrun defaults to 15 s/host and the sweep's worst case (30 s) is the
        very number the cap was built to clear."""
        from hscc_daemon import health
        assert "ssh_kwargs['timeout']=" in health._SPARKRUN_STATUS_SCRIPT
        assert f"ssh_kwargs['timeout']={health.SPARKRUN_SWEEP_HOST_TIMEOUT}" \
            in health._SPARKRUN_STATUS_SCRIPT

    def test_sweep_script_still_compiles(self):
        """The script is an f-string now; a missing `\\n` would only surface as
        an ImportError/IndentationError at daemon runtime, every tick."""
        from hscc_daemon import health
        compile(health._SPARKRUN_STATUS_SCRIPT, "<sparkrun-status>", "exec")

    def test_every_sweep_caller_shares_the_constant(self):
        """No caller may re-hardcode a numeric cap for the sweep operation —
        that is how the 25 s and 10 s caps drifted inside the sweep's budget in
        the first place (audit §c8-1).

        Matched on AST nodes, not grep and not `ast.dump` strings: a text match
        survives reformatting for the wrong thing (a dump-string probe of this
        test's first draft silently MISSED the legacy `sparkrun status` leg
        because `ast.dump` quotes constants with single quotes, and would have
        false-matched `sparkrun proxy status --json` once "fixed" naively).
        """
        import ast
        import inspect
        from pathlib import Path
        from hscc_daemon import health, recover

        def is_sweep_argv(node):
            """`sparkrun_bin.argv("status")` — exactly one positional arg."""
            return (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "argv"
                    and len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "status")

        def is_sweep_call(node):
            if "SPARKRUN_STATUS_SCRIPT" in ast.dump(node.args[0]):
                return "structured"
            if is_sweep_argv(node.args[0]):
                return "legacy"
            return None

        def sweep_calls(path):
            tree = ast.parse(Path(path).read_text())
            hits = []
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "id", None) == "run_cmd"
                        and node.args):
                    continue
                leg = is_sweep_call(node)
                if leg is None:
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                hits.append((leg, node.lineno, kw.get("timeout")))
            return hits

        seen_legs = set()
        for mod in (health, recover):
            src = inspect.getsourcefile(mod)
            hits = sweep_calls(src)
            assert hits, f"{mod.__name__}: expected sweep run_cmd calls"
            for leg, lineno, value in hits:
                seen_legs.add(leg)
                assert not isinstance(value, ast.Constant), (
                    f"{src}:{lineno} ({leg} leg) hardcodes a numeric sweep cap "
                    f"({getattr(value, 'value', None)}s) — use "
                    "health.SPARKRUN_STATUS_TIMEOUT")
        # Both legs must be IN the net: the legacy text-parse leg is the one that
        # carried the 10 s cap, and a guard that only sees the structured leg
        # would let that regression back in unnoticed.
        assert seen_legs == {"structured", "legacy"}, (
            f"guard must cover both sweep legs; saw only {seen_legs}")


class TestFleetVisibilityLastKnownGood:
    """t_84381a8b — a slow-node blip must not become an alarm storm (audit §c8-2).

    Pre-fix, `_sparkrun_workloads` asserted `FLEET VISIBILITY LOST` on the FIRST
    failed sweep and re-printed it per caller: the 2026-10-30 window logged 35
    WARNs for one 35-minute outage while `/health` stayed `3/3 online`. Now every
    successful sweep writes `~/.hscc/fleet_last_seen.json`, and a failed sweep
    publishes the AGE of that snapshot instead of asserting loss; hard loss
    needs either `FLEET_LOST_AFTER_CONSECUTIVE_FAILURES` failed sweeps in a row
    or an aged-out snapshot.

    Hermetic throughout: `FLEET_LAST_SEEN_FILE` is redirected by the package
    conftest (same audit that pins `_WORKER_RELATCH_FILE`), and the in-process
    tally + WARN latch are rebound per test, so nothing here touches the live
    `~/.hscc` or depends on test order.
    """

    _PAYLOAD = json.dumps({"groups": {"cid_a": {
        "meta": {"recipe": "w"},
        "containers": [{"host": "192.0.2.2"}, {"host": "192.0.2.3"}]}},
        "solo_entries": []})

    def _setup(self, tmp_path, monkeypatch, logs=None):
        from hscc_daemon import health, serving, state as state_mod
        sink = logs if logs is not None else []
        monkeypatch.setattr(health, "log",
                            lambda msg, level="INFO": sink.append((level, msg)))
        monkeypatch.setattr(health, "ssh_cmd",
                            lambda *a, **k: {"ok": True, "output": "reachable"})
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": True, "status": 200})
        monkeypatch.setattr(serving, "PRIMARY_NODE", "192.0.2.2")
        monkeypatch.setattr(serving, "VLLM_HEALTH_URL",
                            "http://192.0.2.2:8000/health")
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        health._reset_fleet_visibility_state()
        return health, state_dir, sink

    def _good_sweep(self, monkeypatch, health):
        """Structured leg answers with a real fleet snapshot."""
        monkeypatch.setattr(health, "_sparkrun_venv_python",
                            lambda: "/fake/sparkrun-venv/bin/python")
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True, "output": self._PAYLOAD})

    def _dead_sweep(self, monkeypatch, health):
        """Both legs dead — the double-fault the alarm is about."""
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": False, "output": "boom"})

    def _write_snapshot(self, health, seconds):
        """Write a snapshot stamped `seconds` ago, creating parents.

        Used by the cases that age a snapshot they never wrote (an aged-out or
        corrupted file is a state the daemon can WAKE UP to, not only one it
        earns mid-run), and by the reset-then-age cases.
        """
        from pathlib import Path
        path = Path(health.FLEET_LAST_SEEN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"hosts": ["192.0.2.2"], "workloads": [], "workload_count": 0,
                  "epoch": time.time() - seconds}
        path.write_text(json.dumps(record))
        return record

    def _write_raw(self, health, text):
        """Plant a snapshot VERBATIM (corrupt / foreign-shaped / future-stamped).

        Creates the parent dir: a daemon can WAKE UP to a bad file, so "no file
        was ever written here" must not be the only way to reach the reader.
        """
        from pathlib import Path
        path = Path(health.FLEET_LAST_SEEN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def _age_snapshot(self, health, seconds):
        """Rewrite the snapshot's stamp as if the last good read was `seconds`
        ago. Ages the FILE, not the in-process copy — that is the whole point
        of persisting it, and the reader must agree with the disk."""
        record = self._snapshot(health)
        record["epoch"] = time.time() - seconds
        self._path(health).write_text(json.dumps(record))
        return record

    @staticmethod
    def _path(health):
        """FLEET_LAST_SEEN_FILE is a str in production AND under the conftest
        redirect, so tests go through Path rather than assuming either."""
        from pathlib import Path
        return Path(health.FLEET_LAST_SEEN_FILE)

    def _snapshot(self, health):
        return json.loads(self._path(health).read_text())

    # ── the snapshot ───────────────────────────────────────────────────────
    def test_successful_sweep_writes_the_snapshot(self, tmp_path, monkeypatch):
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)

        assert health._sparkrun_workloads() == (
            [{"name": "w (cid_a)", "container": "cid_a"}], False, "ok")
        record = self._snapshot(health)
        assert record["hosts"] == ["192.0.2.2", "192.0.2.3"]
        assert record["workload_count"] == 1
        assert record["workloads"] == [{"name": "w (cid_a)", "container": "cid_a"}]
        # Both stamps: `as_of` is the field the audit specified, `epoch` is what
        # the reader prefers (no timezone parsing on the hot path).
        assert record["epoch"] == pytest.approx(health._fleet_last_seen_epoch)
        assert health._epoch_from_iso(record["as_of"]) == pytest.approx(
            health._fleet_last_seen_epoch, abs=2)

    def test_legacy_leg_success_also_stamps_freshness(self, tmp_path, monkeypatch):
        """A working text-parse IS an observation — it must not read as stale."""
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python", lambda: None)
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True, "output": "Job: w [cid_a]"})

        assert health._sparkrun_workloads() == (
            [{"name": "w", "container": "cid_a"}], False, "ok")
        assert self._snapshot(health)["workload_count"] == 1

    def test_host_list_early_exit_is_not_a_fresh_observation(self, tmp_path,
                                                             monkeypatch):
        """The script's `if not hosts` path prints {"host_list": []} — stamping
        freshness with that would let a broken host resolution keep the fleet
        looking fresh forever, i.e. permanently hide the outage."""
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(health, "_sparkrun_venv_python",
                            lambda: "/fake/sparkrun-venv/bin/python")
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: {"ok": True,
                                               "output": '{"host_list": []}'})

        workloads, degraded, visibility = health._sparkrun_workloads()
        assert (workloads, degraded, visibility) == ([], False, "ok")
        assert not self._path(health).exists()

    # ── the staleness stamp (the card's acceptance) ────────────────────────
    def test_first_failed_sweep_publishes_stale_not_lost(self, tmp_path, monkeypatch):
        health, state_dir, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health.check_dgx()                      # establishes the snapshot
        self._dead_sweep(monkeypatch, health)

        ok = health.check_dgx()
        entry = json.loads((state_dir / "dgx.json").read_text())

        visibility = entry["details"]["fleet_visibility"]
        assert visibility.startswith("stale (last good ")
        # The RED stream names the SAME age the visibility value carries — one
        # fact, stated once, not two numbers that can drift apart.
        assert visibility[len("stale "):] in entry["message"]
        assert "VISIBILITY STALE" in entry["message"]
        # Still RED: "could not look" is not a healthy read, stale or not.
        assert entry["ok"] is False
        # The alarm rule is unchanged for the serving layer, so a status-polling
        # blip still cannot restart a healthy vLLM.
        assert ok is True
        assert not any("LOST" in msg.upper() for _, msg in logs), logs

    def test_stale_warn_names_the_age(self, tmp_path, monkeypatch):
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)
        # The audit's own example age, stamped into the file the reader uses.
        self._age_snapshot(health, 250)

        health._sparkrun_workloads()
        warns = [m for lvl, m in logs if lvl == "WARN" and "VISIBILITY" in m.upper()]
        assert warns and "4 m 10 s ago" in warns[0], warns
        assert "STALE" in warns[0].upper()

    def test_no_snapshot_at_all_still_asserts_loss(self, tmp_path, monkeypatch):
        """A fleet we have NEVER seen has no last-good to be stale about — the
        old fail-closed verdict must survive, or this card would turn a real
        blind daemon green."""
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._dead_sweep(monkeypatch, health)

        workloads, degraded, visibility = health._sparkrun_workloads()
        assert (workloads, degraded, visibility) == ([], True, "degraded")
        assert not self._path(health).exists()
        assert any(lvl == "WARN" and "LOST" in msg.upper() for lvl, msg in logs)

    def test_unparsable_snapshot_is_not_evidence(self, tmp_path, monkeypatch):
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        self._dead_sweep(monkeypatch, health)
        self._write_raw(health, "{not json")

        assert health._read_fleet_last_seen() == (None, None)
        assert health._sparkrun_workloads()[2] == "degraded"

    def test_future_stamped_snapshot_is_not_evidence(self, tmp_path, monkeypatch):
        """Clock jump / hand-edited file: a snapshot from the future proves
        nothing, and an age computed from it would be negative forever."""
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        self._dead_sweep(monkeypatch, health)
        self._write_raw(health, json.dumps(
            {"as_of": "2099-01-01T00:00:00+00:00", "hosts": [], "workloads": []}))

        assert health._read_fleet_last_seen() == (None, None)
        assert health._sparkrun_workloads()[2] == "degraded"

    def test_aged_snapshot_from_a_previous_process_is_loss_on_tick_one(
            self, tmp_path, monkeypatch):
        """THE reason the age rule exists alongside the streak rule: a daemon
        that RESTARTS mid-outage starts its failure count at zero, so the
        streak can never catch a file that is already an hour old. Only the age
        read off disk knows the truth — and this is the case the whole design
        (a file, not a clock in memory) was chosen for."""
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._dead_sweep(monkeypatch, health)
        self._write_snapshot(health, 3600)   # a stale file left by an old process

        assert health._sparkrun_workloads()[2] == "degraded"
        assert any("aged past" in m for _lvl, m in logs), logs

    # ── the loss threshold ─────────────────────────────────────────────────
    def test_hard_loss_needs_consecutive_failures_not_one_blip(
            self, tmp_path, monkeypatch):
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)

        n = health.FLEET_LOST_AFTER_CONSECUTIVE_FAILURES
        for i in range(n - 1):
            assert health._sparkrun_workloads()[2].startswith("stale"), i
        assert health._sparkrun_workloads()[2] == "degraded"
        lost = [m for lvl, m in logs if "LOST" in m.upper()]
        assert len(lost) == 1, f"expected exactly one LOST line, got {lost}"
        # The hard line is still honest about how long we could actually see.
        assert "Last good fleet read" in lost[0]

    def test_one_success_resets_the_failure_tally(self, tmp_path, monkeypatch):
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)
        n = health.FLEET_LOST_AFTER_CONSECUTIVE_FAILURES
        for _ in range(n - 1):
            health._sparkrun_workloads()
        assert health._fleet_visibility_state["failures"] == n - 1

        self._good_sweep(monkeypatch, health)
        assert health._sparkrun_workloads()[2] == "ok"
        assert health._fleet_visibility_state["failures"] == 0

        self._dead_sweep(monkeypatch, health)
        # Back to ONE failure, not resuming the streak — an intermittent sparkrun
        # that answers now and then is not an outage.
        assert health._sparkrun_workloads()[2].startswith("stale")

    def test_aged_snapshot_is_loss_on_the_first_failure(self, tmp_path, monkeypatch):
        """The age rule is independent of the streak rule: a snapshot older
        than the cap is not evidence of a live fleet even on failure #1."""
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)
        self._age_snapshot(health, health.FLEET_STALE_LOST_AFTER_S + 60)

        assert health._sparkrun_workloads()[2] == "degraded"
        assert any("aged past" in m for _lvl, m in logs), logs

    # ── the dedupe (the ORCH ask: 35 WARNs = 1 alarm) ─────────────────────
    def test_outage_logs_lost_once_however_many_callers(
            self, tmp_path, monkeypatch):
        """The audit's count was 35 LOST lines for 35 minutes of one outage,
        because every caller of the sweep re-printed it on every tick."""
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)

        # A whole outage: the hard threshold plus 30 further ticks/callers.
        for _ in range(health.FLEET_LOST_AFTER_CONSECUTIVE_FAILURES + 30):
            health._sparkrun_workloads()

        lost = [m for _lvl, m in logs if "LOST" in m.upper()]
        assert len(lost) == 1, f"{len(lost)} LOST lines for one outage"
        # The stale path keeps informing (it is not an alarm), so the outage is
        # not silent — 3 stale lines before the threshold was reached.
        assert len([m for _lvl, m in logs if "STALE" in m.upper()]) == 2

    def test_recovery_logs_one_recovered_line(self, tmp_path, monkeypatch):
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)
        for _ in range(health.FLEET_LOST_AFTER_CONSECUTIVE_FAILURES):
            health._sparkrun_workloads()
        assert any("LOST" in m.upper() for _lvl, m in logs)

        self._good_sweep(monkeypatch, health)
        assert health._sparkrun_workloads()[2] == "ok"
        recovered = [m for _lvl, m in logs if "RECOVERED" in m.upper()]
        assert len(recovered) == 1, recovered

        # A second good sweep must not re-announce; a healthy fleet is quiet.
        health._sparkrun_workloads()
        assert len([m for _lvl, m in logs if "RECOVERED" in m.upper()]) == 1

    def test_stale_outage_recovery_is_silent(self, tmp_path, monkeypatch):
        """No alarm was raised ⇒ no recovery line. Otherwise the log gains a
        'recovered' entry for a fault that was never asserted."""
        health, _state, logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        health._sparkrun_workloads()
        self._dead_sweep(monkeypatch, health)
        assert health._sparkrun_workloads()[2].startswith("stale")
        self._good_sweep(monkeypatch, health)
        assert health._sparkrun_workloads()[2] == "ok"
        assert not any("RECOVERED" in m.upper() for _lvl, m in logs), logs

    # ── the arithmetic that keeps the rule from drifting back ─────────────
    def test_loss_threshold_is_not_one_sweep(self):
        """Regression pin for the actual complaint: the FIRST failure must not
        assert loss, so the streak threshold cannot be 1."""
        from hscc_daemon import health
        assert health.FLEET_LOST_AFTER_CONSECUTIVE_FAILURES >= 2
        # 3 dgx ticks (60 s each, PERIODIC_INTERVALS["dgx"]): ~3 minutes, i.e.
        # two full audit-window blips of headroom before a hard alarm.
        assert health.FLEET_LOST_AFTER_CONSECUTIVE_FAILURES == 3
        # The age cap must cover the audit's own worst blip (the LOST window
        # whose example age the card quotes is 4 m 10 s) — otherwise the fix
        # would not have changed that episode's verdict at all — while still
        # keeping a hard alarm reachable in minutes, not hours.
        assert health.FLEET_STALE_LOST_AFTER_S >= 250
        assert health.FLEET_STALE_LOST_AFTER_S <= 600

    def test_verdict_shape_is_stable_for_consumers(self, tmp_path, monkeypatch):
        """`fleet_visibility` is a published key (verify + the audits read it):
        'ok' and 'degraded' keep their exact strings, staleness is additive."""
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)
        assert health._sparkrun_workloads()[2] == "ok"
        # No snapshot anywhere = the never-observed shape, unchanged verdict.
        self._path(health).unlink()
        health._reset_fleet_visibility_state()
        self._dead_sweep(monkeypatch, health)
        assert health._sparkrun_workloads()[2] == "degraded"

    def test_age_phrase_format(self):
        from hscc_daemon import health
        assert health._age_phrase(45) == "45 s ago"
        assert health._age_phrase(250) == "4 m 10 s ago"
        assert health._age_phrase(3725) == "1 h 2 m ago"
        assert health._age_phrase(-5) == "0 s ago"

    def test_snapshot_write_is_atomic_under_concurrent_sweeps(
            self, tmp_path, monkeypatch):
        """check_dgx can be reached by several callers per tick (audit §c6
        counted four sweeps/tick), so a fixed tmp name would let one writer's
        rename take another's tmp with it. Prove the file is always whole."""
        import concurrent.futures
        import glob
        health, _state, _logs = self._setup(tmp_path, monkeypatch)
        self._good_sweep(monkeypatch, health)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: health._sparkrun_workloads(), range(40)))

        # Every write parses — a torn file would raise here.
        record = self._snapshot(health)
        assert record["hosts"] == ["192.0.2.2", "192.0.2.3"]
        leftovers = glob.glob(str(health.FLEET_LAST_SEEN_FILE) + "*")
        assert leftovers == [str(health.FLEET_LAST_SEEN_FILE)], leftovers


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
