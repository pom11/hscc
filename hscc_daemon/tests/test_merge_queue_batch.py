"""MQ batch gating: opt-in ``merge_queue.batch_size``.

``batch_size: 1`` must stay the byte-for-byte single-PR path. With N>1 the
queue gates the combined stack once, then merges each PR through the ordinary
per-PR rebase/push/CI/merge path. A stack conflict drops only that PR, a red
combined gate falls back to singles, and a restart releases every member.

Every test builds a throwaway bare origin + clone under ``tmp_path`` and
injects the ``gh`` runner, so no test touches a real repository or network.
"""

import subprocess

from hscc_daemon import merge_queue as mq
from hscc_daemon.tests.test_merge_queue import (
    FakeGh,
    _git,
    _new_branch,
    _repo_pair,
    _state,
)

# ── helpers ────────────────────────────────────────────────────────────────

def _registry(tmp_path, repo, *, batch_size=1, enabled=True, gates=None):
    path = tmp_path / "registry-batch.yaml"
    body = {"projects": [{
        "name": "alpha",
        "repo": str(repo),
        "merge_queue": {
            "enabled": enabled,
            "gates": list(gates or ["true"]),
            "batch_size": batch_size,
        },
    }]}
    import yaml
    path.write_text(yaml.safe_dump(body, sort_keys=False))
    return str(path)


def _heads(pair, extra):
    """``FakeGh`` PR -> head map for the pair branch plus extra branches."""
    heads = {7: {"headRefName": pair["branch"], "headRefOid": pair["head"]}}
    for pr, (branch, head) in extra.items():
        heads[pr] = {"headRefName": branch, "headRefOid": head}
    return heads


def _enqueue(path, prs):
    for pr in prs:
        mq.add_to_queue("alpha", pr, path=path)


def _tick(tmp_path, registry, gh, *, log=None):
    return mq.tick(
        state_path=_state(tmp_path),
        root=str(tmp_path / "queue"),
        registry_path=registry,
        gh_runner=gh,
        git_runner=subprocess.run,
        log=log or (lambda *a, **k: None),
    )


def _collect(lines):
    return lambda message, level="INFO": lines.append(message)


def _count_gates(monkeypatch):
    """Count gate runs; the real command would be ``true`` in these fixtures."""
    runs = []

    def fake(command, worktree, timeout_s, **kwargs):
        runs.append(command)
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", fake)
    return runs


def _conflicting_branch(repo, name, line):
    """A branch off main that rewrites the SAME shared line as its sibling."""
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", name)
    (repo / "shared.txt").write_text(f"one\n{line}\nthree\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", name)
    _git(repo, "push", "-u", "origin", name)
    return _git(repo, "rev-parse", "HEAD")


# ── batch_size: 1 is today's path ──────────────────────────────────────────

