"""t_3c99c052: the local gate overlaps GitHub CI on the pushed head.

The sequential order gated first and pushed afterwards, so a queued PR paid the
local gate (~25 min) and then GitHub CI (~10-15 min) one after the other.  With
overlap on, the rebased head is pushed FIRST and the local gate runs while CI
runs on that exact SHA; ``_settle_pushed_head`` is still the only merge
authority and refuses anything that is not green for one SHA.
"""

import subprocess

import yaml

from hscc_daemon import merge_queue as mq
from hscc_daemon.tests.test_merge_queue import (
    FakeGh, _git, _repo_pair, _state)


PR = 7
CARD = "t_overlap"
PENDING = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
            "conclusion": None}]
RED = [{"__typename": "CheckRun", "status": "COMPLETED",
        "conclusion": "FAILURE"}]


def _registry(tmp_path, repo, *, name="hscc", gates=("true",),
              overlap_ci=None):
    cfg = {"enabled": True, "gates": list(gates)}
    if overlap_ci is not None:
        cfg["overlap_ci"] = overlap_ci
    path = tmp_path / f"registry-{name}.yaml"
    path.write_text(yaml.safe_dump(
        {"projects": [{"name": name, "repo": str(repo),
                       "merge_queue": cfg}]}, sort_keys=False))
    return str(path)


def _tick(tmp_path, registry, gh, *, log=None, handback=None):
    kwargs = {"handback": handback} if handback is not None else {}
    return mq.tick(state_path=_state(tmp_path),
                   root=str(tmp_path / "queue"), registry_path=registry,
                   gh_runner=gh, git_runner=subprocess.run,
                   auto_source=lambda *a, **k: [], log=log, **kwargs)


def _logger():
    lines = []

    def log(msg, *args, **kwargs):
        level = args[0] if args else kwargs.get("level", "INFO")
        lines.append(f"[{level}] {msg}")

    return lines, log


