"""The anti-churn rules in every generated orchestrator SOUL (t_8e22a6c6).

Operator 2026-11-19: both project orchestrators stalled or looped overnight.
The rules live ONCE in ``generator._ANTI_CHURN_RULES`` and are appended to the
main orchestrator's and every per-project orchestrator's operational block.
These tests pin the block's presence exactly once, its six rules, that
regeneration is idempotent and preserves operator-appended sections, and a
grep guard that no second copy of the rules text exists in the code.
"""
from pathlib import Path

import pytest

import generator
import main_orch
import orch_sync
import rolelib

REPO = Path(__file__).resolve().parent.parent.parent
HEADING = "## Anti-churn rules (operator 2026-11-19)"
RULE_MARKERS = (
    "1. **Done means merged.**",
    "2. **The merge queue lands green PRs.**",
    "3. **At most 2 cards in flight on the same area**",
    "4. **Small findings stay in the PR.**",
    "5. **Don't wait on the operator**",
    "6. **Always pass the board explicitly**",
)
OPERATOR_SECTION = (
    "## Operator notes (keep me)\n\n"
    "This section belongs to the operator and must survive regeneration.\n"
)


def _soul(name, identity="You orchestrate.\n"):
    spec = {"name": name, "identity": identity, "preload_skills": []}
    return generator.compose_soul(spec, base_identity="BASE")


@pytest.mark.parametrize("name", ["general", "demo-orch", "ecofire-flows-orch"])
def test_every_generated_orchestrator_soul_has_the_block_exactly_once(name):
    soul = _soul(name)
    assert soul.count(HEADING) == 1
    assert soul.count(generator._ANTI_CHURN_RULES) == 1
    for marker in RULE_MARKERS:
        assert marker in soul


def test_a_custom_main_orchestrator_name_also_gets_the_block_once(tmp_path):
    main_orch.store_name(
        "ops", profiles_dir=str(tmp_path / "profiles"),
        registry_path=str(tmp_path / "missing-registry.yaml"))
    soul = _soul("ops")
    assert soul.count(generator._ANTI_CHURN_RULES) == 1
    assert "You are the ONE main orchestrator" in soul


def test_workers_and_the_retired_legacy_role_do_not_get_the_block():
    worker = _soul("coder", identity="You build.\n")
    assert HEADING not in worker
    retired = _soul(main_orch.LEGACY_ORCHESTRATOR_ROLE)
    assert HEADING not in retired


def test_rule_2_enqueues_in_merge_queue_mode_and_keeps_the_manual_rule():
    """The queue owns landing when enabled; the old rule survives as the else."""
    rule = generator._ANTI_CHURN_RULES
    assert "`hscc merge-queue add <project> <pr>`" in rule
    assert "never rebase or merge by hand" in rule
    # The pre-queue behaviour stays the fallback for projects without the queue.
    assert "**you land the work**" in rule
    assert "review and merge it yourself in the same turn" in rule
    # Single source: the rule text is emitted once, in the shared block.
    assert rule.count("The merge queue lands green PRs.") == 1
    assert rule.count("you land the work") == 1


def test_anti_churn_block_has_no_format_placeholders():
    assert "{" not in generator._ANTI_CHURN_RULES
    assert "}" not in generator._ANTI_CHURN_RULES
    for block in (generator._MAIN_OPS, generator._PROJECT_ORCH_OPS):
        rendered = block.format(name="x-orch")
        assert "{" not in rendered and "}" not in rendered


def test_anti_churn_block_is_shared_by_both_orchestrator_ops_blocks():
    assert generator._MAIN_OPS.count(generator._ANTI_CHURN_RULES) == 1
    assert generator._PROJECT_ORCH_OPS.count(generator._ANTI_CHURN_RULES) == 1


@pytest.mark.parametrize("name", ["general", "demo-orch"])
def test_regeneration_is_idempotent_and_keeps_operator_sections(
        name, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": name, "identity": "You orchestrate.\n",
            "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    soul_path = tmp_path / "profiles" / name / "SOUL.md"
    soul_path.write_text(soul_path.read_text() + "\n\n" + OPERATOR_SECTION)

    assert generator.generate_profile(spec, base_identity="BASE") is True
    first = soul_path.read_text()
    assert first.count(HEADING) == 1
    assert first.count(generator._ANTI_CHURN_RULES) == 1
    assert first.count("## Operator notes (keep me)") == 1
    assert OPERATOR_SECTION.strip() in first

    assert generator.generate_profile(spec, base_identity="BASE") is False
    assert soul_path.read_text() == first


def test_orch_sync_rerender_also_carries_the_block_once():
    old = ("OLD BASE\n\n## Role: demo-orch\n\nYou orchestrate demo.\n\n"
           "## Operational\n\nstale\n")
    synced = orch_sync.compose(old, "demo-orch", "BASE")
    assert synced is not None
    assert synced.count(HEADING) == 1
    assert synced.count(generator._ANTI_CHURN_RULES) == 1
    assert synced.endswith(generator._PROJECT_ORCH_OPS.format(name="demo-orch"))


def _non_test_python_files():
    for path in REPO.rglob("*.py"):
        parts = set(path.relative_to(REPO).parts)
        if parts & {"tests", "_archive", "__pycache__", ".git"}:
            continue
        yield path.relative_to(REPO).as_posix(), path


def test_rules_text_lives_only_in_the_generator_block():
    """The rules must not be retyped into another module or role spec."""
    offenders = []
    for rel, path in _non_test_python_files():
        if rel == "hscc-roles/generator.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in RULE_MARKERS:
            if marker in text:
                offenders.append(f"{rel}: {marker}")
    assert not offenders, (
        "anti-churn rules text has a second copy outside "
        "hscc-roles/generator.py:\n  " + "\n  ".join(offenders))

    source = (REPO / "hscc-roles/generator.py").read_text(encoding="utf-8")
    for marker in RULE_MARKERS:
        assert source.count(marker) == 1
