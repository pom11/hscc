"""A stale PID file must not outrank an ANSWERED supervisor row (t_88b08940).

`hscc api status` reads two sources: its own `api.pid` file, and `launchd_status()`.
When the file names a process that is gone, the old chain decided the state line from
the file FIRST (`elif os.path.exists(API_PID_FILE): status = "warn"; "stale PID file
(not running)"`), above the two branches that ask launchd. Measured before the fix,
with a canned answered row `{'available': True, 'loaded': True, 'pid': 42424,
'state': 'running'}` and a pid file naming a dead pid:

    PANEL [warn] 'stale PID file (not running)\\n
                  Supervisor:    launchd com.hscc.api loaded, pid 42424, state running\\n
                  Listening:     127.0.0.1:8787'
    rc 0

i.e. the headline the operator reads says the API is not running while two lines
below, the supervisor's own answer says it is running with a pid — and rc 0 reports
that as a clean read. The unit writes no `api.pid`, so a stale file is exactly what a
machine running a launchd-served API leaves behind; the file proves only that the
process IT recorded is gone, never that the API is down. Same false-verdict class as
the launchd pairing (t_d2245e7e), and once the in-suite probe fence (t_251cd84f) lands
it is a screen a suite case that plants a pid file cannot falsify.

Both halves are pinned here, because "never read the file as the verdict" alone would
be satisfied by a verb that always says `unknown`:

  * an ANSWERED running unit + a stale file must say "running" (case 1), and
  * an answered "no unit loaded" — or no supervisor at all — + a stale file must keep
    saying "stale PID file (not running)" (cases 4, 5): that is the plain
    `hscc api start` leftover the branch exists for.
  * the refused variant of the same combination (case 3) belongs here, not in
    `test_api_cli_unverifiable_supervisor_row_t_8bf34130.py`, which drives `status`
    with NO pid file: under a refusal the file's dead pid is still our own read and
    stays on screen, but it must not buy back the verdict the probe declined.

`stop` is pinned as the pair (case 7): its classification reads the same pid source,
and it already gives launchd's pid precedence — that precedence is load-bearing for
the KeepAlive argument, so it is asserted rather than assumed.

Driven with a CANNED supervisor row and no `launchctl` process forked. Never starts a
server, never binds a port, never touches the real `~/.hscc`.
"""
import io
import os
import signal
from contextlib import redirect_stderr, redirect_stdout

import hscc_daemon.api_cli as api_cli
from hscc_daemon import daemon_ops

#: launchd's pid in the canned rows. Not a process: no probe runs, so nothing needs
#: this to exist.
LIVE_PID = 42424

#: A pid no kernel on this fleet can hand out: above Linux's absolute `pid_max`
#: (4194304) and far above macOS's (99999), so `daemon_ops.get_pid`'s own
#: `kill(pid, 0)` classifies it dead on every runner — without the test having to
#: stub the liveness check that decides which branch runs.
DEAD_PID = 4194305

PRINT_RUNNING = """\
GuiJob: com.hscc.api
\tpath = /Users/hscc-fixture/Library/LaunchAgents/com.hscc.api.plist
\ttype = LaunchAgent
\tstate = running

\tpid = 42424
\tplist state = running
"""


class _FakeApi:
    """Just enough of api_server for `status`: config yes, token no (no QR)."""

    def resolve_config(self, **kw):
        return {"host": "127.0.0.1", "port": 8787}

    def load_token(self):
        raise RuntimeError("no token (test)")


def _answered_loaded_row():
    """A real answer from the probe: unit loaded, launchd is running it."""
    return api_cli.launchd_status(
        _run_probe=lambda argv, timeout=10: (0, PRINT_RUNNING))


def _answered_not_loaded_row():
    """A real answer of the other kind: launchctl answered, the unit is not loaded.

    This is the row that makes the stale PID file the whole truth — production
    returns it for `launchctl print` rc != 0 ("no such job").
    """
    return api_cli.launchd_status(
        _run_probe=lambda argv, timeout=10: (3, "Could not find service"))


def _no_supervisor_row():
    """No supervisor to ask on this host (launchctl cannot be run at all).

    No `refused` key: `refused` means "the probe was blocked here", this means
    "there is nothing to ask", and the verbs treat them differently on purpose.
    """
    return api_cli.launchd_status(
        _run_probe=lambda argv, timeout=10: (None, "command not found"))


def _stale_pid_file(tmp_path):
    """Plant an `api.pid` naming a pid that cannot be alive. Returns the path."""
    pid_file = tmp_path / "api.pid"
    pid_file.write_text(str(DEAD_PID))
    # The premise, asserted rather than assumed: if a runner ever did have this pid
    # alive, the case below would be testing the wrong branch and must say so.
    assert daemon_ops.get_pid(str(pid_file)) is None
    assert pid_file.exists()
    return pid_file


