"""Tests for `flightdeck standup` — one-shot digest and --watch mode.

The contract that matters: a single renderer (:func:`render_digest`) draws both
the one-shot report and every watch frame, so the two formats can never drift.
Data is gathered behind a single call (:func:`gather_data`) that the standup
card can supply.

Every external surface (registry, kanban board, git, clock, sleep) is
injected — no test touches a real board, repo, the network, or real time, and
the suite stays fast.
"""

import os
import sys
import time

import pytest
import yaml

from flightdeck.commands import standup as cmd
from flightdeck.core import git_state, kanban, registry

# A clock advanced a little past the stale threshold (45m) so an in-flight
# card with zero commits trips STALE without needing 2700 real seconds.
_STALE_AGE = cmd.kanban.DEFAULT_STALE_THRESHOLD_SECONDS + 10

NOW = 1_700_000_000
BEFORE = NOW - _STALE_AGE


def _project(name="hscc", board="hscc", repo="/repo", verify=None):
    return registry.Project(name=name, repo=repo, board=board, verify=verify)


def _hcard(cid, title="task", status="review", board="hscc", branch=None,
           started_at=BEFORE, workspace_path: "str | None" = "/repo"):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch or f"wt/{cid}",
        "assignee": "coder",
        "created_at": BEFORE,
        "started_at": started_at,
        "workspace_path": workspace_path,
    }


def _args(**overrides):
    """A minimal argparse Namespace carrying the injectable handles."""
    import argparse

    base = {
        "registry": "/tmp/reg.yaml",
        "watch": False,
        "interval": 30,
        "run": None,
        "now": None,
        "sleep": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# --------------------------------------------------------------------------- #
# data gathering — classification lands each card in the right bucket
# --------------------------------------------------------------------------- #


def _install_git(monkeypatch, *, branch_exists=True, is_merged=False, commits_ahead=0):
    """Point the git seams at fixed facts for every branch.

    Also marks the fake project's repo path as existing on disk (``_isdir`` ->
    True), so the happy path treats it as a readable project rather than an
    UNREADABLE one — these tests fake the git facts, so the repo counts as read.
    """
    monkeypatch.setattr(git_state, "branch_exists", lambda *a, **k: branch_exists)
    monkeypatch.setattr(git_state, "is_merged", lambda *a, **k: is_merged)
    monkeypatch.setattr(git_state, "commits_ahead", lambda *a, **k: commits_ahead)
    monkeypatch.setattr(cmd, "_isdir", lambda *a, **k: True)


def test_needs_you_card_gains_verify_line(monkeypatch):
    """A review/blocked card with an unmerged branch lands in NEEDS YOU and
    carries the registry verify command so the renderer shows VERIFY:."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project(verify="cd /repo && run_tests")]
    )
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("t1", status="review")])
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml")
    assert len(data["needs_you"]) == 1
    assert data["needs_you"][0]["verify"] == "cd /repo && run_tests"

    out = cmd.render_digest(data)
    assert "NEEDS YOU (1)" in out
    assert "VERIFY: cd /repo && run_tests" in out


def test_stale_running_zero_commits_old(monkeypatch):
    """An in-flight card past the threshold with zero commits lands in STALE."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("t9", status="running")])
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=0)

    data = cmd.gather_data("/tmp/reg.yaml")
    assert len(data["stale"]) == 1
    assert data["running"] == []


def test_running_card_with_commits_not_stale(monkeypatch):
    """A running card WITH commits is RUNNING, not STALE."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("t3", status="running")])
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=5)

    data = cmd.gather_data("/tmp/reg.yaml")
    assert data["stale"] == []
    assert len(data["running"]) == 1


def test_project_without_board_contributes_cards_by_workspace(monkeypatch):
    """A project with no BOARD but a repo still receives cards whose
    workspace_path resolves to its repo — attribution is by path, not board.
    The board-less project is never omitted: it appears in DRIFT with its board
    marked unknown."""
    called = {"n": 0}

    def fake_list_cards(**kw):
        called["n"] += 1
        # A card whose workspace_path matches project x's repo (/x).
        return [_hcard("t1", status="running", workspace_path="/x")]

    monkeypatch.setattr(registry, "load_registry", lambda path: [registry.Project(name="x", repo="/x")])
    monkeypatch.setattr(kanban, "list_cards", fake_list_cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=5)
    data = cmd.gather_data("/tmp/reg.yaml")
    # Exactly one board pass (not per-project), and the card attributes by path.
    assert called["n"] == 1
    assert len(data["running"]) == 1
    assert data["running"][0]["id"] == "t1"
    # Per-project sections still surface the project — never silently dropped.
    assert len(data["drift"]) == 1
    assert data["drift"][0]["project"] == "x"
    assert data["drift"][0]["board"] is None


def test_rows_sorted_by_board_then_id(monkeypatch):
    """Digest rows are deterministic: sorted by board then card id.

    Attribution is by workspace_path, so two projects on the same shared board
    split correctly; the sort then orders by (board, id) for stable output.
    """
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [
            _project(board="b", repo="/repo-b"),
            _project(board="a", repo="/repo-a"),
        ],
    )

    # One pass over the shared board; each card's workspace_path decides its
    # project (t2/t1 -> repo-b, t4 -> repo-a).
    cards = [
        _hcard("t2", board="b", workspace_path="/repo-b"),
        _hcard("t1", board="b", workspace_path="/repo-b"),
        _hcard("t4", board="a", workspace_path="/repo-a"),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    # review cards with unmerged branches -> all land in needs_you
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    ids = [r["id"] for r in data["needs_you"]]
    assert ids == ["t4", "t1", "t2"]  # sorted by (board, id): a before b


def test_merged_branch_card_never_appears_in_needs_you(monkeypatch):
    """THE core rule: a review/blocked card whose branch is already an ancestor
    of main NEVER lands in NEEDS YOU. The merged-branch exclusion is the whole
    point — it is what turned a 28-card phantom backlog into an honest number.
    ``kanban.classify`` returns ``landed`` for a merged branch, and ``landed``
    is never ``needs_you``."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("tm", status="review")]
    )
    # Branch exists AND is merged -> classify = landed, not needs_you.
    _install_git(monkeypatch, branch_exists=True, is_merged=True, commits_ahead=0)

    data = cmd.gather_data("/tmp/reg.yaml")
    assert data["needs_you"] == []
    assert all(r["id"] != "tm" for r in data["needs_you"])


