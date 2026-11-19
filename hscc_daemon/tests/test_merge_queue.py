"""MQ1: local merge queue — clean merge, conflict handback, red gate,
restart recovery and pause.

Every test builds a throwaway bare origin + clone under ``tmp_path`` and
injects the ``gh`` runner, so no test touches a real repository or network.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from hscc_daemon import merge_queue as mq


# ── helpers ────────────────────────────────────────────────────────────────

def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _repo_pair(tmp_path, *, shared=False):
    """Bare origin + a clone with ``main`` and one PR branch pushed."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   capture_output=True, text=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True, text=True)
    _git(seed, "config", "user.email", "test@example.invalid")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("base\n")
    (seed / "shared.txt").write_text("one\ntwo\nthree\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")

    clone = tmp_path / "project"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True,
                   capture_output=True, text=True)
    _git(clone, "config", "user.email", "test@example.invalid")
    _git(clone, "config", "user.name", "Test")

    branch = "wt/t_card"
    _git(clone, "checkout", "-b", branch)
    if shared:
        (clone / "shared.txt").write_text("one\nbranch\nthree\n")
    else:
        (clone / "topic.txt").write_text("branch change\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "branch change")
    _git(clone, "push", "-u", "origin", branch)
    head = _git(clone, "rev-parse", "HEAD")

    # Advance main so the PR must actually rebase.
    _git(clone, "checkout", "main")
    if shared:
        (clone / "shared.txt").write_text("one\nmain\nthree\n")
    else:
        (clone / "main-only.txt").write_text("main moved\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "main moves")
    _git(clone, "push", "origin", "main")
    _git(clone, "checkout", branch)
    return {"origin": origin, "repo": clone, "branch": branch, "head": head}


def _registry(tmp_path, repo, *, name="alpha", enabled=True, gates=None):
    path = tmp_path / "registry.yaml"
    mq_cfg = {"enabled": enabled}
    if gates is not None:
        mq_cfg["gates"] = gates
    body = {
        "projects": [{
            "name": name,
            "repo": str(repo),
            "merge_queue": mq_cfg,
        }]
    }
    try:
        import yaml
        path.write_text(yaml.safe_dump(body, sort_keys=False))
    except ImportError:  # pragma: no cover - the daemon's deps carry yaml
        path.write_text(
            "projects:\n"
            f"- name: {name}\n"
            f"  repo: {repo}\n"
            "  merge_queue:\n"
            f"    enabled: {'true' if enabled else 'false'}\n")
    return str(path)


class FakeGh:
    """Injectable ``gh`` runner: per-PR view + merge/comment capture."""

    def __init__(self, heads):
        self.heads = heads          # pr -> {headRefName, headRefOid}
        self.calls = []
        self.merged = []
        self.comments = []

    def _view(self, pr):
        info = dict(self.heads[pr])
        info.update({
            "number": pr,
            "state": "MERGED" if pr in self.merged else "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "baseRefName": "main",
            "url": f"https://github.com/example/repo/pull/{pr}",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},
            ],
            "mergeCommit": ({"oid": f"squash-{pr}"}
                            if pr in self.merged else None),
        })
        return info

    def __call__(self, argv, cwd=None, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            pr = int(argv[3])
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                self._view(pr)), stderr="")
        if argv[:3] == ["gh", "pr", "merge"]:
            self.merged.append(int(argv[3]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["gh", "pr", "comment"]:
            self.comments.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected gh call: {argv}")


def _state(tmp_path):
    return str(tmp_path / "merge-queue-state.json")


def _no_runner(argv, **kwargs):
    raise AssertionError(f"runner should not have been called: {argv}")


# ── clean merge ────────────────────────────────────────────────────────────

def test_clean_rebase_gates_push_and_squash_merge(tmp_path):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, card="t_card", assignee="worker",
                    path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [7]
    # The PR branch on origin now carries the rebased commit on top of main.
    origin = pair["origin"]
    branch_files = _git(origin, "show", f"refs/heads/{pair['branch']}:topic.txt")
    assert branch_files == "branch change"
    assert _git(origin, "rev-list", "--count",
                f"main..refs/heads/{pair['branch']}") == "1"

    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert ps["queue"] == [] and ps["in_flight"] is None
    assert ps["last"]["result"] == "merged"
    assert ps["last"]["sha"] == "squash-7"
    # The worktree is the queue's own, under ~/.hscc-style root.
    assert (tmp_path / "queue" / "alpha" / ".git").exists()


def test_one_pr_per_project_per_tick_is_fifo(tmp_path):
    pair = _repo_pair(tmp_path)
    # Second branch off main.
    _git(pair["repo"], "checkout", "main")
    _git(pair["repo"], "checkout", "-b", "wt/t_second")
    (pair["repo"] / "second.txt").write_text("second\n")
    _git(pair["repo"], "add", ".")
    _git(pair["repo"], "commit", "-m", "second")
    _git(pair["repo"], "push", "-u", "origin", "wt/t_second")
    second_head = _git(pair["repo"], "rev-parse", "HEAD")

    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({
        7: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
        8: {"headRefName": "wt/t_second", "headRefOid": second_head},
    })
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    mq.add_to_queue("alpha", 8, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run)

    assert [p["pr"] for p in result["processed"]] == [7]
    assert gh.merged == [7]
    doc = mq.load_state(_state(tmp_path))
    assert [e["pr"] for e in doc["projects"]["alpha"]["queue"]] == [8]


# ── conflict -> resolve handback ───────────────────────────────────────────

def test_conflict_comments_pr_and_hands_card_back(tmp_path):
    pair = _repo_pair(tmp_path, shared=True)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    handed = []
    mq.add_to_queue("alpha", 9, card="t_conflict", assignee="worker",
                    path=_state(tmp_path))

    result = mq.tick(
        state_path=_state(tmp_path), root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        handback=lambda card, assignee, msg: handed.append(
            (card, assignee, msg)) or True)

    assert result["processed"][0]["result"] == "conflict", result
    assert "shared.txt" in result["processed"][0]["conflicted"]
    assert handed and handed[0][0] == "t_conflict"
    assert "shared.txt" in handed[0][2]
    # MQ3: the handback is a resolve step for the card's OWN branch.
    assert "git rebase origin/main" in handed[0][2]
    assert f"git push --force-with-lease origin {pair['branch']}" in handed[0][2]
    assert "Never force-push main or any other branch" in handed[0][2]
    assert gh.comments and "conflicted" in " ".join(gh.comments[0])
    doc = mq.load_state(_state(tmp_path))
    assert doc["projects"]["alpha"]["queue"] == []
    assert doc["projects"]["alpha"]["last"]["result"] == "conflict"
    # aborted cleanly: no rebase left in progress in the queue worktree
    wt = tmp_path / "queue" / "alpha"
    assert not (wt / ".git" / "rebase-merge").exists()
    assert not (wt / ".git" / "rebase-apply").exists()


def test_resolve_instructions_name_the_card_branch():
    """The push target is the branch recorded on the PR, else the card's own."""
    named = mq.resolve_instructions("feature/recorded")
    assert "git rebase origin/main" in named
    assert "git push --force-with-lease origin feature/recorded" in named
    fallback = mq.resolve_instructions(None)
    assert "the branch recorded on your card" in fallback


# ── red gate -> resolve handback ───────────────────────────────────────────

def test_red_gate_comments_tail_and_hands_card_back(tmp_path):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["sh -c 'echo BOOM >&2; exit 7'"])
    gh = FakeGh({11: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    handed = []
    mq.add_to_queue("alpha", 11, card="t_red", assignee="worker",
                    path=_state(tmp_path))

    result = mq.tick(
        state_path=_state(tmp_path), root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        handback=lambda card, assignee, msg: handed.append(
            (card, assignee, msg)) or True)

    assert result["processed"][0]["result"] == "red-gate", result
    assert "exit 7" in result["processed"][0]["gate"]
    assert handed and "BOOM" in handed[0][2]
    assert "BOOM" in " ".join(gh.comments[0])
    # The red-gate handback carries the same own-branch resolve step.
    assert "git rebase origin/main" in handed[0][2]
    assert f"git push --force-with-lease origin {pair['branch']}" in handed[0][2]
    # Nothing was pushed or merged.
    assert gh.merged == []
    assert _git(pair["origin"], "rev-parse",
                f"refs/heads/{pair['branch']}") == pair["head"]


# ── restart mid-run ────────────────────────────────────────────────────────

def test_restart_mid_run_recovers_in_flight_to_the_queue_head(tmp_path):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    mq.add_to_queue("alpha", 13, card="t_restart", path=_state(tmp_path))

    # Simulate the daemon dying after claiming the entry: it is persisted as
    # in_flight, the queue is empty, and the process is gone.
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    ps["in_flight"] = ps["queue"].pop(0)
    mq.save_state(doc, _state(tmp_path))

    gh = FakeGh({13: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run)

    assert result["recovered"] == ["alpha"]
    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [13]
    doc = mq.load_state(_state(tmp_path))
    assert doc["projects"]["alpha"]["in_flight"] is None
    assert doc["projects"]["alpha"]["queue"] == []


# ── pause ──────────────────────────────────────────────────────────────────

def test_pause_leaves_the_queue_untouched(tmp_path):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    mq.add_to_queue("alpha", 17, path=_state(tmp_path))
    mq.set_paused("alpha", True, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry,
                     gh_runner=_no_runner, git_runner=_no_runner)

    assert result["processed"] == []
    assert result["projects"]["alpha"]["paused"] is True
    doc = mq.load_state(_state(tmp_path))
    assert [e["pr"] for e in doc["projects"]["alpha"]["queue"]] == [17]
    assert not (tmp_path / "queue" / "alpha").exists()


# ── readiness gate ─────────────────────────────────────────────────────────

def test_auto_enqueue_picks_a_ready_card_when_the_queue_is_empty(tmp_path):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({21: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})

    def source(cfg, gh_runner=None):
        return [{"pr": 21, "card": "t_auto", "assignee": "worker"}]

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run, auto_source=source)

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [21]
    doc = mq.load_state(_state(tmp_path))
    assert doc["projects"]["alpha"]["last"]["card"] == "t_auto"


def test_pr_ready_requires_open_non_draft_mergeable_green():
    base = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
            "statusCheckRollup": []}
    assert mq._pr_ready(base)
    assert not mq._pr_ready({**base, "isDraft": True})
    assert not mq._pr_ready({**base, "mergeable": "CONFLICTING"})
    assert not mq._pr_ready({**base, "statusCheckRollup": [
        {"__typename": "CheckRun", "status": "IN_PROGRESS",
         "conclusion": None}]})
    assert not mq._pr_ready({**base, "statusCheckRollup": [
        {"state": "FAILURE"}]})


def test_default_hscc_gates_add_build_check_for_app_changes(tmp_path):
    cfg = {"name": "hscc"}
    calls = []

    def fake_git(argv, cwd=None, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ios-app/Foo.swift\n",
                               stderr="")

    gates = mq._resolve_gates(cfg, str(tmp_path), fake_git)
    assert gates[0] == "python3 scripts/address_guard.py --tracked"
    assert "bash scripts/run_tests.sh" in gates
    assert "bash ios-app/scripts/build_check.sh" in gates
    assert "bash macos-app/scripts/build_check.sh" not in gates


# ── read-only view (kanban monitor + GET /v1/merge-queue) ──────────────────

def test_view_rows_carry_position_state_branch_age_and_last_result(tmp_path):
    state = _state(tmp_path)
    mq.add_to_queue("alpha", 7, card="t_a", branch="wt/t_a", path=state)
    mq.add_to_queue("alpha", 8, card="t_b", branch="wt/t_b", path=state)
    doc = mq.load_state(state)
    ps = mq._project_state(doc, "alpha")
    flight = ps["queue"].pop()          # PR 8 is now the claim, not queued
    flight.update({"stage": "gating",
                   "enqueued_at": "2026-11-19T00:00:00+00:00"})
    ps["in_flight"] = flight
    ps["last"] = {"pr": 9, "card": "t_c", "branch": "wt/t_c",
                  "result": "conflict", "at": "2026-11-19T01:00:00+00:00",
                  "conflicted": ["shared.txt"]}
    mq.save_state(doc, state)

    data = mq.view(state_path=state, registry_path=str(tmp_path / "none.yaml"),
                   now=1_800_000_000)
    rows = data["rows"]
    assert [(r["position"], r["pr"], r["state"]) for r in rows] == [
        (1, 7, "queued"), (None, 8, "gating"), (None, 9, "resolve")]
    assert [r["branch"] for r in rows] == ["wt/t_a", "wt/t_b", "wt/t_c"]
    assert rows[0]["age_s"] is not None
    assert rows[1]["age_s"] is not None
    assert rows[2]["last_result"] == "conflict"
    assert rows[2]["detail"] == "shared.txt"
    assert data["queued"] == 1 and data["count"] == 3
    # One map for the review queue's per-card column.
    assert mq.card_states(state_path=state, now=1_800_000_000)["t_b"]["state"] \
        == "gating"


def test_view_shows_the_live_gating_stage_while_the_gate_runs(tmp_path,
                                                               monkeypatch):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["false"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, card="t_card", assignee="worker",
                    path=_state(tmp_path))
    seen = {}

    def gate(command, worktree, timeout_s):
        seen.update(mq.view(state_path=_state(tmp_path),
                            registry_path=registry))
        return False, "gate exploded"

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "red-gate"
    assert [r["state"] for r in seen["rows"]] == ["gating"]
    after = mq.view(state_path=_state(tmp_path), registry_path=registry)
    assert [r["state"] for r in after["rows"]] == ["resolve"]
    assert after["rows"][0]["detail"] == "false"


def test_view_board_filter_keeps_only_that_projects_queue(tmp_path):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], name="alpha")
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    mq.add_to_queue("beta", 8, path=_state(tmp_path))

    assert [r["project"] for r in mq.view(
        state_path=_state(tmp_path), registry_path=registry)["rows"]] == [
            "alpha", "beta"]
    filtered = mq.view(state_path=_state(tmp_path), registry_path=registry,
                       board="not-alpha")
    assert filtered["rows"] == []
