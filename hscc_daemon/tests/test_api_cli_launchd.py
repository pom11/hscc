"""`hscc api status` / `stop` under launchd (t_d2245e7e).

Why these exist: `api_server.serve_main()` deliberately writes NO api.pid, so
`status` reported "not running" for an API that launchd had been serving since
boot, and `stop` had nothing to SIGTERM. The tempting fix — write a pid file —
would let `stop` SIGTERM a `KeepAlive`-supervised process that launchd
resurrects 30 s later: a stop that silently un-stops. So the verbs became
launchd-aware TOGETHER (that pairing is the design decision on the card, and
this file is where it is pinned): `status` consults `launchctl print`, `stop`
runs `launchctl bootout`.

Everything here goes through an injected `_run_probe` — a suite must never ask
the operator's live gui domain, and `launchd_status()` refuses to if no seam
is injected under pytest (`_refused_launchd_status`).

Never starts a server, never binds a port, never touches ~/.hscc.
"""
import io
import os
import plistlib
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

import hscc_daemon.api_cli as api_cli


# --------------------------------------------------------------------------- #
# the probe seam: canned `launchctl print` output, no real launchctl
# --------------------------------------------------------------------------- #

PRINT_RUNNING = """\
GuiJob: com.hscc.api
	path = /Users/dev/Library/LaunchAgents/com.hscc.api.plist
	type = LaunchAgent
	state = running

	pid = 42424
	plist state = running
"""

PRINT_LOADED_NO_PID = """\
GuiJob: com.hscc.api
	path = /Users/dev/Library/LaunchAgents/com.hscc.api.plist
	type = LaunchAgent
	state = not running
"""


class _Probe:
    """A fake launchctl runner. Records every argv it was asked to run."""

    def __init__(self, rc=0, out=PRINT_RUNNING):
        self.rc = rc
        self.out = out
        self.calls = []

    def __call__(self, argv, timeout=10):
        self.calls.append([str(a) for a in argv])
        return self.rc, self.out


#: The canned-probe factory, kept as the readable name the tests call.
_probe = _Probe


def _signals(kill_calls):
    """The real signals issued, excluding signal 0.

    `daemon_ops.get_pid` does a `kill(pid, 0)` liveness probe on the way in, so
    "stop must not touch the process" means no SIGTERM/SIGKILL — filtering the
    probe keeps the assertion saying exactly that.
    """
    import signal
    return [sig for _pid, sig in kill_calls
            if sig in (signal.SIGTERM, signal.SIGKILL)]


def _inject_probe(monkeypatch, probe):
    """Point `api_cli.launchd_status` at the canned probe.

    The original is captured BEFORE the patch: the obvious
    `lambda **kw: api_cli.launchd_status(_run_probe=probe)` self-recurses,
    because by the time the lambda runs the attribute IS the lambda.
    """
    real = api_cli.launchd_status
    monkeypatch.setattr(api_cli, "launchd_status",
                        lambda **kw: real(_run_probe=probe))
    return probe


# --------------------------------------------------------------------------- #
# launchd_status() — the read-only probe
# --------------------------------------------------------------------------- #

def test_probe_parses_pid_and_state_from_launchctl_print():
    st = api_cli.launchd_status(_run_probe=_probe())
    assert st["available"] is True and st["loaded"] is True
    assert st["pid"] == 42424
    assert st["state"] == "running"


def test_probe_target_is_the_gui_domain_and_the_api_label():
    p = _probe()
    api_cli.launchd_status(_run_probe=p)
    assert p.calls == [["launchctl", "print",
                        f"gui/{os.getuid()}/com.hscc.api"]]


def test_probe_reads_only_the_top_level_state_line():
    """`launchctl print` nests deeper-indented `state =` lines. The top-level
    one is tab-indented; matching a nested section would report some other
    subsystem's state as the job's."""
    st = api_cli.launchd_status(_run_probe=_probe(out=PRINT_RUNNING))
    assert st["state"] == "running"          # not the nested "plist state"


def test_probe_not_loaded_is_an_answer_not_a_fault():
    """rc!=0 from `launchctl print` means "no such job" (rc 113 on macOS 26).
    That must read as NOT LOADED — available, loaded=False — not as "cannot
    tell", or the verb would suppress its launchd row on every machine that
    never installed the unit."""
    st = api_cli.launchd_status(_run_probe=_probe(
        rc=113, out="Bad request.\nCould not find service \"com.hscc.api\""))
    assert st["available"] is True and st["loaded"] is False
    assert st["pid"] is None


