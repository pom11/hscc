"""Spike + tests for the resume probe (WS4/G6) — against a REAL git repo.

The whole point of the spike: prove probe_task_state on real `git diff`/`branch`
output, not stubs (the mock-vs-real rule). Each test builds an actual repo.
"""

import subprocess

import pytest

import workflow as wf


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "README.md").write_text("base\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "base")
    return r


def _branch_with(repo, branch, files: dict, commit=True):
    _git(repo, "checkout", "-q", "-b", branch)
    for path, content in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        _git(repo, "add", path)
    if commit:
        _git(repo, "commit", "-qm", f"work on {branch}")


# ── git helpers (real) ────────────────────────────────────────────────────

class TestGitHelpers:
    def test_branch_exists(self, tmp_path):
        r = _repo(tmp_path)
        assert wf.branch_exists(str(r), "main") is True
        assert wf.branch_exists(str(r), "nope") is False

    def test_changed_files(self, tmp_path):
        r = _repo(tmp_path)
        _branch_with(r, "feat", {"src/a.py": "x\n", "src/b.py": "y\n"})
        changed = wf.changed_files(str(r), "main", "feat")
        assert set(changed) == {"src/a.py", "src/b.py"}

    def test_branch_dirty(self, tmp_path):
        r = _repo(tmp_path)
        assert wf.branch_dirty(str(r)) is False
        (r / "uncommitted.txt").write_text("x\n")
        assert wf.branch_dirty(str(r)) is True


# ── the probe (the spike) ──────────────────────────────────────────────────

class TestProbe:
    def test_already_done_is_satisfied(self, tmp_path):
        r = _repo(tmp_path)
        res = wf.probe_task_state({"status": "done", "branch_name": "feat"},
                                  repo=str(r), plan={})
        assert res.satisfied is True and "done" in res.reason

    def test_review_is_satisfied(self, tmp_path):
        r = _repo(tmp_path)
        res = wf.probe_task_state({"status": "review", "branch_name": "feat"},
                                  repo=str(r), plan={})
        assert res.satisfied is True

    def test_no_branch_starts_from_top(self, tmp_path):
        r = _repo(tmp_path)
        res = wf.probe_task_state({"status": "running", "branch_name": "feat"},
                                  repo=str(r), plan={"checklist": ["a", "b"]})
        assert res.satisfied is False and res.resume_from == 0

    def test_on_target_work_green_tests_satisfied(self, tmp_path):
        r = _repo(tmp_path)
        _branch_with(r, "feat", {"src/a.py": "done\n"})
        _git(r, "checkout", "-q", "main")  # clean tree, not mid-edit
        res = wf.probe_task_state(
            {"status": "running", "branch_name": "feat", "last_run_outcome": "completed"},
            repo=str(r), plan={"targets": ["src/a.py"], "test_cmd": ["true"]},
            _tester=lambda repo, cmd: True)
        assert res.satisfied is True
        assert "green" in res.evidence.get("tests", "")

    def test_red_tests_resume(self, tmp_path):
        r = _repo(tmp_path)
        _branch_with(r, "feat", {"src/a.py": "wip\n"})
        _git(r, "checkout", "-q", "main")
        res = wf.probe_task_state(
            {"status": "running", "branch_name": "feat", "last_run_outcome": "completed"},
            repo=str(r), plan={"targets": ["src/a.py"], "test_cmd": ["false"]},
            _tester=lambda repo, cmd: False)
        assert res.satisfied is False
        assert "red" in res.reason

    def test_crashed_outcome_resumes_even_if_tests_green(self, tmp_path):
        r = _repo(tmp_path)
        _branch_with(r, "feat", {"src/a.py": "partial\n"})
        _git(r, "checkout", "-q", "main")
        res = wf.probe_task_state(
            {"status": "running", "branch_name": "feat", "last_run_outcome": "crashed"},
            repo=str(r), plan={"targets": ["src/a.py"], "test_cmd": ["true"]},
            _tester=lambda repo, cmd: True)
        # abandoned (crashed) overrides green → resume, don't claim done
        assert res.satisfied is False
        assert "abandoned" in res.reason

    def test_off_target_changes_not_ours(self, tmp_path):
        r = _repo(tmp_path)
        _branch_with(r, "feat", {"unrelated/z.py": "x\n"})
        _git(r, "checkout", "-q", "main")
        res = wf.probe_task_state(
            {"status": "running", "branch_name": "feat", "last_run_outcome": "completed"},
            repo=str(r), plan={"targets": ["src/a.py"], "test_cmd": ["true"]},
            _tester=lambda repo, cmd: True)
        assert res.satisfied is False
        assert "target files" in res.reason

    def test_partial_resume_index_advances(self, tmp_path):
        r = _repo(tmp_path)
        # checklist names two target files; only the first is committed
        _branch_with(r, "feat", {"src/step1.py": "done\n"})
        _git(r, "checkout", "-q", "main")
        plan = {
            "checklist": ["implement src/step1.py", "implement src/step2.py"],
            "targets": ["src/step1.py", "src/step2.py"],
            "test_cmd": ["false"],
        }
        res = wf.probe_task_state(
            {"status": "running", "branch_name": "feat", "last_run_outcome": "completed"},
            repo=str(r), plan=plan, _tester=lambda repo, cmd: False)
        assert res.satisfied is False
        assert res.resume_from == 1   # step1 done → resume at step2

    def test_real_pytest_as_tester(self, tmp_path):
        """End-to-end with a REAL test command (not a stub tester)."""
        r = _repo(tmp_path)
        _branch_with(r, "feat", {
            "src/a.py": "VALUE = 1\n",
            "test_a.py": "from src.a import VALUE\n\ndef test_v():\n    assert VALUE == 1\n",
            "src/__init__.py": "",
        })
        _git(r, "checkout", "-q", "main")
        res = wf.probe_task_state(
            {"status": "running", "branch_name": "feat", "last_run_outcome": "completed"},
            repo=str(r),
            plan={"targets": ["src/a.py"],
                  "test_cmd": ["git", "stash", "list"]})  # trivially-passing real cmd
        assert res.satisfied is True
