"""Tests for flightdeck.core.kanban — pure decision logic, no I/O.

The reconcile rules are tested against injected git facts (never the live
board or a real repo). ``list_cards`` is tested with a stubbed Hermes
library. Nothing here touches git, the network, or the real kanban DB, so
the suite stays fast and deterministic.
"""

from types import SimpleNamespace

import pytest

from flightdeck.core import kanban

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def card(
    cid="t_abc",
    status="review",
    branch=None,
    assignee="coder",
    board="hscc",
    created_at=1000,
    started_at=None,
    completed_at=None,
):
    """A minimal flightdeck card dict."""
    return {
        "id": cid,
        "title": "some task",
        "status": status,
        "assignee": assignee,
        "board": board,
        "branch": branch,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def facts(card_id, *, branch_exists=True, is_merged=False, commits_ahead=3,
          landed_via_merge=None):
    """A single-card ``git_facts`` mapping for the given card id."""
    return {
        card_id: {
            "branch_exists": branch_exists,
            "is_merged": is_merged,
            # defaults to is_merged: these fixtures mean "genuinely landed".
            # An unstarted branch is is_merged=True with landed_via_merge=False.
            "landed_via_merge": (
                is_merged if landed_via_merge is None else landed_via_merge
            ),
            "commits_ahead": commits_ahead,
        }
    }


NOW = 1_000_000

# --------------------------------------------------------------------------- #
# The rule that matters: a merged branch is LANDED, never NEEDS_YOU.
# --------------------------------------------------------------------------- #


def test_merged_review_card_is_landed_not_needs_you():
    """A card in review whose branch IS merged must close, not nag."""
    c = card(status="review")
    g = facts("t_abc", branch_exists=True, is_merged=True, commits_ahead=0)
    assert kanban.classify(c, g) == "landed"


def test_merged_blocked_card_is_landed_not_needs_you():
    """Same invariant for a blocked card whose branch merged."""
    c = card(status="blocked")
    g = facts("t_abc", branch_exists=True, is_merged=True, commits_ahead=0)
    assert kanban.classify(c, g) == "landed"


def test_merged_running_card_is_landed():
    """landed wins over every status, including a running card whose branch
    already reached main."""
    c = card(status="running", started_at=NOW - 3600)
    g = facts("t_abc", branch_exists=True, is_merged=True, commits_ahead=0)
    assert kanban.classify(c, g, now=NOW) == "landed"


# --------------------------------------------------------------------------- #
# needs_you — requires a genuinely unmerged, existing branch.
# --------------------------------------------------------------------------- #


def test_needs_you_review_unmerged():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=5)
    assert kanban.classify(card(status="review"), g) == "needs_you"


def test_needs_you_blocked_unmerged():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=5)
    assert kanban.classify(card(status="blocked"), g) == "needs_you"


def test_review_card_with_no_branch_not_needs_you():
    """No branch -> no verifiable unmerged work -> not needs_you."""
    g = facts("t_abc", branch_exists=False, is_merged=False, commits_ahead=0)
    # Not older than dead threshold, so it settles to running.
    c = card(status="review", created_at=NOW - 100)
    assert kanban.classify(c, g, now=NOW) == "running"


# --------------------------------------------------------------------------- #
# dead — no branch, no commits, older than N days.
# --------------------------------------------------------------------------- #


def test_dead_no_branch_no_commits_old():
    g = facts("t_abc", branch_exists=False, is_merged=False, commits_ahead=0)
    c = card(status="todo", created_at=NOW - (14 * 86400) - 1)
    assert kanban.classify(c, g, dead_days=14, now=NOW) == "dead"


def test_dead_not_if_recent():
    """A no-branch no-commit card created yesterday is not dead yet."""
    g = facts("t_abc", branch_exists=False, is_merged=False, commits_ahead=0)
    c = card(status="todo", created_at=NOW - 86400)
    assert kanban.classify(c, g, dead_days=14, now=NOW) == "running"


def test_dead_not_if_has_commits():
    """Even if old, a card with commits has work -> not dead."""
    g = facts("t_abc", branch_exists=False, is_merged=False, commits_ahead=2)
    c = card(status="todo", created_at=NOW - (30 * 86400))
    assert kanban.classify(c, g, dead_days=14, now=NOW) == "running"