def test_shared_board_splits_cards_by_workspace_path(monkeypatch):
    """The headline bug: the shared `default` board holds cards from many
    projects. Each card must be attributed to the project whose repo its
    workspace_path lives under — NOT to whatever project owns the board slug."""
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [
            _project(name="flightdeck", repo="/Users/desac/dev/flightdeck"),
            _project(name="hscc", repo="/Users/desac/dev/hscc"),
            _project(name="sphoin", repo="/Users/desac/dev/sphoin_engine"),
        ],
    )
    # All THREE cards share one board; their workspace_paths name their project.
    cards = [
        _hcard("a", status="review", board="default",
               workspace_path="/Users/desac/dev/flightdeck"),
        _hcard("b", status="review", board="default",
               workspace_path="/Users/desac/dev/hscc/.worktrees/t_b"),
        _hcard("c", status="review", board="default",
               workspace_path="/Users/desac/dev/sphoin_engine/.worktrees/t_c"),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    by_id = {r["id"]: r for r in data["needs_you"]}
    assert set(by_id) == {"a", "b", "c"}
    # Each card is checked against ITS OWN project's repo (branch matches exist).
    assert by_id["a"]["board"] == "default"
    assert by_id["b"]["board"] == "default"
    assert by_id["c"]["board"] == "default"
    # All three surfaced — a shared board is no longer a blind digest.


def test_unattributed_card_is_surfaced_not_dropped(monkeypatch):
    """A card whose workspace_path is empty/NULL (or matches no repo) is
    surfaced as UNATTRIBUTED under RUNNING, never silently dropped and never
    guessed into a project. It shows in the render with a UNATTRIBUTED marker."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    cards = [
        _hcard("u1", status="running", workspace_path=None),
        _hcard("u2", status="running", workspace_path=""),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)

    data = cmd.gather_data("/tmp/reg.yaml")
    rows = data["running"]
    assert len(rows) == 2
    assert all(r["unattributed"] for r in rows)
    out = cmd.render_digest(data)
    assert "UNATTRIBUTED" in out
    assert "u1" in out and "u2" in out  # neither vanished


# --------------------------------------------------------------------------- #
# STALE starved/working distinction — empty worktree vs files-present-no-commit
# --------------------------------------------------------------------------- #


def _install_git_stale(monkeypatch):
    """A stale-shaped card: branch exists, unmerged, zero commits."""
    monkeypatch.setattr(git_state, "branch_exists", lambda *a, **k: True)
    monkeypatch.setattr(git_state, "is_merged", lambda *a, **k: False)
    monkeypatch.setattr(git_state, "commits_ahead", lambda *a, **k: 0)


def test_empty_worktree_renders_starved(monkeypatch):
    """An in-flight card past the threshold with zero commits whose worktree is
    EMPTY (or absent) renders as STARVED in STALE — an infrastructure problem,
    surfaced distinctly, never folded into a plain stale or into working."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("ts", status="running")])
    _install_git_stale(monkeypatch)
    # Empty worktree -> starved. _isdir True + empty listing.
    monkeypatch.setattr(cmd, "_isdir", lambda *a, **k: True)

    data = cmd.gather_data("/tmp/reg.yaml", _listdir=lambda p: [])
    assert len(data["stale"]) == 1
    assert data["stale"][0]["kind"] == "starved"
    assert data["running"] == []
    out = cmd.render_digest(data)
    assert "STARVED: empty worktree, likely infrastructure" in out


def test_files_present_no_commit_is_working_not_stale(monkeypatch):
    """An in-flight card past the threshold with zero commits whose worktree has
    FILES present is WORKING, not stale at all — it lands in RUNNING, never in
    STALE, because the worker is genuinely doing the work."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("tw", status="running")])
    _install_git_stale(monkeypatch)
    monkeypatch.setattr(cmd, "_isdir", lambda *a, **k: True)

    data = cmd.gather_data("/tmp/reg.yaml", _listdir=lambda p: ["main.py", "tests.py"])
    assert data["stale"] == []
    assert len(data["running"]) == 1
    assert data["running"][0]["id"] == "tw"
    assert data["running"][0]["kind"] == "working"
    out = cmd.render_digest(data)
    assert "STALE (0)" in out
    assert "RUNNING (1)" in out
    assert "STARVED" not in out


# --------------------------------------------------------------------------- #
# FAILING — projects whose last recorded verify run FAILED (read state, no rerun)
# --------------------------------------------------------------------------- #


def _state(verify_records, tmp_path):
    """Write a verify state doc to tmp_path and return its path."""
    path = tmp_path / "state.yaml"
    doc = {"verify": verify_records} if verify_records else {}
    (tmp_path / "state.yaml").write_text(
        yaml.safe_dump(doc),
        encoding="utf-8",
    )
    return str(path)


def _project_named(name, **kw):
    return registry.Project(name=name, repo=f"/repo-{name}", **kw)


def test_failing_lists_only_failed_projects(monkeypatch, tmp_path):
    """FAILING surfaces only projects whose LAST verify record is FAIL. A
    project with no record, a PASS, or a NO_VERIFY is not failing (absence here
    is the honest 'nothing currently failing' reading)."""
    state_path = _state(
        {
            "hscc": {"status": "FAIL", "timestamp": 1_600_000_000.0, "duration_s": 1.5},
            "okproj": {"status": "PASS", "timestamp": 1_600_000_000.0, "duration_s": 0.5},
            "never": {"status": "NO_VERIFY", "timestamp": 1_600_000_000.0, "duration_s": 0.0},
        },
        tmp_path,
    )
    projects = [
        _project_named("hscc", board="hscc"),
        _project_named("okproj", board="okproj"),
        _project_named("never"),
        _project_named("norecord"),
    ]
    monkeypatch.setattr(registry, "load_registry", lambda path: projects)
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])
    monkeypatch.setattr(
        cmd.deployment,
        "version_drift",
        lambda *a, **k: (None, None, "UNKNOWN"),
    )

    data = cmd.gather_data("/tmp/reg.yaml", _state=state_path)
    assert [r["project"] for r in data["failing"]] == ["hscc"]
    out = cmd.render_digest(data)
    assert "FAILING (1)" in out
    assert "[hscc] hscc — last verify FAILED" in out
    # Projects with a PASS, a NO_VERIFY, or no record at all are NOT failing.
    assert all(r["project"] == "hscc" for r in data["failing"])
    assert "okproj" not in [r["project"] for r in data["failing"]]


def test_failing_empty_state_is_empty(monkeypatch, tmp_path):
    """A state file with no FAIL records (or a missing state file) yields an
    empty FAILING section — never an error, never a fabricated failure."""
    state_path = str(tmp_path / "state.yaml")
    # No file written -> load_state returns {}.
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project(board="hscc")])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])

    data = cmd.gather_data("/tmp/reg.yaml", _state=state_path)
    assert data["failing"] == []
    assert "FAILING (0)" in cmd.render_digest(data)


# --------------------------------------------------------------------------- #
# DRIFT — repo vs installed version, with UNKNOWN kept distinct from OK
# --------------------------------------------------------------------------- #


def test_drift_unknown_renders_distinct_from_ok():
    """DRIFT UNKNOWN must never be rendered as OK. A project with no
    installed_version_cmd -> UNKNOWN, surfaced as such ("not checked"), while an
    OK project reads "OK" — the two are visually and textually distinct."""
    unknown = cmd._render_drift(
        {"project": "x", "board": "b", "repo_version": None, "installed": None, "state": "UNKNOWN"}
    )
    ok = cmd._render_drift(
        {"project": "y", "board": "b", "repo_version": "1.0", "installed": "1.0", "state": "OK"}
    )
    drifted = cmd._render_drift(
        {"project": "z", "board": "b", "repo_version": "2.0", "installed": "1.0", "state": "DRIFTED"}
    )
    assert "OK" in ok and "UNKNOWN" not in ok
    assert "UNKNOWN (not checked)" in unknown
    assert "OK" not in unknown
    assert "DRIFTED" in drifted  # drift row flagged, not OK
    assert unknown != ok


def test_drift_rows_include_every_project_with_missing_board_unknown(monkeypatch):
    """Every registered project appears in DRIFT, and one with no board/appears
    with board None (rendered as unknown) — a project is never silently omitted
    because a field is missing."""
    projects = [
        _project_named("hscc", board="hscc"),
        _project_named("bare"),  # no board
    ]
    monkeypatch.setattr(registry, "load_registry", lambda path: projects)
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])
    results = {
        "hscc": ("1.2.0", "1.2.0", "OK"),
        "bare": (None, None, "UNKNOWN"),
    }
    monkeypatch.setattr(cmd.deployment, "version_drift", lambda p, _run=None: results[p.name])

    data = cmd.gather_data("/tmp/reg.yaml")
    assert [r["project"] for r in data["drift"]] == ["bare", "hscc"]  # sorted by project
    by_name = {r["project"]: r for r in data["drift"]}
    assert by_name["bare"]["board"] is None
    assert by_name["bare"]["state"] == "UNKNOWN"
    assert by_name["hscc"]["state"] == "OK"
    out = cmd.render_digest(data)
    assert "DRIFT (2)" in out
    # bare's unknown board renders as "unknown", its state as UNKNOWN.
    assert "[unknown] bare — UNKNOWN" in out
    assert "[hscc] hscc — OK" in out


# --------------------------------------------------------------------------- #
# --json — stable machine-readable shape for scripting
# --------------------------------------------------------------------------- #


def test_json_shape_is_stable(monkeypatch, capsys, tmp_path):
    """`--json` emits a stable shape: the five section keys, every row carrying
    the full field set (missing optionals as None), UNKNOWN drift distinct from
    OK, and starved stale clearly marked. Nothing is silently dropped."""
    state_path = _state(
        {"hscc": {"status": "FAIL", "timestamp": 1.0, "duration_s": 2.0}},
        tmp_path,
    )
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [
            _project(board="hscc", verify="cmd"),
            _project_named("bare"),
        ],
    )
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("t1", status="review")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=2)
    results = {
        "hscc": ("1.0", "0.9", "DRIFTED"),
        "bare": (None, None, "UNKNOWN"),
    }
    monkeypatch.setattr(cmd.deployment, "version_drift", lambda p, _run=None: results[p.name])

    # cap is read from Hermes config; inject a tmp config declaring cap 3 so
    # the assertion does not depend on the host's real ~/.hermes/config.yaml
    # (a fake HOME in CI would otherwise fall back to the default cap of 1).
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump({"kanban": {"max_in_progress": 3}}), encoding="utf-8"
    )
    # Freshness for the digest's board: inject a recent watermark so the JSON
    # shape does not depend on the host's real board timestamps.
    monkeypatch.setattr(
        kanban, "list_board_watermarks", lambda **kw: {"hscc": NOW}
    )

    args = _args(json=True, state=str(state_path), config_path=str(cfg))
    rc = cmd.cmd_standup(args, "/tmp/reg.yaml")
    assert rc == 0
    import json
    parsed = json.loads(capsys.readouterr().out)
    assert set(parsed.keys()) == {
        "needs_you", "failing", "stale", "running", "drift", "unreadable",
        "coverage",
    }
    # Coverage numbers agree with what was actually read: 2 projects, 1 board,
    # 1 card, 1 attributed, 0 unreadable.
    assert parsed["coverage"] == {
        "projects": 2,
        "boards": 1,
        "cards": 1,
        "attributed": 1,
        "unreadable": 0,
        "unreadable_projects": [],
        "no_source": ["bare"],
        # Fleet-wide concurrency keys: cap read from config, ceiling default 3.
        "cap": 3,
        "ceiling": 3,
        "in_flight": 0,
        "in_flight_boards": [],
        # This card has workspace_path == the project's repo root (/repo), so it
        # is flagged as a main-tree violation — surfaced, never silently dropped.
        "main_tree": 1,
        # New footer keys: no orphan board holds cards, and no flightdeck
        # self-update notice (this registry has no `flightdeck` project).
        "unread_boards": [],
        "unread_cards": 0,
        "update": None,
        # Freshness watermark: the contributing board (hscc) is dated NOW.
        "watermark": NOW,
        "watermark_boards": ["hscc"],
        # Cross-project dependents: neither hscc nor bare declares depends_on,
        # so no project has dependents — empty (no noise in the common case).
        "dependents": [],
    }
    assert parsed["unreadable"] == []
    # needs_you row keeps every field, verify included.
    ny = parsed["needs_you"][0]
    assert ny == {
        "id": "t1", "board": "hscc", "title": "task", "status": "review",
        "branch": "wt/t1", "assignee": "coder", "verify": "cmd",
    }
    # FAILING row carries status/timestamp/duration.
    assert parsed["failing"] == [
        {"project": "hscc", "board": "hscc", "status": "FAIL",
         "timestamp": 1.0, "duration_s": 2.0}
    ]
    # Drift: UNKNOWN distinct from DRIFTED, board None for the bare project.
    drift = {r["project"]: r for r in parsed["drift"]}
    assert drift["bare"] == {
        "project": "bare", "board": None, "repo_version": None,
        "installed": None, "state": "UNKNOWN",
    }
    assert drift["hscc"]["state"] == "DRIFTED"





def test_renderer_identical_between_one_shot_and_watch_frame(monkeypatch):
    """The digest CORE is identical whether rendered by the one-shot path or a
    single watch frame — same render_digest, so the two cannot drift."""
    # Parse the CLI subparser; a watch frame goes through the same renderer.
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["standup", "--watch", "--interval", "5"])

    monkeypatch.setattr(registry, "load_registry", lambda path: [_project(verify="cmd")])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("t1", status="review")])
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=2)

    one_shot = cmd.render_digest(cmd.gather_data("/tmp/reg.yaml"))

    # ONE good frame from watch_frames carries the same digest body.
    gathered = []

    def fake_gather():
        gathered.append(True)
        return cmd.gather_data("/tmp/reg.yaml")

    frames = cmd.watch_frames(fake_gather, interval=5, _sleep=lambda s: None)
    frame = next(frames)
    assert frame["error"] is None
    assert frame["body"] == one_shot  # byte-identical renderer output
    assert len(gathered) == 1


# --------------------------------------------------------------------------- #
# watch interval — respected via injected sleep / clock, no real sleeping
# --------------------------------------------------------------------------- #


def test_interval_passed_to_injected_sleep():
    """The watch loop hands the configured interval to `_sleep`; nothing really
    pauses, because the sleep is a fake."""

    def fake_gather():
        return {"needs_you": [], "stale": [], "running": []}

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    gen = cmd.watch_frames(fake_gather, interval=7, _sleep=fake_sleep)
    next(gen)  # first frame: gathers, sleeps, yields
    assert sleeps == [7]
    next(gen)  # second frame: sleeps again with the same interval
    assert sleeps == [7, 7]


def test_watch_loop_uses_advancing_fake_clock(capsys):
    """The --watch shell renders a fresh timestamp each frame via the injected
    clock; the body is re-gathered each frame. No real time passes."""
    ticks = iter([NOW, NOW + 10])

    def fake_clock():
        return next(ticks)

    def fake_gather():
        return {"needs_you": [], "stale": [], "running": []}

    frames = cmd.watch_frames(fake_gather, interval=30, _sleep=lambda s: None)
    f1 = next(frames)
    f2 = next(frames)
    assert f1["error"] is None and f2["error"] is None
    assert f1["body"] == f2["body"]

    # These timestamps come from the injected clock, not time.strftime(now()).
    ts1 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW))
    ts2 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW + 10))
    assert ts1 != ts2


# --------------------------------------------------------------------------- #
# transient refresh failure — keep last good frame, report error, keep looping
# --------------------------------------------------------------------------- #


def test_failing_refresh_keeps_previous_frame_and_reports_error():
    """A refresh that raises (a momentarily locked kanban DB) keeps the LAST
    GOOD frame in the body and surfaces the error — and keeps looping so an
    unattended monitor does not die on one transient failure."""
    good = {"needs_you": [], "stale": [], "running": [{"board": "hscc", "id": "t1", "title": "ok"}]}
    calls = {"n": 0}

    def flaky_gather():
        calls["n"] += 1
        if calls["n"] == 2:  # one transient failure, then the loop recovers
            raise RuntimeError("database is locked")
        return good

    sleeps = []
    frames = cmd.watch_frames(flaky_gather, interval=30, _sleep=sleeps.append)

    f1 = next(frames)
    assert f1["error"] is None
    assert "RUNNING (1)" in f1["body"]

    f2 = next(frames)
    assert f2["error"] == "refresh failed: database is locked"
    # The body is the LAST GOOD frame, not an empty or partial frame.
    assert f2["body"] == f1["body"]
    assert "RUNNING (1)" in f2["body"]

    f3 = next(frames)
    assert f3["error"] is None  # recovered — gather succeeded again
    assert sleeps == [30, 30, 30]


# --------------------------------------------------------------------------- #
# command discovery
# --------------------------------------------------------------------------- #


def test_standup_is_a_discovered_command():
    """`standup` registers as a real subcommand via cli auto-discovery and
    accepts the --watch / --interval flags."""
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["standup", "--watch", "--interval", "5"])
    assert args.command == "standup"
    assert args.func is not None
    assert args.watch is True
    assert args.interval == 5


# --------------------------------------------------------------------------- #
# Ctrl-C — exits 0, no traceback
# --------------------------------------------------------------------------- #


def test_ctrl_c_exits_zero_without_traceback(monkeypatch, capsys):
    """Sending Ctrl-C into the watch loop returns 0 and never prints a
    traceback to stderr."""
    def fake_sleep(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project()]
    )
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [_hcard("t1")])
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=0)

    rc = cmd.cmd_standup(_args(watch=True, sleep=fake_sleep, now=lambda: NOW), "/tmp/reg.yaml")
    assert rc == 0
    # No traceback leaked to stderr — the KeyboardInterrupt was swallowed by
    # the watch loop (this is what "handles Ctrl-C cleanly" means).
    assert "Traceback" not in capsys.readouterr().err


def test_rows_with_no_board_sort_last_instead_of_crashing():
    """A card attributed by path may have board=None; None is not orderable.

    Attributing by workspace_path means a card can belong to a project that has
    no board mapping at all, and the live digest crashed with
    "'<' not supported between instances of 'NoneType' and 'str'".
    """
    import flightdeck.commands.standup as st

    rows = [
        {"board": None, "id": "t_b", "project": None},
        {"board": "hscc", "id": "t_a", "project": "hscc"},
    ]
    rows.sort(key=lambda r: (r["board"] or "￿", r["id"] or ""))
    assert [r["id"] for r in rows] == ["t_a", "t_b"]
    assert hasattr(st, "gather_data")


# --------------------------------------------------------------------------- #
# COVERAGE — the digest states what it actually read, never a blind all-clear
# --------------------------------------------------------------------------- #


def _coverage(**kw):
    """A minimal coverage dict with safe defaults, overridable per test."""
    base = {
        "projects": 0,
        "boards": 0,
        "cards": 0,
        "attributed": 0,
        "unreadable": 0,
        "unreadable_projects": [],
        "no_source": [],
    }
    base.update(kw)
    return base


def test_loud_warning_when_zero_attributed_with_cards_present():
    """The exact failure measured the night this card was filed: 251 cards on
    the boards but NO project matched any card. A clean digest would be a lie,
    so a loud warning is emitted."""

    cov = _coverage(projects=7, boards=3, cards=251, attributed=0, unreadable=2)
    warning = cmd._coverage_warning(cov)
    assert warning is not None
    assert "ZERO" in warning
    assert "CANNOT be trusted" in warning
    out = "\n".join(cmd._render_coverage(cov))
    assert warning in out


def test_zero_attributed_warning_surfaces_in_rendered_digest():
    """The warning renders through render_digest, not just the pure helper —
    the one-shot path that prints the digest carries it."""
    data = {
        "needs_you": [], "failing": [], "stale": [], "running": [], "drift": [],
        "unreadable": [],
        "coverage": _coverage(projects=2, boards=1, cards=5, attributed=0),
    }
    out = cmd.render_digest(data)
    assert "CANNOT be trusted" in out
    assert "read 2 projects | 1 boards | 5 cards | 0 attributed" in out


def test_healthy_fleet_prints_coverage_with_no_warning():
    """A fully healthy fleet prints the coverage footer with no warning — the
    footer is always present, but the numbers are self-consistent."""
    cov = _coverage(projects=7, boards=3, cards=251, attributed=251)
    assert cmd._coverage_warning(cov) is None
    out = "\n".join(cmd._render_coverage(cov))
    assert (
        "read 7 projects | 3 boards | 251 cards | 251 attributed | 0 unreadable"
        in out
    )
    assert "WARNING" not in out


def test_no_board_project_listed_no_source():
    """A project with no board and no attributed cards is listed as 'no card
    source configured', never silently treated as a clean project."""
    cov = _coverage(projects=2, boards=1, cards=4, attributed=4, no_source=["bare"])
    out = "\n".join(cmd._render_coverage(cov))
    assert "bare: no card source configured" in out


# --------------------------------------------------------------------------- #
# orphan/legacy-board detection — the +N unread board footer line (Gap 4B)
# --------------------------------------------------------------------------- #


def test_orphan_board_renders_footer_pointer(monkeypatch):
    """Cards on a board with no registry entry that standup does NOT otherwise
    surface (workspace resolves to no project) produce the `+ N unread board ...
    run legacy-cards` footer line. Reuses legacy-cards' board-attribution rule,
    so the two commands agree about what counts as an orphan board."""
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [
            _project(name="hscc", board="hscc", repo="/repo"),
        ],
    )
    cards = [
        _hcard("t1", status="review", board="hscc", workspace_path="/repo"),
        # Hidden: board not registered AND workspace resolves to no project.
        _hcard("l1", status="todo", board="legacy", workspace_path=None),
        _hcard("l2", status="todo", board="legacy", workspace_path=None),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    cov = data["coverage"]
    assert cov["unread_boards"] == ["legacy"]
    assert cov["unread_cards"] == 2
    out = cmd.render_digest(data)
    assert (
        "+ 1 unread board (legacy) holding 2 card(s) — run legacy-cards" in out
    )


def test_shared_default_board_is_not_flagged_as_orphan(monkeypatch):
    """The shared `default` board's cards ARE surfaced by workspace attribution
    (attributed to a registered project), so it is NOT flagged as an unread
    orphan — the common healthy case stays quiet instead of crying wolf every
    single run."""
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [_project(name="hscc", board="hscc", repo="/repo")],
    )
    cards = [
        # On the `default` board but workspaces resolve to registered projects.
        _hcard("a", status="review", board="default", workspace_path="/repo"),
        _hcard("b", status="review", board="default", workspace_path="/repo"),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    cov = data["coverage"]
    assert cov["unread_boards"] == []
    assert cov["unread_cards"] == 0
    out = cmd.render_digest(data)
    assert "run legacy-cards" not in out


def test_no_orphan_boards_prints_no_orphan_line():
    """All cards on registered boards -> no orphan footer line at all (the
    common healthy case stays quiet)."""
    cov = _coverage(
        projects=1, boards=1, cards=1, attributed=1,
        unread_boards=[], unread_cards=0,
    )
    out = "\n".join(cmd._render_coverage(cov))
    assert "run legacy-cards" not in out
    assert "+ 1 unread board" not in out


def test_orphan_detection_skips_when_cards_unreadable(monkeypatch):
    """When the single board read FAILS (cards=[]), there is no orphan-board
    declaration — we cannot know what cards exist, so we do not guess."""
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [_project(name="hscc", board="hscc", repo="/repo")],
    )
    monkeypatch.setattr(
        cmd.deployment, "version_drift", lambda p, _run=None: (None, None, "UNKNOWN")
    )

    def fake_list_cards(**kw):
        raise cmd.kanban.KanbanError("database is locked")

    monkeypatch.setattr(kanban, "list_cards", fake_list_cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    cov = data["coverage"]
    assert cov["unread_boards"] == []
    assert cov["unread_cards"] == 0
    out = cmd.render_digest(data)
    assert "run legacy-cards" not in out


# --------------------------------------------------------------------------- #
# version-drift notice — flightdeck's OWN version vs remote, rate-limited
# --------------------------------------------------------------------------- #


def _fd_project(installed_version_cmd="flightdeck --version"):
    """A registered flightdeck project carrying an installed_version_cmd."""
    return registry.Project(
        name="flightdeck",
        repo="/repo",
        board="flightdeck",
        installed_version_cmd=installed_version_cmd,
    )


def test_version_tuple_parses_versions():
    """The semver-ish ordering helper ignores v-prefixes and junk suffixes."""
    assert cmd._version_tuple("0.6.0") == (0, 6, 0)
    assert cmd._version_tuple("v0.7.1") == (0, 7, 1)
    assert cmd._version_tuple("0.10.0") > cmd._version_tuple("0.9.9")
    assert cmd._version_tuple("garbage") == (0,)


def test_latest_tag_picks_newest_skipping_peel_refs():
    """`_latest_tag` ignores the peeled `^{}` refs and picks the max tag, so
    the answer does not depend on ls-remote's sort order."""
    out = "\n".join(
        [
            "abc\trefs/tags/v0.5.0",
            "abc\trefs/tags/v0.5.0^{}",
            "def\trefs/tags/v0.6.0",
            "def\trefs/tags/v0.6.0^{}",
            "ghi\trefs/tags/v0.10.0",
            "ghi\trefs/tags/v0.10.0^{}",
        ]
    )
    assert cmd._latest_tag(out) == "v0.10.0"


def test_latest_tag_none_when_no_version_tags():
    assert cmd._latest_tag("abc\trefs/heads/main\n") is None


def test_update_available_when_remote_ahead(monkeypatch, tmp_path):
    """A registered flightdeck project whose remote newest tag is newer than
    its installed version yields the `flightdeck update available` footer
    line, and the verdict is cached for the once-per-day rate limit."""
    state = tmp_path / "update-check.yaml"

    def fake_fresh(project, *, _run=None):
        return ("v0.7.0", "0.6.0")

    monkeypatch.setattr(cmd, "_fresh_update_check", fake_fresh)

    notice = cmd._version_update_notice(
        [_fd_project()], _now=lambda: NOW, _update_state=str(state)
    )
    assert notice is not None
    assert "flightdeck update available" in notice
    assert "0.6.0" in notice and "v0.7.0" in notice
    assert "run flightdeck update" in notice

    # The verdict is cached for the daily rate limit.
    cached = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert cached["latest"] == "v0.7.0"
    assert cached["installed"] == "0.6.0"
    assert cached["checked_at"] == NOW


def test_update_cached_within_day_does_not_refetch(monkeypatch, tmp_path):
    """Within the once-per-day TTL, a subsequent call reads the cache and does
    NOT re-run the network check — the --watch safety property. We prove the
    ls-remote is not re-invoked by making _fresh_update_check raise if called."""
    state = tmp_path / "update-check.yaml"
    state.write_text(
        yaml.safe_dump(
            {"checked_at": NOW, "latest": "v0.7.0", "installed": "0.6.0"}
        ),
        encoding="utf-8",
    )

    def boom(project, *, _run=None):
        raise AssertionError("network check re-invoked within TTL!")

    monkeypatch.setattr(cmd, "_fresh_update_check", boom)

    notice = cmd._version_update_notice(
        [_fd_project()], _now=lambda: NOW + 3600, _update_state=str(state)
    )
    assert notice is not None
    assert "flightdeck update available" in notice


def test_update_quiescent_when_installed_current(monkeypatch, tmp_path):
    """Installed version equal to (or ahead of) the newest remote tag -> no
    notice, and the cache still records the verdict."""
    state = tmp_path / "update-check.yaml"

    def fake_fresh(project, *, _run=None):
        return ("v0.6.0", "0.6.0")

    monkeypatch.setattr(cmd, "_fresh_update_check", fake_fresh)

    notice = cmd._version_update_notice(
        [_fd_project()], _now=lambda: NOW, _update_state=str(state)
    )
    assert notice is None


def test_update_unknown_does_not_notice(monkeypatch, tmp_path):
    """Neither version determinable (offline / no command output) -> no notice,
    never a fabricated one."""
    state = tmp_path / "update-check.yaml"

    def fake_fresh(project, *, _run=None):
        return (None, None)

    monkeypatch.setattr(cmd, "_fresh_update_check", fake_fresh)
    assert (
        cmd._version_update_notice(
            [_fd_project()], _now=lambda: NOW, _update_state=str(state)
        )
        is None
    )


def test_update_notice_skips_without_self_entry(monkeypatch, tmp_path):
    """No `flightdeck` project in the registry -> no notice, and crucially no
    cache write or network call (the project short-circuits first)."""
    state = tmp_path / "update-check.yaml"
    assert (
        cmd._version_update_notice(
            [_project(name="hscc")], _now=lambda: NOW, _update_state=str(state)
        )
        is None
    )
    assert not state.exists()  # nothing written without a self-entry


def test_update_notice_reaches_footer_in_gather(tmp_path, monkeypatch):
    """The version-drift notice renders as a footer line in the real digest
    when a newer flightdeck exists on the remote."""
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [_fd_project(installed_version_cmd="echo 0.6.0")],
    )
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: []
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=0)
    monkeypatch.setattr(cmd.deployment, "version_drift", lambda p, _run=None: ("0.6.0", "0.6.0", "OK"))
    state = tmp_path / "update-check.yaml"

    def fake_fresh(project, *, _run=None):
        return ("v0.7.0", "0.6.0")

    monkeypatch.setattr(cmd, "_fresh_update_check", fake_fresh)

    data = cmd.gather_data(
        "/tmp/reg.yaml", _now=lambda: NOW, _update_state=str(state)
    )
    out = cmd.render_digest(data)
    assert "flightdeck update available: installed 0.6.0, remote v0.7.0" in out
    assert "run flightdeck update" in out