def test_batch_size_one_keeps_one_pr_per_tick(tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    second = _new_branch(pair["repo"], "wt/t_second")
    third = _new_branch(pair["repo"], "wt/t_third")
    registry = _registry(tmp_path, pair["repo"], batch_size=1)
    gh = FakeGh(_heads(pair, {
        8: ("wt/t_second", second),
        9: ("wt/t_third", third),
    }))
    gates = _count_gates(monkeypatch)
    _enqueue(_state(tmp_path), [7, 8, 9])

    result = _tick(tmp_path, registry, gh)

    assert [p["pr"] for p in result["processed"]] == [7], result
    assert gh.merged == [7]
    assert len(gates) == 1
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [8, 9]
    assert ps["in_flight"] is None


# ── three green -> one gate run, three merges in FIFO order ────────────────

def test_three_green_merge_in_fifo_order_after_one_gate_run(
        tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    second = _new_branch(pair["repo"], "wt/t_second")
    third = _new_branch(pair["repo"], "wt/t_third")
    registry = _registry(tmp_path, pair["repo"], batch_size=3)
    gh = FakeGh(_heads(pair, {
        8: ("wt/t_second", second),
        9: ("wt/t_third", third),
    }))
    gates = _count_gates(monkeypatch)
    lines = []
    _enqueue(_state(tmp_path), [7, 8, 9])

    result = _tick(tmp_path, registry, gh, log=_collect(lines))

    assert [p["pr"] for p in result["processed"]] == [7, 8, 9], result
    assert [p["result"] for p in result["processed"]] == [
        "merged", "merged", "merged"]
    assert gh.merged == [7, 8, 9]
    assert len(gates) == 1, gates
    joined = "\n".join(lines)
    assert "batch start: #7 #8 #9" in joined
    assert "batch green" in joined
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == []
    assert ps["in_flight"] is None
    assert ps["last"]["result"] == "merged"


# ── a stack conflict drops only that PR; the rest still merge ──────────────

def test_stack_conflict_drops_only_that_pr(tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    eighth = _conflicting_branch(pair["repo"], "wt/t_eight", "eight")
    ninth = _conflicting_branch(pair["repo"], "wt/t_nine", "nine")
    registry = _registry(tmp_path, pair["repo"], batch_size=3)
    gh = FakeGh(_heads(pair, {
        8: ("wt/t_eight", eighth),
        9: ("wt/t_nine", ninth),
    }))
    _count_gates(monkeypatch)
    _enqueue(_state(tmp_path), [8, 9, 7])

    result = _tick(tmp_path, registry, gh)

    by_pr = {p["pr"]: p for p in result["processed"]}
    # The stack conflict is decided before the green members are merged.
    assert [p["pr"] for p in result["processed"]] == [9, 8, 7], result
    assert by_pr[8]["result"] == "merged"
    assert by_pr[9]["result"] == "conflict"
    assert "shared.txt" in by_pr[9]["conflicted"]
    assert by_pr[7]["result"] == "merged"
    # The conflicting PR alone is handed back; the rest still merge.
    assert gh.merged == [8, 7]
    assert any(call[3] == "9" for call in gh.comments)
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["queue"] == [] and ps["waiting"] == []
    assert ps["in_flight"] is None


# ── red combined gate -> singles fallback names the culprit ────────────────

def test_red_batch_falls_back_to_singles_and_names_the_culprit(
        tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    for name in ("wt/t_second", "wt/t_third"):
        _new_branch(pair["repo"], name)
    second = _git(pair["repo"], "rev-parse", "wt/t_second")
    third = _git(pair["repo"], "rev-parse", "wt/t_third")
    registry = _registry(tmp_path, pair["repo"], batch_size=3)
    gh = FakeGh(_heads(pair, {
        8: ("wt/t_second", second),
        9: ("wt/t_third", third),
    }))
    calls = []

    def fake_gate(command, worktree, timeout_s, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return "fail", "FAILED tests/test_bad.py::test_bad\n1 failed"
        message = subprocess.run(
            ["git", "-C", str(worktree), "log", "-1", "--format=%s"],
            capture_output=True, text=True).stdout.strip()
        if message == "wt/t_second":
            return "fail", "FAILED tests/test_bad.py::test_bad\n1 failed"
        return "pass", ""

    monkeypatch.setattr(mq, "_run_gate", fake_gate)
    lines = []
    _enqueue(_state(tmp_path), [7, 8, 9])

    result = _tick(tmp_path, registry, gh, log=_collect(lines))

    assert [p["pr"] for p in result["processed"]] == [7, 8, 9], result
    assert [p["result"] for p in result["processed"]] == [
        "merged", "red-gate", "merged"]
    assert gh.merged == [7, 9]
    joined = "\n".join(lines)
    assert "batch red -> singles" in joined
    assert any(call[3] == "8" for call in gh.comments)
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert ps["in_flight"] is None
    assert ps["last"]["result"] == "merged"


# ── restart mid-batch releases every member back to the FIFO ───────────────

def test_restart_mid_batch_releases_every_member(tmp_path, monkeypatch):
    pair = _repo_pair(tmp_path)
    registry = _registry(tmp_path, pair["repo"], batch_size=3,
                         enabled=False)
    _enqueue(_state(tmp_path), [7, 8, 9])

    with mq._file_lock():
        doc = mq.load_state(_state(tmp_path))
        ps = mq._project_state(doc, "alpha")
        members = [ps["queue"].pop(0) for _ in range(3)]
        ps["in_flight"] = {
            "batch": True,
            "stage": "gating",
            "pr": 7,
            "prs": members,
            "pgid": 424242,
        }
        mq.save_state(doc, _state(tmp_path))

    reclaimed = []
    monkeypatch.setattr(
        mq, "_reclaim_orphan_gate",
        lambda flight, **kwargs: reclaimed.append(dict(flight)) or False)

    result = _tick(tmp_path, registry, FakeGh({}))

    assert result["recovered"] == ["alpha"], result
    assert reclaimed and reclaimed[0]["batch"] is True
    ps = mq.load_state(_state(tmp_path))["projects"]["alpha"]
    assert [e["pr"] for e in ps["queue"]] == [7, 8, 9]
    assert ps["in_flight"] is None
    assert result["processed"] == []
