"""Tests for flightdeck.core.release + flightdeck.commands.release.

The release command's job in this card is the REFUSAL half: check preconditions
and print the dry-run plan, mutating nothing. Every external call (git status,
git branch, the verify command) goes through an injectable ``_run`` of the
shell-string convention, so no test touches a real git repo, the network, or a
live system.

The injected runner FAILS CLOSED: any command the test did not explicitly wire
up returns returncode 128, so a surprise (e.g. a library deciding to run an
unexpected git command) surfaces as a test failure rather than a silent pass.
This is also how we assert "no git write": in the all-clear case every issued
command must be one of the handful of reads, never a mutating verb.
"""

import argparse

import pytest
import yaml

from flightdeck.commands import release as release_cmd
from flightdeck.core import release, registry


# --------------------------------------------------------------------------- #
# A fake shell runner (fail-closed)
# --------------------------------------------------------------------------- #

class _Proc:
    """Minimal process-like object: returncode / stdout / stderr."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_run(
    *,
    verify_cmd: str | None = "true",
    branch="main",
    dirty=False,
    verify_rc=0,
    verify_out="",
    verify_err="",
    git_rc=0,
    fail_at: str | None = None,
    install_cmd: str | None = None,
    install_rc=0,
    installed_cmd: str | None = None,
    installed_out="",
    installed_rc=0,
):
    """Build a (runner, calls) pair with the given git/verify/install behaviour.

    ``runner(cmd, cwd)`` is the injected ``_run``. It answers the git reads
    release issues, the one verify command, the VERSION bump write, the release
    mutation commands (commit / tag / push / gh release), an optional install
    command and an optional installed-version command. ANY other command fails
    closed (returncode 128) so an unexpected mutation surfaces loudly.

    When ``install_cmd`` is provided and matches the issued command, it returns
    ``install_rc``. When ``installed_cmd`` is provided and matches the issued
    command, it returns ``installed_out`` (default empty string -> the version
    is UNVERIFIED, never a match). A caller that wants a clean VERIFIED passes
    ``installed_out=<released version>``.

    ``fail_at`` forces that step's command to return non-zero so tests can
    exercise "stop at the first failure". Step names match execute()'s:
    bump, commit, tag, push, gh-release, install.
    """
    calls = []

    def run(cmd, cwd):
        calls.append((cmd, cwd))
        if cmd == "git status --porcelain":
            return _Proc(git_rc, "m\n" if dirty else "", "")
        if cmd == "git rev-parse --abbrev-ref HEAD":
            return _Proc(0, branch + "\n", "")
        if verify_cmd and cmd == verify_cmd:
            return _Proc(verify_rc, verify_out, verify_err)
        if install_cmd and cmd == install_cmd:
            if fail_at == "install":
                return _Proc(1, "", "install failed")
            return _Proc(install_rc, "", "")
        if installed_cmd and cmd == installed_cmd:
            return _Proc(installed_rc, installed_out, "")
        # the bump write (release.execute)
        if "VERSION" in cmd and ">" in cmd:
            if fail_at == "bump":
                return _Proc(1, "", "bump write failed")
            return _Proc(0)
        # release mutation commands
        if cmd.startswith("git add ") and fail_at == "commit":
            return _Proc(1, "", "commit failed")
        if cmd.startswith("git tag ") and fail_at == "tag":
            return _Proc(1, "", "tag failed")
        if cmd.startswith("git push ") and fail_at == "push":
            return _Proc(1, "", "push failed")
        if cmd.startswith("gh release ") and fail_at == "gh-release":
            return _Proc(1, "", "gh release failed")
        if (
            cmd.startswith("git add ")
            or cmd.startswith("git tag ")
            or cmd.startswith("git push ")
            or cmd.startswith("gh release ")
        ):
            return _Proc(0)
        return _Proc(128, "", f"unexpected command: {cmd!r}")

    return run, calls


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

def _write_registry(tmp_path, rows):
    """Write a registry yaml with the given project rows; return its path."""
    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump({"projects": rows}, sort_keys=False), encoding="utf-8")
    return str(p)


def _make_project(tmp_path, *, version="1.8.1", version_file="VERSION",
                  changelog=True, verify_cmd: str | None = "true",
                  changelog_target="1.9.0"):
    """A registry.Project backed by a real tmp_path repo on disk.

    Writes VERSION and CHANGELOG.md into the repo root so the file-reading
    preconditions (changelog section, version comparison) have something real
    to read. The changelog gets a section for BOTH the current version and the
    target (``changelog_target``) so the "has a section for this version"
    check passes by default. The git reads are always stubbed via ``_run`` —
    never real git.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / version_file).write_text(version + "\n", encoding="utf-8")
    if changelog:
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [1.8.1] — previous\n\n### Fixed\n- stuff\n\n"
            f"## [{changelog_target}] — upcoming\n\n### Changed\n- planned\n",
            encoding="utf-8",
        )
    return registry.Project(name="acme", repo=str(repo), verify=verify_cmd)