def test_dead_uses_injected_dead_days():
    g = facts("t_abc", branch_exists=False, is_merged=False, commits_ahead=0)
    c = card(status="todo", created_at=NOW - (3 * 86400))
    assert kanban.classify(c, g, dead_days=2, now=NOW) == "dead"


# --------------------------------------------------------------------------- #
# stale — REQUIRES both time past threshold AND zero commits.
# --------------------------------------------------------------------------- #


def test_stale_running_past_threshold_zero_commits():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=0)
    c = card(status="running", started_at=NOW - (46 * 60))
    assert kanban.classify(c, g, stale_threshold_seconds=45 * 60, now=NOW) == "stale"


def test_running_for_two_hours_WITH_commits_is_running_not_stale():
    """The mandated rule: a card running 2h WITH commits is running, NOT stale."""
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=4)
    c = card(status="running", started_at=NOW - (2 * 3600))
    assert kanban.classify(
        c, g, stale_threshold_seconds=45 * 60, now=NOW
    ) == "running"


def test_running_but_under_threshold_is_running():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=0)
    c = card(status="running", started_at=NOW - (10 * 60))
    assert kanban.classify(c, g, stale_threshold_seconds=45 * 60, now=NOW) == "running"


def test_stale_threshold_is_injectable():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=0)
    c = card(status="running", started_at=NOW - 100)
    # Default threshold (45m) would say running; a tight threshold says stale.
    assert kanban.classify(c, g, stale_threshold_seconds=50, now=NOW) == "stale"
    assert kanban.classify(c, g, stale_threshold_seconds=200, now=NOW) == "running"


def test_stale_not_for_missing_started_at():
    """No started_at -> cannot verify how long it has been running -> not stale."""
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=0)
    c = card(status="running", started_at=None)
    assert kanban.classify(c, g, stale_threshold_seconds=45 * 60, now=NOW) == "running"


# --------------------------------------------------------------------------- #
# running — default fallback.
# --------------------------------------------------------------------------- #


def test_running_in_flight_nothing_needed():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=3)
    assert kanban.classify(card(status="running"), g) == "running"


def test_todo_card_with_branch_is_running():
    g = facts("t_abc", branch_exists=True, is_merged=False, commits_ahead=1)
    assert kanban.classify(card(status="todo"), g) == "running"


# --------------------------------------------------------------------------- #
# Missing / unknown git facts degrade conservatively (never claim landed or
# needs-you without verification).
# --------------------------------------------------------------------------- #


def test_review_card_with_no_facts_is_running_not_needs_you():
    """No git facts -> cannot verify unmerged -> never a phantom needs_you."""
    c = card(status="review", created_at=NOW - 100)
    assert kanban.classify(c, {}, now=NOW) == "running"


def test_landed_requires_facts():
    """Cannot claim landed without facts saying the branch exists + merged."""
    c = card(status="review")
    assert kanban.classify(c, {}, now=NOW) == "running"


# --------------------------------------------------------------------------- #
# reconcile_plan — groups correctly, and never mutates.
# --------------------------------------------------------------------------- #


def test_reconcile_plan_groups_into_close_archive_stale():
    cards = [
        card("c1", status="review", branch="wt/c1"),
        card("c2", status="todo", created_at=NOW - (30 * 86400)),
        card("c3", status="running", started_at=NOW - (2 * 3600)),
        card("c4", status="review"),
    ]
    g = {
        "c1": {"branch_exists": True, "is_merged": True, "commits_ahead": 0,
               "landed_via_merge": True},
        "c2": {"branch_exists": False, "is_merged": False, "commits_ahead": 0},
        "c3": {"branch_exists": True, "is_merged": False, "commits_ahead": 4},
        "c4": {"branch_exists": True, "is_merged": False, "commits_ahead": 2},
    }
    plan = kanban.reconcile_plan(cards, g, dead_days=14, now=NOW)
    assert plan == {
        "close": ["c1"],
        "archive": ["c2"],
        "stale": [],
        "skipped": [],
    }


