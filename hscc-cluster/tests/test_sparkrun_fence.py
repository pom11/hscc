"""Guard tests for the sparkrun argv fence (t_be4b94ca).

The live incident this suite exists to prevent: the operator's inference proxy
on :4000 was SIGTERM'd twice while the hscc-cluster suite ran, because literal
``subprocess.run(["sparkrun", "proxy", "stop"], ...)`` call sites in
hscc-cluster reach the operator's REAL sparkrun CLI (it is on PATH on this
host) and the daemon's ``HSCC_SPARKRUN_BIN`` fence cannot see raw argv.
Mirrors the doctrine of hscc_daemon/tests/conftest.py::_hermetic_sparkrun_resolution
(t_b543e530 precedent), which pins resolution for the daemon side.

These tests pin BOTH directions of the fence — a fence that blocks everything
would pass a naive "did it raise" test while making honest argv-shape tests
impossible, and a fence that blocks nothing is how the operator lost the proxy:

  * a sparkrun argv that resolves to the live CLI must be BLOCKED (checked and
    exec'd, with EXPECT_BLOCK tagged so the session guard ignores it);
  * a per-test fake sparkrun script under tmp_path must be ALLOWED and must
    actually exec;
  * non-sparkrun argv must be untouched;
  * the inline-payload (``python -c "from sparkrun..."``) shape must be blocked.

The session-wide guard (any UNEXPECTED blocked call fails the suite) lives in
conftest.py::pytest_sessionfinish — it must see the whole run, so it cannot
live in one test module.
"""

import os
import subprocess
import sys

import pytest

import sparkrun_fence as sf


@pytest.fixture
def expect_block(monkeypatch):
    """Tag blocks raised by THIS test as intentional for the session guard."""
    monkeypatch.setattr(sf, "EXPECT_BLOCK", True)


class TestDetectionIsExact:
    """check() is pure inspection: assert both directions exec nothing."""

    def test_live_cli_argv_is_violation(self, expect_block):
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH — fence has nothing to protect here")
        reason = sf.check(["sparkrun", "proxy", "stop"])
        assert reason and "REAL sparkrun" in reason
        # The violation names what it resolved to: the operator's own path.
        assert (sf.live_sparkrun() or "<unset>") in reason

    def test_live_cli_argv_including_stop_all_is_violation(self, expect_block):
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH")
        assert sf.check(["sparkrun", "stop", "--all", "--hosts", "10.0.0.2"])

    def test_shell_string_form_is_violation(self, expect_block):
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH")
        # hscc.py cmd_monitor builds a shell=True string ("timeout 3 sparkrun
        # cluster monitor --simple --json"): the shell interprets every token,
        # so every token is a candidate program — not just the first.
        assert sf.check("timeout 3 sparkrun cluster monitor --simple --json",
                        shell=True)
        # Without shell=True the whole string is ONE program name (which does
        # not exist), so it is not the live CLI and must not be blocked.
        assert sf.check("timeout 3 sparkrun cluster monitor") is None

    def test_fake_script_argv_is_allowed(self, tmp_path):
        fake = tmp_path / "sparkrun"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        assert sf.check([str(fake), "proxy", "stop"]) is None

    def test_name_lookup_outside_live_path_is_allowed(self, tmp_path, monkeypatch):
        # A `sparkrun` earlier on PATH than the live CLI is still not the live
        # CLI: resolution decides, not the bare name.
        fakebin = tmp_path / "bin"
        fakebin.mkdir()
        fake = fakebin / "sparkrun"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ["PATH"])
        assert sf.check(["sparkrun", "status"]) is None

    def test_non_sparkrun_argv_is_allowed(self):
        assert sf.check(["git", "status"]) is None
        assert sf.check([sys.executable, "-c", "print(1)"]) is None
        assert sf.check(None) is None
        assert sf.check([]) is None

    def test_inline_sparkrun_payload_is_violation(self, expect_block):
        # The sparkrun_bridge shape: argv[0] is a python interpreter, so the
        # name test misses it — the payload markers catch it.
        reason = sf.check([sys.executable, "-c",
                           "from sparkrun.proxy.config import ProxyConfig"])
        assert reason and "sparkrun.proxy.config" in reason
        reason = sf.check([sys.executable, "-c",
                           "from sparkrun.orchestration.ssh import run_remote_script"])
        assert reason and "sparkrun.orchestration.ssh" in reason


