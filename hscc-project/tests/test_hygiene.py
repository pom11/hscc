"""Tests for flightdeck.core.hygiene — pure decision logic + injectable apply.

Covers the three decay modes and the "nothing mutates without --apply"
contract. All external calls (git subprocess, hermes kanban DB) are stubbed;
nothing here touches a real repo, board, or the network, so the suite stays
fast and deterministic.
"""

from flightdeck.core import hygiene


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def card(
    cid="t_abc",
    title="some task",
    status="todo",
    board="hscc",
    branch=None,
    assignee="coder",
    created_at=1000,
):
    """A minimal flightdeck card dict (same shape as kanban.list_cards)."""
    if branch is None:
        branch = f"wt/{cid}"
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch,
        "assignee": assignee,
        "created_at": created_at,
    }


def facts(card_id, *, branch_exists=True, is_merged=False, commits_ahead=3):
    return {card_id: {"branch_exists": branch_exists, "is_merged": is_merged, "commits_ahead": commits_ahead}}


def merged_facts(card_id, commits=3):
    return facts(card_id, branch_exists=True, is_merged=True, commits_ahead=commits)


# --------------------------------------------------------------------------- #
# Duplicates — catches near-identical, ignores genuinely different.
# --------------------------------------------------------------------------- #


def test_duplicates_catches_identical_titles():
    cards = [
        card("a", "MCP Server Core", created_at=100),
        card("b", "MCP Server Core", created_at=200),
    ]
    groups = hygiene.find_duplicates(cards)
    assert len(groups) == 1
    g = groups[0]
    # Newest is kept; the older one is proposed for archive.
    assert g["keep"]["id"] == "b"
    assert [c["id"] for c in g["archive"]] == ["a"]


def test_duplicates_catch_re_do_variants():
    cards = [
        card("a", "Card 3: Stripe feasibility summary (go/no-go, effort, risks, value)", created_at=100),
        card("b", "Card 3 RE-DO: Stripe feasibility summary (go/no-go, effort, risks, value)", created_at=300),
    ]
    groups = hygiene.find_duplicates(cards)
    assert len(groups) == 1
    assert groups[0]["keep"]["id"] == "b"
    assert groups[0]["archive"][0]["id"] == "a"


def test_duplicates_ignore_genuinely_different_titles():
    cards = [
        card("a", "MCP Server Core"),
        card("b", "Deploy PostgreSQL"),
    ]
    assert hygiene.find_duplicates(cards) == []


def test_duplicates_do_not_cross_boards():
    cards = [
        card("a", "MCP Server Core", board="hscc"),
        card("b", "MCP Server Core", board="other"),
    ]
    assert hygiene.find_duplicates(cards) == []


def test_duplicates_ignore_platform_family():
    """The real Soconn C-XX family are distinct cards sharing a long suffix;
    they must not be merged as duplicates."""
    a = card("a", "Soconn C-01 Instagram — API, SDK, MCP, Deeplink, ToS", created_at=100)
    b = card("b", "Soconn C-02 TikTok — API, SDK, MCP, Deeplink, ToS", created_at=110)
    assert hygiene.find_duplicates([a, b]) == []
    # But a genuine re-attempt of the SAME card is a duplicate.
    c = card("c", "Soconn C-01-R Instagram — API, SDK, MCP, Deeplink, ToS", created_at=200)
    groups = hygiene.find_duplicates([a, c])
    assert len(groups) == 1
    assert groups[0]["keep"]["id"] == "c"
    assert groups[0]["archive"][0]["id"] == "a"


def test_duplicates_threshold_is_injectable():
    a = card("a", "MCP Server Core")
    b = card("b", "MCP Server Core v2")
    # Default 0.88 catches it (ratio 0.909); a stricter threshold 0.95 does not.
    assert len(hygiene.find_duplicates([a, b])) == 1
    assert hygiene.find_duplicates([a, b], threshold=0.95) == []
    # A very loose threshold can split a near-tie in the other direction.
    assert len(hygiene.find_duplicates([a, b], threshold=0.5)) == 1


def test_duplicates_exclude_archived_cards():
    cards = [
        card("a", "MCP Server Core", status="archived", created_at=100),
        card("b", "MCP Server Core", created_at=200),
    ]
    assert hygiene.find_duplicates(cards) == []


def test_duplicates_does_not_mutate_inputs():
    cards = [card("a", "MCP Server Core"), card("b", "MCP Server Core")]
    before = [dict(c) for c in cards]
    hygiene.find_duplicates(cards)
    assert cards == before


# --------------------------------------------------------------------------- #
# Triage trap — detected, and rescue preserves the branch.
# --------------------------------------------------------------------------- #