def test_reconcile_plan_includes_stale_cards():
    cards = [
        card("c1", status="running", started_at=NOW - (2 * 3600)),  # 0 commits -> stale
        card("c2", status="running", started_at=NOW - (2 * 3600)),  # 4 commits -> running
    ]
    g = {
        "c1": {"branch_exists": True, "is_merged": False, "commits_ahead": 0},
        "c2": {"branch_exists": True, "is_merged": False, "commits_ahead": 4},
    }
    plan = kanban.reconcile_plan(cards, g, stale_threshold_seconds=45 * 60, now=NOW)
    assert plan["stale"] == ["c1"]
    assert "c2" not in plan["stale"]


def test_reconcile_plan_disambiguates_all_five_classes():
    """One of each classification: each lands in exactly its own bucket."""
    cards = [
        card("land", status="review", branch="wt/land"),
        card("need", status="review", branch="wt/need"),
        card("dead", status="todo", created_at=NOW - (30 * 86400)),
        card("stal", status="running", started_at=NOW - (2 * 3600)),
        card("runn", status="running", started_at=NOW - 100),
    ]
    g = {
        "land": {"branch_exists": True, "is_merged": True, "commits_ahead": 0,
                 "landed_via_merge": True},
        "need": {"branch_exists": True, "is_merged": False, "commits_ahead": 3},
        "dead": {"branch_exists": False, "is_merged": False, "commits_ahead": 0},
        "stal": {"branch_exists": True, "is_merged": False, "commits_ahead": 0},
        "runn": {"branch_exists": True, "is_merged": False, "commits_ahead": 2},
    }
    plan = kanban.reconcile_plan(cards, g, dead_days=14, now=NOW)
    assert plan == {
        "close": ["land"], "archive": ["dead"], "stale": ["stal"], "skipped": [],
    }
    # needs_you and running cards are NOT in the plan at all.
    assert "need" not in sum(plan.values(), [])
    assert "runn" not in sum(plan.values(), [])


def test_reconcile_plan_does_not_mutate_inputs():
    cards = [
        card("c1", status="review", branch="wt/c1"),
        card("c2", status="todo", created_at=NOW - (30 * 86400)),
    ]
    g = {
        "c1": {"branch_exists": True, "is_merged": True, "commits_ahead": 0,
               "landed_via_merge": True},
        "c2": {"branch_exists": False, "is_merged": False, "commits_ahead": 0},
    }
    cards_before = [dict(c) for c in cards]
    g_before = {k: dict(v) for k, v in g.items()}

    kanban.reconcile_plan(cards, g, dead_days=14, now=NOW)
    kanban.classify(cards[0], g)

    assert cards == cards_before
    assert g == g_before


# --------------------------------------------------------------------------- #
# list_cards — reads through a stubbed Hermes library.
# --------------------------------------------------------------------------- #


def _make_fake_kb(boards=("hscc", "other"), tasks_by_board=None):
    """A stand-in for hermes_cli.kanban_db.

    Duplicates the tiny surface kanban.py uses: list_boards, connect,
    list_tasks. Tasks are SimpleNamespace objects carrying the Task fields
    kanban._to_card reads.
    """
    tasks_by_board = tasks_by_board or {}

    class _Conn:
        def __init__(self, slug):
            self.slug = slug

        def close(self):
            pass

    def list_boards():
        return [{"slug": b} for b in boards]

    def connect(board=None):
        return _Conn(board)

    def list_tasks(conn, include_archived=False):
        return tasks_by_board.get(conn.slug, [])

    return SimpleNamespace(
        list_boards=list_boards, connect=connect, list_tasks=list_tasks
    )


def _stub_kanban_db(kb, monkeypatch):
    monkeypatch.setattr(
        kanban, "_load_kanban_db", lambda: kb, raising=False
    )


def test_list_cards_all_boards(monkeypatch):
    kb = _make_fake_kb(
        boards=("hscc", "other"),
        tasks_by_board={
            "hscc": [
                SimpleNamespace(
                    id="c1", title="one", status="review", assignee="coder",
                    branch_name="wt/c1", created_at=1, started_at=None,
                    completed_at=None, workspace_kind="worktree",
                )
            ],
            "other": [
                SimpleNamespace(
                    id="c2", title="two", status="running", assignee="coder",
                    branch_name=None, created_at=2, started_at=3,
                    completed_at=None, workspace_kind="scratch",
                )
            ],
        },
    )
    _stub_kanban_db(kb, monkeypatch)

    cards = kanban.list_cards(board=None)
    by_id = {c["id"]: c for c in cards}
    assert set(by_id) == {"c1", "c2"}
    # Board source is correctly attributed.
    assert by_id["c1"]["board"] == "hscc"
    assert by_id["c2"]["board"] == "other"
    # Required keys present on every card.
    for c in cards:
        for key in ("id", "title", "status", "assignee", "board", "branch"):
            assert key in c


