"""Tests for the `flightdeck metrics` command + core computation.

The core (``flightdeck.core.metrics.compute``) is tested with prepared card
dicts — no I/O. The command layer (``flightdeck.commands.metrics``) is tested
with injected card sources, git runners, event readers, and clocks. No test
touches the real board, a real repo, git, or the network.

The honesty contract is the spine of this file: a rate below the minimum sample
size is reported "insufficient data (n=N)", NEVER as a misleading 0% or 100%,
and every figure states its window and sample size.
"""

import argparse
import json

import pytest

from flightdeck.commands import metrics as cmd
from flightdeck.core import metrics as metrics_core
from flightdeck.core import registry


def ev(kind, ts):
    return {"kind": kind, "created_at": ts}


def _card(cid, *, status="done", started_at=None, completed_at=None,
          is_merged=True, commits_ahead=1, events=None):
    return {
        "id": cid,
        "title": f"task {cid}",
        "status": status,
        "board": "flightdeck",
        "started_at": started_at,
        "completed_at": completed_at,
        "is_merged": is_merged,
        "commits_ahead": commits_ahead,
        "events": events or [],
    }


# --------------------------------------------------------------------------- #
# Core: each metric computed from injected card+git fixtures
# --------------------------------------------------------------------------- #


def test_first_time_pass_rate_computed():
    # 4 reviewed cards, 2 passed first time (merged, never sent back).
    cards = [
        _card("c1", started_at=1500, completed_at=2500,
              events=[ev("blocked", 2000)]),                       # pass
        _card("c2", started_at=1500, completed_at=2800,
              events=[ev("blocked", 2000), ev("unblocked", 2100),
                      ev("blocked", 2200)]),                        # rework
        _card("c3", started_at=2000, completed_at=3500,
              events=[ev("blocked", 3000)]),                        # pass
        _card("c4", started_at=500, completed_at=None, is_merged=False,
              events=[ev("submitted_for_review", 1000),
                      ev("unblocked", 4000)]),                      # sent back
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000)
    assert r["reviewed"] == 4
    assert r["first_time_pass"]["count"] == 2
    assert r["first_time_pass"]["n"] == 4
    assert r["first_time_pass"]["rate"] == pytest.approx(0.5)


def test_archived_cards_included_by_metrics():
    # The regression this file exists to prevent: completed cards are ARCHIVED
    # as part of the normal review flow, and if metrics excludes archived cards
    # it can only ever see work still open — which has no outcome yet. Every
    # figure then reports n=0 over a window full of merged, archived cards.
    #
    # Metrics computes over COMPLETED history, so archived cards MUST be counted.
    cards = [
        _card(f"c{i}", status="archived", started_at=1500, completed_at=2500 + i,
              is_merged=True, commits_ahead=0, events=[ev("blocked", 2000 + i)])
        for i in range(4)
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000)
    # Not n=0 — the whole point. Archived, completed cards are the history.
    assert r["reviewed"] == 4
    assert r["merged_count"] == 4
    assert r["first_time_pass"]["n"] == 4
    assert r["first_time_pass"]["count"] == 4
    assert r["throughput"]["n"] == 4
    # And they render as real figures, not "insufficient data (n=0)".
    text = "\n".join(cmd.render(r))
    assert "insufficient data" not in text


def test_archived_card_outside_window_still_excluded():
    # Including archived cards must not break the window: an archived card whose
    # completion is OUTSIDE the window still never counts, whatever its status.
    cards = [
        _card("c1", status="archived", started_at=100, completed_at=500,
              is_merged=True, commits_ahead=0, events=[ev("blocked", 400)]),
        _card("c2", status="archived", started_at=1500, completed_at=2500,
              is_merged=True, commits_ahead=0, events=[ev("blocked", 2000)]),
        _card("c3", status="archived", started_at=1600, completed_at=2600,
              is_merged=True, commits_ahead=0, events=[ev("blocked", 2100)]),
        _card("c4", status="archived", started_at=1700, completed_at=2700,
              is_merged=True, commits_ahead=0, events=[ev("blocked", 2200)]),
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000)
    assert r["merged_count"] == 3  # c1 completed before the window


def test_stall_rate_computed_from_git_commits():
    # 3 started cards, 2 stalled (zero commits, ran past threshold).
    cards = [
        _card("s1", status="running", started_at=1000, completed_at=None,
              is_merged=False, commits_ahead=0),
        _card("s2", status="running", started_at=1000, completed_at=None,
              is_merged=False, commits_ahead=0),
        _card("s3", status="running", started_at=9000, completed_at=None,
              is_merged=False, commits_ahead=0),  # fresh, not stalled
    ]
    r = metrics_core.compute(cards, since_ts=0, now=10000)
    assert r["started"] == 3
    assert r["stalled"]["count"] == 2
    assert r["stalled"]["rate"] == pytest.approx(2 / 3)


