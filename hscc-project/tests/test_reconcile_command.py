"""Tests for the `flightdeck reconcile` command layer.

The classification rules live in flightdeck.core.kanban (pure, covered in
test_kanban.py). This file tests reconcile's wiring: that it reads cards,
computes git facts through injectables, presents the plan, closes merged-branch
cards, and — the contract that matters most — never mutates without --apply.

No test touches a live board or repo: git facts come from a fake ``_run`` and
board writes from a fake ``_kdb``.
"""

import argparse
import json

import pytest

from flightdeck.commands import reconcile as cmd
from flightdeck.core import kanban
from flightdeck.core.registry import Project


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "apply": False,
        "days": 14,
        "kdb": None,
        "run": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(board="hscc", repo="/repo"):
    return Project(name="hscc", repo=repo, board=board)


def _hcard(cid, title="task", status="review", board="hscc", created_at=100,
           branch=None, workspace_path="/repo"):
    # workspace_path is the attribution key now: a card belongs to the project
    # whose repo it lives under. The board slug is only a narrowing filter,
    # because the shared `default` board holds cards from many projects.
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch or f"wt/{cid}",
        "assignee": "coder",
        "created_at": created_at,
        "workspace_path": workspace_path,
    }


class FakeRun:
    """A git ``_run(cmd, repo)`` stand-in returning scripted results.

    Tracks which refs exist and whether each ref is an ancestor of main, so the
    fake merges/ancestry logic mirrors git_state without touching a repo.
    """

    def __init__(self, existing=set(), merged=set(), ahead=0, landed=None):
        self.existing = set(existing)
        self.merged = set(merged)      # refs that are ancestors of main
        self.ahead = ahead
        # Refs a --no-ff merge actually carried into main. Defaults to `merged`
        # so existing cases keep their meaning; an unstarted branch is an
        # ancestor of main WITHOUT ever having landed, which is the SEV-1 case
        # and is expressed by passing landed=set().
        self.landed = set(self.merged if landed is None else landed)
        self.calls = []

    def __call__(self, cmd, repo):
        self.calls.append((cmd, repo))
        # branch_exists: rev-parse --verify --quiet <branch>
        if cmd[0] == "git" and cmd[1] == "rev-parse" and "--verify" in cmd:
            ref = cmd[-1]
            cp = argparse.Namespace(returncode=0 if ref in self.existing else 1, stdout="", stderr="")
            return cp
        # is_merged: merge-base --is-ancestor <branch> <base>
        if cmd[0] == "git" and cmd[1] == "merge-base":
            ref = cmd[-2]
            cp = argparse.Namespace(returncode=0 if ref in self.merged else 1, stdout="", stderr="")
            return cp
        # landed_via_merge: rev-parse <branch>, then log --merges --pretty=%P
        if cmd[0] == "git" and cmd[1] == "rev-parse" and "--verify" not in cmd:
            ref = cmd[-1]
            return argparse.Namespace(returncode=0, stdout=f"sha-{ref}", stderr="")
        if cmd[0] == "git" and cmd[1] == "log" and "--merges" in cmd:
            lines = "".join(f"trunk-parent sha-{r}\n" for r in self.landed)
            return argparse.Namespace(returncode=0, stdout=lines, stderr="")
        # commits_ahead: rev-list --count <base>..<branch>
        if cmd[0] == "git" and cmd[1] == "rev-list":
            cp = argparse.Namespace(returncode=0, stdout=f"{self.ahead}", stderr="")
            return cp
        raise AssertionError(f"unexpected git command: {cmd}")


class FakeKdb:
    """A stand-in for hermes_cli.kanban_db with complete_task/archive_task."""

    def __init__(self):
        self.closed = []
        self.archived = []

    def connect(self, board=None):
        return self

    def close(self):
        pass

    def complete_task(self, conn, task_id, **kw):
        self.closed.append(task_id)
        return True

    def archive_task(self, conn, task_id):
        self.archived.append(task_id)
        return True


# --------------------------------------------------------------------------- #
# Closes merged branch cards; never closes unmerged ones.
# --------------------------------------------------------------------------- #


