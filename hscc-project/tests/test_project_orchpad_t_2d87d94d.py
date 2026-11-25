"""`project new` puts orchpad on the orchestrator profile it creates (t_2d87d94d).

Step 6a already created ``<name>-orch``; the operator rule is that every path
which creates or repairs an orchestrator profile also gives it the pad. The
install logic lives in ``hscc-bootstrap/install_orchpad.py`` — this suite only
proves the lifecycle calls it for the new profile, reports the token, and never
lets a failure fail project creation.

Every case runs through the module's injectable ``_run`` seam against a tmp
HERMES_HOME. No hermes CLI, no pip, no network, no live profile tree.
"""

import argparse
import json
import subprocess

import pytest

from flightdeck.commands import project as project_cmd
from flightdeck.core import project_lifecycle as lifecycle


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HSCC_BOOTSTRAP_DIR", raising=False)
    monkeypatch.setattr(lifecycle, "resolve_hermes_python",
                        lambda: "/fake/hermes/venv/bin/python")
    return {"root": root, "tmp": tmp_path}


class Runner:
    """Canned subprocess seam: hermes, git, gh, and the two bootstrap scripts."""

    def __init__(self, profiles=(), orchpad_rc=0, orchpad_stderr="",
                 orchpad_status="enabled"):
        self.profiles = set(profiles)
        self.orchpad_rc = orchpad_rc
        self.orchpad_stderr = orchpad_stderr
        self.orchpad_status = orchpad_status
        self.calls = []
        self.created_profiles = []
        self.orchpad_calls = []

    def _proc(self, cmd, rc=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, cmd, cwd):
        self.calls.append(list(cmd))
        if cmd[:2] == ["hermes", "profile"]:
            verb = cmd[2]
            if verb == "list":
                return self._proc(cmd, 0, "\n".join(sorted(self.profiles)) + "\n")
            if verb == "create":
                self.profiles.add(cmd[3])
                self.created_profiles.append(cmd[3])
                return self._proc(cmd, 0)
            if verb == "delete":
                self.profiles.discard(cmd[3])
                return self._proc(cmd, 0)
        if cmd[0] == "git":
            if cmd[1] == "rev-parse":
                if "--verify" in cmd:
                    return self._proc(cmd, 0, "abc1234")
                return self._proc(cmd, 128, "")
            return self._proc(cmd, 0)
        if cmd[0] == "gh":
            return self._proc(cmd, 0, "created repository")
        if any(str(part).endswith("install_orchpad.py") for part in cmd):
            self.orchpad_calls.append(list(cmd))
            if self.orchpad_rc != 0:
                return self._proc(cmd, self.orchpad_rc, "",
                                  self.orchpad_stderr)
            return self._proc(cmd, 0, json.dumps(
                {"ok": True, "profile": cmd[cmd.index("--profile") + 1],
                 "status": self.orchpad_status}))
        if any(str(part).endswith("enable_plugins.py") for part in cmd):
            return self._proc(
                cmd, 0, "plugins.enabled already tiered (no change): /x\n")
        raise AssertionError(f"unexpected command: {cmd!r}")


class FakeKanban:
    def __init__(self):
        self.created = []

    def board_exists(self, slug):
        return bool(self.created)

    def create_board(self, slug):
        self.created.append(slug)
        return {"slug": slug}


class FakeSessionDB:
    def resolve_session_by_title(self, title):
        return None

    def create_session(self, session_id, **kwargs):
        return session_id

    def set_session_title(self, session_id, title):
        return None

    def close(self):
        return None


def _profile_home(sandbox, name):
    return sandbox["root"] / "profiles" / name


def test_project_new_enables_orchpad_on_the_new_orch_profile(sandbox):
    runner, report = Runner(), {}

    assert lifecycle.ensure_profile(
        "pickolo", _run=runner, orchpad_report=report) == "created"

    assert runner.created_profiles == ["pickolo-orch"]
    assert len(runner.orchpad_calls) == 1, runner.orchpad_calls
    cmd = runner.orchpad_calls[0]
    assert "--apply" in cmd
    assert cmd[cmd.index("--profile") + 1] == "pickolo-orch"
    assert cmd[cmd.index("--hermes-home") + 1] == str(sandbox["root"])
    assert report["status"] == "enabled"


def test_project_new_step_detail_reports_the_orchpad_token(sandbox):
    runner = Runner()
    result = lifecycle.create_project(
        "zeta",
        repo=str(sandbox["tmp"] / "zeta"),
        registry_path=str(sandbox["tmp"] / "registry.yaml"),
        include_topic=False,
        _run=runner,
        _kanban=FakeKanban(),
        _session_db=lambda profile: FakeSessionDB(),
    )

    assert result["ok"] is True, result
    step = next(s for s in result["steps"] if s["id"] == "profile")
    assert step["status"] == "ok"
    assert "orchpad: enabled" in step["detail"]
    assert [c[c.index("--profile") + 1] for c in runner.orchpad_calls] == [
        "zeta-orch"]


def test_an_existing_orch_profile_is_repaired_not_recreated(sandbox):
    runner, report = Runner(profiles={"pickolo-orch"}), {}

    assert lifecycle.ensure_profile(
        "pickolo", _run=runner, orchpad_report=report) == "skipped"

    assert runner.created_profiles == []
    assert report["status"] == "enabled"
    assert len(runner.orchpad_calls) == 1


def test_orchpad_failure_is_a_warning_not_a_project_failure(sandbox):
    runner = Runner(orchpad_rc=1, orchpad_stderr="network unreachable")
    result = lifecycle.create_project(
        "beta",
        repo=str(sandbox["tmp"] / "beta"),
        registry_path=str(sandbox["tmp"] / "registry.yaml"),
        include_topic=False,
        _run=runner,
        _kanban=FakeKanban(),
        _session_db=lambda profile: FakeSessionDB(),
    )

    assert result["ok"] is True, result
    step = next(s for s in result["steps"] if s["id"] == "profile")
    assert step["status"] == "ok"
    assert "orchpad: failed:rc=1" in step["detail"]
    assert "network unreachable" in step["detail"]


def test_only_the_project_orch_profile_is_offered_to_the_installer(sandbox):
    runner = Runner()
    lifecycle.create_project(
        "gamma",
        repo=str(sandbox["tmp"] / "gamma"),
        registry_path=str(sandbox["tmp"] / "registry.yaml"),
        include_topic=False,
        _run=runner,
        _kanban=FakeKanban(),
        _session_db=lambda profile: FakeSessionDB(),
    )

    offered = [c[c.index("--profile") + 1] for c in runner.orchpad_calls]
    assert offered == ["gamma-orch"]
    for worker in ("coder", "reviewer"):
        assert not _profile_home(sandbox, worker).exists()


def test_dry_run_plan_mentions_orchpad(capsys):
    rc = project_cmd.cmd_new(argparse.Namespace(
        name="zeta", repo="/tmp/zeta", github=False, private=False,
        apply=False, dry_run=True))

    assert rc == 0
    assert "orchpad" in capsys.readouterr().out
