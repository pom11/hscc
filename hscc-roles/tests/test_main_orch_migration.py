"""M2: the merged main-orchestrator SOUL and the data-preserving migration.

The design (docs/design/2026-11-19-main-orchestrator-design.md) replaces the
legacy catch-all and the legacy cluster-wide role with ONE main orchestrator
(default ``general``). These tests pin:

  * the merged SOUL contract (roster knowledge, live-state answers, standing
    watch cards, inject-only orchestration, full authority except LIVE publish,
    the standing rules, `hscc notify`);
  * a fresh install (main profile created, no legacy profile invented);
  * an upgrade (memories/sessions copied — never moved, never overwritten —
    and both old profiles retired in their SOUL);
  * idempotence (a second run is a byte-identical no-op);
  * operator config keys surviving the generation/migration.

Everything runs against a throwaway HERMES_HOME/HSCC_DIR; the real
``~/.hermes`` and ``~/.hscc`` are never read or written.
"""

import io
import contextlib
import json
import os

import pytest
import yaml

import generator
import hscc
import main_orch
import migrate_main
import rolelib

MAIN = "general"
LEGACY = "general-orch"

CONTRACT_PHRASES = (
    "main orchestrator",
    "sole authority over the physical cluster",
    "never answer from memory alone",
    "hscc orch inject",
    # t_1992ab13: the policed nudge TOOL, not just the CLI verb. t_9e3d868f
    # made orch_message accept the configured main orchestrator at both ends,
    # so a SOUL that names only `hscc orch inject` leaves the main shelling
    # out to the CLI and losing the rate limit, the loop guard and the
    # signature. Pinned here end-to-end (real SOUL.md on disk) and
    # section-scoped in test_generator.py.
    "orch_message",
    "hscc notify",
    "LIVE publish",
    "autodown",
    "never install built apps on the gateway host",
    "no attribution lines",
    "destructive git",
    "sparkrun/hscc",
    "standing watch cards",
    "`blocked` with a `watch` reason",
    "stuck or failing cards",
    "wedged panes",
    "duplicate cards",
    "PR is not on main",
    "steer when the orchestrator is busy; plain when it is idle",
    "profiles YAML-compare, kanban caps, autodown status",
)

REGISTRY = {
    "projects": [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
        {"name": "ecofire-app", "repo": "/tmp/ecofire", "board": "ecofire-app"},
    ]
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    profiles = home / "profiles"
    profiles.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(tmp_path / "hscc"))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(profiles))
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump(REGISTRY), encoding="utf-8")
    return {"tmp": tmp_path, "profiles": profiles,
            "registry": str(registry)}


def _orch_all(world):
    """Run the real `orch-all` command against the fixture home."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = hscc.cmd_orch_all(["--registry", world["registry"]])
    return code, json.loads(out.getvalue())


def _ensure_main(world):
    """Ensure just the main profile (no project fleet) and return its rc."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = hscc.cmd_orch([MAIN, "--registry", world["registry"]])
    return code


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ── fresh install: one merged SOUL ─────────────────────────────────────────

def test_fresh_install_creates_the_merged_main_soul(world):
    code, report = _orch_all(world)
    assert code == 0, report
    assert all(e["changed"] for e in report["ensured"])
    assert "migration" not in report          # nothing legacy to migrate

    pdir = world["profiles"] / MAIN
    soul = _read(pdir / "SOUL.md")
    for phrase in CONTRACT_PHRASES:
        assert phrase.lower() in soul.lower(), f"missing contract phrase: {phrase!r}"
    # It knows the roster: every project, its orchestrator and its board.
    for name in ("hscc", "ecofire-app"):
        assert f"**{name}**" in soul
        assert f"`{name}-orch`" in soul
        assert f"board `{name}`" in soul
    # The legacy identities are not invented on a fresh install.
    assert not (world["profiles"] / LEGACY).exists()


def test_main_soul_is_not_a_worker_and_holds_cluster_authority(world):
    assert _ensure_main(world) == 0
    soul = _read(world["profiles"] / MAIN / "SOUL.md")
    assert "your own git worktree" not in soul.lower()
    assert "## Role: general" in soul
    assert "## How you manage work" not in soul


