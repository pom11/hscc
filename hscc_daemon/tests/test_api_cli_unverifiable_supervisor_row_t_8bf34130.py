"""`hscc api status` / `stop` when the supervisor row is UNVERIFIABLE (t_8bf34130).

The `launchd_status()` fence answers "the probe could not run" with
`_refused_launchd_status()` — a row that says *unknown*, not *not loaded*. The two
verbs that consume it must not turn that unknown back into a verdict:

  * `status` must not print "not running". That is the exact false answer the
    launchd pairing (t_d2245e7e) was added to kill: an API launchd has served
    since boot writes no api.pid, so a PID-file-only read calls it stopped.
    Reporting an unconsulted supervisor as "not running" re-ships that bug for
    every process the fence now fires in.
  * `stop` must FAIL CLOSED. Without the supervisor's answer it cannot tell
    whether the pid is KeepAlive-supervised, and a SIGTERM on a supervised
    process is a stop that un-stops itself on the next throttle tick. Nor may it
    print "not running" and exit 0 — that is the verdict the refusal just
    refused to make.

Both rulings are written at `launchd_status()`; this file is where they are
pinned. Both halves of the pair are here: a refusal must not produce the false
verdict, AND a real answer must still produce the true one (otherwise "never say
not running" could be satisfied by a probe that always refuses).

Everything is driven with a CANNED supervisor row — the handlers react to the row,
so that is the contract under test, and no `launchctl` process is ever forked.
Never starts a server, never binds a port, never touches the real `~/.hscc`.
"""
import io
import os
import signal
from contextlib import redirect_stderr, redirect_stdout

import hscc_daemon.api_cli as api_cli


#: A pid to name in the canned rows. Not a process: `get_pid` is stubbed so the
#: "live pid" premise does not depend on what happens to exist on the runner.
LIVE_PID = 42424

PRINT_RUNNING = """\
GuiJob: com.hscc.api
	path = /Users/dev/Library/LaunchAgents/com.hscc.api.plist
	type = LaunchAgent
	state = running

	pid = 42424
	plist state = running
"""


class _FakeApi:
    """Just enough of api_server for `status`: config yes, token no (no QR)."""

    def resolve_config(self, **kw):
        return {"host": "127.0.0.1", "port": 8787}

    def load_token(self):
        raise RuntimeError("no token (test)")


def _canned_supervisor(monkeypatch, row):
    """Hand the verbs exactly `row` as the supervisor's answer.

    `_launchd_matters()` is forced True because that gate, not the row, is what
    decides whether the verb asks at all — leaving it to the real plist/pid-file
    check would make the canned row conditional on disk state.
    """
    monkeypatch.setattr(api_cli, "_launchd_matters", lambda: True)
    monkeypatch.setattr(api_cli, "launchd_status", lambda **kw: dict(row))


def _refused_row():
    """The refusal production actually returns (not a shape invented here)."""
    return api_cli._refused_launchd_status()


def _loaded_row():
    """A real answer: launchd has the unit loaded and is running it."""
    return api_cli.launchd_status(
        _run_probe=lambda argv, timeout=10: (0, PRINT_RUNNING))


# --------------------------------------------------------------------------- #
# the row itself: `refused` is not `available: False`
# --------------------------------------------------------------------------- #

def test_refusal_keeps_its_own_key_and_stays_unavailable():
    """`refused` = "the row could not be checked"; `available: False` = "launchctl
    cannot be asked on this host". The verbs branch on them differently, so the
    canned row this file drives must stay what production returns."""
    row = _refused_row()
    assert row["refused"] is True
    assert row["available"] is False and row["loaded"] is False
    assert row["pid"] is None
    # The two are not interchangeable: an unavailable-but-answered host has no
    # `refused` key at all, and the verbs must keep trusting it.
    unanswered = api_cli.launchd_status(
        _run_probe=lambda argv, timeout=10: (None, "command not found"))
    assert unanswered["available"] is False and "refused" not in unanswered


# --------------------------------------------------------------------------- #
# `hscc api status` — an unconsulted supervisor is not "not running"
# --------------------------------------------------------------------------- #

def _drive_status(monkeypatch, tmp_path, row, pid_text=None):
    """Run the verb. Returns (stdout, stderr, rc, [(status, message), ...]).

    `make_status_panel` is wrapped rather than replaced: the call records the
    verdict and the rows the handler chose (exact, render-independent) and still
    renders the real panel, so stdout can be checked as the operator sees it.
    """
    real_make = api_cli.theme.make_status_panel
    panels = []

    def record(message, status="ok", **kw):
        panels.append((status, message))
        return real_make(message, status=status, **kw)

    monkeypatch.setattr(api_cli, "_load_api_server", lambda: _FakeApi())
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(tmp_path / "api.pid"))
    monkeypatch.setattr(api_cli, "API_LOG_FILE", str(tmp_path / "api.log"))
    monkeypatch.setattr(api_cli.theme, "make_status_panel", record)
    if pid_text is not None:
        (tmp_path / "api.pid").write_text(pid_text)
    _canned_supervisor(monkeypatch, row)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_status(["--no-qr"])
    return out.getvalue(), err.getvalue(), rc, panels


