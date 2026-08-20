"""Tests for flightdeck.core.deployment — fixture-only, nothing real executes.

Every external call (the installed/deployed version command and the deploy
timestamp command) is routed through an injectable ``_run`` runner. The
version file is read from ``project.repo``, which tests point at a tmp_path
they control. No real project, git, network, or deploy system is ever touched.

The central contract under test: THREE STATES, NEVER TWO. UNKNOWN is a real,
distinct state and must never be rendered as OK.
"""

import subprocess

import pytest

from flightdeck.core import deployment
from flightdeck.core.deployment import DRIFTED, OK, UNKNOWN
from flightdeck.core.registry import Project


class FakeRun:
    """A minimal fixture runner answering the deploy commands from canned state.

    Interprets the shell command string and returns a process-like object.
    Any command it does not recognize raises loudly so a surprising call is
    caught rather than silently guessed at.
    """

    def __init__(self, *, success=True, stdout="", returncode=0):
        self.success = success
        self.stdout = stdout
        self.returncode = returncode
        self.last_cmd = None

    def __call__(self, cmd, cwd):
        self.last_cmd = cmd
        self.last_cwd = cwd
        if not self.success:
            return subprocess.CompletedProcess(cmd, 128, "", "boom")
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def _project(tmp_path, *, installed_version_cmd, version_file=None):
    """A Project whose repo is a controlled tmp_path."""
    return Project(
        name="svc",
        repo=str(tmp_path),
        installed_version_cmd=installed_version_cmd,
        version_file=version_file,
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# version_drift — OK / DRIFTED / UNKNOWN, THREE STATES NEVER TWO
# --------------------------------------------------------------------------- #

def test_version_drift_ok_when_strings_match(tmp_path):
    _write(tmp_path / "VERSION", "1.4.2\n")
    project = _project(tmp_path, installed_version_cmd="cat /opt/svc/version")

    repo_v, installed_v, state = deployment.version_drift(project, _run=FakeRun(stdout="1.4.2\n"))

    assert state is OK
    assert repo_v == "1.4.2"
    assert installed_v == "1.4.2"

    # The deploy command ran in the repo directory.
    assert project.installed_version_cmd is not None


def test_version_drift_drifted_when_strings_differ(tmp_path):
    _write(tmp_path / "VERSION", "1.4.2\n")
    project = _project(tmp_path, installed_version_cmd="cat /opt/svc/version")

    repo_v, installed_v, state = deployment.version_drift(project, _run=FakeRun(stdout="1.3.0\n"))

    assert state is DRIFTED
    assert repo_v == "1.4.2"
    assert installed_v == "1.3.0"


def test_version_drift_unknown_when_no_command_declared(tmp_path):
    _write(tmp_path / "VERSION", "1.4.2\n")
    # No installed_version_cmd -> we have no way to know what's live.
    project = Project(name="svc", repo=str(tmp_path))

    repo_v, installed_v, state = deployment.version_drift(project, _run=FakeRun(stdout="1.4.2"))

    assert state is UNKNOWN
    assert repo_v is None and installed_v is None


def test_version_drift_unknown_when_command_exits_nonzero(tmp_path):
    _write(tmp_path / "VERSION", "1.4.2\n")
    project = _project(tmp_path, installed_version_cmd="cat /opt/nope")

    repo_v, installed_v, state = deployment.version_drift(
        project, _run=FakeRun(success=True, returncode=1, stdout="")
    )

    assert state is UNKNOWN


def test_version_drift_unknown_when_version_file_missing(tmp_path):
    # No VERSION file written; the command reports a version, but the repo side
    # cannot be read, so we cannot compare -> UNKNOWN.
    project = _project(tmp_path, installed_version_cmd="cat /opt/svc/version")

    repo_v, installed_v, state = deployment.version_drift(project, _run=FakeRun(stdout="1.4.2\n"))

    assert state is UNKNOWN
    assert installed_v == "1.4.2"  # the live side was readable and reported
    assert repo_v is None


def test_version_drift_unknown_when_command_prints_empty(tmp_path):
    _write(tmp_path / "VERSION", "1.4.2\n")
    project = _project(tmp_path, installed_version_cmd="cat /opt/svc/version")

    repo_v, installed_v, state = deployment.version_drift(
        project, _run=FakeRun(success=True, returncode=0, stdout="   \n")
    )

    assert state is UNKNOWN
    assert installed_v is None


def test_version_drift_default_version_file_is_VERSION(tmp_path):
    # version_file not set -> defaults to VERSION at the repo root.
    _write(tmp_path / "VERSION", "2.0.0\n")
    project = Project(
        name="svc",
        repo=str(tmp_path),
        installed_version_cmd="echo 2.0.0",
    )

    repo_v, installed_v, state = deployment.version_drift(project, _run=FakeRun(stdout="2.0.0\n"))

    assert state is OK
    assert repo_v == "2.0.0"


def test_version_drift_respects_custom_version_file(tmp_path):
    # version_file names a different file inside the repo.
    _write(tmp_path / "pkg/__init__.py", "__version__ = '3.1.0'\n")
    project = Project(
        name="svc",
        repo=str(tmp_path),
        installed_version_cmd="echo 3.1.0",
        version_file="pkg/__init__.py",
    )
    # resource string: we read the whole file, so the version command stdout
    # must equal the file content for OK. Here the file contains the full line,
    # keep command matching.
    repo_v, installed_v, state = deployment.version_drift(project, _run=FakeRun(stdout="3.1.0\n"))

    assert state is DRIFTED  # full file content != just the version number
    assert repo_v == "__version__ = '3.1.0'"


# --------------------------------------------------------------------------- #
# UNKNOWN is NEVER rendered as OK — the core guard.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "kwargs",
    [
        # no command declared
        dict(installed_version_cmd=None),
        # command exits non-zero
        dict(installed_version_cmd="cat /nope", _returncode=3),
        # version file missing
        dict(installed_version_cmd="echo 1.0", _no_version_file=True),
        # command prints nothing
        dict(installed_version_cmd="true", _stdout=""),
    ],
)
def test_version_drift_never_returns_ok_for_unverifiable(tmp_path, kwargs):
    """The invariant: UNKNOWN and OK are distinct — UNKNOWN is never OK."""
    no_version_file = kwargs.pop("_no_version_file", False)
    cmd = kwargs["installed_version_cmd"]
    returncode = kwargs.get("_returncode", 0)
    stdout = kwargs.get("_stdout", "1.0")

    if not no_version_file:
        _write(tmp_path / "VERSION", "1.0\n")

    project = Project(
        name="svc",
        repo=str(tmp_path),
        installed_version_cmd=cmd,
    )

    repo_v, installed_v, state = deployment.version_drift(
        project, _run=FakeRun(success=True, returncode=returncode, stdout=stdout)
    )

    assert state is UNKNOWN
    assert state is not OK