def test_watch_frames_respect_once_per_day_rate_limit(tmp_path, monkeypatch):
    """The --watch safety property end-to-end: across multiple frames the
    version check runs the (network) ls-remote exactly ONCE, then every later
    frame reads the cached verdict — a 30s redraw loop never hammers the
    remote, even though gather_data re-runs per frame."""
    state = tmp_path / "update-check.yaml"
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [_fd_project(installed_version_cmd="echo 0.6.0")],
    )
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: []
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=0)
    monkeypatch.setattr(
        cmd.deployment, "version_drift", lambda p, _run=None: ("0.6.0", "0.6.0", "OK")
    )

    calls = {"n": 0}

    def counting_fresh(project, *, _run=None):
        calls["n"] += 1
        return ("v0.7.0", "0.6.0")

    monkeypatch.setattr(cmd, "_fresh_update_check", counting_fresh)

    clock = iter([NOW, NOW + 30, NOW + 60])  # 3 watch frames, 30s apart

    def fake_clock():
        return next(clock)

    def gather():
        return cmd.gather_data(
            "/tmp/reg.yaml", _now=fake_clock, _update_state=str(state)
        )

    frames = cmd.watch_frames(gather, interval=30, _sleep=lambda s: None)
    f1 = next(frames)
    f2 = next(frames)
    f3 = next(frames)
    assert f1["error"] is None and f2["error"] is None and f3["error"] is None
    assert calls["n"] == 1  # the ls-remote fired once; frames 2+ used the cache
    # Every frame still renders the same fresh notice (from the cache).
    assert "flightdeck update available" in f1["body"]
    assert "flightdeck update available" in f2["body"]
    assert "flightdeck update available" in f3["body"]


