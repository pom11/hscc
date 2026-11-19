"""M4 — `hscc main` owns the main orchestrator's tmux pane.

Hermetic by construction: every test injects a fake repl module (pane
discovery + busy detection) and/or a fake tmux runner, so no suite run can
list, create or type into the operator's real tmux server. The fake runner
records argv, which is how "never kill another pane/window" and "never
switch the operator's window" are pinned.
"""

import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import main_cli as mc


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeRepl:
    """Stand-in for hscc-api/repl_inject's discovery + busy seam."""

    def __init__(self, panes=(), busy=False, busy_error=None):
        self.panes = list(panes)
        self.busy = busy
        self.busy_error = busy_error
        self.busy_calls = []

    def hermes_panes(self):
        return [dict(p) for p in self.panes]

    def is_busy(self, pane):
        self.busy_calls.append(pane)
        if self.busy_error is not None:
            raise self.busy_error
        return self.busy


def _pane(pane="%1", target="work:1.0", profile="general"):
    return {"pane": pane, "target": target, "tty": "/dev/ttys001",
            "profile": profile, "cmd": f"hermes -p {profile} chat"}


class FakeRunner:
    """Fake tmux CLI. ``sessions`` empty/None => no tmux server."""

    def __init__(self, sessions=None, new_pane="%9", fail=None):
        self.sessions = sessions
        self.new_pane = new_pane
        self.fail = fail or set()
        self.calls = []

    def __call__(self, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        verb = argv[0]
        if verb in self.fail:
            return 1, "", f"{verb} failed"
        if verb == "list-sessions":
            if not self.sessions:
                return 1, "", "no server running"
            lines = [f"{s['name']}\t{1 if s.get('attached') else 0}\t"
                     f"{s.get('activity', 0)}" for s in self.sessions]
            return 0, "\n".join(lines) + "\n", ""
        if verb == "display-message":
            return 0, "current-session\n", ""
        if verb in ("new-window", "new-session"):
            return 0, self.new_pane + "\n", ""
        return 0, "", ""

    def verbs(self):
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def _pin_state(tmp_path, monkeypatch):
    """Never read the operator's stored main name or tmux env."""
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / "hscc-state"))
    monkeypatch.delenv("TMUX", raising=False)
    home = tmp_path / "hermes"
    (home / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    binary = home / "hermes-agent" / "venv" / "bin" / "hermes"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # The package-wide hermetic floor pins a DIFFERENT kanban root; align it
    # with this fixture's target home so start()'s fail-closed guard is about
    # the thing each test names, not an artefact of the floor.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_BIN", raising=False)
    return binary


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #

def test_start_reuses_the_single_running_pane():
    repl = FakeRepl([_pane()])
    runner = FakeRunner(sessions=[{"name": "work", "attached": True}])
    res = mc.start(repl=repl, runner=runner)
    assert res["ok"] is True and res["action"] == "reused"
    assert res["pane"] == "%1" and res["target"] == "work:1.0"
    assert res["name"] == "general" and res["board"] == "general"
    assert runner.calls == [], "a running pane must be reused with zero writes"


def test_start_creates_a_window_named_after_the_profile():
    repl = FakeRepl([])
    runner = FakeRunner(sessions=[{"name": "work", "attached": True,
                                   "activity": 5}])
    res = mc.start(repl=repl, runner=runner)
    assert res["ok"] is True and res["action"] == "created-window"
    assert res["session"] == "work" and res["pane"] == "%9"
    new = [c for c in runner.calls if c[0] == "new-window"][0]
    assert new[1] == "-d", "must not switch the operator's current window"
    assert new[new.index("-t") + 1] == "=work:", \
        "the trailing colon pins the SESSION; `=work` would be a window target"
    assert new[new.index("-n") + 1] == "general"
    command = new[new.index("--") + 1:]
    assert command[-3:] == ["-p", "general", "chat"]
    assert all("kill" not in v and v not in ("select-window", "select-pane")
               for v in runner.verbs())


def test_start_targets_the_session_named_zero_not_its_window_zero():
    """The operator's session is literally named `0` (live 2.16.0 defect).

    Without the trailing colon tmux reads `=0` as WINDOW 0, so `new-window`
    fails with "index 0 in use" and bootstrap never starts the pane.
    """
    repl = FakeRepl([])
    runner = FakeRunner(sessions=[{"name": "0", "attached": True}])
    res = mc.start(repl=repl, runner=runner)
    assert res["ok"] is True and res["action"] == "created-window"
    assert res["session"] == "0"
    new = [c for c in runner.calls if c[0] == "new-window"][0]
    assert new[new.index("-t") + 1] == "=0:"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_start_creates_the_window_in_a_real_zero_named_session(tmp_path):
    """The fake runner cannot model tmux's target syntax, so use a real server.

    Throwaway socket only (`tmux -L`), never the operator's default server: a
    session named `0` whose window 0 already exists must get a NEW window at
    the next free index instead of "index 0 in use".
    """
    socket = f"hscc-test-{tmp_path.name[-12:]}"

    def tmux(*args):
        return subprocess.run(["tmux", "-L", socket, *args],
                              capture_output=True, text=True)

    binary = tmp_path / "fake-hermes"
    binary.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    binary.chmod(0o755)
    try:
        created = tmux("new-session", "-d", "-s", "0", "-n", "operator",
                       "sleep", "30")
        assert created.returncode == 0, created.stderr

        def runner(argv):
            cp = tmux(*[str(a) for a in argv])
            return cp.returncode, cp.stdout or "", cp.stderr or ""

        res = mc.start(repl=FakeRepl([]), runner=runner,
                       hermes_bin=str(binary))
        assert res["ok"] is True, res
        assert res["action"] == "created-window" and res["session"] == "0"
        assert res["pane"].startswith("%")
        windows = tmux("list-windows", "-t", "=0:", "-F",
                       "#{window_index} #{window_name}")
        assert windows.returncode == 0, windows.stderr
        assert "1 general" in windows.stdout, windows.stdout
    finally:
        tmux("kill-server")


def test_start_uses_the_current_tmux_session_when_inside_one(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,123,0")
    repl = FakeRepl([])
    runner = FakeRunner(sessions=[
        {"name": "older", "attached": True, "activity": 99},
        {"name": "current-session", "attached": True, "activity": 1},
    ])
    res = mc.start(repl=repl, runner=runner)
    assert res["session"] == "current-session"
    assert "display-message" in runner.verbs()


def test_start_creates_a_detached_session_when_no_server():
    repl = FakeRepl([])
    runner = FakeRunner(sessions=None)
    res = mc.start(repl=repl, runner=runner)
    assert res["ok"] is True and res["action"] == "created-session"
    assert res["session"] == "general"
    new = [c for c in runner.calls if c[0] == "new-session"][0]
    assert new[new.index("-d")] == "-d"
    assert new[new.index("-s") + 1] == "general"
    assert new[new.index("-n") + 1] == "general"
    assert new[new.index("--") + 1:][-3:] == ["-p", "general", "chat"]


def test_start_refuses_ambiguous_panes_and_kills_nothing():
    repl = FakeRepl([_pane("%1", "work:1.0"), _pane("%2", "work:2.0")])
    runner = FakeRunner(sessions=[{"name": "work", "attached": True}])
    res = mc.start(repl=repl, runner=runner)
    assert res["ok"] is False and res["action"] == "ambiguous"
    assert "%1" in res["detail"] and "%2" in res["detail"]
    assert runner.calls == []
    assert all("kill" not in v for v in runner.verbs())


def test_start_uses_the_stored_main_name(tmp_path):
    (tmp_path / "hscc-state").mkdir()
    (tmp_path / "hscc-state" / "main-orchestrator.json").write_text(
        json.dumps({"name": "ops"}))
    repl = FakeRepl([])
    runner = FakeRunner(sessions=[{"name": "work", "attached": True}])
    res = mc.start(repl=repl, runner=runner, hermes_bin=sys.executable)
    assert res["name"] == "ops" and res["profile"] == "ops"
    assert res["board"] == "ops"
    new = [c for c in runner.calls if c[0] == "new-window"][0]
    assert new[new.index("-n") + 1] == "ops"
    assert new[-3:] == ["-p", "ops", "chat"]


def test_start_reports_missing_hermes_without_touching_tmux(monkeypatch):
    monkeypatch.setattr(mc, "find_hermes_bin", lambda explicit=None: None)
    runner = FakeRunner(sessions=[{"name": "work", "attached": True}])
    res = mc.start(repl=FakeRepl([]), runner=runner)
    assert res["ok"] is False and res["action"] == "missing-hermes"
    assert runner.calls == []


def test_start_refuses_a_kanban_root_outside_the_target_home(tmp_path,
                                                             monkeypatch):
    """The other find_hermes_bin path must fail closed on a foreign board root.

    Starting the REPL is harmless by itself, but the main orchestrator it
    launches would read and mutate whatever board that root holds.
    """
    foreign = tmp_path / "foreign-kanban"
    foreign.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(foreign))
    runner = FakeRunner(sessions=[{"name": "work", "attached": True}])
    res = mc.start(repl=FakeRepl([]), runner=runner)
    assert res["ok"] is False and res["action"] == "kanban-root-refused"
    assert str(foreign) in res["detail"]
    assert runner.calls == [], "no tmux command may run after the refusal"


def test_start_reports_missing_repl(monkeypatch):
    monkeypatch.setattr(mc, "load_repl_inject", lambda: None)
    res = mc.start(runner=FakeRunner())
    assert res["ok"] is False and res["action"] == "discovery-failed"
    assert "repl_inject" in res["detail"]


def test_start_reports_a_failed_tmux_create():
    runner = FakeRunner(sessions=[{"name": "work", "attached": True}],
                        fail={"new-window"})
    res = mc.start(repl=FakeRepl([]), runner=runner)
    assert res["ok"] is False and res["action"] == "create-failed"
    assert "new-window failed" in res["detail"]


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #

def test_status_busy_uses_orch_injects_busy_detection():
    repl = FakeRepl([_pane("%3", "work:2.1")], busy=True)
    res = mc.status(repl=repl)
    assert res["ok"] is True and res["state"] == "busy" and res["busy"] is True
    assert res["running"] is True and res["pane"] == "%3"
    assert res["target"] == "work:2.1"
    assert res["name"] == "general" and res["board"] == "general"
    assert repl.busy_calls == ["%3"]


def test_status_idle():
    res = mc.status(repl=FakeRepl([_pane()], busy=False))
    assert res["state"] == "idle" and res["busy"] is False


def test_status_not_running_is_honest_and_read_only():
    res = mc.status(repl=FakeRepl([]))
    assert res["ok"] is True and res["state"] == "not-running"
    assert res["running"] is False and res["busy"] is None
    assert res["pane"] is None


def test_status_ambiguous_never_guesses_a_pane():
    res = mc.status(repl=FakeRepl([_pane("%1"), _pane("%2")]))
    assert res["state"] == "ambiguous" and res["pane"] is None
    assert res["panes"] == ["work:1.0", "work:1.0"]


def test_status_unknown_when_the_screen_cannot_be_read():
    res = mc.status(repl=FakeRepl([_pane()], busy_error=RuntimeError("tmux gone")))
    assert res["state"] == "unknown" and res["busy"] is None
    assert "tmux gone" in res["detail"]


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #

def _cli(argv):
    out = io.StringIO()
    rc = None
    with redirect_stdout(out):
        rc = mc.cmd_main(argv)
    return rc, out.getvalue()


def test_cli_help_is_inert(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("help must not start or inspect a pane")
    monkeypatch.setattr(mc, "start", boom)
    monkeypatch.setattr(mc, "status", boom)
    for argv in ([], ["--help"], ["-h"], ["start", "--help"],
                 ["status", "-h"], ["bogus", "--help"]):
        rc, out = _cli(argv)
        assert rc == 0, (argv, out)
        assert "hscc main" in out


def test_cli_status_json_is_the_machine_contract(monkeypatch):
    monkeypatch.setattr(mc, "status", lambda: {
        "ok": True, "name": "general", "profile": "general",
        "board": "general", "state": "idle", "running": True, "busy": False,
        "pane": "%3", "target": "work:2.1", "panes": ["work:2.1"],
        "detail": "idle"})
    rc, out = _cli(["status", "--json"])
    assert rc == 0
    payload = json.loads(out)
    assert payload["name"] == "general" and payload["board"] == "general"
    assert payload["pane"] == "%3" and payload["busy"] is False
    assert payload["state"] == "idle"


def test_cli_start_human_view(monkeypatch):
    monkeypatch.setattr(mc, "start", lambda: {
        "ok": True, "action": "created-window", "name": "general",
        "profile": "general", "board": "general", "pane": "%9",
        "target": None, "session": "work", "command": [], "detail": "started"})
    rc, out = _cli(["start"])
    assert rc == 0
    assert "main orchestrator: general started" in out
    assert "tmux window general in session work" in out


def test_cli_failure_exits_nonzero_with_the_fix(monkeypatch):
    monkeypatch.setattr(mc, "start", lambda: {
        "ok": False, "action": "ambiguous", "name": "general",
        "profile": "general", "board": "general", "pane": None,
        "target": None, "session": None, "command": None,
        "detail": "2 tmux panes run profile 'general'"})
    rc, out = _cli(["start"])
    assert rc == 1
    assert "error: 2 tmux panes" in out


def test_unified_cli_dispatches_main(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "cmd_main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["hscc", "main", "status", "--json"])
    with pytest.raises(SystemExit) as exc:
        unified.main()
    assert exc.value.code == 0
    assert calls == [["status", "--json"]]
