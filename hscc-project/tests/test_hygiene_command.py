"""Tests for the `flightdeck hygiene` command wiring + presentation.

The core decision logic is covered in test_hygiene.py. This file tests the
command layer: that it gathers inputs through injectable handles and — the
contract that matters most — that nothing mutates without ``--apply``. Every
external surface (kanban reader, git, registry, filesystem) is stubbed; no test
touches a live board, repo, or the network.
"""

import json

import pytest

from flightdeck.commands import hygiene as cmd
from flightdeck.core import kanban


def _args(**overrides):
    """A minimal argparse Namespace carrying the injectable handles."""
    import argparse

    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "apply": False,
        "similarity": None,
        "kdb": None,
        "run": None,
        "listdir": None,
        "list_archived": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(board="hscc", repo="/repo"):
    from flightdeck.core.registry import Project

    return Project(name="hscc", repo=repo, board=board)


def _hcard(cid, title="task", status="todo", board="hscc", created_at=100, branch=None):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch or f"wt/{cid}",
        "assignee": "coder",
        "created_at": created_at,
    }


def test_no_apply_does_not_mutate(monkeypatch, capsys):
    """The core contract: without --apply, no mutation helper is invoked."""

    applied = {"card_plan": 0, "wt_cleanup": 0}

    def fake_apply_card_plan(plan, **kw):
        applied["card_plan"] += 1
        return {"archived_duplicates": [], "archived_triage": [], "recreated": []}

    def fake_apply_wt(stale, repo_by_board=None, **kw):
        applied["wt_cleanup"] += 1
        return {"removed": [], "failed": []}

    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", "MCP Server Core", created_at=100),
        _hcard("b", "MCP Server Core", created_at=200),
    ])
    monkeypatch.setattr(cmd, "_git_facts_for_cards", lambda *a, **k: {})
    monkeypatch.setattr(cmd, "_collect_worktrees", lambda *a, **k: [])
    monkeypatch.setattr(cmd.hygiene, "apply_card_plan", fake_apply_card_plan)
    monkeypatch.setattr(cmd.hygiene, "apply_worktree_cleanup", fake_apply_wt)

    rc = cmd.cmd_hygiene(_args(), [_project()])
    assert rc == 0
    assert applied["card_plan"] == 0 and applied["wt_cleanup"] == 0
    captured = capsys.readouterr()
    assert "DUPLICATES" in captured.out
    assert "dry-run" in captured.err


def test_apply_invokes_mutation_helpers(monkeypatch):
    applied = {"card_plan": 0, "wt_cleanup": 0}

    def fake_apply_card_plan(plan, **kw):
        applied["card_plan"] += 1
        return {"archived_duplicates": ["a"], "archived_triage": [], "recreated": []}

    def fake_apply_wt(stale, repo_by_board=None, **kw):
        applied["wt_cleanup"] += 1
        return {"removed": [], "failed": []}

    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", "MCP Server Core", created_at=100),
        _hcard("b", "MCP Server Core", created_at=200),
    ])
    monkeypatch.setattr(cmd, "_git_facts_for_cards", lambda *a, **k: {})
    monkeypatch.setattr(cmd, "_collect_worktrees", lambda *a, **k: [])
    monkeypatch.setattr(cmd.hygiene, "apply_card_plan", fake_apply_card_plan)
    monkeypatch.setattr(cmd.hygiene, "apply_worktree_cleanup", fake_apply_wt)

    rc = cmd.cmd_hygiene(_args(apply=True), [_project()])
    assert rc == 0
    assert applied["card_plan"] == 1 and applied["wt_cleanup"] == 1


def test_json_emits_only_json_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", "MCP Server Core", created_at=100),
        _hcard("b", "MCP Server Core", created_at=200),
    ])
    monkeypatch.setattr(cmd, "_git_facts_for_cards", lambda *a, **k: {})
    monkeypatch.setattr(cmd, "_collect_worktrees", lambda *a, **k: [])

    rc = cmd.cmd_hygiene(_args(json=True), [_project()])
    assert rc == 0
    stdout = capsys.readouterr().out.strip()
    payload = json.loads(stdout)  # must be parseable standalone = pure JSON
    assert set(payload) == {"duplicates", "triage", "stale_worktrees"}
    assert payload["duplicates"][0]["keep"] == "b"


def test_clean_board_proposes_nothing_human(monkeypatch, capsys):
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("a", "MCP Server Core"),
        _hcard("b", "Deploy PostgreSQL"),
    ])
    monkeypatch.setattr(cmd, "_git_facts_for_cards", lambda *a, **k: {})
    monkeypatch.setattr(cmd, "_collect_worktrees", lambda *a, **k: [])

    rc = cmd.cmd_hygiene(_args(), [_project()])
    assert rc == 0
    assert "hygiene clean" in capsys.readouterr().out


def test_command_is_discovered():
    """`hygiene` registers as a real subcommand via cli auto-discovery."""
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["hygiene"])
    assert args.command == "hygiene"
    assert args.func is not None


def test_closed_ids_includes_archived_worktree_cards(monkeypatch, capsys):
    """An archived card with an on-disk worktree is reported stale: archived is
    closed, and the command reads archived cards up front for exactly this."""
    # The single read returns BOTH the non-archived cards and archived worktree
    # cards (include_archived=True), as the command requests.
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("t_live", status="running"),
        _hcard("t_old", status="archived"),
    ])
    monkeypatch.setattr(cmd, "_collect_worktrees", lambda *a, **k: [
        {"card_id": "t_old", "worktree": "/r/.worktrees/t_old", "branch": "wt/t_old", "board": "hscc"}
    ])
    monkeypatch.setattr(cmd, "_git_facts_for_cards", lambda *a, **k: {
        "t_old": {"branch_exists": True, "is_merged": True, "commits_ahead": 3},
        "t_live": {"branch_exists": True, "is_merged": True, "commits_ahead": 0},
    })

    rc = cmd.cmd_hygiene(_args(), [_project()])
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE WORKTREES (1)" in out
    assert "wt/t_old" in out
    # The live worktree is not reported.
    assert "wt/t_live" not in out


def test_archived_worktree_skipped_when_branch_unmerged(monkeypatch, capsys):
    """Even an archived card's worktree is NOT stale when its branch is not
    merged — the stale rule always requires both conditions."""
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [
        _hcard("t_old", status="archived"),
    ])
    monkeypatch.setattr(cmd, "_collect_worktrees", lambda *a, **k: [
        {"card_id": "t_old", "worktree": "/r/.worktrees/t_old", "branch": "wt/t_old", "board": "hscc"}
    ])
    monkeypatch.setattr(cmd, "_git_facts_for_cards", lambda *a, **k: {
        "t_old": {"branch_exists": True, "is_merged": False, "commits_ahead": 3},
    })

    rc = cmd.cmd_hygiene(_args(), [_project()])
    assert rc == 0
    assert "STALE WORKTREES (1)" not in capsys.readouterr().out
