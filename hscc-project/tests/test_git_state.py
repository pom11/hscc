"""Tests for flightdeck.core.git_state — fixture-only, no real git.

Every subprocess call is routed through an injectable ``_run`` runner backed by
a :class:`FakeGit` fixture that simulates a git repository. None of these tests
executes a real ``git`` binary, touches a real repo, or reaches the network, so
the suite stays fast and deterministic.
"""

import subprocess

from flightdeck.core import git_state


class FakeGit:
    """A fixture subprocess runner that answers git commands from canned state.

    Simulates a git repo whose only knowledge is the knobs you construct it
    with. Interprets the argv list produced by ``git_state`` and returns a
    process-like object; raises on any command it does not understand so a
    surprising call is caught loudly rather than silently guessed at.
    """

    def __init__(
        self,
        *,
        is_repo=True,
        merged=None,
        branch_exists=True,
        commits_ahead=3,
        porcelain=(),
        branch="feature/x",
        detached=False,
        commit_ts=1000,
        head="abc123def456abc123def456abc123def456abc123de",
    ):
        self.is_repo = is_repo
        self.merged = merged
        self.branch_exists = branch_exists
        self.commits_ahead = commits_ahead
        self.porcelain = list(porcelain)
        self.branch = branch
        self.detached = detached
        self.commit_ts = commit_ts
        self.head = head

    def _proc(self, cmd, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, repo):
        if not self.is_repo:
            return self._proc(cmd, 128, "", "fatal: not a git repository")
        sub = cmd[1]

        if sub == "merge-base":
            # git merge-base --is-ancestor <branch> <base>
            assert cmd[2] == "--is-ancestor"
            return self._proc(cmd, 0 if self.merged else 1)

        if sub == "rev-list":
            # git rev-list --count <base>..<branch>
            assert cmd[2] == "--count"
            return self._proc(cmd, 0, str(self.commits_ahead))

        if sub == "status":
            # git status --porcelain
            assert cmd[2] == "--porcelain"
            return self._proc(cmd, 0, "\n".join(self.porcelain) + (
                "\n" if self.porcelain else ""))

        if sub == "log":
            # git log -1 --format=%ct [branch]
            assert cmd[2] == "-1"
            assert cmd[3] == "--format=%ct"
            if not self.branch_exists and len(cmd) > 4:
                return self._proc(cmd, 128, "", "fatal: ambiguous argument")
            return self._proc(cmd, 0, str(self.commit_ts))

        if sub == "rev-parse":
            if "--verify" in cmd:
                # git rev-parse --verify --quiet <branch>
                return self._proc(
                    cmd, 0 if self.branch_exists else 1,
                    self.branch if self.branch_exists else "")
            if "--abbrev-ref" in cmd:
                out = "HEAD" if self.detached else self.branch
                return self._proc(cmd, 0, out)
            # plain: git rev-parse HEAD
            return self._proc(cmd, 0, self.head)

        raise AssertionError("FakeGit does not know this command: %r" % (cmd,))


# A shared, stateless non-repo runner used by the degradation tests below.
non_repo = FakeGit(is_repo=False)


# --------------------------------------------------------------------------- #
# is_merged — the "did the work land" question. Direction and exit-code matter.
# --------------------------------------------------------------------------- #

def test_is_merged_true_when_branch_is_ancestor_of_base():
    fake = FakeGit(merged=True)
    assert git_state.is_merged("/repo", "feature/x", "main", _run=fake) is True


def test_is_merged_false_when_branch_not_ancestor():
    fake = FakeGit(merged=False)
    assert git_state.is_merged("/repo", "feature/x", "main", _run=fake) is False


def test_is_merged_default_base_is_main():
    # Calling with only repo+branch must interrogate ancestry against "main".
    fake = FakeGit(merged=True)
    assert git_state.is_merged("/repo", "feature/x", _run=fake) is True


