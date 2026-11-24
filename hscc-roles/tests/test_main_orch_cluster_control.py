"""The "controlling the cluster" section in the main orchestrator's SOUL.

2.17.4 card 5 (t_d0a3ab8a). The agent surface was audited verb by verb
(``docs/audits/2026-11-23-agent-surface.md``, operator-signed-off on
t_4c12ff7f) and the verdict was that the surface is coherent but unwritten: a
fresh install could not tell which door exists, which verb has no tool BY
DECISION, and what belongs to the operator alone. The section
(``orchestrators._MAIN_CLUSTER_CONTROL``) is that verdict table, carried on the
main-orchestrator identity so a bootstrap regeneration cannot drop it.

What is pinned, and why each pin is the pin that matters:

1. It renders — through BOTH write paths (``compose_soul`` and bootstrap's own
   ``ensure_orchestrator`` -> SOUL.md), exactly once, inside the role section
   that ``orch_sync`` keeps byte-for-byte.
2. It is MAIN-only: a project orchestrator (which has no cluster authority) and
   a worker must not be told they hold the cluster.
3. NO INVENTED VERBS — the audit's own F3/F5 class, turned against this card.
   Every backticked tool name must be in the registry
   ``hscc-cluster/__init__.py`` actually registers, and every ``hscc`` verb must
   be dispatched by the shipped CLI (top-level tuple + the group's own
   subcommand table). A name nobody ships is a name the model will call and get
   an error from.
4. The decisions stay written down: the verbs the audit decided AGAINST a tool
   (D2 monitor, D3 fleet power, D6 board brake, D8 merge-queue writes, D11 the
   doctor HTTP route, and the autodown write half) are named as CLI, so nobody
   "fixes" the gap by shelling around a guard. D11 is the one the wave moved:
   §12.2 ruled on the *route* and said the read-only ``doctor`` tool stands, so
   the section maps the tool AND keeps the route-closed decision.
5. The toolsets + skills guarantee is asserted on the GENERATED PROFILE, in a
   sandboxed home — the SOUL claims bootstrap gives this profile
   ``hscc-cluster`` + ``sparkrun``, so the config it is written with must say
   so, whatever the main is named. Every skill the section names must be a
   ``BUNDLED_SKILLS`` entry, because a skill bootstrap never installs is a
   reference the main cannot open.
6. Genericity, same guards as ``_MAIN_ASSISTANT_WORK`` (the repo is public).
7. It does not restate the nudge/channel rules owned by
   ``generator._MAIN_OPS``' "Talking to orchestrators" (t_1992ab13) — two
   copies of a guard list is how one of them goes stale.

Nothing here touches a real profile tree, a real registry, a real ``~/.hscc`` or
the fleet: the profile root goes to a tmp home, the registry to a tmp fixture,
and the CLI/registry checks are pure source reads.
"""
import getpass
import os
import re

import pytest

import generator
import main_orch
import orchestrators

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HEADING = "## Controlling the cluster"

#: The two toolsets the section promises the main profile has.
CLUSTER_TOOLSETS = ("hscc-cluster", "sparkrun")

#: Operative clauses the card asks for, one entry per thing the operator must
#: be able to find in the SOUL. A heading alone would survive a rewrite that
#: deleted the constraint, so the constraint itself is pinned.
DETAILS = (
    # the toolsets + skills guarantee
    "`hscc-cluster` and `sparkrun` toolsets",
    "`hscc-cluster` for these tools and the state",
    # status / monitor: the read surface, and the D2 no-tool decision
    "`cluster_status`",
    "deliberately no monitor tool",
    "`hscc cluster monitor`",
    # the confirm gate, in the order the gate exists to enforce
    "returns its PLAN",
    "read it, then confirm",
    # node pause: the shipped rule, not the registry blurb
    "every model on it runs",
    "refused with no override at all",
    "overrides the",
    # merge queue: read only, writes closed
    "`merge_queue_status`",
    "never enqueues, drops, pauses or",
    # the per-PR hold: the parking lever is named, and why a pull is not it
    "`hscc merge-queue hold`",
    "`hscc merge-queue unhold`",
    "only a pull: the next empty tick auto-enqueues",
    # autodown: read only, arming is the operator's
    "`autodown_status`",
    "never arms, disarms, "
    "wakes or cancels",
    # doctor and main status: the CLI-only reads
    "`hscc project doctor`",
    "`hscc main status`",
    # the board brake and fleet power stay CLI
    "`hscc kanban enforce`",
    "`hscc cluster up`",
    "`hscc cluster down`",
    # operator-only
    "A LIVE publish of a project",
    "Arming or changing autodown",
)

#: The audit decisions the section must keep writing down, so the absence of a
#: tool stays a decision and not an invitation to route around the guard.
DECISIONS = {
    "D2 (no cluster monitor tool)": "no monitor tool",
    "D3 (fleet power has no tool)": "audit D3",
    "D6 (the board brake is CLI-only)": "audit D6",
    "D8 (merge-queue writes stay closed)": "audit D8",
    "D11 (the doctor HTTP route stays refused)": "audit D11",
    "section 2.4 (autodown writes are operator scope)": "audit section 2.4",
}

#: Single-word backticked tokens that are NOT agent tools: the confirm gate's
#: own parameter names, and nothing else. Kept deliberately tiny — every other
#: single-word token in the section is held to the registry (see
#: ``test_every_backticked_tool_name_is_a_registered_tool``), which is what
#: makes this list an allow-list rather than an escape hatch.
NON_TOOL_TOKENS = {"confirm", "force"}