def _ns(**kw):
    """Build an argparse.Namespace with defaults for a release command."""
    defaults = dict(project="acme", version="1.9.0", registry=None, run=None, apply=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# --------------------------------------------------------------------------- #
# core.release -- preconditions
# --------------------------------------------------------------------------- #

def test_all_clear_returns_no_problems(tmp_path):
    """A clean tree on main with green verify, changelog section and a higher
    version yields no problems."""
    proj = _make_project(tmp_path)
    run, _ = _make_run()
    assert release.preconditions(proj, "1.9.0", _run=run) == []


def test_dirty_tree_refused(tmp_path):
    proj = _make_project(tmp_path)
    run, _ = _make_run(dirty=True)
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["dirty"]


def test_wrong_branch_refused(tmp_path):
    proj = _make_project(tmp_path)
    run, _ = _make_run(branch="dev")
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["wrong-branch"]
    assert "dev" in problems[0].message and "main" in problems[0].message


def test_failing_verify_refused(tmp_path):
    proj = _make_project(tmp_path, verify_cmd="make test")
    run, _ = _make_run(verify_cmd="make test", verify_rc=1, verify_err="boom: broke")
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["verify-failed"]
    assert "boom: broke" in problems[0].message


def test_no_verify_refused(tmp_path):
    """No verify command configured is a refusal, never a silent pass."""
    proj = _make_project(tmp_path, verify_cmd=None)
    run, _ = _make_run(verify_cmd=None)
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["no-verify"]


def test_missing_changelog_section_refused(tmp_path):
    """CHANGELOG.md present but with no section for the target version."""
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / "VERSION").write_text("1.8.1\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.8.1] — previous\n", encoding="utf-8"
    )
    proj = registry.Project(name="acme", repo=str(repo), verify="true")
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["missing-changelog"]


def test_missing_changelog_file_refused(tmp_path):
    """No CHANGELOG.md at all is also a missing-section refusal."""
    proj = _make_project(tmp_path, changelog=False)
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["missing-changelog"]


def test_equal_version_refused(tmp_path):
    proj = _make_project(tmp_path, version="1.9.0")
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["version-not-greater"]


def test_lower_version_refused(tmp_path):
    proj = _make_project(tmp_path, version="1.9.0", changelog_target="1.8.0")
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.8.0", _run=run)
    assert [p.code for p in problems] == ["version-not-greater"]


def test_version_not_greater_ignores_v_prefix(tmp_path):
    proj = _make_project(tmp_path, version="1.8.1")
    run, _ = _make_run()
    problems = release.preconditions(proj, "v1.8.1", _run=run)
    assert [p.code for p in problems] == ["version-not-greater"]


def test_comparison_is_numeric_not_lexicographic(tmp_path):
    """1.8.10 must compare greater than 1.8.9 (numeric, not string sort)."""
    proj = _make_project(tmp_path, version="1.8.9", changelog_target="1.8.10")
    run, _ = _make_run()
    assert release.preconditions(proj, "1.8.10", _run=run) == []


# --- prerelease ordering (semver-aware) ---

def test_parse_version_prerelease_sorts_before_its_ga():
    """The core fix: 1.8.1-rc1 must compare LESS than 1.8.1 (semver)."""
    assert release._parse_version("1.8.1-rc1") < release._parse_version("1.8.1")
    assert release._parse_version("1.8.1-beta2") < release._parse_version("1.8.1")
    # same-core, different prereleases tie-break by suffix (rc1 < rc2)
    assert release._parse_version("1.8.1-rc1") < release._parse_version("1.8.1-rc2")
    # a higher prerelease of the OLD line is still allowed
    assert release._parse_version("1.9.0-rc1") > release._parse_version("1.8.1")
    # a prerelease still beats everything that sorts below its core
    assert release._parse_version("1.8.1-rc1") > release._parse_version("1.8.0")


def test_parse_version_matches_ga_and_numeric_bounds():
    """GA ordering, numeric core, v-prefix, and the no-core sentinel."""
    pv = release._parse_version
    assert pv("1.8.1") == pv("v1.8.1") == pv("V1.8.1")
    assert pv("1.8.10") > pv("1.8.9")          # numeric, not string sort
    assert pv("2.0.0") > pv("1.9.9")           # major outweighs minor
    assert pv("1.8.1") > pv("1.8")            # missing patch reads as 0
    assert pv("1.8.1rc1") == pv("1.8.1-rc1")  # no-separator normalized
    # no usable core sorts below everything
    assert pv("") < pv("1.0.0")
    assert pv("abc") < pv("1.0.0")


def test_prerelease_after_its_ga_refused(tmp_path):
    """current=1.8.1 GA, target=1.8.1-rc1 -> refused as version-not-greater.

    This is the audit finding: without semver-aware ordering the rc1 would
    have been sorted ABOVE its GA and allowed through.
    """
    proj = _make_project(tmp_path, version="1.8.1", changelog_target="1.8.1-rc1")
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.8.1-rc1", _run=run)
    assert [p.code for p in problems] == ["version-not-greater"]


def test_prerelease_before_its_ga_allowed(tmp_path):
    """current=1.8.0 GA, target=1.8.1-rc1 -> allowed (valid pre-GA candidate)."""
    proj = _make_project(tmp_path, version="1.8.0", changelog_target="1.8.1-rc1")
    run, _ = _make_run()
    assert release.preconditions(proj, "1.8.1-rc1", _run=run) == []


def test_equal_prerelease_refused(tmp_path):
    """Re-releasing the same prerelease (1.8.1-rc1 on top of itself) is refused."""
    proj = _make_project(tmp_path, version="1.8.1-rc1", changelog_target="1.8.1-rc1")
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.8.1-rc1", _run=run)
    assert [p.code for p in problems] == ["version-not-greater"]


