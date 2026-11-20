"""MQ1: local merge queue — clean merge, conflict handback, red gate,
restart recovery and pause.

Every test builds a throwaway bare origin + clone under ``tmp_path`` and
injects the ``gh`` runner, so no test touches a real repository or network.
"""

import contextlib
import json
import sqlite3
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


def _repo_pair(tmp_path, *, shared=False, base="main"):
    """Bare origin + a clone with ``base`` and one PR branch pushed."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True,
                   capture_output=True, text=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", base, str(seed)], check=True,
                   capture_output=True, text=True)
    _git(seed, "config", "user.email", "test@example.invalid")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("base\n")
    (seed / "shared.txt").write_text("one\ntwo\nthree\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", base)
    _git(origin, "symbolic-ref", "HEAD", f"refs/heads/{base}")

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

    # Advance the base so the PR must actually rebase.
    _git(clone, "checkout", base)
    if shared:
        (clone / "shared.txt").write_text("one\nmain\nthree\n")
    else:
        (clone / "main-only.txt").write_text("main moved\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "main moves")
    _git(clone, "push", "origin", base)
    _git(clone, "checkout", branch)
    return {"origin": origin, "repo": clone, "branch": branch, "head": head,
            "base": base}


def _registry(tmp_path, repo, *, name="alpha", enabled=True, gates=None,
              base_branch=None):
    path = tmp_path / "registry.yaml"
    mq_cfg = {"enabled": enabled}
    if gates is not None:
        mq_cfg["gates"] = gates
    if base_branch is not None:
        mq_cfg["base_branch"] = base_branch
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


def _registry_multi(tmp_path, rows):
    """Registry with several project rows (e.g. two names on one repo)."""
    path = tmp_path / "registry-multi.yaml"
    body = {"projects": [
        {"name": name, "repo": str(repo),
         "merge_queue": {"enabled": True, "gates": ["true"]}}
        for name, repo in rows]}
    import yaml
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    return str(path)


def _new_branch(repo, name, base="main"):
    """A second PR branch off ``base``, pushed to origin; returns its head."""
    _git(repo, "checkout", base)
    _git(repo, "checkout", "-b", name)
    (repo / f"{name.replace('/', '_')}.txt").write_text(f"{name}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", name)
    _git(repo, "push", "-u", "origin", name)
    head = _git(repo, "rev-parse", "HEAD")
    return head


class FakeGh:
    """Injectable ``gh`` runner: per-PR view + merge/comment capture.

    ``views`` optionally supplies per-PR view overrides consumed in order, so a
    test can change the answer after the queue's force-push.
    ``required_checks`` is the answer to the branch-protection probe: a list
    means required checks exist, ``None`` means the endpoint 404s (no checks).
    """

    def __init__(self, heads, *, views=None, required_checks=None):
        self.heads = heads          # pr -> {headRefName, headRefOid}
        self.views = {pr: list(seq) for pr, seq in (views or {}).items()}
        self.required_checks = required_checks
        self.calls = []
        self.merged = []
        self.merge_calls = []
        self.comments = []

    def _view(self, pr, cwd=None):
        info = dict(self.heads[pr])
        info.setdefault("baseRefName", "main")
        info.setdefault("isCrossRepository", False)
        info.update({
            "number": pr,
            "state": "MERGED" if pr in self.merged else "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "url": f"https://github.com/example/repo/pull/{pr}",
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "COMPLETED",
                 "conclusion": "SUCCESS"},
            ],
            "mergeCommit": ({"oid": f"squash-{pr}"}
                            if pr in self.merged else None),
        })
        # A real `gh pr view` sees the remote branch, so mirror a successful
        # queue push from the clone's remote-tracking ref (this is what makes
        # the post-push head check meaningful in these fixtures).
        if cwd and info.get("headRefName"):
            proc = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", "--quiet",
                 f"refs/remotes/origin/{info['headRefName']}"],
                capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                info["headRefOid"] = proc.stdout.strip()
        if self.views.get(pr):
            info.update(self.views[pr].pop(0))
        return info

    def __call__(self, argv, cwd=None, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            pr = int(argv[3])
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                self._view(pr, cwd=cwd)), stderr="")
        if argv[:3] == ["gh", "pr", "merge"]:
            self.merged.append(int(argv[3]))
            self.merge_calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["gh", "pr", "comment"]:
            self.comments.append(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["gh", "api"] and "required_status_checks" in argv[2]:
            if self.required_checks is None:
                return SimpleNamespace(
                    returncode=1, stdout="",
                    stderr="gh: Not Found (HTTP 404)")
            body = {"contexts": list(self.required_checks), "checks": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body),
                                   stderr="")
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


def test_merge_passes_the_pushed_head_to_match_head_commit(tmp_path):
    """F5: the merge must be pinned to the SHA the queue just pushed."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    pushed = mq.load_state(_state(tmp_path))["projects"]["alpha"]["last"][
        "pushed_sha"]
    merge_call = gh.merge_calls[0]
    assert "--match-head-commit" in merge_call
    assert merge_call[merge_call.index("--match-head-commit") + 1] == pushed
    assert len(pushed) == 40