#: The operator's 07:24 rule (main-only cluster surface), clause by clause. The
#: SOUL must state it out loud — the shipped role text already claims "Only the
#: orchestrator profile has this toolset; workers do not", and the operator's
#: measurement today is that every profile inherits the plugins, so the claim is
#: only true once it is written down here AND enforced by the bootstrap/skills
#: cards the same brief lists. This file pins the statement; those cards own the
#: enforcement (and ``test_a_worker_still_lacks_the_cluster_toolsets`` pins the
#: half that is already true in code).
#:
#: t_398ea0d5 sharpened this tuple and DELETED three entries
#: ("the hscc skills live here alone", "no worker and no project orchestrator
#: carries them", "toolsets are written into this profile's config") because
#: ``docs/audits/2026-11-23-main-only-surface.md`` measured each of them false of
#: the code as shipped. A pin that holds a false sentence is worse than no pin:
#: it makes the next worker preserve the lie. What is pinned instead is the
#: POLICY (main alone holds the surface, workers and project orchs have none and
#: ask) plus the arrival path, and ``test_the_section_claims_nothing_the_code_
#: falsifies`` below guards the boundary in both directions.
MAIN_ONLY_CLAUSES = (
    "is the ONLY profile on this fleet with cluster control",
    "whatever it was called",                      # resolver-driven, never spelled
    "The `hscc-cluster` tools",
    "the `hscc` slash commands",
    "the `sparkrun` passthrough",
    "the hscc skills are this profile's surface",
    "a worker profile and a project orchestrator profile have none of it",
    "they ask — they do not act",
    "The `hscc-cluster` and `sparkrun` toolsets",
    "a skill is a reading, not a licence",
    "it comes to you",
    "a card on its own board",
    "`orch_message`",
    "a cluster verb is never handed to a profile",
)

#: Sentences that would put a present-tense mechanism claim in the section that
#: the 2026-11-23 audit measured FALSE. Each is paired below with the shipped
#: code that falsifies it, so this list is a guard and not a superstition.
FALSIFIED_CLAIMS = {
    "the skills are exclusive to this profile": "live here alone",
    "the skills are exclusive (second phrasing)": "belong to this profile alone",
    "project orchs carry no cluster toolset": "no project orchestrator carries them",
    "bootstrap writes the toolsets into this config": "written into this profile's config",
}


def _missing_exclusivity_claims(text):
    """Every MAIN_ONLY_CLAUSES entry absent from ``text``.

    A function rather than a loop inside one test, so the mutation probe below
    can point the SAME predicate at a gutted copy of the section and prove the
    pin bites. `test_the_main_only_cluster_rule_is_stated` asserts this returns
    nothing for the shipped text; the probe asserts it returns the deleted claim
    for a text with the claim removed.
    """
    return [c for c in MAIN_ONLY_CLAUSES if c not in text]


#: Phrasings that would quietly hand the cluster surface to a profile that must
#: ask for it instead. The operator's rule is not only "do not give it to them";
#: the SOUL must not imply it either, because the SOUL is what an agent follows.
WORKER_ACCESS_PHRASES = (
    "workers can", "worker can", "workers may", "worker may",
    "workers have", "worker has", "workers get", "worker gets",
    "workers call", "worker calls", "any profile has", "every profile has",
    "all profiles have", "both profiles have", "other profiles have",
)


# --------------------------------------------------------------------------
# the shipped surface these pins read (pure source reads — no imports of the
# plugin package, no hermes runtime, no fleet)
# --------------------------------------------------------------------------

_TOOL_LIST_RE = re.compile(r"^(_\w*TOOLS) = \[(.*?)^\]", re.M | re.S)
_TOOL_ENTRY_RE = re.compile(r'^\s*\("(\w+)",', re.M)
_TOP_LEVEL_RE = re.compile(r"TOP_LEVEL_COMMANDS = \((.*?)\)", re.M | re.S)
_QUOTED_RE = re.compile(r'"([a-z][a-z0-9_-]*)"')

#: Which source file(s) own each CLI group's dispatch, so a group subcommand is
#: checked against the table that actually routes it. A group absent here is
#: only ever checked at top level.
GROUP_SOURCES = {
    "cluster": ("hscc_daemon/hscc.py",),
    "template": ("hscc_daemon/hscc.py",),
    "node": ("hscc_daemon/hscc.py",),
    "autodown": ("hscc_daemon/autodown_cli.py",),
    "kanban": ("hscc_daemon/kanban_cli.py", "hscc_daemon/kanban_blocked.py"),
    "api": ("hscc_daemon/api_cli.py",),
    "main": ("hscc_daemon/main_cli.py",),
    "merge-queue": ("hscc_daemon/merge_queue_cli.py",),
    "cards": ("hscc_daemon/cards_live.py",),
    # `project` is the delegated flightdeck family (audit section 2.7): the
    # family-level verdict is that its verbs live outside this repo's tables, so
    # only the top-level verb is checked for it.
    "project": (),
}


def _registered_tools():
    """Every name ``hscc-cluster/__init__.py`` registers, from its own source."""
    src = open(os.path.join(REPO, "hscc-cluster", "__init__.py"),
               encoding="utf-8").read()
    found = set()
    for _name, body in _TOOL_LIST_RE.findall(src):
        found.update(_TOOL_ENTRY_RE.findall(body))
    assert len(found) >= 28, (
        f"the registry read produced {len(found)} tools; the parser no longer "
        "matches the registry shape, so this pin is silently vacuous")
    return found


def _bundled_skills():
    """Names ``hscc-skills/hscc.py`` installs — the installer's own list."""
    src = open(os.path.join(REPO, "hscc-skills", "hscc.py"), encoding="utf-8").read()
    block = re.search(r"BUNDLED_SKILLS = \[(.*?)^\]", src, re.M | re.S)
    assert block, "BUNDLED_SKILLS not found — the installer's list moved"
    return set(re.findall(r'"([a-z0-9-]+)"', block.group(1)))


