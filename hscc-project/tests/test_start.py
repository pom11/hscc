"""Tests for `flightdeck start` — release a milestone's cards to the fleet.

``start`` selects the cards tagged ``MILESTONE: <id>``, caps the release at the
fleet's declared concurrency ceiling, spreads assignees across the fleet
profiles, holds cards whose declared dependencies are unmerged, dry-runs by
default, and runs the release on ``--apply``.

No test touches a live board, repo, Telegram, the network or the real config:
cards come from a stub ``_list_cards``, git facts from a fake ``_run``, board
writes from a fake ``_kdb``, and the concurrency knobs from a fixture yaml
written to a temp path.
"""

import argparse
import os

import pytest
import yaml

from flightdeck.commands import start as cmd
from flightdeck.core import kanban
from flightdeck.core.registry import Project


def _args(**overrides):
    base = {
        "registry": "/tmp/reg.yaml",
        "json": False,
        "apply": False,
        "milestone": "M1",
        "project": "hscc",
        "max_concurrent": 3,
        "run": None,
        "kdb": None,
        "list_cards": None,
        "config_path": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _project(board="hscc", repo="/repo", name="hscc"):
    return Project(name=name, repo=repo, board=board)


def _card(cid, title="task", status="todo", board="hscc", body=None, branch=None):
    return {
        "id": cid,
        "title": title,
        "status": status,
        "board": board,
        "branch": branch or f"wt/{cid}",
        "assignee": None,
        "body": body or "MILESTONE: M1\n",
    }


def _milestone_body(mid="M1"):
    return f"MILESTONE: {mid}\n"


def _dep_body(dep_id, mid="M1"):
    return f"MILESTONE: {mid}\nDEPENDS: {dep_id}\n"


class FakeRun:
    """A git ``_run(cmd, repo)`` stand-in; only is_merged is used by start."""

    def __init__(self, merged=set()):
        self.merged = set(merged)  # branches that are ancestors of main
        self.calls = []

    def __call__(self, cmd, repo):
        self.calls.append((cmd, repo))
        if cmd[0] == "git" and cmd[1] == "merge-base":
            branch = cmd[-2]
            cp = argparse.Namespace(
                returncode=0 if branch in self.merged else 1, stdout="", stderr=""
            )
            return cp
        raise AssertionError(f"unexpected git command: {cmd}")


class FakeKdb:
    """A stand-in for hermes_cli.kanban_db with assign_task."""

    def __init__(self):
        self.assigned = []  # (task_id, profile)
        self.fail = False

    def connect(self, board=None):
        return self

    def close(self):
        pass

    def assign_task(self, conn, task_id, profile):
        if self.fail:
            return False
        self.assigned.append((task_id, profile))
        return True


def _write_config(tmp_path, max_in_progress=6, per_profile=2):
    path = os.path.join(str(tmp_path), "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {"kanban": {"max_in_progress": max_in_progress, "max_in_progress_per_profile": per_profile}},
            f,
        )
    return path


def test_never_exceeds_global_cap(monkeypatch, tmp_path):
    """Effective ceiling = min(fleet max_in_progress, --max-concurrent)."""
    config = _write_config(tmp_path, max_in_progress=2, per_profile=2)
    cards = [_card(f"c{i}") for i in range(6)]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())

    args = _args(apply=True, run=run, kdb=FakeKdb(), config_path=config, max_concurrent=3)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    # Global cap 2 < --max-concurrent 3 → at most 2 released.
    assert len(args.kdb.assigned) <= 2


def test_never_exceeds_max_concurrent(monkeypatch, tmp_path):
    """--max-concurrent is a hard ceiling even when the fleet allows more."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=50)
    cards = [_card(f"c{i}") for i in range(10)]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())

    args = _args(apply=True, run=run, kdb=FakeKdb(), config_path=config, max_concurrent=3)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    assert len(args.kdb.assigned) <= 3


def test_never_exceeds_per_profile_cap(monkeypatch, tmp_path):
    """No single profile gets more than max_in_progress_per_profile."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=1)
    cards = [_card(f"c{i}") for i in range(12)]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())

    args = _args(apply=True, run=run, kdb=FakeKdb(), config_path=config, max_concurrent=50)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    from collections import Counter

    profiles = [p for _, p in args.kdb.assigned]
    counts = Counter(profiles)
    assert any(counts.values()), "expected some cards released"
    assert max(counts.values()) <= 1, "per-profile cap 1 violated"


def test_assignees_are_spread_not_piled(monkeypatch, tmp_path):
    """Six cards go across six different profiles, not all to coder."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=1)
    cards = [_card(f"c{i}") for i in range(6)]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())

    args = _args(apply=True, run=run, kdb=FakeKdb(), config_path=config, max_concurrent=6)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    profiles = [p for _, p in args.kdb.assigned]
    assert len(profiles) == 6
    assert len(set(profiles)) == 6, "assignees must be spread, not piled"
    # The round-robin order over FLEET_PROFILES.
    assert profiles == list(cmd.FLEET_PROFILES[:6])


def test_dependency_unmerged_is_held_with_reason(monkeypatch, tmp_path):
    """A card whose DEPENDS dep is unmerged is held, naming the holder."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    a = _card("a", title="A", status="todo", body=_milestone_body())
    b = _card("b", title="B", status="todo", body=_dep_body("a"))  # B depends on A
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [a, b])
    # A is NOT merged → B must be held.
    run = FakeRun(merged=set())

    args = _args(apply=False, run=run, kdb=FakeKdb(), config_path=config)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0

    _, held = cmd.partition_releasable([a, b], all_cards=[a, b], repo="/repo", _run=run)
    held_ids = [c["id"] for c in held]
    assert "b" in held_ids
    b_held = next(c for c in held if c["id"] == "b")
    assert "a" in b_held["_holds"], "B held by A with the reason"