def test_post_push_re_read_does_not_merge_a_non_green_head(tmp_path):
    """F5: checks reset by the force-push must stop the merge, not be ignored."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh(
        {3: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={3: [{}, {
            "statusCheckRollup": [
                {"__typename": "CheckRun", "status": "IN_PROGRESS",
                 "conclusion": None}],
        }]})
    mq.add_to_queue("alpha", 3, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "waiting", result
    assert gh.merged == []
    doc = mq.load_state(_state(tmp_path))
    assert [e["pr"] for e in doc["projects"]["alpha"]["queue"]] == [3]
    # The push did happen; only the merge was withheld.
    assert _git(pair["origin"], "rev-parse",
                f"refs/heads/{pair['branch']}") != pair["head"]


def test_post_push_re_read_does_not_merge_a_changed_head(tmp_path):
    """F5: merge only the exact head the queue pushed and re-read."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh(
        {4: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={4: [{}, {"headRefOid": "c" * 40}]})
    mq.add_to_queue("alpha", 4, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "waiting", result
    assert gh.merged == []
    doc = mq.load_state(_state(tmp_path))
    assert [e["pr"] for e in doc["projects"]["alpha"]["queue"]] == [4]


@pytest.mark.parametrize("required,expected_merge", [
    (["ci"], False),
    (None, True),
])
def test_empty_rollup_after_push_follows_the_required_checks_answer(
        tmp_path, required, expected_merge):
    """F5: empty after a push is green only when the repo requires no checks."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh(
        {5: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={5: [{}, {"statusCheckRollup": []}]},
        required_checks=required)
    mq.add_to_queue("alpha", 5, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    if expected_merge:
        assert result["processed"][0]["result"] == "merged", result
        assert gh.merged == [5]
    else:
        assert result["processed"][0]["result"] == "waiting", result
        assert gh.merged == []


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


# ── remove/pause/disable during a run (F4) ─────────────────────────────────

def test_remove_during_gate_cancels_before_push_and_never_merges(
        tmp_path, monkeypatch):
    """F4: a removed PR must not be force-pushed or merged."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, card="t_removed", path=_state(tmp_path))

    def gate(command, worktree, timeout_s):
        mq.remove_from_queue("alpha", 7, path=_state(tmp_path))
        return True, ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "removed", result
    assert gh.merged == []
    # Nothing was pushed: origin still has the original PR head.
    assert _git(pair["origin"], "rev-parse",
                f"refs/heads/{pair['branch']}") == pair["head"]
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert ps["queue"] == [] and ps["in_flight"] is None


@pytest.mark.parametrize("action,expected", [
    ("pause", "paused"),
    ("disable", "disabled"),
])
def test_pause_or_disable_during_gate_stops_before_push_and_keeps_entry(
        tmp_path, monkeypatch, action, expected):
    """F4: pause/disable stop the run; the queue entry survives."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({8: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 8, card="t_hold", path=_state(tmp_path))

    def gate(command, worktree, timeout_s):
        if action == "pause":
            mq.set_paused("alpha", True, path=_state(tmp_path))
        else:
            mq.set_enabled("alpha", False, path=_state(tmp_path))
        return True, ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == expected, result
    assert gh.merged == []
    assert _git(pair["origin"], "rev-parse",
                f"refs/heads/{pair['branch']}") == pair["head"]
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert [e["pr"] for e in ps["queue"]] == [8]
    assert ps["paused"] is (action == "pause")


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
    assert not mq._pr_ready({**base, "reviewDecision": "CHANGES_REQUESTED"})


def test_empty_rollup_is_green_only_when_no_required_checks():
    """F5: an empty rollup is not green merely because nothing was reported."""
    empty = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
             "statusCheckRollup": []}
    assert mq._pr_ready(empty, required_checks=False)
    assert not mq._pr_ready(empty, required_checks=True)
    # Unknown (could not ask GitHub) is not positive evidence of "no checks".
    assert not mq._pr_ready(empty, required_checks=None)
    assert mq._checks_green([], required_checks=False)
    assert not mq._checks_green([], required_checks=True)


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


# ── the production runner (no injected runner) ─────────────────────────────

def test_default_runner_drives_real_git_in_a_temp_repo(tmp_path):
    """F1: the real default runner must work, not only injected runners."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    proc = mq._git(["rev-parse", "--is-inside-work-tree"], str(repo),
                   mq._default_runner)

    assert proc is not None
    assert proc.returncode == 0, proc.stderr
    assert isinstance(proc.stdout, str)
    assert proc.stdout.strip() == "true"