def _skill_or_toolset_vocabulary():
    """Backticked single words that are skills or toolsets, not agent tools.

    Derived from the installer's list plus the two cluster toolset names, so the
    set is the shipped vocabulary rather than a hand-written excuse.
    """
    return _bundled_skills() | set(CLUSTER_TOOLSETS)


def _top_level_verbs():
    src = open(os.path.join(REPO, "hscc_daemon", "hscc.py"), encoding="utf-8").read()
    body = _TOP_LEVEL_RE.search(src)
    assert body, "TOP_LEVEL_COMMANDS not found — the CLI's verb contract moved"
    return set(_QUOTED_RE.findall(body.group(1)))


def _group_subcommands(group):
    """Quoted literals in the module(s) that dispatch this group's verbs."""
    words = set()
    for rel in GROUP_SOURCES.get(group, ()):
        path = os.path.join(REPO, *rel.split("/"))
        if not os.path.exists(path):
            continue
        words.update(_QUOTED_RE.findall(open(path, encoding="utf-8").read()))
    return words


# --------------------------------------------------------------------------
# render fixtures (same shape as test_main_orch_assistant_work.py)
# --------------------------------------------------------------------------

@pytest.fixture
def registry_file(tmp_path):
    """A fixture registry — never the operator's real ``registry.yaml``.

    Project names here deliberately do NOT collide with the product or CLI name
    (``hscc``), because the strict pin below forbids any registry project name
    appearing in the section and this section legitimately names the ``hscc``
    skill and the ``hscc`` verb on purpose.
    """
    import yaml
    doc = {"projects": [
        {"name": "ecofire-app", "repo": "/tmp/ecofire", "board": "ecofire-app"},
        {"name": "pontaje", "repo": "/tmp/pontaje", "board": "pontaje"},
    ]}
    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(p)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the generator's profile root at a tmp home (never ``~/.hermes``)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    return str(tmp_path / "profiles")


def _main_spec(registry_file):
    resolved = orchestrators.resolve_orchestrator(None, path=registry_file)
    return orchestrators._main_orch_spec(resolved, registry_file)


def _rendered_soul(registry_file):
    return generator.compose_soul(_main_spec(registry_file),
                                  base_identity="BASE-IDENTITY")


def _bootstrapped(registry_file, isolated_home, base_identity="BASE-IDENTITY"):
    """The main profile as BOOTSTRAP writes it: (SOUL text, config dict)."""
    import yaml
    got = orchestrators.ensure_orchestrator(None, base_identity=base_identity,
                                            path=registry_file)
    assert got["profile"] == main_orch.main_profile()
    pdir = os.path.join(isolated_home, got["profile"])
    with open(os.path.join(pdir, "SOUL.md"), encoding="utf-8") as fh:
        soul = fh.read()
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return soul, cfg


def _section_only(text):
    """Just this section, so pins judge our text and not the rest of the SOUL."""
    start = text.index(HEADING)
    rest = text[start + len(HEADING):]
    nxt = rest.find("\n## ")
    return text[start:] if nxt < 0 else text[start:start + nxt]


# --------------------------------------------------------------------------
# 1. it renders, once, on both write paths
# --------------------------------------------------------------------------

def test_the_section_survives_a_bootstrap_render(registry_file, isolated_home):
    soul, _cfg = _bootstrapped(registry_file, isolated_home)
    assert soul.count(HEADING) == 1


def test_every_named_clause_renders(registry_file):
    soul = _rendered_soul(registry_file)
    assert soul.count(HEADING) == 1
    for detail in DETAILS:
        assert detail in soul, f"required clause missing from the render: {detail!r}"


@pytest.mark.parametrize("name", ["general", "ops"])
def test_the_section_follows_the_main_name_not_a_literal(
        registry_file, tmp_path, name):
    """A bootstrap that named its main something else still gets the section."""
    if name != "general":
        main_orch.store_name(
            name, profiles_dir=str(tmp_path / "profiles"),
            registry_path=str(tmp_path / "absent-registry.yaml"))
    spec = {"name": name,
            "identity": orchestrators._main_orch_spec(
                {"profile": name}, registry_file)["identity"],
            "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE-IDENTITY")
    assert soul.count(HEADING) == 1
    assert "You are the ONE main orchestrator" in soul


def test_a_project_orchestrator_does_not_get_the_section(registry_file):
    """Project orchestrators hold a board, NOT the physical cluster."""
    resolved = orchestrators.resolve_orchestrator("pontaje", path=registry_file)
    spec = orchestrators._project_orch_spec("pontaje", resolved)
    soul = generator.compose_soul(spec, base_identity="BASE")
    assert HEADING not in soul


def test_a_worker_does_not_get_the_section():
    soul = generator.compose_soul(
        {"name": "coder", "identity": "You build.\n", "preload_skills": []},
        base_identity="BASE")
    assert HEADING not in soul


def test_the_section_sits_inside_the_role_section(registry_file):
    """Between ``## Role:`` and ``## Operational`` — the span both write paths own."""
    soul = _rendered_soul(registry_file)
    role_at = soul.index("\n## Role: ")
    ops_at = soul.index("\n## Operational", role_at)
    assert role_at < soul.index(HEADING) < ops_at


def test_the_block_accessor_is_what_the_soul_carries(registry_file):
    """One source of truth: the render embeds the accessor's exact text."""
    block = orchestrators.main_cluster_control_block()
    assert block.startswith(HEADING)
    assert block in _rendered_soul(registry_file)


