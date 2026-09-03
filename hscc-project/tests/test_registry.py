"""Tests for flightdeck.core.registry.

All tests use pytest's tmp_path -- never touch ~/.flightdeck, git, or the
network. Pure temp-file logic only.
"""

import os

import pytest

from flightdeck.core.registry import (
    DuplicateProjectError,
    MissingRepoError,
    Project,
    ProjectNotFoundError,
    RegistryError,
    add_project,
    bind_topic,
    dependent_notice,
    dependents_for,
    detect_project_from_cwd,
    get_project,
    load_registry,
    project_for_cwd,
    remove_project,
    resolve_project_arg,
    save_registry,
    unbind_topic,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _home_abs(repo: str) -> str:
    """Expand `~` in a repo string the same way the module does, for asserts."""
    return os.path.expanduser(repo)


def test_minimal_registry_loads_repo_only(tmp_path):
    """A project with only `repo` set must load fine (no optional fields)."""
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: hscc\n    repo: ~/dev/hscc\n")

    projects = load_registry(str(reg))

    assert len(projects) == 1
    assert projects[0].name == "hscc"
    assert projects[0].repo == _home_abs("~/dev/hscc")
    # every optional field degrades to None -> "unknown", never an error
    assert projects[0].board is None
    assert projects[0].topic is None
    assert projects[0].verify is None
    assert projects[0].roadmap is None


def test_missing_optional_fields_are_none_not_errors(tmp_path):
    """Only `repo` present; all optional fields are None, no row dropped."""
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: x\n    repo: /tmp/x\n")

    projects = load_registry(str(reg))

    assert len(projects) == 1
    p = projects[0]
    assert p.board is None and p.topic is None
    assert p.topic_name is None
    assert p.verify is None and p.roadmap is None
    assert p.missing_fields() == [
        "board",
        "session",
        "topic",
        "topic_name",
        "verify",
        "roadmap",
        "install_cmd",
        "installed_version_cmd",
        "version_file",
        "deployed_at_cmd",
    ]


def test_tilde_expansion(tmp_path):
    """A `~` in repo expands to the user's home directory."""
    reg = _write(tmp_path / "registry.yaml",
                 "projects:\n  - name: p\n    repo: ~/dev/thing\n")

    p = load_registry(str(reg))[0]

    assert p.repo == _home_abs("~/dev/thing")
    assert p.repo.startswith("/")  # absolute after expansion
    assert "~" not in p.repo


def test_load_absent_file_returns_empty(tmp_path):
    """A missing registry file is an empty registry, not an error."""
    assert load_registry(str(tmp_path / "nope.yaml")) == []


def test_round_trip_save_load(tmp_path):
    """save_registry then load_registry preserves all fields."""
    reg = tmp_path / "registry.yaml"
    projects = [
        Project(
            name="hscc",
            repo="/Users/desac/dev/hscc",
            board="hscc",
            topic=140,
            verify="cd /Users/desac/dev/hscc && ./scripts/run_tests.sh",
            roadmap="docs/ROADMAP.md",
        ),
        Project(name="bare", repo="/Users/desac/dev/bare"),
    ]

    save_registry(projects, str(reg))
    loaded = load_registry(str(reg))

    assert len(loaded) == 2
    assert loaded[0] == projects[0]
    assert loaded[1] == projects[1]
    assert loaded[1].board is None  # None optional field round-trips as None


def test_deploy_fields_round_trip(tmp_path):
    """The deployment-declaring fields survive save/load.

    ``installed_version_cmd``, ``version_file`` and ``deployed_at_cmd`` are
    ordinary optional fields: set them, save, load, and they come back intact.
    None stays None.
    """
    reg = tmp_path / "registry.yaml"
    projects = [
        Project(
            name="svc",
            repo="/Users/desac/dev/svc",
            installed_version_cmd="cat /opt/svc/version",
            version_file="VERSION",
            deployed_at_cmd="stat -c %Y /opt/svc/binary",
        ),
        Project(name="plain", repo="/Users/desac/dev/plain"),
    ]

    save_registry(projects, str(reg))
    loaded = load_registry(str(reg))

    assert loaded[0].installed_version_cmd == "cat /opt/svc/version"
    assert loaded[0].version_file == "VERSION"
    assert loaded[0].deployed_at_cmd == "stat -c %Y /opt/svc/binary"
    # absent -> None, not an error, not dropped
    assert loaded[1].installed_version_cmd is None
    assert loaded[1].version_file is None
    assert loaded[1].deployed_at_cmd is None


def test_unknown_project_name_raises_clear_error(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: hscc\n    repo: /x\n")

    with pytest.raises(ProjectNotFoundError) as exc:
        get_project("ghost", str(reg))
    assert "ghost" in str(exc.value)


def test_add_project_then_get(tmp_path):
    reg = tmp_path / "registry.yaml"

    add_project(
        "hscc",
        "~/dev/hscc",
        board="hscc",
        topic=140,
        path=str(reg),
    )

    got = get_project("hscc", str(reg))
    assert got.name == "hscc"
    assert got.repo == _home_abs("~/dev/hscc")
    assert got.board == "hscc"
    assert got.topic == 140
    assert got.verify is None


def test_add_project_without_repo_raises(tmp_path):
    with pytest.raises(MissingRepoError):
        add_project("x", "", path=str(tmp_path / "r.yaml"))


def test_add_duplicate_raises(tmp_path):
    reg = tmp_path / "registry.yaml"
    add_project("hscc", "/tmp/hscc", path=str(reg))
    with pytest.raises(DuplicateProjectError):
        add_project("hscc", "/tmp/hscc", path=str(reg))


def test_remove_project(tmp_path):
    reg = tmp_path / "registry.yaml"
    add_project("hscc", "/tmp/hscc", path=str(reg))
    add_project("other", "/tmp/other", path=str(reg))

    removed = remove_project("hscc", str(reg))
    assert removed.name == "hscc"

    remaining = load_registry(str(reg))
    assert [p.name for p in remaining] == ["other"]


def test_remove_missing_project_raises(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects: []\n")
    with pytest.raises(ProjectNotFoundError):
        remove_project("ghost", str(reg))


def test_row_without_repo_is_not_silently_dropped(tmp_path):
    """A malformed row (no repo) must raise, never silently vanish."""
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n  - name: good\n    repo: /tmp/good\n  - name: bad\n",
    )

    with pytest.raises(MissingRepoError):
        load_registry(str(reg))


def test_shipped_example_file_loads_with_all_projects():
    """docs/registry.example.yaml stays valid and non-empty (seed contract)."""
    example = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs",
        "registry.example.yaml",
    )
    projects = load_registry(example)
    assert projects, "example registry must not be empty"
    names = {p.name for p in projects}
    # Neutral placeholder project names, not the operator's real ones
    # (the repo ships for public release).
    assert {"project-a", "project-b", "project-c", "project-d", "project-e", "project-f", "project-g"} <= names
    # project-a is the reference seed with both board and topic set
    seed = get_project("project-a", example)
    assert seed.board == "project-a" and seed.topic == 120
    # `~` expands to an absolute path in every seeded entry
    assert all(p.repo.startswith("/") for p in projects)


def test_bind_topic_sets_binding_and_persists(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: hscc\n    repo: ~/dev/hscc\n    topic: 140\n")
    updated = bind_topic("hscc", 99, str(reg))
    assert updated.topic == 99
    # persisted on disk, not just in memory
    assert get_project("hscc", str(reg)).topic == 99


def test_bind_topic_replaces_prior_binding(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: hscc\n    repo: ~/dev/hscc\n    topic: 140\n")
    bind_topic("hscc", 140, str(reg))
    bind_topic("hscc", 7777, str(reg))  # rebind overrides
    assert get_project("hscc", str(reg)).topic == 7777


def test_bind_missing_project_raises(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects: []\n")
    with pytest.raises(ProjectNotFoundError):
        bind_topic("ghost", 5, str(reg))


def test_unbind_topic_clears_binding(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: hscc\n    repo: ~/dev/hscc\n    topic: 140\n")
    updated = unbind_topic("hscc", str(reg))
    assert updated.topic is None
    assert get_project("hscc", str(reg)).topic is None


def test_unbind_missing_project_raises(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects: []\n")
    with pytest.raises(ProjectNotFoundError):
        unbind_topic("ghost", str(reg))


def test_topic_name_round_trips_through_save(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n  - name: hscc\n    repo: ~/dev/hscc\n    topic: 140\n"
        "    topic_name: HSCC cluster\n",
    )
    projects = load_registry(str(reg))
    assert projects[0].topic_name == "HSCC cluster"
    save_registry(projects, str(reg))
    reloaded = load_registry(str(reg))
    assert reloaded[0].topic_name == "HSCC cluster"


# --------------------------------------------------------------------------- #
# cwd -> project auto-detection (detect_project_from_cwd / resolve_project_arg)
# --------------------------------------------------------------------------- #


def _projs(*repos):
    """Build a list of Projects from (name, repo) tuples."""
    return [Project(name=n, repo=r) for n, r in repos]


def test_detect_deepest_match_wins(tmp_path):
    """cwd nested inside multiple registered repos -> the deepest repo wins."""
    flightdeck = str(tmp_path / "flightdeck")
    inner = str(tmp_path / "flightdeck" / ".worktrees" / "t_abc")
    projects = _projs(("flightdeck", flightdeck), ("hscc", str(tmp_path / "hscc")))

    assert detect_project_from_cwd(projects, cwd=inner).name == "flightdeck"


def test_detect_cwd_inside_repo_subdir(tmp_path):
    """cwd inside a repo (but not at its root) still detects that repo."""
    repo = str(tmp_path / "flightdeck")
    cwd = str(tmp_path / "flightdeck" / "flightdeck" / "core")
    projects = _projs(("flightdeck", repo))

    assert detect_project_from_cwd(projects, cwd=cwd).name == "flightdeck"


def test_detect_no_match_returns_none(tmp_path):
    """cwd outside any registered repo -> None (caller falls through)."""
    projects = _projs(("flightdeck", str(tmp_path / "flightdeck")))
    outside = str(tmp_path / "unrelated" / "deep" / "dir")

    assert detect_project_from_cwd(projects, cwd=outside) is None


def test_detect_empty_projects_returns_none(tmp_path):
    """No registered projects -> no detection, ever."""
    assert detect_project_from_cwd([], cwd=str(tmp_path)) is None


def test_resolve_explicit_arg_wins(tmp_path):
    """An explicit project arg always wins and never marks detected."""
    projects = _projs(("flightdeck", str(tmp_path / "flightdeck")))
    notes = []

    name, detected = resolve_project_arg(
        projects, "hscc",
        cwd=str(tmp_path / "flightdeck"),
        _print=notes.append,
    )

    assert name == "hscc"
    assert detected is False
    assert notes == []  # no detection note for an explicit arg


def test_resolve_detects_from_cwd_and_prints_note(tmp_path):
    """Omitted arg + cwd inside a repo -> detected, with a visible note."""
    projects = _projs(("flightdeck", str(tmp_path / "flightdeck")))
    notes = []

    name, detected = resolve_project_arg(
        projects, None,
        cwd=str(tmp_path / "flightdeck" / "core"),
        _print=notes.append,
    )

    assert name == "flightdeck"
    assert detected is True
    assert notes == [f"using project 'flightdeck' (detected from cwd)"]


def test_resolve_no_match_falls_through(tmp_path):
    """Omitted arg + cwd outside any repo -> None, no note, not detected."""
    projects = _projs(("flightdeck", str(tmp_path / "flightdeck")))
    notes = []

    name, detected = resolve_project_arg(
        projects, None,
        cwd=str(tmp_path / "outside"),
        _print=notes.append,
    )

    assert name is None
    assert detected is False
    assert notes == []


def test_project_for_cwd_matches_repo_and_nonmatch(tmp_path):
    """project_for_cwd: cwd inside a repo resolves it; outside matches None.

    Acceptance anchor for the card: ``project_for_cwd(<repo>)`` returns the
    project whose repo is that path, while a cwd matching no registry repo
    returns ``None`` (never a guess).
    """
    projects = _projs(("flightdeck", str(tmp_path / "flightdeck")))

    assert project_for_cwd(str(tmp_path / "flightdeck"), projects=projects).name == "flightdeck"
    assert project_for_cwd(str(tmp_path / "none"), projects=projects) is None


# --------------------------------------------------------------------------- #
# depends_on -- parse / validate / dependents_for / dependent_notice
# --------------------------------------------------------------------------- #


def test_depends_on_absent_defaults_to_empty_list(tmp_path):
    reg = _write(tmp_path / "registry.yaml", "projects:\n  - name: x\n    repo: /tmp/x\n")
    projects = load_registry(str(reg))
    assert projects[0].depends_on == []


def test_depends_on_valid_list_passes_through_trimmed(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n"
        "  - name: bc\n    repo: /tmp/bc\n"
        "  - name: app\n    repo: /tmp/app\n"
        "    depends_on: [' bc ']\n",
    )
    projects = load_registry(str(reg))
    app = next(p for p in projects if p.name == "app")
    assert app.depends_on == ["bc"]


def test_depends_on_non_list_raises(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n  - name: x\n    repo: /tmp/x\n    depends_on: bc\n",
    )
    with pytest.raises(RegistryError):
        load_registry(str(reg))


def test_depends_on_non_string_entry_raises(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n  - name: x\n    repo: /tmp/x\n    depends_on: [1]\n",
    )
    with pytest.raises(RegistryError):
        load_registry(str(reg))


def test_depends_on_empty_string_entry_raises(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n  - name: x\n    repo: /tmp/x\n    depends_on: ['']\n",
    )
    with pytest.raises(RegistryError):
        load_registry(str(reg))


def test_depends_on_unresolvable_reference_raises_naming_it(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n  - name: app\n    repo: /tmp/app\n    depends_on: [bc]\n",
    )
    with pytest.raises(RegistryError) as exc:
        load_registry(str(reg))
    assert "app" in str(exc.value)
    assert "bc" in str(exc.value)


def test_depends_on_resolvable_reference_loads_cleanly(tmp_path):
    reg = _write(
        tmp_path / "registry.yaml",
        "projects:\n"
        "  - name: bc\n    repo: /tmp/bc\n"
        "  - name: app\n    repo: /tmp/app\n    depends_on: [bc]\n",
    )
    projects = load_registry(str(reg))
    app = next(p for p in projects if p.name == "app")
    assert app.depends_on == ["bc"]


def test_add_project_with_valid_dependency_succeeds(tmp_path):
    reg = tmp_path / "registry.yaml"
    add_project("bc", "/tmp/bc", path=str(reg))
    add_project("app", "/tmp/app", depends_on=["bc"], path=str(reg))

    got = get_project("app", str(reg))
    assert got.depends_on == ["bc"]


def test_add_project_with_unresolvable_dependency_raises_and_does_not_write(tmp_path):
    reg = tmp_path / "registry.yaml"
    add_project("app", "/tmp/app", path=str(reg))
    before = reg.read_text(encoding="utf-8")

    with pytest.raises(RegistryError):
        add_project("other", "/tmp/other", depends_on=["nonexistent"], path=str(reg))

    assert reg.read_text(encoding="utf-8") == before


def test_dependents_for_multiple_sorted_and_deduped(tmp_path):
    projects = _projs(("bc", "/tmp/bc"), ("app", "/tmp/app"), ("driver", "/tmp/driver"))
    projects[1].depends_on = ["bc"]
    projects[2].depends_on = ["bc"]

    assert dependents_for("bc", projects) == ["app", "driver"]


def test_dependents_for_none_returns_empty_list(tmp_path):
    projects = _projs(("bc", "/tmp/bc"), ("app", "/tmp/app"))
    assert dependents_for("bc", projects) == []


def test_dependents_for_unknown_project_returns_empty_list(tmp_path):
    projects = _projs(("bc", "/tmp/bc"))
    assert dependents_for("nonexistent", projects) == []


def test_dependent_notice_singular_exact_string(tmp_path):
    projects = _projs(("bc", "/tmp/bc"), ("app", "/tmp/app"))
    projects[1].depends_on = ["bc"]

    assert dependent_notice("bc", projects) == (
        "1 dependent project(s): app — consider verifying they still work"
    )


def test_dependent_notice_plural_exact_string(tmp_path):
    projects = _projs(("bc", "/tmp/bc"), ("app", "/tmp/app"), ("driver", "/tmp/driver"))
    projects[1].depends_on = ["bc"]
    projects[2].depends_on = ["bc"]

    assert dependent_notice("bc", projects) == (
        "2 dependent project(s): app, driver — consider verifying they still work"
    )


def test_dependent_notice_none_when_no_dependents(tmp_path):
    projects = _projs(("bc", "/tmp/bc"), ("app", "/tmp/app"))
    assert dependent_notice("bc", projects) is None

