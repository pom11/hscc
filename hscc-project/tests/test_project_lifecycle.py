"""Tests for flightdeck.core.project_lifecycle — the profile + session steps.

Covers the two lifecycle steps this card adds — the ``<name>-orch`` orchestrator
profile (Step 5a) and the titled chat session on it (Step 5b) — plus their
wiring into ``create_project``.

Everything external is faked: the ``_run`` seam answers ``hermes profile``
commands from canned state (no real hermes, no real profile, no live runtime
mutation), and the ``_session_db`` seam is a small in-memory SessionDB stand-in
with the same three methods ``ensure_session`` relies on. No test writes to
``~/.hermes/profiles/*/state.db``, creates a real profile, or reaches the
network / Telegram.
"""

import subprocess

import pytest

from flightdeck.core import project_lifecycle as lifecycle
from flightdeck.core.project_lifecycle import LifecycleError


class FakeHermes:
    """A ``_run`` seam answering ``hermes profile`` commands from canned state.

    ``exists`` lists the profiles ``hermes profile list`` should report.
    A ``create`` always succeeds unless ``create_fails=True``.
    """

    def __init__(self, exists=(), create_fails=False):
        self._exists = list(exists)
        self.create_fails = create_fails
        self.profile_list_calls = 0
        self.profile_create_calls = []

    def _proc(self, cmd, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def profile(self, cmd, cwd):
        """Handle ``hermes profile ...`` (called by a composite runner)."""
        if cmd[2] == "list":
            self.profile_list_calls += 1
            return self._proc(cmd, 0, "\n".join(self._exists) + "\n")
        if cmd[2] == "create":
            target = cmd[3]
            self.profile_create_calls.append(target)
            if self.create_fails:
                return self._proc(cmd, 1, "", "could not reach hermes")
            self._exists.append(target)
            return self._proc(cmd, 0)
        raise AssertionError(f"unexpected hermes profile subcommand: {cmd}")


class FakeRunner:
    """A composite ``_run`` seam for whole-create_project tests.

    Delegates ``hermes profile`` to a :class:`FakeHermes` and lets every other
    command (git, gh) succeed with rc 0 — so the non-under-test lifecycle
    steps (repo, roadmap, ...) pass without touching a real git repo.
    """

    def __init__(self, hermes):
        self.hermes = hermes

    def _proc(self, cmd, rc, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, cwd):
        if cmd and cmd[0] == "hermes":
            assert cmd[1] == "profile"
            return self.hermes.profile(cmd, cwd)
        return self._proc(cmd, 0)


class FakeSessionDB:
    """In-memory stand-in for ``hermes_state.SessionDB``.

    Implements only the three methods ``ensure_session`` uses, so the session
    step is exercised without any real ``state.db`` file.
    """

    def __init__(self, titled=()):
        self._titled = set(titled)
        self._ids = []
        self.closed = False

    def resolve_session_by_title(self, title):
        return title in self._titled

    def create_session(self, sid, **kwargs):
        self._ids.append(sid)

    def set_session_title(self, sid, title):
        self._titled.add(title)

    def close(self):
        self.closed = True


def _db_provider(db):
    """Adapt a FakeSessionDB into an ``_session_db`` opener (ignores profile)."""

    def _open(profile):
        return db

    return _open


def _default_project_args():
    """Default (succeeding) create_project kwargs for the under-test steps.

    Returned as a literal dict so ``create_project(**...)`` type-check is not
    confused by a dynamic spread. The repo / registry paths are fake temp
    names that never touch the operator's real files.
    """
    return {
        "name": "myproj",
        "repo": "/tmp/fakerepo_lifecycle_x",
        "registry_path": "/tmp/fakereg_lifecycle_x.json",
    }


# --------------------------------------------------------------------------- #
# ensure_profile
# --------------------------------------------------------------------------- #

def test_ensure_profile_creates_when_missing():
    hermes = FakeHermes(exists=())
    assert lifecycle.ensure_profile("myproj", _run=FakeRunner(hermes)) == "created"
    assert hermes.profile_create_calls == ["myproj-orch"]


def test_ensure_profile_is_skipped_when_exists():
    hermes = FakeHermes(exists=("myproj-orch", "default"))
    assert lifecycle.ensure_profile("myproj", _run=FakeRunner(hermes)) == "skipped"
    # The idempotent path must never issue a create.
    assert hermes.profile_create_calls == []


def test_ensure_profile_raises_when_create_fails():
    hermes = FakeHermes(exists=(), create_fails=True)
    with pytest.raises(LifecycleError):
        lifecycle.ensure_profile("myproj", _run=FakeRunner(hermes))


# --------------------------------------------------------------------------- #
# ensure_session
# --------------------------------------------------------------------------- #

def test_ensure_session_creates_when_missing():
    db = FakeSessionDB()
    result = lifecycle.ensure_session("myproj", "myproj-orch", _session_db=_db_provider(db))
    assert result["status"] == "created"
    assert result["title"] == "myproj"
    assert result["session"]
    assert db.closed is True


def test_ensure_session_is_exists_when_present():
    db = FakeSessionDB(titled=("myproj",))
    result = lifecycle.ensure_session("myproj", "myproj-orch", _session_db=_db_provider(db))
    assert result["status"] == "exists"
    assert db._ids == []  # never clobbers / never creates a duplicate


def test_ensure_session_raises_when_no_db():
    with pytest.raises(LifecycleError):
        lifecycle.ensure_session("myproj", "myproj-orch", _session_db=lambda p: None)


def test_ensure_session_is_idempotent():
    db = FakeSessionDB()
    first = lifecycle.ensure_session("myproj", "myproj-orch", _session_db=_db_provider(db))
    second = lifecycle.ensure_session("myproj", "myproj-orch", _session_db=_db_provider(db))
    assert first["status"] == "created"
    assert second["status"] == "exists"
    assert len(db._ids) == 1  # a single session created across both calls


# --------------------------------------------------------------------------- #
# create_project wiring
# --------------------------------------------------------------------------- #

def _steps_by_id(results):
    return {s["id"]: s for s in results["steps"]}


def test_create_project_includes_profile_and_session_steps():
    results = lifecycle.create_project(
        **_default_project_args(),
        _run=FakeRunner(FakeHermes(exists=())),
        _session_db=_db_provider(FakeSessionDB()),
    )
    steps = _steps_by_id(results)
    assert "profile" in steps and steps["profile"]["status"] == "ok"
    assert "session" in steps and steps["session"]["status"] == "ok"
    # Profile before session, both before registry.
    ids = [s["id"] for s in results["steps"]]
    assert ids.index("profile") < ids.index("session") < ids.index("registry")


def test_create_project_is_idempotent_for_profile_and_session():
    db = FakeSessionDB(titled=("myproj",))
    results = lifecycle.create_project(
        **_default_project_args(),
        _run=FakeRunner(FakeHermes(exists=("myproj-orch",))),
        _session_db=_db_provider(db),
    )
    steps = _steps_by_id(results)
    assert steps["profile"]["status"] == "skipped"
    assert steps["session"]["status"] == "ok"
    assert db._ids == []  # nothing created on the already-present path


def test_create_project_reports_profile_failure_not_raise():
    db = FakeSessionDB()
    results = lifecycle.create_project(
        **_default_project_args(),
        _run=FakeRunner(FakeHermes(exists=(), create_fails=True)),
        _session_db=_db_provider(db),
    )
    steps = _steps_by_id(results)
    assert steps["profile"]["status"] == "failed"
    assert "retry" in steps["profile"]
    assert results["ok"] is False


def test_create_project_reports_session_failure_not_raise():
    results = lifecycle.create_project(
        **_default_project_args(),
        _run=FakeRunner(FakeHermes(exists=())),
        _session_db=lambda profile: None,  # profile has no state.db
    )
    steps = _steps_by_id(results)
    assert steps["session"]["status"] == "failed"
    assert "retry" in steps["session"]
    assert results["ok"] is False