def test_regeneration_keeps_the_section_once(registry_file, isolated_home):
    """A second bootstrap render is a no-op; the section is neither lost nor doubled."""
    soul, _cfg = _bootstrapped(registry_file, isolated_home)
    assert orchestrators.ensure_orchestrator(
        None, base_identity="BASE-IDENTITY", path=registry_file)["changed"] is False
    soul_path = os.path.join(isolated_home, main_orch.main_profile(), "SOUL.md")
    with open(soul_path, encoding="utf-8") as fh:
        again = fh.read()
    assert again == soul
    assert again.count(HEADING) == 1


def test_the_main_soul_still_carries_the_other_sections_once(registry_file,
                                                             isolated_home):
    """Adding a section must not disturb what was already guaranteed."""
    soul, _cfg = _bootstrapped(registry_file, isolated_home)
    assert soul.count("## Operational") == 1
    assert soul.count(generator._ANTI_CHURN_RULES) == 1
    assert soul.count("\n## Role: ") == 1
    assert soul.count(orchestrators.main_assistant_work_block()) == 1
    assert "You are the ONE main orchestrator" in soul


# --------------------------------------------------------------------------
# 2. no invented verbs (the audit's own F3/F5 class, applied to this card)
# --------------------------------------------------------------------------

def _backticked(section):
    return re.findall(r"`([^`]+)`", section)


# --------------------------------------------------------------------------
# what "mapped" means: a SHAPE, not a mention (t_053a7210)
# --------------------------------------------------------------------------
#
# Both coverage pins below were, until now, `` f"`{tool}`" in section ``.
# Measured on t_8024d1ff: deleting the whole `doctor` row left the roles leg
# GREEN, because a neighbouring sentence happened to carry "not on the `doctor`
# tool above". A tool the main only meets in a parenthetical is a tool it will
# not think to call — the exact failure this gate exists to prevent, which a
# substring check cannot prevent.
#
# So the pins ask for the section's OWN convention, stated in the section:
# "Each line is a shipped door: the agent tool, then the CLI verb it mirrors in
# brackets." A tool is MAPPED when a bullet row carries its backticked name in
# NAME POSITION — at the head of the row, just after a short ``Label: `` at the
# row head or a clause boundary, or as the next name of a ``, ` / ` and ` /
# ` then `` list — and the row goes on to carry a definition (the ` — ` job
# clause, a bracketed CLI verb, or a name list). A name dropped into prose
# satisfies none of that, so a passing mention no longer closes the gate.
_MAP_NAME = re.compile(r"`([a-z][a-z_]*)`")
_MAP_LABEL = re.compile(r"[^`:—.;]{1,48}: ")
_MAP_NOTE = re.compile(r"(?:\s*(?:\[[^\]]*\]|\([^)]*\)))*")
_MAP_LIST = re.compile(r"(?:,\s(?:then\s)?|\sand\s)")
_MAP_JOB = re.compile(r"\s—\s")
_MAP_CLAUSE = re.compile(r"(?:\.|;|:)(?:\s|$)")
_MAP_DEFINES = re.compile(r"(?:\s—\s|\s\[|,\s)")
_MAP_BOUNDARY = re.compile(r"[.;] ")


def _mapped_tools(section):
    """Every tool the section MAPS — name position plus a definition.

    Returns the tool names, not the rows: the pins ask membership, and a caller
    that wants the row can re-read it. See the block comment above for the rule
    and for the mutation that made it necessary.
    """
    mapped = set()
    for line in section.splitlines():
        if not line.startswith("- ") or not _MAP_DEFINES.search(line):
            continue  # not a row, or a sentence no agent reads as a door
        for start in [2] + [m.end() for m in _MAP_BOUNDARY.finditer(line)]:
            pos, at_row_head = start, start == 2
            label = _MAP_LABEL.match(line, start)
            if label:
                pos, at_row_head = label.end(), False
            while True:
                name = _MAP_NAME.match(line, pos)
                if not name:
                    break
                note = _MAP_NOTE.match(line, name.end())
                after = note.end() if note else name.end()
                listing = _MAP_LIST.match(line, after)
                if listing:
                    mapped.add(name.group(1))
                    pos = listing.end()
                    continue
                if _MAP_JOB.match(line, after):
                    mapped.add(name.group(1))
                elif not at_row_head and _MAP_CLAUSE.match(line, after):
                    mapped.add(name.group(1))
                break
    return mapped


def test_every_backticked_tool_name_is_a_registered_tool():
    """A tool name the registry does not register is a tool that will not exist.

    Single-word backticked tokens are exactly how agent tools are written in the
    section, so each one must be in the shipped registry — or be one of the two
    confirm-gate parameter names on the allow-list. Renaming a tool here without
    renaming it in the plugin reddens this.
    """
    registered = _registered_tools()
    vocabulary = _skill_or_toolset_vocabulary()
    section = _section_only(orchestrators.main_cluster_control_block())
    invented = set()
    named_tools = set()
    for token in _backticked(section):
        if " " in token or not re.fullmatch(r"[a-z][a-z_]*", token):
            continue  # a CLI verb phrase, a skill/toolset name (hyphenated) or a filename
        if token in NON_TOOL_TOKENS or token in vocabulary:
            continue  # a confirm-gate parameter, or a skill the installer ships
        (named_tools if token in registered else invented).add(token)
    assert not invented, (
        f"the section names tool(s) the registry does not register: "
        f"{sorted(invented)}")
    assert len(named_tools) >= 20, (
        f"only {len(named_tools)} registered tools named — the section is "
        "supposed to be the whole which-tool-for-which-job map, not a sample")


