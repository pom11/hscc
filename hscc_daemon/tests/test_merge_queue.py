"""MQ1: local merge queue — clean merge, conflict handback, red gate,
restart recovery and pause.

Every test builds a throwaway bare origin + clone under ``tmp_path`` and
injects the ``gh`` runner, so no test touches a real repository or network.
"""

import contextlib
import importlib.util
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import threading
import time
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
              base_branch=None, timeout_s=None, waiting_max_age_s=None,
              ci_pending_timeout_s=None):
    path = tmp_path / "registry.yaml"
    mq_cfg = {}
    if enabled is not None:
        mq_cfg["enabled"] = enabled
    if gates is not None:
        mq_cfg["gates"] = gates
    if base_branch is not None:
        mq_cfg["base_branch"] = base_branch
    if timeout_s is not None:
        mq_cfg["timeout_s"] = timeout_s
    if waiting_max_age_s is not None:
        mq_cfg["waiting_max_age_s"] = waiting_max_age_s
    if ci_pending_timeout_s is not None:
        mq_cfg["ci_pending_timeout_s"] = ci_pending_timeout_s
    body = {
        "projects": [{
            "name": name,
            "repo": str(repo),
            **({"merge_queue": mq_cfg} if mq_cfg else {}),
        }]
    }
    try:
        import yaml
        path.write_text(yaml.safe_dump(body, sort_keys=False))
    except ImportError:  # pragma: no cover - the daemon's deps carry yaml
        lines = ["projects:", f"- name: {name}", f"  repo: {repo}",
                 "  merge_queue:"]
        if enabled is not None:
            lines.append(f"    enabled: {'true' if enabled else 'false'}")
        if gates is not None:
            lines.append("    gates: [" + ", ".join(
                f'"{g}"' for g in gates) + "]")
        if timeout_s is not None:
            lines.append(f"    timeout_s: {timeout_s}")
        path.write_text("\n".join(lines) + "\n")
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
    ``rollup`` replaces the always-green ``statusCheckRollup`` for EVERY view,
    so a test can hold a PR red across ticks (``views`` only answers once).
    ``update_results`` is the per-call answer to ``gh pr update-branch``:
    ``"ok"`` (default), ``"current"``, ``"conflict"`` or ``"error"``, consumed
    in order and then repeating the last one.
    """

    def __init__(self, heads, *, views=None, required_checks=None,
                 rollup=None, update_results=None, closed=None):
        self.heads = heads          # pr -> {headRefName, headRefOid}
        self.views = {pr: list(seq) for pr, seq in (views or {}).items()}
        self.required_checks = required_checks
        self.rollup = rollup
        self.update_results = list(update_results or [])
        self.closed = list(closed or [])   # PRs GitHub answers CLOSED for
        self.calls = []
        self.merged = []
        self.merge_calls = []
        self.comments = []
        self.updates = []           # PR numbers passed to update-branch

    def _view(self, pr, cwd=None):
        info = dict(self.heads[pr])
        info.setdefault("baseRefName", "main")
        info.setdefault("isCrossRepository", False)
        info.update({
            "number": pr,
            "state": ("MERGED" if pr in self.merged else
                      "CLOSED" if pr in self.closed else "OPEN"),
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "url": f"https://github.com/example/repo/pull/{pr}",
            "statusCheckRollup": (
                self.rollup if self.rollup is not None else [
                    {"__typename": "CheckRun", "status": "COMPLETED",
                     "conclusion": "SUCCESS"},
                ]),
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
        if argv[:3] == ["gh", "pr", "update-branch"]:
            self.updates.append(int(argv[3]))
            kind = self.update_results.pop(0) if self.update_results else "ok"
            if kind == "conflict":
                return SimpleNamespace(
                    returncode=1, stdout="",
                    stderr="failed to update branch: merge conflict between "
                           "base and head")
            if kind == "error":
                return SimpleNamespace(
                    returncode=1, stdout="",
                    stderr="could not resolve to PullRequest")
            if kind == "current":
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"PR #{argv[3]} is already up to date.", stderr="")
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
    """F5: checks reset by the force-push must stop the merge, not be ignored.

    The pushed head's CI is PENDING, not failed: the claim must stay in flight
    at ``ci-pending`` (not fall into a wait list that only drains on an empty
    queue) and a later tick merges that exact pushed SHA once it is green.
    """
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

    assert result["processed"][0]["result"] == "ci-pending", result
    assert gh.merged == []
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == []
    assert [e["pr"] for e in ps["waiting"]] == []
    assert ps["in_flight"]["stage"] == "ci-pending"
    pushed = _git(pair["origin"], "rev-parse", f"refs/heads/{pair['branch']}")
    assert ps["in_flight"]["pushed_sha"] == pushed
    assert [r["state"] for r in mq.view(
        state_path=_state(tmp_path), registry_path=registry)["rows"]] == [
            "ci-pending"]

    # Next tick: CI on the pushed head is green. Re-check in place and merge
    # that SHA; nothing was re-rebased or re-gated.
    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [3]
    assert gh.merge_calls[0][-1] == pushed
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"] is None and ps["queue"] == [] \
        and ps["waiting"] == []


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
    assert [e["pr"] for e in doc["projects"]["alpha"]["queue"]] == []
    assert [e["pr"] for e in doc["projects"]["alpha"]["waiting"]] == [4]


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
        # Empty rollup with required checks is "not reported yet": hold the
        # pushed head's claim instead of confusing it with a red gate.
        assert result["processed"][0]["result"] == "ci-pending", result
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


# ── a gate that runs out of budget ─────────────────────────────────────────
#
# The queue used to conflate "the gate never answered" with "the gate said
# no": one `(False, output)` tuple served both, so a slow queue recorded
# `red-gate`, dropped the PR out of the queue and told the author to fix code
# that was never shown to be broken.  Measured on main, the PR #360 and #460
# handbacks both end in the literal line `timeout after 1800s` and neither is
# a test failure.  These cases pin the split.  No case here runs a real gate:
# `mq.subprocess` — the only thing `_run_gate` shells through — is replaced by
# a namespace carrying `run` plus the real `TimeoutExpired` class, so a fake
# timeout is the only answer the leg can get and no repo or socket is touched
# by the gate path.

class _FakeGateProc:
    """The slice of ``subprocess.Popen`` that ``_run_gate`` actually touches.

    Deliberately *not* a real process: a gate test must never run a real suite,
    and a fake whose ``pid`` belongs to no OS process is also the proof that
    ``_kill_gate_group`` cannot crash the tick when its kill lands on ESRCH.
    """

    def __init__(self, *, returncode=0, stdout="", stderr="",
                 timeout=None, pid=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self._timeout = timeout
        self.pid = pid
        self.killed = False
        #: Every `timeout=` the leg was actually given.  Recorded rather than
        #: consumed: the budget is passed to `communicate()`, not to `Popen`, so
        #: the recorded kwargs cannot show that a timed-out leg was told where
        #: its budget was — and a leg with no budget is the bug this file exists.
        self.budgets = []

    def communicate(self, timeout=None):
        self.budgets.append(timeout)
        if self._timeout is not None:
            raise self._timeout
        return self.stdout, self.stderr

    def kill(self):
        self.killed = True


def _fake_subprocess(*, popem):
    """Stand-in for the module ``_run_gate`` shells through.

    Returns ``(fake_module, calls)``: ``calls`` is a mutable list that receives
    one entry per ``Popen`` — ``(argv, kwargs)`` — which is how a test reads the
    argv the gate *built* instead of guessing at it, and how it reaches the proc
    that ``_run_gate`` handed to ``_kill_gate_group``.
    """
    calls = []

    def popem_factory(argv, **kwargs):
        proc = popem(argv, kwargs)
        calls.append((argv, kwargs, proc))
        return proc

    fake = SimpleNamespace(Popen=popem_factory, PIPE=subprocess.PIPE,
                           TimeoutExpired=subprocess.TimeoutExpired)
    return fake, calls


def _timeout_popem(stdout=b"", stderr=b"", *, pid=None):
    """A gate that never answers, carrying ``stdout``/``stderr`` as the OS does.

    The attributes are assigned after construction because that is exactly what
    CPython's ``Popen._communicate`` does on the timeout path — it attaches the
    partial output as ``.stdout``/``.stderr``.  Asserting through the real
    attribute path is the point: the old code threw these values away here.
    """
    def build(argv, kwargs):
        exc = subprocess.TimeoutExpired(
            cmd=argv, timeout=kwargs.get("timeout") or 0)
        exc.stdout = stdout
        exc.stderr = stderr
        return _FakeGateProc(timeout=exc, pid=pid)
    return build


def _exit_popem(code, stderr="", stdout=""):
    def build(argv, kwargs):
        return _FakeGateProc(returncode=code, stdout=stdout, stderr=stderr)
    return build


def _tick_that_timed_out(tmp_path, monkeypatch, *, pr, card):
    """One tick whose single gate leg exhausts a 42 s budget."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["sh -c 'sleep 999'"], timeout_s=42)
    gh = FakeGh({pr: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    handed = []
    mq.add_to_queue("alpha", pr, card=card, assignee="worker",
                    path=_state(tmp_path))
    fake, _calls = _fake_subprocess(popem=_timeout_popem())
    monkeypatch.setattr(mq, "subprocess", fake)
    result = mq.tick(
        state_path=_state(tmp_path), root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        handback=lambda card_, assignee, msg: handed.append(
            (card_, assignee, msg)) or True)
    return result, pair, gh, handed, registry


def test_run_gate_returns_a_tri_state_verdict_not_a_bool(tmp_path,
                                                         monkeypatch):
    """The budget is a THIRD answer, not a flavour of failure."""
    worktree = str(tmp_path)

    fake, _calls = _fake_subprocess(popem=_timeout_popem())
    monkeypatch.setattr(mq, "subprocess", fake)
    verdict, output = mq._run_gate("sh -c 'sleep 999'", worktree, 42)
    assert verdict == "timeout", verdict
    assert "timeout after 42s" in output, output

    fake, _calls = _fake_subprocess(popem=_exit_popem(0))
    monkeypatch.setattr(mq, "subprocess", fake)
    assert mq._run_gate("true", worktree, 42)[0] == "pass"

    fake, _calls = _fake_subprocess(popem=_exit_popem(7, "BOOM"))
    monkeypatch.setattr(mq, "subprocess", fake)
    verdict, output = mq._run_gate("sh -c 'exit 7'", worktree, 42)
    assert verdict == "fail", verdict
    assert "BOOM" in output, output


# ── gate process handling: scheduling posture, the orphan, and the evidence ──
#
# Three measured failures of the same ~15 lines, two of them still open inside
# _run_gate and one fixed where it actually lives:
#   1. the gate inherited the daemon launchd job's `ProcessType => Background`
#      and every one of the 12 pytest legs was scheduled as background work.
#      The fix is NOT here: it is `ProcessType => Standard` in the three plist
#      emitters (test_daemon_process_type_standard_t_ae87b783.py pins them).  The
#      `taskpolicy -c utility` prefix this file used to build in front of the leg
#      was measured against real launchd and moved nothing — a clamped child of a
#      Background job stayed at `ps -o pri` 4 and paid the full ~7x, and under
#      Standard the job's children already sit in the utility band, so the clamp
#      was a no-op.  It is gone; the block above `LIVENESS_STAMP_S` in
#      merge_queue.py carries the table;
#   2. the timeout killed the `sh` but not the process group, so the gate's
#      pytest outlived its own budget and the next attempt ran against it;
#   3. `TimeoutExpired.stdout` arrives as `bytes` and was discarded by an
#      `isinstance(..., str)` test, so a timeout handback measured as
#      `['', '', 'timeout after 1800s']` and was indistinguishable from a
#      failure by anyone reading the PR comment.
# Every case below injects a fake gate runner and asserts on a *recorded* value:
# no wall-clock measurement (flaky on a shared host), no real repo, no network.

def _gate_argv(monkeypatch, *, popem, command="bash scripts/run_tests.sh",
               worktree="/nonexistent-worktree", timeout=42):
    """Run one fake gate leg and hand back the argv it was launched with.

    Only the *process* is faked; the leg still runs through the real
    ``_run_gate``, so what a test reads is the argv production built rather than
    one the test assembled for itself.  The cwd is never opened: a fake
    ``Popen`` does not validate it, which is how these cases stay off the repo.
    """
    fake, calls = _fake_subprocess(popem=popem)
    monkeypatch.setattr(mq, "subprocess", fake)
    mq._run_gate(command, worktree, timeout)
    return calls


def test_run_gate_builds_a_bare_shell_argv_with_no_qos_clamp(tmp_path,
                                                             monkeypatch):
    """The gate is the operator's shell string and nothing in front of it.

    Asserted on the constructed argv rather than on a stopwatch, because a
    wall-clock assertion on a shared host measures whatever else is running and
    would be deleted the first time it flaked — taking the guard with it.

    "Nothing in front of it" is now itself the invariant.  A `taskpolicy -c
    utility` prefix used to sit here, added to lift the leg out of the launchd
    job's background posture; measured against real launchd it did not move that
    posture at all (pri 4 either way) and under `Standard` it is a no-op, so it
    bought 0x and read as protection.  See the block above `LIVENESS_STAMP_S` in
    merge_queue.py.
    """
    command = "sh -c 'sleep 999'"
    calls = _gate_argv(monkeypatch, popem=_exit_popem(0), command=command)

    assert len(calls) == 1, calls
    argv, kwargs = calls[0][0], calls[0][1]
    # The operator's gate string arrives as ONE argument to a shell — the
    # invariant the removed QoS prefix had to not break, and still does not.
    assert argv == [mq.GATE_SHELL, "-c", command], argv
    # No scheduling-policy program is exec'd ahead of the shell: the leg runs at
    # the daemon's own posture, which the plist pins to Standard.
    assert not [a for a in argv if "taskpolicy" in a], argv
    # NOT shell=True: with a list argv, POSIX `shell=True` runs
    # `/bin/sh -c argv[0] argv[1] ...`, so only the first element would be the
    # command and the rest would become the shell's $0/$1/$2 — which is exactly
    # how the old prefix ended up exec'd bare, exit 64, `red-gate` on every PR.
    # See :func:`test_the_gate_argv_actually_runs_the_operators_command`, which
    # execs the argv for real rather than trusting its shape.
    assert kwargs.get("shell") is False, kwargs
    # The gate string survives as exactly ONE argv element — never split across
    # several, which is what silently swallowed it before.
    assert argv[-1] == command, argv
    assert argv.count(command) == 1, argv


def test_the_gate_argv_no_longer_carries_the_removed_qos_machinery():
    """Re-adding a `taskpolicy` clamp must be a deliberate, measured decision.

    The three module attributes this asserts on are the machinery the gate used
    to build its prefix from.  They are gone because the mechanism was measured
    inert (real launchd, throwaway probe jobs, 2026-11-25: a clamped child of a
    Background job kept `ps -o pri` 4 and the full ~7x; under Standard the
    children already sit in the utility band at pri 20), and because
    `taskpolicy -c` parses only `utility`, `background` and `maintenance` — there
    is no clamp that means "standard".  If someone wants a prefix back, the
    honest path is a new measurement that moves `ps -o pri`, not a re-typing of
    these names.
    """
    for gone in ("GATE_QOS_POLICY_CLASS", "TASKPOLICY_PATH", "_gate_qos_prefix"):
        assert not hasattr(mq, gone), gone


def test_the_gate_argv_actually_runs_the_operators_command(tmp_path):
    """The argv must be EXECUTED, not only inspected.

    This is the case the shape assertions cannot stand in for.  A fake ``Popen``
    records whatever argv it is handed and can never notice that a list argv with
    ``shell=True`` means "exec argv[0], hand the rest to the shell as $0/$1/$2":
    that build satisfied every shape assertion and then exited **64** for real,
    because the command the OS ran was a bare `taskpolicy`.  Through the queue
    that is a `red-gate` on every PR on the Mac, against code that was never
    compiled — the exact false-red this card exists to avoid.

    So this case runs the argv `_run_gate` builds, unchanged, for real.  No fake
    is injected and the production path (argv, `shell=False`, the new session)
    is what is under test.  The gate here is a `printf` in a throwaway tmp dir,
    not the suite: no repo, no network, no daemon, milliseconds — and no
    wall-clock claim, so it cannot flake on a busy host.
    """
    command = "printf alpha; printf ' %s' beta && printf ' %s' gamma"
    verdict, output = mq._run_gate(command, str(tmp_path), 60)

    assert verdict == "pass", (verdict, output)
    # A shell ran the WHOLE string: two commands, a `&&` and a quoted format
    # argument all survived the argv construction intact.
    assert "alpha beta gamma" in output, output


def test_run_gate_starts_its_gate_in_its_own_session(tmp_path, monkeypatch):
    """Without a new session there is no group to kill, so item 2 is a no-op."""
    calls = _gate_argv(monkeypatch, popem=_exit_popem(0))

    assert calls[0][1].get("start_new_session") is True, calls[0][1]


def test_an_unconfigured_timeout_s_still_gates_on_the_3600s_default(tmp_path,
                                                                    monkeypatch):
    """A registry that says nothing about the budget still gets 3600 s.

    The default lives in `process_entry` (`cfg.get("timeout_s") or
    DEFAULT_GATE_TIMEOUT_S`), *above* `_run_gate`, so it is pinned through a real
    tick whose injected runner records the budget it was handed.  Two halves
    matter: the queue must still pass 3600 down, and the leg must actually be
    told it — a gate started with no budget is a gate that can run forever.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["bash scripts/run_tests.sh"])  # no timeout_s
    import yaml
    declared = yaml.safe_load(open(registry))["projects"][0]["merge_queue"]
    assert "timeout_s" not in declared, declared
    gh = FakeGh({21: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 21, path=_state(tmp_path))
    fake, calls = _fake_subprocess(popem=_timeout_popem(pid=1))
    monkeypatch.setattr(mq, "subprocess", fake)
    monkeypatch.setattr(mq.os, "killpg", lambda pgid, sig: None)

    result = mq.tick(state_path=_state(tmp_path), root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run, handback=lambda *args: True)

    row = result["processed"][0]
    assert row["result"] == "gate-timeout", row
    assert row["timeout_s"] == 3600, row
    assert mq.DEFAULT_GATE_TIMEOUT_S == 3600, mq.DEFAULT_GATE_TIMEOUT_S
    budgets = calls[0][2].budgets
    # The leg was told where its budget was.  The second, budget-less call is
    # the reaping `communicate()` after the SIGKILL — it must not carry one.
    assert budgets[0] == mq.DEFAULT_GATE_TIMEOUT_S, budgets
    assert len(budgets) == 2 and budgets[1] is None, budgets


def test_the_gate_argv_is_platform_independent():
    """No host gets a special program in front of the gate any more.

    The removed prefix was darwin-only and cached for the life of the process,
    so its answer depended on the platform and on whether `/usr/sbin/taskpolicy`
    happened to exist — three states to reason about for a mechanism measured to
    change nothing.  This asserts the argv is the same three elements on the
    host running the test, and (by reading the function's own source) that no
    platform branch has crept back in to make the answer host-dependent again.
    """
    import inspect

    assert mq._gate_shell_argv("true") == [mq.GATE_SHELL, "-c", "true"]
    src = inspect.getsource(mq._gate_shell_argv)
    assert "sys.platform" not in src, src
    # Everything after the docstring is code, and no code may name a policy tool.
    assert "taskpolicy" not in src[src.rindex('"""') + 3:], src


def test_gate_timeout_kills_the_whole_process_group_and_still_requeues(
        tmp_path, monkeypatch):
    """No leg may survive its own budget, and the kill must not be load-bearing.

    Asserted on the *recorded* process group ``os.killpg`` was aimed at — not on
    a sleep.  The recorded pid is the gate's own pid, which is only equal to its
    process-group id because the leg runs `start_new_session=True`; killing that
    pgid reaches the pytest the shell spawned, which is the orphan that poisoned
    the next attempt.
    """
    killed = []
    fake, calls = _fake_subprocess(popem=_timeout_popem(pid=424242))
    monkeypatch.setattr(mq, "subprocess", fake)
    monkeypatch.setattr(mq.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)

    assert len(killed) == 1, killed
    pgid, sig = killed[0]
    assert pgid == 424242, "must target the gate's own group, not its parent"
    assert sig == mq.signal.SIGKILL, sig
    # And the process group the kill aimed at is the one the leg was started in.
    assert calls[0][1].get("start_new_session") is True, calls[0][1]
    assert calls[0][2].killed is True, "the shell itself must be reaped too"
    # The kill is cleanup, never the thing that ends the leg: the verdict, the
    # budget and the tail are what the caller requeues on.
    assert verdict == "timeout", verdict
    assert "timeout after 42s" in output, output


def test_gate_timeout_returns_its_verdict_even_when_the_kill_cannot_land(
        tmp_path, monkeypatch):
    """A kill that cannot land may not eat the verdict that saves the PR.

    Two real shapes: the group already gone (`ESRCH`), and a fake/absent pid.
    Both used to be hypothetical; an escaping exception here would take the
    daemon's whole tick down instead of handing the PR back.
    """
    def exploding_killpg(pgid, sig):
        raise ProcessLookupError(3, "No such process")

    fake, calls = _fake_subprocess(popem=_timeout_popem(pid=999999))
    monkeypatch.setattr(mq, "subprocess", fake)
    monkeypatch.setattr(mq.os, "killpg", exploding_killpg)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)
    assert verdict == "timeout", verdict
    assert "timeout after 42s" in output, output
    assert calls[0][2].killed is True

    # No pid at all: still a timeout, still a requeue, still no escape.
    fake, calls = _fake_subprocess(popem=_timeout_popem(pid=None))
    monkeypatch.setattr(mq, "subprocess", fake)
    hit = []
    monkeypatch.setattr(mq.os, "killpg",
                        lambda pgid, sig: hit.append(pgid))

    verdict, _output = mq._run_gate("bash scripts/run_tests.sh",
                                    str(tmp_path), 42)
    assert verdict == "timeout", verdict
    assert hit == [], "no pid means no group to aim at, not a blind kill"


def test_gate_timeout_handback_carries_bytes_evidence(tmp_path, monkeypatch):
    """A timeout must be readable as a timeout, with real last lines.

    This is the `['', '', 'timeout after 1800s']` case: `TimeoutExpired.stdout`
    is `bytes`, and the old `isinstance(..., str)` test turned real output into
    an empty handback, so nobody could tell a timeout from a failure.
    """
    fake, _calls = _fake_subprocess(
        popem=_timeout_popem(stdout=b"real tail line",
                             stderr=b"FAILED hscc_daemon/tests/test_x.py"))
    monkeypatch.setattr(mq, "subprocess", fake)
    monkeypatch.setattr(mq.os, "killpg", lambda pgid, sig: None)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)

    assert verdict == "timeout", verdict
    assert "real tail line" in output, output
    assert "FAILED hscc_daemon/tests/test_x.py" in output, output
    assert "timeout after 42s" in output, output


def test_gate_timeout_handback_survives_a_truncated_multibyte_tail(
        tmp_path, monkeypatch):
    """One half-written character at the kill point may not eat the whole tail."""
    fake, _calls = _fake_subprocess(
        popem=_timeout_popem(stdout=b"leg 7 ok\xe2\x80"))  # cut mid-em-dash
    monkeypatch.setattr(mq, "subprocess", fake)
    monkeypatch.setattr(mq.os, "killpg", lambda pgid, sig: None)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)

    assert verdict == "timeout", verdict
    assert "leg 7 ok" in output, output