def test_probe_unavailable_when_launchctl_cannot_run():
    """rc None = the binary is missing (Linux, container). The caller must fall
    back to the PID-file view, never read this as "the unit is stopped"."""
    st = api_cli.launchd_status(_run_probe=lambda argv, timeout=10:
                                (None, "command not found: launchctl"))
    assert st["available"] is False and st["loaded"] is False


def test_probe_loaded_without_pid_is_its_own_state():
    """A crash-looping tailnet unit spends most of its life here (throttled
    after a refusal). Loaded-but-no-pid is a real, reportable state."""
    st = api_cli.launchd_status(_run_probe=_probe(out=PRINT_LOADED_NO_PID))
    assert st["loaded"] is True and st["pid"] is None
    assert st["state"] == "not running"


def test_probe_refuses_to_run_inside_pytest_without_a_seam():
    """Same fence shape as hscc-bootstrap/doctor.py's _refused_supervision_check:
    the default targets are the OPERATOR's live gui domain, so a suite that
    forgets to inject must get a visible refusal, not a silent 'not loaded'
    that passes a test for the wrong reason."""
    st = api_cli.launchd_status()
    assert st.get("refused") is True
    assert st["available"] is False


def test_launchd_matters_gates_the_fork(monkeypatch, tmp_path):
    """`hscc api status` for an operator who never installed the unit must not
    fork a launchctl on every call — and a test that forgets the seam cannot
    reach the live domain."""
    monkeypatch.setattr(api_cli.sys, "platform", "linux")
    assert api_cli._launchd_matters() is False

    monkeypatch.setattr(api_cli.sys, "platform", "darwin")
    monkeypatch.delenv("HSCC_LAUNCHD_PROBE", raising=False)
    from hscc_daemon import api_launchd
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH",
                        str(tmp_path / "absent.plist"))
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(tmp_path / "no.pid"))
    assert api_cli._launchd_matters() is False

    # a unit file on disk is enough to make it worth asking
    plist = tmp_path / "com.hscc.api.plist"
    plist.write_text("x")
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist))
    assert api_cli._launchd_matters() is True


# --------------------------------------------------------------------------- #
# `hscc api status` — the PID file is no longer the only source of truth
# --------------------------------------------------------------------------- #

class _FakeApi:
    def resolve_config(self, **kw):
        return {"host": "127.0.0.1", "port": 8787}

    def load_token(self):
        raise RuntimeError("no token (test)")


def _run_status(monkeypatch, tmp_path, pid_text=None, probe=None,
                argv=None, plist=True):
    """Run `_handle_status` with the server, the pid file and launchd stubbed."""
    monkeypatch.setattr(api_cli, "_load_api_server", lambda: _FakeApi())
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(tmp_path / "api.pid"))
    if pid_text is not None:
        (tmp_path / "api.pid").write_text(pid_text)
    from hscc_daemon import api_launchd
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH",
                        str(tmp_path / ("u.plist" if plist else "absent.plist")))
    if plist:
        (tmp_path / "u.plist").write_text("x")
    if probe is not None:
        _inject_probe(monkeypatch, probe)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_status(list(argv or []) + ["--no-qr"])
    return out.getvalue(), err.getvalue(), rc


def test_status_reports_launchd_running_when_pid_file_is_absent(
        monkeypatch, tmp_path):
    """THE bug on the card: launchd serves the API, no api.pid exists, and the
    verb said 'not running'. It must say running and name the pid."""
    out, _err, rc = _run_status(monkeypatch, tmp_path, pid_text=None,
                               probe=_probe())
    assert rc == 0
    assert "running under launchd" in out
    assert "42424" in out
    assert "not running" not in out


def test_status_shows_the_supervisor_row_always(monkeypatch, tmp_path):
    """A `hscc api start` process beside a loaded unit is the orphan class
    doctor_supervision hunts: the operator has to be able to SEE both."""
    out, _err, _rc = _run_status(monkeypatch, tmp_path,
                                 pid_text=str(os.getpid()), probe=_probe())
    assert f"HSCC API is running (PID {os.getpid()})" in out
    assert "Supervisor:" in out and "com.hscc.api" in out