def test_the_named_read_tools_include_the_two_read_only_additions():
    """The read tools the recent waves added are mapped by name.

    ``autodown_status`` / ``merge_queue_status`` from the audit wave,
    ``doctor`` from t_7f570861, ``heal_status`` from t_14b86753. The
    parametrized coverage test above derives its
    cases from the registry and would also pass if a later diff moved a mapped
    tool into its exclusion set; this one is the static pin that keeps these
    reads named whatever the registry does.

    MAPPED, not mentioned: ``_mapped_tools`` is the whole predicate, so a diff
    that deletes one of these rows and leaves a sentence about it goes red here
    rather than green (t_8024d1ff measured exactly that on `doctor`).
    """
    mapped = _mapped_tools(_section_only(orchestrators.main_cluster_control_block()))
    for tool in ("autodown_status", "merge_queue_status", "doctor",
                 "heal_status",
                 "node_paused", "node_pause", "node_resume", "orch_message"):
        assert tool in mapped, (
            f"{tool} is not mapped to a job — the section must carry it as a "
            "row (the tool in name position, then its job), not merely name it")


@pytest.mark.parametrize("tool", sorted(_registered_tools()))
def test_every_registered_tool_is_named_or_deliberately_excluded(tool):
    """The section covers the registry surface.

    Exclusion is allowed but must be argued: a tool the section skips is a tool
    the main will not think to call, so the exclusion list is deliberately short
    and any NEW registered tool must either be mapped or be reasoned about here.

    "Mapped" is the shape rule in ``_mapped_tools``, tightened on t_053a7210
    after a deleted `doctor` row passed the old substring check on a passing
    mention. The exclusion set itself is deliberately UNCHANGED by that
    tightening: this hardens what a row is, it does not widen who is exempt.
    """
    excluded = {
        "pick_node",       # a helper inside provision_model, not a job on its own
        "list_recipes",    # recipe browsing; provisioning names the recipe path
    }
    mapped = _mapped_tools(_section_only(orchestrators.main_cluster_control_block()))
    assert tool in excluded or tool in mapped, (
        f"{tool} is registered but neither mapped as a row in the section nor "
        "listed in its exclusion set — add the row (the tool in name position, "
        "then the job it does), or say here why the main never needs it")


def test_a_passing_mention_is_not_a_mapping():
    """The mutant this card was cut for, kept as a case: a mention is not a row.

    t_8024d1ff deleted the `doctor` row and the roles leg stayed GREEN, because a
    neighbouring sentence carried "not on the `doctor` tool above". The shapes
    that hid it are fed here to the SAME predicate the two coverage pins use, and
    each must come back unmapped. The last arm is the positive control: a real
    row must still map, because a predicate that refused everything would also
    pass the first three arms and be worth nothing.
    """
    mention = ("- Two reads stay CLI-only on purpose. The preflight verdict is "
               "not on the `doctor` tool above.")
    bare = "- `doctor`."
    labelled = "- Note: `doctor`."
    row = "- `doctor` — what the preflight door says about this host."
    prose = f"{HEADING}\n{mention}\n{bare}\n{labelled}\n"
    assert "doctor" not in _mapped_tools(prose), (
        "a passing mention satisfied the coverage gate — the shape rule broke")
    assert "doctor" in _mapped_tools(f"{HEADING}\n{row}\n"), (
        "the predicate refuses a real mapping row too, which would make both "
        "coverage pins vacuous")


def test_every_named_cli_verb_is_dispatched_by_the_cli():
    """Every backticked ``hscc ...`` verb the section names exists in the dispatch.

    Top-level verbs are checked against ``TOP_LEVEL_COMMANDS``; group verbs
    against the subcommand table in the module that routes that group. Backticks
    are the contract — prose may say "the hscc skills", a code span may not say
    a verb the CLI would answer "unknown command" to. This is the audit's F3/F5
    class turned around: a verb this SOUL invents is a verb an agent will run and
    then improvise around.
    """
    top = _top_level_verbs()
    section = _section_only(orchestrators.main_cluster_control_block())
    bad = []
    checked = 0
    for span in _backticked(section):
        m = re.fullmatch(r"hscc ([a-z][a-z0-9-]*)(.*)", span.strip())
        if not m:
            continue
        verb, rest = m.group(1), m.group(2)
        if verb not in top:
            bad.append(f"hscc {verb}")
            continue
        checked += 1
        # A group verb is followed by " <sub>" before any punctuation.
        sub_m = re.match(r"^\s+([a-z][a-z0-9-]*)", rest)
        if not sub_m or verb == "project":
            continue
        sub = sub_m.group(1)
        if sub not in _group_subcommands(verb) and verb in GROUP_SOURCES:
            bad.append(f"hscc {verb} {sub}")
    assert not bad, f"undispatched verb(s) named in the section: {sorted(set(bad))}"
    assert checked >= 15, (
        f"only {checked} verbs checked — the section should carry the surface, "
        "and a pin that inspects almost nothing passes vacuously")


# --------------------------------------------------------------------------
# 3. the decisions stay written down
# --------------------------------------------------------------------------

@pytest.mark.parametrize("decision", sorted(DECISIONS))
def test_the_no_tool_decisions_stay_written_down(decision):
    """Each verb the audit decided against a tool is named as CLI.

    The audit's verdict is only durable where somebody reads it: an agent that
    cannot find a tool for a verb it was told to do will route around the guard.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    assert DECISIONS[decision] in section, f"{decision} is no longer written down"


def test_the_per_pr_hold_is_the_named_parking_lever():
    """Parking one PR is `hold`, and the section says why a pull is not one.

    The symptom this closes (t_662753d6, wording owned by t_c354c61c): PRs
    "parked" with ``hscc merge-queue remove`` came back on their next push,
    because auto-enqueue runs on every empty tick and the only existing
    suppression covers a PR that just failed a red gate on the SAME head. The
    agent doing the parking has to be told both halves — the verb that holds,
    and the reason the verb it reached for does not — or it goes back to
    ``remove`` plus a PR comment and re-pulls by hand.

    Both verbs are written as full ``hscc ...`` code spans, so the dispatch pin
    above checks them against ``merge_queue_cli.py``: a hold the CLI does not
    route would redden here and not in prose.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    for verb in ("`hscc merge-queue hold`", "`hscc merge-queue unhold`"):
        assert verb in section, f"{verb} is missing from the write-half list"
    assert "only a pull" in section, "the section stopped saying a pull is not a hold"
    assert "auto-enqueues" in section, "the re-queue mechanism behind the hold went missing"
    assert "survives a push" in section, "the hold's own guarantee went missing"
    assert "project-wide lever" in section, (
        "the pause/hold scope distinction went missing — a hold is per PR, "
        "pause halts every queued PR in the project")