def test_unreadable_repo_surfaces_under_unreadable_with_reason(monkeypatch):
    """A project whose repo path does not exist appears under UNREADABLE with its
    reason and does NOT reduce to a clean row — no cards counted, and not listed
    as a no-source project either (it cannot be read at all)."""
    monkeypatch.setattr(cmd, "_isdir", lambda p: False)
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [registry.Project(name="gone", repo="/does/not/exist", board="gone_board")],
    )
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("t1", board="gone_board")]
    )
    monkeypatch.setattr(
        cmd.deployment, "version_drift", lambda p, _run=None: (None, None, "UNKNOWN")
    )

    data = cmd.gather_data("/tmp/reg.yaml")
    assert len(data["unreadable"]) == 1
    assert data["unreadable"][0]["project"] == "gone"
    assert "repo path does not exist" in data["unreadable"][0]["reason"]
    cov = data["coverage"]
    assert cov["unreadable"] == 1
    # One pass still READS the card; it simply cannot be ATTRIBUTED to a
    # project whose repo is missing. Read and attributed are different facts.
    assert cov["attributed"] == 0
    assert "gone" not in cov["no_source"]
    out = cmd.render_digest(data)
    assert "UNREADABLE (1)" in out
    # The reason is carried into the rendered row.
    assert data["unreadable"][0]["reason"] in out