def test_custom_main_name_drives_profile_soul_and_retirement(world):
    main_orch.store_name("ops", registry_path=world["registry"])
    (world["profiles"] / LEGACY).mkdir()
    _write(world["profiles"] / LEGACY / "SOUL.md", "old catch-all\n")
    code, report = _orch_all(world)
    assert code == 0, report
    assert (world["profiles"] / "ops" / "SOUL.md").is_file()
    assert not (world["profiles"] / "general" / "SOUL.md").exists()
    retired = _read(world["profiles"] / LEGACY / "SOUL.md")
    assert "retired — use ops" in retired
    assert report["migration"]["retired"][LEGACY] == "retired"


# ── upgrade: copy, never move/overwrite; retire, never delete ──────────────

def test_upgrade_copies_missing_memory_and_sessions_only(world):
    legacy = world["profiles"] / LEGACY
    _write(legacy / "memories" / "MEMORY.md", "# legacy memory\n")
    _write(legacy / "memories" / "archive" / "old.md", "kept\n")
    _write(legacy / "sessions" / "s-legacy.json", '{"legacy": true}\n')
    main = world["profiles"] / MAIN
    _write(main / "memories" / "MEMORY.md", "# main memory wins\n")
    _write(main / "sessions" / "mine.json", '{"mine": true}\n')

    code, report = _orch_all(world)
    assert code == 0, report

    # Main's own files are untouched.
    assert _read(main / "memories" / "MEMORY.md") == "# main memory wins\n"
    assert _read(main / "sessions" / "mine.json") == '{"mine": true}\n'
    # Missing legacy files were copied over, including nested paths.
    assert _read(main / "memories" / "archive" / "old.md") == "kept\n"
    assert _read(main / "sessions" / "s-legacy.json") == '{"legacy": true}\n'
    # The source is never moved or deleted.
    assert _read(legacy / "memories" / "MEMORY.md") == "# legacy memory\n"
    assert _read(legacy / "sessions" / "s-legacy.json") == '{"legacy": true}\n'
    mig = report["migration"]
    assert f"{MAIN}/memories/archive/old.md" in mig["copied"]
    assert f"{MAIN}/memories/MEMORY.md" in mig["skipped"]


def test_copy_creates_missing_destination_dirs(world):
    legacy = world["profiles"] / LEGACY
    _write(legacy / "memories" / "MEMORY.md", "# legacy\n")
    (world["profiles"] / MAIN).mkdir()

    report = migrate_main.migrate(str(world["profiles"]), MAIN)
    assert report["changed"] is True
    assert _read(world["profiles"] / MAIN / "memories" / "MEMORY.md") == "# legacy\n"
    assert f"{MAIN}/memories" in report["created_dirs"]


def test_upgrade_retires_both_legacy_souls_and_keeps_their_content(world):
    legacy = world["profiles"] / LEGACY
    _write(legacy / "SOUL.md", "# old catch-all history\n\nkeep me\n")
    old_role = world["profiles"] / "orchestrator"
    _write(old_role / "SOUL.md", "# old cluster role\n\n## Role: orchestrator\n")
    _write(old_role / "config.yaml", yaml.safe_dump({"operator_key": 7}))

    code, report = _orch_all(world)
    assert code == 0, report
    catch_all = _read(legacy / "SOUL.md")
    role = _read(old_role / "SOUL.md")
    for soul in (catch_all, role):
        assert "retired — use general" in soul
    assert "# old catch-all history" in catch_all      # content preserved
    assert "# old cluster role" in role
    assert report["migration"]["retired"][LEGACY] == "retired"
    assert report["migration"]["retired"]["orchestrator"] == "retired"
    # Retirement never touches config.
    assert yaml.safe_load(_read(old_role / "config.yaml")) == {"operator_key": 7}


def test_second_run_is_a_byte_identical_noop(world):
    legacy = world["profiles"] / LEGACY
    _write(legacy / "memories" / "MEMORY.md", "# legacy\n")
    _write(legacy / "sessions" / "s.json", "{}\n")
    _write(legacy / "SOUL.md", "# legacy soul\n")

    code1, r1 = _orch_all(world)
    snap = {p: p.read_bytes() for p in world["profiles"].rglob("*")
            if p.is_file()}
    code2, r2 = _orch_all(world)

    assert code1 == code2 == 0
    assert "migration" in r1
    assert "migration" not in r2, "no-op re-run must not report migration churn"
    after = {p: p.read_bytes() for p in world["profiles"].rglob("*")
             if p.is_file()}
    assert after == snap, "second run rewrote profile files"
    assert _read(legacy / "SOUL.md").count("retired — use") == 1