def test_missing_version_file_refused(tmp_path):
    proj = _make_project(tmp_path, version_file="NOPE")
    run, _ = _make_run()
    problems = release.preconditions(proj, "1.9.0", _run=run)
    assert [p.code for p in problems] == ["version-unreadable"]


def test_changelog_section_matches_bracketed_and_v(tmp_path):
    """A section like ``## [1.9.0] — title`` or ``## v1.9.0`` matches."""
    core_has = release._has_changelog_section
    assert core_has("## [1.9.0] — ship it\n", "1.9.0")
    assert core_has("## v1.9.0 — ship it\n", "1.9.0")
    assert core_has("## 1.9.0\n", "v1.9.0")
    assert not core_has("## 1.9.1 — ship it\n", "1.9.0")
    assert not core_has("## 1.90 — ship it\n", "1.9.0")


# --------------------------------------------------------------------------- #
# core.release -- execute (step 1: bump VERSION)
# --------------------------------------------------------------------------- #

def test_execute_bumps_version_via_run():
    """execute() issues the VERSION bump write FIRST, carrying the target."""
    proj = registry.Project(name="acme", repo="/tmp/acme", verify="true")
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        return _Proc(0)

    release.execute(proj, "1.10.0", _run=run)

    # the FIRST command issued is the bump write (with the repo as cwd)
    cmd = issued[0]
    assert "1.10.0" in cmd, f"bump must carry the target version, got: {cmd!r}"
    assert "> VERSION" in cmd, f"bump must write to VERSION, got: {cmd!r}"


def test_execute_strips_v_prefix(tmp_path):
    """A leading ``v`` on the version is stripped before the write."""
    proj = _make_project(tmp_path)
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        return _Proc(0)

    release.execute(proj, "v1.10.0", _run=run)

    assert "1.10.0" in issued[0]
    assert "v1.10.0" not in issued[0]


def test_execute_honors_version_file_override(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "VERSION").write_text("1.8.1\n", encoding="utf-8")
    proj = registry.Project(name="acme", repo=str(repo), verify="true",
                            version_file="MYVERSION")
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        return _Proc(0)

    release.execute(proj, "1.10.0", _run=run)

    assert "> MYVERSION" in issued[0]


def test_execute_raises_on_failed_write():
    """A non-zero returncode from the write is surfaced, not silently passed."""
    proj = registry.Project(name="acme", repo="/tmp/acme", verify="true")

    def run(cmd, cwd):
        return _Proc(1, "", "boom")

    try:
        release.execute(proj, "1.10.0", _run=run)
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected execute to raise on a failed write")


# --------------------------------------------------------------------------- #
# command layer -- flightdeck release
# --------------------------------------------------------------------------- #

def test_command_dirty_refused(tmp_path, capsys):
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, _ = _make_run(dirty=True)
    args = _ns(registry=reg, run=run)

    code = release_cmd.run(args, reg)

    assert code == 1
    assert "dirty" in capsys.readouterr().err


def test_command_wrong_branch_refused(tmp_path, capsys):
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, _ = _make_run(branch="dev")
    args = _ns(registry=reg, run=run)

    code = release_cmd.run(args, reg)

    assert code == 1
    assert "expected 'main'" in capsys.readouterr().err


def test_command_failing_verify_refused(tmp_path, capsys):
    proj = _make_project(tmp_path, verify_cmd="make test")
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "make test"}])
    run, _ = _make_run(verify_cmd="make test", verify_rc=1, verify_err="boom")
    args = _ns(registry=reg, run=run)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "verify" in err and "boom" in err


def test_command_missing_changelog_refused_and_prints_skeleton(tmp_path, capsys):
    proj = _make_project(tmp_path, changelog=False)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, _ = _make_run()
    args = _ns(registry=reg, run=run)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "missing-changelog" in err
    # the section skeleton is printed for the operator to fill in
    assert "## [1.9.0] — describe this release" in err
    assert "### Added" in err and "### Changed" in err and "### Fixed" in err


def test_command_equal_version_refused(tmp_path, capsys):
    proj = _make_project(tmp_path, version="1.9.0")
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, _ = _make_run()
    args = _ns(version="1.9.0", registry=reg, run=run)

    code = release_cmd.run(args, reg)

    assert code == 1
    assert "not greater" in capsys.readouterr().err