def test_run_gate_reports_a_gate_that_could_not_be_started(tmp_path, monkeypatch):
    """An un-startable gate is a real failure, not a budget problem.

    A `timeout` verdict requeues and retries, so mislabelling this as a timeout
    would turn a broken gate into a PR that never lands and never explains why.
    """
    def raiser(argv, kwargs):
        raise OSError(2, "No such file or directory")

    fake, _calls = _fake_subprocess(popem=raiser)
    monkeypatch.setattr(mq, "subprocess", fake)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)

    assert verdict == "fail", verdict
    assert "could not run gate" in output, output


# ── cancellation: a gate leg the queue no longer wants ─────────────────────
#
# t_6c3ae143. All six `_cancel_reason` checkpoints in `process_entry` sit
# OUTSIDE a gate leg, and a leg blocks in `communicate(timeout=)`; so a
# remove/pause/disable that lands *during* a leg used to be recorded correctly
# and obeyed never. Measured on 2026-11-24: a full-suite leg kept running for a
# PR that had already been merged by hand, with `in_flight` already null.
#
# The two things these cases exist to protect are the two things the fix must
# not do: end a gate because of ELAPSED TIME (only the ledger may cancel it),
# and kill only the SHELL (the `pytest` grandchild is the part that burns the
# box). The first is asserted against a still-pending PR; the second against a
# real process group, because no fake pid can show a second process dying.

class _CancellableGateProc(_FakeGateProc):
    """A leg that keeps running until something kills it.

    ``hold_s`` is how long it stays alive when nothing kills it, so the cases
    that assert NO cancellation still finish. ``on_block`` runs on the thread
    that entered the leg, which is how a test lands a ledger change *during* the
    gate with no sleep and no race: by the time the leg is running, the change
    is already a fact.
    """

    def __init__(self, *, pid, hold_s=5.0, on_block=None):
        super().__init__(returncode=0, pid=pid)
        self.hold_s = hold_s
        self._on_block = on_block
        self._blocked = False
        self._released = threading.Event()

    def communicate(self, timeout=None):
        self.budgets.append(timeout)
        # Once per leg, not once per call: `_kill_gate_group` also calls
        # `communicate()` when it reaps, and a second `remove` would be the fake
        # inventing a side effect the OS does not have.
        if self._on_block is not None and not self._blocked:
            self._blocked = True
            self._on_block()
        self._released.wait(self.hold_s)
        return self.stdout, self.stderr

    def kill(self):
        super().kill()
        # What the OS reports for a process the group kill took — not a code the
        # gate chose, which is the only thing that lets `_run_gate` tell a
        # cancelled leg apart from a suite that failed on its own merits.
        self.returncode = -signal.SIGKILL
        self._released.set()


def _cancellable_popem(*, hold_s=5.0, on_block=None, pid=4242):
    def build(argv, kwargs):
        return _CancellableGateProc(pid=pid, hold_s=hold_s, on_block=on_block)
    return build


def _record_killpg(monkeypatch):
    """Record every group kill without aiming it at the OS.

    A fake proc's pid belongs to no OS process, and may collide with one that
    belongs to something else, so the real `killpg` must not run — the same
    reason the existing fake-gate tests all patch it. Returns the hit list:
    ``(pgid, sig)`` per call, which is how a test reads *which group* the kill
    was aimed at.
    """
    hits = []
    monkeypatch.setattr(mq.os, "killpg",
                        lambda pgid, sig: hits.append((pgid, sig)))
    return hits


def _live_cancel_watches():
    """Mirror of `_live_heartbeats`: a watcher must not outlive its leg either."""
    return [t for t in threading.enumerate()
            if t.name.startswith(mq._CANCEL_THREAD_PREFIX)]


def _count_cancels(monkeypatch):
    """Count the mid-leg reads, by wrapping the real predicate."""
    polls = []
    real = mq._cancel_reason

    def counting(state_path, project, entry, cfg):
        polls.append(entry["pr"])
        return real(state_path, project, entry, cfg)

    monkeypatch.setattr(mq, "_cancel_reason", counting)
    return polls


def test_a_pr_removed_mid_leg_is_cancelled_before_its_budget_is_spent(
        tmp_path, monkeypatch):
    """The bug, end to end: `remove` lands during a leg and the leg stops.

    Driven through `tick()` so the claim is claimed by the queue and released by
    the queue, and with `timeout_s=3600` so the answer cannot come from the
    budget: the only thing that can end this leg is the ledger.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["bash scripts/run_tests.sh"], timeout_s=3600)
    gh = FakeGh({41: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    # Without this the tick has no entry to claim and would emit nothing at all
    # — a green run that would prove nothing whatever.
    mq.add_to_queue("alpha", 41, path=_state(tmp_path))
    handed = []
    hits = _record_killpg(monkeypatch)
    monkeypatch.setattr(mq, "GATE_CANCEL_POLL_S", 0.05)
    gone = lambda: mq.remove_from_queue("alpha", 41, path=_state(tmp_path))
    fake, calls = _fake_subprocess(popem=_cancellable_popem(hold_s=20.0,
                                                            on_block=gone))
    monkeypatch.setattr(mq, "subprocess", fake)

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     handback=lambda card, assignee, msg: handed.append(
                         (card, assignee, msg)) or True)

    assert result["processed"][0]["result"] == "removed", result
    proc = calls[0][2]
    # The leg was given its full budget and never got to spend it. Exactly ONE
    # `communicate()`: a watcher that also reaped would be a second reader on the
    # same proc, double-closing the pipes the leg is still reading.
    assert proc.budgets == [3600], proc.budgets
    assert proc.killed is True
    # Aimed at the gate's OWN pid, which `start_new_session` made its group id:
    # that is what reaches the pytest child, not just the shell.
    assert hits == [(proc.pid, signal.SIGKILL)], hits
    # Nothing was merged, nothing was pushed, and nobody was told anything.
    assert gh.merged == []
    assert handed == [], "a cancelled leg must not hand back to the author"
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert ps["queue"] == [] and ps["waiting"] == []
    # And above all: a cancelled leg records NO verdict about the code.
    assert ps["last"] is None, ps["last"]
    assert _live_cancel_watches() == []


def test_a_gate_leg_on_a_still_pending_pr_is_never_cancelled(tmp_path,
                                                             monkeypatch):
    """The guard rail: polling may observe a healthy leg, never end one.

    The entry keeps its claim for the whole leg, so the poll runs repeatedly and
    fires nothing. If cancellation were keyed to elapsed time this case would
    kill a legitimately running gate, which is the failure the card forbids.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["bash scripts/run_tests.sh"], timeout_s=3600)
    gh = FakeGh({42: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 42, path=_state(tmp_path))
    hits = _record_killpg(monkeypatch)
    polls = _count_cancels(monkeypatch)
    monkeypatch.setattr(mq, "GATE_CANCEL_POLL_S", 0.05)
    # hold_s 1.0 against a 0.05 poll is ~20 polls, so the assertion below still
    # holds on a box slow enough to miss most of them.
    fake, calls = _fake_subprocess(popem=_cancellable_popem(hold_s=1.0))
    monkeypatch.setattr(mq, "subprocess", fake)

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    proc = calls[0][2]
    assert proc.killed is False
    assert hits == [], hits
    # More reads than `process_entry` has checkpoints (six, and only ~4 are on
    # this path): proof the poll ran beside the leg rather than not at all.
    assert len(polls) >= 6, polls
    assert _live_cancel_watches() == []


def test_a_cancel_check_that_raises_leaves_the_running_gate_alone(
        tmp_path, monkeypatch):
    """Fail OPEN. `_cancel_reason` takes the file lock; a lock hiccup, a torn
    read or any other surprise must never be what kills a healthy suite.
    """
    hits = _record_killpg(monkeypatch)
    monkeypatch.setattr(mq, "GATE_CANCEL_POLL_S", 0.05)
    raised = []

    def raiser():
        raised.append(1)
        raise RuntimeError("state file unreadable")

    fake, calls = _fake_subprocess(popem=_cancellable_popem(hold_s=0.4))
    monkeypatch.setattr(mq, "subprocess", fake)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   3600, cancel_check=raiser)

    assert verdict == "pass", output
    assert raised, "the poll never ran, so this case proved nothing"
    assert hits == [] and calls[0][2].killed is False


