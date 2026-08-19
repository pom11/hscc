"""Tests for `flightdeck project` (commands/project.py + core/project_lifecycle.py).

Every external effect is stubbed:

- ``FakeRun`` — a stateful subprocess runner for git + gh. It tracks whether
  the repo has been initialized and committed so idempotency is REAL: after
  ``git init`` succeeds, ``rev-parse --is-inside-work-tree`` returns 0.
- ``FakeTG``  — the telegram MCP client, same shape as core.telegram expects.
- ``FakeKanban`` — a stand-in for ``hermes_cli.kanban_db`` with the tiny
  surface lifecycle uses: ``board_exists`` + ``create_board`` (mkdir-p).

No test touches real git, the network, Telegram, or the live cluster. The
registry is written to a pytest tmp_path, never ~/.flightdeck.
"""

import argparse
import subprocess

import pytest

from flightdeck.commands import project as project_cmd
from flightdeck.core import project_lifecycle, registry


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

class FakeRun:
    """A stateful (cmd_list, cwd) -> proc runner for git + gh.

    Tracks the repo lifecycle so idempotency is enforced, not assumed:
    ``git init`` flips ``is_repo`` on; ``git commit`` succeeds only after
    ``git add``; a second ``git init``/``commit`` is a no-op that errors
    ("already exists"/"nothing to commit"). Callers can also ``fail()`` an
    arbitrary command to exercise partial-failure paths deterministically.
    """

    def __init__(self, *, is_repo=False, committed=False):
        self.is_repo = is_repo
        self.committed = committed
        self.fail_cmd = None      # e.g. ("git", "init") fail next matching call
        self.fail_stderr = "boom"
        self.calls: list[list[str]] = []

    def fail_next(self, prefix: tuple, stderr: str = "boom"):
        """Make the next call whose cmd starts with ``prefix`` return rc 1."""
        self.fail_cmd = prefix
        self.fail_stderr = stderr

    def _proc(self, cmd, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, cwd):
        self.calls.append(list(cmd))
        if self.fail_cmd and cmd[: len(self.fail_cmd)] == list(self.fail_cmd):
            self.fail_cmd = None
            return self._proc(cmd, 1, "", self.fail_stderr)

        if cmd[0] == "git":
            if cmd[1] == "rev-parse":
                # rev-parse --is-inside-work-tree
                return self._proc(cmd, 0 if self.is_repo else 128,
                                  "true" if self.is_repo else "")
            if cmd[1] == "init":
                if self.is_repo:
                    return self._proc(cmd, 1, "", "Reinitialized existing Git repository")
                self.is_repo = True
                return self._proc(cmd, 0, "Initialized empty Git repository")
            if cmd[1] == "add":
                return self._proc(cmd, 0, "")
            if cmd[1] == "commit":
                if self.committed:
                    return self._proc(cmd, 1, "", "nothing to commit, working tree clean")
                self.committed = True
                return self._proc(cmd, 0, "1 file changed")
        if cmd[0] == "gh":
            if cmd[1] == "repo":
                return self._proc(cmd, 0, "created repository")
        raise AssertionError(f"FakeRun does not know this command: {cmd!r}")


class FakeTG:
    """A (tool, args) -> str telegram client, same shape core.telegram uses."""

    def __init__(self, topics=None):
        self.state = dict(topics or {})   # id -> name
        self.calls = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if tool_name == "telegram_topic_status":
            return "\n".join(
                f"topic_id={tid} title={n}" for tid, n in sorted(self.state.items())
            )
        if tool_name == "telegram_topic_create":
            new_id = max(self.state) + 1 if self.state else 1
            self.state[new_id] = arguments["name"]
            return f"created topic_id={new_id} title={arguments['name']}"
        raise AssertionError(f"FakeTG does not know tool {tool_name!r}")


class FakeKanban:
    """A stand-in for hermes_cli.kanban_db: board_exists + create_board.

    ``create_board`` is mkdir-p (returns existing when present), mirroring the
    real Hermes semantics so idempotency is real.
    """

    def __init__(self, boards=()):
        self.boards = set(boards)
        self.created = []

    def board_exists(self, slug):
        return slug in self.boards

    def create_board(self, slug):
        if slug not in self.boards:
            self.boards.add(slug)
            self.created.append(slug)
        return {"slug": slug}