# ── auto-enqueue repo identity ─────────────────────────────────────────────

def _board_with_cards(tmp_path, cards):
    """A minimal kanban DB behind the read-only board seam.

    A card is ``(id, body)`` or ``(id, body, status)``; the default status is
    ``ready``.
    """
    path = tmp_path / "kanban.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, body TEXT, "
        "status TEXT)")
    conn.execute(
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT, body TEXT)")
    for card in cards:
        card_id, body = card[0], card[1]
        status = card[2] if len(card) > 2 else "ready"
        conn.execute(
            "INSERT INTO tasks VALUES (?, 'worker', ?, ?)",
            (card_id, body, status))
    conn.commit()
    conn.close()

    class FakeKanban:
        def connect_closing(self, board=None):
            db = sqlite3.connect(path)
            db.row_factory = sqlite3.Row
            return contextlib.closing(db)

    return FakeKanban()


def _board_seam(monkeypatch, kb):
    from hscc_daemon import autodown
    monkeypatch.setattr(autodown, "_load_kanban_db_or_default", lambda: kb)
    monkeypatch.setattr(autodown, "_enum_board_names",
                        lambda *a, **k: (["alpha"], False))


def test_auto_enqueue_ignores_a_pr_url_from_another_repo(tmp_path,
                                                        monkeypatch):
    """F2: a card linking another repo's #42 must not enqueue this repo's #42."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin",
         "https://github.com/pom11/alpha.git")
    _board_seam(monkeypatch, _board_with_cards(
        tmp_path, [("t_foreign", "see https://github.com/other/repo/pull/42")]))
    gh = FakeGh({42: {"headRefName": "wt/t_foreign",
                      "headRefOid": "a" * 40}})
    cfg = {"name": "alpha", "repo": str(repo), "board": "alpha"}

    assert mq._discover_ready_cards(cfg, gh_runner=gh) == []
    assert gh.calls == []


def test_auto_enqueue_accepts_a_pr_url_from_the_projects_origin(tmp_path,
                                                               monkeypatch):
    """The same URL on the project's own origin (ssh, mixed case) is accepted."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", "git@github.com:Pom11/Alpha.git")
    _board_seam(monkeypatch, _board_with_cards(
        tmp_path, [("t_own", "https://github.com/pom11/alpha/pull/42")]))
    gh = FakeGh({42: {"headRefName": "wt/t_own", "headRefOid": "b" * 40}})
    cfg = {"name": "alpha", "repo": str(repo), "board": "alpha"}

    ready = mq._discover_ready_cards(cfg, gh_runner=gh)

    assert ready == [{"pr": 42, "card": "t_own", "assignee": "worker",
                      "branch": "wt/t_own"}]