def test_a_cancelled_gate_takes_its_whole_process_group_with_it(tmp_path,
                                                                monkeypatch):
    """The part that actually burns the box is the gate's CHILD, so this case
    runs a REAL process group.

    A fake cannot show that a second process died: it has one pid and it execs
    nothing. Here a shell backgrounds a `sleep`, reports its pid, and the test
    asserts that pid is dead afterwards and that the group kill was aimed at the
    group that pid belongs to — the shape of `bash scripts/run_tests.sh` with a
    `pytest -q -rs` under it, measured live as pids 75734/27435 sharing pgid
    75734. Only processes this test spawned are killed.
    """
    marker = tmp_path / "child.pid"
    seen = {}

    def cancel_check():
        # Never cancel before the child exists: until the marker is written
        # there is nothing to prove, and a premature kill would prove nothing.
        try:
            child = int(marker.read_text().strip())
            seen["child"] = child
            seen["pgid"] = os.getpgid(child)
        except (OSError, ValueError):
            return None
        return "removed"

    hits = []
    real_killpg = os.killpg

    def recording(pgid, sig):
        hits.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(mq.os, "killpg", recording)
    command = f"( sleep 30 & echo $! > '{marker}' ; wait )"
    try:
        verdict, output = mq._run_gate(command, str(tmp_path), 60,
                                       cancel_check=cancel_check,
                                       cancel_poll_s=0.05)
    finally:
        # Never leave a `sleep` running for the rest of the suite.
        if "pgid" in seen:
            with contextlib.suppress(OSError):
                real_killpg(seen["pgid"], signal.SIGKILL)

    assert verdict == "cancelled", output
    assert hits and all(sig == signal.SIGKILL for _, sig in hits), hits
    # The kill was aimed at the group the CHILD belongs to — not at a bare pid.
    assert hits[0][0] == seen["pgid"], (hits, seen)
    assert seen["child"] != seen["pgid"], "no second process, no tree proof"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(seen["child"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the gate's child survived the group kill")


def test_gate_timeout_is_not_a_red_gate_and_keeps_the_pr(tmp_path,
                                                         monkeypatch):
    """A budget exhaustion is non-terminal: back to the wait list, not gone."""
    result, pair, gh, _handed, _registry_path = _tick_that_timed_out(
        tmp_path, monkeypatch, pr=31, card="t_slow")

    row = result["processed"][0]
    assert row["result"] == "gate-timeout", result
    assert row["timeout_s"] == 42, row
    assert "sleep 999" in row["gate"], row

    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert [e["pr"] for e in ps["waiting"]] == [31], ps
    assert ps["queue"] == []
    assert ps["in_flight"] is None
    assert ps["last"]["result"] == "gate-timeout", ps["last"]
    # Nothing recorded anywhere may read as a test failure.
    assert "red-gate" not in json.dumps(doc), doc
    # And nothing was pushed or merged on the way out.
    assert gh.merged == []
    assert _git(pair["origin"], "rev-parse",
                f"refs/heads/{pair['branch']}") == pair["head"]


def test_gate_timeout_handback_names_the_budget_and_blames_nobody(
        tmp_path, monkeypatch):
    """The author must be told it is a budget, not a broken PR."""
    _result, _pair, gh, handed, _registry_path = _tick_that_timed_out(
        tmp_path, monkeypatch, pr=32, card="t_slow2")

    assert handed, "a handed-back gate must still say something"
    card, assignee, body = handed[0]
    assert (card, assignee) == ("t_slow2", "worker"), handed
    assert "42s" in body, body
    assert "NOT a test failure" in body, body
    assert "will look again" in body, body
    # The resolve/force-push script belongs to a gate that actually said no.
    assert "Resolve it as this card's worker" not in body, body
    assert "force-with-lease" not in body, body
    assert "git rebase origin/main" not in body, body
    # The PR comment carries the same words, not the old "gate failed" text.
    comment = " ".join(gh.comments[0])
    assert "NOT a test failure" in comment, comment
    assert "gate failed" not in comment, comment


def test_a_real_red_gate_still_drops_the_entry(tmp_path, monkeypatch):
    """Must not regress: a genuine non-zero gate is still terminal."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["sh -c 'exit 7'"],
                         timeout_s=42)
    gh = FakeGh({33: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 33, card="t_red_drop", path=_state(tmp_path))
    fake, _calls = _fake_subprocess(popem=_exit_popem(7, "BOOM"))
    monkeypatch.setattr(mq, "subprocess", fake)

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "red-gate", result
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    # Dropped, not requeued: `waiting` stays empty, which is exactly what the
    # timeout case above must NOT do.
    assert ps["queue"] == [] and ps["waiting"] == []
    assert ps["in_flight"] is None
    assert ps["last"]["result"] == "red-gate", ps["last"]
    assert gh.merged == []


# ── the failing TEST NAME must survive the tail ────────────────────────────
#
# Measured on main 2026-11-23: PR #673 came back `red-gate` and its handback
# named no test at all.  It was not a truncated write — the CLI comment path
# stores what it is given (`--max-len` is opt-in and the queue never passes it)
# — it was `_tail(text, 40)`: `scripts/run_tests.sh` runs 12 legs and the failed
# leg's pytest short summary (`FAILED path::test - reason`) sits ~60 lines above
# the end of the run, because the legs that ran *after* it plus the trailing
# stderr noise the API appends push it out of the window.  The most the author
# and the orchestrator could read was `✗ hscc-api (pytest exit 1)`, which says a
# leg went red but not which test — so flake-vs-real needed a manual
# `gh pr checks` plus an Actions crawl of a 22-minute log.
#
# The digest below is taken from the FULL output and leads the handback; the
# tail itself is untouched, so a green gate's output is byte-identical.

#: The line the 40-line tail lost, spelled exactly as pytest prints it.
_FAILED_TEST = ("FAILED hscc-api/tests/test_ws_relay.py::"
                "test_writer_restarts_after_kill - assert 0 == 1")

#: The only line in the old handback that hinted anything was red.
_RED_LEG_ROW = "  ✗ hscc-api (pytest exit 1)"


def _multi_leg_gate_output():
    """`run_tests.sh`-shaped output whose failure is NOT in the last 40 lines.

    The distance is the whole bug: every line after `_FAILED_TEST` is filler the
    tail window kept, and there are >40 of them ON PURPOSE, so this case fails
    without the digest instead of passing by luck of a short suite.
    """
    rows = ["━━━ hscc_daemon ━━━", "................ [100%]",
            "649 passed, 5 skipped in 1358.65s (0:22:38)",
            "━━━ hscc-api ━━━", "....F.... [ 42%]",
            "========== short test summary info ==========", _FAILED_TEST,
            "1 failed, 412 passed in 300.12s"]
    for leg in ("memori_byodb", "scripts", ".github/scripts",
                "comfyui-sparkrun"):
        rows.append(f"━━━ {leg} ━━━")
        rows += [f"......... {leg} progress {i} [ 50%]" for i in range(13)]
        rows.append("134 passed in 40.54s")
    rows += ["", "━━━ Summary ━━━", "  ✓ hscc_daemon", _RED_LEG_ROW,
             "  ✓ comfyui-sparkrun", "FAILURES ABOVE"]
    return "\n".join(rows)


#: The stderr that ran_tests.sh's consumers append after stdout, which lands
#: *behind* every pytest line in the combined tail (the concatenation in
#: `_run_gate` puts stderr last).
_GATE_STDERR_NOISE = "\n".join(
    f"client 127.0.0.1 disconnected mid-request (BrokenPipeError)"
    for _ in range(5))


def test_a_failing_legs_test_line_survives_the_40_line_tail(tmp_path,
                                                            monkeypatch):
    """A red leg's `FAILED path::test` must reach the handback, verbatim.

    PR #673 is the case: the tail window held the later legs' dots and the
    summary block, so the comment proved the suite was red and nothing else.
    """
    fake, _calls = _fake_subprocess(
        popem=_exit_popem(7, stderr=_GATE_STDERR_NOISE,
                          stdout=_multi_leg_gate_output()))
    monkeypatch.setattr(mq, "subprocess", fake)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)

    assert verdict == "fail", verdict
    assert _FAILED_TEST in output, output
    assert _RED_LEG_ROW.strip() in output, output
    # The tail is still there: the digest is added, never substituted.
    assert "FAILURES ABOVE" in output, output
    assert output.index(_FAILED_TEST) < output.index("FAILURES ABOVE"), output


def test_a_green_gate_hands_back_the_plain_tail_unchanged(tmp_path,
                                                          monkeypatch):
    """Nothing may shift for a leg that passed — the digest is for failures.

    Pinning byte-equality here is what keeps the fix from quietly reformatting
    every green run's evidence (and from scanning 12 green legs for markers).
    """
    text = "━━━ scripts ━━━\n......... [100%]\n649 passed in 1358.65s\n"
    fake, _calls = _fake_subprocess(popem=_exit_popem(0, stdout=text))
    monkeypatch.setattr(mq, "subprocess", fake)

    verdict, output = mq._run_gate("bash scripts/run_tests.sh", str(tmp_path),
                                   42)

    assert verdict == "pass", verdict
    assert output == mq._tail(text), repr(output)


def test_failure_lines_are_verbatim_deduped_and_capped():
    """The digest is the gate's own lines, capped — not a rewrite, not a dump.

    Capping is what makes it safe to read the FULL output: a branch whose
    fixture blew up can print hundreds of `FAILED` rows, and a card comment is
    not a log file.
    """
    many = "\n".join(
        [f"FAILED tests/test_a.py::test_{i} - boom {i}"
         for i in range(mq.MAX_FAILURE_LINES + 5)]
        # A duplicate short-summary line (same line twice) must appear once.
        + ["FAILED tests/test_a.py::test_0 - boom 0",
           # Noise the matchers must NOT pick up: a progress line, a mention
           # mid-line, and a `✗` that is not a red-leg row.
           "FAILED is what the fixture text mentions inline",
           "........ [ 50%]", "  ✓ hscc-daemon", ""]
        + [f"  ✗ leg-{i} (pytest exit 1)" for i in range(3)]
        + ["3 failed, 900 passed in 12.3s"])

    lines = mq._failure_lines(many)

    # The rows naming the leg lead, and the cap may never cost them their place:
    # `scripts/run_tests.sh` prints them LAST, so a cap that respected print order
    # would drop the one line saying WHICH leg went red first.
    assert lines[0] == "✗ leg-0 (pytest exit 1)", lines[0]
    assert "✗ leg-2 (pytest exit 1)" in lines, lines
    assert "3 failed, 900 passed in 12.3s" in lines, lines
    # Then the failing tests, in the order pytest printed them, de-duplicated.
    assert lines.index("FAILED tests/test_a.py::test_0 - boom 0") < \
        lines.index("FAILED tests/test_a.py::test_1 - boom 1"), lines
    assert lines.count("FAILED tests/test_a.py::test_0 - boom 0") == 1, lines
    assert "........ [ 50%]" not in lines, lines
    assert "FAILED is what the fixture text mentions inline" not in lines, lines
    assert "  ✓ hscc-daemon" not in lines, lines
    # Capped, and the cap says how many it hid rather than silently cutting.
    shown = [ln for ln in lines if not ln.startswith("…")]
    assert len(shown) <= mq.MAX_FAILURE_LINES, len(shown)
    assert "FAILED tests/test_a.py::test_24 - boom 24" not in lines, lines
    assert any("more failure line(s) not shown" in ln for ln in lines), lines


def test_a_long_failure_line_is_capped_not_dropped():
    """A 2 KB failure reason may not eat the comment; the test id is the point."""
    long_line = "FAILED tests/test_ws.py::test_long - " + "x" * 5000

    lines = mq._failure_lines(long_line)

    assert len(lines) == 1, lines
    assert len(lines[0]) <= mq.MAX_FAILURE_LINE_CHARS, len(lines[0])
    assert lines[0].startswith("FAILED tests/test_ws.py::test_long"), lines[0]


def test_the_red_gate_handback_names_the_test_the_leg_and_the_attempt(
        tmp_path, monkeypatch):
    """The card comment alone must be enough to decide re-queue vs fix.

    This is the deliverable: leg name, attempt, and the verbatim `FAILED`
    lines, on the card — so the driver never has to open the Actions log to
    tell a flake from a real break.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["bash scripts/run_tests.sh"], timeout_s=42)
    gh = FakeGh({34: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    handed = []
    mq.add_to_queue("alpha", 34, card="t_red_named", assignee="worker",
                    path=_state(tmp_path))
    fake, _calls = _fake_subprocess(
        popem=_exit_popem(1, stderr=_GATE_STDERR_NOISE,
                          stdout=_multi_leg_gate_output()))
    monkeypatch.setattr(mq, "subprocess", fake)

    result = mq.tick(
        state_path=_state(tmp_path), root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        handback=lambda card, assignee, msg: handed.append(
            (card, assignee, msg)) or True)

    assert result["processed"][0]["result"] == "red-gate", result
    card, assignee, body = handed[0]
    assert (card, assignee) == ("t_red_named", "worker"), handed
    assert _FAILED_TEST in body, body
    assert _RED_LEG_ROW.strip() in body, body
    # The attempt is 1-based: `attempts` counts retries already spent, so a
    # first-pass bounce reads `Attempt: 1`, never `Attempt: 0`.
    assert "Attempt: 1" in body, body
    # Evidence leads: the failing line is above the resolve boilerplate.
    assert body.index(_FAILED_TEST) < body.index(
        "Resolve it as this card's worker"), body
    # The PR comment is the same text, not a thinner copy.
    comment = " ".join(gh.comments[0])
    assert _FAILED_TEST in comment, comment


def test_a_real_gate_run_carries_its_failure_lines(tmp_path):
    """The fake-Popen proof, replayed through a gate the OS actually ran.

    A fake `Popen` hands back exactly the strings the test wrote, so on its own
    it cannot show that the real `communicate()` output reaches the digest
    through the real argv.  This runs a `sh` that prints the same 12-leg shape
    and exits 1: milliseconds, no repo, no network, no wall-clock claim.
    """
    script = tmp_path / "fake_gate.sh"
    script.write_text("#!/bin/sh\ncat <<'HSCCEOF'\n"
                      f"{_multi_leg_gate_output()}\n"
                      "HSCCEOF\n"
                      "printf 'client disconnected (BrokenPipeError)\\n' >&2\n"
                      "exit 1\n")

    verdict, output = mq._run_gate(f"sh '{script}'", str(tmp_path), 60)

    assert verdict == "fail", (verdict, output)
    assert _FAILED_TEST in output, output
    assert _RED_LEG_ROW.strip() in output, output


def test_a_timed_out_gate_stamps_the_timeout_stage_on_the_health_stream(
        tmp_path, monkeypatch):
    """``hscc verify`` reads the stream, so the stream must not keep saying
    ``gating`` (or worse, ``merging``) for a PR the queue has handed back.

    The stage is stamped while the claim is still held, so the health stream and
    the ledger agree at that moment; the requeue that follows is what makes the
    row render ``waiting``.  Without the stamp a timed-out PR looks like a PR
    still being merged until the daemon's next check writes the tick verdict.
    """
    _result, _pair, _gh, _handed, _registry = _tick_that_timed_out(
        tmp_path, monkeypatch, pr=35, card="t_slow4")

    stamp = _require_stream(tmp_path)
    flight = stamp.get("in_flight") or {}
    assert flight.get("stage") == "timeout", stamp
    assert flight.get("pr") == 35, stamp
    assert flight.get("stage") in mq.MERGE_QUEUE_STATES


def test_outcome_state_maps_a_gate_timeout_to_waiting():
    """The read model may never call a timeout a failure to go fix."""
    assert mq.outcome_state("gate-timeout") == "waiting"
    assert "timeout" in mq.MERGE_QUEUE_STATES
    # A real failure still lands where the author can act on it.
    assert mq.outcome_state("red-gate") == "resolve"
    assert mq.outcome_state("merged") == "merged"
    assert mq.outcome_state("nonsense") == "failed"


def test_view_shows_a_timed_out_pr_as_waiting(tmp_path, monkeypatch):
    """`status` / GET /v1/merge-queue must not render it resolve or failed."""
    _result, _pair, _gh, _handed, registry = _tick_that_timed_out(
        tmp_path, monkeypatch, pr=34, card="t_slow3")

    seen = mq.view(state_path=_state(tmp_path), registry_path=registry)
    project_rows = seen["projects"]["alpha"]["rows"]
    rows = [r for r in project_rows if r.get("pr") == 34]
    assert len(rows) == 1, project_rows
    row = rows[0]
    assert row["state"] == "waiting", row
    assert row["state"] in mq.MERGE_QUEUE_STATES
    assert row["last_result"] == "gate-timeout", row
    assert "sleep 999" in str(row["detail"]), row
    # Nowhere in the whole view does this PR surface as a failure to go fix.
    assert [r["state"] for r in seen["rows"] if r.get("pr") == 34] == ["waiting"]
    assert seen["waiting"] == 1 and seen["queued"] == 0, seen["waiting"]


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


def test_the_running_gates_pgid_is_in_the_ledger_while_the_leg_runs(
        tmp_path, monkeypatch):
    """A daemon restart can only find the orphan gate through the ledger."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    seen = []

    def gate(command, worktree, timeout_s, cancel_check=None, *,
             on_start=None, **kw):
        on_start(4242)
        flight = mq.load_state(_state(tmp_path))[
            "projects"]["alpha"]["in_flight"]
        seen.append(flight.get("pgid"))
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     auto_source=lambda *a, **k: [])

    assert result["processed"][0]["result"] == "merged", result
    assert seen == [4242], seen
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert ps["queue"] == []


def test_run_gate_hands_the_spawned_pgid_to_the_ledger_callback(
        tmp_path, monkeypatch):
    """The pgid exists only after Popen; a callback failure never fails a gate."""
    fake, _calls = _fake_subprocess(
        popem=lambda argv, kwargs: _FakeGateProc(returncode=0, pid=4242))
    monkeypatch.setattr(mq, "subprocess", fake)
    seen = []

    verdict, _output = mq._run_gate("true", str(tmp_path), 42,
                                    on_start=seen.append)
    assert verdict == "pass"
    assert seen == [4242]

    def explode(_pid):
        raise RuntimeError("ledger write failed")

    verdict, _output = mq._run_gate("true", str(tmp_path), 42,
                                    on_start=explode)
    assert verdict == "pass", "bookkeeping may not fail the gate it records"


def test_restart_kills_the_previous_daemons_gate_group_before_regating(
        tmp_path):
    """The 2.17.6 orphan: gate reparented to launchd, still in the worktree."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    mq.add_to_queue("alpha", 13, card="t_restart", path=_state(tmp_path))
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    ps["in_flight"] = dict(ps["queue"].pop(0), pgid=4242, stage="gating")
    # Recovery runs before pause/enable can stop the tick, exactly as it must:
    # a paused queue still cannot leave a suite running in its checkout.
    ps["paused"] = True
    mq.save_state(doc, _state(tmp_path))

    calls = []
    alive = {4242}

    def fake_kill(pgid, sig):
        calls.append((pgid, sig))
        if pgid not in alive:
            raise ProcessLookupError(pgid)
        if sig == signal.SIGKILL:
            alive.discard(pgid)

    lines, log = _logger()
    result = mq.tick(state_path=_state(tmp_path), registry_path=registry,
                     gh_runner=_no_runner, git_runner=_no_runner,
                     kill_runner=fake_kill, pause=lambda _s: None, log=log)

    assert result["recovered"] == ["alpha"]
    assert [sig for _pgid, sig in calls if sig] == [
        signal.SIGTERM, signal.SIGKILL], calls
    assert all(pgid == 4242 for pgid, _sig in calls), calls
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [13]
    assert "pgid" not in ps["queue"][0], ps["queue"][0]
    assert any("gate process group 4242" in ln and "TERM" in ln
               for ln in lines), lines
    assert any("survived TERM" in ln and "KILL" in ln for ln in lines), lines


def test_recovery_never_signals_our_own_group_or_an_already_dead_one():
    """A live tick's ledger must not make the daemon kill itself, ever."""
    def explode(pgid, sig):
        raise AssertionError(f"signalled {pgid} with {sig}")

    for pgid in (os.getpgrp(), os.getpid(), 1, 0, None):
        assert mq._reclaim_orphan_gate({"pr": 1, "pgid": pgid},
                                       kill_runner=explode) is False

    def gone(pgid, sig):
        raise ProcessLookupError(pgid)

    assert mq._reclaim_orphan_gate({"pr": 1, "pgid": 4242},
                                   kill_runner=gone) is False


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

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        mq.remove_from_queue("alpha", 7, path=_state(tmp_path))
        return "pass", ""

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

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        if action == "pause":
            mq.set_paused("alpha", True, path=_state(tmp_path))
        else:
            mq.set_enabled("alpha", False, path=_state(tmp_path))
        return "pass", ""

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
    _write_leg_roster(tmp_path)
    calls = []

    def fake_git(argv, cwd=None, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ios-app/Foo.swift\n",
                               stderr="")

    gates = mq._resolve_gates(cfg, str(tmp_path), fake_git)
    assert gates[0] == "python3 scripts/address_guard.py --tracked"
    assert gates[1] == 'HSCC_TEST_LEGS="scripts" bash scripts/run_tests.sh'
    assert "bash ios-app/scripts/build_check.sh" in gates
    assert "bash macos-app/scripts/build_check.sh" not in gates


# ── local gate narrowing (t_0763da12) ──────────────────────────────────────

LOCAL_LEG_ROSTER = ("hscc-bootstrap", "hscc_daemon", "hscc-api", "scripts",
                    ".github/scripts")


def _write_leg_roster(worktree, legs=LOCAL_LEG_ROSTER):
    """The worktree the queue reads its leg roster from."""
    path = worktree / "scripts" / "run_tests.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DIRS=(" + " ".join(legs) + ")\n", encoding="utf-8")
    return path


def _diff_git(*paths):
    """A fake git runner whose diff is exactly ``paths`` (hermetic)."""
    def fake_git(argv, cwd=None, **kwargs):
        assert argv[:3] == ["git", "diff", "--name-only"], argv
        return SimpleNamespace(returncode=0,
                               stdout="\n".join(paths) + "\n", stderr="")

    return fake_git


def test_local_gate_runs_only_the_touched_leg(tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates(
        {"name": "hscc"}, str(tmp_path),
        _diff_git("hscc_daemon/merge_queue.py",
                  "hscc_daemon/tests/test_merge_queue.py",
                  "changelog.d/t_0763da12.md"))

    assert gates[0] == "python3 scripts/address_guard.py --tracked"
    assert gates[1] == ('HSCC_TEST_LEGS="hscc_daemon scripts" '
                        'bash scripts/run_tests.sh')
    assert len(gates) == 2


def test_local_gate_runs_every_touched_leg_in_roster_order(tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates(
        {"name": "hscc"}, str(tmp_path),
        _diff_git("hscc-api/x.py", "hscc_daemon/y.py", "scripts/tests/z.py"))

    assert gates[1] == ('HSCC_TEST_LEGS="hscc_daemon hscc-api scripts" '
                        'bash scripts/run_tests.sh')


@pytest.mark.parametrize("shared", [
    "scripts/run_tests.sh",
    "conftest.py",
    ".github/workflows/pytest-legs.yml",
    "scripts/changelog_fragments.py",
])
def test_shared_file_change_falls_back_to_the_full_suite(tmp_path, shared):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates({"name": "hscc"}, str(tmp_path),
                              _diff_git("hscc_daemon/x.py", shared))

    assert gates[1] == "bash scripts/run_tests.sh"


def test_more_than_three_legs_falls_back_to_the_full_suite(tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates(
        {"name": "hscc"}, str(tmp_path),
        _diff_git(*[f"{leg}/x.py" for leg in LOCAL_LEG_ROSTER]))

    assert gates[1] == "bash scripts/run_tests.sh"


def test_a_diff_outside_every_leg_falls_back_to_the_full_suite(tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates({"name": "hscc"}, str(tmp_path),
                              _diff_git("tools/deploy.sh"))

    assert gates[1] == "bash scripts/run_tests.sh"


def test_local_gate_narrows_a_docs_only_pr_to_the_scripts_leg(tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates(
        {"name": "hscc"}, str(tmp_path),
        _diff_git("docs/audits/2026-11-23-main-only-surface.md"))

    assert gates[0] == "python3 scripts/address_guard.py --tracked"
    assert gates[1] == 'HSCC_TEST_LEGS="scripts" bash scripts/run_tests.sh'
    assert len(gates) == 2


def test_local_gate_narrows_a_changelog_only_pr_to_the_scripts_leg(tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates({"name": "hscc"}, str(tmp_path),
                              _diff_git("changelog.d/t_8bffa4f7.md"))

    assert gates[1] == 'HSCC_TEST_LEGS="scripts" bash scripts/run_tests.sh'
    assert len(gates) == 2


def test_local_gate_narrows_an_apple_only_pr_to_scripts_and_its_build_check(
        tmp_path):
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates({"name": "hscc"}, str(tmp_path),
                              _diff_git("ios-app/widgets/x.json"))

    assert gates[1] == 'HSCC_TEST_LEGS="scripts" bash scripts/run_tests.sh'
    assert "bash ios-app/scripts/build_check.sh" in gates


def test_local_gate_keeps_a_docs_path_under_a_leg_dir_in_that_leg(tmp_path):
    """The prose prefixes are ROOT-anchored, exactly like leg_select's rules."""
    _write_leg_roster(tmp_path)
    gates = mq._resolve_gates({"name": "hscc"}, str(tmp_path),
                              _diff_git("hscc-api/docs/WIDGET_ROUTES.md"))

    assert gates[1] == 'HSCC_TEST_LEGS="hscc-api" bash scripts/run_tests.sh'


def test_local_gate_widens_prose_when_the_roster_has_no_scripts_leg(tmp_path):
    """A roster without the leg that guards prose cannot be narrowed."""
    _write_leg_roster(tmp_path, ("hscc_daemon", "hscc-api"))
    gates = mq._resolve_gates({"name": "hscc"}, str(tmp_path),
                              _diff_git("docs/x.md"))

    assert gates[1] == "bash scripts/run_tests.sh"


# ── leg_select parity (t_3e4e44ea) ─────────────────────────────────────────

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _leg_select():
    """Load the CI selector by path (stdlib only, so no import machinery)."""
    path = REPO_ROOT / ".github" / "scripts" / "leg_select.py"
    spec = importlib.util.spec_from_file_location("leg_select_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("paths", [
    ["docs/audits/2026-11-23-main-only-surface.md"],
    ["changelog.d/t_8bffa4f7.md"],
    ["ios-app/widgets/x.json"],
    ["macos-app/App.swift"],
    ["README.md", "CHANGELOG.md"],
    ["hscc-api/x.py", "docs/x.md"],
    ["hscc-api/docs/WIDGET_ROUTES.md"],
    ["tools/deploy.sh"],
    ["conftest.py"],
])
def test_local_selection_agrees_with_leg_select(paths):
    """The daemon's narrowing mirrors .github/scripts/leg_select.py::_RULES.

    Runs against the REAL roster so a drift on either side goes red. A CI
    selection of the whole roster maps to the daemon's ``None``; above
    MAX_LOCAL_TEST_LEGS the daemon widens where CI does not (pinned below).
    """
    roster = mq._test_leg_roster(str(REPO_ROOT))
    assert roster, "scripts/run_tests.sh must declare a roster"

    ci_legs, _ = _leg_select().select_legs(list(paths), roster)
    local = mq._local_test_legs(list(paths), roster)
    if len(ci_legs) > mq.MAX_LOCAL_TEST_LEGS:
        assert local is None, (ci_legs, local)
    else:
        assert local == ci_legs


def test_local_selection_widens_above_max_where_ci_does_not():
    roster = mq._test_leg_roster(str(REPO_ROOT))
    assert roster, "scripts/run_tests.sh must declare a roster"
    paths = ["hscc-bootstrap/x.py", "hscc-commands/x.py",
             "hscc-roles/x.py", "hscc-cluster/x.py"]

    ci_legs, _ = _leg_select().select_legs(paths, roster)
    assert len(ci_legs) > mq.MAX_LOCAL_TEST_LEGS, ci_legs
    assert mq._local_test_legs(paths, roster) is None


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

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        seen.update(mq.view(state_path=_state(tmp_path),
                            registry_path=registry))
        return "fail", "gate exploded"

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


# ── a concurrent CLI add survives the tick's auto-enqueue write (F9) ────────

def test_concurrent_cli_add_during_auto_enqueue_is_not_clobbered(tmp_path):
    """F9: the tick must reload+merge under the lock after its network probes.

    Reproduces audit reproduction K: a CLI `add` lands while auto-enqueue is
    probing; the tick's later save must preserve it instead of writing the
    snapshot it loaded at the top of the tick.
    """
    pair = _repo_pair(tmp_path)
    cli_head = _new_branch(pair["repo"], "wt/t_cli_add")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({
        7: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
        99: {"headRefName": "wt/t_cli_add", "headRefOid": cli_head},
    })

    def source(cfg, gh_runner=None):
        # The operator's add lands between the tick's load and its save.
        mq.add_to_queue("alpha", 99, card="t_cli_add", path=_state(tmp_path))
        return [{"pr": 7, "card": "t_auto", "assignee": "worker"}]

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run, auto_source=source)

    # The added PR is older, so it is the one claimed; the auto candidate
    # stays queued. Before the fix the stale save erased 99 entirely.
    assert result["processed"][0]["pr"] == 99, result
    assert gh.merged == [99]
    doc = mq.load_state(_state(tmp_path))
    ps = doc["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [7]
    assert ps["in_flight"] is None


# ── a red-gated PR is not auto-re-enqueued until its head changes (F11) ─────

def test_red_gate_is_not_auto_re_enqueued_until_the_head_changes(tmp_path):
    """F11: dropping a red-gate PR must not make the next tick rerun it."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"],
                         gates=["sh -c 'echo BOOM >&2; exit 7'"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, card="t_red", assignee="worker",
                    path=_state(tmp_path))

    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run)
    assert first["processed"][0]["result"] == "red-gate", first
    comments = len(gh.comments)
    last = mq.load_state(_state(tmp_path))["projects"]["alpha"]["last"]
    assert last["result"] == "red-gate"
    assert last["head_sha"] == pair["head"]

    def source(cfg, gh_runner=None):
        return [{"pr": 7, "card": "t_red", "assignee": "worker"}]

    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     auto_source=source)
    assert second["processed"] == [], second
    assert len(gh.comments) == comments
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == []

    # The worker pushes a fix; only then may auto-enqueue pick it up again.
    _git(pair["repo"], "checkout", pair["branch"])
    (pair["repo"] / "fixed.txt").write_text("fixed\n")
    _git(pair["repo"], "add", ".")
    _git(pair["repo"], "commit", "-m", "fix the gate")
    _git(pair["repo"], "push", "origin", pair["branch"])
    new_head = _git(pair["repo"], "rev-parse", "HEAD")
    gh.heads[7]["headRefOid"] = new_head
    fixed_registry = _registry(tmp_path, pair["repo"], gates=["true"])

    third = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"),
                    registry_path=fixed_registry, gh_runner=gh,
                    git_runner=subprocess.run, auto_source=source)
    assert third["processed"][0]["result"] == "merged", third
    assert gh.merged == [7]


# ── a non-green head leaves the head slot for the wait list (F12) ──────────

def test_non_green_head_moves_to_the_wait_list_so_the_fifo_proceeds(tmp_path):
    """F12: one pending PR must not starve the entry behind it forever."""
    pair = _repo_pair(tmp_path)
    second_head = _new_branch(pair["repo"], "wt/t_second")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                "conclusion": None}]
    gh = FakeGh({
        7: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
        8: {"headRefName": "wt/t_second", "headRefOid": second_head},
    }, views={7: [{"statusCheckRollup": pending},
                  {"statusCheckRollup": pending},
                  {"statusCheckRollup": pending},
                  {"statusCheckRollup": pending}]})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    mq.add_to_queue("alpha", 8, path=_state(tmp_path))

    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run)
    assert first["processed"][0]["result"] == "waiting", first
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [8]
    assert [e["pr"] for e in ps["waiting"]] == [7]
    rows = mq.view(state_path=_state(tmp_path),
                   registry_path=registry)["rows"]
    assert [(r["pr"], r["state"]) for r in rows] == [(8, "queued"),
                                                     (7, "waiting")]

    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert second["processed"][0]["pr"] == 8, second
    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [8]

    # The wait-listed PR is NOT retried by the claim step. With the FIFO empty
    # nothing is claimed at all: `_drain_waiting` probed #7 on this same tick,
    # found its checks still pending, and left it in the wait list. Claiming it
    # anyway is exactly the #710 churn — `process_entry` would call it not green
    # and bounce it back, once per tick, forever.
    third = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run)
    assert third["processed"] == [], third
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and [e["pr"] for e in ps["waiting"]] == [7]


def test_a_pending_waiting_pr_is_not_claimed_again_every_tick(tmp_path):
    """A wait-listed PR whose checks are still running must be LEFT alone.

    Live churn, PR #710 on 2.17.5: 16 ticks in a row each logged
    `checks not green; moved to the wait list` then `-> waiting`. The claim step
    reached into `waiting` whenever the active FIFO was empty — with no
    readiness check of its own — so it popped an entry `_drain_waiting` had
    already probed and found not green, handed it to `process_entry`, and got it
    bounced straight back. The drain is the only door back into the FIFO, and it
    admits a green rollup and nothing else; three ticks after this PR is parked
    there must be zero claims, zero queue moves and zero log lines.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                "conclusion": None}]
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=pending)
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    logged = []
    run_tick = lambda: mq.tick(
        state_path=_state(tmp_path), root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        log=lambda msg, *a, **k: logged.append(msg))

    # Tick 1 is a legitimate claim: the entry was in the FIFO, its checks are
    # pending, so it goes to the wait list. That is the ONE move.
    first = run_tick()
    assert first["processed"][0]["pr"] == 7, first
    assert first["processed"][0]["result"] == "waiting", first
    parked = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in parked["waiting"]] == [7], parked

    # Ticks 2, 3 and 4: still pending, so nothing may be claimed at all. The
    # old code processed #7 again on every one of these.
    for tick_no in (2, 3, 4):
        del logged[:]
        result = run_tick()
        assert result["processed"] == [], (tick_no, result)
        assert not [line for line in logged if "moved to the wait list" in line], \
            (tick_no, logged)
        ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
        assert ps["queue"] == [] and ps["in_flight"] is None, (tick_no, ps)
        assert [e["pr"] for e in ps["waiting"]] == [7], (tick_no, ps)
        assert (ps["waiting"][0]["enqueued_at"]
                == parked["waiting"][0]["enqueued_at"]), (tick_no, ps)
    assert gh.merged == []


# ── generated_at is the ledger write time, not the read time (F14) ─────────

# ── the four "the stamp moved" cases, and what an absolute freeze does to them ──
#
# test_view_generated_at_is_the_last_state_write_not_the_read,
# test_tick_stamps_the_ledger_even_when_it_does_no_work,
# test_gate_heartbeat_advances_the_stream_while_the_gate_runs and
# test_check_merge_queue_stamps_the_stream_when_the_tick_does_no_work all assert that
# TWO SUCCESSIVE WRITES stamp differently. `save_state` stamps `generated_at` from
# `merge_queue._now_iso()`, so under an ABSOLUTE freeze (t_3b38aca4's t3b3_freeze,
# `--frozen <iso> --freeze-time`) every write in the process answers the same instant
# and the assertion is unsatisfiable by construction: measured red at frozen
# 2020-01-01 AND frozen 2200-01-01 with `assert '<instant>' != '<instant>'`.
#
# That both-directions shape is the signature of a clock that cannot ADVANCE, not of a
# two-clock window — a window defect flips in ONE direction only, when the fixture
# literal crosses it. The cross-check agrees: all four are green at the real clock and
# under the shipped relative harness at ±400 days and at the equivalent travel
# (HSCC_WALL_SHIFT_DAYS=±2518/+63225), where the clock keeps moving (t_0389093a).
# Nothing here goes red on a future date; do not "fix" it by pinning the stamps, which
# would delete the freshness promise these four cases exist to hold.


def test_view_generated_at_is_the_last_state_write_not_the_read(tmp_path):
    """F14: two reads with no tick/write between them share one timestamp."""
    state = _state(tmp_path)
    mq.add_to_queue("alpha", 7, path=state)
    first = mq.view(state_path=state,
                    registry_path=str(tmp_path / "none.yaml"))
    second = mq.view(state_path=state,
                     registry_path=str(tmp_path / "none.yaml"))
    doc = mq.load_state(state)

    assert first["generated_at"] == second["generated_at"]
    assert first["generated_at"] == doc["generated_at"]

    time.sleep(0.01)
    mq.add_to_queue("alpha", 8, path=state)
    third = mq.view(state_path=state,
                    registry_path=str(tmp_path / "none.yaml"))
    assert third["generated_at"] != first["generated_at"]


def test_tick_stamps_the_ledger_even_when_it_does_no_work(tmp_path):
    """F14: a live-but-idle daemon is distinguishable from a dead one."""
    state = _state(tmp_path)
    registry = _registry(tmp_path, tmp_path / "repo", enabled=False)
    first = mq.tick(state_path=state, registry_path=registry)
    stamp = first["generated_at"]

    time.sleep(0.01)
    second = mq.tick(state_path=state, registry_path=registry)
    assert second["processed"] == []
    assert second["generated_at"] != stamp
    assert mq.load_state(state)["generated_at"] == second["generated_at"]


# ── default-on eligibility (t_0546ef10) ────────────────────────────────────

def _github_mirror(pair):
    """Make ``origin`` look like github.com while git still resolves locally.

    Eligibility reads the RAW configured URL (``git config --get``); git's
    ``url.*.insteadOf`` rewrite keeps fetch/push inside ``tmp_path`` so no test
    touches the network.
    """
    local = str(pair["origin"])
    url = "https://github.com/example/project.git"
    _git(pair["repo"], "config", f"url.{local}.insteadOf", url)
    _git(pair["repo"], "remote", "set-url", "origin", url)


def _status(tmp_path, registry, name="alpha"):
    from hscc_daemon.merge_queue_cli import _status_doc
    return _status_doc(registry_path=registry,
                       state_path=_state(tmp_path))["projects"][name]


def test_default_on_for_a_github_checkout_with_declared_gates(tmp_path):
    """The default is on with only repo + github origin + gates declared."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=["true"])

    row = _status(tmp_path, registry)

    assert row["enabled"] is True
    assert row["enabled_source"] == "default"
    assert row["default_enabled"] is True
    assert row["default_reason"] is None
    assert mq.view(state_path=_state(tmp_path),
                   registry_path=registry)["projects"]["alpha"][
                       "enabled"] is True


def test_hscc_builtin_gates_make_it_eligible(tmp_path):
    """hscc needs no registry gates: its built-in gates count."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], name="hscc", enabled=None)

    row = _status(tmp_path, registry, "hscc")

    assert row["enabled"] is True
    assert row["default_reason"] is None


def test_hscc_explicit_empty_gates_disables_the_builtins(tmp_path):
    """`gates: []` is an explicit no-gates choice, even for hscc."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], name="hscc", enabled=None,
                         gates=[])

    row = _status(tmp_path, registry, "hscc")

    assert row["enabled"] is False
    assert row["default_reason"] == "no gates"


def test_default_off_without_a_repo(tmp_path):
    """A registry path that does not exist is 'no repo'."""
    registry = _registry(tmp_path, tmp_path / "missing", enabled=None,
                         gates=["true"])

    row = _status(tmp_path, registry)

    assert row["enabled"] is False
    assert row["enabled_source"] == "default"
    assert row["default_reason"] == "no repo"


def test_default_off_when_the_path_is_not_a_git_checkout(tmp_path):
    """A directory that is not a work-tree root is 'no repo'."""
    plain = tmp_path / "plain"
    plain.mkdir()
    registry = _registry(tmp_path, plain, enabled=None, gates=["true"])

    row = _status(tmp_path, registry)

    assert row["enabled"] is False
    assert row["default_reason"] == "no repo"


@pytest.mark.parametrize("remote", ["local-path", "none"])
def test_default_off_without_a_github_remote(tmp_path, remote):
    """A git checkout whose origin is not github.com is 'no GitHub remote'."""
    pair = _repo_pair(tmp_path)
    if remote == "none":
        _git(pair["repo"], "remote", "remove", "origin")
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=["true"])

    row = _status(tmp_path, registry)

    assert row["enabled"] is False
    assert row["enabled_source"] == "default"
    assert row["default_reason"] == "no GitHub remote"


@pytest.mark.parametrize("gates", [None, []])
def test_default_off_without_gates(tmp_path, gates):
    """A github checkout with no (or empty) gates stays off."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=gates)

    row = _status(tmp_path, registry)

    assert row["enabled"] is False
    assert row["default_reason"] == "no gates"


def test_explicit_disable_beats_default_on_and_persists(tmp_path):
    """A CLI disable wins over the default and survives a fresh ledger read."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=["true"])
    state = _state(tmp_path)
    assert _status(tmp_path, registry)["enabled"] is True

    mq.set_enabled("alpha", False, path=state)

    row = _status(tmp_path, registry)
    assert row["enabled"] is False
    assert row["enabled_source"] == "cli"
    assert row["default_enabled"] is True   # the default still applies...
    assert mq.load_state(state)["projects"]["alpha"]["enabled"] is False
    assert mq.view(state_path=state, registry_path=registry)[
        "projects"]["alpha"]["enabled"] is False


def test_explicit_enable_beats_missing_gates(tmp_path):
    """The operator can still opt in a project the default declines."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=None)
    state = _state(tmp_path)
    assert _status(tmp_path, registry)["default_reason"] == "no gates"

    mq.set_enabled("alpha", True, path=state)

    row = _status(tmp_path, registry)
    assert row["enabled"] is True
    assert row["enabled_source"] == "cli"


def test_registry_enabled_false_still_forces_off(tmp_path):
    """An explicit registry ``enabled: false`` is not overridden by the default."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=False, gates=["true"])

    row = _status(tmp_path, registry)

    assert row["enabled"] is False
    assert row["enabled_source"] == "registry"


def test_registry_enabled_true_still_forces_on_without_gates(tmp_path):
    """The old explicit opt-in keeps working for a project with no gates."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=True, gates=None)

    row = _status(tmp_path, registry)

    assert row["enabled"] is True
    assert row["enabled_source"] == "registry"


def test_status_prints_why_a_default_project_stays_off(tmp_path, monkeypatch,
                                                      capsys):
    """`hscc merge-queue status` names the failed condition in its line."""
    from hscc_daemon import merge_queue_cli
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=None)
    monkeypatch.setenv("FLIGHTDECK_REGISTRY", registry)
    monkeypatch.setenv("HSCC_HOME", str(tmp_path / "hscc"))

    assert merge_queue_cli.cmd_merge_queue(["status"]) == 0

    out = capsys.readouterr().out
    assert "alpha: disabled (no gates)" in out


def test_help_documents_default_on_and_the_three_reasons(capsys):
    """The --help text is the operator contract for the default."""
    from hscc_daemon import merge_queue_cli

    assert merge_queue_cli.cmd_merge_queue(["--help"]) == 0

    out = capsys.readouterr().out
    assert "ON BY DEFAULT" in out
    assert "no repo" in out and "no GitHub remote" in out
    assert "no gates" in out


def test_tick_processes_a_project_enabled_by_the_default(tmp_path):
    """No registry ``enabled`` flag: eligibility alone drives a real tick."""
    pair = _repo_pair(tmp_path)
    _github_mirror(pair)
    registry = _registry(tmp_path, pair["repo"], enabled=None, gates=["true"])
    gh = FakeGh({11: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 11, card="t_default", path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [11]
    assert result["projects"]["alpha"]["enabled"] is True
    assert result["projects"]["alpha"]["enabled_source"] == "default"


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo.git",
    "http://github.com/owner/repo",
    "git@github.com:owner/repo.git",
    "ssh://git@github.com/owner/repo.git",
    "git://github.com/owner/repo.git",
    "https://GitHub.com/owner/repo.git",
])
def test_github_remote_recognises_the_usual_url_forms(url):
    assert mq._github_remote(url)


@pytest.mark.parametrize("url", [
    "",
    None,
    "/tmp/origin.git",
    "https://gitlab.com/owner/repo.git",
    "git@bitbucket.org:owner/repo.git",
])
def test_github_remote_rejects_non_github_urls(url):
    assert not mq._github_remote(url)


# ── liveness stamping while a gate runs (t_444c32c9) ───────────────────────
#
# ``check_merge_queue`` is a 60 s daemon stream and ``hscc verify`` calls
# ``state/merge_queue.json`` stale after 600 s, while ONE gate leg may run for
# the whole 1800 s ``DEFAULT_GATE_TIMEOUT_S``. That made a demonstrably live
# runner read as dead on main (measured: stamp 31 min past the budget, the gate
# a live child of the daemon pid). These tests pin the fix from the OUTSIDE:
# what the stream FILE says while the work runs, and what stops saying it once
# the work stops. None of them reach into verify — the fix is on the stamping
# side, so the judging side stays exactly as strict.

def _stream_file(tmp_path):
    """``state/merge_queue.json`` under the redirected STATE_DIR."""
    return tmp_path / "state" / f"{mq.STREAM_NAME}.json"


def _read_stream(tmp_path):
    """The current stream stamp, or None when nothing has been written."""
    path = _stream_file(tmp_path)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _require_stream(tmp_path):
    """The stream stamp, failing the test when the stream is dark."""
    stamp = _read_stream(tmp_path)
    assert stamp is not None, f"{_stream_file(tmp_path)} was never written"
    return stamp


def _heartbeat_stamps(seen):
    """Only the stamps the heartbeat wrote — a stage stamp carries gate=None."""
    return [s for _ts, s in seen if (s.get("in_flight") or {}).get("gate")]


def _live_heartbeats():
    return [t for t in threading.enumerate()
            if t.name.startswith(mq._LIVENESS_THREAD_PREFIX)]


@pytest.fixture(autouse=True)
def _redirect_stream_state_dir(tmp_path, monkeypatch):
    """Keep EVERY stream write inside ``tmp_path`` for this whole module.

    ``tick()`` now stamps the stream from INSIDE the run — the claim, each stage
    transition, each gate heartbeat — and ``STATE_DIR`` is an import-time
    constant in ``hscc_daemon.state``. Unredirected, the ~40 ticks in this file
    would stamp the OPERATOR's live ``~/.hscc/state``, where a test's invented PR
    number would read as a real in-flight merge.
    """
    from hscc_daemon import state as daemon_state
    monkeypatch.setattr(daemon_state, "STATE_DIR", str(tmp_path / "state"))


def _isolated_hscc_root(tmp_path, monkeypatch):
    """Point the queue at a scratch state root with no registry to tick.

    ``check_merge_queue()`` takes no arguments — it resolves the real ledger
    path and the real registry itself — so an isolated run needs both pins.
    """
    monkeypatch.setenv("HSCC_HOME", str(tmp_path / "hscc-home"))
    monkeypatch.setenv("FLIGHTDECK_REGISTRY",
                       str(tmp_path / "no-registry.yaml"))


def test_gate_heartbeat_advances_the_stream_while_the_gate_runs(tmp_path,
                                                                monkeypatch):
    """The stream stamp must advance DURING a gate leg, not only after it.

    The fake leg blocks until the heartbeat has stamped twice (capped, so a
    missing heartbeat FAILS rather than hangs) and samples the stream from
    INSIDE the leg — so every stamp collected here was, by construction, read
    while the gate was in flight. ``LIVENESS_STAMP_S`` is retuned to 0.5 s to
    keep it fast; the shape is the measured one (a leg far longer than the
    stream's allowed silence).
    """
    pair = _repo_pair(tmp_path)
    gate_cmd = "bash scripts/run_tests.sh"
    registry = _registry(tmp_path, pair["repo"], gates=[gate_cmd])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))

    interval = 0.5
    monkeypatch.setattr(mq, "LIVENESS_STAMP_S", interval)

    seen = []          # (timestamp, stamp) per DISTINCT stamp read in-leg
    leg = {}

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        leg["start"] = time.monotonic()
        deadline = leg["start"] + 10.0
        while time.monotonic() < deadline:
            stamp = _read_stream(tmp_path)
            ts = stamp.get("timestamp") if stamp else None
            if ts and (not seen or seen[-1][0] != ts):
                seen.append((ts, stamp))
            if len(_heartbeat_stamps(seen)) >= 2:
                break
            time.sleep(0.02)
        leg["end"] = time.monotonic()
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    beats = _heartbeat_stamps(seen)
    # (1) the stamp ADVANCED while the gate ran (verify reads this same
    #     ``timestamp`` field, not the file mtime — verify.py's recency check).
    assert len({s["timestamp"] for s in beats}) >= 2, (
        f"stream dark through the whole leg; saw {[t for t, _ in seen]!r}")
    # (2) and every heartbeat says WHICH gate is running, on which claim.
    assert beats, "no heartbeat ever named the running gate"
    for stamp in beats:
        row = stamp["in_flight"]
        assert row["gate"] == gate_cmd
        assert row["stage"] == "gating"
        assert row["project"] == "alpha" and row["pr"] == 7
        assert row["elapsed_s"] >= 0
        assert stamp["ok"] is True
    # (3) the cadence is the configured interval, not "two stamps at once": the
    #     leg could not end before the second beat existed, and a beat's own
    #     elapsed_s is measured from the leg's start.
    assert leg["end"] - leg["start"] >= interval * 0.9, leg
    assert max(s["in_flight"]["elapsed_s"] for s in beats) >= interval * 0.9
    # (4) nothing outlives the tick.
    assert _live_heartbeats() == []


