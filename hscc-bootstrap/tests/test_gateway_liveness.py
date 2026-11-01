"""Tests for the single gateway-liveness implementation (t_c14d18cb).

Why a suite for the module itself, when `TestGatewayCheck` in test_doctor.py
already covers the row: the module now has TWO consumers — the doctor row (via
`gateway_verdict`) and the daemon's gateway stream (via `job_is_alive`, loaded
by `hscc_daemon/gateway_probe.py`). Two renderers over one set of probes is
exactly where a fork reappears, so the probe behaviour and the difference
between the two readings are pinned HERE, at the source, and not only through
one of the consumers.

Hermetic: every probe goes through an injected `_run` (or injected `ps` lines),
so the live process table, launchd and systemd are never touched.
"""

import os

import pytest

import gateway_liveness as gl


REAL_ARGV = ("/Users/example/.hermes/hermes-agent/venv/bin/python "
             "-m hermes_cli.main gateway run --replace")
LAUNCHD_OUT = (
    '{\n\t"Label" = "ai.hermes.gateway";\n\t"OnDemand" = true;\n'
    '\t"LastExitStatus" = 9;\n\t"PID" = 74857;\n'
    '\t"Program" = "/Users/example/.hermes/hermes-agent/venv/bin/python";\n};\n')


class _R:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _runner(results):
    """Fake probe runner keyed by argv[0]; records every argv it was asked for."""
    seen = []

    def run(argv, timeout):
        seen.append(list(argv))
        if argv[0] not in results:
            raise AssertionError(f"unexpected probe in test: {argv}")
        return results[argv[0]]
    run.seen = seen  # type: ignore[attr-defined]
    return run


# ── the transports ───────────────────────────────────────────────────────────

