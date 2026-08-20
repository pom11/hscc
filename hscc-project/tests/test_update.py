"""Tests for `flightdeck update` (commands/update.py + core/self_update.py).

Every external effect is stubbed:

- ``FakeRunner`` — a stateful subprocess runner mirroring the shape git_state
  uses. It answers the git commands the plan applies, and can be told to fail
  specific commands so a failed/interrupted update is exercised deterministically.
- The install mechanism is faked by monkeypatching ``self_update.installed_source``
  (so the tests do not depend on how the real environment installed flightdeck).

No test touches a real repo, the network, or the live install, and mock pip/git
calls mean the tests never actually reinstall flightdeck.
"""

import argparse
import subprocess

import pytest

from flightdeck.commands import update as update_cmd
from flightdeck.core import self_update


# --------------------------------------------------------------------------- #
# Fake runner
# --------------------------------------------------------------------------- #

class FakeRunner:
    """A command-keyed (not path-keyed) subprocess runner for the update flow.

    Knows the knobs that matter to a self-update:
      * installed_sha / upstream_sha — what ``HEAD`` and ``ls-remote`` report,
        so the plan can decide up-to-date vs update-available.
      * upstream_version — what ``git show FETCH_HEAD:VERSION`` in the temp
        clone reports as the upstream version.
      * editable kwargs (is_repo, is_dirty, on_default, diverged, pull_moved)
        for the apply path.
      * a ``fail`` knob to make a named command fail (returncode != 0).
    """

    def __init__(
        self,
        *,
        installed_sha="aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        upstream_sha="bbbb2222cccc3333dddd4444eeee5555ffff6666",
        upstream_version="0.7.0",
        is_repo=True,
        is_dirty=False,
        on_default=True,          # current branch == default branch
        default_branch="main",
        diverged=False,
        pull_moved=True,
    ):
        self.installed_sha = installed_sha
        self.upstream_sha = upstream_sha
        self.upstream_version = upstream_version
        self.is_repo = is_repo
        self.is_dirty = is_dirty
        self.on_default = on_default
        self.default_branch = default_branch
        self.diverged = diverged
        self.pull_moved = pull_moved
        self.calls: list[list[str]] = []
        self.fail: str | None = None  # substring of the command to fail

    def _proc(self, cmd, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, repo):
        self.calls.append(list(cmd))
        c = cmd[1]  # second token is the subcommand (or `install` for pip)

        if self.fail and self.fail in cmd:
            return self._proc(cmd, 128, "", f"fail: {self.fail}")

        if c == "rev-parse":
            if "--is-inside-work-tree" in cmd:
                return self._proc(cmd, 0 if self.is_repo else 128,
                                  "true" if self.is_repo else "")
            if "--abbrev-ref" in cmd:
                if not self.on_default:
                    return self._proc(cmd, 0, "wt/feature")
                return self._proc(cmd, 0, self.default_branch)
            # plain: git rev-parse HEAD
            return self._proc(cmd, 0, self.installed_sha)

        if c == "remote":
            # git remote get-url origin
            return self._proc(cmd, 0, "https://github.com/example/flightdeck.git")

        if c == "ls-remote":
            # git ls-remote <url> HEAD
            return self._proc(
                cmd, 0,
                f"{self.upstream_sha}\tHEAD",
            )

        if c == "show":
            # git show FETCH_HEAD:VERSION  (temp clone reads upstream VERSION)
            if self.upstream_version is None:
                return self._proc(cmd, 128, "", "fatal: path not in ref")
            return self._proc(cmd, 0, self.upstream_version)

        if c == "init":
            # git init -q <tmpdir>
            return self._proc(cmd, 0, "")

        if c == "fetch":
            if cmd[2] == "--depth":
                # temp-clone fetch
                return self._proc(cmd, 0, "")
            # git fetch --prune origin  (apply source path)
            return self._proc(cmd, 0, "")

        if c == "pull":
            # git pull --ff-only — on success, HEAD moves to the upstream sha
            # (unless pull_moves=False to simulate a no-op pull).
            if self.diverged:
                return self._proc(cmd, 1, "", "not possible to fast-forward")
            if self.pull_moved:
                self.installed_sha = self.upstream_sha
            return self._proc(cmd, 0, "")

        if c == "rev-list":
            return self._proc(cmd, 0, "3")  # N commits pulled

        if c == "install":
            # pip install --upgrade --force-reinstall git+<url>
            return self._proc(cmd, 0, "Successfully installed flightdeck-0.7.0")

        if c == "status":
            # git status --porcelain
            if self.is_dirty:
                return self._proc(cmd, 0, " M flightdeck/core/git_state.py\n")
            return self._proc(cmd, 0, "")

        if c == "symbolic-ref":
            return self._proc(cmd, 0, f"refs/remotes/origin/{self.default_branch}")

        raise AssertionError(f"FakeRunner does not know this command: {cmd!r}")


