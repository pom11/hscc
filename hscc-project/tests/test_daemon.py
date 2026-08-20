"""Tests for `flightdeck daemon` — the monitoring/logging daemon.

Covers (per the card's VERIFY list):
  * each check stream in isolation (mocked reads — no real board/git access),
  * the daemon loop calling each stream at its configured interval,
  * `daemon status` reporting accurately,
  * NO code path in the daemon mutating state (grep for mutation terms).

All board reads are stubbed via the injectable ``_cards`` / ``_projects`` /
``_run`` / ``_now`` seams; state writes go to a per-test tmp dir via
monkeypatched ``STATE_DIR``/paths, so nothing touches a real board, repo,
network, or the operator's ``~/.flightdeck``.
"""

from __future__ import annotations

import argparse
import subprocess
import threading
import time

import pytest

from flightdeck.commands import daemon as cmd
from flightdeck.core import daemon as d
from flightdeck.core import registry


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _card(board, status="running", workspace=None):
    """A minimal flightdeck card dict (enough for in_flight/attribution).

    ``workspace`` defaults to a path under the project repo so a card on a
    registered board attributes cleanly (never a false orphan). Pass a different
    ``workspace`` (or None) to exercise unattributed/orphan cases.
    """
    return {
        "id": "t_x",
        "board": board,
        "status": status,
        "workspace_path": workspace
        if workspace is not None
        else "/tmp/fake-flightdeck/.worktrees/t_x",
    }


def _project(**over):
    """A registry Project with safe defaults; override any field."""
    fields = dict(
        name="flightdeck",
        repo="/tmp/fake-flightdeck",
        board="flightdeck",
        installed_version_cmd="cat VERSION",
    )
    fields.update(over)
    return registry.Project(**fields)