def test_command_all_clear_prints_plan_in_order_and_no_git_write(tmp_path, capsys):
    """All-clear prints the ordered plan, exits 0, and performs no git write."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, calls = _make_run(verify_cmd="true")
    args = _ns(registry=reg, run=run)

    code = release_cmd.run(args, reg)

    assert code == 0
    out = capsys.readouterr().out
    # the ordered plan
    assert "release plan for acme 1.9.0" in out
    order = [
        "bump VERSION",
        "commit the version bump",
        "tag the release",
        "push",
        "create the GitHub release",
        "install the release",
        "verify the installed version",
    ]
    idx = [out.index(s) for s in order]
    assert idx == sorted(idx), "plan steps must appear in the documented order"

    # NOTHING mutating was executed: the runner only saw the three reads.
    issued = [cmd for cmd, _ in calls]
    assert issued == [
        "git status --porcelain",
        "git rev-parse --abbrev-ref HEAD",
        "true",  # the verify command (passed through core.verify)
    ]
    mutating = ("commit", "tag", "push", "gh release", "add ", "checkout", "install")
    for cmd in issued:
        assert not any(m in cmd for m in mutating), f"mutating command issued: {cmd!r}"


def test_command_user_reports_no_git_write_without_run(tmp_path, capsys):
    """When no ``_run`` is injected, run() must not fall through to the shell.

    The command defaults ``_run`` to None; in the CLI path cli.py never sets it.
    But run() must refuse (or route to core's default runner) — the test here
    guards that run() itself never shells out: with ``run=None`` and a problem
    detected, no subprocess is spawned by the command layer itself.
    """
    proj = _make_project(tmp_path, version="1.9.0")  # equal version -> refused
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    args = _ns(version="1.9.0", registry=reg)  # run stays None

    code = release_cmd.run(args, reg)

    assert code == 1
    assert "not greater" in capsys.readouterr().err


def test_command_unknown_project(tmp_path, capsys):
    reg = _write_registry(tmp_path, [])
    args = _ns(project="ghost", registry=reg, run=lambda c, d: _Proc(128))

    code = release_cmd.run(args, reg)

    assert code == 2
    assert "ghost" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# build_subparser wiring
# --------------------------------------------------------------------------- #

def test_build_subparser_and_run_hook_present():
    assert hasattr(release_cmd, "build_subparser")
    assert hasattr(release_cmd, "run")


def test_subparser_accepts_project_and_version(capsys):
    parser = argparse.ArgumentParser(prog="flightdeck")
    sub = parser.add_subparsers(dest="command")
    release_cmd.build_subparser(sub)
    ns = parser.parse_args(["release", "acme", "1.9.0"])
    assert ns.command == "release"
    assert ns.project == "acme"
    assert ns.version == "1.9.0"


def test_discovery_loads_release_module():
    """cli._discover_commands finds release via its run/build_subparser hooks."""
    from flightdeck.cli import _discover_commands

    mods = _discover_commands()
    assert "release" in mods


# --------------------------------------------------------------------------- #
# command layer -- --apply performs the release (bump, commit, tag, push, gh)
# --------------------------------------------------------------------------- #

_COMMIT = "git add VERSION && git commit -m \"release: v1.9.0\""


def _mutation_cmds(calls, install_cmd=None):
    """The mutating release commands issued, minus the pure preconditions.

    Includes the optional registry-declared install command when given.
    """
    return [
        cmd
        for cmd, _ in calls
        if cmd.startswith("git add ")
        or cmd.startswith("git tag ")
        or cmd.startswith("git push ")
        or cmd.startswith("gh release ")
        or (install_cmd is not None and cmd == install_cmd)
        or ("VERSION" in cmd and ">" in cmd)
    ]


def test_command_apply_full_release_in_order(tmp_path, capsys):
    """`--apply` on a clean project with install declared issues all seven
    steps in exact order — the verified happy path."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "install_cmd": "make install",
        "installed_version_cmd": "acme --version",
    }])
    run, calls = _make_run(
        verify_cmd="true",
        install_cmd="make install",
        installed_cmd="acme --version",
        installed_out="1.9.0\n",
    )
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    ms = _mutation_cmds(calls, install_cmd="make install")
    # the seven release commands in order: bump, commit, tag, push, gh release,
    # install, verify (verify is a read, not in _mutation_cmds)
    assert ms[0].startswith("sh -c 'printf"), f"first must be bump, got: {ms[0]!r}"
    assert ms[1] == _COMMIT, f"second must be the commit, got: {ms[1]!r}"
    assert ms[2].startswith("git tag -a v1.9.0"), f"third must be the tag: {ms[2]!r}"
    assert ms[3] == "git push origin main --tags", f"fourth must be push: {ms[3]!r}"
    assert ms[4].startswith("gh release create v1.9.0"), f"fifth must be gh release: {ms[4]!r}"
    assert ms[5] == "make install", f"sixth must be install: {ms[5]!r}"
    assert len(ms) == 6, f"six mutating commands + verify read expected, got: {ms!r}"
    out = capsys.readouterr().out
    for step in ("bump", "commit", "tag", "push", "gh-release", "install", "verify"):
        assert f"released step: {step}" in out, f"step {step} not reported"
    assert "bumped acme VERSION to 1.9.0" in out


def test_command_apply_bumps_version(tmp_path, capsys):
    """`--apply` on a clean project issues the VERSION bump write (kept)."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "install_cmd": "make install",
        "installed_version_cmd": "acme --version",
    }])
    run, calls = _make_run(
        verify_cmd="true",
        install_cmd="make install",
        installed_cmd="acme --version",
        installed_out="1.9.0\n",
    )
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    write_cmds = [c for c, _ in calls if "VERSION" in c and ">" in c]
    assert write_cmds, "--apply must issue the VERSION bump write"
    assert "1.9.0" in write_cmds[0]
    assert "bumped acme VERSION to 1.9.0" in capsys.readouterr().out


def test_command_apply_strips_v_prefix(tmp_path, capsys):
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "install_cmd": "make install",
        "installed_version_cmd": "acme --version",
    }])
    run, calls = _make_run(
        verify_cmd="true",
        install_cmd="make install",
        installed_cmd="acme --version",
        installed_out="1.9.0\n",
    )
    args = _ns(version="v1.9.0", registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    write_cmds = [c for c, _ in calls if "VERSION" in c and ">" in c]
    assert write_cmds
    assert "1.9.0" in write_cmds[0]
    assert "v1.9.0" not in write_cmds[0]


def test_command_apply_refused_on_problems(tmp_path, capsys):
    """`--apply` with a refused precondition must not bump, returns non-zero."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, calls = _make_run(dirty=True)
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    assert "dirty" in capsys.readouterr().err
    # NO mutating command must have been issued
    assert _mutation_cmds(calls) == [], "a refused release must execute nothing"


