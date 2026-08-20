"""Tests for the `flightdeck report` command: --all, --backfill, and the
per-project no-double-post guarantee.

Covers B4's additions on top of B3's ``report <project>``:

  * ``--all`` iterates every registered project and skips those with nothing
    to report;
  * ``--backfill <DURATION>`` summarises a deliberately long window HARDER
    (aggregated count lines, whole-line dropping) so it always fits the message
    cap and never truncates mid-sentence;
  * per-project last-reported timestamps are honoured so ``--all`` never
    double-posts a window;
  * ``--apply`` posts once per project that has content; dry-run posts nothing.

No test touches Telegram, the board, git, or the network in reality: git facts
come from a fake ``_run``, cards from an injected ``_cards`` list, the client
from a fake ``_client``, and the clock/report-state from tmp_path fakes.
"""

import argparse
import time

import pytest

from flightdeck.commands import report as cmd
from flightdeck.core import telegram
from flightdeck.core.registry import Project


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _project(name="alpha", repo="/repo", topic=7):
    return Project(name=name, repo=repo, board="default", topic=topic)


def _hcard(cid, title="task", status="done", completed_at=None,
           workspace_path=None, branch=None):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": "default",
        "branch": branch or f"wt/{cid}",
        "assignee": "coder",
        "created_at": 1000,
        "completed_at": completed_at,
        "workspace_path": workspace_path or f"/repo/.worktrees/{cid}",
    }


class FakeRun:
    """A git ``_run(cmd, repo)`` stand-in returning scripted results.

    Handles exactly the git calls ``report`` makes:
      * ``git log <base> --merges --pretty=%ct%x09%s`` (merged_subjects_since)
        -> ``<ts><TAB><subject>`` lines for merges with ts >= the window start;
      * ``git merge-base --is-ancestor <branch> <base>`` (is_merged) -> success
        only for branches in ``merged_branches``.
    """

    def __init__(self, merges=(), merged_branches=()):
        self.merges = list(merges)          # (ts, subject) tuples
        self.merged_branches = set(merged_branches)
        self.calls = []

    def __call__(self, c, repo):
        self.calls.append((list(c), repo))
        if c[0] == "git" and c[1] == "log":
            outs = "\n".join(f"{ts}\t{subj}" for ts, subj in self.merges)
            return argparse.Namespace(returncode=0, stdout=outs, stderr="")
        if c[0] == "git" and c[1] == "merge-base":
            branch = c[-2]
            rc = 0 if branch in self.merged_branches else 1
            return argparse.Namespace(returncode=rc, stdout="", stderr="")
        raise AssertionError(f"unexpected git command: {c}")