def test_status_reports_throttled_unit_honestly(monkeypatch, tmp_path):
    """Loaded, no pid: a --tailscale unit whose bind refused at boot. Neither
    'running' (it is not serving) nor 'not running' (the unit IS installed and
    will retry) — report launchd's own word."""
    out, _err, _rc = _run_status(monkeypatch, tmp_path, pid_text=None,
                                 probe=_probe(out=PRINT_LOADED_NO_PID))
    assert "loaded, no process" in out
    assert "state: not running" in out


def test_status_unchanged_when_no_unit_installed(monkeypatch, tmp_path):
    """No unit file, no pid file: exactly the pre-card answer, and no probe."""
    p = _probe()
    out, _err, _rc = _run_status(monkeypatch, tmp_path, pid_text=None,
                                 probe=p, plist=False)
    assert "not running" in out
    assert p.calls == []           # the fork never happened


def test_status_never_prints_the_token_under_launchd(monkeypatch, tmp_path):
    """The token-never-leaked guarantee is unchanged by the launchd row."""
    class _Tok(_FakeApi):
        def load_token(self):
            return "SUPERSECRETTOKEN"
    monkeypatch.setattr(api_cli, "_load_api_server", lambda: _Tok())
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(tmp_path / "api.pid"))
    from hscc_daemon import api_launchd
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(tmp_path / "u.plist"))
    (tmp_path / "u.plist").write_text("x")
    _inject_probe(monkeypatch, _probe())
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        api_cli._handle_status([])
    assert "SUPERSECRETTOKEN" not in out and "SUPERSECRETTOKEN" not in err


# --------------------------------------------------------------------------- #
# `hscc api stop` — bootout, never a resurrectable SIGTERM
# --------------------------------------------------------------------------- #

