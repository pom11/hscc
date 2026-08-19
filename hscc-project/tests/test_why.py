"""Tests for the `flightdeck why <card_id>` command layer.

Each section renders from injected kanban + git facts; the verdict is correct
for each of the five states; a card with no branch is handled; an unknown id
errors naming the boards searched; ``--json`` carries the same fields. No test
touches the real board or git — the kanban library (``_kdb``), git subprocess
(``_run``), and clock (``_now``) are all fakes.
"""

import argparse
import json
import re

import pytest

from flightdeck.commands import why as cmd
from flightdeck.core import kanban
from flightdeck.core.registry import Project

NOW = 1_000_000


def _project(name="flightdeck", repo="/repo", board="flightdeck"):
    return Project(name=name, repo=repo, board=board)


class _Conn:
    """A tiny connection stand-in: carries the board slug and a no-op close."""

    def __init__(self, board):
        self.board = board

    def close(self):
        pass


class FakeKdb:
    """A hermes.kanban_db stand-in: boards -> list of fake Task-like objects.

    ``connect(board=slug)`` returns a connection carrying the slug so
    ``list_tasks`` can look up the right board's tasks, mirroring how
    flightdeck reads one board at a time and closes the connection.
    """

    def __init__(self, boards, tasks_by_board):
        self.boards = boards            # list of {"slug": ...}
        self.tasks = tasks_by_board     # {board_slug: [TaskLike, ...]}

    def list_boards(self):
        return self.boards

    def connect(self, board=None):
        return _Conn(board)

    def list_tasks(self, conn, include_archived=False):
        return self.tasks.get(conn.board, [])


class TaskLike:
    """A minimal stand-in for hermes_cli.kanban_db.Task."""

    def __init__(self, cid, *, title="some task", status="review", board="board",
                 assignee="coder", branch=None, created_at=NOW - 1000,
                 started_at=NOW - 500, last_heartbeat_at=None, body=None,
                 workspace_kind="worktree", workspace_path=None):
        self.id = cid
        self.title = title
        self.body = body
        self.status = status
        self.board = board
        self.assignee = assignee
        self.created_at = created_at
        self.started_at = started_at
        self.last_heartbeat_at = last_heartbeat_at
        self.workspace_kind = workspace_kind
        if workspace_path is None:
            workspace_path = f"/repo/.worktrees/{cid}"
        self.workspace_path = workspace_path
        self.branch_name = branch
        self.completed_at = None


def _kdb(cards, boards=("flightdeck", "hscc")):
    """A FakeKdb seeded with the given cards on the first board searched."""
    by_board = {b: [] for b in boards}
    for c in cards:
        by_board[boards[0]].append(c)
    return FakeKdb(
        [{"slug": b} for b in boards],
        {b: by_board[b] for b in boards},
    )


class FakeRun:
    """A git ``_run(cmd, repo)`` stand-in; tracks which refs exist / landed."""

    def __init__(self, existing=set(), commits=(), landed=set(), uncommitted=()):
        self.existing = set(existing)
        self.commits = list(commits)
        self.landed = set(landed)
        self.uncommitted = list(uncommitted)

    def __call__(self, repo_cmd, repo):
        c = repo_cmd
        # branch_exists: rev-parse --verify --quiet <branch>
        if c[1] == "rev-parse" and "--verify" in c:
            return argparse.Namespace(
                returncode=0 if c[-1] in self.existing else 1, stdout="", stderr=""
            )
        # landed_via_merge: rev-parse <branch> then git log base --merges --pretty=%P
        if c[1] == "log" and "--merges" in c:
            # Only the SECOND parent of each merge is a landed branch tip.
            lines = "".join(f"trunk-parent sha-{r}\n" for r in self.landed)
            return argparse.Namespace(returncode=0, stdout=lines, stderr="")
        # commit_subjects: git log <base>..<branch> --pretty=%s
        if c[1] == "log":
            return argparse.Namespace(
                returncode=0, stdout="\n".join(self.commits), stderr=""
            )
        if c[1] == "rev-parse" and "--verify" not in c:
            return argparse.Namespace(
                returncode=0, stdout=f"sha-{c[-1]}", stderr=""
            )
        # uncommitted_files: git status --porcelain
        if c[1] == "status":
            return argparse.Namespace(
                returncode=0,
                stdout="\n".join(f" M {f}" for f in self.uncommitted),
                stderr="",
            )
        raise AssertionError(f"unexpected git command: {c}")


# --------------------------------------------------------------------------- #
# Story structure — each section renders from injected facts
# --------------------------------------------------------------------------- #