def test_board_open_failure_surfaces_unreadable(monkeypatch):
    """A project whose board DB cannot be opened is UNREADABLE with the reason,
    and does not take down the whole digest — the readable project still reads."""
    monkeypatch.setattr(cmd, "_isdir", lambda p: True)
    monkeypatch.setattr(
        registry,
        "load_registry",
        lambda path: [
            registry.Project(name="hscc", repo="/hscc", board="hscc"),
            registry.Project(name="broken", repo="/broken", board="broken"),
        ],
    )
    monkeypatch.setattr(cmd.deployment, "version_drift", lambda p, _run=None: (None, None, "UNKNOWN"))

    def fake_list_cards(**kw):
        raise cmd.kanban.KanbanError("database is locked")

    monkeypatch.setattr(kanban, "list_cards", fake_list_cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    # The one read failed, so nothing is known about ANY project -- every one
    # is reported unreadable rather than rendered clean.
    assert len(data["unreadable"]) == 2
    reasons = " ".join(r["reason"] for r in data["unreadable"])
    assert "could not read cards" in reasons
    out = cmd.render_digest(data)
    assert "database is locked" in out
    assert "NEEDS YOU (0)" in out


def test_footer_counts_match_rendered_sections(monkeypatch):
    """Footer card count equals the cards rendered; boards counts what was read."""
    monkeypatch.setattr(
        registry, "load_registry",
        lambda path: [_project(name="hscc", board="hscc", repo="/repo"),
                      _project(name="other", board="other", repo="/repo-other")],
    )
    monkeypatch.setattr(
        kanban, "list_cards",
        lambda **kw: [_hcard("t1", board="hscc", workspace_path="/repo"),
                      _hcard("t2", board="hscc", workspace_path="/repo"),
                      _hcard("t3", board="other", workspace_path="/repo-other")],
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    cov = data["coverage"]
    rendered = len(data["needs_you"]) + len(data["stale"]) + len(data["running"])
    assert cov["cards"] == 3
    assert cov["attributed"] == 3
    assert cov["boards"] == 2
    assert rendered == cov["cards"]
    out = cmd.render_digest(data)
    assert "read 2 projects | 2 boards | 3 cards | 3 attributed | 0 unreadable" in out


def test_json_coverage_matches_text_footer(monkeypatch, capsys):
    """--json carries the same coverage numbers as the text footer."""
    monkeypatch.setattr(
        registry, "load_registry", lambda path: [_project(board="hscc"), _project(board="other")]
    )

    monkeypatch.setattr(
        kanban, "list_cards",
        lambda **kw: [_hcard("t1", board="hscc", workspace_path="/repo")],
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=1)

    data = cmd.gather_data("/tmp/reg.yaml")
    text = cmd.render_digest(data)
    # Extract the footer line from the text render.
    footer = next(l for l in text.splitlines() if l.startswith("read "))
    # "boards" counts the boards that actually CONTRIBUTED cards in the single
    # pass, not the boards projects happen to declare.
    assert footer == (
        "read 2 projects | 1 boards | 1 cards | 1 attributed | 0 unreadable"
    )

    import json
    parsed = json.loads(json.dumps(cmd._render_json(data)))
    cov = parsed["coverage"]
    assert (
        f"read {cov['projects']} projects | {cov['boards']} boards | "
        f"{cov['cards']} cards | {cov['attributed']} attributed | "
        f"{cov['unreadable']} unreadable"
    ) == footer


# --------------------------------------------------------------------------- #
# fleet-wide concurrency — the cap is per board, so sum across ALL of them
# --------------------------------------------------------------------------- #


def _fleet_cards(*items):
    """A board's set of cards, one entry per (id, board, status)."""
    return [
        _hcard(cid, status=status, board=board, workspace_path="/repo")
        for cid, board, status in items
    ]


def test_six_boards_one_cap_fleet_count_sums_all(monkeypatch):
    """THE six-boards-one-cap failure: kanban.max_in_progress is 3 but six
    cards run at once, one on each of six boards. The fleet-wide in-flight
    count must SUM the running cards across ALL boards (6), not just cap one
    board — this is the silent oversubscription the card exists to surface."""
    projects = [
        _project(name=f"p{i}", board=f"b{i}", repo=f"/repo-{i}") for i in range(6)
    ]
    monkeypatch.setattr(registry, "load_registry", lambda path: projects)
    cards = _fleet_cards(
        ("t0", "b0", "running"), ("t1", "b1", "running"), ("t2", "b2", "running"),
        ("t3", "b3", "running"), ("t4", "b4", "running"), ("t5", "b5", "running"),
    )
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml")
    cov = data["coverage"]
    # Six cards, one per board, all running -> fleet-wide 6 across 6 boards.
    assert cov["in_flight"] == 6
    assert cov["in_flight_boards"] == ["b0", "b1", "b2", "b3", "b4", "b5"]


def test_fleet_warning_fires_above_ceiling_and_names_boards(monkeypatch):
    """Above the fleet ceiling, the digest prints a distinct warning line
    naming exactly the boards contributing in-flight cards."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards",
        lambda **kw: _fleet_cards(
            ("t1", "ecofire-app", "running"), ("t2", "sphoin", "running"),
            ("t3", "soconn", "running"), ("t4", "flosana", "running"),
            ("t5", "powerbi", "running"), ("t6", "ecofire-bc", "running"),
        ),
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=4)

    data = cmd.gather_data("/tmp/reg.yaml", _ceiling=3)
    out = cmd.render_digest(data)
    assert "in flight: 6 cards across 6 boards" in out
    # Boards are listed in the sorted order the digest renders them in.
    assert (
        "WARNING: 6 in flight fleet-wide (ceiling 3) — boards: "
        "ecofire-app, ecofire-bc, flosana, powerbi, soconn, sphoin" in out
    )


def test_fleet_no_warning_below_ceiling(monkeypatch):
    """Below (or at) the fleet ceiling there is no WARNING line."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards",
        lambda **kw: _fleet_cards(
            ("t1", "b1", "running"), ("t2", "b2", "running"),
        ),
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml", _ceiling=3)
    assert data["coverage"]["in_flight"] == 2
    out = cmd.render_digest(data)
    assert "in flight: 2 cards across 2 boards" in out
    assert "WARNING" not in out


def test_warning_can_be_silenced_with_higher_max_fleet(monkeypatch):
    """`--max-fleet` raises the ceiling, so the same fleet no longer warns."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards",
        lambda **kw: _fleet_cards(
            ("t1", "b1", "running"), ("t2", "b2", "running"), ("t3", "b3", "running"),
        ),
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml", _ceiling=2)  # 3 in flight > 2
    assert "WARNING" in cmd.render_digest(data)
    data2 = cmd.gather_data("/tmp/reg.yaml", _ceiling=10)  # --max-fleet 10
    assert "WARNING" not in cmd.render_digest(data2)


def test_cap_is_read_from_config_not_hardcoded(tmp_path):
    """The per-board cap in the footer comes from kanban.max_in_progress in the
    Hermes config — read, never hardcoded. Two different config files yield
    two different caps."""
    p1 = tmp_path / "c1.yaml"
    p1.write_text(yaml.safe_dump({"kanban": {"max_in_progress": 7}}), encoding="utf-8")
    p2 = tmp_path / "c2.yaml"
    p2.write_text(yaml.safe_dump({"kanban": {"max_in_progress": 2}}), encoding="utf-8")

    assert cmd.kanban.read_max_in_progress(str(p1)) == 7
    assert cmd.kanban.read_max_in_progress(str(p2)) == 2
    # A config with no kanban.max_in_progress falls back conservatively (1).
    p3 = tmp_path / "c3.yaml"
    p3.write_text("irrelevant: true\n", encoding="utf-8")
    assert cmd.kanban.read_max_in_progress(str(p3)) == cmd.kanban._DEFAULT_MAX_IN_PROGRESS


def test_fleet_cap_renders_in_footer_from_config(monkeypatch, tmp_path):
    """The in-flight footer shows the cap read from config (e.g. cap 7/board)."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"kanban": {"max_in_progress": 7}}), encoding="utf-8")
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: _fleet_cards(("t1", "b1", "running"))
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml", _config=str(cfg))
    assert data["coverage"]["cap"] == 7
    out = cmd.render_digest(data)
    assert "in flight: 1 card across 1 board (cap 7/board)" in out

# --------------------------------------------------------------------------- #
# MAIN TREE -- a card that would run in a repo's main tree, surfaced distinctly
# --------------------------------------------------------------------------- #


def _main_tree_card(cid, repo="/repo", kind="scratch", status="running"):
    """A card whose workspace is the project's repo ROOT (the near-miss shape)."""
    return {
        "id": cid,
        "title": "task " + cid,
        "status": status,
        "board": "hscc",
        "branch": "wt/" + cid,
        "assignee": "coder",
        "created_at": BEFORE,
        "started_at": BEFORE,
        "workspace_path": repo,
        "workspace_kind": kind,
    }


def test_gather_surfaces_main_tree_count(monkeypatch):
    """A card whose workspace_path is the repo root is counted as a main-tree
    card in the digest data, distinct from every advisory lint finding."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_main_tree_card("t_root", repo="/repo")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml")
    assert data["coverage"]["main_tree"] == 1
    assert data["main_tree"][0]["id"] == "t_root"
    assert data["main_tree"][0]["path"] == "/repo"


def test_render_main_tree_is_most_interruptive_line(monkeypatch):
    """The MAIN TREE block renders distinctly at the TOP of the digest -- more
    urgent than a stale card, never buried among advisory output."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_main_tree_card("t_root")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml")
    out = cmd.render_digest(data)
    assert "MAIN TREE" in out
    assert "CRITICAL: 1 card would run in a repo's MAIN TREE" in out
    assert "t_root" in out
    # It is the VERY FIRST line of the digest -- above NEEDS YOU.
    assert out.startswith("MAIN TREE")


def test_render_no_main_tree_block_when_all_clear(monkeypatch):
    """No main-tree card -> no MAIN TREE block; the digest renders exactly
    as before."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("t1", status="running", workspace_path="/repo/.worktrees/t1")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml")
    out = cmd.render_digest(data)
    assert data["coverage"]["main_tree"] == 0
    assert "MAIN TREE" not in out
    assert out.startswith("NEEDS YOU") or out.startswith("RUNNING")


def test_standup_prints_no_main_tree_for_unclaimed_at_root(monkeypatch):
    """N17 regression: an UNCLAIMED worktree-kind card at the repo root is
    NORMAL (Hermes rewrites workspace_path to .worktrees/<id> on claim), so
    standup prints NO MAIN TREE line and coverage reports zero — the guard
    stays silent in the normal case instead of crying wolf."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban,
        "list_cards",
        lambda **kw: [
            _main_tree_card("t_a", kind="worktree", status="ready"),
            _main_tree_card("t_b", kind="worktree", status="todo"),
            _main_tree_card("t_c", kind="worktree", status="blocked"),
        ],
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=3)

    data = cmd.gather_data("/tmp/reg.yaml")
    out = cmd.render_digest(data)
    assert data["coverage"]["main_tree"] == 0
    assert "MAIN TREE" not in out
    assert "CRITICAL" not in out