def test_list_cards_single_board(monkeypatch):
    kb = _make_fake_kb(
        boards=("hscc", "other"),
        tasks_by_board={
            "hscc": [SimpleNamespace(
                id="c1", title="one", status="review", assignee="coder",
                branch_name="wt/c1", created_at=1, started_at=None,
                completed_at=None, workspace_kind="worktree",
            )],
            "other": [SimpleNamespace(
                id="c2", title="two", status="running", assignee="coder",
                branch_name=None, created_at=2, started_at=3,
                completed_at=None, workspace_kind="scratch",
            )],
        },
    )
    _stub_kanban_db(kb, monkeypatch)

    cards = kanban.list_cards(board="hscc")
    assert [c["id"] for c in cards] == ["c1"]


def test_list_cards_branch_falls_back_to_wt_prefix(monkeypatch):
    """A card with no recorded branch gets the wt/<id> convention."""
    kb = _make_fake_kb(
        boards=("hscc",),
        tasks_by_board={
            "hscc": [SimpleNamespace(
                id="c9", title="nine", status="running", assignee="coder",
                branch_name=None, created_at=1, started_at=None,
                completed_at=None, workspace_kind="worktree",
            )],
        },
    )
    _stub_kanban_db(kb, monkeypatch)

    cards = kanban.list_cards()
    assert cards[0]["branch"] == "wt/c9"


def test_list_cards_uses_recorded_branch_over_convention(monkeypatch):
    kb = _make_fake_kb(
        boards=("hscc",),
        tasks_by_board={
            "hscc": [SimpleNamespace(
                id="c7", title="seven", status="review", assignee="coder",
                branch_name="feature/xyz", created_at=1, started_at=None,
                completed_at=None, workspace_kind="worktree",
            )],
        },
    )
    _stub_kanban_db(kb, monkeypatch)

    cards = kanban.list_cards()
    assert cards[0]["branch"] == "feature/xyz"


def test_list_cards_raises_clear_error_when_hermes_missing(monkeypatch):
    """If Hermes is not installed, surface a clear error, not an ImportError."""

    def _fail_load():
        raise kanban.KanbanError("Hermes agent source not found at ...")

    monkeypatch.setattr(kanban, "_load_kanban_db", _fail_load, raising=False)
    with pytest.raises(kanban.KanbanError):
        kanban.list_cards()


def test_load_kanban_db_raises_when_path_missing(monkeypatch):
    """The real loader raises a clear KanbanError on an absent path."""
    monkeypatch.setenv("HERMES_AGENT_PATH", "/nonexistent/hermes/nope")
    with pytest.raises(kanban.KanbanError):
        kanban._load_kanban_db()


# --------------------------------------------------------------------------- #
# branch convention
# --------------------------------------------------------------------------- #


def test_card_branch_defaults_to_wt_prefix():
    assert kanban._card_branch(card("t_abc", branch=None)) == "wt/t_abc"


def test_card_branch_uses_recorded_branch():
    assert kanban._card_branch(card("t_abc", branch="feature/x")) == "feature/x"


def test_unstarted_branch_goes_to_skipped_not_close():
    """SEV-1: ancestor-of-main is NOT proof the work landed.

    A freshly created worktree branch points at the main tip it forked from, so
    once main advances it reads as merged by ancestry alone. Reconcile proposed
    closing three cards in exactly this state while they were running.
    """
    cards = [card("c1", status="review", branch="wt/c1")]
    g = {"c1": {"branch_exists": True, "is_merged": True, "commits_ahead": 0,
                "landed_via_merge": False}}
    plan = kanban.reconcile_plan(cards, g, dead_days=14, now=NOW)
    assert plan["close"] == []
    assert [e["id"] for e in plan["skipped"]] == ["c1"]
    assert "never landed" in plan["skipped"][0]["reason"]