def test_command_no_apply_issues_no_mutation(tmp_path, capsys):
    """Without `--apply`, run() prints only the plan; no mutating command
    reaches the runner."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, calls = _make_run(verify_cmd="true")
    args = _ns(registry=reg, run=run)  # apply=False

    code = release_cmd.run(args, reg)

    assert code == 0
    assert _mutation_cmds(calls) == [], "--apply not set -> no mutating command"
    assert "release plan for acme 1.9.0" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# command layer -- --apply stops at the first failing step
# --------------------------------------------------------------------------- #

def test_command_apply_stops_at_failed_step(tmp_path, capsys):
    """If a step fails, run() reports that step, returns non-zero, and issues
    NO later command."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, calls = _make_run(verify_cmd="true", fail_at="tag")
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "stopped at step 'tag'" in err, f"must report the failing step: {err}"
    ms = _mutation_cmds(calls)
    # bump and commit ran, tag failed; push and gh release NEVER issued
    assert ms[0].startswith("sh -c 'printf")
    assert ms[1] == _COMMIT
    assert ms[2].startswith("git tag -a")
    assert len(ms) == 3, f"no command after the failed tag, got: {ms!r}"


@pytest.mark.parametrize(
    "fail_step",
    ["bump", "commit", "tag", "push", "gh-release"],
)
def test_command_apply_stops_at_each_step(tmp_path, capsys, fail_step):
    """Every step, when it fails, halts the release and issues nothing after it."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, calls = _make_run(verify_cmd="true", fail_at=fail_step)
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    expected_position = ["bump", "commit", "tag", "push", "gh-release"].index(fail_step)
    ms = _mutation_cmds(calls)
    # only the mutating commands UP TO the failing one were issued
    assert len(ms) == expected_position + 1, (
        f"fail at {fail_step}: issued after the failure, got {ms!r}"
    )
    assert code == 1
    assert f"stopped at step '{fail_step}'" in capsys.readouterr().err


def test_command_apply_tag_message_carries_changelog_section(tmp_path, capsys):
    """The `git tag -a` command embeds the CHANGELOG section for the version."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "install_cmd": "make install",
        "installed_version_cmd": "acme --version",
    }])
    run, calls = _make_run(
        verify_cmd="true",
        install_cmd="make install",
        installed_cmd="acme --version",
        installed_out="1.9.0\n",
    )
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    tag_cmd = next(c for c, _ in calls if c.startswith("git tag -a"))
    # the tag message must carry the CHANGELOG section for v1.9.0
    assert "## [1.9.0] — upcoming" in tag_cmd
    assert "### Changed" in tag_cmd
    assert "- planned" in tag_cmd
    gh_cmd = next(c for c, _ in calls if c.startswith("gh release "))
    assert "### Changed" in gh_cmd and "- planned" in gh_cmd


def test_command_apply_generated_messages_have_no_attribution(tmp_path, capsys):
    """No commit/tag/release message this tool generates contains an
    AI-attribution string (hard requirement of the target repos)."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "install_cmd": "make install",
        "installed_version_cmd": "acme --version",
    }])
    run, calls = _make_run(
        verify_cmd="true",
        install_cmd="make install",
        installed_cmd="acme --version",
        installed_out="1.9.0\n",
    )
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    for marker in ("claude", "anthropic", "openai", "gpt", "copilot"):
        for cmd, _ in calls:
            assert marker not in cmd.lower(), (
                f"attribution marker {marker!r} leaked into a command: {cmd!r}"
            )


# --------------------------------------------------------------------------- #
# core.release -- execute full flow
# --------------------------------------------------------------------------- #

def test_execute_returns_steps_in_order():
    """execute() returns the completed step names in order on success.

    With no install_cmd declared, steps 1-5 run and install is skipped — the
    outcome is UNVERIFIED (not a clean success)."""
    proj = registry.Project(name="acme", repo="/tmp/acme", verify="true")
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        return _Proc(0)

    result = release.execute(proj, "1.10.0", _run=run)
    assert result.completed == ["bump", "commit", "tag", "push", "gh-release"]
    # no install command declared -> UNVERIFIED, never a clean success
    assert result.outcome.status == release.UNVERIFIED
    assert result.verified is False


def test_execute_tag_and_gh_carry_changelog_section(tmp_path):
    """The tag and gh-release commands embed the version's CHANGELOG section."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.10.0] — the big one\n\n### Added\n- thing\n\n"
        "## [1.9.0] — previous\n",
        encoding="utf-8",
    )

    proj = registry.Project(name="acme", repo=str(repo), verify="true")
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        return _Proc(0)

    release.execute(proj, "1.10.0", _run=run)

    tag_cmd = next(c for c in issued if c.startswith("git tag -a"))
    gh_cmd = next(c for c in issued if c.startswith("gh release "))
    assert "the big one" in tag_cmd and "### Added" in tag_cmd and "thing" in tag_cmd
    assert "the big one" in gh_cmd and "### Added" in gh_cmd


