"""Main orchestrator: one stored name, one resolver (card t_c6eccd49).

The design (docs/design/2026-11-19-main-orchestrator-design.md) names ONE main
orchestrator at bootstrap, stores it once under ``~/.hscc``, and routes every
component through ``main_orch``. These tests pin the resolver fallback, the
stored name's precedence, the validation rules from the interview, and a
grep-guard that the old hardcoded profile/board literals cannot come back
outside this resolver.
"""
import json
import re
from pathlib import Path

import pytest
import yaml

import generator
import main_orch
import orchestrators

REPO = Path(__file__).resolve().parent.parent.parent
CODE_DIRS = ("hscc-roles", "hscc-bootstrap", "hscc-api", "hscc-commands",
             "hscc_daemon", "hscc-cluster", "hscc-project", "hscc-cli",
             "scripts")
# The resolver itself is the ONE place the legacy names may appear; docs and
# tests are allowed to explain the migration.
RESOLVER_FILES = {"hscc-roles/main_orch.py", "hscc-roles/orchestrators.py"}


@pytest.fixture
def hscc_dir(tmp_path, monkeypatch):
    root = tmp_path / "hscc"
    root.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(root))
    return root


@pytest.fixture
def registry_file(tmp_path):
    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump({"projects": [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"}]}),
        encoding="utf-8")
    return str(p)


# ── resolver fallback + stored name ─────────────────────────────────────────

def test_falls_back_to_general_when_nothing_is_stored(hscc_dir):
    assert main_orch.stored_name() is None
    assert main_orch.main_name() == "general"
    assert main_orch.main_profile() == "general"
    assert main_orch.main_board() == "general"


def test_stored_name_wins_for_name_profile_and_board(hscc_dir):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    assert main_orch.main_name() == "ops"
    assert main_orch.main_profile() == "ops"
    assert main_orch.main_board() == "ops"
    doc = json.loads((hscc_dir / "main-orchestrator.json").read_text())
    assert doc == {"name": "ops"}


def test_store_refuses_to_change_a_stored_name(hscc_dir):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    assert main_orch.store_name("ops", registry_path="/nonexistent.yaml") == "ops"
    with pytest.raises(main_orch.MainOrchestratorError, match="already stored"):
        main_orch.store_name("other", registry_path="/nonexistent.yaml")
    assert main_orch.main_name() == "ops"


def test_resolver_default_uses_the_stored_main_identity(hscc_dir):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    got = orchestrators.resolve_orchestrator(None, path="/nonexistent.yaml")
    assert got == {"project": "ops", "profile": "ops", "session": "ops",
                   "board": "ops", "repo": None}
    assert orchestrators.resolve_orchestrator("general",
                                              path="/nonexistent.yaml") == got
    assert orchestrators.resolve_orchestrator("ops",
                                              path="/nonexistent.yaml") == got


def test_resolver_keeps_the_explicit_legacy_catch_all_working(hscc_dir):
    got = orchestrators.resolve_orchestrator("general-orch",
                                             path="/nonexistent.yaml")
    assert got == {"project": "general", "profile": "general-orch",
                   "session": "general", "board": "default", "repo": None}


# ── validation (the interview contract) ─────────────────────────────────────

@pytest.mark.parametrize("name", ["", "General", "gen eral", "-general",
                                  "general-", "gen--eral", "a_b", "general!"])
def test_validation_rejects_non_slugs(name, tmp_path):
    with pytest.raises(main_orch.MainOrchestratorError):
        main_orch.validate_name(name, profiles_dir=str(tmp_path / "profiles"),
                                registry_path="/nonexistent.yaml")


def test_validation_rejects_role_profile_names(tmp_path):
    with pytest.raises(main_orch.MainOrchestratorError, match="role profile"):
        main_orch.validate_name("orchestrator",
                                profiles_dir=str(tmp_path / "profiles"),
                                registry_path="/nonexistent.yaml")
    with pytest.raises(main_orch.MainOrchestratorError, match="role profile"):
        main_orch.validate_name("coder", profiles_dir=str(tmp_path / "profiles"),
                                registry_path="/nonexistent.yaml")


def test_validation_rejects_a_name_in_the_project_orch_namespace(tmp_path):
    profiles = tmp_path / "profiles"
    (profiles / "hscc-orch").mkdir(parents=True)
    with pytest.raises(main_orch.MainOrchestratorError, match="-orch"):
        main_orch.validate_name("hscc-orch", profiles_dir=str(profiles),
                                registry_path="/nonexistent.yaml")


def test_validation_rejects_a_custom_name_that_lands_on_a_project_orch(
        tmp_path):
    profiles = tmp_path / "profiles"
    (profiles / "ops-orch").mkdir(parents=True)
    with pytest.raises(main_orch.MainOrchestratorError, match="ops-orch"):
        main_orch.validate_name("ops", profiles_dir=str(profiles),
                                registry_path="/nonexistent.yaml")


def test_default_general_is_valid_even_when_general_orch_exists(tmp_path):
    """The existing install still has general-orch until the migration card;
    that must never make the default name un-storeable."""
    profiles = tmp_path / "profiles"
    (profiles / "general-orch").mkdir(parents=True)
    assert main_orch.validate_name(
        "general", profiles_dir=str(profiles),
        registry_path="/nonexistent.yaml") == "general"


def test_validation_rejects_a_registry_project(tmp_path, registry_file):
    with pytest.raises(main_orch.MainOrchestratorError, match="-orch"):
        main_orch.validate_name("hscc", profiles_dir=str(tmp_path / "profiles"),
                                registry_path=registry_file)


def test_validation_rejects_the_legacy_orch_suffix(tmp_path):
    with pytest.raises(main_orch.MainOrchestratorError, match="-orch"):
        main_orch.validate_name("nothing-orch",
                                profiles_dir=str(tmp_path / "profiles"),
                                registry_path="/nonexistent.yaml")


def test_validation_accepts_a_fresh_slug(tmp_path):
    assert main_orch.validate_name(
        "ops-main", profiles_dir=str(tmp_path / "profiles"),
        registry_path="/nonexistent.yaml") == "ops-main"


# ── generator recognises the main profile as cluster authority ──────────────

def test_generator_treats_the_stored_main_profile_as_orchestrator(hscc_dir):
    main_orch.store_name("ops", registry_path="/nonexistent.yaml")
    assert generator._is_orchestrator("ops")
    assert not generator._is_project_orchestrator("ops")
    soul = generator.compose_soul(
        {"name": "ops", "identity": "You orchestrate.\n", "preload_skills": []},
        base_identity="BASE")
    assert "physical cluster" in soul.lower()
    assert "gateway node" in soul.lower()


def test_legacy_orchestrator_role_still_recognised(hscc_dir):
    assert generator._is_orchestrator("orchestrator")
    assert not generator._is_project_orchestrator("orchestrator")
    assert generator._is_orchestrator("ecofire-app-orch")


# ── grep guard: no hardcoded old profile/board literals come back ───────────

def _code_files():
    for directory in CODE_DIRS:
        root = REPO / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def test_no_hardcoded_old_profile_or_board_literals_outside_resolver():
    offenders = []
    for path in _code_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in RESOLVER_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for literal in ("GENERAL_PROFILE", "GENERAL_BOARD", "general-orch"):
            if literal in text:
                offenders.append(f"{rel}: {literal}")
        # Profile-name comparisons against the legacy cluster role must go
        # through the resolver; a serving-unit role string is a different
        # concept and does not use this ``name ==`` shape.
        if re.search(r"\bname\s*[!=]=\s*[\"']orchestrator[\"']", text):
            offenders.append(f'{rel}: name == "orchestrator"')
    assert not offenders, (
        "hardcoded main-orchestrator identity outside the resolver:\n  "
        + "\n  ".join(offenders)
        + "\nFix by routing the read through hscc-roles/main_orch.py "
        "(main_profile() / main_board() / legacy_general_profile(), whose "
        "docstring gives the reason) and by naming the legacy identity "
        "indirectly in prose. Do NOT add the offending file to RESOLVER_FILES: "
        "that set is the resolver's own two files, and a consumer listed there "
        "could hardcode the very identity it is supposed to resolve (ruled in "
        "t_d1afc6e6, NOTES-t_d1afc6e6.md).")