class TestExecLayerRaisesInsteadOfRunning:
    """The autouse fixture must turn a blocked call into a test failure, not
    an exec — and leave honest calls working."""

    def test_fenced_run_of_live_cli_raises(self, expect_block):
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH")
        # subprocess.run here is the FENCED wrapper (autouse conftest fixture).
        # Without the fence this would SIGTERM the operator's proxy.
        with pytest.raises(sf.SparkrunFenceViolation) as ei:
            subprocess.run(["sparkrun", "proxy", "stop"],
                           capture_output=True, text=True, timeout=5)
        assert "SPARKRUN FENCE" in str(ei.value)

    def test_fenced_popen_of_live_cli_raises(self, expect_block):
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH")
        with pytest.raises(sf.SparkrunFenceViolation):
            subprocess.Popen(["sparkrun", "stop", "--all", "--hosts", "10.0.0.2"])

    def test_fenced_run_of_fake_sparkrun_execs_normally(self, tmp_path):
        fake = tmp_path / "sparkrun"
        fake.write_text("#!/bin/sh\necho faked\n")
        fake.chmod(0o755)
        r = subprocess.run([str(fake), "status"], capture_output=True,
                           text=True, timeout=15)
        assert r.returncode == 0 and "faked" in r.stdout

    def test_per_test_subprocess_patch_intercepts_before_fence(self):
        # The compatibility property the existing suite depends on: a test that
        # patches subprocess.run itself (unittest.mock.patch) replaces the
        # wrapper, so argv-shape tests keep working untouched.
        from unittest.mock import MagicMock, patch
        with patch.object(subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="",
                                                 stderr="")) as m:
            r = subprocess.run(["sparkrun", "proxy", "start"])
        assert r.returncode == 0
        assert m.call_args[0][0] == ["sparkrun", "proxy", "start"]

    def test_recorder_sees_blocked_call(self, expect_block):
        sf.SEEN.clear()
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH")
        with pytest.raises(sf.SparkrunFenceViolation):
            subprocess.run(["sparkrun", "proxy", "stop"])
        assert sf.SEEN and sf.SEEN[-1]["blocked"]
        assert sf.SEEN[-1]["argv"][:2] == ["sparkrun", "proxy"]


class TestFenceItselfCannotBeSilentlyBroken:
    """Mutation-resistance for the fence module itself."""

    def test_report_is_never_silent_about_the_live_cli(self):
        text = sf.report()
        assert "SPARKRUN FENCE" in text

    def test_check_is_pure_no_process_spawned_for_blocked_argv(self, expect_block,
                                                                monkeypatch):
        # check() must not itself exec anything while inspecting.
        if not sf.live_sparkrun():
            pytest.skip("no sparkrun on PATH")
        calls = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: calls.append(a))
        sf.check(["sparkrun", "proxy", "stop"])
        assert calls == []

    def test_unexpected_blocked_tracks_only_unexpected(self, monkeypatch):
        # The session guard's predicate, pinned directly: an event recorded
        # while EXPECT_BLOCK is set is never "unexpected"; one without it is.
        n0 = len(sf.unexpected_blocked())
        monkeypatch.setattr(sf, "EXPECT_BLOCK", False)
        sf._record({"argv": ["sparkrun", "x"], "resolved": "/live/sparkrun",
                    "blocked": True, "why": "synthetic"})
        assert len(sf.unexpected_blocked()) == n0 + 1
        # Clean up the synthetic event so the session guard (and the final
        # report) does not see it.
        sf.BLOCKED.remove(sf.BLOCKED[-1])
        sf.SEEN.remove(sf.SEEN[-1])
        sf.EVENTS.remove(sf.EVENTS[-1])
        assert len(sf.unexpected_blocked()) == n0