def test_is_merged_uses_exact_ancestor_command_and_branch_base_order():
    # Pins the command construction and the direction: it must be
    # `merge-base --is-ancestor <branch> <base>`, never the reverse — stating
    # "branch is ancestor of base" is what means the work landed.

    captured = {}

    def recording_runner(cmd, repo):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert git_state.is_merged("/repo", "feature/x", "main", _run=recording_runner)
    assert captured["cmd"] == [
        "git", "merge-base", "--is-ancestor", "feature/x", "main",
    ]


def test_is_merged_false_for_nonexistent_branch():
    fake = FakeGit(is_repo=True, merged=False)
    assert git_state.is_merged("/repo", "ghost", "main", _run=fake) is False


def test_is_merged_false_for_non_repo():
    assert git_state.is_merged("/no/repo", "feature/x", "main", _run=non_repo) is False


# --------------------------------------------------------------------------- #
# branch_exists
# --------------------------------------------------------------------------- #

def test_branch_exists_true():
    assert git_state.branch_exists("/repo", "feature/x", _run=FakeGit(branch_exists=True)) is True


def test_branch_exists_false_for_unknown_branch():
    assert git_state.branch_exists("/repo", "ghost", _run=FakeGit(branch_exists=False)) is False


def test_branch_exists_false_for_non_repo():
    assert git_state.branch_exists("/no/repo", "feature/x", _run=non_repo) is False


# --------------------------------------------------------------------------- #
# commits_ahead
# --------------------------------------------------------------------------- #

def test_commits_ahead_returns_count():
    assert git_state.commits_ahead("/repo", "feature/x", _run=FakeGit(commits_ahead=5)) == 5


def test_commits_ahead_zero_is_stall_signal():
    assert git_state.commits_ahead("/repo", "feature/x", _run=FakeGit(commits_ahead=0)) == 0


def test_commits_ahead_zero_for_non_repo():
    assert git_state.commits_ahead("/no/repo", "feature/x", _run=non_repo) == 0


# --------------------------------------------------------------------------- #
# is_dirty
# --------------------------------------------------------------------------- #

def test_is_dirty_counts_modified_files():
    fake = FakeGit(porcelain=[" M file_a.py", " M file_b.py", "?? new.txt"])
    assert git_state.is_dirty("/repo", _run=fake) == 3


def test_is_dirty_clean_tree_is_zero():
    assert git_state.is_dirty("/repo", _run=FakeGit(porcelain=[])) == 0


def test_is_dirty_counts_untracked_and_deleted():
    fake = FakeGit(porcelain=["?? brand_new.py", " D gone.py"])
    assert git_state.is_dirty("/repo", _run=fake) == 2


def test_is_dirty_zero_for_non_repo():
    assert git_state.is_dirty("/no/repo", _run=non_repo) == 0


# --------------------------------------------------------------------------- #
# current_branch
# --------------------------------------------------------------------------- #

def test_current_branch_returns_name():
    assert git_state.current_branch("/repo", _run=FakeGit(branch="main")) == "main"


def test_current_branch_detached_returns_head():
    assert git_state.current_branch("/repo", _run=FakeGit(branch="main", detached=True)) == "HEAD"


def test_current_branch_none_for_non_repo():
    assert git_state.current_branch("/no/repo", _run=non_repo) is None


# --------------------------------------------------------------------------- #
# last_commit_age_seconds (injected clock)
# --------------------------------------------------------------------------- #

def test_last_commit_age_uses_injected_clock():
    fake = FakeGit(commit_ts=1000)
    # _now returns 1500 -> age 500s
    assert git_state.last_commit_age_seconds("/repo", _run=fake, _now=lambda: 1500) == 500


def test_last_commit_age_for_specific_branch():
    fake = FakeGit(commit_ts=500)

    def now():
        return 1000

    assert git_state.last_commit_age_seconds("/repo", "feature/x", _run=fake, _now=now) == 500