def test_landed_branch_still_closes():
    """The fix must not regress the behaviour reconcile exists for."""
    cards = [card("c1", status="review", branch="wt/c1")]
    g = {"c1": {"branch_exists": True, "is_merged": True, "commits_ahead": 0,
                "landed_via_merge": True}}
    plan = kanban.reconcile_plan(cards, g, dead_days=14, now=NOW)
    assert plan["close"] == ["c1"]
    assert plan["skipped"] == []


def test_active_card_is_never_closed_even_when_landed():
    """A running card is never closed, whatever its branch looks like."""
    cards = [card("c1", status="running", branch="wt/c1")]
    g = {"c1": {"branch_exists": True, "is_merged": True, "commits_ahead": 0,
                "landed_via_merge": True}}
    plan = kanban.reconcile_plan(cards, g, dead_days=14, now=NOW)
    assert plan["close"] == []
    assert [e["id"] for e in plan["skipped"]] == ["c1"]


# --------------------------------------------------------------------------- #
# Project attribution by repo PATH (not board slug)
# --------------------------------------------------------------------------- #
#
# The shared `default` board holds cards from MANY projects. A card belongs to a
# project when its workspace_path is that project's repo or lives under it
# (worktrees at <repo>/.worktrees/<card>), after both are resolved + normalised.
# Board is never the attribution rule.


def _proj(name, repo):
    """A minimal project-shaped object exposing ``repo``."""
    return SimpleNamespace(name=name, repo=repo)


def _att(cid, workspace_path):
    """A card carrying just the attribution field + id."""
    return {"id": cid, "workspace_path": workspace_path}


def test_attribute_exact_repo_with_tilde_mismatch(monkeypatch, tmp_path):
    """A workspace_path given as an ABSOLUTE path attributes to a project whose
    repo is the same path spelled with a leading ``~`` — the two must be the
    SAME project despite the tilde/absolute difference.

    ``~`` is pinned to a tmp home so the tilde form resolves independently of
    the host's real ``$HOME`` (a fake HOME in CI must not break the match).
    """
    home = tmp_path / "home"
    import os as _os
    monkeypatch.setattr(
        _os.path, "expanduser",
        lambda p: str(home) + p[1:] if p.startswith("~") else p,
    )
    projects = [_proj("flightdeck", f"{home}/dev/flightdeck")]
    assert kanban.project_for_card(
        _att("t1", f"{home}/dev/flightdeck"), projects
    ) is projects[0]


def test_shared_board_splits_cards_across_projects_by_workspace():
    """A shared board holding cards from three projects splits each card to its
    own project by workspace_path, not by board (all three share the board)."""
    projects = [
        _proj("flightdeck", "/Users/desac/dev/flightdeck"),
        _proj("hscc", "/Users/desac/dev/hscc"),
        _proj("sphoin", "/Users/desac/dev/sphoin_engine"),
    ]
    cards = [
        _att("a", "/Users/desac/dev/flightdeck"),
        _att("b", "/Users/desac/dev/hscc/.worktrees/t_b"),
        _att("c", "/Users/desac/dev/sphoin_engine/.worktrees/t_c"),
    ]
    by_id = {c["id"]: kanban.project_for_card(c, projects) for c in cards}
    assert by_id["a"] is projects[0]
    assert by_id["b"] is projects[1]
    assert by_id["c"] is projects[2]


def test_worktree_path_attributes_to_parent_repo():
    """A card whose workspace is a worktree under a repo belongs to that repo's
    project — the common live case (`<repo>/.worktrees/<card>`)."""
    projects = [_proj("hscc", "/Users/desac/dev/hscc")]
    result = kanban.project_for_card(
        _att("t", "/Users/desac/dev/hscc/.worktrees/t_X"), projects
    )
    assert result is projects[0]


def test_null_and_empty_workspace_path_are_unattributed():
    """A card with NULL/empty workspace_path lands in UNATTRIBUTED rather than
    vanishing or being guessed into a project."""
    projects = [_proj("hscc", "/Users/desac/dev/hscc")]
    assert kanban.project_for_card(_att("a", None), projects) is kanban.UNATTRIBUTED
    assert kanban.project_for_card(_att("b", ""), projects) is kanban.UNATTRIBUTED
    # a card dict missing the key entirely is also unattributed
    assert kanban.project_for_card({"id": "c"}, projects) is kanban.UNATTRIBUTED