def test_push_happens_before_the_first_gate_command(tmp_path, monkeypatch):
    """hscc defaults to overlap: the gate must already see the pushed head."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="hscc")
    gh = FakeGh({PR: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("hscc", PR, card=CARD, path=_state(tmp_path))
    seen = {}

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        seen["origin"] = _git(pair["origin"], "rev-parse",
                              f"refs/heads/{pair['branch']}")
        seen["gate_head"] = _git(worktree, "rev-parse", "HEAD")
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    lines, log = _logger()
    result = _tick(tmp_path, registry, gh, log=log)

    assert result["processed"][0]["result"] == "merged", result
    # The gate ran on the rebased commit, and origin already carried it: the
    # push is not waiting behind the gate any more.
    assert seen["gate_head"] != pair["head"]
    assert seen["origin"] == seen["gate_head"]
    assert any("gate start (overlapping CI on " in line for line in lines), lines
    merge_call = gh.merge_calls[0]
    assert merge_call[merge_call.index("--match-head-commit") + 1] == \
        seen["gate_head"]


def test_merge_is_refused_until_ci_is_green_on_the_same_pushed_sha(
        tmp_path, monkeypatch):
    """A green local gate does not merge a pushed head whose CI is pending."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="hscc")
    gh = FakeGh(
        {PR: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={PR: [{}, {"statusCheckRollup": PENDING}]})
    mq.add_to_queue("hscc", PR, card=CARD, path=_state(tmp_path))
    gate_heads = []

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        gate_heads.append(_git(worktree, "rev-parse", "HEAD"))
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    first = _tick(tmp_path, registry, gh)

    assert first["processed"][0]["result"] == "ci-pending", first
    assert gh.merged == []
    ps = mq.load_state(_state(tmp_path))["projects"]["hscc"]
    assert ps["in_flight"]["stage"] == "ci-pending"
    pushed = ps["in_flight"]["pushed_sha"]
    assert pushed == gate_heads[0]

    # The next tick only re-reads CI on that SHA; it never re-gates.
    second = _tick(tmp_path, registry, gh)

    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [PR]
    merge_call = gh.merge_calls[0]
    assert merge_call[merge_call.index("--match-head-commit") + 1] == pushed
    assert gate_heads == [pushed]


def test_local_red_skips_the_ci_wait_and_hands_the_card_back(
        tmp_path, monkeypatch):
    """A red local gate answers first; CI is never consulted afterwards."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="hscc")
    gh = FakeGh(
        {PR: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={PR: [{}, {"statusCheckRollup": RED}]})
    mq.add_to_queue("hscc", PR, card=CARD, assignee="worker",
                    path=_state(tmp_path))
    handed = []

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        return "fail", "FAILED tests/test_x.py::test_y - boom"

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = _tick(tmp_path, registry, gh,
                   handback=lambda card, who, body: handed.append(
                       (card, who, body)) or True)

    assert result["processed"][0]["result"] == "red-gate", result
    assert gh.merged == []
    assert handed and handed[0][0] == CARD and "boom" in handed[0][2]
    ps = mq.load_state(_state(tmp_path))["projects"]["hscc"]
    assert ps["in_flight"] is None and ps["waiting"] == []
    assert ps["last"]["result"] == "red-gate"
    # No post-gate CI read: the local leg already answered.
    views = [c for c in gh.calls if c[:3] == ["gh", "pr", "view"]]
    assert len(views) == 1, gh.calls


def test_ci_red_cancels_the_gate_through_the_cancel_check_path(
        tmp_path, monkeypatch):
    """CI red beside a running leg cancels it; the CI verdict is the outcome."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="hscc")
    gh = FakeGh(
        {PR: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={PR: [{},
                    {"statusCheckRollup": PENDING},
                    {"statusCheckRollup": RED}]})
    mq.add_to_queue("hscc", PR, card=CARD, assignee="worker",
                    path=_state(tmp_path))
    monkeypatch.setattr(mq, "OVERLAP_CI_POLL_S", 0)
    polls = []

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        first = cancel_check()
        polls.append(first)
        assert first is None, ("the pending read must not cancel the leg",
                               first)
        second = cancel_check()
        polls.append(second)
        if second == mq._OverlapCiWatch.RED:
            return "cancelled", "gate cancelled (ci-red) before it finished"
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    handed = []
    result = _tick(tmp_path, registry, gh,
                   handback=lambda card, who, body: handed.append(
                       (card, who, body)) or True)

    assert polls[0] is None
    assert polls[1] == mq._OverlapCiWatch.RED
    assert result["processed"][0]["result"] == "red-ci", result
    assert gh.merged == []
    assert handed and "CI is red" in handed[0][2]
    assert gh.comments and "CI is red" in gh.comments[0][-1]
    ps = mq.load_state(_state(tmp_path))["projects"]["hscc"]
    assert ps["in_flight"] is None and ps["waiting"] == []
    assert ps["last"]["result"] == "red-ci"


def test_overlap_ci_false_keeps_the_sequential_order(tmp_path, monkeypatch):
    """An explicit false (even on hscc) pushes only after the gate is green."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="hscc",
                         overlap_ci=False)
    gh = FakeGh({PR: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("hscc", PR, card=CARD, path=_state(tmp_path))
    seen = {}

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        seen["origin"] = _git(pair["origin"], "rev-parse",
                              f"refs/heads/{pair['branch']}")
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    lines, log = _logger()
    result = _tick(tmp_path, registry, gh, log=log)

    assert result["processed"][0]["result"] == "merged", result
    # The old order: origin still had the PRE-rebase head while the gate ran.
    assert seen["origin"] == pair["head"]
    assert not any("overlapping CI" in line for line in lines), lines


def test_head_moved_during_the_gate_releases_to_waiting(tmp_path, monkeypatch):
    """A foreign push over the overlapped head is not ours to settle."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="hscc")
    foreign = "f" * 40
    gh = FakeGh(
        {PR: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={PR: [{}, {"headRefOid": foreign}]})
    mq.add_to_queue("hscc", PR, card=CARD, path=_state(tmp_path))

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = _tick(tmp_path, registry, gh)

    assert result["processed"][0]["result"] == "waiting", result
    assert gh.merged == []
    ps = mq.load_state(_state(tmp_path))["projects"]["hscc"]
    assert [e["pr"] for e in ps["waiting"]] == [PR]
    assert ps["in_flight"] is None