def test_auto_enqueue_only_accepts_running_ready_or_review_cards(
        tmp_path, monkeypatch):
    """F10: blocked/scheduled/todo cards are not mergeable work."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin",
         "https://github.com/pom11/alpha.git")
    cards = []
    heads = {}
    for pr, (card, status) in enumerate([
            ("t_blocked", "blocked"), ("t_scheduled", "scheduled"),
            ("t_todo", "todo"), ("t_running", "running"),
            ("t_ready", "ready"), ("t_review", "review")], start=61):
        cards.append(
            (card, f"https://github.com/pom11/alpha/pull/{pr}", status))
        heads[pr] = {"headRefName": f"wt/{card}", "headRefOid": "d" * 40}
    _board_seam(monkeypatch, _board_with_cards(tmp_path, cards))
    gh = FakeGh(heads)
    cfg = {"name": "alpha", "repo": str(repo), "board": "alpha"}

    ready = mq._discover_ready_cards(cfg, gh_runner=gh)

    assert {row["card"] for row in ready} == {
        "t_running", "t_ready", "t_review"}


def test_auto_enqueue_respects_changes_requested_review(tmp_path, monkeypatch):
    """F10: a PR whose review was rejected is not auto-enqueued."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin",
         "https://github.com/pom11/alpha.git")
    _board_seam(monkeypatch, _board_with_cards(tmp_path, [
        ("t_changes", "https://github.com/pom11/alpha/pull/71", "review"),
        ("t_approved", "https://github.com/pom11/alpha/pull/72", "review"),
    ]))
    gh = FakeGh({
        71: {"headRefName": "wt/t_changes", "headRefOid": "e" * 40,
             "reviewDecision": "CHANGES_REQUESTED"},
        72: {"headRefName": "wt/t_approved", "headRefOid": "f" * 40,
             "reviewDecision": "APPROVED"},
    })
    cfg = {"name": "alpha", "repo": str(repo), "board": "alpha"}

    ready = mq._discover_ready_cards(cfg, gh_runner=gh)

    assert [row["pr"] for row in ready] == [72]
    assert "reviewDecision" in mq._PR_JSON_FIELDS


# ── fresh base before every entry (F3) ─────────────────────────────────────