def _ns(**kw):
    defaults = dict(
        client=None, run=None, kanban=None, registry=None, json=False,
        apply=False, dry_run=False, name="", repo=None, github=False,
        private=False, project_cmd=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _reg(tmp_path):
    """A registry path inside a tmp_path."""
    return str(tmp_path / "registry.yaml")


def _assert_has(reg, name):
    """The registry contains a project named ``name``; return it."""
    return registry.get_project(name, path=reg)


# --------------------------------------------------------------------------- #
# new — dry-run touches nothing
# --------------------------------------------------------------------------- #

def test_new_dry_run_mutates_nothing(tmp_path, capsys):
    reg = _reg(tmp_path)
    run, tg, kb = FakeRun(), FakeTG(), FakeKanban()
    rc = project_cmd.cmd_new(_ns(
        name="zeta", repo=str(tmp_path / "zeta"), registry=reg,
        run=run, client=tg, kanban=kb, dry_run=True, apply=False,
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert "nothing performed" in out
    # nothing touched: no git, no telegram, no board, no roadmap, no registry
    assert run.calls == []
    assert tg.calls == []
    assert kb.created == []
    assert not (tmp_path / "zeta").exists()

    # A fresh run (not --dry-run but also not --apply) is the same: refused.
    rc2 = project_cmd.cmd_new(_ns(
        name="zeta", repo=str(tmp_path / "zeta"), registry=reg,
        run=run, client=tg, kanban=kb, apply=False,
    ))
    capsys.readouterr()
    assert rc2 == 0
    assert run.calls == [] and tg.calls == [] and kb.created == []
    assert not (tmp_path / "zeta").exists()


def test_new_apply_creates_everything_and_repairs(tmp_path, capsys):
    """Full happy path: apply creates repo+topic+board+roadmap+registry, and
    re-running with apply repairs (no duplication, nothing re-created)."""
    reg = _reg(tmp_path)
    run, tg, kb = FakeRun(), FakeTG(), FakeKanban()
    repo = str(tmp_path / "zeta")

    rc = project_cmd.cmd_new(_ns(
        name="zeta", repo=repo, registry=reg, run=run, client=tg, kanban=kb, apply=True,
    ))
    capsys.readouterr()
    assert rc == 0

    # Registry entry bound to all three surfaces.
    p = _assert_has(reg, "zeta")
    assert p.repo == repo
    assert p.board == "zeta"
    assert p.topic is not None and p.topic in tg.state
    assert p.topic_name == "zeta"

    # Repo inited + seeded ROADMAP.md.
    assert run.is_repo and run.committed
    assert (tmp_path / "zeta" / "ROADMAP.md").exists()

    # Board created once.
    assert kb.created == ["zeta"]

    # ---- re-run (repair) must not duplicate anything ----
    run2, tg2, kb2 = FakeRun(is_repo=True, committed=True), FakeTG(tg.state), FakeKanban(kb.boards)
    rc = project_cmd.cmd_new(_ns(
        name="zeta", repo=repo, registry=reg, run=run2, client=tg2, kanban=kb2, apply=True,
    ))
    capsys.readouterr()
    assert rc == 0
    # No second topic created, no second board created, no second commit.
    assert len(tg2.state) == len(tg.state)
    assert kb2.created == []
    # Registry still one entry.
    assert len(registry.load_registry(reg)) == 1


# --------------------------------------------------------------------------- #
# idempotency of each step, run twice, in isolation
# --------------------------------------------------------------------------- #

def test_each_step_is_idempotent_when_run_twice(tmp_path):
    """Every lifecycle step run twice leaves no duplicates and no errors."""
    reg = _reg(tmp_path)
    repo = str(tmp_path / "alpha")

    # Step 1: git repo
    run = FakeRun()
    first = project_lifecycle.ensure_git_repo(repo, _run=run)
    assert first["status"] == "created"
    second = project_lifecycle.ensure_git_repo(repo, _run=FakeRun(is_repo=True, committed=True))
    assert second["status"] == "exists"
    assert run.calls.count(["git", "init"]) == 1

    # Step 2: topic
    tg = FakeTG()
    t1 = project_lifecycle.ensure_topic("alpha", _client=tg)
    t2 = project_lifecycle.ensure_topic("alpha", _client=tg)
    assert t1 == t2
    assert len(tg.state) == 1  # one topic, adopted not duplicated

    # Step 3: board
    kb = FakeKanban()
    project_lifecycle.ensure_board("alpha", _kanban=kb)
    project_lifecycle.ensure_board("alpha", _kanban=kb)
    assert kb.boards == {"alpha"} and kb.created == ["alpha"]

    # Step 4: roadmap
    project_lifecycle.ensure_roadmap(repo, _run=FakeRun(is_repo=True))
    project_lifecycle.ensure_roadmap(repo, _run=FakeRun(is_repo=True))
    roadmap_text = (tmp_path / "alpha" / "ROADMAP.md").read_text()
    assert "## Now" in roadmap_text and "## Next" in roadmap_text and "## Later" in roadmap_text

    # Step 5: registry (upsert twice -> one entry)
    project_lifecycle.upsert_project(reg, name="alpha", repo=repo)
    project_lifecycle.upsert_project(reg, name="alpha", repo=repo, board="alpha", topic=7)
    assert len(registry.load_registry(reg)) == 1
    assert registry.load_registry(reg)[0].board == "alpha"
    assert registry.load_registry(reg)[0].topic == 7


# --------------------------------------------------------------------------- #
# partial failure: what succeeded is recorded + the retry command is given
# --------------------------------------------------------------------------- #

def test_partial_failure_records_successes_and_reports_retry(tmp_path, capsys):
    """Board step fails after repo+topic+roadmap succeed: those are recorded in
    the registry, the failure is reported with the exact retry command, and a
    re-run repairs only the missing board."""
    reg = _reg(tmp_path)
    repo = str(tmp_path / "beta")
    run = FakeRun()
    tg = FakeTG()

    # Make the board step fail: board_exists raises.
    class FlakyKanban:
        def board_exists(self, slug):
            raise RuntimeError("network unreachable")
        def create_board(self, slug):
            raise RuntimeError("network unreachable")

    rc = project_cmd.cmd_new(_ns(
        name="beta", repo=repo, registry=reg, run=run, client=tg,
        kanban=FlakyKanban(), apply=True,
    ))
    out = capsys.readouterr().out
    assert rc == 1
    assert "partial failure" in out
    assert "retry command: flightdeck project new beta --repo" in out

    # Repo + topic + roadmap succeeded and ARE recorded; board/topic not lost.
    p = _assert_has(reg, "beta")
    assert p.repo == repo
    assert p.topic is not None            # topic succeeded -> recorded
    assert p.roadmap == "ROADMAP.md"      # roadmap succeeded -> recorded
    assert p.board is None                # board failed -> NOT recorded (unknown)
    assert p.topic_name == "beta"
    assert (tmp_path / "beta" / "ROADMAP.md").exists()

    # Re-run with a healthy kanban provider repairs ONLY the board.
    run2 = FakeRun(is_repo=True, committed=True)
    tg2 = FakeTG(tg.state)
    kb2 = FakeKanban()
    rc = project_cmd.cmd_new(_ns(
        name="beta", repo=repo, registry=reg, run=run2, client=tg2,
        kanban=kb2, apply=True,
    ))
    capsys.readouterr()
    assert rc == 0
    # No new topic (adopted), no new roadmap (exists), only the board created.
    assert kb2.created == ["beta"]
    assert len(tg2.state) == len(tg.state)
    p = _assert_has(reg, "beta")
    assert p.board == "beta"              # now repaired
    assert len(registry.load_registry(reg)) == 1


def test_partial_failure_board_failed_records_topic(tmp_path, capsys):
    """Explicit: when board fails, the succeeded topic is still recorded."""
    reg = _reg(tmp_path)
    repo = str(tmp_path / "gamma")

    class AlwaysFail:
        def board_exists(self, slug):
            raise RuntimeError("boom")
        def create_board(self, slug):
            raise RuntimeError("boom")

    rc = project_cmd.cmd_new(_ns(
        name="gamma", repo=repo, registry=reg,
        run=FakeRun(), client=FakeTG(), kanban=AlwaysFail(), apply=True,
    ))
    capsys.readouterr()
    assert rc == 1
    p = _assert_has(reg, "gamma")
    assert p.topic is not None and p.board is None
    assert p.repo == repo


def test_failed_repo_step_writes_no_registry(tmp_path, capsys):
    """The one guard: with no repo there is nothing valid to record."""
    reg = _reg(tmp_path)
    repo = str(tmp_path / "delta")

    run = FakeRun()
    run.fail_next(("git", "init"), stderr="permission denied")
    rc = project_cmd.cmd_new(_ns(
        name="delta", repo=repo, registry=reg, run=run, client=FakeTG(), kanban=FakeKanban(), apply=True,
    ))
    capsys.readouterr()
    assert rc == 1
    assert registry.load_registry(reg) == []  # nothing recorded


# --------------------------------------------------------------------------- #
# project list — read-only, never mutates
# --------------------------------------------------------------------------- #

def test_list_shows_columns_and_health(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "sigma")
    (tmp_path / "sigma").mkdir()
    kb = FakeKanban(boards={"sigma"})
    tg = FakeTG({140: "sigma"})
    registry.add_project("sigma", repo=repo, board="sigma", topic=140, path=reg)

    rc = project_cmd.cmd_list(_ns(registry=reg, run=FakeRun(), client=tg, kanban=kb))
    out = capsys.readouterr().out
    assert rc == 0
    assert "sigma" in out
    assert "HEALTH" in out
    assert "ok" in out

    # Read-only: nothing was created or mutated.
    assert kb.created == []
    assert tg.calls  # a status read happened, but no create


def test_list_json_shape(tmp_path, capsys):
    import json

    reg = _reg(tmp_path)
    repo = str(tmp_path / "tau")
    (tmp_path / "tau").mkdir()
    kb = FakeKanban()
    tg = FakeTG()
    registry.add_project("tau", repo=repo, path=reg)

    rc = project_cmd.cmd_list(_ns(registry=reg, run=FakeRun(), client=tg, kanban=kb, json=True))
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == [{
        "name": "tau", "repo": repo, "board": "unknown",
        "topic": "unknown", "health": "ok",
    }]


# --------------------------------------------------------------------------- #
# project remove — registry entry only, NEVER the repo or the topic
# --------------------------------------------------------------------------- #

def test_remove_requires_apply(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta2")
    registry.add_project("zeta2", repo=repo, board="zeta2", topic=99, path=reg)
    (tmp_path / "zeta2").mkdir()
    run = FakeRun()

    rc = project_cmd.cmd_remove(_ns(name="zeta2", registry=reg, run=run, apply=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "does NOT delete the git repo" in out
    assert "does NOT delete the Telegram topic" in out
    assert "pass --apply" in out
    assert _assert_has(reg, "zeta2")  # still there

    # FakeRun tracks subprocesses; no git/gh/remove command was called at all.
    assert run.calls == []


def test_remove_with_apply_removes_entry_but_touches_no_repo_topic(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "eta")
    (tmp_path / "eta").mkdir()
    tg = FakeTG({50: "eta"})
    registry.add_project("eta", repo=repo, board="eta", topic=50, path=reg)

    rc = project_cmd.cmd_remove(_ns(name="eta", registry=reg, run=FakeRun(), apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "removed registry entry" in out
    # registry entry gone
    with pytest.raises(registry.ProjectNotFoundError):
        registry.get_project("eta", path=reg)
    # repo dir still exists, topic state untouched
    assert (tmp_path / "eta").exists()
    assert tg.state.get(50) == "eta"


# --------------------------------------------------------------------------- #
# project repair — same as re-running `new`, fills only what is missing
# --------------------------------------------------------------------------- #

def test_repair_requires_apply(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "iota")
    registry.add_project("iota", repo=repo, path=reg)

    rc = project_cmd.cmd_repair(_ns(name="iota", registry=reg, run=FakeRun(), apply=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "pass --apply" in out
    assert not (tmp_path / "iota").exists()  # nothing created
    assert registry.load_registry(reg)[0].board is None


def test_repair_fills_only_missing_pieces(tmp_path, capsys):
    """A project with repo+roadmap but no board/topic: repair adds only those."""
    reg = _reg(tmp_path)
    repo = str(tmp_path / "kappa")
    (tmp_path / "kappa").mkdir()
    (tmp_path / "kappa" / "ROADMAP.md").write_text("## Now\n- [ ] x\n", encoding="utf-8")
    registry.add_project("kappa", repo=repo, roadmap="ROADMAP.md", path=reg)

    run = FakeRun(is_repo=True, committed=True)
    tg = FakeTG()
    kb = FakeKanban(boards={"kappa"})
    rc = project_cmd.cmd_repair(_ns(
        name="kappa", registry=reg, run=run, client=tg, kanban=kb, apply=True,
    ))
    out = capsys.readouterr().out
    assert rc == 0
    assert "repaired" in out

    p = _assert_has(reg, "kappa")
    assert p.board == "kappa"
    assert p.topic is not None
    assert p.topic_name == "kappa"
    # roadmap file untouched by repair (existed already)
    assert "## Now" in (tmp_path / "kappa" / "ROADMAP.md").read_text()

    # The pre-existing repo was NOT re-inited (run started as a valid repo).
    assert run.is_repo is True


def test_repair_requires_existing_project(tmp_path, capsys):
    reg = _reg(tmp_path)
    rc = project_cmd.cmd_repair(_ns(name="missing", registry=reg, run=FakeRun(), apply=True))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no project named 'missing'" in err


# --------------------------------------------------------------------------- #
# GitHub remote — optional part of step 1
# --------------------------------------------------------------------------- #

def test_new_with_github_creates_remote(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "ghproj")
    run = FakeRun()  # gh succeeds
    rc = project_cmd.cmd_new(_ns(
        name="ghproj", repo=repo, registry=reg, run=run, client=FakeTG(),
        kanban=FakeKanban(), github=True, private=True, apply=True,
    ))
    capsys.readouterr()
    assert rc == 0
    gh_calls = [c for c in run.calls if c[0] == "gh"]
    assert gh_calls and "--private" in gh_calls[0]
    assert "--push" in gh_calls[0]


def test_ensure_github_is_idempotent_when_remote_exists():
    """gh reporting the remote already exists reads as `exists`, not a failure."""

    class AlreadyExists:
        def __call__(self, cmd, cwd):
            return subprocess.CompletedProcess(
                cmd, 1, "", "ERROR: Repository creation failed. A repository named 'x' already exists"
            )

    assert project_lifecycle.ensure_github("/repo/x", _run=AlreadyExists()) == "exists"


def test_ensure_github_failure_raises():
    """A gh failure that is NOT 'already exists' surfaces as a LifecycleError."""

    class OtherFailure:
        def __call__(self, cmd, cwd):
            return subprocess.CompletedProcess(cmd, 1, "", "gh not authenticated")

    with pytest.raises(project_lifecycle.LifecycleError):
        project_lifecycle.ensure_github("/repo/x", _run=OtherFailure())


# --------------------------------------------------------------------------- #
# CLI auto-discovery — `project` is picked up by cli.py without editing it
# --------------------------------------------------------------------------- #

def test_project_is_auto_discovered_by_cli():
    from flightdeck.cli import build_parser
    args = build_parser().parse_args(["project", "list"])
    assert args.command == "project"
    assert args.project_cmd == "list"


# --------------------------------------------------------------------------- #
# project pull — safe fetch + fast-forward-only pull
# --------------------------------------------------------------------------- #

class FakePullPush:
    """A deterministic runner simulating a git repo for pull/push.

    Knobs (all via kwargs): is_repo (True), default (\"main\"),
    branch (\"main\") — currently checked out, dirty (0) uncommitted change
    count, fetch_ok (True), pull_rc (0) exit of ``git pull --ff-only``,
    pull_moved (True) whether pull advances HEAD, revlist_count (2) the
    number returned by any ``git rev-list --count``, upstream
    (\"origin/main\"), has_upstream (True), push_rc (0) exit of ``git push``.

    Tracks ``mutations``: the list of mutating commands actually executed
    (``git pull`` / ``git push``), so tests assert the repo was NOT touched.
    """

    def __init__(self, **kw):
        self.is_repo = kw.get("is_repo", True)
        self.default = kw.get("default", "main")
        self.branch = kw.get("branch", "main")
        self.dirty = kw.get("dirty", 0)
        self.fetch_ok = kw.get("fetch_ok", True)
        self.pull_rc = kw.get("pull_rc", 0)
        self.pull_moved = kw.get("pull_moved", True)
        self.revlist_count = kw.get("revlist_count", 2)
        self.upstream = kw.get("upstream", "origin/main")
        self.has_upstream = kw.get("has_upstream", True)
        self.push_rc = kw.get("push_rc", 0)
        self.head = kw.get("head", "h_base")
        self.mutations: list[list[str]] = []

    def _proc(self, cmd, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, repo):
        sub = cmd[1]
        if sub == "rev-parse":
            if "--is-inside-work-tree" in cmd:
                return self._proc(cmd, 0 if self.is_repo else 128,
                                  "true" if self.is_repo else "")
            if "--abbrev-ref" in cmd:
                # git rev-parse --abbrev-ref <branch>@{upstream}
                if len(cmd) > 3 and cmd[3].endswith("@{upstream}"):
                    if not self.has_upstream:
                        return self._proc(cmd, 128, "",
                                          "fatal: no upstream configured for branch")
                    return self._proc(cmd, 0, self.upstream)
                # git rev-parse --abbrev-ref HEAD
                return self._proc(cmd, 0, self.branch)
            # git rev-parse HEAD
            return self._proc(cmd, 0, self.head)
        if sub == "symbolic-ref":
            # git symbolic-ref refs/remotes/origin/HEAD
            if not self.default:
                return self._proc(cmd, 1, "")
            return self._proc(cmd, 0, f"refs/remotes/origin/{self.default}")
        if sub == "status":
            return self._proc(cmd, 0, "\n".join([" M x.py"] * self.dirty))
        if sub == "fetch":
            if not self.fetch_ok:
                return self._proc(cmd, 128, "", "couldn't connect to host")
            self.head = "h_fetched"
            return self._proc(cmd, 0, "")
        if sub == "pull":
            self.mutations.append(list(cmd))
            if self.pull_rc != 0:
                return self._proc(cmd, self.pull_rc, "",
                                  "Not possible to fast-forward, aborting")
            if self.pull_moved:
                self.head = "h_pulled"
            return self._proc(cmd, 0, "Updating ...")
        if sub == "rev-list":
            # git rev-list --count <a>..<b>
            return self._proc(cmd, 0, str(self.revlist_count))
        if sub == "push":
            self.mutations.append(list(cmd))
            return self._proc(cmd, self.push_rc, "",
                              "" if self.push_rc == 0 else "rejected!")
        raise AssertionError(f"FakePullPush does not know this command: {cmd!r}")


def test_pull_clean_fast_forward_succeeds(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="main", default="main", pull_moved=True, revlist_count=4)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_pull(_ns(registry=reg, run=fake))
    out = capsys.readouterr().out
    assert rc == 0
    assert "pulled 4 commit(s)" in out
    # The pull actually ran (--ff-only, no --force anywhere).
    assert any(c[1] == "pull" for c in fake.mutations)
    for c in fake.mutations:
        assert "--force" not in c and "-f" not in c


def test_pull_unknown_project_errors(tmp_path, capsys):
    reg = _reg(tmp_path)
    rc = project_cmd.cmd_pull(_ns(name="missing", registry=reg, run=FakePullPush()))
    err = capsys.readouterr().err
    assert rc == 2
    assert "no project named 'missing'" in err


def test_pull_dirty_tree_is_skipped_not_touched(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="main", default="main", dirty=2)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_pull(_ns(registry=reg, run=fake))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    assert "2 uncommitted change(s), not touching" in out
    assert fake.mutations == []  # never reached an actual pull


def test_pull_non_default_branch_is_skipped_not_switched(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="feature/x", default="main")
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_pull(_ns(registry=reg, run=fake))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    assert "on branch feature/x, not main, leaving untouched" in out
    assert fake.mutations == []  # no pull, no checkout/switch


def test_pull_diverged_history_is_skipped_not_merged(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="main", default="main", pull_rc=1)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_pull(_ns(registry=reg, run=fake))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    assert "diverged history" in out
    assert "resolve by hand" in out
    # A diverged pull must never fall back to an auto-merge.
    assert not any(c[1] == "merge" for c in fake.mutations)


def test_pull_all_projects_in_sequence_and_json(tmp_path, capsys):
    import json

    reg = _reg(tmp_path)
    r1 = str(tmp_path / "alpha")
    r2 = str(tmp_path / "beta")
    registry.add_project("alpha", repo=r1, path=reg)
    registry.add_project("beta", repo=r2, path=reg)

    fake_a = FakePullPush(branch="main", default="main", pull_moved=True, revlist_count=3)
    fake_b = FakePullPush(branch="feature/y", default="main")

    class MultiRunner:
        def __call__(self, cmd, repo):
            return (fake_a if repo == r1 else fake_b)(cmd, repo)

    rc = project_cmd.cmd_pull(_ns(registry=reg, run=MultiRunner(), json=True))
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == [
        {"name": "alpha", "repo": r1, "status": "pulled", "detail": "pulled 3 commit(s)", "n": 3},
        {"name": "beta", "repo": r2, "status": "skipped",
         "detail": "on branch feature/y, not main, leaving untouched", "n": 0},
    ]


# --------------------------------------------------------------------------- #
# project push — gated behind --apply, never --force
# --------------------------------------------------------------------------- #

def test_push_without_apply_only_reports_what_would_push(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="main", upstream="origin/main",
                        has_upstream=True, revlist_count=5)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_push(_ns(registry=reg, run=fake, apply=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "would push 5 commit(s) to origin/main" in out
    assert "(--apply not given: nothing was pushed)" in out
    assert fake.mutations == []  # dry-run did NOT actually push


def test_push_apply_pushes_current_branch(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="feature/x", upstream="origin/feature/x",
                        has_upstream=True, revlist_count=2, push_rc=0)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_push(_ns(registry=reg, run=fake, apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "pushed 2 commit(s) to origin/feature/x" in out
    # Only a plain `git push` — never a force push, and only the current branch.
    assert fake.mutations == [["git", "push"]]


def test_push_never_force_pushes_on_rejection(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="main", upstream="origin/main", has_upstream=True,
                        revlist_count=3, push_rc=1)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_push(_ns(registry=reg, run=fake, apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    assert "push rejected (remote has diverged), resolve by hand" in out
    # The failed push must never be retried with --force.
    assert fake.mutations == [["git", "push"]]


def test_push_with_no_upstream_reports_clearly(tmp_path, capsys):
    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="feature/x", upstream="origin/feature/x",
                        has_upstream=False)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_push(_ns(registry=reg, run=fake, apply=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "SKIPPED" in out
    assert f"no upstream tracking branch for feature/x" in out
    assert fake.mutations == []  # no upstream -> nothing attempted


def test_push_dry_run_json(tmp_path, capsys):
    import json

    reg = _reg(tmp_path)
    repo = str(tmp_path / "zeta")
    fake = FakePullPush(branch="main", upstream="origin/main",
                        has_upstream=True, revlist_count=4)
    registry.add_project("zeta", repo=repo, path=reg)

    rc = project_cmd.cmd_push(_ns(registry=reg, run=fake, apply=False, json=True))
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == [{
        "name": "zeta", "repo": repo, "branch": "main",
        "upstream": "origin/main", "ahead": 4, "status": "would_push",
        "detail": "4 commit(s) ahead of origin/main (pass --apply to push)",
    }]
    assert fake.mutations == []


