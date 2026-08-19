"""Tests for `flightdeck roadmap progress` — linking cards to milestones.

``progress`` attributes each kanban card to a project (by workspace_path), reads
the ``MILESTONE: <id>`` tag in its body to link it to a milestone, and renders
per-milestone counts from the linked cards' statuses.

The board is stubbed by monkeypatching ``kanban.list_cards`` (the same seam the
reconcile command tests use), and the project lives on a real tmp_path repo so
``project_roadmap`` resolves a real ROADMAP.md. No test touches git, the live
board, Telegram or the network.
"""

import argparse
import json

import pytest

from flightdeck.commands import roadmap as cmd
from flightdeck.core import kanban, roadmap as rm
from flightdeck.core.registry import Project

# A new-style roadmap with two subprojects and two milestones (stable ids).
ROADMAP = """\
# Subproject: client-portal

## Milestone: auth-hardening <!-- id: auth-hardening -->
status: now
- [ ] server-side key enforcement
- [x] password reset restricted to self/admin

# Subproject: billing

## Milestone: refunds <!-- id: refunds -->
status: next
- [ ] refund webhook
"""


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "project": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(repo, name="hscc"):
    return Project(name=name, repo=str(repo))


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _card(cid, *, status="todo", repo, body="", board="hscc"):
    return {
        "id": cid,
        "title": f"task {cid}",
        "status": status,
        "board": board,
        "branch": f"wt/{cid}",
        "workspace_path": repo,
        "body": body,
    }


@pytest.fixture
def proj(tmp_path):
    return _project(tmp_path)


def _roadmap(tmp_path):
    return _write(tmp_path / "ROADMAP.md", ROADMAP)


# --------------------------------------------------------------------------- #
# linking: a MILESTONE: x body links the card
# --------------------------------------------------------------------------- #


def test_milestone_tag_links_card(tmp_path, proj):
    _roadmap(tmp_path)
    cards = [_card("a", status="running", repo=str(tmp_path),
                   body="plans\nMILESTONE: auth-hardening\n")]

    data = cmd._aggregate_progress([proj], cards)

    ms = data["hscc"]["milestones"]
    auth = next(m for m in ms if m["id"] == "auth-hardening")
    assert auth["total"] == 1
    assert auth["running"] == 1
    assert auth["done"] == 0
    # A tag pointing at the OTHER milestone does not link to auth-hardening.
    refunds = next(m for m in ms if m["id"] == "refunds")
    assert refunds["total"] == 0


def test_case_and_leading_whitespace_variants_link(tmp_path, proj):
    _roadmap(tmp_path)
    cards = [
        _card("a", body="  milestone:  AUTH-HARDENING", repo=str(tmp_path)),  # case + indent
        _card("b", body="\tMilestone: auth-hardening", repo=str(tmp_path)),    # tab indent
        _card("c", body="MILESTONE: Auth-Hardening", repo=str(tmp_path)),      # mixed-case id
    ]

    data = cmd._aggregate_progress([proj], cards)

    auth = next(m for m in data["hscc"]["milestones"] if m["id"] == "auth-hardening")
    assert auth["total"] == 3
    assert data["hscc"]["unlinked"] == 0


# --------------------------------------------------------------------------- #
# unlinked cards are counted, never dropped
# --------------------------------------------------------------------------- #


def test_card_without_tag_is_unlinked_not_dropped(tmp_path, proj):
    _roadmap(tmp_path)
    cards = [_card("a", body="no tag anywhere", repo=str(tmp_path))]

    data = cmd._aggregate_progress([proj], cards)

    auth = next(m for m in data["hscc"]["milestones"] if m["id"] == "auth-hardening")
    assert auth["total"] == 0          # not guessed into a milestone
    assert data["hscc"]["unlinked"] == 1   # but still counted, not dropped