def test_heartbeat_stops_the_moment_the_gate_leg_ends(tmp_path, monkeypatch):
    """A beat that outlived its leg would report a DEAD tick as alive — the
    exact inversion of the defect this fixes, so the stop is asserted and not
    assumed. The thread is joined in __exit__, so the stamping stops with it.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 9, path=_state(tmp_path))

    interval = 0.2
    monkeypatch.setattr(mq, "LIVENESS_STAMP_S", interval)
    # Stub matches the real `_run_gate` signature, `cancel_check` and `on_start`
    # included: a stub that silently absorbed `**kwargs` would keep passing if a
    # parameter were renamed, and the real caller would then be the only thing
    # that knows.
    monkeypatch.setattr(mq, "_run_gate",
                        lambda c, w, t, cancel_check=None,
                        on_start=None: ("pass", ""))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert result["processed"][0]["result"] == "merged", result

    last = _read_stream(tmp_path)
    assert last is not None
    time.sleep(interval * 5)          # 5 full intervals of silence, if it stopped
    assert _read_stream(tmp_path) == last, "heartbeat still stamping after tick()"
    assert _live_heartbeats() == []


def test_stream_mirrors_every_stage_transition_the_ledger_records(tmp_path,
                                                                  monkeypatch):
    """One writer, one truth: the stream's ``in_flight`` row follows the LEDGER's
    ``stage`` at the same moment — from the claim through rebase, gate, merge.

    Without this the stream advances only when ``tick()`` returns, minutes
    behind the stage the queue is actually in.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({11: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 11, path=_state(tmp_path))

    mirrored = []
    real_stamp = mq._stream_stamp

    def spy(**fields):
        mirrored.append(fields)
        return real_stamp(**fields)

    monkeypatch.setattr(mq, "_stream_stamp", spy)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert result["processed"][0]["result"] == "merged", result

    stages = [f["stage"] for f in mirrored]
    # The heartbeat repeats "gating" once per interval, so collapse repeats.
    sequence = [s for i, s in enumerate(stages)
                if i == 0 or s != stages[i - 1]]
    assert sequence == ["claimed", "rebasing", "gating", "merging"], sequence
    assert all(f["project"] == "alpha" and f["pr"] == 11 for f in mirrored)
    # The claim is stamped before any work, and names no gate.
    assert mirrored[0]["stage"] == "claimed"
    assert mirrored[0].get("gate") is None
    assert mirrored[0].get("elapsed_s") is None
    # Readers get a STABLE row: all five keys are always present, not only when
    # a gate happens to be running.
    row = _require_stream(tmp_path)["in_flight"]
    assert set(row) == {"project", "pr", "stage", "gate", "elapsed_s"}
    assert row["stage"] == "merging" and row["gate"] is None


def test_check_merge_queue_stamps_the_stream_when_the_tick_does_no_work(
        tmp_path, monkeypatch):
    """Stream twin of test_tick_stamps_the_ledger_even_when_it_does_no_work.

    A dark stream is indistinguishable from a dead daemon, so an idle tick still
    stamps — and stamps WITHOUT an in_flight row, because nothing is in flight:
    the tick's real verdict replaces the in-run mirrors when it returns.
    """
    _isolated_hscc_root(tmp_path, monkeypatch)

    first = mq.check_merge_queue()
    assert first["ok"] is True, first
    stamp = _read_stream(tmp_path)
    assert stamp is not None, "an idle tick left the stream dark"
    assert stamp["stream"] == mq.STREAM_NAME
    assert stamp["ok"] is True
    assert "in_flight" not in stamp, "a finished tick must not leave a claim row"

    time.sleep(0.01)
    second = mq.check_merge_queue()
    assert second["generated_at"] != first["generated_at"]
    again = _read_stream(tmp_path)
    assert again is not None
    assert again["timestamp"] != stamp["timestamp"]


def test_check_merge_queue_never_raises_when_write_state_fails(tmp_path,
                                                               monkeypatch):
    """A health-reporting write must never fail the health check itself."""
    from hscc_daemon import state as daemon_state
    _isolated_hscc_root(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(daemon_state, "write_state", boom)
    result = mq.check_merge_queue()          # must not raise into the stream
    assert result["ok"] is True, result
    assert not _stream_file(tmp_path).exists()


def test_a_failing_stream_write_never_breaks_the_gate_run(tmp_path, monkeypatch):
    """The in-run mirrors carry the SAME contract as the final stamp.

    A read-only filesystem or a full disk must not lose the merge: if the stage
    mirror or the heartbeat let an OSError escape, ``process_entry`` aborts and
    the entry is retried instead of merged — which is what this pins.
    """
    from hscc_daemon import state as daemon_state
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({13: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 13, path=_state(tmp_path))

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(daemon_state, "write_state", boom)
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [13]
    assert not _stream_file(tmp_path).exists()


# ── merge-queue livelock (t_ec0821ed) ──────────────────────────────────────
#
# Right after the base moves, GitHub answers ``mergeable: UNKNOWN`` for every
# open PR until it recomputes. The queue treated that as "not ready" and parked
# the entry in `waiting`, which used to drain only via auto-enqueue on an EMPTY
# queue — so a non-empty queue never drained it. The same trap swallowed every
# pushed PR whose CI was still running. These cases pin the three remedies:
# bounded re-poll, every-tick wait-list drain, and a held `ci-pending` claim.

def test_mergeable_unknown_is_repolled_then_merged(tmp_path, monkeypatch):
    """UNKNOWN is "not decided yet": re-poll, do not park the PR."""
    monkeypatch.setattr(mq, "MERGEABLE_POLL_DELAY_S", 0.0)
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh(
        {17: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={17: [{"mergeable": "UNKNOWN"},
                    {"mergeable": "UNKNOWN"}]})
    mq.add_to_queue("alpha", 17, path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [17]
    views = [argv for argv in gh.calls if argv[:3] == ["gh", "pr", "view"]]
    assert len(views) >= mq.MERGEABLE_POLL_ATTEMPTS, gh.calls
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == []


def test_still_unknown_after_the_polls_keeps_the_head_slot(tmp_path,
                                                           monkeypatch):
    """A still-UNKNOWN PR stays at the FIFO head, and the next tick merges it."""
    monkeypatch.setattr(mq, "MERGEABLE_POLL_DELAY_S", 0.0)
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    unknown = {"mergeable": "UNKNOWN"}
    gh = FakeGh(
        {18: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={18: [unknown, unknown, unknown]})
    mq.add_to_queue("alpha", 18, path=_state(tmp_path))

    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run)

    assert first["processed"][0]["result"] == "mergeable-unknown", first
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [18]
    assert ps["waiting"] == [] and ps["in_flight"] is None
    assert [r["state"] for r in mq.view(
        state_path=_state(tmp_path), registry_path=registry)["rows"]] == [
            "queued"]

    # GitHub has recomputed by the next tick: the same entry proceeds.
    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [18]


def test_waiting_green_returns_to_the_fifo_while_the_queue_is_not_empty(
        tmp_path):
    """The waiting drain must not wait for an empty FIFO (the livelock)."""
    pair = _repo_pair(tmp_path)
    second_head = _new_branch(pair["repo"], "wt/t_second")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({
        7: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
        8: {"headRefName": "wt/t_second", "headRefOid": second_head},
    })
    # PR 8 was enqueued first, then parked in waiting; PR 7 now occupies the
    # active FIFO, so auto-enqueue (which requires an EMPTY FIFO) never runs.
    mq.add_to_queue("alpha", 8, path=_state(tmp_path))
    doc = mq.load_state(_state(tmp_path))
    ps = mq._project_state(doc, "alpha")
    ps["waiting"].append(ps["queue"].pop())
    mq.save_state(doc, _state(tmp_path))
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))

    logged = []
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     log=lambda msg, *a, **k: logged.append(msg))

    # The older waiting PR came back and was claimed ahead of the queued PR.
    assert result["processed"][0]["pr"] == 8, result
    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [8]
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [7]
    assert ps["waiting"] == []
    assert any("returned green waiting" in line for line in logged), logged


def test_ci_pending_is_rechecked_in_place_without_rerunning_the_gates(
        tmp_path, monkeypatch):
    """The held claim is a CI wait, not a reason to rebase and gate again."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                "conclusion": None}]
    gh = FakeGh(
        {19: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={19: [{}, {"statusCheckRollup": pending}]})
    mq.add_to_queue("alpha", 19, path=_state(tmp_path))
    gate_calls = []

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        gate_calls.append(command)
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)

    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run)
    assert first["processed"][0]["result"] == "ci-pending", first
    assert len(gate_calls) == 1
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"]["stage"] == "ci-pending"

    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [19]
    assert len(gate_calls) == 1, "the pushed head was re-gated on resume"


def test_post_push_unknown_mergeable_keeps_the_claim_not_waiting(
        tmp_path, monkeypatch):
    """Green checks + still-UNKNOWN mergeable after a push is not "ready" yet."""
    monkeypatch.setattr(mq, "MERGEABLE_POLL_DELAY_S", 0.0)
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    unknown = {"mergeable": "UNKNOWN"}
    gh = FakeGh(
        {22: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={22: [{}, unknown, unknown, unknown]})
    mq.add_to_queue("alpha", 22, path=_state(tmp_path))

    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run)

    assert first["processed"][0]["result"] == "ci-pending", first
    assert gh.merged == []
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"]["stage"] == "ci-pending"
    assert ps["waiting"] == []

    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)
    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [22]


def test_ci_red_on_the_pushed_head_hands_the_card_back(tmp_path):
    """A red CI is a real verdict, unlike a pending one: resolve, not wait."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    red = [{"__typename": "CheckRun", "status": "COMPLETED",
            "conclusion": "FAILURE"}]
    gh = FakeGh(
        {20: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={20: [{}, {"statusCheckRollup": red}]})
    mq.add_to_queue("alpha", 20, card="t_ci_red", assignee="worker",
                    path=_state(tmp_path))
    handed = []

    def handback(card, assignee, body):
        handed.append((card, assignee, body))
        return True

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     handback=handback)

    assert result["processed"][0]["result"] == "red-ci", result
    assert gh.merged == []
    assert handed and handed[0][0] == "t_ci_red"
    assert gh.comments and "CI is red" in gh.comments[0][-1]
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"] is None and ps["waiting"] == []
    assert ps["last"]["result"] == "red-ci"
    assert [r["state"] for r in mq.view(
        state_path=_state(tmp_path), registry_path=registry)["rows"]] == [
            "resolve"]