class TestSupervisorTransport:
    def test_launchd_named_pid_is_the_authority(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(0, LAUNCHD_OUT)})
        assert gl.gateway_launchd_state(_run=run) == (True, 74857)

    def test_launchd_loaded_without_pid(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        no_pid = LAUNCHD_OUT.replace('\t"PID" = 74857;\n', "")
        run = _runner({"launchctl": _R(0, no_pid)})
        assert gl.gateway_launchd_state(_run=run) == (True, None)

    def test_launchd_service_missing_is_answered_not_loaded(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(1, "Could not find service.")})
        assert gl.gateway_launchd_state(_run=run) == (False, None)

    def test_launchd_probe_failure_is_unknown(self, monkeypatch):
        """rc 0 is not required to be believed; a probe that cannot run is None.

        This distinction is the whole GAP-F-3 lesson: "the probe broke" and
        "there is nothing there" are different facts.
        """
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        assert gl.gateway_launchd_state(_run=lambda a, t: None) == (None, None)

        def boom(argv, timeout):
            raise OSError("launchctl not runnable")
        assert gl.gateway_launchd_state(_run=boom) == (None, None)

    def test_non_macos_does_not_probe_launchctl(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        run = _runner({})
        assert gl.gateway_launchd_state(_run=run) == (None, None)
        assert run.seen == []

    def test_systemd_active_reports_loaded(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(gl.shutil, "which",
                            lambda n: "/bin/systemctl" if n == "systemctl" else None)
        run = _runner({"systemctl": _R(0, "active\n")})
        assert gl.gateway_systemd_state(_run=run) == (True, None)

    def test_systemd_inactive_and_failed_are_answered_false(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(gl.shutil, "which",
                            lambda n: "/bin/systemctl" if n == "systemctl" else None)
        for state in ("inactive", "failed"):
            run = _runner({"systemctl": _R(3, state + "\n")})
            assert gl.gateway_systemd_state(_run=run) == (False, None)

    def test_systemd_activating_is_unknown(self, monkeypatch):
        """Neither yes nor no — the caller must not read it as "down"."""
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(gl.shutil, "which",
                            lambda n: "/bin/systemctl" if n == "systemctl" else None)
        run = _runner({"systemctl": _R(0, "activating\n")})
        assert gl.gateway_systemd_state(_run=run) == (None, None)

    def test_no_systemctl_binary_is_unknown_not_false(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(gl.shutil, "which", lambda n: None)
        assert gl.gateway_systemd_state(_run=_runner({})) == (None, None)


class TestArgvScan:
    @pytest.mark.parametrize("argv", [
        REAL_ARGV,
        "python -m hermes_cli.main gateway run",
        "/opt/homebrew/bin/hermes gateway run --replace",
        "/venv/bin/python -m hermes_cli.main --replace gateway run",
        "/venv/bin/python -m hermes_cli.main gateway serve",
        "/venv/bin/python -m hermes_cli.main gateway start",
        "python3 -m hermes_cli.main gateway run --replace -p qa",
    ])
    def test_gateway_shapes_match(self, argv):
        assert gl.argv_is_gateway(argv) is True

    @pytest.mark.parametrize("argv", [
        "python /Users/example/.hermes/plugins/hscc-bootstrap/doctor.py --json",
        "/venv/bin/python -m hermes_cli.main -p qa --cli chat -q work task t_x",
        '/venv/bin/python -m hermes_cli.main chat -q "why did the gateway run fail"',
        '/bin/bash -c pgrep -f "hermes_cli.main gateway run --replace"',
        "grep gateway run --replace",
        "/usr/sbin/httpd -D FOREGROUND",
        "python -m venv /tmp/gateway",
        "/Users/example/.hermes/hermes-agent/venv/bin/python -m pytest hscc-bootstrap",
        "",
        "   ",
    ])
    def test_unrelated_argv_does_not_match(self, argv):
        assert gl.argv_is_gateway(argv) is False

    def test_scanner_skips_pid1_and_self(self):
        lines = [f"1 {__file__} gateway run --replace",
                 f"{os.getpid()} python -m hermes_cli.main gateway run --replace"]
        assert gl.gateway_argv_pid(_lines=lines) is None

    def test_scanner_returns_the_real_pid(self):
        lines = ["1 /sbin/launchd",
                 "400 /usr/sbin/httpd -D FOREGROUND",
                 f"74857 {REAL_ARGV}"]
        assert gl.gateway_argv_pid(_lines=lines) == 74857

    def test_unusable_ps_output_is_none(self):
        assert gl.gateway_argv_pid(_lines=None,
                                   _run=_runner({"ps": _R(1, "ps: error")})) is None


# ── the stream's reading (job_is_alive) ──────────────────────────────────────

class TestJobIsAlive:
    """What the daemon's gateway stream decides — three states, not two.

    The stream drives an auto-heal relaunch, so "we could not look" must not be
    reported as "down".
    """

    def test_pgrep_blind_but_ps_visible_reads_alive(self, monkeypatch):
        """The GAP-F-3 acceptance shape, at the source.

        On the operator machine the live gateway is invisible to pgrep and
        visible to ps; the supervisor is unreadable. The scan is what carries
        the verdict — and pgrep is never asked.
        """
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(1, "Could not find service."),
                       "pgrep": _R(1, ""),
                       "ps": _R(0, f"74857 {REAL_ARGV}\n")})
        alive, detail = gl.job_is_alive(_run=run)
        assert alive is True
        assert "74857" in detail
        assert not any("pgrep" in arg for argv in run.seen for arg in argv)

    def test_supervisor_pid_wins_without_a_ps_shellout(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(0, LAUNCHD_OUT)})
        alive, detail = gl.job_is_alive(_run=run)
        assert alive is True and "74857" in detail
        assert [a[0] for a in run.seen] == ["launchctl"]

    def test_loaded_job_without_pid_counts_as_present(self, monkeypatch):
        """Stream reading: a loaded job is the thing the kickstart path restarts.

        The doctor row reads the SAME state as "job loaded but no running
        process" (it prints a fix hint). Both readings come from one
        `gateway_states`, which is the point of the shared module.
        """
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        no_pid = LAUNCHD_OUT.replace('\t"PID" = 74857;\n', "")
        run = _runner({"launchctl": _R(0, no_pid)})
        assert gl.job_is_alive(_run=run)[0] is True
        check = gl.gateway_verdict(_run=_runner({"launchctl": _R(0, no_pid),
                                                 "ps": _R(0, "")}))
        assert check[0] is False and "kickstart" in check[2]

    def test_nothing_there_is_a_false_answer(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(1, "Could not find service."),
                       "ps": _R(0, "400 /usr/sbin/httpd -D FOREGROUND\n")})
        alive, detail = gl.job_is_alive(_run=run)
        assert alive is False
        assert detail

    def test_all_transports_silent_is_unknown(self, monkeypatch):
        """rc-1-vs-cannot-run: every probe broke, so there is NO verdict."""
        monkeypatch.setattr(gl.sys, "platform", "darwin")

        def boom(argv, timeout):
            raise OSError("nothing is readable here")
        alive, detail = gl.job_is_alive(_run=boom)
        assert alive is None
        assert "could not answer" in detail

    def test_systemd_active_answers_alive(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(gl.shutil, "which",
                            lambda n: "/bin/systemctl" if n == "systemctl" else None)
        run = _runner({"systemctl": _R(0, "active\n")})
        assert gl.job_is_alive(_run=run)[0] is True


# ── the doctor's reading (gateway_verdict) ───────────────────────────────────

class TestGatewayVerdict:
    def test_verdict_triple_shape(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(0, LAUNCHD_OUT)})
        alive, detail, fix = gl.gateway_verdict(_run=run)
        assert alive is True
        assert detail == "running (launchd pid 74857)"
        assert fix == ""

    def test_down_verdict_carries_a_fix_hint(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "darwin")
        run = _runner({"launchctl": _R(1, "Could not find service."),
                       "ps": _R(0, "")})
        alive, detail, fix = gl.gateway_verdict(_run=run)
        assert (alive, detail) == (False, "not running")
        assert fix

    def test_linux_loaded_job_hint_names_the_unit(self, monkeypatch):
        monkeypatch.setattr(gl.sys, "platform", "linux")
        monkeypatch.setattr(gl.shutil, "which",
                            lambda n: "/bin/systemctl" if n == "systemctl" else None)
        run = _runner({"systemctl": _R(3, "inactive\n"), "ps": _R(0, "")})
        alive, detail, fix = gl.gateway_verdict(_run=run)
        assert alive is False and detail == "not running"