def _cp(returncode=0, stdout="", stderr=""):
    """A fake subprocess result (the shape _run callers expect)."""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class _FakeRun:
    """A scriptable ``_run(cmd, cwd)`` for version-check tests.

    Handles both forms the check emits: a string command (the
    ``installed_version_cmd``) and a list git command (``ls-remote``). Tracks
    how many times ``ls-remote`` fired so rate-limiting is directly assertable.
    """

    def __init__(self, ls_remote_out):
        self.ls_remote_out = ls_remote_out
        self.ls_remote_calls = 0

    def __call__(self, cmd, cwd):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            if "ls-remote" in cmd:
                self.ls_remote_calls += 1
                return _cp(0, self.ls_remote_out)
            return _cp(0)
        # string command -> the installed_version_cmd
        return _cp(0, "0.6.0")


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """Point daemon state/log/pid at a per-test tmp dir (never the real home)."""
    monkeypatch.setattr(d, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(d, "LOG_FILE", str(tmp_path / "daemon.log"))
    monkeypatch.setattr(d, "PID_FILE", str(tmp_path / "daemon.pid"))
    return tmp_path


def _projects_seam(monkeypatch, projects):
    """Make ``cmd.registry.load_registry`` return ``projects`` (restored after)."""
    monkeypatch.setattr(cmd.registry, "load_registry", lambda _path=None: projects)


# --------------------------------------------------------------------------- #
# check_fleet
# --------------------------------------------------------------------------- #


def test_fleet_ok_under_ceiling(isolated_state):
    res = cmd.check_fleet("reg.yaml", max_fleet=5, _cards=lambda: [_card("a"), _card("b")])
    assert res["ok"] is True
    assert res["in_flight"] == 2
    assert res["per_board"] == {"a": 1, "b": 1}


def test_fleet_over_ceiling_flags(isolated_state):
    res = cmd.check_fleet(
        "reg.yaml", max_fleet=1,
        _cards=lambda: [_card("a"), _card("b"), _card("c")],
    )
    assert res["ok"] is False
    assert res["in_flight"] == 3
    assert res["ceiling"] == 1


def test_fleet_ignores_nonactive(isolated_state):
    # review/blocked/todo cards are NOT in flight (a worker is not engaged).
    res = cmd.check_fleet(
        "reg.yaml", max_fleet=1,
        _cards=lambda: [
            _card("a", status="review"),
            _card("a", status="todo"),
            _card("a", status="blocked"),
        ],
    )
    assert res["ok"] is True
    assert res["in_flight"] == 0


def test_fleet_reads_cap_from_config(isolated_state, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("kanban:\n  max_in_progress: 7\n")
    res = cmd.check_fleet("reg.yaml", _cards=lambda: [], _config=str(cfg))
    assert res["cap"] == 7


def test_fleet_unreadable_boards(isolated_state):
    def boom():
        raise cmd.kanban.KanbanError("lock")

    res = cmd.check_fleet("reg.yaml", _cards=boom)
    assert res["ok"] is False
    assert res["in_flight"] == -1


# --------------------------------------------------------------------------- #
# check_freshness
# --------------------------------------------------------------------------- #


def test_freshness_records_and_reads(isolated_state):
    res = cmd.check_freshness(
        "reg.yaml", threshold=3600,
        _cards=lambda: [_card("a"), _card("b")], _now=lambda: 1000.0,
    )
    assert res["ok"] is True
    assert res["boards"] == 2
    assert res["stale"] == []
    assert res["last_read"] == {"a": 1000.0, "b": 1000.0}


def test_freshness_flags_stale_board_after_threshold(isolated_state):
    # First tick records board 'a' at t=0.
    cmd.check_freshness(
        "reg.yaml", threshold=100, _cards=lambda: [_card("a")], _now=lambda: 0.0,
    )
    # Later 'a' is no longer read and ages past the threshold.
    res = cmd.check_freshness(
        "reg.yaml", threshold=100, _cards=lambda: [_card("b")], _now=lambda: 500.0,
    )
    assert res["ok"] is False
    assert "a" in res["stale"]
    assert res["last_read"]["a"] == 0.0


def test_freshness_unreadable(isolated_state):
    def boom():
        raise cmd.kanban.KanbanError("lock")

    res = cmd.check_freshness("reg.yaml", _cards=boom)
    assert res["ok"] is False


# --------------------------------------------------------------------------- #
# check_orphans
# --------------------------------------------------------------------------- #


def test_orphans_none_when_all_registered(isolated_state):
    cards = [_card("flightdeck", status="running")]
    res = cmd.check_orphans("reg.yaml", _cards=lambda: cards, _projects=lambda: [_project()])
    assert res["ok"] is True
    assert res["orphan_boards"] == {}
    assert res["unmanaged_cards"] == 0


def test_orphans_flags_unregistered_board(isolated_state):
    # 'legacy-board' has no registry mapping -> orphan.
    cards = [
        _card("flightdeck", status="running"),
        _card("legacy-board", status="review"),
    ]
    res = cmd.check_orphans("reg.yaml", _cards=lambda: cards, _projects=lambda: [_project()])
    assert res["ok"] is False
    assert res["orphan_boards"] == {"legacy-board": 1}
    assert res["unmanaged_cards"] == 1


def test_orphans_unreadable(isolated_state):
    def boom():
        raise cmd.kanban.KanbanError("lock")

    res = cmd.check_orphans("reg.yaml", _cards=boom, _projects=lambda: [_project()])
    assert res["ok"] is False


# --------------------------------------------------------------------------- #
# check_version (rate-limited, injectable _run/_now/_cache)
# --------------------------------------------------------------------------- #


def test_version_up_to_date(isolated_state, monkeypatch, tmp_path):
    _projects_seam(monkeypatch, [_project()])
    fake = _FakeRun("abc123\trefs/tags/v0.6.0\n")
    res = cmd.check_version(
        "reg.yaml", _run=fake, _now=lambda: 1000.0, _cache=str(tmp_path / "c.yaml")
    )
    assert res["ok"] is True
    assert res["state"] == "OK"
    assert res["installed"] == "0.6.0"
    assert res["remote"] == "v0.6.0"
    assert fake.ls_remote_calls == 1


def test_version_drifted(isolated_state, monkeypatch, tmp_path):
    _projects_seam(monkeypatch, [_project()])
    fake = _FakeRun("abc123\trefs/tags/v0.7.0\n")
    res = cmd.check_version(
        "reg.yaml", _run=fake, _now=lambda: 1000.0, _cache=str(tmp_path / "c.yaml")
    )
    assert res["ok"] is False
    assert res["state"] == "DRIFTED"
    assert "0.7.0" in res["message"]


def test_version_rate_limited_uses_cache(isolated_state, monkeypatch, tmp_path):
    _projects_seam(monkeypatch, [_project()])
    fake = _FakeRun("abc123\trefs/tags/v0.7.0\n")
    cache = str(tmp_path / "c.yaml")
    cmd.check_version("reg.yaml", _run=fake, _now=lambda: 1000.0, _cache=cache)
    assert fake.ls_remote_calls == 1
    # Second call within TTL reads the cache; ls-remote does NOT fire again.
    res = cmd.check_version("reg.yaml", _run=fake, _now=lambda: 2000.0, _cache=cache)
    assert fake.ls_remote_calls == 1
    assert res["cache_hit"] is True
    assert res["state"] == "DRIFTED"


def test_version_no_self_entry(isolated_state, monkeypatch):
    # Empty registry -> NO_ENTRY, not an error. Patch load_registry to [].
    _projects_seam(monkeypatch, [])
    res = cmd.check_version("reg.yaml")
    assert res["state"] == "NO_ENTRY"
    assert res["ok"] is True


def test_version_unknown_when_cmd_missing(isolated_state, monkeypatch):
    # A 'flightdeck' project with no installed_version_cmd -> UNKNOWN.
    _projects_seam(monkeypatch, [_project(installed_version_cmd=None)])
    res = cmd.check_version("reg.yaml")
    assert res["state"] == "UNKNOWN"
    assert res["ok"] is True


# --------------------------------------------------------------------------- #
# run loop — calls each stream at its configured interval, stops cooperatively
# --------------------------------------------------------------------------- #


def test_run_daemon_loop_calls_each_stream(isolated_state):
    calls: dict[str, int] = {}

    def mk(name):
        def check(reg):
            calls[name] = calls.get(name, 0) + 1
            return {"ok": True, "message": f"{name} ok"}
        return check

    checks = {name: mk(name) for name in cmd.STREAM_NAMES}
    stop = threading.Event()
    t = threading.Thread(
        target=d.run_daemon_loop,
        args=("reg.yaml", checks),
        kwargs={"intervals": {n: 0 for n in checks}, "stop_event": stop},
    )
    t.start()
    time.sleep(0.6)  # let each interval-0 stream run at least one tick
    stop.set()
    t.join(timeout=5)

    assert not t.is_alive(), "loop did not exit after stop_event"
    for name in cmd.STREAM_NAMES:
        assert calls[name] >= 1, f"{name} was never called by the loop"


def test_run_periodic_honors_interval(isolated_state):
    """run_periodic runs the check once immediately, then guards with _sleep."""
    ticks: list[str] = []

    def check(reg):
        ticks.append("tick")
        return {"ok": True, "message": "ok"}

    stop = threading.Event()
    sleeps: list[int] = []

    def fake_sleep(n):
        sleeps.append(n)
        if len(sleeps) >= 2:
            stop.set()  # stop after two waits so the thread ends

    t = threading.Thread(
        target=d.run_periodic,
        args=("fleet", "reg.yaml", stop),
        kwargs={"check_fn": check, "interval": 30, "_sleep": fake_sleep},
    )
    t.start()
    t.join(timeout=5)

    assert ticks == ["tick", "tick"]  # one immediately, one after the interval
    assert sleeps == [30, 30]         # the configured interval was honored


def test_run_one_persists_failing_state(isolated_state):
    res = d._run_one("fleet", "reg.yaml", lambda reg: {"ok": False, "message": "boom"})
    assert res["ok"] is False
    assert d.read_state("fleet")["ok"] is False


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_reports_stopped_when_no_pid(isolated_state, capsys):
    code = cmd.cmd_status(argparse.Namespace(), "reg.yaml")
    assert code == 0
    out = capsys.readouterr().out
    assert "STOPPED" in out


def test_status_shows_stream_results(isolated_state, capsys):
    d.write_state("fleet", {"ok": True, "message": "0 in flight"})
    cmd.cmd_status(argparse.Namespace(), "reg.yaml")
    out = capsys.readouterr().out
    assert "fleet" in out
    assert "OK" in out


# --------------------------------------------------------------------------- #
# no-mutation guarantee
# --------------------------------------------------------------------------- #


def test_no_mutation_terms_in_daemon_code():
    """The daemon must contain no live code path that mutates board/state.

    We scan the daemon source files (the core engine + the command module) for
    tokens that would indicate a board/state-mutating call, using ``tokenize``
    so string literals (docstrings/help text that merely NAME the prohibition)
    and comments are excluded — we only flag real identifier uses, i.e. actual
    call sites. The installer is intentionally excluded here: its whole job is
    managing the launchd auto-start, and its ``--apply`` is the explicit gate
    (covered separately in ``test_install_is_gated_behind_apply``).
    """
    import io
    import os
    import tokenize

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    source_files = [
        os.path.join(root, "flightdeck/core/daemon.py"),
        os.path.join(root, "flightdeck/commands/daemon.py"),
    ]
    disallowed = {"archive_task", "create_task", "git push", "kanban.merge"}

    for path in source_files:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        # Collect only NAME/OP tokens (real code), excluding strings/comments.
        code_tokens: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (
                tokenize.NAME,
                tokenize.OP,
            ):
                code_tokens.append(tok.string)
        code_text = " ".join(code_tokens)
        for token in disallowed:
            assert token not in code_text, f"{path} contains a live {token!r} call"


def test_install_is_gated_behind_apply(isolated_state, monkeypatch, tmp_path, capsys):
    """`daemon install` without --apply is a read-only dry-run (nothing written)."""
    code = cmd.cmd_install(argparse.Namespace(apply=False), "reg.yaml")
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