def test_execute_stops_at_first_failure():
    """execute() raises ReleaseStepError at the failing step and issues no
    command after it."""
    proj = registry.Project(name="acme", repo="/tmp/acme-xl", verify="true")
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        if cmd.startswith("git push "):
            return _Proc(1, "", "remote rejected")
        return _Proc(0)

    try:
        release.execute(proj, "1.10.0", _run=run)
    except release.ReleaseStepError as exc:
        assert exc.step == "push", f"expected the push step, got {exc.step!r}"
    else:  # pragma: no cover
        raise AssertionError("expected ReleaseStepError on a failed push")

    # nothing after the failed push (no gh release)
    assert not any(c.startswith("gh release ") for c in issued), issued


def test_subparser_accepts_apply_flag(capsys):
    parser = argparse.ArgumentParser(prog="flightdeck")
    sub = parser.add_subparsers(dest="command")
    release_cmd.build_subparser(sub)
    ns = parser.parse_args(["release", "acme", "1.9.0", "--apply"])
    assert ns.apply is True
    ns = parser.parse_args(["release", "acme", "1.9.0"])
    assert ns.apply is False


# --------------------------------------------------------------------------- #
# command layer -- --apply install + post-install verify (merged is not live)
# --------------------------------------------------------------------------- #

_VERIFIED_RECIPE = {
    "name": "acme",
    "repo": None,  # replaced with proj.repo by the caller
    "verify": "true",
    "install_cmd": "make install",
    "installed_version_cmd": "acme --version",
}


def _verified_registry(tmp_path, proj):
    """A registry row declaring an install command, for the verified path."""
    row = dict(_VERIFIED_RECIPE)
    row["repo"] = proj.repo
    return _write_registry(tmp_path, [row])


def _verified_run(installed_out="1.9.0\n", **kw):
    """A runner that installs and reports the installed version as matching."""
    kw.setdefault("verify_cmd", "true")
    kw.setdefault("install_cmd", "make install")
    kw.setdefault("installed_cmd", "acme --version")
    kw.setdefault("installed_out", installed_out)
    return _make_run(**kw)


def test_apply_install_runs_when_declared(tmp_path, capsys):
    """A registry-declared install command is actually run (pip/npm/opaque)."""
    proj = _make_project(tmp_path)
    reg = _verified_registry(tmp_path, proj)
    run, calls = _verified_run()
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    issued = [c for c, _ in calls]
    assert "make install" in issued, "the declared install command must run"


