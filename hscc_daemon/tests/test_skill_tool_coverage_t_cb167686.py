"""The bundled ``hscc-cluster`` skill must document the registry, not a stale copy of it.

Audit F1 (docs/audits/2026-11-23-agent-surface.md §7, §8.1) measured the bundled
``install/hscc-skills/hscc-cluster/SKILL.md`` at **zero** occurrences of the
registered agent tools — it described 6 standalone-plugin commands and none of the
then-26 tools — while being byte-identical to the copy bootstrap installs. That is
why the finding reads "the template is the ceiling": a perfect installer cannot ship
content that is not there, so a fresh host produces an agent that cannot know its own
toolset exists.

The card that refreshed the skill (t_cb167686) is a one-time fix; this file is the
gate that keeps it fixed. It bites on the two ways the audit says the drift happened:

* **a tool registered and never documented** — the registry grows (it went 23 → 26 →
  28 inside one workstream) and the skill keeps describing the old one;
* **a tool documented that does not exist** — the audit's own card body listed
  ``kanban_paused``, ``main_status`` and ``doctor`` as tools this wave "adds", and
  §10 decided all three against (D6, D9, D11). §12.2 (the operator verdict) then
  ruled that D11 was about the *route* and that the read-only ``doctor`` **tool**
  stands, so two of the three are still refused and ``doctor`` is now registered —
  which is exactly the kind of move this gate is built to notice rather than let
  drift: the count, the table and the decided-against list have to be edited in the
  same deliberate commit. A document that names a phantom tool is worse than one
  that omits a real one, because an agent wastes a turn calling it.

So the tool tables and the registry must be the SAME SET, in both directions — the
same both-directions rule ``test_plugin_manifest_drift.py`` applies to ``plugin.yaml``,
applied to the surface agents actually read.

Parses only: never imports the plugin (hyphenated dirs are not importable), never
touches ``~/.hscc``, the fleet or the network. The registered-name parser is imported
from ``hscc_daemon.verify`` itself — the same implementation the manifest drift test
and the shipped ``verify.check_plugins`` use, so this gate cannot pass on a private
parser while the real check diverges (the single-implementation rule the drift test's
docstring states).

Skipped, not silently vacuous, when the tree is not the repo layout.
"""

import re
from pathlib import Path

import pytest

from hscc_daemon.verify import _registered_tool_names

REPO = Path(__file__).resolve().parents[2]

if not (REPO / "scripts" / "run_tests.sh").is_file():
    pytest.skip("not the repo layout — the bundled skill is a repo promise",
                allow_module_level=True)

SKILL_DIR = REPO / "install" / "hscc-skills" / "hscc-cluster"
SKILL_MD = SKILL_DIR / "SKILL.md"
BUNDLED_LIST = REPO / "hscc-skills" / "hscc.py"

#: Everything above this heading is the tool tables. Everything below it is the
#: section that records which verbs were decided AGAINST a tool. The split is the
#: whole test: "documented as a tool" and "declared not to be a tool" are different
#: claims and the same name must not be making both.
TOOL_ABSENT_HEADING = "## Tools that deliberately do NOT exist"

#: A tool-table row: the tool name in backticks as the first cell.
TOOL_ROW = re.compile(r"^\| `([a-z][a-z0-9_]+)` \|", re.M)

#: (surface string as the skill must spell it, audit decision id). Each is a verb a
#: reasonable worker would expect a tool for — and each was decided against, in the
#: sign-off on the audit. If one of these ever gains a tool, the decision changed and
#: BOTH this list and the skill's "do not exist" table must be edited deliberately.
DECIDED_AGAINST = [
    ("hscc cluster monitor", "D2"),          # cluster_status + discovery_status cover a worker
    ("hscc cluster up", "D3"),               # fleet-wide power stays operator + confirm-gated route
    ("kanban_paused", "D6"),                 # a board brake an agent can pull is not a brake
    ("merge-queue add", "D8"),               # an agent-writable merge door is a self-merge path
    ("heal_status", "D4"),                   # read-only tool recommended, NOT yet registered
    ("template validate", "D5"),             # route + slash hint recommended, no tool
    ("hscc notify", "D7"),                   # an agent that can page the operator trains them to ignore pages
    ("hscc main status", "D9"),              # dashboards get a read ROUTE, not an agent tool
    # ("hscc doctor", "D11") was here. It came out deliberately on t_7f570861:
    # the operator verdict (§12.2) ruled that D11 refused the HTTP *route*
    # ("it reads this host's trees") and that "the read-only agent *tool* is a
    # different surface and stands". The tool is registered, so this list — which
    # means "no tool, and the skill says so" — no longer describes it; the skill
    # keeps a row recording the route refusal. The repair half (`--fix`) is still
    # refused, pinned in test_plugin_manifest_drift.py.
]