def test_the_operators_only_actions_are_named():
    section = _section_only(orchestrators.main_cluster_control_block())
    for clause in ("**Operator-only.**", "**A LIVE publish of a project.**",
                   "**Arming or changing autodown.**"):
        assert clause in section, f"{clause!r} missing"
    # ...and the gate is the shipped one, not a new invention.
    assert "hscc notify" in section


def test_the_section_offers_no_bypass_of_the_guards():
    """The iron rule: the section must not hand the main a raw transport.

    Naming ``docker``, ``ssh`` or a raw sparkrun stop as the way to get a job
    done would contradict the operator's fleet-access rule and the audit's
    S1/S4 finding about second implementations.
    """
    section = _section_only(orchestrators.main_cluster_control_block()).lower()
    for bypass in ("docker", " ssh", "launchctl", "sudo"):
        assert bypass not in section, f"the section names a bypass path: {bypass!r}"
    assert "never shell" in section, "the anti-bypass sentence itself went missing"


def test_the_section_defers_the_channel_rules_to_the_shared_block():
    """One copy of the nudge guard list (coordination note, t_1992ab13).

    ``orch_message``'s channel rules (queue-only, rate-limited, auto-signed,
    audited, not a work order, no side channels) belong to
    ``generator._MAIN_OPS``' "Talking to orchestrators". Restating them here
    gives the SOUL two copies of one guard, and the copy nobody edits is the one
    that goes stale.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    assert "`orch_message`" in section, "the tool itself must still be mapped"
    for phrase in ("queue-only", "rate-limited", "auto-signed", "audited",
                   "work order", "side channel", "dry-run"):
        assert phrase not in section.lower(), (
            f"the nudge/channel rule {phrase!r} was restated here; it lives in "
            "the Talking to orchestrators section")


# --------------------------------------------------------------------------
# 3b. the operator's main-only cluster rule (07:24 brief), stated out loud
# --------------------------------------------------------------------------

@pytest.mark.parametrize("clause", MAIN_ONLY_CLAUSES)
def test_the_main_only_cluster_rule_is_stated(clause):
    """The section says the MAIN profile alone holds the cluster surface.

    The operator rule (2026-11-23 07:24) is that the cluster control and monitor
    surface — cluster tools, slash commands, the sparkrun passthrough and the
    hscc skills — is hooked to the bootstrap-created main orchestrator alone.
    Each clause here is one piece of that sentence; the whole sentence is what an
    agent reads before deciding whether to act or to ask.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    assert clause in section, f"main-only clause missing: {clause!r}"


def test_the_main_is_named_by_the_resolver_never_spelled():
    """The exclusivity is keyed on the bootstrap-configured main, not a name.

    The operator may call the main anything at bootstrap; a SOUL that spells the
    default name is wrong on every install that does, and is the same
    hardcoding class ``orch_message`` was fixed for (#635).
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    assert "general" not in section.lower(), (
        "a hardcoded main-orchestrator name in the section — read it from the "
        "bootstrap config, as the resolver does")
    assert "whatever it was called" in section


def test_cluster_access_is_never_phrased_as_available_to_a_worker():
    """No sentence may imply a worker or project orchestrator holds the surface.

    Two halves: no access phrase aimed at another profile kind, and every line
    that DOES name one either denies it or routes it to the main. Plus a positive
    control, because a phrasing guard that cannot fail is worth nothing.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    lowered = section.lower()
    for phrase in WORKER_ACCESS_PHRASES:
        assert phrase not in lowered, (
            f"the section phrases cluster access as available to another "
            f"profile: {phrase!r}")
    for line in section.splitlines():
        low = line.lower()
        if not any(kind in low for kind in ("worker", "project orchestrator",
                                            "-orch")):
            continue
        assert any(mark in low for mark in ("ask", "no worker", "never",
                                            "alone", "not ")), (
            f"a line names another profile kind without denying it the surface "
            f"or sending it to the main: {line!r}")
    # Positive control: the predicates are satisfiable.
    violation = "- workers may also call `cluster_status` on any profile has it"
    assert any(p in violation for p in WORKER_ACCESS_PHRASES)
    bad_line = "- a worker can run the same tools directly"
    assert not any(mark in bad_line for mark in
                   ("ask", "no worker", "never", "alone", "not "))


def _installed_skill_homes(tmp_path):
    """Every profile home the shipped skills installer writes into — by RUNNING it.

    ``install_review_skill.skill_dirs`` is the installer's OWN discovery function
    (which trees "installed" covers), and it is pure over a directory: pointed at
    a tmp home it answers without a profile tree of its own, a real home or a
    fleet. This is the proof behind ``FALSIFIED_CLAIMS``: the cluster skills go
    to the root home and to every named profile, worker and ``*-orch`` alike.
    """
    import importlib.util
    src = os.path.join(REPO, "hscc-bootstrap", "install_review_skill.py")
    spec = importlib.util.spec_from_file_location("_irs_exclusivity_probe", src)
    assert spec is not None and spec.loader is not None, (
        f"cannot load the installer's discovery module from {src} — it moved; "
        "re-measure the skills half of FALSIFIED_CLAIMS before trusting it")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    for name in ("general", "backend-engineer", "demo-orch"):
        (home / "profiles" / name / "skills").mkdir(parents=True)
    (home / "profiles" / ".deleted").mkdir()          # not a profile
    return {label for label, _dir, _home in mod.skill_dirs(home)}