def test_reconcile_closes_merged_branch_card(monkeypatch):
    run = FakeRun(existing={"wt/a"}, merged={"wt/a"}, ahead=0)
    kdb = FakeKdb()
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", title="Landed work", status="review", branch="wt/a"),
    ])

    rc = cmd.cmd_reconcile(_args(apply=True, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.closed == ["a"]
    assert kdb.archived == []


def test_reconcile_does_not_close_unmerged_card(monkeypatch):
    run = FakeRun(existing={"wt/b"}, merged=set(), ahead=4)
    kdb = FakeKdb()
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("b", title="Needs review", status="review", branch="wt/b"),
    ])

    rc = cmd.cmd_reconcile(_args(apply=True, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.closed == []      # never closed
    assert kdb.archived == []


def test_dry_run_mutates_nothing(monkeypatch, capsys):
    run = FakeRun(existing={"wt/a"}, merged={"wt/a"}, ahead=0)
    kdb = FakeKdb()
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", title="Landed work", status="review", branch="wt/a"),
    ])

    rc = cmd.cmd_reconcile(_args(apply=False, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.closed == [] and kdb.archived == []
    # The plan is still shown, and the dry-run notice is issued.
    captured = capsys.readouterr()
    assert "CLOSE (work landed) (1)" in captured.out
    assert "dry-run" in captured.err


def test_apply_archives_dead_card(monkeypatch):
    now = 1_000_000
    # dead: no branch, no commits, older than N days. --days tiny so created_at
    # (old) crosses it.
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        {**_hcard("c", title="Unstarted", status="todo", created_at=now - (3 * 86400)),
         "branch": "wt/c"},
    ])
    run = FakeRun(existing=set(), merged=set(), ahead=0)
    kdb = FakeKdb()
    rc = cmd.cmd_reconcile(_args(apply=True, days=2, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.archived == ["c"]
    assert kdb.closed == []


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["reconcile"])
    assert args.command == "reconcile"
    assert args.func is not None


def test_json_output_shape(monkeypatch, capsys):
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", title="Landed", status="review", branch="wt/a"),
    ])
    run = FakeRun(existing={"wt/a"}, merged={"wt/a"}, ahead=0)
    rc = cmd.cmd_reconcile(_args(json=True, run=run), [_project()])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"close", "archive", "stale", "skipped"}
    assert payload["close"][0]["card_id"] == "a"


def test_card_whose_workspace_matches_no_project_is_never_claimed(monkeypatch):
    """A card that cannot be attributed cannot be verified -> never acted on.

    The rule moved from board slug to repo path: a card belongs to the project
    whose repo its workspace_path lives under. A workspace matching no
    registered repo is UNATTRIBUTED, and we never close or archive what we
    could not verify.
    """
    run = FakeRun(existing={"wt/a"}, merged={"wt/a"}, ahead=0, landed={"wt/a"})
    kdb = FakeKdb()
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", title="Elsewhere", status="review", branch="wt/a",
               workspace_path="/some/other/repo"),
    ])

    rc = cmd.cmd_reconcile(_args(apply=True, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.closed == [] and kdb.archived == []

def test_unstarted_branch_is_never_closed_even_though_it_looks_merged(monkeypatch):
    """The exact live failure: reconcile proposed closing three RUNNING cards.

    A freshly created worktree branch points at the main tip it forked from, so
    once main advances it is an ancestor of main — indistinguishable from a
    merged branch by ancestry alone. It carried no work and must never be
    closed. ``landed=set()`` means "ancestor of main, but no merge carried it".
    """
    run = FakeRun(existing={"wt/a"}, merged={"wt/a"}, ahead=0, landed=set())
    kdb = FakeKdb()
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", title="Never started", status="review", branch="wt/a"),
    ])

    rc = cmd.cmd_reconcile(_args(apply=True, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.closed == [], "an unstarted branch must never be closed"
    assert kdb.archived == []


def test_genuinely_merged_branch_is_still_closed(monkeypatch):
    """The fix must not regress the behaviour reconcile exists for."""
    run = FakeRun(existing={"wt/a"}, merged={"wt/a"}, ahead=0, landed={"wt/a"})
    kdb = FakeKdb()
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", title="Landed", status="review", branch="wt/a"),
    ])

    rc = cmd.cmd_reconcile(_args(apply=True, run=run, kdb=kdb), [_project()])
    assert rc == 0
    assert kdb.closed == ["a"]