def _registered():
    return list(_registered_tool_names((REPO / "hscc-cluster" / "__init__.py").read_text(encoding="utf-8")))


def _sections():
    """(tool-table section, everything after the decided-against heading)."""
    text = SKILL_MD.read_text(encoding="utf-8")
    head, marker, tail = text.partition(TOOL_ABSENT_HEADING)
    assert marker, (
        f"{SKILL_MD.relative_to(REPO)} lost its {TOOL_ABSENT_HEADING!r} heading — "
        "without it there is nothing separating 'is a tool' from 'was decided "
        "against', and this gate cannot tell a stale copy from a rewritten one"
    )
    return head, tail


def test_skill_is_bundled_so_bootstrap_actually_ships_it():
    """A skill nobody installs cannot fix a stale installed one.

    Audit F12 is exactly this failure in mirror image (``hscc-lessons`` sits on disk
    but is absent from ``BUNDLED_SKILLS``, so no bootstrap ever copies it).
    """
    assert SKILL_MD.is_file(), f"missing {SKILL_MD}"
    block = re.search(r"BUNDLED_SKILLS\s*=\s*\[(.*?)\n\]",
                      BUNDLED_LIST.read_text(encoding="utf-8"), re.S | re.M)
    assert block is not None, (
        f"BUNDLED_SKILLS no longer parses out of {BUNDLED_LIST.relative_to(REPO)} — "
        "if the list changed shape, this gate must change with it rather than pass vacuously"
    )
    names = re.findall(r'^\s*"([\w.-]+)",\s*$', block.group(1), re.M)
    assert "hscc-cluster" in names, (
        "hscc-cluster is not in BUNDLED_SKILLS, so the rewritten skill is never "
        "installed and the agent keeps the stale copy"
    )


def test_skill_tool_tables_are_exactly_the_registered_tools():
    """Same set, both directions — the drift rule, applied to the skill."""
    registered = set(_registered())
    assert registered, "the registry parsed empty — the parser or __init__.py changed shape"
    documented = set(TOOL_ROW.findall(_sections()[0]))

    missing = sorted(registered - documented)
    assert not missing, (
        f"{len(missing)} registered tool(s) the skill does not document: "
        f"{', '.join(missing)}. The audit's F1 finding is precisely this: the registry "
        "moved and the bundled skill kept describing the old toolset, so a bootstrapped "
        "agent cannot discover the tool it needs."
    )

    phantom = sorted(documented - registered)
    assert not phantom, (
        f"the skill documents {', '.join(phantom)} as a tool, but "
        "hscc-cluster/__init__.py does not register it — an agent will call a tool "
        "that does not exist"
    )
    assert len(documented) == len(registered)


def test_decided_against_tools_are_declared_absent_and_not_documented():
    """The decided-against verbs must be *said* not to exist, below the split.

    Silence is not enough: the audit's own card body assumed three of these were
    being added, which is how a worker ends up hunting for a ``kanban_paused`` tool
    that D6 deliberately refused. The doc has to carry the refusal and the decision id.
    """
    head, tail = _sections()
    documented_as_tool = set(TOOL_ROW.findall(head))

    for surface, decision in DECIDED_AGAINST:
        assert surface in tail, (
            f"{surface} was decided against a tool ({decision}) but the skill no "
            "longer says so in the decided-against section"
        )
        assert decision in tail, f"the skill lost the {decision} decision id for {surface}"
        snake = surface.replace("hscc ", "").replace(" ", "_").replace("-", "_")
        assert snake not in documented_as_tool, (
            f"{snake} appears in the tool tables — it is a {decision} 'no tool' "
            "decision, so documenting it as a tool is a false promise to an agent"
        )