def _run_stop(monkeypatch, tmp_path, pid_text=None, probe=None,
              bootout_rc=0, bootout_out="", gone=True):
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(tmp_path / "api.pid"))
    if pid_text is not None:
        (tmp_path / "api.pid").write_text(pid_text)
    from hscc_daemon import api_launchd
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(tmp_path / "u.plist"))
    (tmp_path / "u.plist").write_text("x")
    _inject_probe(monkeypatch, probe if probe is not None else _probe())
    monkeypatch.setattr(api_cli.daemon_ops, "log", lambda *a, **k: None)
    kill_calls = []
    monkeypatch.setattr(api_cli.os, "kill",
                        lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr(api_cli, "_wait_for_pid_gone", lambda pid, **kw: gone)
    bootouts = []

    def run(argv, timeout=10):
        bootouts.append([str(a) for a in argv])
        return bootout_rc, bootout_out
    monkeypatch.setattr(api_cli, "_launchctl_run", run)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_stop([])
    return out.getvalue(), err.getvalue(), rc, kill_calls, bootouts


def test_stop_boots_the_unit_out_and_never_signals_it(monkeypatch, tmp_path):
    """The core of the card: the pid file names the launchd-owned process, so
    SIGTERM-ing it is a stop that silently un-stops (KeepAlive respins it on
    the throttle). bootout is the only stop that sticks — and os.kill must
    never be called at all."""
    out, err, rc, kills, bootouts = _run_stop(
        monkeypatch, tmp_path, pid_text="42424")
    assert rc == 0
    assert _signals(kills) == [], f"stop must not signal a supervised pid: {kills}"
    assert bootouts == [["launchctl", "bootout",
                         f"gui/{os.getuid()}/com.hscc.api"]]
    assert "booted out" in out


def test_stop_states_the_stop_is_session_scoped(monkeypatch, tmp_path):
    """"stopped" must not read as permanent: the plist stays on disk and
    RunAtLoad brings the API back at the next login."""
    out, _err, rc, _k, _b = _run_stop(monkeypatch, tmp_path, pid_text="42424")
    assert rc == 0
    assert "SESSION-SCOPED" in out
    assert "uninstall-service" in out


def test_stop_boots_out_even_when_launchd_has_no_pid_right_now(
        monkeypatch, tmp_path):
    """A crash-looping unit is loaded-with-no-pid most of the time. KeepAlive
    is still armed, so a SIGTERM would still be undone — bootout stays right."""
    _out, _err, rc, kills, bootouts = _run_stop(
        monkeypatch, tmp_path, pid_text="9999",
        probe=_probe(out=PRINT_LOADED_NO_PID))
    assert rc == 0
    assert _signals(kills) == []
    assert len(bootouts) == 1


def test_stop_still_sigterms_an_unsupervised_process(monkeypatch, tmp_path):
    """Regression: a plain `hscc api start` (unit not loaded) keeps the old
    SIGTERM path exactly."""
    out, _err, rc, kills, bootouts = _run_stop(
        monkeypatch, tmp_path, pid_text=str(os.getpid()),
        probe=_probe(rc=113, out="Could not find service"))
    import signal
    assert rc == 0
    assert bootouts == []
    assert _signals(kills) == [signal.SIGTERM]
    assert "stopped" in out


def test_stop_surfaces_a_bootout_failure_as_an_error(monkeypatch, tmp_path):
    """launchd refusing is NOT a stop. rc must be non-zero and the message must
    say bootout failed — never 'stopped'."""
    _out, err, rc, _k, _b = _run_stop(
        monkeypatch, tmp_path, pid_text="42424",
        bootout_rc=1, bootout_out="Boot-out failed: Operation not permitted")
    assert rc == 1
    assert "bootout failed" in err
    assert "not permitted" in err


def test_stop_treats_already_unloaded_as_success(monkeypatch, tmp_path):
    """Idempotence: bootout on a unit launchd no longer knows about is the
    state the operator asked for, so exit 0 without pretending to do work."""
    out, _err, rc, _k, _b = _run_stop(
        monkeypatch, tmp_path, pid_text="42424",
        bootout_rc=3, bootout_out="Could not find service \"com.hscc.api\" "
                                  "in domain for user gui: 501")
    assert rc == 0
    assert "already unloaded" in out


def test_stop_says_which_launchd_process_it_stopped(monkeypatch, tmp_path):
    """The pid launchd reported belongs on screen: the operator is entitled to
    know which process the bootout took down."""
    out, _err, _rc, _k, _b = _run_stop(monkeypatch, tmp_path, pid_text=None)
    assert "42424" in out


def test_stop_removes_a_stale_pid_file_after_bootout(monkeypatch, tmp_path):
    """The launchd path writes no pid file, so a stale one from an old
    `hscc api start` must not survive the bootout and make `status` claim a
    PID that is not the supervisor's."""
    pid_file = tmp_path / "api.pid"
    _out, _err, _rc, _k, _b = _run_stop(monkeypatch, tmp_path, pid_text="42424")
    assert not pid_file.exists()


# --------------------------------------------------------------------------- #
# the honesty surface: help text + install-service note
# --------------------------------------------------------------------------- #

def test_stop_help_names_bootout_and_the_session_scope(capsys):
    assert api_cli.cmd_api(["stop", "--help"]) == 0
    out = capsys.readouterr().out
    assert "launchctl bootout" in out
    assert "SESSION-SCOPED" in out
    assert "KeepAlive" in out


def test_status_help_names_the_launchctl_probe(capsys):
    assert api_cli.cmd_api(["status", "--help"]) == 0
    out = capsys.readouterr().out
    assert "launchctl print" in out


def test_install_service_no_longer_warns_that_status_looks_stopped(
        home_and_cli_env, capsys):
    """The old note apologised for an asymmetry this card closes. Keeping it
    would tell the operator a verb is blind when it is not."""
    rc = home_and_cli_env(["install-service"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "look stopped" not in out
    assert "boots it out" in out


@pytest.fixture()
def home_and_cli_env(tmp_path, monkeypatch):
    """install-service in a private HOME (same two-module pin as
    test_api_launchd._cli — bare and package imports are distinct objects)."""
    from hscc_daemon import api_launchd
    import hscc_daemon.api_launchd as pkg_launchd
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.hscc.api.plist"
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist_path))
    monkeypatch.setattr(pkg_launchd, "API_PLIST_PATH", str(plist_path))
    py = tmp_path / "hermes-agent" / "venv" / "bin" / "python3"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hscc_daemon import api_cli as cli_mod
    monkeypatch.setattr(cli_mod, "_resolve_api_dir", lambda: tmp_path / "hscc-api")
    (tmp_path / "hscc-api").mkdir(exist_ok=True)
    return cli_mod.cmd_api