def test_ci_pending_timeout_releases_to_the_rechecked_wait_list(tmp_path):
    """A CI that never answers is not terminal: the wait list is rechecked."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                "conclusion": None}]
    gh = FakeGh(
        {21: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={21: [{"statusCheckRollup": pending}]})
    state = _state(tmp_path)
    doc = mq.load_state(state)
    ps = mq._project_state(doc, "alpha")
    ps["in_flight"] = {
        "pr": 21, "card": "t_ci_timeout", "assignee": "worker",
        "branch": pair["branch"], "stage": "ci-pending",
        "pushed_sha": pair["head"], "ci_base": "main",
        "ci_since": "2020-01-01T00:00:00+00:00",
        "enqueued_at": "2020-01-01T00:00:00+00:00",
    }
    mq.save_state(doc, state)

    result = mq.tick(state_path=state,
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert result["processed"][0]["result"] == "ci-timeout", result
    assert gh.merged == []
    ps = mq.load_state(state)["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert [e["pr"] for e in ps["waiting"]] == [21]
    assert mq.outcome_state("ci-timeout") == "waiting"


def test_ci_pending_budget_defaults_and_is_the_projects_to_set():
    """The CI-pending budget is 3600 s unless a valid registry line moves it."""
    assert mq.CI_PENDING_TIMEOUT_S == 3600
    for cfg, want in (({}, 3600), (None, 3600),
                      ({"ci_pending_timeout_s": 900}, 900),
                      ({"ci_pending_timeout_s": "900"}, 900),
                      ({"ci_pending_timeout_s": 900.9}, 900),
                      ({"ci_pending_timeout_s": 0}, 3600),
                      ({"ci_pending_timeout_s": -1}, 3600),
                      ({"ci_pending_timeout_s": "30m"}, 3600),
                      ({"ci_pending_timeout_s": ""}, 3600),
                      ({"ci_pending_timeout_s": True}, 3600),
                      ({"ci_pending_timeout_s": None}, 3600),
                      ({"ci_pending_timeout_s": []}, 3600)):
        assert mq.ci_pending_budget_s(cfg) == want, cfg

    # The expiry predicate reads the budget it is handed, not the constant.
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                        time.gmtime(time.time() - 120))
    assert mq._ci_pending_expired(old, 60)
    assert not mq._ci_pending_expired(old, 3600)

    warned = []

    def log(msg, level="INFO"):
        warned.append((msg, level))

    assert mq.ci_pending_budget_s(
        {"name": "alpha", "ci_pending_timeout_s": "30m"}, log=log) == 3600
    assert warned and warned[0][1] == "WARN"
    assert "ignoring invalid merge_queue.ci_pending_timeout_s='30m'" in \
        warned[0][0]
    warned.clear()
    assert mq.ci_pending_budget_s({"ci_pending_timeout_s": 60}, log=log) == 60
    assert warned == []          # a good value is silent


def test_ci_pending_expiry_uses_the_registry_budget_and_names_a_backlog(tmp_path):
    """A 60 s override expires where 120 s of pending CI would otherwise hold."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"],
                         ci_pending_timeout_s=60)
    pending = [{"__typename": "CheckRun", "status": "QUEUED",
                "conclusion": None}]
    gh = FakeGh(
        {21: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
        views={21: [{"statusCheckRollup": pending}]})
    state = _state(tmp_path)
    doc = mq.load_state(state)
    ps = mq._project_state(doc, "alpha")
    ps["in_flight"] = {
        "pr": 21, "card": "t_ci_timeout", "assignee": "worker",
        "branch": pair["branch"], "stage": "ci-pending",
        "pushed_sha": pair["head"], "ci_base": "main",
        "ci_since": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                  time.gmtime(time.time() - 120)),
        "enqueued_at": "2020-01-01T00:00:00+00:00",
    }
    mq.save_state(doc, state)
    lines = []

    result = mq.tick(state_path=state,
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     log=lambda msg, *a, **k: lines.append(msg))

    assert result["processed"][0]["result"] == "ci-timeout", result
    assert result["processed"][0]["timeout_s"] == 60
    assert gh.merged == []
    assert any("CI still running after 60s (runner backlog)" in line
               for line in lines), lines
    ps = mq.load_state(state)["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert [e["pr"] for e in ps["waiting"]] == [21]


def test_checks_verdict_separates_pending_from_red():
    green = [{"__typename": "CheckRun", "status": "COMPLETED",
              "conclusion": "SUCCESS"}]
    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                "conclusion": None}]
    red = [{"__typename": "CheckRun", "status": "COMPLETED",
            "conclusion": "FAILURE"}]
    assert mq._checks_verdict({"statusCheckRollup": green}) == "green"
    assert mq._checks_verdict({"statusCheckRollup": pending}) == "pending"
    assert mq._checks_verdict({"statusCheckRollup": red}) == "red"
    # Empty is green only when GitHub positively said there are no required
    # checks; otherwise nothing has reported yet.
    assert mq._checks_verdict({"statusCheckRollup": []},
                              required_checks=False) == "green"
    assert mq._checks_verdict({"statusCheckRollup": []},
                              required_checks=True) == "pending"
    assert mq._checks_verdict({"statusCheckRollup": []},
                              required_checks=None) == "pending"


def test_daemon_entry_point_logs_merge_queue_outcomes(tmp_path, monkeypatch):
    """The daemon tick must log through ~/.hscc (or $HSCC_HOME)/daemon.log."""
    _isolated_hscc_root(tmp_path, monkeypatch)
    seen = {}

    def fake_tick(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "generated_at": "2026-01-01T00:00:00+00:00"}

    monkeypatch.setattr(mq, "tick", fake_tick)
    mq.check_merge_queue()
    assert seen["log"] is mq._daemon_log

    mq._daemon_log("merge-queue test: PR #1 -> merged")
    log_path = tmp_path / "hscc-home" / "daemon.log"
    assert "merge-queue test: PR #1 -> merged" in log_path.read_text()


# ── t_8c461597: a declined claim must say why ──────────────────────────────
#
# Measured 2026-11-23: PR #716 sat unclaimed ~65 min with the queue enabled,
# unpaused and nothing in flight, and the daemon log carried no line that
# explained it.  These tests pin the three silences that made that possible and
# the one state hazard (`remove` while the entry's gate is still running) that
# made the ledger read inconsistently while it happened.

def _logger():
    """A ``log=`` sink in the shape tick() calls it: ``log(msg, level)``."""
    lines = []

    def log(msg, *args, **kwargs):
        level = args[0] if args else kwargs.get("level", "INFO")
        lines.append(f"[{level}] {msg}")

    return lines, log


def test_tick_names_the_pr_it_declined_when_the_registry_row_is_gone(tmp_path):
    """A queued FIFO the tick will not touch must name the PR and the reason."""
    pair = _repo_pair(tmp_path)
    # alpha exists only in the ledger: the registry no longer has a row for it,
    # so `tick()` skips its whole repo group. Before t_8c461597 that skip was
    # silent — ~60 ticks, zero lines, PR #7 still sitting there.
    registry = _registry(tmp_path, pair["repo"], name="gamma", gates=["true"])
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    lines, log = _logger()

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=_no_runner, git_runner=_no_runner,
                     auto_source=lambda *a, **k: [], log=log)

    assert result["processed"] == [], result
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [7] and ps["in_flight"] is None
    reasons = [ln for ln in lines if "not claimed" in ln]
    assert any("PR #7" in ln for ln in reasons), lines
    assert any("config" in ln.lower() or "registry" in ln.lower()
               for ln in reasons), reasons


def test_tick_names_the_pr_waiting_behind_a_ci_pending_claim(tmp_path,
                                                             monkeypatch):
    """One claim per repo: the FIFO behind it must be told what holds the repo."""
    pair = _repo_pair(tmp_path)
    registry = _registry_multi(tmp_path, [("alpha", pair["repo"]),
                                          ("beta", pair["repo"])])
    second = _new_branch(pair["repo"], "wt/t_second")
    pending = [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                "conclusion": None}]
    gh = FakeGh({19: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]},
                 20: {"headRefName": "wt/t_second", "headRefOid": second}},
                views={19: [{}, pending, pending, pending, pending]})
    monkeypatch.setattr(mq, "_run_gate",
                        lambda command, worktree, timeout_s, cancel_check=None, **kw: ("pass", ""))

    lines, log = _logger()
    mq.add_to_queue("alpha", 19, path=_state(tmp_path))
    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run,
                    auto_source=lambda *a, **k: [], log=log)
    assert first["processed"], (first, lines)
    assert first["processed"][0]["result"] == "ci-pending", first
    assert (mq.load_state(_state(tmp_path))["projects"]["alpha"]
            ["in_flight"]["stage"] == "ci-pending")

    mq.add_to_queue("beta", 20, path=_state(tmp_path))
    second_tick = mq.tick(state_path=_state(tmp_path),
                          root=str(tmp_path / "queue"), registry_path=registry,
                          gh_runner=gh, git_runner=subprocess.run,
                          auto_source=lambda *a, **k: [], log=log)

    assert [e["pr"] for e in second_tick["projects"]["beta"]["queue"]] == [20]
    reasons = [ln for ln in lines if "not claimed" in ln]
    assert any("PR #20" in ln for ln in reasons), lines
    assert any("19" in ln for ln in reasons), reasons


def test_a_raced_pause_is_reported_against_the_pr_it_left_queued(tmp_path,
                                                                monkeypatch):
    """A pause that lands mid-tick must not decline the head entry silently."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))

    def pause_during_discovery(project, cfg, **kwargs):
        mq.set_paused("alpha", True, path=_state(tmp_path))
        return []

    monkeypatch.setattr(mq, "_drain_waiting", pause_during_discovery)
    lines, log = _logger()

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=_no_runner, git_runner=_no_runner,
                     auto_source=lambda *a, **k: [], log=log)

    assert result["processed"] == [], result
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [7] and ps["in_flight"] is None
    reasons = [ln for ln in lines if "not claimed" in ln]
    assert any("PR #7" in ln and "paus" in ln.lower() for ln in reasons), lines


def test_gate_leg_announces_itself_before_it_holds_the_queue(tmp_path,
                                                             monkeypatch):
    """The hour a gate runs is the hour no other PR can be claimed.

    The measured 65-minute silence was one 3600 s gate leg holding the single
    merge_queue thread; the log's first line about it was the timeout at the
    END.  The leg must name itself (PR, command, budget) before it starts.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"], timeout_s=120)
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, path=_state(tmp_path))
    lines, log = _logger()
    at_gate_start = []

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        at_gate_start.append(list(lines))
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     auto_source=lambda *a, **k: [], log=log)

    assert result["processed"][0]["result"] == "merged", result
    assert at_gate_start, "the gate never ran"
    assert any("gate start" in ln and "PR #7" in ln for ln in at_gate_start[0]), \
        at_gate_start[0]
    start = [ln for ln in at_gate_start[0] if "gate start" in ln][0]
    assert "120" in start, start


def test_a_tick_that_dies_before_claiming_is_logged(tmp_path, monkeypatch):
    """check_merge_queue() must not swallow a tick into the state stamp only."""
    _isolated_hscc_root(tmp_path, monkeypatch)

    def explode(**kwargs):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(mq, "tick", explode)
    result = mq.check_merge_queue()

    assert result["ok"] is False, result
    text = (tmp_path / "hscc-home" / "daemon.log").read_text()
    assert "RuntimeError" in text and "registry exploded" in text, text
    assert "[ERROR]" in text, text


def test_remove_during_a_gate_leaves_no_verdict_for_a_pr_that_is_gone(
        tmp_path, monkeypatch):
    """FIFO and `last` may not disagree about a claim the operator removed.

    The measured trigger: `hscc merge-queue remove hscc 696` while #696 was
    in flight with a gate running. `remove_from_queue` cleared the claim (that
    is why the stalled queue read `in_flight: null`), the gate then settled
    afterwards and stamped `-> red-gate` for a PR no list held any more.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})
    mq.add_to_queue("alpha", 7, card="t_removed", path=_state(tmp_path))

    def gate(command, worktree, timeout_s, cancel_check=None, **kw):
        mq.remove_from_queue("alpha", 7, path=_state(tmp_path))
        return "fail", "FAILED hscc_daemon/tests/test_x.py::test_y - boom"

    monkeypatch.setattr(mq, "_run_gate", gate)
    lines, log = _logger()

    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     auto_source=lambda *a, **k: [], log=log)

    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert result["processed"][0]["pr"] == 7, result
    assert ps["queue"] == [] and ps["waiting"] == [] and ps["in_flight"] is None
    # The verdict belongs to a run the operator cancelled: it must not be
    # recorded against a PR the FIFO no longer holds.
    assert not (ps["last"] and ps["last"].get("pr") == 7
                and ps["last"].get("result") == "red-gate"), ps["last"]
    assert any("PR #7" in ln and "dropped" in ln and "removed" in ln
               for ln in lines), lines
# ── production-shaped runners (t_499b6919) ────────────────────────────────

def _parked_ci_claim(tmp_path, pr, pair, *, waiting=False):
    """A claim parked exactly as the daemon writes it after a push."""
    state = _state(tmp_path)
    mq.add_to_queue("alpha", pr, path=state)
    doc = mq.load_state(state)
    ps = mq._project_state(doc, "alpha")
    entry = ps["queue"].pop()
    entry.update({
        "branch": pair["branch"], "stage": "ci-pending",
        "pushed_sha": pair["head"], "ci_base": "main",
        "ci_since": mq._now_iso(),
    })
    if waiting:
        ps["waiting"].append(entry)
    else:
        ps["in_flight"] = entry
    mq.save_state(doc, state)
    return state, entry


def test_resume_ci_pending_defaults_the_production_runners(tmp_path,
                                                          monkeypatch):
    """The daemon calls `_resume_ci_pending` with no runners; it must default."""
    pair = _repo_pair(tmp_path)
    state, entry = _parked_ci_claim(tmp_path, 23, pair)
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0, stdout='{"mergeCommit": {"oid": "squash-23"}}',
            stderr="")

    monkeypatch.setattr(mq, "_default_runner", runner)
    monkeypatch.setattr(mq, "pr_view_settled", lambda repo, pr, runner=None: {
        "state": "OPEN", "headRefName": pair["branch"],
        "headRefOid": pair["head"], "mergeable": "MERGEABLE",
        "isDraft": False,
        "statusCheckRollup": [{"__typename": "CheckRun",
                               "status": "COMPLETED",
                               "conclusion": "SUCCESS"}],
    })
    cfg = {"repo": str(pair["repo"]), "enabled_declared": True,
           "enabled": True}

    result = mq._resume_ci_pending("alpha", cfg, entry, state_path=state)

    assert result["result"] == "merged", result
    assert ["gh", "pr", "merge", "23", "--squash", "--delete-branch",
            "--match-head-commit", pair["head"]] in calls, calls


def test_parked_ci_pending_entry_is_drained_and_merged(tmp_path):
    """A `waiting` entry with stale ci-pending fields is not stranded."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh(
        {24: {"headRefName": pair["branch"], "headRefOid": pair["head"]}})
    state, _ = _parked_ci_claim(tmp_path, 24, pair, waiting=True)
    doc = mq.load_state(state)
    entry = mq._project_state(doc, "alpha")["waiting"][0]
    entry.update({"attempts": 1,
                  "last_error": "'NoneType' object is not callable"})
    mq.save_state(doc, state)

    result = mq.tick(state_path=state,
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run)

    assert [row["pr"] for row in result["processed"]] == [24], result
    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [24]
    ps = mq.load_state(state)["projects"]["alpha"]
    assert ps["waiting"] == [] and ps["queue"] == []


def test_a_raising_resume_keeps_its_traceback_in_last_error(tmp_path,
                                                            monkeypatch):
    """A crash on the ci-pending path must leave a traceback, not just str()."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    state, _ = _parked_ci_claim(tmp_path, 25, pair)
    gh = FakeGh({25: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    logged = []

    def boom(project, cfg, entry, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mq, "_resume_ci_pending", boom)
    result = mq.tick(
        state_path=state, root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        log=lambda msg, level="INFO", **kwargs: logged.append((msg, level)))

    assert result["processed"][0]["result"] == "retry", result
    ps = mq.load_state(state)["projects"]["alpha"]
    error = ps["queue"][0]["last_error"]
    assert isinstance(error, str)
    assert error.splitlines()[0] == "RuntimeError: kaboom"
    assert "Traceback (most recent call last)" in error
    assert "boom" in error
    assert any(level == "ERROR" and "PR #25" in msg
               and "Traceback (most recent call last)" in msg
               for msg, level in logged), logged


def test_a_failed_pr_comment_logs_a_warn_and_does_not_raise(tmp_path):
    """A swallowed gh failure must at least name the PR it lost."""
    pair = _repo_pair(tmp_path)
    logged = []

    def failing_gh(argv, cwd=None, **kwargs):
        raise OSError("gh missing")

    mq._handback({"pr": 26, "card": "t_x"},
                 {"repo": str(pair["repo"])}, "body",
                 lambda card, assignee, msg: True, failing_gh,
                 log=lambda msg, level="INFO", **kwargs:
                     logged.append((msg, level)))

    assert any(level == "WARN" and "PR #26" in msg
               and "gh missing" in msg for msg, level in logged), logged


# ── stale-base auto-refresh (t_4f8d3bca) ──────────────────────────────────
#
# A wait-listed PR was red AT THE MOMENT ITS CI RAN, and the wait list never
# asked again: PRs sat 9-18 h red because main itself was broken at the time,
# or because one flaky leg happened to fail on their run. The per-tick drain
# now updates such a PR onto the current base with `gh pr update-branch`, which
# re-runs CI on a base that may since have been fixed. The property these cases
# pin is the BOUND: one update per (PR, base sha), and never a retry after a
# conflict — an unbounded refresh would keep CI busy forever.

_RED_CHECK = [{"__typename": "CheckRun", "status": "COMPLETED",
               "conclusion": "FAILURE"}]
_CANCELLED_CHECK = [{"__typename": "CheckRun", "status": "COMPLETED",
                     "conclusion": "CANCELLED"}]
_GREEN_CHECK = [{"__typename": "CheckRun", "status": "COMPLETED",
                 "conclusion": "SUCCESS"}]
_PENDING_CHECK = [{"__typename": "CheckRun", "status": "IN_PROGRESS"}]


def _waiting_entry(pr, branch):
    return {"pr": pr, "card": f"t_pr{pr}", "assignee": "worker",
            "branch": branch, "enqueued_at": mq._now_iso(), "attempts": 0}


def _park(*, state_path, project, entries):
    """Put entries straight into the wait list (that is where they land)."""
    doc = mq.load_state(state_path)
    mq._project_state(doc, project)["waiting"].extend(entries)
    mq.save_state(doc, state_path)


def _waiting(state_path, project, pr):
    """The live wait-list entry for ``pr``."""
    waiting = mq.load_state(state_path)["projects"][project]["waiting"]
    return next(e for e in waiting if e["pr"] == pr)


def _origin_sha(pair, base="main"):
    """``base``'s sha read from the BARE origin — independent of the code."""
    return _git(pair["origin"], "rev-parse", base)