def test_apply_install_skipped_with_message_when_not_declared(tmp_path, capsys):
    """No install_cmd -> install skipped with a clear message, and the release
    is UNVERIFIED (non-zero), never a clean success."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{"name": "acme", "repo": proj.repo, "verify": "true"}])
    run, calls = _make_run(verify_cmd="true")
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "install skipped" in err
    assert "UNVERIFIED" in err
    # no install command -> no install issued
    assert not any("install" in c for c, _ in calls), calls


def test_apply_merged_is_not_live_after_install_reports_failure(tmp_path, capsys):
    """A version mismatch after install is a FAILURE, not a warning — this is
    the 'merged is not live' rule: the installed artifact differs from the just
    released version, e.g. a daemon running days-old code or a CLI executing
    from an install path rather than the repo."""
    proj = _make_project(tmp_path)
    reg = _verified_registry(tmp_path, proj)
    run, calls = _verified_run(installed_out="1.8.1\n")  # stale — install failed
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "merged is not live" in err
    assert "1.8.1" in err and "1.9.0" in err  # both versions surfaced
    # install DID run — the failure is a verification failure, not a skip
    issued = [c for c, _ in calls]
    assert "make install" in issued


def test_apply_verification_match_reports_success_with_both_versions(tmp_path, capsys):
    """A match after install reports success carrying BOTH versions."""
    proj = _make_project(tmp_path)
    reg = _verified_registry(tmp_path, proj)
    run, _ = _verified_run(installed_out="1.9.0\n")
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    out = capsys.readouterr().out
    assert "verified: installed version 1.9.0 matches released 1.9.0" in out
    assert "1.9.0" in out


def test_apply_no_install_command_means_unverified_never_ok(tmp_path, capsys):
    """No install command -> UNVERIFIED, never OK. Even with an
    installed_version_cmd declared, no install means we cannot confirm the
    released version is live, so it must not read as a clean success."""
    proj = _make_project(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "installed_version_cmd": "acme --version",
    }])
    run, calls = _make_run(
        verify_cmd="true",
        installed_cmd="acme --version",
        installed_out="1.9.0\n",
    )
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "UNVERIFIED" in err
    # the installed_version_cmd must NOT have been run — no install happened
    issued = [c for c, _ in calls]
    assert "acme --version" not in issued


def test_apply_install_failure_is_failed(tmp_path, capsys):
    """An install command that exits non-zero stops the release as FAILED and
    no verification is attempted."""
    proj = _make_project(tmp_path)
    reg = _verified_registry(tmp_path, proj)
    run, calls = _verified_run(installed_out="1.9.0\n", install_rc=1)
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    err = capsys.readouterr().err
    assert "stopped at step 'install'" in err
    # no verification read after a failed install
    assert "acme --version" not in [c for c, _ in calls]


def test_apply_cannot_reread_installed_version_is_unverified(tmp_path, capsys):
    """Install runs but the installed version cannot be re-read (no
    installed_version_cmd, it fails, or prints nothing) -> UNVERIFIED."""
    proj = _make_project(tmp_path)
    reg = _verified_registry(tmp_path, proj)
    run, _ = _verified_run(installed_out="")  # prints nothing -> cannot verify
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 1
    assert "UNVERIFIED" in capsys.readouterr().err


def test_dry_run_executes_nothing_without_apply(tmp_path, capsys):
    """Without --apply, not one mutation runs: no bump, no install, no
    installed-version read. The command only checks preconditions and prints
    the plan."""
    proj = _make_project(tmp_path)
    reg = _verified_registry(tmp_path, proj)
    run, calls = _verified_run()
    args = _ns(registry=reg, run=run, apply=False)

    code = release_cmd.run(args, reg)

    assert code == 0
    issued = [c for c, _ in calls]
    assert issued == [
        "git status --porcelain",
        "git rev-parse --abbrev-ref HEAD",
        "true",  # verify
    ]
    assert "make install" not in issued
    assert "acme --version" not in issued
    assert not [c for c in issued if "VERSION" in c and ">" in c]
    out = capsys.readouterr().out
    assert "release plan for acme 1.9.0" in out


def test_execute_full_seven_step_order():
    """execute() with install declared runs the full 7-step order: bump,
    commit, tag, push, gh release, install, verify — against the injected
    runner."""
    proj = registry.Project(
        name="acme",
        repo="/tmp/acme",
        verify="true",
        install_cmd="make install",
        installed_version_cmd="acme --version",
    )
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        if cmd == "acme --version":
            return _Proc(0, "1.10.0\n", "")
        return _Proc(0)

    result = release.execute(proj, "1.10.0", _run=run)

    # the exact ordered sequence, including install and verify
    order = ["bump", "commit", "tag", "push", "gh-release", "install", "verify"]
    assert result.completed == order
    assert result.verified is True
    assert result.outcome.status == release.VERIFIED
    assert result.outcome.installed_version == "1.10.0"
    # install precedes the installed-version read in the issued commands
    assert issued.index("make install") < issued.index("acme --version")


def test_install_failure_stops_verify_not_attempted():
    """A failing install command raises ReleaseStepError and no verification
    is attempted."""
    proj = registry.Project(
        name="acme",
        repo="/tmp/acme",
        verify="true",
        install_cmd="make install",
        installed_version_cmd="acme --version",
    )
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        if cmd == "make install":
            return _Proc(1, "", "install exploded")
        return _Proc(0)

    try:
        release.execute(proj, "1.10.0", _run=run)
    except release.ReleaseStepError as exc:
        assert exc.step == "install", f"expected install step, got {exc.step!r}"
    else:  # pragma: no cover
        raise AssertionError("expected ReleaseStepError on a failed install")

    # no installed-version read after a failed install
    assert "acme --version" not in issued


# --------------------------------------------------------------------------- #
# core.release -- bump updates EVERY version source (VERSION + pyproject), the
# v0.2.0 incident: the VERSION file was bumped but pyproject.toml's
# `[project] version` was left stale, so the tag/release went out against a
# package that still identified itself as the previous version.
# --------------------------------------------------------------------------- #

def _pyproject(version: str | None = "1.8.1", *, dynamic=False):
    """A pyproject.toml body exercising formatting/comments elsewhere, for
    the byte-identical assertion. ``version=None`` omits the literal version
    line; ``dynamic=True`` declares a dynamic version instead."""
    lines = [
        "# FlightDeck build config — hand-edited, keep the comments.\n",
        "[build-system]\n",
        'requires = ["setuptools>=70"]\n',
        'build-backend = "setuptools.build_meta"\n',
        "\n",
        "[project]\n",
        'name = "flightdeck"  # trailing comment kept\n',
    ]
    if dynamic:
        lines.append('dynamic = ["version"]\n')
    elif version is not None:
        lines.append(f'version = "{version}"\n')
    lines += [
        'description = "a deck of flights"   # three spaces before #\n',
        "\n",
        "[tool.pytest.ini_options]\n",
        'addopts = "-q"  # be terse\n',
    ]
    return "".join(lines)


def _project_with_pyproject(tmp_path, *, version="1.8.1", py_version="1.8.1"):
    """A registry.Project whose repo root carries BOTH a VERSION file and a
    pyproject.toml declaring a literal `[project] version`."""
    proj = _make_project(tmp_path, version=version)
    (tmp_path / "repo" / "pyproject.toml").write_text(
        _pyproject(py_version), encoding="utf-8"
    )
    return proj


def test_parse_pyproject_version_extracts_literal():
    assert release._parse_pyproject_version(_pyproject("1.8.1")) == "1.8.1"


def test_parse_pyproject_version_none_for_dynamic_and_absent():
    assert release._parse_pyproject_version(_pyproject(dynamic=True)) is None
    assert release._parse_pyproject_version(_pyproject(None)) is None


def test_bump_updates_both_version_sources(tmp_path):
    """execute() issues the bump for BOTH VERSION (via the injected runner)
    and pyproject.toml (in-process), and ReleaseResult.files_written lists
    both files."""
    proj = _project_with_pyproject(tmp_path)
    run, calls = _make_run()  # the VERSION write goes through _run, git reads stubbed

    result = release.execute(proj, "1.9.0", _run=run)

    # the VERSION bump was issued through the injected runner
    assert any("1.9.0" in c and "> VERSION" in c for c, _ in calls)
    # pyproject on disk was rewritten in-process to carry the new version
    on_disk = (tmp_path / "repo" / "pyproject.toml").read_text(encoding="utf-8")
    assert release._parse_pyproject_version(on_disk) == "1.9.0"
    assert result.files_written == ("VERSION", "pyproject.toml")


def test_project_with_only_version_still_works(tmp_path):
    """No pyproject.toml -> bump writes VERSION only; files_written lists it."""
    proj = _make_project(tmp_path)
    run, _ = _make_run()

    result = release.execute(proj, "1.9.0", _run=run)

    assert result.files_written == ("VERSION",)


def test_pyproject_byte_identical_outside_version_line(tmp_path):
    """Only the `[project] version` value changes; every other byte of the
    pyproject.toml (comments, whitespace, other fields, trailing newline) is
    byte-identical after the bump."""
    proj = _project_with_pyproject(tmp_path, py_version="1.8.1")
    before = (tmp_path / "repo" / "pyproject.toml").read_text(encoding="utf-8")
    run, _ = _make_run()

    release.execute(proj, "1.9.0", _run=run)

    after = (tmp_path / "repo" / "pyproject.toml").read_text(encoding="utf-8")
    # the whole file minus the version value is unchanged
    assert after.replace("1.9.0", "<V>") == before.replace("1.8.1", "<V>")
    # and only the version value differs
    assert before != after
    assert "1.8.1" in before and "1.8.1" not in after
    # comments/formatting survived
    assert "trailing comment kept" in after
    assert "three spaces before #" in after
    assert "be terse" in after


def test_v020_incident_pyproject_disagreement_refused(tmp_path):
    """Pre-existing disagreement between VERSION and pyproject's version is a
    PRECONDITION failure naming BOTH values — the direct lesson of v0.2.0,
    where releasing from an inconsistent state shipped a self-identifying
    0.1.0 package. Named for the incident."""
    proj = _project_with_pyproject(tmp_path, version="1.8.1", py_version="1.8.0")
    run, _ = _make_run()

    problems = release.preconditions(proj, "1.9.0", _run=run)

    codes = [p.code for p in problems]
    assert "version-mismatch" in codes, codes
    mismatch = next(p for p in problems if p.code == "version-mismatch")
    assert "1.8.1" in mismatch.message and "1.8.0" in mismatch.message


def test_dynamic_pyproject_version_handled(tmp_path):
    """A dynamic `[project] version` is harmless: the bump writes what exists
    (VERSION only) and never errors."""
    proj = _make_project(tmp_path)
    (tmp_path / "repo" / "pyproject.toml").write_text(
        _pyproject(dynamic=True), encoding="utf-8"
    )
    run, _ = _make_run()
    result = release.execute(proj, "1.9.0", _run=run)
    assert result.files_written == ("VERSION",)
    # the dynamic pyproject was NOT rewritten
    on_disk = (tmp_path / "repo" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in on_disk


def test_absent_pyproject_version_handled(tmp_path):
    """No pyproject.toml at all is fine: the bump updates just VERSION."""
    proj = _make_project(tmp_path)
    run, _ = _make_run()
    result = release.execute(proj, "1.9.0", _run=run)
    assert result.files_written == ("VERSION",)


def test_command_apply_reports_files_written(tmp_path, capsys):
    """The command layer's bump output reports exactly which version sources
    were written."""
    proj = _project_with_pyproject(tmp_path)
    reg = _write_registry(tmp_path, [{
        "name": "acme", "repo": proj.repo, "verify": "true",
        "install_cmd": "make install",
        "installed_version_cmd": "acme --version",
    }])
    run, _ = _verified_run(installed_out="1.9.0\n")
    args = _ns(registry=reg, run=run, apply=True)

    code = release_cmd.run(args, reg)

    assert code == 0
    out = capsys.readouterr().out
    assert "bumped acme VERSION to 1.9.0 (wrote VERSION, pyproject.toml)" in out


def test_execute_commit_stages_every_file_the_bump_wrote(tmp_path):
    """The commit step must `git add` every file bump wrote, not just VERSION.

    Real bug behind the v0.2.2 release: bump correctly wrote both VERSION and
    pyproject.toml's [project] version, but the commit step's `git add` was
    hardcoded to `version_file` alone, so pyproject.toml's bump was left
    uncommitted -- the released tag disagreed with the installed package.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "acme"\nversion = "1.9.0"\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.10.0] — release\n\n### Added\n- thing\n", encoding="utf-8"
    )
    proj = registry.Project(name="acme", repo=str(repo), verify="true")
    issued = []

    def run(cmd, cwd):
        issued.append(cmd)
        return _Proc(0)

    release.execute(proj, "1.10.0", _run=run)

    commit_cmd = next(c for c in issued if c.startswith("git add"))
    assert "VERSION" in commit_cmd
    assert "pyproject.toml" in commit_cmd