def test_stalled_requires_zero_commits():
    # A long-running card WITH commits is progress, not a stall.
    cards = [_card("c1", status="running", started_at=1000, completed_at=None,
                   is_merged=False, commits_ahead=5)]
    r = metrics_core.compute(cards, since_ts=0, now=10000)
    assert r["started"] == 1
    assert r["stalled"]["count"] == 0


def test_merged_card_never_counted_stalled():
    # A card that merged in the window is never stalled, even if it started
    # long ago and its branch now shows zero commits (it was absorbed into
    # main). commits_ahead==0 on a merged branch is LANDED, not a stall.
    cards = [_card("c1", started_at=1000, completed_at=8000,
                   is_merged=True, commits_ahead=0,
                   events=[ev("blocked", 2000)])]
    r = metrics_core.compute(cards, since_ts=0, now=10000)
    assert r["stalled"]["count"] == 0
    assert r["merged_count"] == 1


def test_review_latency_median_and_p90():
    # 5 merged cards with latencies 100,200,300,400,500 -> median 300.
    # p90 (exclusive interpolation over 5 samples) is ~540.
    cards = [
        _card(f"m{i}", completed_at=5000 + i * 100,
              events=[ev("blocked", 5000 + i * 100 - (100 + i * 100))])
        for i in range(5)
    ]
    r = metrics_core.compute(cards, since_ts=0, now=10000)
    assert r["review_latency"]["n"] == 5
    assert r["review_latency"]["median"] == pytest.approx(300)
    assert r["review_latency"]["p90"] == pytest.approx(540)


def test_throughput_per_day():
    since = 100000 - 3 * 86400
    cards = [
        _card(f"m{i}", completed_at=since + 1000 + i,
              events=[ev("blocked", since + 900 + i)])
        for i in range(5)
    ]
    r = metrics_core.compute(cards, since_ts=since, now=100000)
    assert r["throughput"]["n"] == 5
    assert r["throughput"]["per_day"] == pytest.approx(5 / 3)