def _editable_args(tmp_path, **kw):
    """An argparse Namespace for an editable-install update run."""
    defaults = dict(run=None, registry=str(tmp_path / "registry.yaml"),
                    apply=False, dry_run=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# --------------------------------------------------------------------------- #
# installed_source — install-mechanism detection is honest, never guessed
# --------------------------------------------------------------------------- #

def test_installed_source_editable(tmp_path, monkeypatch):
    """An editable file:// URL resolves to a local path, editable == True."""
    dist_dir = tmp_path / "flightdeck-0.1.0.dist-info"
    dist_dir.mkdir()
    (dist_dir / "direct_url.json").write_text(
        '{"dir_info": {"editable": true}, "url": "file:///Users/desac/dev/flightdeck/.worktrees/t_7ced1ee7"}',
        encoding="utf-8",
    )

    class FakeDist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return (dist_dir / "direct_url.json").read_text(encoding="utf-8")

    monkeypatch.setattr(self_update, "_iter_dist", _single(FakeDist()))
    src = self_update.installed_source()
    assert src["mechanism"] == "editable"
    assert src["source"] == "/Users/desac/dev/flightdeck/.worktrees/t_7ced1ee7"


def test_installed_source_non_editable_git(tmp_path, monkeypatch):
    dist_dir = tmp_path / "flightdeck-0.1.0.dist-info"
    dist_dir.mkdir()
    (dist_dir / "direct_url.json").write_text(
        '{"url": "https://github.com/pom11/flightdeck.git", "vcs_info": {"vcs": "git", "requested_revision": null}}',
        encoding="utf-8",
    )

    class FakeDist:
        def read_text(self, name):
            return (dist_dir / "direct_url.json").read_text(encoding="utf-8")

    monkeypatch.setattr(self_update, "_iter_dist", _single(FakeDist()))
    src = self_update.installed_source()
    assert src["mechanism"] == "git"
    assert src["source"] == "https://github.com/pom11/flightdeck.git"


def test_installed_source_no_direct_url_is_non_git(tmp_path, monkeypatch):
    """No direct_url.json -> a plain index install -> cannot self-update."""

    class FakeDist:
        def read_text(self, name):
            return None

    monkeypatch.setattr(self_update, "_iter_dist", _single(FakeDist()))
    src = self_update.installed_source()
    assert src["mechanism"] == "non-git"
    assert src["source"] is None


def test_installed_source_not_installed(monkeypatch):
    monkeypatch.setattr(self_update, "_iter_dist", _single(None))
    src = self_update.installed_source()
    assert src["mechanism"] == "not-installed"
    assert src["source"] is None


# --------------------------------------------------------------------------- #
# plan — the dry-run report
# --------------------------------------------------------------------------- #

def test_plan_up_to_date_when_shas_match(tmp_path):
    fake = FakeRunner(installed_sha="s" * 40, upstream_sha="s" * 40)
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    plan = self_update.plan(str(tmp_path), _run=fake)
    assert plan["status"] == self_update.UP_TO_DATE
    assert plan["installed_version"] == "0.6.0"
    assert plan["detail"] == "already up to date"
    # A dry-run must not pull or fetch into the source.
    assert not any("pull" in c for c in fake.calls)


def test_plan_update_available_when_shas_differ(tmp_path):
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      upstream_version="0.7.0")
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    plan = self_update.plan(str(tmp_path), _run=fake)
    assert plan["status"] == self_update.UPDATE_AVAILABLE
    assert plan["installed_version"] == "0.6.0"
    assert plan["upstream_version"] == "0.7.0"
    assert plan["upstream_sha"] == "b" * 40


