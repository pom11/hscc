"""The "Your pad (orchpad)" section in orchestrator SOULs (card t_2d87d94d).

The pad is an orchestrator surface. The main orchestrator and every project
orchestrator get the section exactly once; workers, reviewers and the retired
legacy role do not. Everything here is pure text: no profile tree, no home.
"""

import generator
import main_orch

HEADING = "## Your pad (orchpad)"
PROMOTE = "orchpad_promote"


def _soul(name, identity="You orchestrate.\n"):
    spec = {"name": name, "identity": identity, "preload_skills": []}
    return generator.compose_soul(spec, base_identity="BASE")


def test_the_main_orchestrator_gets_the_pad_section_once():
    soul = _soul("general")
    assert soul.count(HEADING) == 1
    assert PROMOTE in soul
    assert "start of every session" in soul
    assert "Your pad is for your own follow-ups, ideas, observations" in soul


def test_a_project_orchestrator_gets_the_pad_section_once():
    soul = _soul("demo-orch")
    assert soul.count(HEADING) == 1
    assert PROMOTE in soul


def test_a_custom_main_orchestrator_name_gets_the_pad_section(tmp_path):
    main_orch.store_name(
        "ops", profiles_dir=str(tmp_path / "profiles"),
        registry_path=str(tmp_path / "missing-registry.yaml"))
    soul = _soul("ops")
    assert soul.count(HEADING) == 1


def test_workers_and_the_retired_role_do_not_get_the_pad():
    assert HEADING not in _soul("coder", identity="You build.\n")
    assert HEADING not in _soul(main_orch.LEGACY_ORCHESTRATOR_ROLE)


def test_the_pad_text_is_one_shared_block():
    assert generator._MAIN_OPS.count(generator._PAD_SECTION) == 1
    assert generator._PROJECT_ORCH_OPS.count(generator._PAD_SECTION) == 1
    assert generator._PAD_SECTION.count(HEADING) == 1