def _story(**over):
    """A fully-populated story dict for one card in the AWAITING REVIEW state."""
    base = {
        "id": "t_abc",
        "title": "Implement why command",
        "status": "review",
        "assignee": "coder",
        "board": "flightdeck",
        "project": "flightdeck",
        "milestone": "review-loop",
        "created_at": NOW - 2000,
        "started_at": NOW - 1500,
        "last_heartbeat_at": NOW - 10,
        "status_duration_s": 500,
        "branch": "wt/t_abc",
        "branch_exists": True,
        "commits": ["feat(why): add story rendering"],
        "landed": False,
        "uncommitted": [],
        "workspace_path": "/repo/.worktrees/t_abc",
        "is_worktree": True,
        "boards_searched": ["flightdeck", "hscc"],
    }
    base.update(over)
    base["verdict"] = cmd.verdict(base)
    return base


# --------------------------------------------------------------------------- #
# The five verdicts
# --------------------------------------------------------------------------- #

def test_verdict_landed():
    s = _story(landed=True)
    v = cmd.verdict(s)
    assert "LANDED" in v
    assert "close the card" in v


def test_verdict_awaiting_review():
    s = _story(commits=["feat: thing"], landed=False)
    v = cmd.verdict(s)
    assert "AWAITING REVIEW" in v
    assert "merge" in v


def test_verdict_working():
    # commits empty, files present -> WORKING
    s = _story(commits=[], uncommitted=["src/mod.py"])
    v = cmd.verdict(s)
    assert "WORKING" in v
    assert "commit" in v


def test_verdict_starved():
    # branch exists, no commits, no files -> STARVED (infrastructure)
    s = _story(commits=[], uncommitted=[])
    v = cmd.verdict(s)
    assert "STARVED" in v
    assert "worktree empty" in v


def test_verdict_no_branch():
    s = _story(branch_exists=False, branch="wt/t_abc", commits=[], uncommitted=[])
    v = cmd.verdict(s)
    assert "NO BRANCH" in v


# --------------------------------------------------------------------------- #
# Branch / workspace facts
# --------------------------------------------------------------------------- #

def test_story_gathers_git_facts():
    task = TaskLike("t_abc", status="review")
    run = FakeRun(existing={"wt/t_abc"}, commits=["feat: x"], landed=set(), uncommitted=["a.py"])
    s = cmd.gather(
        "t_abc", [_project()], _run=run, _now=lambda: NOW, _kdb=_kdb([task])
    )
    assert s["branch_exists"] is True
    assert s["commits"] == ["feat: x"]
    assert s["landed"] is False
    assert s["uncommitted"] == ["a.py"]


def test_card_with_no_branch_handled():
    # A card whose branch does not exist on disk is handled without crashing.
    task = TaskLike("t_abc", status="todo", branch="wt/t_abc")
    run = FakeRun(existing=set(), commits=[], landed=set())
    s = cmd.gather(
        "t_abc", [_project()], _run=run, _now=lambda: NOW, _kdb=_kdb([task])
    )
    assert s["branch_exists"] is False
    assert s["commits"] == []
    assert s["verdict"].startswith("NO BRANCH")


def test_landed_via_merge_is_used_not_ancestry():
    # The branch exists with commits but was NOT carried into main via a
    # --no-ff merge (landed=set()): it must NOT read as landed. A fresh
    # worktree branch is an ancestor of main the moment main advances, so
    # existence alone must never imply landed.
    task = TaskLike("t_abc", status="review")
    run = FakeRun(existing={"wt/t_abc"}, commits=["feat: x"], landed=set())
    s = cmd.gather(
        "t_abc", [_project()], _run=run, _now=lambda: NOW, _kdb=_kdb([task])
    )
    assert s["landed"] is False
    assert s["verdict"].startswith("AWAITING REVIEW")


def test_branch_carried_into_main_reads_landed():
    task = TaskLike("t_abc", status="review")
    run = FakeRun(existing={"wt/t_abc"}, commits=["feat: x"], landed={"wt/t_abc"})
    s = cmd.gather(
        "t_abc", [_project()], _run=run, _now=lambda: NOW, _kdb=_kdb([task])
    )
    assert s["landed"] is True
    assert s["verdict"].startswith("LANDED")


def test_workspace_root_is_not_a_worktree():
    task = TaskLike("t_abc", workspace_path="/repo")
    s = cmd.gather(
        "t_abc", [_project()], _run=FakeRun(existing=set()), _now=lambda: NOW,
        _kdb=_kdb([task]),
    )
    assert s["is_worktree"] is False


def test_uncommitted_read_from_worktree_not_repo_root():
    # The status check for uncommitted files must run in the card's WORKTREE
    # checkout (where the worker's files live), never the repo root — otherwise
    # a card with files in hand would falsely read STARVED.
    task = TaskLike("t_abc", workspace_path="/repo/.worktrees/t_abc")

    def make_run(status_dirs):
        class _R:
            def __call__(self, c, repo):
                if c[1] == "rev-parse" and "--verify" in c:
                    return argparse.Namespace(returncode=0, stdout="", stderr="")
                if c[1] == "log" and "--merges" in c:
                    return argparse.Namespace(returncode=0, stdout="", stderr="")
                if c[1] == "log":
                    return argparse.Namespace(returncode=0, stdout="", stderr="")
                if c[1] == "rev-parse":
                    return argparse.Namespace(returncode=0, stdout="sha-wt/t_abc", stderr="")
                if c[1] == "status":
                    status_dirs.append(repo)
                    return argparse.Namespace(
                        returncode=0, stdout=" M a.py", stderr=""
                    )
                raise AssertionError(c)

        return _R()

    status_dirs = []
    run = make_run(status_dirs)
    s = cmd.gather(
        "t_abc", [_project()], _run=run, _now=lambda: NOW, _kdb=_kdb([task])
    )
    assert status_dirs == ["/repo/.worktrees/t_abc"]  # the worktree, not /repo
    assert s["uncommitted"] == ["a.py"]
    assert s["verdict"].startswith("WORKING")