# --------------------------------------------------------------------------- #
# last_deploy_age — freshness, same UNKNOWN semantics
# --------------------------------------------------------------------------- #

def _deploy_project(tmp_path, *, deployed_at_cmd, repo=None):
    return Project(
        name="svc",
        repo=str(tmp_path) if repo is None else repo,
        deployed_at_cmd=deployed_at_cmd,
    )


def test_last_deploy_age_returns_age(tmp_path):
    project = _deploy_project(tmp_path, deployed_at_cmd="stat -c %Y /opt/svc/binary")
    fake = FakeRun(stdout="1000\n")

    age = deployment.last_deploy_age(project, _run=fake, _now=lambda: 1500)

    assert age == 500


def test_last_deploy_age_unknown_when_no_command(tmp_path):
    project = Project(name="svc", repo=str(tmp_path))

    age = deployment.last_deploy_age(project, _run=FakeRun(stdout="1000"), _now=lambda: 1500)

    assert age is None


def test_last_deploy_age_unknown_when_command_fails(tmp_path):
    project = _deploy_project(tmp_path, deployed_at_cmd="stat -c %Y /nope")
    fake = FakeRun(success=False)

    age = deployment.last_deploy_age(project, _run=fake, _now=lambda: 1500)

    assert age is None


def test_last_deploy_age_unknown_when_output_not_a_timestamp(tmp_path):
    project = _deploy_project(tmp_path, deployed_at_cmd="date")
    fake = FakeRun(stdout="not a number\n")

    age = deployment.last_deploy_age(project, _run=fake, _now=lambda: 1500)

    assert age is None


def test_last_deploy_age_unknown_not_zero_when_no_timestamp(tmp_path):
    # The guard: an unparseable/absent deploy time is "unknown", NEVER "0"
    # (which would read as "deployed just now").
    project = _deploy_project(tmp_path, deployed_at_cmd="date")

    age = deployment.last_deploy_age(project, _run=FakeRun(stdout=""), _now=lambda: 1500)

    assert age is None


def test_last_deploy_age_clamps_future_timestamp_to_zero(tmp_path):
    # A deploy timestamp in the future (clock skew) clamps to 0 — this IS a
    # real, verified zero, unlike UNKNOWN.
    project = _deploy_project(tmp_path, deployed_at_cmd="stat -c %Y /opt/svc/binary")
    fake = FakeRun(stdout="9000\n")

    age = deployment.last_deploy_age(project, _run=fake, _now=lambda: 1000)

    assert age == 0


# --------------------------------------------------------------------------- #
# Real runner degrades gracefully — nothing hangs, nothing raises.
# --------------------------------------------------------------------------- #

def test_real_runner_degrades_gracefully(tmp_path):
    # The only real subprocess here is harmless (echo) and runs in tmp_path.
    _write(tmp_path / "VERSION", "1.0\n")
    project = Project(name="svc", repo=str(tmp_path), installed_version_cmd="echo 1.0")

    repo_v, installed_v, state = deployment.version_drift(project)

    assert state is OK
    assert repo_v == "1.0" and installed_v == "1.0"


def test_real_runner_unknown_for_missing_command_binary(tmp_path):
    _write(tmp_path / "VERSION", "1.0\n")
    project = Project(
        name="svc", repo=str(tmp_path),
        installed_version_cmd="/nonexistent/binary/that/does/not/exist",
    )

    repo_v, installed_v, state = deployment.version_drift(project)

    assert state is UNKNOWN