def test_triage_card_detected():
    cards = [card("t1", "Some spec", status="triage", branch="wt/t1")]
    traps = hygiene.find_triage_traps(cards, {})
    assert len(traps) == 1
    assert traps[0]["card"]["id"] == "t1"
    assert traps[0]["branch"] == "wt/t1"


def test_triage_rescue_preserves_branch_and_assignee():
    cards = [card("t1", "MCP Server Core", status="triage", assignee="coder", branch="wt/t1")]
    traps = hygiene.find_triage_traps(cards, {})
    rc = traps[0]["recreate"]
    assert rc["branch"] == "wt/t1"
    assert rc["assignee"] == "coder"
    assert rc["title"] == "MCP Server Core"


def test_triage_non_triage_cards_ignored():
    cards = [card("a", "todo"), card("b", "running"), card("c", "done")]
    assert hygiene.find_triage_traps(cards, {}) == []


def test_triage_branch_work_flag_from_git_facts():
    cards = [card("t1", "spec", status="triage", branch="wt/t1")]
    g = facts("t1", branch_exists=True, is_merged=False, commits_ahead=5)
    traps = hygiene.find_triage_traps(cards, g)
    assert traps[0]["branch_has_work"] is True
    assert traps[0]["commits_ahead"] == 5


def test_triage_branch_no_work_flag():
    cards = [card("t1", "spec", status="triage", branch="wt/t1")]
    g = facts("t1", branch_exists=True, is_merged=False, commits_ahead=0)
    traps = hygiene.find_triage_traps(cards, g)
    assert traps[0]["branch_has_work"] is False


# --------------------------------------------------------------------------- #
# Stale worktrees — detected only when branch is merged.
# --------------------------------------------------------------------------- #


def _wt(cid, worktree=None):
    return {"card_id": cid, "worktree": worktree or f"/repo/.worktrees/{cid}", "branch": f"wt/{cid}"}


def test_stale_worktree_only_when_branch_merged():
    cards = [card("c1", status="done", branch="wt/c1")]
    g = merged_facts("c1")  # is_merged=True
    closed_ids = {c["id"] for c in cards if c["status"] in hygiene.CLOSED_STATUSES}
    stale = hygiene.find_stale_worktrees([_wt("c1")], g, closed_ids)
    assert len(stale) == 1
    assert stale[0]["card_id"] == "c1"
    assert stale[0]["branch"] == "wt/c1"


def test_stale_worktree_skipped_when_branch_unmerged():
    cards = [card("c1", status="done", branch="wt/c1")]
    g = facts("c1", branch_exists=True, is_merged=False, commits_ahead=3)
    closed_ids = {c["id"] for c in cards if c["status"] in hygiene.CLOSED_STATUSES}
    assert hygiene.find_stale_worktrees([_wt("c1")], g, closed_ids) == []


def test_stale_worktree_skipped_when_card_open():
    """A worktree whose card is still running/todo is never stale, even if the
    branch happens to be merged (the operator may still want the checkout)."""
    cards = [card("c1", status="running", branch="wt/c1")]
    g = merged_facts("c1")
    closed_ids = {c["id"] for c in cards if c["status"] in hygiene.CLOSED_STATUSES}
    assert hygiene.find_stale_worktrees([_wt("c1")], g, closed_ids) == []


def test_stale_worktree_detected_for_archived_card():
    """An ARCHIVED card's worktree is stale: archived is closed, and the card
    is excluded from the non-archived read, so closed_ids carries it."""
    cards = []  # archived card is not in the non-archived read
    g = merged_facts("c1")
    stale = hygiene.find_stale_worktrees([_wt("c1")], g, closed_ids={"c1"})
    assert len(stale) == 1
    assert stale[0]["card_id"] == "c1"


def test_stale_worktree_maps_board_for_repo():
    cards = [card("c1", status="done", board="hscc", branch="wt/c1")]
    g = merged_facts("c1")
    closed_ids = {"c1"}
    _wt_entry = {"card_id": "c1", "worktree": "/repo/.worktrees/c1", "branch": "wt/c1", "board": "hscc"}
    stale = hygiene.find_stale_worktrees([_wt_entry], g, closed_ids)
    assert stale[0]["board"] == "hscc"


# --------------------------------------------------------------------------- #
# build_plan — clean inputs propose nothing.
# --------------------------------------------------------------------------- #


def test_build_plan_clean_proposes_nothing():
    cards = [card("a", "MCP Server Core"), card("b", "Deploy PostgreSQL")]
    g = {"a": {"branch_exists": True, "is_merged": False, "commits_ahead": 2},
         "b": {"branch_exists": True, "is_merged": False, "commits_ahead": 1}}
    plan = hygiene.build_plan(cards, g, worktrees=[], closed_ids=set(), threshold=0.88)
    assert plan == {"duplicates": [], "triage": [], "stale_worktrees": []}