def test_retire_is_idempotent_and_creates_a_soul_when_absent(world):
    (world["profiles"] / MAIN).mkdir()
    legacy = world["profiles"] / LEGACY
    legacy.mkdir()
    assert not (legacy / "SOUL.md").exists()
    first = migrate_main.migrate(str(world["profiles"]), MAIN)
    assert first["retired"][LEGACY] == "retired"
    soul = _read(legacy / "SOUL.md")
    assert "retired — use general" in soul
    second = migrate_main.migrate(str(world["profiles"]), MAIN)
    assert second["retired"][LEGACY] == "already-retired"
    assert second["changed"] is False
    assert _read(legacy / "SOUL.md") == soul


# ── config keys: YAML merge preserves operator values ─────────────────────

def test_operator_config_keys_survive_main_generation(world):
    main = world["profiles"] / MAIN
    original = {
        "__config_version": 9,
        "browser": {"driver": "operator-choice"},
        "memory": {"memory_char_limit": 9000, "provider": "my-provider",
                   "user_char_limit": 500},
        "compression": {"threshold_tokens": 120000, "operator_flag": True},
    }
    _write(main / "config.yaml", yaml.safe_dump(original))

    code, report = _orch_all(world)
    assert code == 0, report
    cfg = yaml.safe_load(_read(main / "config.yaml"))
    assert cfg["__config_version"] == 9
    assert cfg["browser"] == {"driver": "operator-choice"}
    assert cfg["memory"]["memory_char_limit"] == 9000       # never lowered
    assert cfg["memory"]["provider"] == "my-provider"       # never replaced
    assert cfg["memory"]["user_char_limit"] == 500
    assert cfg["compression"]["threshold_tokens"] == 120000  # never raised
    assert cfg["compression"]["operator_flag"] is True


def test_legacy_configs_are_not_rewritten_by_migration(world):
    legacy = world["profiles"] / LEGACY
    _write(legacy / "SOUL.md", "# legacy\n")
    _write(legacy / "config.yaml", yaml.safe_dump({"memory": {"x": 1}}))
    role = world["profiles"] / "orchestrator"
    _write(role / "config.yaml", yaml.safe_dump({"browser": {"y": 2}}))

    code, _report = _orch_all(world)
    assert code == 0
    assert yaml.safe_load(_read(legacy / "config.yaml")) == {"memory": {"x": 1}}
    assert yaml.safe_load(_read(role / "config.yaml")) == {"browser": {"y": 2}}


# ── generator: the legacy role spec is retired on every generate ──────────

def test_generate_marks_the_legacy_orchestrator_role_retired(world, monkeypatch):
    roles = world["tmp"] / "roles"
    roles.mkdir()
    (roles / "orchestrator.yaml").write_text(yaml.safe_dump({
        "name": "orchestrator",
        "identity": "You orchestrate.\n",
        "routing_description": "Legacy cluster orchestration.",
    }), encoding="utf-8")
    monkeypatch.setattr(rolelib, "ROLES_DIR", str(roles))
    monkeypatch.setattr(hscc, "_base_identity", lambda: "BASE\n")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert hscc.cmd_generate([]) == 0
    soul = _read(world["profiles"] / "orchestrator" / "SOUL.md")
    assert "retired — use general" in soul
    assert "no longer hold authority over the physical cluster" in soul
    # Re-running is byte-identical (the marker is not duplicated).
    before = soul
    with contextlib.redirect_stdout(io.StringIO()):
        assert hscc.cmd_generate([]) == 0
    assert _read(world["profiles"] / "orchestrator" / "SOUL.md") == before


def test_compose_soul_retirement_uses_the_stored_name(world):
    main_orch.store_name("ops", registry_path=world["registry"])
    soul = generator.compose_soul(
        {"name": "orchestrator", "identity": "You orchestrate.\n",
         "preload_skills": []},
        base_identity="BASE")
    assert "retired — use ops" in soul
    assert "`general`" not in soul.split("## Retired", 1)[1]