def test_unattributed_card_is_skipped(tmp_path, proj):
    _roadmap(tmp_path)
    # workspace_path on a DIFFERENT repo -> attributes to no project.
    cards = [_card("a", body="MILESTONE: auth-hardening", repo="/elsewhere")]

    data = cmd._aggregate_progress([proj], cards)

    assert data["hscc"]["unlinked"] == 0
    assert all(m["total"] == 0 for m in data["hscc"]["milestones"])


# --------------------------------------------------------------------------- #
# counts come from the linked cards' statuses
# --------------------------------------------------------------------------- #


def test_counts_match_card_statuses(tmp_path, proj):
    _roadmap(tmp_path)
    cards = [
        _card("a", status="done", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("b", status="closed", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("c", status="review", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("d", status="blocked", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("e", status="running", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("f", status="todo", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
    ]

    data = cmd._aggregate_progress([proj], cards)

    auth = next(m for m in data["hscc"]["milestones"] if m["id"] == "auth-hardening")
    assert auth["total"] == 6
    assert auth["done"] == 2
    assert auth["awaiting_review"] == 2
    assert auth["running"] == 1


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def test_milestone_with_no_cards_still_renders_not_started(tmp_path, proj, monkeypatch, capsys):
    # No cards at all; the `refunds` milestone has 0 items done and 0 cards, so
    # it reports not started — with BOTH its (open) item count and card count
    # visible, never a bare `0/0 · not started` that hides the roadmap.
    _roadmap(tmp_path)
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])

    rc = cmd.cmd_progress(_args(), [proj])

    assert rc == 0
    out = capsys.readouterr().out
    assert "refunds" in out
    assert "items 0/1" in out   # the roadmap's open item is never hidden
    assert "cards 0" in out
    assert "not started" in out


def test_project_without_roadmap_says_no_roadmap(tmp_path, proj, monkeypatch, capsys):
    # No ROADMAP.md written.
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])

    rc = cmd.cmd_progress(_args(), [proj])

    assert rc == 0
    assert "no roadmap" in capsys.readouterr().out


def test_render_full_line(tmp_path, proj):
    _roadmap(tmp_path)
    cards = [
        _card("a", status="done", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("b", status="review", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("c", status="running", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("d", status="done", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
    ]
    data = cmd._aggregate_progress([proj], cards)
    line = cmd._milestone_line(
        next(m for m in data["hscc"]["milestones"] if m["id"] == "auth-hardening")
    )

    assert "client-portal / auth-hardening" in line
    assert "[now]" in line
    # ITEM counts come from the roadmap file: this milestone has 2 items, 1 done.
    assert "items 1/2" in line
    # CARD counts come from linked cards: 4 cards, 2 done.
    assert "cards 4" in line
    assert "1 awaiting review" in line
    assert "1 running" in line
    # Cards exist, so no state word (neither complete nor not started).
    assert "not started" not in line
    assert "complete" not in line


# --------------------------------------------------------------------------- #
# N14: roadmap progress counts ITEMS (from the roadmap file) as well as cards
# --------------------------------------------------------------------------- #
#
# The live failure: a freshly adopted roadmap has milestones with all items
# ticked but zero kanban cards yet, and progress rendered them as `0/0 ·
# not started` — the most complete project in the fleet read as untouched.


def test_all_items_ticked_no_cards_renders_complete_never_not_started(tmp_path, proj, monkeypatch, capsys):
    # The live hscc scenario: milestone's items are all `- [x]` but no kanban
    # cards exist yet. It must read COMPLETE, never "not started".
    _write(tmp_path / "ROADMAP.md", """\
# Subproject: hscc

## Milestone: monitoring-daemon <!-- id: monitoring-daemon -->
status: later
- [x] telemetry collection
- [x] alert routing
- [x] fleet reconciliation
""")
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])

    rc = cmd.cmd_progress(_args(), [proj])

    assert rc == 0
    out = capsys.readouterr().out
    assert "items 3/3" in out
    assert "cards 0" in out
    assert "complete" in out
    assert "not started" not in out


def test_some_items_done_and_some_cards_shows_both_figures(tmp_path, proj, monkeypatch, capsys):
    # A milestone with a mix of roadmap items done AND linked cards shows both
    # figures — neither source hides the other.
    _write(tmp_path / "ROADMAP.md", """\
## Milestone: oracle-track <!-- id: oracle-track -->
status: now
- [x] oracle setup
- [ ] oracle sync
- [x] oracle backup
- [ ] oracle failover
""")
    cards = [
        _card("a", status="done", repo=str(tmp_path), body="MILESTONE: oracle-track"),
        _card("b", status="review", repo=str(tmp_path), body="MILESTONE: oracle-track"),
        _card("c", status="running", repo=str(tmp_path), body="MILESTONE: oracle-track"),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)

    rc = cmd.cmd_progress(_args(), [proj])

    assert rc == 0
    out = capsys.readouterr().out
    # items 2/4 from the roadmap; cards 3 (1 awaiting review, 1 running).
    assert "items 2/4" in out
    assert "cards 3" in out
    assert "1 awaiting review" in out
    assert "1 running" in out

    # JSON carries both numbers too.
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    rc = cmd.cmd_progress(_args(json=True), [proj])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ms = next(m for m in payload["hscc"]["milestones"] if m["id"] == "oracle-track")
    assert (ms["items"]["done"], ms["items"]["total"]) == (2, 4)
    assert (ms["cards"]["total"], ms["cards"]["done"],
            ms["cards"]["awaiting_review"], ms["cards"]["running"]) == (3, 1, 1, 1)


def test_unknown_project_is_an_error(tmp_path, proj, monkeypatch, capsys):
    _roadmap(tmp_path)
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [])

    rc = cmd.cmd_progress(_args(project="nope"), [proj])

    assert rc == 2
    assert "no project named" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# JSON carries the same numbers as the text
# --------------------------------------------------------------------------- #


def test_json_matches_text_counts(tmp_path, proj, monkeypatch, capsys):
    _roadmap(tmp_path)
    cards = [
        _card("a", status="done", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("b", status="review", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("c", status="running", repo=str(tmp_path), body="MILESTONE: auth-hardening"),
        _card("d", status="todo", repo=str(tmp_path), body="MILESTONE: refunds"),
        _card("e", body="no tag", repo=str(tmp_path)),
    ]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)

    rc = cmd.cmd_progress(_args(json=True), [proj])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    hscc = payload["hscc"]
    assert hscc["present"] is True
    auth = next(m for m in hscc["milestones"] if m["id"] == "auth-hardening")
    # CARD counts: 3 linked cards, 1 done / 1 review / 1 running.
    assert (auth["cards"]["total"], auth["cards"]["done"],
            auth["cards"]["awaiting_review"], auth["cards"]["running"]) == (3, 1, 1, 1)
    # ITEM counts from the roadmap: 2 items, 1 done.
    assert (auth["items"]["total"], auth["items"]["done"]) == (2, 1)
    refunds = next(m for m in hscc["milestones"] if m["id"] == "refunds")
    assert (refunds["cards"]["total"], refunds["cards"]["done"]) == (1, 0)
    assert (refunds["items"]["total"], refunds["items"]["done"]) == (1, 0)
    assert hscc["unlinked"] == 1

    # The text render carries exactly the same numbers.
    data = cmd._aggregate_progress([proj], cards)
    text = "\n".join(cmd._render_progress(data))
    # auth in text form: items 1/2 · cards 3 (1 awaiting review, 1 running)
    assert "items 1/2" in text and "cards 3" in text
    assert "1 awaiting review" in text and "1 running" in text
    # refunds in text form: items 0/1 · cards 1
    assert "items 0/1" in text and "cards 1" in text


def test_command_is_discovered():
    from flightdeck.cli import build_parser

    args = build_parser().parse_args(["roadmap", "progress"])
    assert args.command == "roadmap"
    assert args.roadmap_cmd == "progress"
    assert args.func is not None