def test_standup_never_reports_settled_cards_as_running(tmp_path, monkeypatch):
    """Regression: a settled (done) card must not surface in standup's digest.

    This is the read-side sibling of the review close-card bug: before the
    fix, ``kanban.list_cards()`` only excluded ``archived`` (via Hermes' own
    SQL filter) but returned ``done`` cards, which ``classify`` then bucketed
    as ``running`` — so `flightdeck standup` reported a completed-but-not-
    archived card as still RUNNING.

    Uses the REAL ``hermes_cli.kanban_db`` against a fully ISOLATED
    ``HERMES_KANBAN_HOME`` (a tmp_path) so it never touches the operator's real
    `~/.hermes` state. Skips cleanly on a host without a Hermes checkout.
    """
    if not os.path.isdir(os.path.expanduser("~/.hermes/hermes-agent")):
        pytest.skip("no Hermes checkout at ~/.hermes/hermes-agent")
    sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
    real_kdb = pytest.importorskip("hermes_cli.kanban_db")

    # Isolated board root — never the real ~/.hermes state.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))

    # Route flightdeck's read seam at the REAL Hermes library, so the digest
    # reads the isolated board on disk exactly as production does.
    monkeypatch.setattr(kanban, "_load_kanban_db", lambda: real_kdb, raising=False)

    # A real repo path the project will be attributed to (must exist on disk
    # for gather_data to treat the project as readable).
    repo = tmp_path / "repo"
    repo.mkdir()

    # Create a board + a task, then complete it (status -> done) but DO NOT
    # archive it. This is the exact state that used to be reported as RUNNING.
    real_kdb.create_board("fdtest")
    conn = real_kdb.connect(board="fdtest")
    try:
        tid = real_kdb.create_task(
            conn,
            title="settled card",
            assignee="coder",
            workspace_kind="worktree",
            workspace_path=str(repo),
            board="fdtest",
        )
        real_kdb.complete_task(conn, tid)
    finally:
        conn.close()

    # Sanity: the card really is done on disk (the bug's premise).
    conn = real_kdb.connect(board="fdtest")
    try:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "done"

    # Registry mapping the project to the isolated board.
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        f"projects:\n  - name: p\n    repo: {repo}\n    board: fdtest\n"
    )

    # Inject git/clock/config reads so nothing touches real state, and have
    # standup classify with branch-not-exists facts (the reading that, pre-fix,
    # fell through to ``running`` for a done card).
    monkeypatch.setattr(git_state, "branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(git_state, "is_merged", lambda *a, **k: False)
    monkeypatch.setattr(git_state, "commits_ahead", lambda *a, **k: 0)
    monkeypatch.setattr(cmd, "_isdir", lambda p: True)
    missing_config = tmp_path / "no-config.yaml"
    monkeypatch.setattr(
        cmd.deployment, "version_drift",
        lambda p, _run=None: (None, None, cmd.deployment.UNKNOWN),
    )

    data = cmd.gather_data(
        str(reg),
        _now=lambda: NOW,
        _config=str(missing_config),
    )
    out = cmd.render_digest(data)

    # The done card must appear in NO interruptive/attention section.
    assert tid not in [r["id"] for r in data["running"]]
    assert tid not in [r["id"] for r in data["needs_you"]]
    assert tid not in [r["id"] for r in data["stale"]]
    assert tid not in out
    assert "NEEDS YOU (0)" in out


# --------------------------------------------------------------------------- #
# Freshness watermark — "data as of <time>" (brainstorm Gap 1)
# --------------------------------------------------------------------------- #

def test_fresh_read_shows_recent_watermark(monkeypatch):
    """A fresh read (all contributing boards recently changed) renders a
    recent ``data as of`` line naming the contributing board."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("t1", status="running",
                                                  workspace_path="/repo")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=5)
    # The contributing board (hscc) has a recent watermark.
    monkeypatch.setattr(kanban, "list_board_watermarks", lambda **kw: {"hscc": NOW})

    data = cmd.gather_data("/tmp/reg.yaml")
    # The digest-level watermark is the board's fresh timestamp.
    assert data["coverage"]["watermark"] == NOW
    assert data["coverage"]["watermark_boards"] == ["hscc"]

    out = cmd.render_digest(data)
    assert "data as of" in out
    assert "board: hscc" in out
    # A fresh read is not flagged as old/unknown.
    assert "data as of <unknown>" not in out


def test_old_non_empty_source_drags_the_watermark_plainly(monkeypatch):
    """When ONE contributing board is genuinely old (not just quiet), the
    watermark reflects the OLDEST — the digest says plainly its board data is
    old, never hiding a stale source behind a fresher one."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("t1", status="running",
                                                  workspace_path="/repo")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=5)
    # The contributing board's newest state-change is OLD (a genuinely stale
    # source). The digest must say so, not claim everything is current. Use
    # wall-clock so the rendered age (which defaults to real time) is stable.
    old_ts = int(time.time()) - (2 * 86400)  # two days ago
    monkeypatch.setattr(
        kanban, "list_board_watermarks", lambda **kw: {"hscc": old_ts}
    )

    data = cmd.gather_data("/tmp/reg.yaml")
    assert data["coverage"]["watermark"] == old_ts
    out = cmd.render_digest(data)
    assert "data as of" in out
    # The old age is surfaced plainly, so staleness cannot hide.
    assert "(2d" in out


