"""t_be4b94ca — the topology fallback must not exec the operator's real CLI.

`resolve_cluster_config()`'s sparkrun fallback used to be
``subprocess.run("timeout 2 sparkrun cluster list --json", shell=True, ...)``.
A shell string cannot be argv-rewritten by ``sparkrun_bin.exec_argv``, and the
test suite's ``HSCC_SPARKRUN_BIN`` pin is invisible to it — the daemon
conftest's own docstring admits this ("the ``sparkrun cluster list --json``
fallback shell-out, which the ``HSCC_SPARKRUN_BIN`` override does NOT cover").
The consequence, measured: three tests that deliberately hit the fallback
(missing / non-dict / malformed cluster.json) exec'd the operator's LIVE
sparkrun CLI. The sparkrun argv fence caught all three.

Fix: build the fallback as a LIST argv through ``sparkrun_bin.argv`` — the
repo's single execution chokepoint for sparkrun (PATH-first absolute
resolution + the ``HSCC_SPARKRUN_BIN`` override) — and drop ``shell=True``,
which the command never needed (no shell metacharacters).

These tests pin the fix at the seam, not at the source text: a fake CLI on a
PATH-less HOME proves the fallback runs the RESOLVED binary.
"""

import json
import os
import stat

import pytest


def _fake_cli(tmp_path, payload="[]"):
    """An executable fake sparkrun that records how it was invoked."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    cli = bin_dir / "sparkrun"
    cli.write_text("#!/bin/sh\n"
                   f'echo "$0 $*" >> "{log}"\n'
                   f"printf '%s' '{payload}'\n"
                   "exit 0\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # `timeout` is not guaranteed on every PATH (macOS keeps it in Homebrew);
    # ship a shim so the fallback reaches the fake instead of dying on 127.
    tmo = bin_dir / "timeout"
    tmo.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    tmo.chmod(tmo.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return cli, log


def _clean_serving(tmp_path, monkeypatch, cluster_json=None):
    """Point serving at empty tmp state and force the sparkrun fallback."""
    from hscc_daemon import serving
    monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster_json or
                                                  tmp_path / "cluster.json"))
    monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_path / "serving.json"))
    serving.reset_resolution_cache()
    serving._materialize_topology_defaults()
    return serving


class TestFallbackGoesThroughTheResolutionSeam:
    def test_missing_cluster_json_runs_the_resolved_cli_not_a_bare_name(
            self, tmp_path, monkeypatch):
        cli, log = _fake_cli(tmp_path, json.dumps(
            [{"name": "hscc", "hosts": ["10.0.0.7"], "default": True}]))
        monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
        from hscc_daemon import sparkrun_bin
        sparkrun_bin.reset_cache()
        try:
            serving = _clean_serving(tmp_path, monkeypatch)
            serving.resolve_cluster_config()
            assert serving.PRIMARY_NODE == "10.0.0.7", (
                "fallback did not resolve from the fake cluster list")
            calls = log.read_text().splitlines() if log.exists() else []
            assert calls, "fallback never invoked sparkrun"
            # argv[0] is the RESOLVED absolute path — never the bare name that
            # a service PATH without ~/.local/bin cannot exec, and never the
            # operator's CLI when an override is set.
            assert calls[0].split()[0] == os.path.realpath(str(cli)), calls
            assert "cluster list --json" in calls[0]
        finally:
            sparkrun_bin.reset_cache()

    def test_fallback_carries_no_shell(self, tmp_path, monkeypatch):
        """shell=True is what made this call invisible to every fence/override.

        A fake CLI that answers the cluster list proves the fallback works
        WITHOUT a shell; the source-text pin is the cheap canary that the
        rewrite cannot silently regress.
        """
        cli, log = _fake_cli(tmp_path, json.dumps(
            [{"name": "hscc", "hosts": ["10.0.0.9"], "default": True}]))
        monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
        from hscc_daemon import sparkrun_bin, serving
        sparkrun_bin.reset_cache()
        seen = []

        real_run = __import__("subprocess").run

        def spy(argv, **kwargs):
            seen.append((argv, kwargs.get("shell", False)))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(serving.subprocess, "run", spy)
        try:
            serving = _clean_serving(tmp_path, monkeypatch)
            serving.resolve_cluster_config()
        finally:
            sparkrun_bin.reset_cache()
        spark_calls = [s for s in seen
                       if any("sparkrun" in str(a) for a in (s[0] if isinstance(s[0], list) else [s[0]]))]
        assert spark_calls, "the fallback never shelled out at all"
        for argv, shell in spark_calls:
            assert shell is False, "sparkrun fallback still uses shell=True"
            assert isinstance(argv, list), "sparkrun fallback must be list argv"

    def test_malformed_cluster_json_does_not_reach_a_real_cli(
            self, tmp_path, monkeypatch):
        """The exact shape the fence caught: corrupt cluster.json + fallback.

        With no sparkrun resolvable anywhere the fallback must fail LOUD-SAFE
        (a warning, default topology) rather than raise out of resolution.
        """
        missing = tmp_path / "no-such-bin" / "sparkrun"
        monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(missing))
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        from hscc_daemon import sparkrun_bin
        sparkrun_bin.reset_cache()
        try:
            cluster = tmp_path / "cluster.json"
            cluster.write_text("{bad json")
            serving = _clean_serving(tmp_path, monkeypatch, cluster_json=cluster)
            serving.resolve_cluster_config()   # must not raise
            assert serving.PRIMARY_NODE        # still a usable topology
        finally:
            sparkrun_bin.reset_cache()