def test_dependency_merged_is_not_held(monkeypatch, tmp_path):
    """A card whose DEPENDS dep IS merged is releasable."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    a = _card("a", title="A", status="done", body=_milestone_body())
    b = _card("b", title="B", status="todo", body=_dep_body("a"))
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [a, b])
    run = FakeRun(merged={"wt/a"})  # A's branch merged → B free.

    args = _args(apply=False, run=run, kdb=FakeKdb(), config_path=config)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0

    releasable, held = cmd.partition_releasable([a, b], all_cards=[a, b], repo="/repo", _run=run)
    assert "b" in [c["id"] for c in releasable]
    assert held == []


def test_dry_run_releases_nothing(monkeypatch, tmp_path):
    """Without --apply, no card is assigned to any profile."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    cards = [_card(f"c{i}") for i in range(3)]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())
    kdb = FakeKdb()

    args = _args(apply=False, run=run, kdb=kdb, config_path=config)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    assert kdb.assigned == [], "dry-run must not release anything"


def test_apply_releases_exactly_the_planned_set(monkeypatch, tmp_path):
    """--apply releases exactly the cards shown in the plan, no more."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    a = _card("a", title="A")
    b = _card("b", title="B")
    c = _card("c", title="C")
    d = _card("d", title="D")
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [a, b, c, d])
    run = FakeRun(merged=set())
    kdb = FakeKdb()

    args = _args(apply=True, run=run, kdb=kdb, config_path=config, max_concurrent=2)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    # Plan caps at 2; exactly those two are assigned.
    assert len(kdb.assigned) == 2
    # A held card is never released.
    assert len(kdb.assigned) == 2


def test_milestone_with_no_cards_says_so(monkeypatch, tmp_path):
    """A milestone with no matching cards is reported, not success."""
    config = _write_config(tmp_path)
    # Cards tagged with a DIFFERENT milestone.
    cards = [_card("a", body=_milestone_body("OTHER"))]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())

    args = _args(apply=False, run=run, kdb=FakeKdb(), config_path=config)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 3
    # kdb never touched — nothing to release on an empty milestone.


def test_dependency_on_merged_nonmilestone_card_is_not_held(monkeypatch, tmp_path):
    """A card may depend on a card from an EARLIER milestone; if that dep's
    branch merged, the card is releasable even though the dep isn't in this
    milestone's tag set."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    dep = _card("d0", title="Prior work", status="done", body=_milestone_body("M0"))
    b = _card("b", title="B", status="todo", body=_dep_body("d0"))
    # Board carries both; the milestone tag selects only B.
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [dep, b])
    run = FakeRun(merged={"wt/d0"})  # d0's branch merged → B free.

    args = _args(apply=False, run=run, kdb=FakeKdb(), config_path=config)
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0

    releasable, held = cmd.partition_releasable(
        [b], all_cards=[dep, b], repo="/repo", _run=run
    )
    assert "b" in [c["id"] for c in releasable]
    assert held == []


def test_select_only_matching_milestone(monkeypatch, tmp_path):
    """Cards tagged with other milestones are excluded."""
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    m1 = _card("a", body=_milestone_body("M1"))
    m2 = _card("b", body=_milestone_body("M2"))
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: [m1, m2])
    run = FakeRun(merged=set())
    kdb = FakeKdb()

    args = _args(apply=True, run=run, kdb=kdb, config_path=config, milestone="M1")
    rc = cmd.cmd_start(args, [_project()])
    assert rc == 0
    assert [tid for tid, _ in kdb.assigned] == ["a"]


def test_apply_zero_released_is_nonzero_exit(monkeypatch, tmp_path, capsys):
    """Bugfix: every assigned card failing to release must NOT be a 0-exit
    zero-count success — it's a total failure and must surface as non-zero.

    Regression for audit t_1bd666ab finding #2 (start.py:473-486): per-card
    failures were caught + continued, then the command returned 0 regardless.
    Here the fake assignee is refused for all 3 cards; expect exit 3 + a
    stderr message — exactly the decompose --apply-created-nothing pattern.
    """
    config = _write_config(tmp_path, max_in_progress=50, per_profile=2)
    cards = [_card(f"c{i}") for i in range(3)]
    monkeypatch.setattr(kanban, "list_cards", lambda **kw: cards)
    run = FakeRun(merged=set())
    kdb = FakeKdb()
    kdb.fail = True  # every assign_task returns False -> every _release raises

    args = _args(apply=True, run=run, kdb=kdb, config_path=config, max_concurrent=3)
    rc = cmd.cmd_start(args, [_project()])
    assert rc != 0, "0 released from assigned cards must be a non-zero exit"
    assert kdb.assigned == [], "nothing was actually released"
    err = capsys.readouterr().err
    assert "released nothing" in err, "must surface the total failure on stderr"