def test_unrecognized_workspace_path_is_unattributed():
    """A workspace_path that resolves to no registered repo is UNATTRIBUTED,
    never silently dropped and never guessed into a project."""
    projects = [_proj("hscc", "/Users/desac/dev/hscc")]
    result = kanban.project_for_card(
        _att("t", "/Users/desac/dev/other-project/.worktrees/t_X"), projects
    )
    assert result is kanban.UNATTRIBUTED


def test_board_is_never_the_attribution_rule():
    """Attribution ignores board entirely: a card from another board whose
    workspace_path matches a project's repo is still attributed BY PATH."""
    projects = [_proj("hscc", "/Users/desac/dev/hscc")]
    card_h = _att("t", "/Users/desac/dev/hscc")
    card_h["board"] = "unrelated-board"
    assert kanban.project_for_card(card_h, projects) is projects[0]


def test_path_comparison_trailing_slash_insensitive():
    """A trailing slash on either side does not change the attribution."""
    projects = [_proj("hscc", "/Users/desac/dev/hscc/")]
    assert kanban.project_for_card(
        _att("a", "/Users/desac/dev/hscc/"), projects
    ) is projects[0]
    assert kanban.project_for_card(
        _att("b", "/Users/desac/dev/hscc"), projects
    ) is projects[0]


def test_path_comparison_rejects_sibling_prefix():
    """`/dev/hscc-evil` and `/dev/hscc-2` must NOT match `/dev/hscc` — prefix
    matching is segment-based, so a sibling project is never folded in."""
    projects = [_proj("hscc", "/Users/desac/dev/hscc")]
    assert kanban.project_for_card(
        _att("a", "/Users/desac/dev/hscc-evil/.worktrees/t_a"), projects
    ) is kanban.UNATTRIBUTED
    assert kanban.project_for_card(
        _att("b", "/Users/desac/dev/hscc2"), projects
    ) is kanban.UNATTRIBUTED


def test_path_comparison_symlink_insensitive(tmp_path):
    """Resolving symlinks: a workspace reached through a symlinked repo dir maps
    to the real repo path."""
    real = tmp_path / "real-repo"
    real.mkdir()
    link = tmp_path / "link"  # a symlink to real-repo
    link.symlink_to(real)
    projects = [_proj("hscc", str(real))]
    # workspace given via the symlink path
    result = kanban.project_for_card(_att("a", str(link)), projects)
    assert result is projects[0]


# --------------------------------------------------------------------------- #
# find_card — one card across every board; unknown -> None
# --------------------------------------------------------------------------- #