# --------------------------------------------------------------------------- #
# `hscc api status`
# --------------------------------------------------------------------------- #

def _drive_status(monkeypatch, tmp_path, row, *, stale_file):
    """Run the verb. Returns (stdout, stderr, rc, [(status, message), ...]).

    `make_status_panel` is wrapped, not replaced: the call records the verdict and
    the rows the handler chose (exact, render-independent) and still renders the real
    panel, so stdout can be checked as the operator sees it. `message` is the panel
    body; its FIRST line is the state line — the headline a reader takes the verdict
    from, which is exactly what this card is about.
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
    # `_launchd_matters()` is forced True because that gate, not the row, decides
    # whether the verb asks at all; leaving it to the real plist/pid-file check
    # would make the canned row conditional on disk state.
    monkeypatch.setattr(api_cli, "_launchd_matters", lambda: True)
    monkeypatch.setattr(api_cli, "launchd_status", lambda **kw: dict(row))
    if stale_file:
        _stale_pid_file(tmp_path)
    else:
        assert not (tmp_path / "api.pid").exists()
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_status(["--no-qr"])
    return out.getvalue(), err.getvalue(), rc, panels


def _headline(panels):
    """The panel's first body line — the line the operator reads as the verdict."""
    return panels[-1][1].splitlines()[0]


def test_stale_pid_file_does_not_outrank_an_answered_running_unit(
        monkeypatch, tmp_path):
    """THE fix. launchd answered `loaded, pid 42424, state running` and the pid file
    names a dead pid: the headline must be the supervisor's answer, not the file's.
    Before the fix this printed `stale PID file (not running)` as the headline, in the
    same panel that printed the live pid two lines below, with rc 0."""
    out, _err, rc, panels = _drive_status(
        monkeypatch, tmp_path, _answered_loaded_row(), stale_file=True)
    assert rc == 0
    status, message = panels[-1]
    assert status == "ok", f"a running API is not a warn: {status}"
    assert "running under launchd" in _headline(panels)
    assert "not running" not in message.lower()
    assert "not running" not in out.lower(), "the false verdict, as printed"
    assert "unknown" not in message.lower()
    assert str(LIVE_PID) in message


def test_stale_pid_file_is_still_shown_beside_the_supervisor(
        monkeypatch, tmp_path):
    """Precedence, not deletion: the stale file loses the headline, not its row. A
    leftover api.pid beside a loaded unit is the orphan class `hscc doctor` hunts, so
    the operator must still be able to SEE it."""
    _out, _err, _rc, panels = _drive_status(
        monkeypatch, tmp_path, _answered_loaded_row(), stale_file=True)
    message = panels[-1][1]
    assert "stale" in message.lower()
    assert any(line.lower().lstrip().startswith("pid file")
               for line in message.splitlines()), message
    # ...and it stays a subordinate row: the headline is still the running verdict.
    assert "running under launchd" in _headline(panels)


def test_refused_supervisor_with_a_stale_pid_file_never_says_not_running(
        monkeypatch, tmp_path):
    """The combination #765's guard does not cover (it drives `status` with no pid
    file). Under the suite fence every case that plants a pid file lands here, so the
    dead pid in that file may NOT be read back as "not running" — the verdict the
    probe just refused to reach. It is still reported, as a fact."""
    out, _err, rc, panels = _drive_status(
        monkeypatch, tmp_path, api_cli._refused_launchd_status(), stale_file=True)
    assert rc == 0                              # unknown is not an error
    status, message = panels[-1]
    assert status == "warn"
    assert "not running" not in message.lower()
    assert "not running" not in out.lower(), "the false verdict, as printed"
    assert "stale PID file" in message          # our own read, still on screen
    assert "unknown" in message.lower()
    joined = "\n".join(m for _s, m in panels)
    assert "launchd_status(_run_probe=" in joined   # and it names the seam


def test_answered_unit_not_loaded_still_reports_the_stale_pid_file(
        monkeypatch, tmp_path):
    """CONTROL half one: launchd ANSWERED that no unit is loaded, and the recorded
    process is gone — then "stale PID file (not running)" is the truth, and a verb
    that said `unknown` here (or `running`) would be wrong in a different direction."""
    out, _err, rc, panels = _drive_status(
        monkeypatch, tmp_path, _answered_not_loaded_row(), stale_file=True)
    assert rc == 0
    status, message = panels[-1]
    assert status == "warn"
    assert _headline(panels) == "stale PID file (not running)"
    assert "unknown" not in message.lower()