def test_build_plan_finds_all_three_modes():
    cards = [
        card("dup1", "MCP Server Core", created_at=100),
        card("dup2", "MCP Server Core", created_at=200),
        card("trp1", "lost spec", status="triage", branch="wt/trp1"),
        card("wt1", "done work", status="done", branch="wt/wt1"),
        card("live", "running work", status="running", branch="wt/live"),
    ]
    g = {
        "dup1": {"branch_exists": True, "is_merged": True, "commits_ahead": 4},
        "dup2": {"branch_exists": True, "is_merged": True, "commits_ahead": 4},
        "trp1": {"branch_exists": True, "is_merged": False, "commits_ahead": 7},
        "wt1": {"branch_exists": True, "is_merged": True, "commits_ahead": 3},
        "live": {"branch_exists": True, "is_merged": False, "commits_ahead": 0},
    }
    closed_ids = {"wt1"}
    plan = hygiene.build_plan(cards, g, worktrees=[_wt("wt1")], closed_ids=closed_ids)
    assert len(plan["duplicates"]) == 1
    assert len(plan["triage"]) == 1
    assert len(plan["stale_worktrees"]) == 1
    assert plan["stale_worktrees"][0]["card_id"] == "wt1"


# --------------------------------------------------------------------------- #
# Apply — mutates only through injected handles.
# --------------------------------------------------------------------------- #


def _make_fake_kdb():
    """A stand-in for hermes_cli.kanban_db exposing the apply path: connect,
    archive_task, create_task, get_task. Archive records what was archived;
    create_task returns a deterministic new id and records created specs.
    Uses SimpleNamespace so the callables are not self-bound as methods."""
    from types import SimpleNamespace

    state = {
        "archived": [],
        "created": [],
    }

    class _Conn:
        def __init__(self, board=None):
            self.board = board

        def close(self):
            pass

    def connect(board=None):
        return _Conn(board)

    def archive_task(conn, task_id):
        state["archived"].append(task_id)
        return True

    def create_task(conn, **kw):
        new_id = "t_new_{}".format(len(state["created"]) + 1)
        state["created"].append({**kw, "id": new_id})
        return new_id

    def get_task(conn, task_id):
        return SimpleNamespace(id=task_id, body=f"body of {task_id}")

    return SimpleNamespace(
        connect=connect,
        archive_task=archive_task,
        create_task=create_task,
        get_task=get_task,
    ), state


def test_apply_duplicates_archives_all_but_keeper():
    plan = {
        "duplicates": [
            {"board": "hscc", "keep": card("b", created_at=200),
             "archive": [card("a", created_at=100)]}
        ],
        "triage": [],
    }
    _kdb, state = _make_fake_kdb()
    summary = hygiene.apply_card_plan(plan, _kdb=_kdb)
    assert state["archived"] == ["a"]  # keeper not archived
    assert summary["archived_duplicates"] == ["a"]
    assert state["created"] == []


def test_apply_triage_archives_and_recreates_preserving_branch():
    trapped = card("t1", "MCP Server Core", status="triage", branch="wt/t1", assignee="coder")
    plan = {"duplicates": [], "triage": [
        {"card": trapped, "branch": "wt/t1",
         "recreate": {"title": "MCP Server Core", "branch": "wt/t1", "assignee": "coder"}}
    ]}
    _kdb, state = _make_fake_kdb()
    summary = hygiene.apply_card_plan(plan, _kdb=_kdb)
    assert state["archived"] == ["t1"]
    assert len(state["created"]) == 1
    created = state["created"][0]
    # The recreated card keeps the branch reference and workspace kind.
    assert created["branch_name"] == "wt/t1"
    assert created["workspace_kind"] == "worktree"
    assert created["title"] == "MCP Server Core"
    assert created["assignee"] == "coder"
    assert summary["recreated"] == [{"old_id": "t1", "new_id": "t_new_1"}]


def test_apply_worktree_cleanup_removes_merged_worktree_and_branch():
    stale = [
        {"card_id": "c1", "board": "hscc", "repo": "/repo",
         "worktree": "/repo/.worktrees/c1", "branch": "wt/c1"}
    ]
    calls = []

    def fake_run(cmd, repo):
        calls.append((cmd, repo))
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    result = hygiene.apply_worktree_cleanup(stale, _run=fake_run)
    assert result["removed"] == [{"card_id": "c1", "worktree": "/repo/.worktrees/c1"}]
    assert result["failed"] == []
    # worktree remove, then safe branch delete — never rm -rf. The repo the
    # cleanup acts on is the one carried on the entry (repo-path attribution).
    assert calls[0][0] == ["git", "worktree", "remove", "/repo/.worktrees/c1"]
    assert calls[0][1] == "/repo"
    assert calls[1][0] == ["git", "branch", "-d", "wt/c1"]