def test_plan_not_git_when_source_not_a_repo(tmp_path):
    fake = FakeRunner(is_repo=False)
    plan = self_update.plan(str(tmp_path), _run=fake)
    assert plan["status"] == self_update.NOT_GIT
    assert plan["detail"] == "installed source is not a git repository"


def test_plan_no_remote_reports_unknown_not_up_to_date(tmp_path):
    class NoRemote(FakeRunner):
        def __call__(self, cmd, repo):
            if cmd[1] == "remote":
                return self._proc(cmd, 128, "", "fatal: 'origin' does not appear")
            return super().__call__(cmd, repo)

    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    plan = self_update.plan(str(tmp_path), _run=NoRemote())
    assert plan["status"] == self_update.NO_REMOTE
    assert "cannot learn an upstream" in plan["detail"]


def test_plan_cannot_reach_remote_is_not_up_to_date(tmp_path):
    class CannotReach(FakeRunner):
        def __call__(self, cmd, repo):
            if cmd[1] == "ls-remote":
                return self._proc(cmd, 128, "", "could not read Username")
            return super().__call__(cmd, repo)

    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    plan = self_update.plan(str(tmp_path), _run=CannotReach())
    assert plan["status"] == self_update.CANNOT_REACH
    assert "cannot reach upstream" in plan["detail"]


# --------------------------------------------------------------------------- #
# apply_source — the editable update + its verification
# --------------------------------------------------------------------------- #

def test_apply_source_updates_and_verifies(tmp_path):
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      upstream_version="0.7.0")
    result = self_update.apply_source(str(tmp_path), _run=fake)
    assert result["status"] == self_update.APPLIED
    assert result["verified"] is True
    assert "pull" in [c for c in fake.calls if len(c) > 1 and c[1] == "pull"][0]
    assert any("ls-remote" in c for c in fake.calls)  # verify re-check


def test_apply_source_skips_dirty_tree(tmp_path):
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40, is_dirty=True)
    result = self_update.apply_source(str(tmp_path), _run=fake)
    assert result["status"] == "skipped"
    assert result["verified"] is False
    assert "uncommitted" in result["detail"] or "dirty" in result["detail"].lower()


def test_apply_source_skips_non_default_branch(tmp_path):
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      on_default=False)
    result = self_update.apply_source(str(tmp_path), _run=fake)
    assert result["status"] == "skipped"
    assert result["verified"] is False


def test_apply_source_skips_diverged(tmp_path):
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      diverged=True)
    result = self_update.apply_source(str(tmp_path), _run=fake)
    assert result["status"] == "skipped"
    assert result["verified"] is False
    assert "diverged" in result["detail"]


def test_apply_source_reports_unverified_when_head_does_not_move(tmp_path):
    """The pull 'succeeds' but HEAD does not land on upstream -> UNVERIFIED."""
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      pull_moved=False)
    # HEAD stays at 'a' even after pull (simulate a no-op that shouldn't happen).
    result = self_update.apply_source(str(tmp_path), _run=fake)
    assert result["status"] == self_update.UP_TO_DATE  # pull itself saw no move
    assert result["verified"] is False  # installed != upstream -> not live


# --------------------------------------------------------------------------- #
# apply_git — the non-editable pip reinstall path
# --------------------------------------------------------------------------- #

def test_apply_git_runs_pip_and_succeeds(tmp_path):
    fake = FakeRunner()
    result = self_update.apply_git("https://github.com/example/flightdeck.git", _run=fake)
    pip_call = [c for c in fake.calls if c[0] == "pip"][0]
    assert pip_call == [
        "pip", "install", "--upgrade", "--force-reinstall",
        "git+https://github.com/example/flightdeck.git",
    ]
    assert result["status"] == "applied"
    assert result["verified"] is True