def test_every_entry_fetches_current_base_before_rebasing(tmp_path):
    """A PR merged between two entries must be in the second entry's base."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    assert mq.tick(state_path=_state(tmp_path),
                   root=str(tmp_path / "queue"), registry_path=registry,
                   gh_runner=gh, git_runner=subprocess.run
                   )["processed"][0]["result"] == "merged"

    # Someone lands another PR on the base while the queue worktree exists,
    # from a SECOND clone: a push from this clone would refresh its own
    # remote-tracking ref and hide the staleness.
    repo = pair["repo"]
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(pair["origin"]), str(other)],
                   check=True, capture_output=True, text=True)
    _git(other, "config", "user.email", "test@example.invalid")
    _git(other, "config", "user.name", "Test")
    (other / "landed-between-entries.txt").write_text("new base\n")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "landed between entries")
    _git(other, "push", "origin", "main")
    new_main = _git(other, "rev-parse", "HEAD")

    # The existing queue worktree must fetch on every entry, not only when it
    # is first created.
    worktree = tmp_path / "queue" / "alpha"
    assert _git(worktree, "rev-parse", "origin/main") != new_main
    mq.ensure_worktree({"name": "alpha", "repo": str(repo)},
                       root=str(tmp_path / "queue"),
                       git_runner=subprocess.run)
    assert _git(worktree, "rev-parse", "origin/main") == new_main

    _git(repo, "checkout", "main")
    _git(repo, "merge", "--ff-only", "origin/main")
    second_head = _new_branch(repo, "wt/t_second")
    gh2 = FakeGh({8: {"headRefName": "wt/t_second",
                      "headRefOid": second_head}})
    mq.add_to_queue("alpha", 8, path=_state(tmp_path))
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh2, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    # The pushed branch is based on the CURRENT origin/main, not the commit
    # the queue worktree was created from.
    assert _git(pair["origin"], "merge-base", "--is-ancestor", new_main,
                "refs/heads/wt/t_second") == ""
    assert _git(worktree, "rev-parse", "origin/main") == new_main


# ── default branch detection, not hard-coded origin/main (F8) ──────────────

def test_master_default_branch_is_detected_and_used(tmp_path):
    pair = _repo_pair(tmp_path, base="master")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"],
                     "baseRefName": "master"}})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [7]
    assert _git(pair["origin"], "rev-list", "--count",
                f"master..refs/heads/{pair['branch']}") == "1"


# ── baseRefName is enforced, or configured explicitly (F6) ─────────────────

def test_non_default_base_pr_is_refused_once_and_not_re_enqueued(
        tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"],
                     "baseRefName": "release"}})
    mq.add_to_queue("alpha", 9, card="t_wrong_base", path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "refused", result
    assert gh.merged == []
    assert gh.comments and "release" in " ".join(gh.comments[0])
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert ps["queue"] == [] and ps["in_flight"] is None
    assert ps["last"]["result"] == "refused"
    assert "release" in ps["last"]["error"]

    # Auto-enqueue skips it too, so the tick cannot re-add it every pass.
    repo = tmp_path / "repo-foreign-origin"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin",
         "https://github.com/pom11/alpha.git")
    _board_seam(monkeypatch, _board_with_cards(
        tmp_path, [("t_wrong_base",
                    "https://github.com/pom11/alpha/pull/9")]))
    cfg = {"name": "alpha", "repo": str(repo), "board": "alpha",
           "base_branch": "main"}
    assert mq._discover_ready_cards(cfg, gh_runner=gh) == []


def test_configured_base_branch_is_rebased_onto(tmp_path):
    pair = _repo_pair(tmp_path)
    repo = pair["repo"]
    _git(repo, "checkout", "main")
    release_tip = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-b", "release")
    _git(repo, "push", "-u", "origin", "release")

    _git(repo, "checkout", "main")
    (repo / "main-after-release.txt").write_text("main moved again\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "main after release")
    _git(repo, "push", "origin", "main")
    main_tip = _git(repo, "rev-parse", "main")

    _git(repo, "checkout", "-b", "wt/t_release", "release")
    (repo / "release-topic.txt").write_text("release topic\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "release topic")
    _git(repo, "push", "-u", "origin", "wt/t_release")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "fetch", "origin")

    registry = _registry(tmp_path, repo, gates=["true"],
                         base_branch="release")
    gh = FakeGh({5: {"headRefName": "wt/t_release", "headRefOid": head,
                     "baseRefName": "release"}})
    mq.add_to_queue("alpha", 5, path=_state(tmp_path))
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    branch_ref = "refs/heads/wt/t_release"
    assert _git(pair["origin"], "merge-base", "--is-ancestor", release_tip,
                branch_ref) == ""
    # Before the fix the PR was rebased onto main, making main its base.
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", main_tip, branch_ref],
        cwd=str(pair["origin"]), capture_output=True, text=True)
    assert proc.returncode != 0, "PR was rebased onto main, not release"


# ── fork PRs are refused, not retried (F15) ────────────────────────────────

def test_fork_pr_is_refused_with_clear_reason_and_not_retried(
        tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({12: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"],
                      "isCrossRepository": True}})
    mq.add_to_queue("alpha", 12, card="t_fork", path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "refused", result
    assert gh.merged == []
    assert gh.comments and "fork" in " ".join(gh.comments[0])
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert ps["queue"] == [] and ps["in_flight"] is None
    assert ps["last"]["result"] == "refused"

    # Discovery skips it too: no retry loop through auto-enqueue.
    repo = tmp_path / "repo-fork"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "remote", "add", "origin",
         "https://github.com/pom11/alpha.git")
    _board_seam(monkeypatch, _board_with_cards(
        tmp_path, [("t_fork", "https://github.com/pom11/alpha/pull/12")]))
    cfg = {"name": "alpha", "repo": str(repo), "board": "alpha"}
    assert mq._discover_ready_cards(cfg, gh_runner=gh) == []


# ── projects sharing one repo path share one queue/lock (F3) ───────────────

def test_projects_sharing_a_repo_process_one_pr_per_repo_per_tick(tmp_path):
    pair = _repo_pair(tmp_path)
    second_head = _new_branch(pair["repo"], "wt/t_second")
    registry = _registry_multi(
        tmp_path, [("alpha", pair["repo"]), ("beta", pair["repo"])])
    gh = FakeGh({
        7: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
        8: {"headRefName": "wt/t_second", "headRefOid": second_head},
    })
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    mq.add_to_queue("beta", 8, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    # ONE FIFO per repo: the beta entry waits even though it is another project.
    assert [p["project"] for p in result["processed"]] == ["alpha"], result
    assert gh.merged == [7]
    doc = mq.load_state(_state(tmp_path))
    assert doc["projects"]["alpha"]["queue"] == []
    assert [e["pr"] for e in doc["projects"]["beta"]["queue"]] == [8]

    # Same repo path -> same lock file; a different repo -> a different one.
    lock_a = mq.repo_lock_path(str(pair["repo"]), root=str(tmp_path))
    lock_b = mq.repo_lock_path(str(pair["repo"] / "."), root=str(tmp_path))
    other = mq.repo_lock_path(str(tmp_path / "other"), root=str(tmp_path))
    assert lock_a == lock_b
    assert lock_a != other

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert [p["pr"] for p in result["processed"]] == [8], result
    assert gh.merged == [7, 8]


# ── sandbox isolation (HSCC_HOME / HSCC_DIR) ───────────────────────────────

def test_state_paths_honour_hscc_home_and_hscc_dir(tmp_path, monkeypatch):
    """F13: the ledger/worktrees follow the sandbox root, like alerts.py."""
    monkeypatch.delenv("HSCC_HOME", raising=False)
    monkeypatch.delenv("HSCC_DIR", raising=False)
    home = tmp_path / "sandbox-home"
    monkeypatch.setenv("HSCC_HOME", str(home))
    assert mq.state_file() == str(home / "merge-queue-state.json")
    assert mq.queue_root() == str(home / "merge-queue")

    hscc_dir = tmp_path / "sandbox-dir"
    monkeypatch.delenv("HSCC_HOME")
    monkeypatch.setenv("HSCC_DIR", str(hscc_dir))
    assert mq.state_file() == str(hscc_dir / "merge-queue-state.json")
    assert mq.queue_root() == str(hscc_dir / "merge-queue")

    # HSCC_HOME (the alerts.py variable) wins when both are set.
    monkeypatch.setenv("HSCC_HOME", str(home))
    assert mq.state_file() == str(home / "merge-queue-state.json")


def test_add_to_queue_writes_only_under_hscc_home(tmp_path, monkeypatch):
    """A sandboxed write must land in $HSCC_HOME, never $HOME/.hscc."""
    home = tmp_path / "home"
    sandbox = tmp_path / "sandbox"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HSCC_HOME", str(sandbox))
    monkeypatch.delenv("HSCC_DIR", raising=False)

    mq.add_to_queue("alpha", 7, card="t_sandbox")

    assert (sandbox / "merge-queue-state.json").exists()
    assert not (home / ".hscc" / "merge-queue-state.json").exists()
    assert mq.load_state()["projects"]["alpha"]["queue"][0]["pr"] == 7