def _advance_main(repo, n, base="main"):
    """One more commit on ``base``, pushed to origin; returns the new sha."""
    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", base)
    (repo / f"main-{n}.txt").write_text(f"main commit {n}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"main {n}")
    _git(repo, "push", "origin", base)
    sha = _git(repo, "rev-parse", "HEAD")
    if current != base:
        _git(repo, "checkout", current)
    return sha


def test_red_waiting_pr_updates_onto_main_once_per_main_commit(tmp_path):
    """One update per main commit: not per tick, and not zero when main moves."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_RED_CHECK)
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    logged = []

    def drain():
        return mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh, git_runner=subprocess.run,
                                 log=lambda msg, *a, **k: logged.append(msg))

    assert drain() == []                      # red: it does not go back to the FIFO
    assert gh.updates == [9]                  # EXACTLY one update-branch
    entry = _waiting(state, "alpha", 9)
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert entry["refreshed_at"]
    assert not entry.get("update_conflict")
    assert any("PR #9 red on stale base; updated branch onto main "
               + _origin_sha(pair)[:12] in line for line in logged), logged

    # The same base on the following ticks: the recorded sha IS the bound.
    drain()
    drain()
    assert gh.updates == [9]
    assert len([line for line in logged
                if "red on stale base" in line]) == 1, logged

    # Main moves: exactly one more update, and onto the NEW sha.
    new_sha = _advance_main(pair["repo"], 2)
    drain()
    assert gh.updates == [9, 9]
    assert _waiting(state, "alpha", 9)["refreshed_main_sha"] == new_sha


def test_conflicted_update_is_flagged_and_never_retried(tmp_path):
    """A conflict marks the entry for hand-back and stops asking, forever."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}},
                rollup=_RED_CHECK, update_results=["conflict"])
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    logged = []

    mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                      git_runner=subprocess.run,
                      log=lambda msg, *a, **k: logged.append(msg))

    assert gh.updates == [9]
    entry = _waiting(state, "alpha", 9)
    assert entry["update_conflict"] is True
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert any("update-branch conflicted" in line for line in logged), logged
    assert not any("red on stale base" in line for line in logged), logged

    # Main moves twice more: a conflict is not retried onto a newer base, not
    # by the sha bound and not by the passage of ticks.
    _advance_main(pair["repo"], 2)
    _advance_main(pair["repo"], 3)
    mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                      git_runner=subprocess.run,
                      log=lambda msg, *a, **k: logged.append(msg))
    assert gh.updates == [9]


def test_a_failed_update_records_the_attempt_and_does_not_loop(tmp_path):
    """A gh error is still an attempt: the bound must hold on it too."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}},
                rollup=_RED_CHECK, update_results=["error"])
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    logged = []

    for _ in range(3):
        mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                          git_runner=subprocess.run,
                          log=lambda msg, *a, **k: logged.append(msg))

    assert gh.updates == [9]                     # three ticks, ONE attempt
    entry = _waiting(state, "alpha", 9)
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert "could not resolve" in entry["last_update_error"]
    assert not entry.get("update_conflict")       # not a conflict: card 2's rule


def test_a_branch_already_on_main_is_not_logged_as_updated(tmp_path):
    """``gh`` answers "already up to date": record the sha, do not claim one."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}},
                rollup=_RED_CHECK, update_results=["current"])
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    logged = []

    mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                      git_runner=subprocess.run,
                      log=lambda msg, *a, **k: logged.append(msg))

    assert gh.updates == [9]
    assert _waiting(state, "alpha", 9)["refreshed_main_sha"] == _origin_sha(pair)
    assert any("branch already onto main" in line for line in logged), logged
    assert not any("updated branch onto main" in line for line in logged), logged


def test_refresh_through_the_production_default_git_runner(tmp_path):
    """NO injected git runner: the base probe runs the production path.

    Every other queue case passes ``git_runner=subprocess.run``, which hides
    ``_default_runner`` entirely — the same seam that once raised ``TypeError``
    on every production call because the tests all injected a runner. This one
    does not, so the new ``git ls-remote`` probe is proven through the runner
    the daemon actually uses.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_RED_CHECK)
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])

    assert mq._drain_waiting("alpha", cfg, state_path=state,
                             gh_runner=gh) == []

    assert gh.updates == [9]
    entry = _waiting(state, "alpha", 9)
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert entry["refreshed_at"]


def test_green_waiting_pr_returns_and_is_never_updated(tmp_path):
    """The refresh must not touch a PR the drain is already returning."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}})          # green by default
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])

    assert mq._drain_waiting("alpha", cfg, state_path=state,
                             gh_runner=gh, git_runner=subprocess.run) == [9]
    assert gh.updates == []
    ps = mq.load_state(state)["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [9] and ps["waiting"] == []


def test_prs_outside_the_wait_list_are_never_touched(tmp_path):
    """Read-only is not a vibe: an empty wait list makes ZERO gh calls."""
    pair = _repo_pair(tmp_path)
    other = _new_branch(pair["repo"], "wt/t_other")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({11: {"headRefName": "wt/t_other", "headRefOid": other}},
                rollup=_RED_CHECK)
    state = _state(tmp_path)
    mq.add_to_queue("alpha", 11, path=state)     # queued, NOT wait-listed

    assert mq._drain_waiting("alpha", cfg, state_path=state,
                             gh_runner=gh, git_runner=subprocess.run) == []
    assert gh.calls == [], gh.calls


def test_which_wait_listed_pr_is_eligible_for_a_base_refresh():
    """Red and cancelled qualify; everything else is left strictly alone."""
    def info(rollup, **over):
        return {"state": "OPEN", "isDraft": False,
                "statusCheckRollup": rollup, **over}

    assert mq._red_on_stale_base({"pr": 1}, info(_RED_CHECK))
    # A cancelled run will never answer: waiting on it is the 18 h trap.
    assert mq._red_on_stale_base({"pr": 1}, info(_CANCELLED_CHECK))
    assert not mq._red_on_stale_base({"pr": 1}, info(_GREEN_CHECK))
    assert not mq._red_on_stale_base({"pr": 1}, info(_PENDING_CHECK))
    assert not mq._red_on_stale_base({"pr": 1}, info([]))
    # Card 2 owns a conflicted entry; the queue must not keep poking it.
    assert not mq._red_on_stale_base({"pr": 1, "update_conflict": True},
                                     info(_RED_CHECK))
    assert not mq._red_on_stale_base({"pr": 1},
                                     info(_RED_CHECK, state="CLOSED"))
    assert not mq._red_on_stale_base({"pr": 1},
                                     info(_RED_CHECK, state="MERGED"))
    assert not mq._red_on_stale_base({"pr": 1},
                                     info(_RED_CHECK, isDraft=True))


def test_update_branch_separates_a_local_fault_from_an_answer():
    """``answered`` — not ``ok`` — is what the base-sha bound keys on.

    A gh that could not run at all leaves the base unburned; a gh that answered
    with a non-zero exit is a verdict on this base and does burn it, which is
    what ``test_a_failed_update_records_the_attempt_and_does_not_loop`` pins.
    """
    def raiser(argv, **kwargs):
        raise OSError("gh missing")

    def silent(argv, **kwargs):
        return None                                  # a runner with no proc

    def refuses(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="could not resolve to PullRequest")

    ok, conflict, output, answered = mq._update_branch("/repo", 9, raiser)
    assert (ok, conflict, answered) == (False, False, False)
    assert "raised" in output
    assert mq._update_branch("/repo", 9, silent)[3] is False
    ok, conflict, output, answered = mq._update_branch("/repo", 9, refuses)
    assert (ok, conflict, answered) == (False, False, True)
    assert "could not resolve" in output


def test_a_local_gh_fault_does_not_burn_the_base_sha(tmp_path):
    """gh not runnable/raised is a LOCAL fault: the next tick must try again.

    Recording the base sha on such a fault locks the entry out of every later
    refresh for that commit — the 18 h wait this feature exists to open, made
    permanent by one transient local error (the None-runner crash the live
    daemon hit every tick). The entry keeps the error for the monitors and the
    refresh resumes the moment gh can run.

    Under the routing drain the entry must ALSO keep being routed to the refresh
    rather than to the hand-back: a local fault is not a verdict on this base,
    so the author must not be blamed for it.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_RED_CHECK)
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    logged = []
    runnable = {"gh": False}
    attempts = []

    def local_gh(argv, cwd=None, **kwargs):
        argv = list(argv)
        if argv[:3] == ["gh", "pr", "update-branch"]:
            attempts.append(int(argv[3]))
            if not runnable["gh"]:
                raise FileNotFoundError(2, "No such file or directory: 'gh'")
        return gh(argv, cwd=cwd)

    def drain():
        return mq._drain_waiting(
            "alpha", cfg, state_path=state, gh_runner=local_gh,
            git_runner=subprocess.run,
            log=lambda msg, level="INFO", **k: logged.append((msg, level)))

    for _ in range(2):
        assert drain() == []

    entry = _waiting(state, "alpha", 9)
    assert attempts == [9, 9], attempts            # asked again, not once
    assert gh.updates == []                        # never reached GitHub
    assert "refreshed_main_sha" not in entry, entry
    assert "No such file or directory" in entry["last_update_error"]
    assert not entry.get("handed_back_main_sha"), entry   # not the author's fault
    assert any("could not run" in msg and level == "WARNING"
               for msg, level in logged), logged

    # gh comes back: the same base is refreshed after all, because it was
    # never written off as attempted.
    runnable["gh"] = True
    assert drain() == []
    assert gh.updates == [9]
    entry = _waiting(state, "alpha", 9)
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert entry["refreshed_at"]


def test_refresh_through_the_production_default_gh_runner(tmp_path,
                                                          monkeypatch):
    """NO injected runners at all: the refresh runs the production path.

    The daemon's ``check_merge_queue`` calls ``tick`` — and with it the drain —
    with no runners, and every other case here injects one, which is exactly
    how a ``None`` reached ``_gh`` in production and killed the whole tick
    (``TypeError: 'NoneType' object is not callable``). This drives the drain
    the way the daemon does and pins the argv the default runner was handed.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_RED_CHECK)
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    seen = []

    def production_default(argv, **kwargs):
        # The argv _gh/_git build, then the keywords they add — the real
        # signature of the runner the daemon installs.
        argv = [str(part) for part in argv]
        seen.append(argv)
        if argv[0] == "gh":
            return gh(argv, cwd=kwargs.get("cwd"))
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(mq, "_default_runner", production_default)

    assert mq._drain_waiting("alpha", cfg, state_path=state) == []

    assert [a for a in seen
            if a[:3] == ["gh", "pr", "update-branch"]] == [
                ["gh", "pr", "update-branch", "9"]], seen
    entry = _waiting(state, "alpha", 9)
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert not entry.get("last_update_error")


def test_the_refresh_defaults_its_own_runners(tmp_path, monkeypatch):
    """Called directly, ``_refresh_stale_red`` still has no ``None`` to hand on.

    The drain's defaults cover what the drain calls. This is the seam that makes
    the ``gh`` call, so it defaults for ITSELF: a caller that reaches it
    directly — or one added later — cannot repeat the production crash. Pinned
    the way ``test_resume_ci_pending_defaults_the_production_runners`` pins that
    path: NO runners injected anywhere, and the argv the default runner was
    handed is the assertion, so a callee defaulting once more layers down would
    not save it either.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_RED_CHECK)
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    seen = []

    def production_default(argv, **kwargs):
        argv = [str(part) for part in argv]
        seen.append(argv)
        if argv[0] == "gh":
            return gh(argv, cwd=kwargs.get("cwd"))
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(mq, "_default_runner", production_default)

    assert mq._refresh_stale_red("alpha", cfg, "main",
                                [_waiting_entry(9, pair["branch"])],
                                state_path=state) == [9]

    assert [a for a in seen
            if a[:3] == ["gh", "pr", "update-branch"]] == [
                ["gh", "pr", "update-branch", "9"]], seen
    entry = _waiting(state, "alpha", 9)
    assert entry["refreshed_main_sha"] == _origin_sha(pair)
    assert not entry.get("last_update_error")


# ── wait-list hand-back: red for its own reasons (t_bcc4e414) ───────────────
#
# The refresh (above) is the queue's last move. After it there are two kinds of
# red left in the wait list: a base the branch will not move onto (a conflict —
# never retried), and a check that is red AGAIN on the current base. Both are
# verdicts on the PR, and until now the wait list sat on them silently while the
# card waited for nobody. These cases pin the hand-back and, more importantly,
# its bound: one hand-back per (PR, base sha), with the entry still waiting.

def _named_red(*names, extra=()):
    """A rollup of named FAILURE check runs — so a body can be checked for names."""
    return ([{"__typename": "CheckRun", "name": n, "status": "COMPLETED",
              "conclusion": "FAILURE"} for n in names] + list(extra))


def _handback_recorder(seen):
    def handback(card, assignee, body):
        seen.append((card, assignee, body))
        return True
    return handback


def test_a_conflicted_base_update_is_commented_on_and_handed_back_once(tmp_path):
    """A conflict is the queue's dead end: it says so, once, and keeps waiting."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"), update_results=["conflict"])
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    handed, logged = [], []

    def drain():
        return mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh, git_runner=subprocess.run,
                                 handback=_handback_recorder(handed),
                                 log=lambda msg, *a, **k: logged.append(msg))

    drain()                       # tick 1: the update is ATTEMPTED, not reported
    assert gh.updates == [9] and handed == [] and gh.comments == []

    drain()                       # tick 2: the conflict is the author's problem
    assert len(handed) == 1 and len(gh.comments) == 1, (handed, gh.comments)
    assert handed[0][0] == "t_pr9" and handed[0][1] == "worker"
    body = gh.comments[0][-1]
    assert "cannot be moved onto the current base" in body
    assert "Failing check(s): scripts" in body        # names what is red
    assert _origin_sha(pair)[:12] in body             # the base it was tried on
    assert pair["branch"] in body                     # resolve step: its own ref
    assert any("handed back to card t_pr9: conflict" in line for line in logged)

    # The bound: three more ticks, still one comment and one board handback.
    for _ in range(3):
        drain()
    assert len(handed) == 1 and len(gh.comments) == 1, (handed, gh.comments)
    assert gh.updates == [9]                  # a conflict is never retried
    ps = mq.load_state(state)["projects"]["alpha"]
    assert [e["pr"] for e in ps["waiting"]] == [9] and ps["queue"] == []
    entry = _waiting(state, "alpha", 9)
    assert entry["handed_back_main_sha"] == _origin_sha(pair)
    assert entry["handed_back_at"]


def test_a_red_check_on_the_refreshed_base_hands_the_card_back_once(tmp_path):
    """Red on the base the queue itself moved it onto is the author's verdict."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_named_red("unit", "lint"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    handed, logged = [], []

    def drain():
        return mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh, git_runner=subprocess.run,
                                 handback=_handback_recorder(handed),
                                 log=lambda msg, *a, **k: logged.append(msg))

    drain()                       # tick 1: refreshed onto the current main
    assert gh.updates == [9] and handed == []
    assert not _waiting(state, "alpha", 9).get("handed_back_main_sha")

    drain()                       # tick 2: red again on THAT base
    assert len(handed) == 1 and len(gh.comments) == 1, (handed, gh.comments)
    assert handed[0][0] == "t_pr9"
    body = gh.comments[0][-1]
    assert "red again on that base" in body
    assert "Failing check(s): unit, lint" in body
    assert any("handed back to card t_pr9: red-after-refresh" in line
               for line in logged), logged
    assert gh.updates == [9]      # card 1's bound still holds: no second update
    drain()
    assert len(handed) == 1       # once per main sha

    # Main moves: the PR gets its one free re-run on the NEW base first (no
    # blaming the author for a base that may have been broken), and only then a
    # second hand-back — the bound is per main sha, not per PR.
    _advance_main(pair["repo"], 2)
    drain()
    assert gh.updates == [9, 9] and len(handed) == 1
    drain()
    assert len(handed) == 2 and len(gh.comments) == 2

    # And it was never dropped: green now returns it to the FIFO as always.
    gh.rollup = _GREEN_CHECK
    assert drain() == [9]
    ps = mq.load_state(state)["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [9] and ps["waiting"] == []


def test_a_green_or_unmovable_waiting_pr_is_never_handed_back(tmp_path):
    """Green returns to the FIFO; a closed or draft PR is left strictly alone."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"], "headRefOid": pair["head"]}})
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])
    handed = []

    def drain():
        return mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh, git_runner=subprocess.run,
                                 handback=_handback_recorder(handed))

    assert drain() == [9]
    assert handed == [] and gh.comments == [] and gh.updates == []

    # Same red, but the PR is closed: not the queue's to comment on any more.
    gh.closed = [10]
    gh.heads[10] = {"headRefName": pair["branch"], "headRefOid": pair["head"]}
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(10, pair["branch"])])
    gh.rollup = _named_red("scripts")
    assert drain() == []
    assert handed == [] and gh.comments == []
    assert gh.updates == []       # and no refresh burned on a closed PR either


def test_a_waiting_entry_with_no_card_is_handed_back_to_its_branch_card(
        tmp_path, monkeypatch):
    """`wt/t_x` IS the card: a manually added PR still reaches its author."""
    pair = _repo_pair(tmp_path)
    branch = _new_branch(pair["repo"], "wt/t_branch_card")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    cfg["board"] = "alpha"
    _board_seam(monkeypatch, _board_with_cards(
        tmp_path, [("t_branch_card", "hand back please", "running")]))
    gh = FakeGh({12: {"headRefName": "wt/t_branch_card", "headRefOid": branch}},
                rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[{"pr": 12, "card": None, "assignee": None,
                    "branch": "wt/t_branch_card",
                    "enqueued_at": mq._now_iso(), "attempts": 0}])
    handed = []

    def drain():
        return mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh, git_runner=subprocess.run,
                                 handback=_handback_recorder(handed))

    drain()                       # the refresh attempt
    drain()                       # red on the current base -> hand back
    assert [h[0] for h in handed] == ["t_branch_card"], handed
    # The board supplies the assignee the entry never had, so the reassign lands.
    assert handed[0][1] == "worker", handed
    assert "wt/t_branch_card" in gh.comments[0][-1]


def test_hand_back_through_the_production_default_runner_shape(tmp_path):
    """No injected git runner and no injected handback: the daemon's own path.

    The drain's base probe runs through `_default_runner`, and the handback
    resolves to the module-level `default_handback` the daemon gets when `tick`
    injects nothing — the seam that once broke every production call while the
    injected runners passed. (conftest fences that handback's board write, so
    this proves the wiring without touching a live card.)
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}}, rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"])])

    for _ in range(2):
        assert mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh) == []

    assert gh.updates == [9] and len(gh.comments) == 1, gh.calls
    entry = _waiting(state, "alpha", 9)
    assert entry["handed_back_main_sha"] == _origin_sha(pair)
    assert entry["handed_back_at"]


def test_the_hand_back_rule_and_its_once_per_base_sha_bound():
    """The predicate table: which red goes back to whom, and when it stops."""
    def info(rollup, **over):
        return {"state": "OPEN", "isDraft": False,
                "statusCheckRollup": rollup, **over}

    sha, other = "a" * 40, "b" * 40
    # Nothing tried yet on this base: card 1 refreshes, nobody is blamed.
    assert mq._own_red_reason({"pr": 1}, sha) is None
    # Tried against THIS base and red: the author's turn.
    assert mq._own_red_reason({"refreshed_main_sha": sha}, sha) \
        == "red-after-refresh"
    # Tried against an OLDER base: that red is stale, so it is card 1's turn.
    assert mq._own_red_reason({"refreshed_main_sha": other}, sha) is None
    # A conflict is a wall whatever CI says about the head.
    assert mq._own_red_reason({"update_conflict": True}, sha) == "conflict"
    # The bound: at most once per (PR, base sha)...
    assert mq._own_red_reason({"update_conflict": True,
                               "handed_back_main_sha": sha}, sha) is None
    assert mq._own_red_reason({"refreshed_main_sha": sha,
                               "handed_back_main_sha": sha}, sha) is None
    # ...and a newer base re-arms it.
    assert mq._own_red_reason({"update_conflict": True,
                               "handed_back_main_sha": other}, sha) == "conflict"
    # No base sha answered: neither pass may act on a guess.
    assert mq._own_red_reason({"update_conflict": True}, None) is None
    assert mq._own_red_reason({"refreshed_main_sha": sha}, None) is None

    # Who is even eligible: red/cancelled or conflicted, OPEN and not a draft.
    assert mq._waiting_own_red({}, info(_RED_CHECK))
    assert mq._waiting_own_red({}, info(_CANCELLED_CHECK))
    assert mq._waiting_own_red({"update_conflict": True}, info(_GREEN_CHECK))
    assert not mq._waiting_own_red({}, info(_GREEN_CHECK))
    assert not mq._waiting_own_red({}, info(_PENDING_CHECK))
    assert not mq._waiting_own_red({}, info([]))
    assert not mq._waiting_own_red({}, info(_RED_CHECK, state="CLOSED"))
    assert not mq._waiting_own_red({}, info(_RED_CHECK, state="MERGED"))
    assert not mq._waiting_own_red({}, info(_RED_CHECK, isDraft=True))
    assert not mq._waiting_own_red({"update_conflict": True},
                                   info(_RED_CHECK, state="CLOSED"))

    # The card id comes from the branch, the failing names from the rollup.
    assert mq._card_from_branch("wt/t_1a2b3c") == "t_1a2b3c"
    assert mq._card_from_branch("  wt/t_x  ") == "t_x"
    assert mq._card_from_branch("main") is None
    assert mq._card_from_branch("") is None
    assert mq._card_from_branch(None) is None
    assert mq._failing_check_names(info(_named_red("unit", "lint"))) == [
        "unit", "lint"]
    assert mq._failing_check_names(info(_named_red(
        "legacy", extra=[{"context": "ci/legacy", "state": "FAILURE"},
                         {"__typename": "CheckRun", "name": "ok",
                          "status": "COMPLETED", "conclusion": "SUCCESS"}]))) \
        == ["legacy", "ci/legacy"]
    assert mq._failing_check_names(info([])) == []
    assert mq._failing_check_names(info(_named_red(
        *[f"c{i}" for i in range(mq.MAX_HANDBACK_CHECK_NAMES + 3)])))[-1] \
        == "(+3 more)"


def test_the_drain_never_hands_a_none_runner_downward(tmp_path, monkeypatch):
    """Whatever the drain calls gets a runner, never the ``None`` it was given.

    The defaults at the top of ``_drain_waiting`` are the point, not the one
    call they happen to cover today: the daemon enters the wait list through
    here with no runners, and ``pr_view``, ``default_branch`` and the base probe
    each default for themselves — which is exactly how a ``None`` survived all
    the way to the exec seam in production. So the capture is on the seam: every
    runner is recorded as it is HANDED DOWN, before any callee can paper over it.

    The routing drain has TWO downward paths, and both are parked here: an entry
    never refreshed yet (the stale-base refresh, whose ``update-branch`` is the
    call that crashed) and an entry red on THIS base (the hand-back's PR
    comment). Removing the drain defaults therefore goes red on card 1's path AND
    on card 2's, not just on whichever one happens to run.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]},
                 11: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_waiting_entry(9, pair["branch"]),                   # refresh
                   dict(_waiting_entry(11, pair["branch"]),             # hand-back
                        refreshed_main_sha=_origin_sha(pair))])
    seen = []

    def runner_at(index):
        def extract(*args, **kwargs):
            if len(args) > index:
                return args[index]
            return kwargs.get("runner")
        return extract

    def spy(name, runner_of):
        real = getattr(mq, name)

        def wrapper(*args, **kwargs):
            seen.append((name, runner_of(*args, **kwargs)))
            return real(*args, **kwargs)
        return wrapper

    def production_default(argv, **kwargs):
        argv = [str(part) for part in argv]
        if argv[0] == "gh":
            return gh(argv, cwd=kwargs.get("cwd"))
        return subprocess.run(argv, **kwargs)

    monkeypatch.setattr(mq, "_default_runner", production_default)
    # ``_gh``/``_git`` take the runner as their third positional argument: that
    # IS the seam the live daemon died on. The rest are the callees each of the
    # drain's two red paths reaches, so a ``None`` cannot hide by being default
    # once more layers down.
    for name, index in (("_gh", 2), ("_git", 2), ("_remote_base_sha", 2),
                        ("_update_branch", 2), ("_comment_pr", 3)):
        monkeypatch.setattr(mq, name, spy(name, runner_at(index)))
    monkeypatch.setattr(mq, "pr_view", spy("pr_view", runner_at(2)))

    assert mq._drain_waiting("alpha", cfg, state_path=state,
                             handback=_handback_recorder([])) == []

    # Both red paths were really walked, so the capture is not vacuous: the
    # refresh moved PR 9 and the hand-back commented on PR 11.
    assert gh.updates == [9], gh.calls
    assert len(gh.comments) == 1, gh.calls
    seams = {name for name, _ in seen}
    assert {"_update_branch", "_comment_pr", "_gh"} <= seams, seen
    handed = [runner for _, runner in seen]
    assert handed, seen
    assert all(callable(runner) for runner in handed), seen