def test_the_exclusivity_claim_reaches_both_render_paths(registry_file,
                                                         isolated_home):
    """The claim is not just in the constant — the agent actually receives it.

    Both write paths are checked: ``compose_soul`` and bootstrap's
    ``ensure_orchestrator`` -> SOUL.md. A claim that lives in the source but is
    dropped by one of the two renderers is a claim nobody reads.
    """
    assert not _missing_exclusivity_claims(
        _section_only(_rendered_soul(registry_file))), "compose_soul dropped the rule"
    soul, _cfg = _bootstrapped(registry_file, isolated_home)
    assert not _missing_exclusivity_claims(_section_only(soul)), (
        "the bootstrap SOUL.md dropped the rule")


def test_the_request_path_names_both_channels_and_both_profile_kinds():
    """A cluster action reaches the main as a board card or `orch_message`.

    The operator's rule is only half "the main holds it"; the other half is how
    a request ARRIVES, because that is the sentence a worker reads before it
    shells a cluster verb itself. Both channels must be named, and the sentence
    that names them must name both kinds that have to ask.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    assert "a card on its own board" in section, "the board path dropped"
    assert "`orch_message`" in section, "the orch_message path dropped"
    arriving = [ln for ln in section.splitlines() if "it comes to you" in ln]
    assert arriving, "no sentence carries the request path"
    assert "worker" in arriving[0] and "project orchestrator" in arriving[0], (
        f"the request path is not aimed at both profile kinds: {arriving[0]!r}")


@pytest.mark.parametrize("claim", (
    "is the ONLY profile on this fleet with cluster control",
    "a worker profile and a project orchestrator profile have none of it",
    "a card on its own board",
))
def test_deleting_the_claim_reddens_the_pin(claim):
    """The card's mutation probe, committed rather than run once by hand.

    Delete the claim from the section and the SAME predicate the pin above uses
    must report it missing. This is what makes the enforcement a property of the
    test and not a claim about one: the parent card ran its probes by hand and
    reverted them, so nothing here proved the pin bites if someone edits it flat.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    assert not _missing_exclusivity_claims(section), (
        "the shipped section is already missing a claim; the probe would pass vacuously")
    gutted = section.replace(claim, "")
    assert gutted != section, f"the probe did not bite: {claim!r} is not in the section"
    assert claim in _missing_exclusivity_claims(gutted), (
        f"deleting {claim!r} left the pin green — the claim is not enforced")


def test_the_section_claims_nothing_the_code_falsifies(tmp_path):
    """The exclusivity is the POLICY; the section may not state it as shipped fact.

    Each forbidden sentence is paired with the shipped code that falsifies it, so
    this guard is evidence rather than taste — and it is the reason the deleted
    clauses are NOT restored from the parent card's version:

    * "the skills live here alone" / "belong to this profile alone" — the
      installer's own ``skill_dirs`` returns the root home plus every profile
      (run below, in a tmp home).
    * "no project orchestrator carries them" — ``_is_orchestrator`` matches any
      ``*-orch``, and ``orchestrator_toolsets()`` carries both cluster toolsets,
      so ``generator`` writes them into a project orchestrator's config too.
    * "written into this profile's config" — that key is not what loads a plugin
      toolset (per-home ``plugins.enabled`` is, and nothing here writes it), so
      it names a mechanism the code does not implement.
    """
    section = _section_only(orchestrators.main_cluster_control_block())
    for why, phrase in FALSIFIED_CLAIMS.items():
        assert phrase not in section, (
            f"the section states {phrase!r} — {why}, and "
            "docs/audits/2026-11-23-main-only-surface.md measured it false")
    homes = _installed_skill_homes(tmp_path)
    assert {"default (root)", "general", "backend-engineer", "demo-orch"} <= homes, (
        f"the installer's own discovery changed shape ({sorted(homes)}); re-measure "
        "before trusting the skills half of this guard")
    assert generator._is_orchestrator("demo-orch"), (
        "project orchestrators stopped being orchestrators — re-check whether the "
        "config-falsification above still applies")
    assert set(CLUSTER_TOOLSETS) <= set(generator.rolelib.orchestrator_toolsets())


def test_the_falsified_claim_guard_can_fire():
    """Positive control: the guard above is satisfiable, not unfalsifiable."""
    bad = ("the hscc skills live here alone and belong to this profile alone; "
           "no project orchestrator carries them, because the toolsets are "
           "written into this profile's config")
    for why, phrase in FALSIFIED_CLAIMS.items():
        assert phrase in bad, f"control text does not contain {why}: {phrase!r}"
    section = _section_only(orchestrators.main_cluster_control_block())
    assert all(p not in section for p in FALSIFIED_CLAIMS.values())


def test_a_worker_profile_is_still_written_without_the_cluster_toolsets():
    """The half of the operator's rule that is already true in code.

    ``rolelib`` keeps the cluster tools on the orchestrator side of one boundary
    (``_FULL_TOOLSETS`` versus ``_ORCH_TOOLSETS``); a generated worker config that
    grew them would make the SOUL's exclusivity sentence a lie in the other
    direction. The rest of the rule — scoping the plugins, reversing the skill
    fan-out — is the operator's own follow-up cards, not this one, and the
    worker-template wording sweep is t_c3a29c37 (not this card).
    """
    assert all(ts not in generator.rolelib.role_toolsets()
               for ts in CLUSTER_TOOLSETS)
    assert set(CLUSTER_TOOLSETS) <= set(generator.rolelib.orchestrator_toolsets())