def test_last_commit_age_defaults_to_head():
    # No branch argument -> command is `git log -1 --format=%ct` with no ref.
    captured = {}

    def recording_runner(cmd, repo):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "1000", "")

    assert git_state.last_commit_age_seconds("/repo", _run=recording_runner, _now=lambda: 1500) == 500
    assert captured["cmd"] == ["git", "log", "-1", "--format=%ct"]


def test_last_commit_age_clamps_future_commit_to_zero():
    fake = FakeGit(commit_ts=9000)
    assert git_state.last_commit_age_seconds("/repo", _run=fake, _now=lambda: 1000) == 0


def test_last_commit_age_none_for_non_repo():
    assert git_state.last_commit_age_seconds("/no/repo", _run=non_repo, _now=lambda: 1000) is None


def test_last_commit_age_none_for_nonexistent_branch():
    fake = FakeGit(branch_exists=False)
    assert git_state.last_commit_age_seconds("/repo", "ghost", _run=fake, _now=lambda: 1000) is None


# --------------------------------------------------------------------------- #
# head_sha
# --------------------------------------------------------------------------- #

def test_head_sha_returns_sha():
    fake = FakeGit(head="deadbeef")
    assert git_state.head_sha("/repo", _run=fake) == "deadbeef"


def test_head_sha_none_for_non_repo():
    assert git_state.head_sha("/no/repo", _run=non_repo) is None


# --------------------------------------------------------------------------- #
# commit_subjects — the branch's OWN work
# --------------------------------------------------------------------------- #

def _log_runner(lines):
    """Runner answering `git log <base>..<branch> --pretty=%s` with ``lines``."""

    def runner(cmd, repo):
        assert cmd[1] == "log"
        return subprocess.CompletedProcess(cmd, 0, "\n".join(lines), "")

    return runner


def test_commit_subjects_returns_branch_commits():
    runner = _log_runner(["feat: one", "feat: two"])
    assert git_state.commit_subjects("/repo", "feature/x", _run=runner) == [
        "feat: one",
        "feat: two",
    ]


def test_commit_subjects_empty_for_unstarted_branch():
    runner = _log_runner([])
    assert git_state.commit_subjects("/repo", "feature/x", _run=runner) == []


def test_commit_subjects_empty_for_non_repo():
    assert git_state.commit_subjects("/no/repo", "feature/x", _run=non_repo) == []


def test_commit_subjects_command_shape():
    captured = {}

    def recording_runner(cmd, repo):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    git_state.commit_subjects("/repo", "feature/x", _run=recording_runner)
    assert captured["cmd"] == [
        "git", "log", "main..feature/x", "--pretty=%s",
    ]


# --------------------------------------------------------------------------- #
# uncommitted_files — the worktree's dirty files
# --------------------------------------------------------------------------- #

def test_uncommitted_files_lists_paths():
    fake = FakeGit(porcelain=[" M a.py", " M b.py", "?? new.txt"])
    assert git_state.uncommitted_files("/repo", _run=fake) == ["a.py", "b.py", "new.txt"]


def test_uncommitted_files_clean_tree_is_empty():
    assert git_state.uncommitted_files("/repo", _run=FakeGit(porcelain=[])) == []


def test_uncommitted_files_empty_for_non_repo():
    assert git_state.uncommitted_files("/no/repo", _run=non_repo) == []


def test_uncommitted_files_rename_uses_destination_path():
    fake = FakeGit(porcelain=["R  old.py -> new.py"])
    assert git_state.uncommitted_files("/repo", _run=fake) == ["new.py"]


# --------------------------------------------------------------------------- #
# Graceful degradation with the REAL runner on a path that is not a repo.
# --------------------------------------------------------------------------- #