def _task(cid, **over):
    base = dict(
        id=cid, title="some task", status="review", assignee="coder",
        branch_name="wt/" + cid, created_at=1, started_at=None,
        completed_at=None, workspace_kind="worktree",
        last_heartbeat_at=None, body=None, workspace_path=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_find_card_returns_matching_card(monkeypatch):
    kb = _make_fake_kb(
        boards=("hscc", "other"),
        tasks_by_board={
            "hscc": [_task("c1")],
            "other": [_task("c2")],
        },
    )
    _stub_kanban_db(kb, monkeypatch)
    card = kanban.find_card("c2")
    assert card is not None
    assert card["id"] == "c2"
    assert card["board"] == "other"
    assert "last_heartbeat_at" in card


def test_find_card_returns_none_when_absent(monkeypatch):
    kb = _make_fake_kb(boards=("hscc", "other"), tasks_by_board={})
    _stub_kanban_db(kb, monkeypatch)
    assert kanban.find_card("ghost") is None


def test_find_card_with_explicit_boards(monkeypatch):
    kb = _make_fake_kb(
        boards=("hscc", "other"),
        tasks_by_board={"hscc": [_task("c1")], "other": [_task("c2")]},
    )
    _stub_kanban_db(kb, monkeypatch)
    # Searching only "hscc" must not find c2 even though it exists on "other".
    assert kanban.find_card("c2", boards=["hscc"]) is None


def test_list_boards_searched_returns_all_boards(monkeypatch):
    kb = _make_fake_kb(boards=("hscc", "other"))
    _stub_kanban_db(kb, monkeypatch)
    assert kanban.list_boards_searched() == ["hscc", "other"]


def test_list_boards_searched_honours_explicit_boards():
    assert kanban.list_boards_searched(boards=["a", "b"]) == ["a", "b"]


# --------------------------------------------------------------------------- #
# status_changed_at / status_duration_seconds — timing facts
# --------------------------------------------------------------------------- #

NOW = 10_000


def test_status_changed_at_uses_most_recent_transition_event():
    card = _card_dict(created_at=1000, started_at=3000)
    events = [
        {"kind": "created", "created_at": 1000},
        {"kind": "claimed", "created_at": 3000},
        {"kind": "submitted_for_review", "created_at": 8000},
        {"kind": "heartbeat", "created_at": 9500},  # not a transition — ignored
    ]
    assert kanban.status_changed_at(card, events, now=NOW) == 8000


def test_status_changed_at_ignores_non_transition_events():
    card = _card_dict(created_at=1000, started_at=3000)
    events = [{"kind": "heartbeat", "created_at": 9000}]
    assert kanban.status_changed_at(card, events, now=NOW) == 3000  # falls back to started_at


def test_status_changed_at_falls_back_to_created_at():
    card = _card_dict(created_at=1000, started_at=None)
    assert kanban.status_changed_at(card, [], now=NOW) == 1000


def test_status_changed_at_none_when_no_time_at_all():
    card = _card_dict(created_at=None, started_at=None)
    assert kanban.status_changed_at(card, []) is None


def test_status_duration_seconds_is_now_minus_status_change():
    card = _card_dict(created_at=1000, started_at=3000)
    events = [{"kind": "claimed", "created_at": 3000}]
    assert kanban.status_duration_seconds(card, events, now=NOW) == 7000


def test_status_duration_seconds_clamps_negative_to_zero():
    # A status-change time in the future (clock skew) must clamp to 0, not go
    # negative.
    card = _card_dict(created_at=20000)
    assert kanban.status_duration_seconds(card, [], now=NOW) == 0


def _card_dict(**over):
    base = {
        "id": "c1", "title": "t", "status": "review", "assignee": "coder",
        "board": "hscc", "branch": "wt/c1", "created_at": None,
        "started_at": None, "completed_at": None, "last_heartbeat_at": None,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Freshness watermark — the "data as of <time>" seam (brainstorm Gap 1)
# --------------------------------------------------------------------------- #

def _make_watermark_kb(watermarks):
    """A stand-in for hermes_cli.kanban_db whose connect().execute() returns
    each board's MAX(ts) — duplicating the tiny surface list_board_watermarks
    uses: list_boards, connect (with execute/fetchone/close)."""
    class _Conn:
        def __init__(self, slug):
            self.slug = slug

        def execute(self, sql, *args):
            return self

        def fetchone(self):
            ts = watermarks.get(self.slug)
            return (ts,) if ts is not None else (None,)

        def close(self):
            pass

    def list_boards():
        return [{"slug": b} for b in watermarks]

    def connect(board=None):
        return _Conn(board)

    return SimpleNamespace(list_boards=list_boards, connect=connect)


def test_list_board_watermarks_returns_each_boards_newest_change():
    kb = _make_watermark_kb({"hscc": 5000, "pulse": 9000})
    wm = kanban.list_board_watermarks(_kdb=kb)
    assert wm == {"hscc": 5000, "pulse": 9000}


def test_list_board_watermarks_skips_empty_unreadable_boards():
    # An empty board (MAX(ts) NULL) dates nothing — its quietness must not
    # read as a stale timestamp.
    kb = _make_watermark_kb({"hscc": 5000, "quiet": None})
    wm = kanban.list_board_watermarks(_kdb=kb)
    assert wm == {"hscc": 5000}


def test_freshness_watermark_is_the_oldest_contributing_board():
    wm = {"fresh": 9000, "old": 5000}
    assert kanban.freshness_watermark(["fresh", "old"], watermarks=wm) == 5000


def test_freshness_watermark_ignores_non_contributing_quiet_boards():
    """An empty board (no watermark entry) in the full set must not lower the
    digest watermark — only boards that actually contributed cards count."""
    wm = {"fresh": 9000, "old": 5000, "empty": None}
    assert kanban.freshness_watermark(["fresh", "empty"], watermarks=wm) == 9000


def test_freshness_watermark_none_when_no_contributing_board_dateable():
    assert kanban.freshness_watermark([], watermarks={}) is None
    assert kanban.freshness_watermark(["quiet"], watermarks={"quiet": None}) is None

