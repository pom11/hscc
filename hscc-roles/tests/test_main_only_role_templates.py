"""Main-only cluster rule in the non-main SOUL templates (t_c3a29c37).

The 2026-11-23 audit (``docs/audits/2026-11-23-main-only-surface.md``, operator
rulings in ``~/fleet-check/verdict-main-only.txt``) made cluster control a
main-orchestrator-only surface. This file pins the ROLE-TEXT half of that rule:

* a worker or project-orchestrator section says exactly one thing about cluster
  actions — they go through the main orchestrator (board card or orch message);
* it never names a cluster tool or toolset as callable by that profile;
* the main section keeps stating the exclusivity out loud;
* reintroducing a cluster-tool claim into either non-main block reddens the
  SAME predicate (the mutation probe is a committed test, not a one-off run).

Everything is a pure text/read assertion: no profile tree, no fleet, no home.
"""
import os
import re

import generator
import orchestrators

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The operator's required wording (R4). It says who may act, not what any
#: config contains — the enforcement cards own the config half.
RULE = "cluster actions go through the main orchestrator (board card or orch message)"

#: Names that are cluster control, not model-call plumbing: the two toolsets,
#: the sparkrun passthrough, and every tool ``hscc-cluster`` registers (read
#: from its source, not re-typed here).
CLUSTER_TOOLSETS = ("hscc-cluster", "sparkrun")
CLUSTER_PASSTHROUGH = ("sparkrun_exec",)

_TOOL_LIST_RE = re.compile(r"^(_\w*TOOLS) = \[(.*?)^\]", re.M | re.S)
_TOOL_ENTRY_RE = re.compile(r'^\s*\("(\w+)",', re.M)


def _registered_cluster_tools():
    src = open(os.path.join(REPO, "hscc-cluster", "__init__.py"),
               encoding="utf-8").read()
    found = set()
    for _name, body in _TOOL_LIST_RE.findall(src):
        found.update(_TOOL_ENTRY_RE.findall(body))
    assert len(found) >= 28, (
        f"the registry read produced {len(found)} tools; the parser no longer "
        "matches the registry shape, so this pin is silently vacuous")
    return found


#: Tools registered by ``hscc-cluster`` that are coordination, not cluster
#: control: a project orchestrator may call ``orch_message`` (the tool itself
#: refuses workers, and rule 11 is pinned to the ``*-orch`` block). The main-only
#: rule is about the fleet surface, not about which agent may nudge a peer.
COORDINATION_TOOLS = {"orch_message"}


def _cluster_capability_mentions(text):
    """Every cluster control surface *text* names as present.

    Word-boundary matching, so a tool named without backticks is caught too.
    Deliberately not a capability-aware sentence parser: naming the surface at
    all in a non-main block is the defect this card removes. ``orch_message``
    is excluded — it is the coordination channel, not cluster control.
    """
    names = (set(CLUSTER_TOOLSETS) | set(CLUSTER_PASSTHROUGH)
             | _registered_cluster_tools()) - COORDINATION_TOOLS
    return sorted(n for n in names
                  if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", text))


def _worker_block():
    return generator._WORKER_OPS.format(name="coder")


def _project_orch_block():
    return generator._PROJECT_ORCH_OPS.format(name="demo-orch")


def test_worker_section_carries_the_main_only_rule():
    block = _worker_block()
    assert RULE in block
    assert RULE in generator.CLUSTER_ACTIONS_RULE
    # The model-call path is not cluster control and must not be swept away
    # with it: the worker ops still describe the worktree/task lifecycle.
    assert "your own git worktree" in block


def test_project_orch_section_carries_the_main_only_rule():
    block = _project_orch_block()
    assert RULE in block
    assert "you do NOT hold authority over the physical cluster" in block
    # The retired cluster-wide role must not be named as the authority.
    assert "cluster-wide `orchestrator` role" not in block


def test_worker_section_names_no_cluster_tool_as_callable():
    assert _cluster_capability_mentions(_worker_block()) == []


def test_project_orch_section_names_no_cluster_tool_as_callable():
    assert _cluster_capability_mentions(_project_orch_block()) == []


def test_orch_message_rule_stays_out_of_the_worker_block():
    """Rule 11 is a project-orch rule; the tool already refuses workers."""
    assert "orch_message" not in _worker_block()
    assert "`orch_message`" in _project_orch_block()


def test_the_main_section_still_states_the_exclusivity():
    section = orchestrators.main_cluster_control_block()
    assert "is the ONLY profile on this fleet with cluster control" in section
    assert "a worker profile and a project orchestrator profile have none of it" \
        in section
    assert "they ask — they do not act" in section


def test_the_mutation_probe_reddens_the_non_main_predicate():
    """A reintroduced cluster-tool claim must fail the same predicate."""
    for block, name in ((_worker_block(), "coder"),
                        (_project_orch_block(), "demo-orch")):
        assert _cluster_capability_mentions(block) == []
        gutted = block + (
            "\nUse `cluster_status` and `provision_model`, and the "
            "`hscc-cluster` toolset, to change the fleet yourself.\n")
        assert gutted != block
        mentions = _cluster_capability_mentions(gutted)
        assert "cluster_status" in mentions
        assert "provision_model" in mentions
        assert "hscc-cluster" in mentions
        assert name in gutted


def test_the_non_main_blocks_never_spell_the_main_name():
    """R2/D-A: the rule is keyed on the resolver, never a literal profile name."""
    for block in (_worker_block(), _project_orch_block()):
        assert "general" not in block.lower()