def test_real_runner_degrades_on_non_existent_path():
    # Use the default (real) subprocess runner against a directory that cannot
    # possibly be a git repo. Must return sentinels, never raise.
    repo = "/nonexistent/flightdeck/test/no/repo/here"
    assert git_state.is_merged(repo, "feature/x") is False
    assert git_state.branch_exists(repo, "feature/x") is False
    assert git_state.commits_ahead(repo, "feature/x") == 0
    assert git_state.is_dirty(repo) == 0
    assert git_state.current_branch(repo) is None
    assert git_state.last_commit_age_seconds(repo) is None
    assert git_state.head_sha(repo) is None


# --------------------------------------------------------------------------- #
# remote_url — the installed tool's own upstream (self-update)
# --------------------------------------------------------------------------- #

def test_remote_url_returns_url():
    def runner(cmd, repo):
        assert cmd == ["git", "remote", "get-url", "origin"]
        return subprocess.CompletedProcess(cmd, 0,
                                           "https://github.com/pom11/flightdeck.git\n", "")
    assert git_state.remote_url("/repo", _run=runner) == \
        "https://github.com/pom11/flightdeck.git"


def test_remote_url_none_when_remote_missing():
    def runner(cmd, repo):
        return subprocess.CompletedProcess(cmd, 128, "",
                                           "fatal: 'origin' does not appear to be a git repository")
    assert git_state.remote_url("/repo", _run=runner) is None


def test_remote_url_none_when_non_repo():
    class F:
        def __call__(self, cmd, repo):
            return subprocess.CompletedProcess(cmd, 128, "", "boom")
    assert git_state.remote_url("/no/repo", _run=F()) is None


# --------------------------------------------------------------------------- #
# upstream_head_sha — remote HEAD via ls-remote (no local fetch)
# --------------------------------------------------------------------------- #

def test_upstream_head_sha_parses_head_line():
    def runner(cmd, repo):
        assert cmd == ["git", "ls-remote", "https://github.com/x/y.git", "HEAD"]
        return subprocess.CompletedProcess(
            cmd, 0, "0123456789abcdef0123456789abcdef01234567\tHEAD\n", "")
    assert git_state.upstream_head_sha("https://github.com/x/y.git", _run=runner) == \
        "0123456789abcdef0123456789abcdef01234567"


def test_upstream_head_sha_ignores_non_head_lines():
    def runner(cmd, repo):
        return subprocess.CompletedProcess(
            cmd, 0,
            "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b\trefs/heads/main\n"
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\tHEAD\n",
            "")
    assert git_state.upstream_head_sha("url", _run=runner) == \
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_upstream_head_sha_none_when_unreachable():
    def runner(cmd, repo):
        return subprocess.CompletedProcess(cmd, 128, "",
                                           "could not read Username for 'https://...'")
    assert git_state.upstream_head_sha("url", _run=runner) is None


def test_upstream_head_sha_none_when_no_head_line():
    def runner(cmd, repo):
        return subprocess.CompletedProcess(cmd, 0, "")
    assert git_state.upstream_head_sha("url", _run=runner) is None


# --------------------------------------------------------------------------- #
# file_at_ref — read a blob (e.g. VERSION) at a ref without checkout
# --------------------------------------------------------------------------- #

def test_file_at_ref_returns_contents():
    def runner(cmd, repo):
        assert cmd == ["git", "show", "main:VERSION"]
        return subprocess.CompletedProcess(cmd, 0, "0.7.0\n", "")
    assert git_state.file_at_ref("/repo", "main", "VERSION", _run=runner) == "0.7.0\n"


def test_file_at_ref_none_when_path_missing_at_ref():
    def runner(cmd, repo):
        return subprocess.CompletedProcess(cmd, 128, "",
                                           "fatal: path 'VERSION' does not exist in 'main'")
    assert git_state.file_at_ref("/repo", "main", "VERSION", _run=runner) is None


def test_file_at_ref_none_when_ref_unresolvable():
    def runner(cmd, repo):
        return subprocess.CompletedProcess(cmd, 128, "",
                                           "fatal: ambiguous argument 'ghost:VERSION'")
    assert git_state.file_at_ref("/repo", "ghost", "VERSION", _run=runner) is None