# --------------------------------------------------------------------------- #
# Unknown card id
# --------------------------------------------------------------------------- #

def test_unknown_id_errors_naming_boards_searched():
    kdb = _kdb([TaskLike("t_known")], boards=("flightdeck", "hscc"))
    with pytest.raises(cmd.UnknownCardError) as ei:
        cmd.gather("t_nope", [_project()], _run=FakeRun(), _now=lambda: NOW, _kdb=kdb)
    msg = str(ei.value)
    assert "t_nope" in msg
    assert "flightdeck" in msg
    assert "hscc" in msg


def test_unknown_id_via_command_returns_1_and_names_boards(capsys, monkeypatch):
    from flightdeck.core import kanban as core_kanban
    called = {"n": 0}

    def fake_find(cid, **_kw):
        called["n"] += 1
        return None

    def fake_boards_searched(**_kw):
        return ["b1", "b2"]

    monkeypatch.setattr(core_kanban, "find_card", fake_find)
    monkeypatch.setattr(core_kanban, "list_boards_searched", fake_boards_searched)
    rc = cmd.cmd_why(_args(), [_project()])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown card id" in err
    assert "b1" in err and "b2" in err


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "card_id": "t_abc",
        "run": None,
        "now": None,
        "kdb": None,
        "events": None,
        "boards": None,
        "stderr": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_render_covers_all_sections():
    lines = cmd.render(_story())
    text = "\n".join(lines)
    for section in ("IDENTITY", "TIMING", "BRANCH", "WORKSPACE", "VERDICT"):
        assert section in text
    assert "review-loop" in text          # milestone
    assert "flightdeck" in text           # project
    assert "AWAITING REVIEW" in text      # verdict


def test_milestone_absent_when_no_tag():
    lines = cmd.render(_story(milestone=None))
    assert "milestone -" in "\n".join(lines)


def test_json_carries_same_fields(capsys, monkeypatch):
    from flightdeck.core import kanban as core_kanban
    story = _story()
    monkeypatch.setattr(cmd, "gather", lambda *a, **k: story)
    rc = cmd.cmd_why(_args(json=True), [_project()])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    for key in (
        "id", "title", "status", "assignee", "board", "project", "milestone",
        "created_at", "started_at", "last_heartbeat_at", "status_duration_s",
        "branch", "branch_exists", "commits", "landed", "uncommitted",
        "workspace_path", "is_worktree", "verdict", "boards_searched",
    ):
        assert key in payload
        assert payload[key] == story[key]


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["why", "t_abc"])
    assert args.command == "why"
    assert args.card_id == "t_abc"
    assert args.func is not None


def test_milestone_tag_parsing():
    assert cmd._milestone_id("do the thing\n\nMILESTONE: review-loop\n") == "review-loop"
    assert cmd._milestone_id("no tag here") is None
    assert cmd._milestone_id(None) is None


# --------------------------------------------------------------------------- #
# Timing — how long in the current status
# --------------------------------------------------------------------------- #

def test_status_duration_computed_from_events(capsys):
    # The most recent status-transition event sets when the current status
    # began; duration = now - that. Here the last transition ("submitted_for_review")
    # is at NOW-500, so the card has been in review for 500s.
    events = [
        {"kind": "created", "created_at": NOW - 2000},
        {"kind": "claimed", "created_at": NOW - 1500},
        {"kind": "submitted_for_review", "created_at": NOW - 500},
        {"kind": "heartbeat", "created_at": NOW - 10},  # not a transition
    ]
    task = TaskLike("t_abc", status="review", started_at=NOW - 1500)
    s = cmd.gather(
        "t_abc", [_project()], _run=FakeRun(existing=set()), _now=lambda: NOW,
        _kdb=_kdb([task]), _events=lambda cid: events,
    )
    assert s["status_duration_s"] == 500
    lines = cmd.render(s)
    assert "in current status 8m" in "\n".join(lines)  # 500s -> 8m


def test_status_duration_falls_back_to_started_at_when_no_events():
    task = TaskLike("t_abc", status="review", started_at=NOW - 1000)
    s = cmd.gather(
        "t_abc", [_project()], _run=FakeRun(existing=set()), _now=lambda: NOW,
        _kdb=_kdb([task]), _events=lambda cid: [],
    )
    assert s["status_duration_s"] == 1000