def test_apply_worktree_cleanup_uses_entry_repo_not_board():
    """A stale worktree is cleaned up in the repo its workspace_path resolves
    to, not the repo the board maps to. The entry carries ``repo`` — the
    collector attributed it by path — so cleanup must act there."""
    stale = [
        # board maps to /flightdeck, but the worktree lives under /hscc
        {"card_id": "c1", "board": "flightdeck", "repo": "/hscc",
         "worktree": "/hscc/.worktrees/c1", "branch": "wt/c1"}
    ]
    repos = []

    def fake_run(cmd, repo):
        repos.append(repo)
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    hygiene.apply_worktree_cleanup(stale, _run=fake_run)
    # Every git call ran in /hscc, never the board's repo.
    assert repos and all(r == "/hscc" for r in repos)


def test_apply_worktree_cleanup_skips_entry_with_no_repo():
    """A stale worktree with no resolvable repo is reported failed and never
    acted on — we do not touch what we cannot verify."""
    stale = [{"card_id": "c1", "worktree": "/repo/.worktrees/c1", "branch": "wt/c1"}]
    calls = []

    def fake_run(cmd, repo):
        calls.append((cmd, repo))
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    result = hygiene.apply_worktree_cleanup(stale, _run=fake_run)
    assert result["removed"] == []
    assert result["failed"][0]["card_id"] == "c1"
    assert calls == []  # nothing invoked


def test_apply_worktree_cleanup_reports_failure_without_removing():
    stale = [
        {"card_id": "c1", "board": "hscc", "repo": "/repo",
         "worktree": "/repo/.worktrees/c1", "branch": "wt/c1"}
    ]
    calls = []

    def fake_run(cmd, repo):
        calls.append((cmd, repo))
        return type("CP", (), {"returncode": 128, "stdout": "", "stderr": "worktree is dirty"})()

    result = hygiene.apply_worktree_cleanup(stale, _run=fake_run)
    assert result["removed"] == []
    assert result["failed"][0]["card_id"] == "c1"
    # No branch delete was attempted after a failed worktree remove.
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# completion_guard — refuse done/blocked when the worktree has uncommitted
# changes, allow a clean worktree. The acceptance test for the lost-work fix:
# it FAILS if the uncommitted-changes guard is removed.
# --------------------------------------------------------------------------- #


def _guard_card(workspace_path="/repo/.worktrees/t_abc", cid="MCP-REL"):
    """A worktree-kind card dict carrying its workspace path (and id/title
    used in the refusal message)."""
    c = card(cid, status="running", branch=f"wt/{cid}")
    c["workspace_path"] = workspace_path
    c["title"] = "expose the release as an MCP tool"
    return c


def test_completion_guard_refuses_uncommitted_changes():
    """A card whose worktree reports any uncommitted change is REFUSED with a
    message naming that file. Fails if the uncommitted-changes guard is removed."""
    card = _guard_card()

    def fake_run(cmd, repo):
        assert cmd == ["git", "status", "--porcelain"]
        assert repo == "/repo/.worktrees/t_abc"
        return type("CP", (), {
            "returncode": 0,
            "stdout": " M flightdeck/mcp_server.py\n?? unattached_new.py\n",
            "stderr": "",
        })()

    refusal = hygiene.completion_guard(card, _run=fake_run)
    assert refusal is not None
    assert "cannot complete MCP-REL" in refusal
    assert "flightdeck/mcp_server.py" in refusal
    assert "commit them first" in refusal


def test_completion_guard_clean_worktree_completes_normally():
    """A clean worktree returns None (completion proceeds). Fails if the
    uncommitted-changes guard is removed (by refusing clean cards)."""
    card = _guard_card()

    def fake_run(cmd, repo):
        assert cmd == ["git", "status", "--porcelain"]
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    assert hygiene.completion_guard(card, _run=fake_run) is None


def test_completion_guard_passes_scratch_card_without_worktree():
    """A card with no workspace_path (scratch) has no worktree whose changes
    could be lost, so it passes without ever consulting git."""
    called = []

    def fake_run(cmd, repo):
        called.append(cmd)
        return type("CP", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    assert hygiene.completion_guard(card("t_s", status="running"), _run=fake_run) is None
    assert called == []  # no git invoked


def test_completion_guard_degrades_on_unreadable_worktree():
    """An unreadable worktree (non-repo) degrades to [] from
    uncommitted_files, so the guard treats it as clean rather than refusing on
    a guess — it never blocks a verified-clean completion."""
    card = _guard_card()

    def fake_run(cmd, repo):
        return type("CP", (), {"returncode": 128, "stdout": "", "stderr": "not a repo"})()

    assert hygiene.completion_guard(card, _run=fake_run) is None