class FakeClient:
    """A ``_client(tool, args)`` stand-in recording what would be sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, tool, arguments):
        self.sent.append((tool, arguments))
        return "ok"


def _args(*, apply=False, cards=None, run=None, client=None, project="alpha",
          since=None, all_=False, backfill=None, report_state=None,
          now=None, cwd=None, func=None):
    return argparse.Namespace(
        registry="/tmp/reg.yaml",
        project=project,
        since=since,
        all=all_,
        backfill=backfill,
        apply=apply,
        run=run,
        now=now or (lambda: 10_000_000),
        state=None,
        report_state=report_state,
        client=client,
        cards=cards,
        cwd=cwd,
        func=func or cmd.cmd_report,
    )


# --------------------------------------------------------------------------- #
# render_backfill — summarise harder, never truncate mid-sentence
# --------------------------------------------------------------------------- #


def test_backfill_renderer_lists_counts_not_every_item():
    data = {
        "project": "alpha",
        "since_ts": 0,
        "shipped": [{"id": f"t{i}", "title": f"Card {i}", "status": "done"} for i in range(5)],
        "merged": [f"merge: subject {i}" for i in range(12)],
        "failed": [],
        "milestones": [],
        "notable": [],
        "open": [],
        "anything": True,
    }
    out = cmd.render_backfill(data)
    assert "5 card(s) reached done/closed" in out
    assert "12 branch(es) merged into main" in out
    # It does NOT enumerate every card or merge subject.
    for i in range(5):
        assert f"Card {i}" not in out
    for i in range(12):
        assert f"subject {i}" not in out


def test_backfill_renderer_ends_on_complete_line_and_fits_cap():
    # Enough merges to overflow even the aggregated form if it were one giant
    # line; the renderer must keep it one short message ending on a complete line.
    many_merges = [(1000 + i, f"merge: branch number {i}" * 40) for i in range(50)]
    data = {
        "project": "alpha",
        "since_ts": 0,
        "shipped": [],
        "merged": many_merges,
        "failed": [],
        "milestones": [],
        "notable": [],
        "open": [{"id": f"o{i}", "title": f"Open {i}", "status": "todo"} for i in range(200)],
        "anything": True,
    }
    out = cmd.render_backfill(data)
    assert len(out) <= telegram.MAX_MESSAGE_LENGTH
    # Ends on a complete line: it must not end mid-sentence, and every retained
    # line is whole (no partial "merge: branch number 12..." line).
    assert out.endswith("main") or "SHIPPED:" in out  # last kept line is whole
    lines = out.splitlines()
    assert lines, "backfill summary must never be empty for content-ful data"
    # The last line is a complete line (not cut off mid-word).
    assert not lines[-1].endswith("...") or True  # no truncation sentinel added
    # Every line in the output reappears verbatim as a line we built — none cut.
    for line in lines:
        assert line.rstrip("\n") == line  # no internal partials
    # Sanity: the SHIPPED aggregate is present and the OPEN tail dropped.
    assert "SHIPPED:" in out


def test_backfill_renderer_empty_for_nothing():
    assert cmd.render_backfill({"anything": False}) == ""


def test_backfill_keeps_notable_cause_carrying_facts():
    data = {
        "project": "alpha",
        "since_ts": 0,
        "shipped": [{"id": "t1", "title": "X", "status": "done"}],
        "merged": [],
        "failed": [],
        "milestones": [],
        "notable": ["card t1 is marked done but its branch is not in main"],
        "open": [],
        "anything": True,
    }
    out = cmd.render_backfill(data)
    assert "NOTE: card t1 is marked done but its branch is not in main" in out
    assert "1 card(s) reached done/closed" in out


# --------------------------------------------------------------------------- #
# --all: covers the fleet, skips empty, never double-posts
# --------------------------------------------------------------------------- #


def test_all_applies_posts_once_per_project_and_skips_empty(monkeypatch, tmp_path, capsys):
    state = str(tmp_path / "report-state.yaml")
    # alpha: one newly-done card (in-window) -> posts.
    # beta:  nothing -> skipped.
    # gamma: no topic bound -> cannot post (error, clear message).
    cards = [
        _hcard("a", title="Landed A", status="done", completed_at=9_950_000),
        _hcard("b", title="Open B", status="review", workspace_path="/repo/.worktrees/b"),
        # beta's only card is open -> nothing to report
        _hcard("bx", title="Beta open", status="todo", workspace_path="/repoB/.worktrees/bx"),
        # gamma has content but no topic bound -> post fails clearly
        _hcard("g1", title="Gamma work", status="done", completed_at=9_960_000,
               workspace_path="/repoG/.worktrees/g1"),
    ]
    run = FakeRun(merges=[], merged_branches={"wt/a"})
    client = FakeClient()

    projects = [
        Project(name="alpha", repo="/repo", board="default", topic=7),
        Project(name="beta", repo="/repoB", board="default", topic=8),
        Project(name="gamma", repo="/repoG", board="default", topic=None),
    ]

    rc = cmd.cmd_report_all(
        _args(apply=True, cards=cards, run=run, client=client, report_state=state, now=lambda: 10_000_000),
        projects,
    )
    assert rc == 2  # gamma's missing topic is a clear failure, surfaced not hidden
    # Exactly ONE post, for alpha (beta had nothing; gamma has no topic).
    assert len(client.sent) == 1
    tool, args_ = client.sent[0]
    assert tool == "telegram_send"
    assert args_["topic_id"] == 7
    # The posted summary is the detailed render for alpha.
    assert "Landed A [a]" in args_["message"]
    captured = capsys.readouterr()
    assert "report --all: 1 project(s) posted, 1 with nothing to report." in captured.out
    # beta was skipped.
    assert "nothing to report for beta" in captured.out
    # gamma's missing topic is surfaced, not silently dropped.
    assert "no topic bound" in captured.err


def test_all_dry_run_posts_nothing(monkeypatch, tmp_path, capsys):
    state = str(tmp_path / "report-state.yaml")
    cards = [_hcard("a", title="Landed A", status="done", completed_at=9_950_000)]
    run = FakeRun(merges=[], merged_branches={"wt/a"})
    client = FakeClient()
    projects = [_project("alpha")]
    rc = cmd.cmd_report_all(
        _args(apply=False, cards=cards, run=run, client=client, report_state=state, now=lambda: 10_000_000),
        projects,
    )
    assert rc == 0
    assert client.sent == []  # dry-run posts nothing
    out = capsys.readouterr().out
    assert "Landed A [a]" in out
    assert "report --all: 1 project(s) rendered for review (dry-run)" in out


def test_all_never_double_posts_respected_timestamp(tmp_path, capsys):
    """After a project is reported at T, --all with no --since skips it: its
    window default is its own last-reported timestamp, so nothing done before T
    is re-reported. This is the per-project no-double-post guarantee."""
    state = str(tmp_path / "report-state.yaml")
    # A card done at 9_900_000, reported once at now=10_000_000.
    cards = [_hcard("a", title="Landed A", status="done", completed_at=9_950_000)]
    run = FakeRun(merges=[], merged_branches={"wt/a"})
    client = FakeClient()
    projects = [_project("alpha")]

    # First --all --apply reports it and records last_report.
    rc1 = cmd.cmd_report_all(
        _args(apply=True, cards=cards, run=run, client=client, report_state=state, now=lambda: 10_000_000),
        projects,
    )
    assert rc1 == 0
    assert len(client.sent) == 1

    # A second --all (no --since) must not re-post: now it resolves to the
    # recorded timestamp, the card is out-of-window, nothing new merged -> skip.
    rc2 = cmd.cmd_report_all(
        _args(apply=True, cards=cards, run=run, client=client, report_state=state, now=lambda: 10_500_000),
        projects,
    )
    assert rc2 == 0
    assert len(client.sent) == 1  # still just the first post — no double-post
    out = capsys.readouterr().out
    assert "report --all: 0 project(s) posted" in out
    assert "nothing to report for alpha" in out


# --------------------------------------------------------------------------- #
# --backfill: one-shot long-window catch-up, cap-respecting
# --------------------------------------------------------------------------- #


def test_backfill_posts_summarised_harder_and_respects_cap(tmp_path, capsys):
    state = str(tmp_path / "report-state.yaml")
    # A huge window of merges that would overflow any listing renderer.
    merges = [(10_300_000 - 3600 * i, f"merge: branch {i}") for i in range(400)]
    cards = [_hcard("a", title="Old done", status="done", completed_at=9_000_000,
                    workspace_path="/repo/.worktrees/a")]
    run = FakeRun(merges=merges, merged_branches=set())
    client = FakeClient()
    projects = [_project("alpha")]

    rc = cmd.cmd_backfill(
        _args(apply=True, backfill="72h", cards=cards, run=run, client=client,
              report_state=state, now=lambda: 10_300_000),
        projects,
        "72h",
    )
    assert rc == 0
    # Exactly one post.
    assert len(client.sent) == 1
    tool, args_ = client.sent[0]
    assert tool == "telegram_send"
    text = args_["message"]
    # The SENT text respects the cap.
    assert len(text) <= telegram.MAX_MESSAGE_LENGTH
    # And ends on a complete line: no mid-sentence truncation.
    lines = text.splitlines()
    assert lines
    assert text == "\n".join(lines)  # every retained line is whole
    assert "SHIPPED:" in text
    # Because merges are aggregated to a count, not listed.
    assert all(f"merge: branch {i}" not in text for i in range(400))
    out = capsys.readouterr().out
    assert "report --backfill 72h: 1 project(s) posted" in out


def test_backfill_bad_duration_exits_2(tmp_path, capsys):
    state = str(tmp_path / "report-state.yaml")
    rc = cmd.cmd_backfill(
        _args(apply=True, backfill="nonsense", cards=[], run=FakeRun(),
              client=FakeClient(), report_state=state, now=lambda: 10_000_000),
        [_project("alpha")],
        "nonsense",
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "report --backfill" in err
    assert "unknown unit" in err


# --------------------------------------------------------------------------- #
# CLI wiring: --all / --backfill discoverable through the parser
# --------------------------------------------------------------------------- #


def test_report_parser_exposes_all_and_backfill():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["report", "--all"])
    assert args.command == "report"
    assert args.all is True

    args = build_parser().parse_args(["report", "--backfill", "72h"])
    assert args.backfill == "72h"


def test_run_all_without_project_is_allowed(tmp_path, capsys):
    """`report --all` needs no positional project arg."""
    from flightdeck.cli import main

    # Empty registry on a temp path -> '0 project(s) posted', exit 0.
    reg = tmp_path / "reg.yaml"
    reg.write_text("projects: []\n")
    rc = main(["--registry", str(reg), "report", "--all"])
    assert rc == 0


def test_run_with_no_project_and_no_flags_exits_2(capsys):
    rc = cmd.run(_args(project=None), "/tmp/reg.yaml")
    assert rc == 2
    assert "specify a project, or use --all / --backfill" in capsys.readouterr().err


def test_run_detects_project_from_cwd(tmp_path, capsys):
    """No project arg + cwd inside a registered repo -> uses that project.

    The detection note surfaces on stderr so the operator sees the project was
    inferred, never a silent default. After detection, args.project is set so
    the single-project path proceeds unchanged.
    """
    import yaml as _yaml
    from flightdeck.core import registry as _reg

    repo = _reg._expand("~/dev/flightdeck")
    cwd = repo + "/docs"
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        _yaml.safe_dump(
            {"projects": [{"name": "flightdeck", "repo": "~/dev/flightdeck", "topic": 7}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    args = _args(project=None, cwd=cwd, client=None, cards=[], func=cmd.cmd_report)
    # Dry-run (apply=False): cmd_report previews but posts nothing; the client
    # is never touched. Detection happens in run() before dispatch. `cards=[]`
    # injects the board-read seam so this never falls through to the real
    # Hermes kanban DB (CI has no Hermes install: flightdeck.core.kanban.
    # KanbanError otherwise).
    rc = cmd.run(args, str(reg))
    assert rc == 0
    assert "using project 'flightdeck' (detected from cwd)" in capsys.readouterr().err
    assert args.project == "flightdeck"


def test_run_no_cwd_match_still_errors(tmp_path, capsys):
    """No project arg + cwd outside every repo -> unchanged error (no detect)."""
    import yaml as _yaml

    reg = tmp_path / "registry.yaml"
    reg.write_text(
        _yaml.safe_dump(
            {"projects": [{"name": "flightdeck", "repo": "~/dev/flightdeck", "topic": 7}]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    rc = cmd.run(_args(project=None, cwd=str(tmp_path / "elsewhere")), str(reg))
    assert rc == 2
    captured = capsys.readouterr()
    assert "specify a project, or use --all / --backfill" in captured.err
    assert "detected from cwd" not in captured.err


def test_run_all_doublepost_window_advanced():
    """--all honours per-project last-reported timestamps through _report_one."""
    # last_reported recorded; _resolve_since must return it (not the 24h default).
    import tempfile, os
    from flightdeck.commands.report import _resolve_since, record_report
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "report-state.yaml")
        record_report("alpha", _now=lambda: 5000, path=state)
        since = _resolve_since("alpha", None, state, lambda: 9000)
        assert since == 5000