def test_apply_git_failure_reported_honestly(tmp_path):
    fake = FakeRunner()
    fake.fail = "pip"
    result = self_update.apply_git("https://github.com/example/flightdeck.git", _run=fake)
    assert result["status"] == "failed"
    assert result["verified"] is False
    assert "pip" in result["detail"]


# --------------------------------------------------------------------------- #
# command-level: the --apply gate + honest reporting end to end
# --------------------------------------------------------------------------- #

def test_command_dry_run_mutates_nothing(tmp_path, capsys, monkeypatch):
    """No-args (dry-run) reports the plan without mutating anything."""
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      upstream_version="0.7.0")
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "editable", "source": str(tmp_path)},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake), str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "install mechanism: editable" in out
    assert "installed  0.6.0" in out
    assert "upstream   0.7.0" in out
    # No pip, no pull, nothing mutated.
    assert not any(c[0] == "pip" for c in fake.calls)
    assert not any("pull" in c for c in fake.calls)


def test_command_dry_run_up_to_date(tmp_path, capsys, monkeypatch):
    fake = FakeRunner(installed_sha="s" * 40, upstream_sha="s" * 40)
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "editable", "source": str(tmp_path)},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake), str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "already up to date" in out


def test_command_apply_updates(tmp_path, capsys, monkeypatch):
    """--apply actually runs the update (mocked git) and verifies it."""
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40,
                      upstream_version="0.7.0")
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "editable", "source": str(tmp_path)},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake, apply=True),
                        str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "updated to" in out
    assert "verified:" in out
    # The pull actually happened (a git pull --ff-only was issued).
    pulls = [c for c in fake.calls if len(c) > 1 and c[1] == "pull"]
    assert pulls and "pull" in pulls[0] and "--ff-only" in pulls[0]


def test_command_apply_refuses_when_up_to_date(tmp_path, capsys, monkeypatch):
    """--apply on an already-current install updates nothing and is honest."""
    fake = FakeRunner(installed_sha="s" * 40, upstream_sha="s" * 40)
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "editable", "source": str(tmp_path)},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake, apply=True),
                        str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "already up to date" in out
    assert not any("pull" in c for c in fake.calls)  # nothing pulled


def test_command_apply_diverged_is_honest_failure(tmp_path, capsys, monkeypatch):
    """A diverged --apply is reported as failed, never as a clean success."""
    fake = FakeRunner(installed_sha="a" * 40, upstream_sha="b" * 40, diverged=True)
    (tmp_path / "VERSION").write_text("0.6.0\n", encoding="utf-8")
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "editable", "source": str(tmp_path)},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake, apply=True),
                        str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 1
    assert "update did NOT happen" in out
    assert "diverged" in out


def test_command_git_mechanism_plans_pip(tmp_path, capsys, monkeypatch):
    """Non-editable git source reports the pip mechanism (no pip run dry-run)."""
    fake = FakeRunner()
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "git",
                 "source": "https://github.com/pom11/flightdeck.git"},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake), str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "pip install --upgrade --force-reinstall" in out
    assert not any(c[0] == "pip" for c in fake.calls)


def test_command_git_mechanism_apply_runs_pip(tmp_path, capsys, monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "git",
                 "source": "https://github.com/pom11/flightdeck.git"},
    )
    rc = update_cmd.run(_editable_args(tmp_path, run=fake, apply=True),
                        str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert any(c[0] == "pip" for c in fake.calls)
    assert "installed from" in out


def test_command_non_git_reports_cannot_self_update(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        self_update, "installed_source",
        lambda: {"mechanism": "non-git", "source": None},
    )
    rc = update_cmd.run(_editable_args(tmp_path), str(tmp_path / "registry.yaml"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "cannot self-update" in out


# --------------------------------------------------------------------------- #
# plumbing helper for faking distribution discovery
# --------------------------------------------------------------------------- #

def _single(obj):
    """A tiny iterable that yields exactly one item (the fake distribution)."""

    def _iter():
        if obj is None:
            return
        yield obj

    return _iter