# ── wait-list expiry and dead PRs (t_a72d0867) ──────────────────────────────
#
# Cards 1 and 2 made the wait list ACT on a red PR; both keep the entry, so the
# wait list still had no exit for a PR nobody was pushing any more, and it kept
# counting PRs that had already landed or been closed. (One of those was swept by
# hand on 2026-11-24: a PR merged at 11:35:08Z was still in the queue afterwards,
# and the tell was `fatal: bad revision origin/wt/<id>` — a merged PR's branch is
# deleted on merge. That is why every case here keys on the PR's STATE and none of
# them asks whether the branch still resolves.)
#
# The properties these pin: a 25 h wait is dropped with ONE comment naming the
# budget and the command to re-add; a 23 h wait is kept and still worked; a merged
# or closed PR leaves immediately with no comment at all; the budget is the
# project's to set; and a non-OPEN PR in the FIFO is rejected before a rebase or
# a gate can spend a budget on a commit already on main.

_HOUR = 3600


def _aged_entry(pr, branch, age_s):
    """A wait-list entry stamped ``age_s`` seconds ago.

    The stamp is written against the real clock because the code under test reads
    the real clock: freezing ``_now_iso`` would freeze the fixture and the
    measurement together, which proves nothing about either.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                          time.gmtime(time.time() - age_s))
    return {"pr": pr, "card": f"t_pr{pr}", "assignee": "worker",
            "branch": branch, "enqueued_at": stamp, "attempts": 0}


def _waiting_prs(state_path, project):
    """The PR numbers still in the wait list, in wait-list order."""
    return [e.get("pr") for e in
            mq.load_state(state_path)["projects"][project]["waiting"]]


def _last(state_path, project):
    return mq.load_state(state_path)["projects"][project]["last"]


def test_a_waiting_pr_past_the_budget_is_dropped_with_a_comment(tmp_path):
    """25 h of waiting is the end of the line: out of the queue, one comment."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_aged_entry(9, pair["branch"], 25 * _HOUR)])
    logged = []

    def drain():
        return mq._drain_waiting("alpha", cfg, state_path=state,
                                 gh_runner=gh, git_runner=subprocess.run,
                                 log=lambda msg, *a, **k: logged.append(msg))

    assert drain() == []                     # it goes nowhere: it is dropped
    ps = mq.load_state(state)["projects"]["alpha"]
    assert ps["waiting"] == [] and ps["queue"] == []
    assert len(gh.comments) == 1, gh.comments
    body = gh.comments[0][-1]
    assert "dropped from the merge queue after 25h waiting" in body
    assert "hscc merge-queue add alpha 9" in body       # the way back in
    assert "Still red: scripts" in body                 # what never went green
    assert any("PR #9 dropped from the wait list after 25h waiting "
               "(budget 24h)" in line for line in logged), logged
    last = _last(state, "alpha")
    assert last["result"] == "expired" and last["pr"] == 9
    assert "waited 25h" in last["error"] and "budget 24h" in last["error"]
    # Expired, not re-tried: the sweep runs BEFORE the red routing, so a PR the
    # queue has given up on never burns an update-branch on a fresh base.
    assert gh.updates == []

    # Gone means gone: the next tick has an empty wait list and says nothing.
    assert drain() == []
    assert len(gh.comments) == 1


def test_a_waiting_pr_inside_the_budget_is_kept_and_still_worked(tmp_path):
    """23 h is not 25 h: the entry stays, and cards 1 and 2 still apply to it."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"],
                     "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_aged_entry(9, pair["branch"], 23 * _HOUR)])
    logged = []

    assert mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                             git_runner=subprocess.run,
                             log=lambda msg, *a, **k: logged.append(msg)) == []

    assert _waiting_prs(state, "alpha") == [9]
    assert gh.comments == []
    # The refresh is what a kept red entry gets — the sweep did not pre-empt it.
    assert gh.updates == [9]
    assert _waiting(state, "alpha", 9)["refreshed_main_sha"] == _origin_sha(pair)
    assert _last(state, "alpha") is None      # nothing terminal was recorded
    assert not any("dropped from the wait list" in line for line in logged), logged


def test_merged_and_closed_waiting_prs_leave_without_a_comment(tmp_path):
    """A PR the queue no longer owns is out of the ledger, at once, silently."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
                 10: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"), closed=[10])
    gh.merged = [9]
    state = _state(tmp_path)
    # #9 is 25 h old AND merged: the state wins, because "this landed" is the
    # truer thing to say about it than "this ran out of budget".
    _park(state_path=state, project="alpha",
          entries=[_aged_entry(9, pair["branch"], 25 * _HOUR),
                   _aged_entry(10, pair["branch"], 1 * _HOUR)])
    logged = []

    assert mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                             git_runner=subprocess.run,
                             log=lambda msg, *a, **k: logged.append(msg)) == []

    assert _waiting_prs(state, "alpha") == []
    assert gh.comments == []          # commenting on a merged PR is noise
    assert gh.updates == []           # and no refresh is spent on it either
    assert any("PR #9 merged-elsewhere; dropped from the wait list" in line
               for line in logged), logged
    assert any("PR #10 closed; dropped from the wait list" in line
               for line in logged), logged
    last = _last(state, "alpha")      # ONE slot: the last drop of the pass
    assert last["result"] == "closed" and last["pr"] == 10
    assert mq.outcome_state("merged-elsewhere") == "merged"
    assert mq.outcome_state("closed") == "failed"


def test_the_wait_list_budget_is_the_projects_to_set(tmp_path):
    """`waiting_max_age_s` moves the line, and the pass honours it per project."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"],
                         waiting_max_age_s=6 * _HOUR)
    cfg = mq.project_config("alpha", registry_path=registry)
    assert mq.waiting_budget_s(cfg) == 6 * _HOUR     # read through the registry
    gh = FakeGh({9: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
                 10: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_aged_entry(9, pair["branch"], 7 * _HOUR),
                   _aged_entry(10, pair["branch"], 5 * _HOUR)])

    assert mq._drain_waiting("alpha", cfg, state_path=state, gh_runner=gh,
                             git_runner=subprocess.run) == []

    assert _waiting_prs(state, "alpha") == [10]      # 5 h is inside 6 h
    assert len(gh.comments) == 1, gh.comments
    assert "hscc merge-queue add alpha 9" in gh.comments[0][-1]
    assert "after 7h waiting" in gh.comments[0][-1]
    assert "6h wait-list budget" in gh.comments[0][-1]
    last = _last(state, "alpha")
    assert last["result"] == "expired" and last["pr"] == 9


def test_the_sweep_through_the_production_default_runner_shape(tmp_path):
    """No injected git runner: the drop and the base probe share one pass.

    Every queue case injects ``git_runner``, which hides ``_default_runner`` —
    the same seam that once raised ``TypeError`` on every production call while
    the injected runners passed. Here the 25 h entry is dropped AND a second,
    in-budget red entry still needs the base sha, so the drop is proven alongside
    the daemon's own git path rather than instead of it.
    """
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    cfg = mq.project_config("alpha", registry_path=registry)
    gh = FakeGh({9: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
                 11: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"))
    state = _state(tmp_path)
    _park(state_path=state, project="alpha",
          entries=[_aged_entry(9, pair["branch"], 25 * _HOUR),
                   _waiting_entry(11, pair["branch"])])

    assert mq._drain_waiting("alpha", cfg, state_path=state,
                             gh_runner=gh) == []

    assert _waiting_prs(state, "alpha") == [11]
    assert len(gh.comments) == 1 and gh.updates == [11]
    assert _waiting(state, "alpha", 11)["refreshed_main_sha"] == _origin_sha(pair)


def test_the_sweep_predicates_their_budget_their_clock_and_their_state():
    """The predicate table behind the sweep, including every bad-ledger shape."""
    assert mq.WAITING_MAX_AGE_S == 24 * 3600
    # The budget is the project's to set — and a bad line never empties one.
    for cfg, want in (({}, 24 * 3600), (None, 24 * 3600),
                      ({"waiting_max_age_s": 3600}, 3600),
                      ({"waiting_max_age_s": "3600"}, 3600),
                      ({"waiting_max_age_s": 3600.9}, 3600),
                      ({"waiting_max_age_s": 0}, 24 * 3600),
                      ({"waiting_max_age_s": -1}, 24 * 3600),
                      ({"waiting_max_age_s": "24h"}, 24 * 3600),
                      ({"waiting_max_age_s": ""}, 24 * 3600),
                      ({"waiting_max_age_s": True}, 24 * 3600),
                      ({"waiting_max_age_s": None}, 24 * 3600),
                      ({"waiting_max_age_s": []}, 24 * 3600)):
        assert mq.waiting_budget_s(cfg) == want, cfg

    # The clock: exactly the budget is KEPT, and a missing stamp is no expiry.
    assert mq._waiting_expired(86401, 86400)
    assert not mq._waiting_expired(86400, 86400)
    assert not mq._waiting_expired(None, 86400)

    # The state decides, so a deleted branch cannot be mistaken for a fault and
    # an OPEN answer with a squash commit already on main is still "landed".
    assert mq._pr_gone({"state": "MERGED"}) == "merged-elsewhere"
    assert mq._pr_gone({"state": "OPEN",
                        "mergeCommit": {"oid": "abc"}}) == "merged-elsewhere"
    assert mq._pr_gone({"state": "CLOSED"}) == "closed"
    for still_ours in ({}, None, {"state": "OPEN"},
                       {"state": "OPEN", "mergeCommit": None},
                       {"state": "OPEN", "mergeCommit": {"oid": None}},
                       {"state": "OPEN", "mergeCommit": "junk"}):
        assert mq._pr_gone(still_ours) is None, still_ours

    # Spans read as a person would say them; entry numbers survive a ledger that
    # stored them as strings.
    assert mq._age_span(24 * 3600) == "24h"
    assert mq._age_span(90000) == "25h"
    assert mq._age_span(3599) == "59m"
    assert mq._age_span(59) == "59s"
    assert mq._age_span(0) == "0s"
    assert mq._age_span(None) == "0s"
    assert mq._entry_pr({"pr": "9"}) == 9
    assert mq._entry_pr({"pr": 9.0}) == 9
    for junk in ({}, {"pr": None}, {"pr": "x"}, None, "x", 9):
        assert mq._entry_pr(junk) is None, junk


def test_a_merged_or_closed_pr_in_the_fifo_is_rejected_before_any_gate(tmp_path):
    """Tick time: the tell is the PR's state, and nothing past it may run.

    The branch is deleted from origin first — the exact shape of the specimen, a
    merged PR still sitting in the queue — so a rebase or a diff attempted here
    would die on a ref that no longer exists and read as a broken queue. The gate
    leaves a marker behind, so "no gate ran" is a fact about a file, not a vibe.
    """
    pair = _repo_pair(tmp_path)
    marker = tmp_path / "gate-ran"
    registry = _registry(tmp_path, pair["repo"],
                         gates=[f"sh -c 'touch {marker}'"])
    gh = FakeGh({9: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
                 10: {"headRefName": pair["branch"], "headRefOid": pair["head"]}},
                rollup=_named_red("scripts"), closed=[10])
    gh.merged = [9]
    _git(pair["repo"], "push", "origin", "--delete", pair["branch"])
    state = _state(tmp_path)
    mq.add_to_queue("alpha", 9, card="t_pr9", path=state)

    def tick():
        return mq.tick(state_path=state, root=str(tmp_path / "queue"),
                       registry_path=registry, gh_runner=gh,
                       git_runner=subprocess.run)

    result = tick()
    assert result["processed"][0]["result"] == "already-merged", result
    assert not marker.exists()              # no budget spent on a landed commit
    assert gh.merge_calls == []             # GitHub was never asked to merge it
    assert gh.comments == []
    ps = mq.load_state(state)["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == [] and ps["in_flight"] is None
    assert ps["last"]["result"] == "already-merged"

    # A CLOSED claim is the same rejection — and it no longer answers with the
    # merged label this path used to return for every non-OPEN PR.
    mq.add_to_queue("alpha", 10, card="t_pr10", path=state)
    result = tick()
    assert result["processed"][0]["result"] == "already-closed", result
    assert not marker.exists() and gh.merge_calls == []
    assert _last(state, "alpha")["result"] == "already-closed"


# ── a per-PR hold survives a push (t_662753d6) ──────────────────────────────
#
# `hscc merge-queue remove <project> <pr>` is a pull, not a hold: the next empty
# tick runs auto-enqueue again, and the ONLY existing suppression
# (`_auto_enqueue_suppressed`) covers a single PR that just failed a RED GATE on
# the SAME head SHA — so the push a review request causes is exactly the event
# that lifts it. Measured on the deployed copy (2026-11-24): a PR pulled while a
# `HOLD — do not re-queue this PR` comment sat on it was auto-enqueued again by
# the tick after its fold commit (`beba4c4c` -> `2ca09b1c`), and a PR whose gate
# exhausted its 3600s budget was handed back and began a second hour. The only
# lever was project-wide (`pause`), which halts every other queued PR too.
#
# These cases pin the per-PR hold: recorded in the SAME state document as
# `enabled`/`paused`, honoured by every path that can put a PR back into the
# FIFO (auto-enqueue, the wait-list drain, the wait-list retry claim), honoured
# regardless of the head SHA, and released ONLY by `unhold`. The ticks are driven
# with `_no_runner` wherever the queue must not touch git/gh at all: a held PR
# that is skipped before the readiness probe costs no network call, so an
# injected runner that raises is the assertion that nothing was probed.

def test_a_held_pr_stays_out_of_the_fifo_after_its_head_changes(
        tmp_path, monkeypatch):
    """THE regression: a push must not lift a hold (a push is what a review causes)."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    mq.hold_pr("alpha", 7, reason="defect-carrying head", path=_state(tmp_path))

    def source(cfg, gh_runner=None):
        return [{"pr": 7, "card": "t_hold", "assignee": "worker",
                 "branch": pair["branch"]}]

    def run_tick():
        # No runner may be touched: the hold wins BEFORE the readiness probe, so
        # a held PR is neither probed (`gh`) nor processed (git) at all.
        return mq.tick(state_path=_state(tmp_path),
                       root=str(tmp_path / "queue"), registry_path=registry,
                       auto_source=source, git_runner=_no_runner,
                       gh_runner=_no_runner)

    first = run_tick()
    assert first["processed"] == [], first
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == [] and ps["in_flight"] is None
    assert [h["pr"] for h in ps["held"]] == [7], ps
    assert ps["held"][0]["reason"] == "defect-carrying head", ps

    # The worker pushes the fix. Today this is the event that clears the red-gate
    # suppression and re-queues the PR; a hold must survive it.
    _git(pair["repo"], "checkout", pair["branch"])
    (pair["repo"] / "folded.txt").write_text("folded\n")
    _git(pair["repo"], "add", ".")
    _git(pair["repo"], "commit", "-m", "fold the review fix")
    _git(pair["repo"], "push", "origin", pair["branch"])
    assert _git(pair["repo"], "rev-parse", "HEAD") != pair["head"]

    second = run_tick()
    assert second["processed"] == [], second
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == [], ps
    assert [h["pr"] for h in ps["held"]] == [7], ps


def test_unhold_lets_the_pr_back_in_on_the_next_tick(tmp_path, monkeypatch):
    """Release is explicit: `unhold`, and then the normal path re-queues it."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"], "headRefOid": pair["head"]}})
    mq.hold_pr("alpha", 7, reason="parked by the orchestrator",
               path=_state(tmp_path))

    def source(cfg, gh_runner=None):
        return [{"pr": 7, "card": "t_hold", "assignee": "worker",
                 "branch": pair["branch"]}]

    def run_tick():
        return mq.tick(state_path=_state(tmp_path),
                       root=str(tmp_path / "queue"), registry_path=registry,
                       auto_source=source, gh_runner=gh,
                       git_runner=subprocess.run)

    assert run_tick()["processed"] == []
    assert gh.calls == [], "a held PR must not even be probed"

    mq.unhold_pr("alpha", 7, path=_state(tmp_path))
    result = run_tick()

    assert result["processed"][0]["pr"] == 7, result
    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [7]
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["held"] == [], ps


def test_a_hold_only_stops_its_own_pr_and_pause_still_halts_everything(
        tmp_path, monkeypatch):
    """A hold is per-PR: the rest of the FIFO runs; `pause` remains the sledgehammer."""
    pair = _repo_pair(tmp_path)
    second_head = _new_branch(pair["repo"], "wt/t_second")
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({
        7: {"headRefName": pair["branch"], "headRefOid": pair["head"]},
        8: {"headRefName": "wt/t_second", "headRefOid": second_head},
    })
    mq.hold_pr("alpha", 7, reason="prose diff the selector cannot narrow",
               path=_state(tmp_path))

    def source(cfg, gh_runner=None):
        return [{"pr": 7, "card": "t_a", "assignee": "worker"},
                {"pr": 8, "card": "t_b", "assignee": "worker"}]

    def run_tick():
        return mq.tick(state_path=_state(tmp_path),
                       root=str(tmp_path / "queue"), registry_path=registry,
                       auto_source=source, gh_runner=gh,
                       git_runner=subprocess.run)

    first = run_tick()
    assert [e["pr"] for e in first["processed"]] == [8], first
    assert first["processed"][0]["result"] == "merged", first
    assert gh.merged == [8]
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == [], ps
    assert [h["pr"] for h in ps["held"]] == [7], ps

    # `pause` still halts the whole project — including an UNHELD ready PR.
    mq.unhold_pr("alpha", 7, path=_state(tmp_path))
    mq.set_paused("alpha", True, path=_state(tmp_path))
    assert run_tick()["processed"] == []
    assert gh.merged == [8], "a paused project must not be processed"
    mq.set_paused("alpha", False, path=_state(tmp_path))
    assert run_tick()["processed"][0]["pr"] == 7


def test_a_hold_survives_a_gate_timeout_transition(tmp_path, monkeypatch):
    """Instance 2: the gate-timeout hand-back must not start a second hour."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["sh -c 'sleep 999'"],
                         timeout_s=42)
    gh = FakeGh({31: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}})
    fake, _calls = _fake_subprocess(popem=_timeout_popem())
    monkeypatch.setattr(mq, "subprocess", fake)
    mq.add_to_queue("alpha", 31, card="t_slow", assignee="worker",
                    path=_state(tmp_path))

    first = mq.tick(state_path=_state(tmp_path), root=str(tmp_path / "queue"),
                    registry_path=registry, gh_runner=gh,
                    git_runner=subprocess.run,
                    handback=lambda card, assignee, msg: True)
    assert first["processed"][0]["result"] == "gate-timeout", first
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["waiting"]] == [31], ps

    # The operator pulls it AFTER the timeout handed it back: the pull must hold.
    logged = []
    mq.hold_pr("alpha", 31, reason="gate budget exhausted twice",
               path=_state(tmp_path))
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["waiting"] == [] and ps["queue"] == [], ps
    assert [h["pr"] for h in ps["held"]] == [31], ps
    assert ps["held"][0]["reason"] == "gate budget exhausted twice", ps

    # And the hand-back path must not start a second hour: the every-tick
    # wait-list drain leaves it out, and the gate never runs again for it.
    second = mq.tick(state_path=_state(tmp_path), root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run,
                     log=lambda msg, *a, **k: logged.append(msg))
    assert second["processed"] == [], second
    assert gh.merged == [], second
    assert not [line for line in logged if "returned green waiting" in line], \
        logged
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["waiting"] == [] and ps["queue"] == [], ps
    assert ps["held"][0]["reason"] == "gate budget exhausted twice", ps


def test_a_held_pr_is_never_claimed_even_when_it_was_added_on_purpose(
        tmp_path, monkeypatch):
    """A held entry may sit in the FIFO (an explicit `add`) but is never claimed."""
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({7: {"headRefName": pair["branch"], "headRefOid": pair["head"]}})
    mq.hold_pr("alpha", 7, reason="parked", path=_state(tmp_path))
    mq.add_to_queue("alpha", 7, card="t_add", path=_state(tmp_path))

    result = mq.tick(state_path=_state(tmp_path), root=str(tmp_path / "queue"),
                     registry_path=registry, gh_runner=gh,
                     git_runner=subprocess.run)

    assert result["processed"] == [], result
    assert gh.merged == []
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [7], ps
    assert [h["pr"] for h in ps["held"]] == [7], ps