def test_status_without_any_supervisor_still_reports_the_stale_pid_file(
        monkeypatch, tmp_path):
    """CONTROL half two, and the reason the branch may not be deleted: a plain
    `hscc api start` process on a host where launchctl cannot be asked (Linux,
    container) is the only source there is. Same screen as before the fix."""
    _out, _err, rc, panels = _drive_status(
        monkeypatch, tmp_path, _no_supervisor_row(), stale_file=True)
    assert rc == 0
    status, _message = panels[-1]
    assert status == "warn"
    assert _headline(panels) == "stale PID file (not running)"


def test_loaded_unit_without_a_pid_outranks_the_stale_pid_file_too(
        monkeypatch, tmp_path):
    """The second supervisor branch, now above the file: loaded with no pid (a
    throttled unit, or one mid-exit) is launchd's own account of the unit and is the
    headline; the dead pid file stays a row underneath."""
    row = dict(_answered_loaded_row(), pid=None, state="throttled")
    _out, _err, _rc, panels = _drive_status(
        monkeypatch, tmp_path, row, stale_file=True)
    status, message = panels[-1]
    assert status == "warn"                     # loaded, but no process to call ok
    assert _headline(panels).startswith("launchd unit loaded, no process")
    assert "stale PID file (not running)" not in message
    assert "stale" in message                   # the file is still reported


# --------------------------------------------------------------------------- #
# `hscc api stop` — the pair, so the two verbs cannot drift again
# --------------------------------------------------------------------------- #

def _drive_stop(monkeypatch, tmp_path, row, *, stale_file):
    """Run the verb. Returns (stdout, stderr, rc, signals, bootouts, pid_file).

    `get_pid` is stubbed here (unlike the `status` driver) because this driver has to
    stub `os.kill` to keep the verb from signalling anything real, and that same stub
    would blind the real liveness check — so "the pid file names a dead process" is
    stated as a premise, from the file the case planted, instead of being asked of the
    machine. The pid FILE is planted for real: `write_stopped` removes a file, so its
    being there is what makes "must not clear a file it should leave alone" an
    assertion rather than a tautology.
    """
    pid_file = tmp_path / "api.pid"
    if stale_file:
        _stale_pid_file(tmp_path)

    def fake_get_pid(path=None):
        target = path or str(pid_file)
        try:
            pid = int(open(target).read().strip())
        except (OSError, ValueError):
            return None
        return None if pid == DEAD_PID else pid

    monkeypatch.setattr(api_cli, "API_PID_FILE", str(pid_file))
    monkeypatch.setattr(api_cli, "API_LOG_FILE", str(tmp_path / "api.log"))
    monkeypatch.setattr(api_cli.daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(api_cli.daemon_ops, "get_pid", fake_get_pid)
    signals_sent = []
    monkeypatch.setattr(api_cli.os, "kill",
                        lambda pid, sig: signals_sent.append((pid, sig)))
    bootouts = []

    def run(argv, timeout=10):
        bootouts.append([str(a) for a in argv])
        return 0, ""

    monkeypatch.setattr(api_cli, "_launchctl_run", run)
    monkeypatch.setattr(api_cli, "_wait_for_pid_gone", lambda pid, **kw: True)
    monkeypatch.setattr(api_cli, "_launchd_matters", lambda: True)
    monkeypatch.setattr(api_cli, "launchd_status", lambda **kw: dict(row))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_stop([])
    real_signals = [s for _p, s in signals_sent
                    if s in (signal.SIGTERM, signal.SIGKILL)]
    return (out.getvalue(), err.getvalue(), rc, real_signals, bootouts, pid_file)


def test_stop_acts_on_launchds_pid_and_never_signals_the_stale_one(
        monkeypatch, tmp_path):
    """The same pid source, the other verb: a stale file must not talk `stop` out of
    the bootout, and must not end up as the pid it names. If the file's pid ever won,
    the verb would signal a dead process, print "already stopped", exit 0 and leave a
    KeepAlive-supervised API serving — the silent-un-stop the pairing exists to kill."""
    out, err, rc, sigs, bootouts, pid_file = _drive_stop(
        monkeypatch, tmp_path, _answered_loaded_row(), stale_file=True)
    assert rc == 0
    assert sigs == [], f"stop must not signal a stale pid: {sigs}"
    assert bootouts == [["launchctl", "bootout",
                         f"gui/{os.getuid()}/com.hscc.api"]]
    assert str(LIVE_PID) in out, "launchd's pid is the one it acted on"
    assert str(DEAD_PID) not in out, "the stale file's pid must not be the actor"
    assert "booted out" in out
    assert not pid_file.exists()
    assert "not running" not in out.lower()
    assert err == ""