def test_rework_more_than_one_round():
    cards = [
        _card("c1", completed_at=2500, events=[ev("blocked", 2000)]),
        _card("c2", completed_at=2800,
              events=[ev("blocked", 2000), ev("unblocked", 2100),
                      ev("blocked", 2200)]),
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000, min_sample=1)
    assert r["rework"]["count"] == 1
    assert r["rework"]["share"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Honesty: small samples print insufficient-data, never a misleading 0/100
# --------------------------------------------------------------------------- #


def test_no_data_prints_insufficient_not_zero():
    r = metrics_core.compute([], since_ts=1000, now=5000)
    assert r["first_time_pass"]["rate"] is None
    assert r["stalled"]["rate"] is None
    assert r["review_latency"]["median"] is None
    assert r["throughput"]["per_day"] is None
    assert r["rework"]["share"] is None
    # The renderer turns EVERY None figure into the insufficient-data string,
    # on every rate line — never a bare "-" and never a fabricated 0%/100%.
    text = "\n".join(cmd.render(r))
    lines = text.splitlines()
    for name in ("first-time-pass", "stall", "review latency", "throughput", "rework"):
        line = next(ln for ln in lines if name in ln)
        assert "insufficient data (n=0)" in line, line
    assert "0%" not in text and "100%" not in text


def test_small_sample_prints_insufficient_not_misleading_pct():
    # 2 reviewed cards, both passed first time -> would be 100%, but n=2 is
    # below MIN_SAMPLE_SIZE=3, so it must print insufficient, never 100%.
    cards = [
        _card("c1", completed_at=2500, events=[ev("blocked", 2000)]),
        _card("c2", completed_at=3500, events=[ev("blocked", 3000)]),
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000)  # default min=3
    text = "\n".join(cmd.render(r))
    assert "insufficient data (n=2)" in text
    assert "100%" not in text


def test_rate_from_n3_shown_with_sample_size():
    # Exactly MIN_SAMPLE_SIZE cards is enough to show, but n is stated.
    cards = [
        _card(f"c{i}", started_at=1500, completed_at=2500 + i,
              events=[ev("blocked", 2000 + i)])
        for i in range(3)
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000)
    text = "\n".join(cmd.render(r))
    assert "first-time-pass" in text
    assert "n=3" in text
    assert "insufficient data" not in text


# --------------------------------------------------------------------------- #
# Window honoured and stated
# --------------------------------------------------------------------------- #


def test_window_honoured_merged_outside_not_counted():
    # A card merged BEFORE the window must not contribute to throughput or
    # first-time-pass.
    cards = [
        _card("old", completed_at=900, events=[ev("blocked", 800)]),
        _card("in1", completed_at=2500, events=[ev("blocked", 2000)]),
        _card("in2", completed_at=2600, events=[ev("blocked", 2100)]),
        _card("in3", completed_at=2700, events=[ev("blocked", 2200)]),
    ]
    r = metrics_core.compute(cards, since_ts=1000, now=5000)
    assert r["merged_count"] == 3  # only the three inside the window
    # The window header is rendered on the table.
    text = "\n".join(cmd.render(r))
    assert "metrics:" in text


def test_window_is_stated_in_header():
    r = metrics_core.compute([], since_ts=1000, now=1000 + 86400)
    text = "\n".join(cmd.render(r))
    assert "last 24h" in text


# --------------------------------------------------------------------------- #
# Command layer: injected fixtures, no real board/git
# --------------------------------------------------------------------------- #


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "project": None,
        "since": None,
        "cwd": None,
        "run": None,
        "events": None,
        "now": None,
        "cards": None,
        "stderr": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class _OkRun:
    """A git runner that says every branch exists, is merged, has commits."""

    def __call__(self, c, repo):
        if c[1] == "merge-base":  # is_merged
            return argparse.Namespace(returncode=0, stdout="", stderr="")
        if c[1] == "rev-list":  # commits_ahead
            return argparse.Namespace(returncode=0, stdout="3", stderr="")
        return argparse.Namespace(returncode=0, stdout="", stderr="")


class _ZeroRun:
    """A git runner that says every branch has zero commits (stall signal)."""

    def __call__(self, c, repo):
        if c[1] == "merge-base":  # is_merged
            return argparse.Namespace(returncode=0, stdout="", stderr="")
        if c[1] == "rev-list":  # commits_ahead = 0
            return argparse.Namespace(returncode=0, stdout="0", stderr="")
        return argparse.Namespace(returncode=0, stdout="", stderr="")


def _registry_card(cid, status="done", started_at=1000, completed_at=2500):
    return {
        "id": cid,
        "title": f"task {cid}",
        "status": status,
        "board": "flightdeck",
        "branch": f"wt/{cid}",
        "started_at": started_at,
        "completed_at": completed_at,
        "workspace_path": f"/repo/.worktrees/{cid}",
    }


def test_command_renders_table_from_injected_inputs(capsys):
    from flightdeck.core.registry import Project
    proj = Project(name="flightdeck", repo="/repo", board="flightdeck")
    cards = [
        _registry_card("c1", completed_at=2500),
        _registry_card("c2", completed_at=2600),
        _registry_card("c3", completed_at=2700),
    ]

    def events(cid):
        return [ev("blocked", 2000)]

    rc = cmd.cmd_metrics(
        _args(cards=cards, run=_OkRun(), events=events,
              now=lambda: 5000, since="4000s"),
        [proj],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "metrics:" in out
    assert "first-time-pass" in out
    assert "n=3" in out
    assert "100%" in out  # 3 passed, 3 reviewed


def test_command_insufficient_when_no_data(capsys):
    rc = cmd.cmd_metrics(_args(cards=[], run=_OkRun(), events=lambda c: [],
                               now=lambda: 5000, since="4000s"), [])
    assert rc == 0
    out = capsys.readouterr().out
    assert "insufficient data (n=0)" in out
    assert "0%" not in out and "100%" not in out


def test_project_with_no_cards_reports_cleanly(capsys):
    from flightdeck.core.registry import Project
    proj = Project(name="empty", repo="/repo", board="flightdeck")
    # No cards, but the project itself exists.
    rc = cmd.cmd_metrics(_args(cards=[], run=_OkRun(), events=lambda c: [],
                               now=lambda: 5000, since="24h"), [proj])
    assert rc == 0
    out = capsys.readouterr().out
    assert "metrics:" in out          # renderer still runs
    assert "insufficient data (n=0)" in out


def test_json_matches_table(capsys):
    from flightdeck.core.registry import Project
    proj = Project(name="flightdeck", repo="/repo", board="flightdeck")
    cards = [
        _registry_card("c1", completed_at=2500),
        _registry_card("c2", completed_at=2600),
        _registry_card("c3", completed_at=2700),
    ]

    def events(cid):
        return [ev("blocked", 2000)]

    rc = cmd.cmd_metrics(_args(cards=cards, run=_OkRun(), events=events,
                               now=lambda: 5000, since="4000s", json=True),
                         [proj])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["first_time_pass"]["rate"] == pytest.approx(1.0)
    assert payload["first_time_pass"]["count"] == 3
    assert payload["first_time_pass"]["n"] == 3
    assert payload["reviewed"] == 3
    assert "window" in payload


def test_json_insufficient_is_null_not_zero(capsys):
    rc = cmd.cmd_metrics(_args(cards=[], run=_OkRun(), events=lambda c: [],
                               now=lambda: 5000, since="24h", json=True), [])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["first_time_pass"]["rate"] is None
    assert payload["throughput"]["per_day"] is None


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["metrics"])
    assert args.command == "metrics"
    assert args.func is not None


def test_unknown_project_errors():
    from flightdeck.core.registry import Project
    proj = Project(name="known", repo="/repo", board="b")
    rc = cmd.cmd_metrics(_args(cards=[], run=_OkRun(), events=lambda c: [],
                               now=lambda: 5000, since="24h",
                               project="nope"), [proj])
    assert rc == 2


def test_detects_project_from_cwd_and_scopes(capsys):
    """No project arg + cwd inside a repo -> metrics scoped, note on stderr."""
    from flightdeck.core.registry import Project
    proj = Project(name="flightdeck", repo=registry._expand("~/dev/flightdeck"), board="fd")
    cwd = registry._expand("~/dev/flightdeck") + "/flightdeck/core"
    cards = [
        _registry_card("c1", completed_at=2500),
        _registry_card("c2", completed_at=2600),
        _registry_card("c3", completed_at=2700),
    ]

    def events(cid):
        return [ev("blocked", 2000)]

    rc = cmd.cmd_metrics(_args(cards=cards, run=_OkRun(), events=events,
                               now=lambda: 5000, since="4000s", cwd=cwd), [proj])
    assert rc == 0
    captured = capsys.readouterr()
    assert "first-time-pass" in captured.out
    assert "using project 'flightdeck' (detected from cwd)" in captured.err


def test_no_cwd_match_means_whole_fleet(capsys):
    """No project arg + cwd outside any repo -> unchanged fleet-wide default.

    All three projects' cards feed the table, and no detection note prints.
    """
    from flightdeck.core.registry import Project
    projs = [
        Project(name="a", repo="/dev/a", board="a"),
        Project(name="b", repo="/dev/b", board="b"),
        Project(name="flightdeck", repo=registry._expand("~/dev/flightdeck"), board="fd"),
    ]
    cards = [
        _registry_card("c1", completed_at=2500),
        _registry_card("c2", completed_at=2600),
        _registry_card("c3", completed_at=2700),
    ]

    def events(cid):
        return [ev("blocked", 2000)]

    rc = cmd.cmd_metrics(_args(cards=cards, run=_OkRun(), events=events,
                               now=lambda: 5000, since="4000s",
                               cwd="/tmp/elsewhere"), projs)
    assert rc == 0
    captured = capsys.readouterr()
    assert "first-time-pass" in captured.out
    assert "detected from cwd" not in captured.err


def test_explicit_project_wins_over_cwd(capsys):
    """An explicit project arg beats cwd detection, with no detection note."""
    from flightdeck.core.registry import Project
    a = Project(name="a", repo="/dev/a", board="a")
    b = Project(name="b", repo="/dev/b", board="b")
    cwd = registry._expand("~/dev/a") + "/sub"
    cards = [_registry_card("c1", completed_at=2500)]

    def events(cid):
        return [ev("blocked", 2000)]

    rc = cmd.cmd_metrics(_args(cards=cards, run=_OkRun(), events=events,
                               now=lambda: 5000, since="4000s",
                               project="b", cwd=cwd), [a, b])
    assert rc == 0
    captured = capsys.readouterr()
    assert "detected from cwd" not in captured.err


# --------------------------------------------------------------------------- #
# Only metrics reads history; other commands still exclude archived cards
# --------------------------------------------------------------------------- #
#
# Metrics is the ONE command that must see completed (archived) history. The
# non-metrics commands deliberately exclude archived cards at their reader —
# `standup`, `qa` and `reconcile` call `list_cards()` without opting into
# archived cards. This guard asserts that contract stays true (metrics alone
# passes `include_archived=True`, and only metrics' own reader may).


def _list_cards_calls(module) -> list[str]:
    """Every `list_cards(...)` call expression in a command module's source."""
    import re

    src = _module_source(module)
    return [
        m.group(0)
        for m in re.finditer(r"list_cards\s*\([^)]*\)", src)
    ]


def _module_source(module) -> str:
    import inspect

    return inspect.getsource(module)


def test_non_metrics_commands_still_exclude_archived():
    # standup, qa and reconcile read cards WITHOUT `include_archived=True`, so
    # their reader still excludes archived cards — only metrics needs history.
    from flightdeck.commands import qa, reconcile, standup

    for module in (standup, qa, reconcile):
        calls = _list_cards_calls(module)
        assert calls, f"{module.__name__} must read cards via list_cards(...)"
        for call in calls:
            assert "include_archived=True" not in call, (
                f"{module.__name__} must not opt into archived cards: {call}"
            )


def test_metrics_command_reads_archived_cards():
    # The flip side: the metrics command's reader explicitly opts into archived
    # cards so it can compute over completed history (this is what fixes n=0).
    assert "include_archived=True" in _module_source(cmd)