def test_hold_and_unhold_are_cli_verbs_that_record_and_release_the_reason(
        tmp_path, monkeypatch, capsys):
    """`hold`/`unhold` are the operator surface, and `add` cannot defeat a hold."""
    from hscc_daemon import merge_queue_cli
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    home = tmp_path / "hscc-home"
    monkeypatch.setenv("HSCC_HOME", str(home))
    monkeypatch.setenv("FLIGHTDECK_REGISTRY", registry)

    assert merge_queue_cli.cmd_merge_queue(
        ["hold", "alpha", "7", "--reason", "defect-carrying head"]) == 0
    out = capsys.readouterr().out
    assert "held PR #7" in out, out
    ps = mq.load_state(mq.state_file())["projects"]["alpha"]
    assert ps["held"] == [{"pr": 7, "reason": "defect-carrying head",
                           "held_at": ps["held"][0]["held_at"]}], ps

    # A held PR is not re-added by an explicit `add`: `unhold` is the release.
    assert merge_queue_cli.cmd_merge_queue(["add", "alpha", "7"]) == 1
    refusal = capsys.readouterr()
    assert "unhold" in refusal.out + refusal.err, \
        "the refusal must name unhold"
    assert mq.load_state(mq.state_file())["projects"]["alpha"]["queue"] == []

    assert merge_queue_cli.cmd_merge_queue(["unhold", "alpha", "7"]) == 0
    assert "released PR #7" in capsys.readouterr().out
    assert mq.load_state(mq.state_file())["projects"]["alpha"]["held"] == []

    # The verbs are in the operator-facing help, both group and per-verb.
    assert merge_queue_cli.cmd_merge_queue(["--help"]) == 0
    helptext = capsys.readouterr().out
    assert "merge-queue hold <project> <pr>" in helptext, helptext
    assert "merge-queue unhold <project> <pr>" in helptext, helptext


def test_the_read_surfaces_show_a_hold_with_its_reason_and_never_write(
        tmp_path, monkeypatch, capsys):
    """A hold is not folklore in a PR comment — and rendering it is read-only.

    The byte-identical-ledger guarantee #753 pinned for the queue line is
    re-asserted here with a hold in the document: `view`, the CLI's `status` and
    `list --json`, and the monitor line must all carry the reason, and the ledger
    must not gain a byte, a mtime tick, a lock file or a worktree directory.
    """
    from hscc_daemon import kanban_live, merge_queue_cli
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    home = tmp_path / "hscc-home"
    monkeypatch.setenv("HSCC_HOME", str(home))
    monkeypatch.setenv("FLIGHTDECK_REGISTRY", registry)
    mq.hold_pr("alpha", 7, reason="defect-carrying head",
               path=mq.state_file())
    mq.add_to_queue("alpha", 8, card="t_other", path=mq.state_file())
    path = mq.state_file()

    with open(path, "rb") as f:
        before = (os.stat(path).st_mtime_ns, f.read())
    before_dir = sorted(os.listdir(home))

    rows = mq.view(state_path=path, registry_path=registry)["projects"]["alpha"]
    assert rows["held_len"] == 1, rows
    assert rows["held"][0]["pr"] == 7, rows
    assert rows["held"][0]["reason"] == "defect-carrying head", rows
    # The held PR is a hold, not a queued or waiting row.
    assert [r["pr"] for r in rows["rows"]] == [8], rows

    assert merge_queue_cli.cmd_merge_queue(["status"]) == 0
    out = capsys.readouterr().out
    assert "held=1" in out, out
    assert "defect-carrying head" in out, out

    assert merge_queue_cli.cmd_merge_queue(["list", "alpha", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    held = payload["projects"]["alpha"]["held"]
    assert [h["pr"] for h in held] == [7], payload
    assert held[0]["reason"] == "defect-carrying head", payload

    data = kanban_live.attach_merge_queue(
        {}, registry_path=registry, state_path=path, now=time.time())
    line = kanban_live.merge_queue_line(data)
    assert line is not None
    assert "held #7" in line.plain, line.plain
    assert "defect-carrying head" in line.plain, line.plain

    with open(path, "rb") as f:
        assert (os.stat(path).st_mtime_ns, f.read()) == before
    assert sorted(os.listdir(home)) == before_dir


# ── post-push HEAD READ LAG ────────────────────────────────────────────────
#
# `gh pr view` keeps answering the PRE-push ``headRefOid`` for a few seconds
# after a push, and the queue's own ``_push_branch`` is the commonest push this
# file makes.  On 2.17.5 that lagging read was taken for "a worker pushed over
# us" twice — PR #742 at 00:53:49Z and #761 at 00:09:56Z, each one releasing a
# claim whose ~75-minute local gate had already passed — because a head that
# had simply not been served yet and a head someone else had moved looked
# identical to ``_settle_pushed_head``.  These tests pin the distinction:
# the sha we pushed FROM is re-polled and then HELD, only a THIRD sha releases.

def _lagging_head_views(pr, pre_push_sha, *, lag=None):
    """Per-PR ``FakeGh`` views whose post-push head stays on ``pre_push_sha``.

    The first read (the pre-push one ``process_entry`` makes) is the normal
    answer; after it GitHub reports the PRE-push sha for ``lag`` consecutive
    looks — the post-push one plus that many re-reads.  ``lag`` defaults to a
    whole budget (the first look + every re-read the queue is allowed), i.e.
    GitHub never catches up within the tick.
    """
    if lag is None:
        lag = 1 + max(1, int(mq.HEAD_READ_POLL_ATTEMPTS))
    stale = {"headRefOid": pre_push_sha}
    return {pr: [{}] + [dict(stale) for _ in range(max(0, int(lag)))]}


def test_a_lagging_head_read_after_our_push_is_repolled_not_released(
        tmp_path, monkeypatch):
    """GitHub serving the pre-push head is read lag: re-read, do not release."""
    monkeypatch.setattr(mq, "HEAD_READ_POLL_DELAY_S", 0.0)
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    # One lagging read, then GitHub catches up on the queue's first re-read.
    gh = FakeGh({31: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}},
                views=_lagging_head_views(31, pair["head"], lag=1))
    mq.add_to_queue("alpha", 31, path=_state(tmp_path))
    logged = []
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     log=lambda msg, *a, **k: logged.append(msg))

    assert result["processed"][0]["result"] == "merged", result
    assert gh.merged == [31]
    assert not any("head moved" in line for line in logged), logged
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == [] and ps["in_flight"] is None
    # pre-push read + lagging read + the re-read that caught up + post-merge.
    views = [argv for argv in gh.calls if argv[:3] == ["gh", "pr", "view"]]
    assert len(views) == 4, views


def test_a_head_read_that_never_catches_up_holds_the_claim(tmp_path,
                                                           monkeypatch):
    """Still the pre-push head after every re-read: hold, and merge next tick.

    The hold is what makes this cheap: the gate this claim carries has already
    passed, and the next tick re-reads the PR in place — it does not rebase and
    re-gate, which is the ~75 minutes the false release used to throw away.
    """
    monkeypatch.setattr(mq, "HEAD_READ_POLL_DELAY_S", 0.0)
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({32: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}},
                views=_lagging_head_views(32, pair["head"]))
    mq.add_to_queue("alpha", 32, path=_state(tmp_path))
    gate_calls = []

    def gate(command, worktree, timeout_s, **extra):
        gate_calls.append(command)
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", gate)
    logged = []
    first = mq.tick(state_path=_state(tmp_path),
                    root=str(tmp_path / "queue"), registry_path=registry,
                    gh_runner=gh, git_runner=subprocess.run,
                    log=lambda msg, *a, **k: logged.append(msg))

    assert first["processed"][0]["result"] == "ci-pending", first
    assert gh.merged == []
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"]["stage"] == "ci-pending"
    pushed = ps["in_flight"]["pushed_sha"]
    assert pushed and pushed != pair["head"]
    # The sha GitHub was reporting is carried on the claim, so a later tick that
    # reads it again recognises the SAME lag instead of releasing.
    assert ps["in_flight"]["pre_push_sha"] == pair["head"]
    assert ps["waiting"] == []
    assert any("holding the claim" in line for line in logged), logged
    assert not any("head moved" in line for line in logged), logged

    second = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     log=lambda msg, *a, **k: logged.append(msg))
    assert second["processed"][0]["result"] == "merged", second
    assert gh.merged == [32]
    assert len(gate_calls) == 1, "the held claim was re-gated"
    # It merges the SHA it pushed, not whatever the lagging read reported.
    assert gh.merge_calls[0][-1] == pushed, gh.merge_calls[0]


def test_a_head_still_stale_on_a_later_tick_holds_the_claim_too(tmp_path,
                                                                monkeypatch):
    """A lag longer than one tick must not release either — the budget does."""
    monkeypatch.setattr(mq, "HEAD_READ_POLL_DELAY_S", 0.0)
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({33: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}},
                views=_lagging_head_views(
                    33, pair["head"],
                    lag=2 * (1 + max(1, int(mq.HEAD_READ_POLL_ATTEMPTS)))))
    mq.add_to_queue("alpha", 33, path=_state(tmp_path))
    logged = []
    tick = lambda: mq.tick(  # noqa: E731 - one shape, three ticks
        state_path=_state(tmp_path), root=str(tmp_path / "queue"),
        registry_path=registry, gh_runner=gh, git_runner=subprocess.run,
        log=lambda msg, *a, **k: logged.append(msg))

    assert tick()["processed"][0]["result"] == "ci-pending"
    assert tick()["processed"][0]["result"] == "ci-pending"
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"]["stage"] == "ci-pending"
    assert ps["waiting"] == [] and gh.merged == []
    assert not any("head moved" in line for line in logged), logged

    assert tick()["processed"][0]["result"] == "merged"
    assert gh.merged == [33]


def test_a_third_sha_after_the_push_is_still_released(tmp_path, monkeypatch):
    """A head that is neither pushed nor pre-push IS a foreign push: release."""
    pair = _repo_pair(tmp_path)
    # A real sha on origin that is neither the head we started from nor the
    # head the queue pushed: what a worker pushing over the claim looks like.
    foreign = _git(pair["repo"], "rev-parse", "origin/main")
    assert foreign not in (pair["head"],)
    registry = _registry(tmp_path, pair["repo"], gates=["true"])
    gh = FakeGh({34: {"headRefName": pair["branch"],
                      "headRefOid": pair["head"]}},
                views={34: [{}, {"headRefOid": foreign}]})
    mq.add_to_queue("alpha", 34, path=_state(tmp_path))
    logged = []
    result = mq.tick(state_path=_state(tmp_path),
                     root=str(tmp_path / "queue"), registry_path=registry,
                     gh_runner=gh, git_runner=subprocess.run,
                     log=lambda msg, *a, **k: logged.append(msg))

    assert result["processed"][0]["result"] == "waiting", result
    assert gh.merged == []
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert [e["pr"] for e in ps["waiting"]] == [34]
    assert any("head moved from the pushed" in line for line in logged), logged
    # A third sha is decided at once: re-reading cannot make a real push go
    # away, and burning the poll budget on it only slows the queue down.
    views = [argv for argv in gh.calls if argv[:3] == ["gh", "pr", "view"]]
    assert len(views) == 2, views


# ── a red PR with no live card gets a fix card (t_30dfe9fe) ────────────────
#
# Hermes closes a card (done) as soon as its PR opens, so the queue's red-gate
# handback used to comment on a done card and reassign it to nobody: #734 and
# #767 sat stranded until an operator carded them by hand. The handback now
# cuts a fix card on the project's board and remembers its id, so the next
# failure of the same PR comments on that card instead of cutting another.

class _FakeKanbanRunner:
    """The two hermes verbs the handback may use: show and create."""

    def __init__(self, cards=None):
        self.cards = {card: dict(info) for card, info in (cards or {}).items()}
        self.calls = []
        self.bodies = {}
        self._next = 0

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        verb = argv[4]
        if verb == "show":
            card = argv[5]
            info = self.cards.get(card)
            if info is None:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr=f"no such task: {card}")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"task": {"id": card, **info}}))
        if verb == "create":
            self._next += 1
            card = f"t_fix_{self._next}"
            body_path = argv[argv.index("--body-file") + 1]
            self.bodies[card] = pathlib.Path(body_path).read_text(
                encoding="utf-8")
            initial = (argv[argv.index("--initial-status") + 1]
                       if "--initial-status" in argv else "ready")
            self.cards[card] = {
                "status": initial,
                "assignee": argv[argv.index("--assignee") + 1]}
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"id": card, "status": initial}))
        if verb == "block":
            card = argv[5]
            info = self.cards.setdefault(card, {})
            info.update(status="blocked",
                        block_kind=argv[argv.index("--kind") + 1])
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                {"id": card}))
        raise AssertionError(f"unexpected kanban call: {argv}")


def _gh_comment_ok(argv, cwd=None, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def _handed(seen):
    def handback(card, assignee, body):
        seen.append((card, assignee, body))
        return True
    return handback


_QUEUE_RED_BODY = ("Merge queue: gate failed after rebasing onto origin/main.\n\n"
                   "Gate: bash scripts/run_tests.sh\n"
                   "Attempt: 1\n\n"
                   "FAILED hscc_daemon/tests/test_x.py::test_y - AssertionError\n\n"
                   "Resolve it as this card's worker:\n"
                   "1. In your own card worktree: `git fetch origin && git rebase "
                   "origin/main`.\n")


def _red_handback(tmp_path, entry, runner, handed, *, cfg=None, **over):
    cfg = cfg or {"name": "alpha", "repo": "/tmp/project", "board": "alpha"}
    over.setdefault("reason", "gate failed")
    over.setdefault("state_path", str(tmp_path / "merge-queue-state.json"))
    over.setdefault("project", "alpha")
    over.setdefault("kanban_runner", runner)
    mq._handback(entry, cfg, _QUEUE_RED_BODY, _handed(handed),
                 _gh_comment_ok, **over)


def test_a_red_handback_with_no_card_creates_a_fix_card(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner()
    entry = {"pr": 12, "card": None, "assignee": "worker",
             "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, entry, runner, handed)

    assert [c[4] for c in runner.calls] == ["create"], runner.calls
    create = runner.calls[0]
    assert create[5] == "PR #12 red in merge queue: gate failed"
    assert create[create.index("--assignee") + 1] == "worker"
    assert create[create.index("--priority") + 1] == "2"
    # No fix_card block in this registry row: the card is dispatchable.
    assert "--initial-status" not in create
    assert not create[5].startswith("[opencode]")
    body = runner.bodies["t_fix_1"]
    # The queue's failure evidence leads; the rule is on the card.
    assert "FAILED hscc_daemon/tests/test_x.py::test_y" in body
    assert "hscc merge-queue add alpha 12" in body
    assert "no hand-merge" in body
    assert "wt/t_card" in body
    # The comment/reassign went to the NEW card, not to a cardless no-op.
    assert handed[0][0] == "t_fix_1" and handed[0][1] == "worker"
    assert entry["card"] == "t_fix_1"
    state = mq.load_state(str(tmp_path / "merge-queue-state.json"))
    assert state["projects"]["alpha"]["fix_cards"]["12"]["card"] == "t_fix_1"


def test_a_red_handback_on_a_done_card_creates_a_fix_card(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner({"t_done": {"status": "done",
                                           "assignee": "previous-worker"}})
    entry = {"pr": 13, "card": "t_done", "assignee": None,
             "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, entry, runner, handed)

    assert [c[4] for c in runner.calls] == ["show", "create"], runner.calls
    # The previous card's assignee is the target when the entry has none.
    create = runner.calls[1]
    assert create[create.index("--assignee") + 1] == "previous-worker"
    assert handed[0][0] == "t_fix_1"
    assert entry["card"] == "t_fix_1"


def test_a_red_handback_on_an_open_card_only_comments(tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner({"t_open": {"status": "running",
                                           "assignee": "open-worker"}})
    entry = {"pr": 14, "card": "t_open", "assignee": None,
             "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, entry, runner, handed)

    assert [c[4] for c in runner.calls] == ["show"], runner.calls
    assert handed == [("t_open", "open-worker", _QUEUE_RED_BODY)]
    assert entry["card"] == "t_open"


def test_a_second_red_handback_reuses_the_created_fix_card(tmp_path,
                                                           monkeypatch):
    """At most one open fix card per PR, even across a re-added entry."""
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner({"t_done": {"status": "done",
                                           "assignee": "worker"}})
    first = {"pr": 15, "card": None, "assignee": "worker", "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, first, runner, handed)
    assert [c[4] for c in runner.calls] == ["create"], runner.calls

    # The red gate dropped the entry; the operator re-adds the PR naming the
    # ORIGINAL (still done) card. The open fix card from the map must win.
    second = {"pr": 15, "card": "t_done", "assignee": None,
              "branch": "wt/t_card"}
    _red_handback(tmp_path, second, runner, handed)

    assert [c[4] for c in runner.calls] == ["create", "show", "show"], \
        runner.calls
    assert handed[-1][0] == "t_fix_1" and handed[-1][1] == "worker"
    assert second["card"] == "t_fix_1"
    state = mq.load_state(str(tmp_path / "merge-queue-state.json"))
    assert state["projects"]["alpha"]["fix_cards"]["15"]["card"] == "t_fix_1"


def test_a_gate_timeout_never_creates_a_fix_card(tmp_path, monkeypatch):
    """A timeout is not a verdict on the code: no worker is sent to fix it."""
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner()
    entry = {"pr": 16, "card": "t_done", "assignee": "worker",
             "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, entry, runner, handed, reason=None, create=False)

    assert runner.calls == []
    assert handed[0][0] == "t_done"


def test_a_board_aware_handback_is_told_the_projects_board(tmp_path,
                                                           monkeypatch):
    """The fix card lives on the project's board, so the comment must too."""
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner({"t_open": {"status": "running",
                                           "assignee": "worker"}})
    seen = []

    def handback(card, assignee, body, *, board=None):
        seen.append((card, board))
        return True

    mq._handback({"pr": 18, "card": "t_open"},
                 {"name": "alpha", "repo": "/tmp/project", "board": "alpha"},
                 _QUEUE_RED_BODY, handback, _gh_comment_ok,
                 state_path=str(tmp_path / "state.json"), project="alpha",
                 reason="gate failed", kanban_runner=runner)

    assert seen == [("t_open", "alpha")]


def test_default_handback_addresses_every_verb_on_the_named_board(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    monkeypatch.setattr(mq.subprocess, "run", run)

    assert mq.default_handback("t_x", "worker", "msg", board="alpha") is True
    assert calls[0] == ["/bin/hermes", "kanban", "--board", "alpha",
                        "comment", "t_x", "msg"]
    assert calls[1] == ["/bin/hermes", "kanban", "--board", "alpha",
                        "reassign", "t_x", "worker", "--reclaim"]
    assert calls[2] == ["/bin/hermes", "kanban", "--board", "alpha",
                        "unblock", "t_x"]


def test_an_unreadable_board_does_not_cut_a_duplicate(tmp_path, monkeypatch):
    """A transient show error is not proof the card is gone."""
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")

    class LockedBoard(_FakeKanbanRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="database is locked")

    runner = LockedBoard({"t_open": {"status": "running"}})
    entry = {"pr": 17, "card": "t_open", "assignee": "worker",
             "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, entry, runner, handed)

    assert [c[4] for c in runner.calls] == ["show"], runner.calls
    assert handed[0][0] == "t_open"


def _gh_comment_runner(seen):
    def run(argv, cwd=None, **kwargs):
        argv = list(argv)
        if argv[:3] == ["gh", "pr", "comment"]:
            seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def _parked_cfg(**fix_card):
    return {"name": "alpha", "repo": "/tmp/project", "board": "alpha",
            "fix_card": {"assignee": "coder", "park": True, **fix_card}}


def test_a_parked_fix_card_is_blocked_typed_and_assigned_to_the_pane(
        tmp_path, monkeypatch):
    """park=true cuts an [opencode] card no cluster worker can claim."""
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner()
    entry = {"pr": 19, "card": None, "assignee": "worker",
             "branch": "wt/t_card"}
    handed, comments = [], []

    mq._handback(entry, _parked_cfg(), _QUEUE_RED_BODY, _handed(handed),
                 _gh_comment_runner(comments),
                 reason="gate failed",
                 state_path=str(tmp_path / "merge-queue-state.json"),
                 project="alpha", kanban_runner=runner)

    assert [c[4] for c in runner.calls] == ["create", "block"], runner.calls
    create = runner.calls[0]
    assert create[5] == ("[opencode] PR #19 red in merge queue: gate failed")
    assert create[create.index("--assignee") + 1] == "coder"
    assert create[create.index("--initial-status") + 1] == "blocked"
    block = runner.calls[1]
    assert "t_fix_1" in block and block.index("--kind") < block.index("t_fix_1")
    assert block[block.index("--kind") + 1] == "capability"
    assert handed[0][0] == "t_fix_1" and handed[0][1] == "coder"
    assert entry["card"] == "t_fix_1"
    # The PR's handback comment names the parked card the driver must run.
    assert comments, "no PR comment"
    body = comments[-1][comments[-1].index("--body") + 1]
    assert "t_fix_1" in body and "OpenCode" in body
    state = mq.load_state(str(tmp_path / "merge-queue-state.json"))
    assert state["projects"]["alpha"]["fix_cards"]["19"]["card"] == "t_fix_1"


def test_a_parked_fix_card_reuses_an_open_card_without_cutting_another(
        tmp_path, monkeypatch):
    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    runner = _FakeKanbanRunner({"t_open": {"status": "blocked",
                                           "assignee": "coder",
                                           "block_kind": "capability"}})
    entry = {"pr": 20, "card": "t_open", "assignee": None,
             "branch": "wt/t_card"}
    handed = []

    _red_handback(tmp_path, entry, runner, handed, cfg=_parked_cfg())

    assert [c[4] for c in runner.calls] == ["show"], runner.calls
    assert handed[0][0] == "t_open" and handed[0][1] == "coder"
    assert entry["card"] == "t_open"


def test_the_default_handback_never_unblocks_a_parked_card(monkeypatch):
    """An unblock would hand the parked card straight to the cluster."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mq, "_hermes_cli", lambda: "/bin/hermes")
    monkeypatch.setattr(mq.subprocess, "run", run)

    assert mq.default_handback("t_x", "coder", "msg", board="alpha",
                               parked=True) is True
    verbs = [call[4] for call in calls]
    assert verbs == ["comment", "reassign"], calls
    assert not any("unblock" in call for call in calls), calls