# ── the two readings must not drift apart ────────────────────────────────────

class TestRenderersAgreeOnFacts:
    """Same probes, two verdict styles — but they may never disagree on FACTS.

    Where they may differ is the MEANING of "job loaded, no process" (the
    stream says present, the doctor row says not running with a kickstart
    hint), and that difference is pinned explicitly in TestJobIsAlive above.
    What is not allowed is one renderer seeing a process the other does not.
    """

    @pytest.mark.parametrize("results", [
        # supervisor names a pid
        ({"launchctl": _R(0, LAUNCHD_OUT)}, "darwin", True),
        # argv scan finds it
        ({"launchctl": _R(1, "Could not find service."),
          "ps": _R(0, f"74857 {REAL_ARGV}\n")}, "darwin", True),
        # nothing there
        ({"launchctl": _R(1, "Could not find service."),
          "ps": _R(0, "400 /usr/sbin/httpd\n")}, "darwin", False),
        # systemd active
        ({"systemctl": _R(0, "active\n")}, "linux", True),
    ])
    def test_running_is_running_for_both(self, results, monkeypatch):
        table, platform, expect_running = results
        monkeypatch.setattr(gl.sys, "platform", platform)
        monkeypatch.setattr(gl.shutil, "which",
                            lambda n: "/bin/systemctl" if n == "systemctl" else None)
        stream_alive = gl.job_is_alive(_run=_runner(table))[0]
        doctor_alive = gl.gateway_verdict(_run=_runner(dict(table)))[0]
        assert (stream_alive is True) == expect_running
        assert (doctor_alive is True) == expect_running

    def test_injected_ps_lines_are_honoured_by_both(self):
        lines = [f"74857 {REAL_ARGV}"]
        assert gl.job_is_alive(_ps_lines=lines, _run=_runner({}))[0] is True
        assert gl.gateway_verdict(_ps_lines=lines, _run=_runner({}))[0] is True