# --------------------------------------------------------------------------
# 4. the profile-level guarantee the section claims (sandboxed home)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("main_name", ["general", "ops"])
def test_the_main_profile_is_written_with_the_cluster_toolsets(
        registry_file, tmp_path, monkeypatch, main_name):
    """The SOUL claims bootstrap gives the main the cluster toolsets.

    Asserted on the config the generator actually writes for the MAIN profile
    (resolved through ``main_orch``, so a renamed main is covered too), in a
    tmp home — the same claim, checked where it is made.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    if main_name != "general":
        main_orch.store_name(
            main_name, profiles_dir=str(tmp_path / "profiles"),
            registry_path=str(tmp_path / "absent-registry.yaml"))
    resolved = orchestrators.resolve_orchestrator(None, path=registry_file)
    assert resolved["profile"] == main_orch.main_profile()
    generator.generate_profile(orchestrators._main_orch_spec(resolved,
                                                             registry_file),
                               base_identity="BASE")
    cfg_path = os.path.join(str(tmp_path / "profiles"), resolved["profile"],
                            "config.yaml")
    import yaml
    with open(cfg_path, encoding="utf-8") as fh:
        toolsets = yaml.safe_load(fh)["toolsets"]
    for ts in CLUSTER_TOOLSETS:
        assert ts in toolsets, (
            f"{ts} missing from the generated main profile ({main_name}); the "
            "SOUL names cluster tools the profile cannot call")
    assert set(CLUSTER_TOOLSETS) <= set(generator.rolelib.orchestrator_toolsets())


def test_a_worker_still_lacks_the_cluster_toolsets():
    """The guarantee is main/orchestrator-scoped; widening it is the bug class.

    (Pinned from the operator-rule side too in
    ``test_a_worker_profile_is_still_written_without_the_cluster_toolsets``;
    this one is the config-generation side, on a generated profile.)
    """
    assert all(ts not in generator.rolelib.role_toolsets()
               for ts in CLUSTER_TOOLSETS)


def test_every_skill_the_section_names_is_one_bootstrap_installs():
    """The section points only at skills and toolsets that actually exist.

    A named skill bootstrap never installs is a reference the main cannot open —
    the audit's F11/F12 shape, caught here at the sentence that makes the promise
    rather than at spawn time. The two skills the section relies on must be in
    the installer's list, and no other single-word backticked token may be a
    name nothing ships.
    """
    bundled = _bundled_skills()
    section = _section_only(orchestrators.main_cluster_control_block())
    # The reference the section makes is these two skills.
    for skill in ("hscc", "hscc-cluster"):
        assert f"`{skill}`" in section, f"the section stopped naming the {skill} skill"
        assert skill in bundled, (
            f"{skill} is not in BUNDLED_SKILLS — bootstrap installs no such skill")
    named = {t for t in _backticked(section)
             if " " not in t and ("-" in t or t in bundled)}
    assert named, "the section names no skill or toolset at all"
    unknown = named - _skill_or_toolset_vocabulary()
    assert not unknown, (
        f"the section points at {sorted(unknown)}, which is neither a bundled "
        "skill nor a cluster toolset")


# --------------------------------------------------------------------------
# 5. genericity: the repo is public
# --------------------------------------------------------------------------

ASSISTANT_PRODUCT_NAMES = (
    "claude", "chatgpt", "copilot", "gemini", "cursor", "codex",
    "grok", "llama", "mistral",
)


def test_the_section_has_no_absolute_or_home_relative_paths():
    section = _section_only(orchestrators.main_cluster_control_block())
    assert "/" not in section, "path-shaped token in the section"
    assert "~" not in section, "home-relative path in the section"
    assert "\\" not in section
    assert not re.search(r"[A-Za-z]:[\\/]", section)


def test_the_section_never_names_the_local_account():
    section = _section_only(orchestrators.main_cluster_control_block())
    lowered = section.lower()
    user = getpass.getuser().lower()
    home_basename = os.path.basename(os.path.expanduser("~")).lower()
    assert user not in lowered
    assert home_basename not in lowered
    assert os.path.expanduser("~").lower() not in lowered
    assert "@" not in section
    assert not re.search(r"\bhttps?://", lowered)
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", section), (
        "an IP literal in a fleet template")


@pytest.mark.parametrize("product", ASSISTANT_PRODUCT_NAMES)
def test_the_section_names_no_assistant_product(product):
    section = _section_only(orchestrators.main_cluster_control_block())
    assert product not in section.lower()


def test_the_section_is_self_contained_prose(registry_file):
    soul = _rendered_soul(registry_file)
    section = _section_only(soul)
    assert "{" not in section and "}" not in section
    assert "TODO" not in section and "FIXME" not in section
    for project in orchestrators.list_registry_projects(registry_file):
        assert project not in section, (
            f"the fixture project name {project!r} got baked into the section")


def test_the_genericity_guards_are_not_vacuous():
    """Positive control: each guard above can actually fire on real text."""
    leaked = ("- run `hscc cluster bogus` on 192.0.2.10 from ~/.hscc as "
              f"{getpass.getuser()} with {ASSISTANT_PRODUCT_NAMES[0]}")
    section = _section_only(orchestrators.main_cluster_control_block())
    assert "/" in leaked and "~" in leaked
    assert getpass.getuser().lower() in leaked.lower()
    assert re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", leaked)
    assert ASSISTANT_PRODUCT_NAMES[0] in leaked.lower()
    # ...and the real section is clean on the very predicates just demonstrated.
    assert "/" not in section and "~" not in section
    assert getpass.getuser().lower() not in section.lower()
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", section)