def test_status_does_not_say_not_running_when_the_probe_refused(
        monkeypatch, tmp_path):
    """THE fix. No pid file + an unconsultable supervisor = the verb knows nothing,
    and "not running" is a claim it just said it cannot make."""
    out, _err, rc, panels = _drive_status(monkeypatch, tmp_path, _refused_row())
    assert rc == 0                                  # unreadable != an error
    assert panels, "the verb must still report"
    status, message = panels[-1]
    assert "not running" not in message.lower()
    assert "not running" not in out.lower()
    assert "unknown" in message.lower()             # says what it actually knows
    assert status in ("ok", "warn")                 # never an alarm, never "error"


def test_status_refusal_still_names_the_seam(monkeypatch, tmp_path):
    """Never a silent pass: the screen must show WHY the supervisor is missing and
    how to inject it, so an in-suite drive cannot quietly read as "stopped"."""
    out, _err, _rc, panels = _drive_status(monkeypatch, tmp_path, _refused_row())
    joined = "\n".join(m for _s, m in panels)
    assert "refused" in joined and "refused" in out
    assert "launchd_status(_run_probe=" in joined


def test_status_still_reports_launchd_running_when_the_probe_answers(
        monkeypatch, tmp_path):
    """Control half, and the reason the fix may not be "always say unknown": a
    loaded unit with a pid and no pid file IS running (t_d2245e7e)."""
    out, _err, rc, panels = _drive_status(monkeypatch, tmp_path, _loaded_row())
    assert rc == 0
    _status, message = panels[-1]
    assert "running under launchd" in message
    assert str(LIVE_PID) in message
    assert "unknown" not in message.lower()


# --------------------------------------------------------------------------- #
# `hscc api stop` — a refusal is not a licence to guess
# --------------------------------------------------------------------------- #

def _drive_stop(monkeypatch, tmp_path, row, live_pid=None):
    """Run the verb. Returns (stdout, stderr, rc, signals, bootouts, pid_file).

    `get_pid` is stubbed so "there is a live process" is a premise of the test
    rather than a property of the machine running it, and the pid FILE is planted
    to match: `write_stopped` removes a file, so the file being there is what
    makes "must not write 'stopped' over a live process" an assertion instead of
    a tautology.
    """
    pid_file = tmp_path / "api.pid"
    if live_pid is not None:
        pid_file.write_text(str(live_pid))
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(pid_file))
    monkeypatch.setattr(api_cli, "API_LOG_FILE", str(tmp_path / "api.log"))
    monkeypatch.setattr(api_cli.daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(api_cli.daemon_ops, "get_pid",
                        lambda pid_file=None: live_pid)
    signals_sent = []
    monkeypatch.setattr(api_cli.os, "kill",
                        lambda pid, sig: signals_sent.append((pid, sig)))
    bootouts = []

    def run(argv, timeout=10):
        bootouts.append([str(a) for a in argv])
        return 0, ""

    monkeypatch.setattr(api_cli, "_launchctl_run", run)
    monkeypatch.setattr(api_cli, "_wait_for_pid_gone", lambda pid, **kw: True)
    _canned_supervisor(monkeypatch, row)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_stop([])
    real_signals = [s for _p, s in signals_sent
                    if s in (signal.SIGTERM, signal.SIGKILL)]
    return (out.getvalue(), err.getvalue(), rc, real_signals, bootouts,
            pid_file)


def test_stop_refuses_to_signal_a_pid_it_cannot_classify(
        monkeypatch, tmp_path):
    """A live pid plus an unconsultable supervisor is the dangerous pair: the old
    code SIGTERMs it, KeepAlive respawns it, and the operator is told "stopped".
    Failing closed costs the operator one manual command; guessing costs a daemon
    that is running while everything says it is not."""
    out, err, rc, sigs, bootouts, pid_file = _drive_stop(
        monkeypatch, tmp_path, _refused_row(), live_pid=LIVE_PID)
    assert sigs == [], f"stop must not signal a pid it cannot classify: {sigs}"
    assert rc != 0, "a stop that could not act must not report success"
    assert bootouts == []               # and not boot out a unit it cannot see
    assert "refused" in err
    assert "KeepAlive" in err           # says WHICH risk it is declining
    assert "launchd_status(_run_probe=" in err
    assert "stopped" not in out.lower()
    assert pid_file.exists(), "must not write 'stopped' over a live process"


def test_stop_does_not_claim_not_running_when_the_probe_refused(
        monkeypatch, tmp_path):
    """The other half: with no pid to signal either, the old code printed "HSCC
    API is not running" and exited 0 — a verdict, from a refusal. Not knowing is
    not knowing-that-it-is-not-running."""
    out, err, rc, sigs, _bootouts, _pid_file = _drive_stop(
        monkeypatch, tmp_path, _refused_row(), live_pid=None)
    assert rc != 0
    assert "not running" not in out.lower()
    assert sigs == []
    assert "refused" in err


def test_stop_still_boots_out_a_loaded_unit_when_the_probe_answers(
        monkeypatch, tmp_path):
    """Control half: a refusal is the only case that changes. Loaded + a real
    answer keeps the bootout, the SIGTERM ban, and exit 0."""
    out, _err, rc, sigs, bootouts, pid_file = _drive_stop(
        monkeypatch, tmp_path, _loaded_row(), live_pid=LIVE_PID)
    assert rc == 0
    assert sigs == []
    assert bootouts == [["launchctl", "bootout",
                         f"gui/{os.getuid()}/com.hscc.api"]]
    assert "booted out" in out
    assert not pid_file.exists()