def test_quiet_non_contributing_board_never_lowers_the_watermark(monkeypatch):
    """A board that contributed NO card (an empty/quiet board, however old) must
    not drag the digest watermark down — a healthy digest over an active board
    stays fresh even when some unrelated board is silent."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(
        kanban, "list_cards", lambda **kw: [_hcard("t1", status="running",
                                                  workspace_path="/repo")]
    )
    _install_git(monkeypatch, branch_exists=True, is_merged=False, commits_ahead=5)
    # Two boards on the host: hscc (feeds the digest, fresh) and quiet (old but
    # contributed NOTHING). Only hscc is a critical input. Use wall-clock for a
    # stable rendered age (the renderer defaults to real time).
    now = int(time.time())
    monkeypatch.setattr(
        kanban,
        "list_board_watermarks",
        lambda **kw: {"hscc": now, "quiet": now - (10 * 86400)},
    )

    data = cmd.gather_data("/tmp/reg.yaml")
    assert data["coverage"]["watermark"] == now
    assert data["coverage"]["watermark_boards"] == ["hscc"]
    out = cmd.render_digest(data)
    assert "data as of" in out
    assert "(10d" not in out  # quiet board's silence did not lower the watermark


def test_empty_digest_renders_watermark_unknown(monkeypatch):
    """A digest with no contributing boards (no cards at all) renders
    ``data as of <unknown>`` rather than inventing a date."""
    monkeypatch.setattr(registry, "load_registry", lambda path: [_project()])
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])
    _install_git(monkeypatch)
    monkeypatch.setattr(kanban, "list_board_watermarks", lambda **kw: {})

    data = cmd.gather_data("/tmp/reg.yaml")
    assert data["coverage"]["watermark"] is None
    out = cmd.render_digest(data)
    assert "data as of <unknown>" in out
