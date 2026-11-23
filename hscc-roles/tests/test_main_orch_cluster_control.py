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
   (D2 monitor, D3 fleet power, D6 board brake, D8 merge-queue writes, D11
   doctor, and the autodown write half) are named as CLI, so nobody "fixes" the
   gap by shelling around a guard.
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
    "D11 (doctor stays CLI)": "audit D11",
    "section 2.4 (autodown writes are operator scope)": "audit section 2.4",
}

#: Single-word backticked tokens that are NOT agent tools: the confirm gate's
#: own parameter names, and nothing else. Kept deliberately tiny — every other
#: single-word token in the section is held to the registry (see
#: ``test_every_backticked_tool_name_is_a_registered_tool``), which is what
#: makes this list an allow-list rather than an escape hatch.
NON_TOOL_TOKENS = {"confirm", "force"}


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
    """The tools the wave added (autodown_status, merge_queue_status) are mapped."""
    section = _section_only(orchestrators.main_cluster_control_block())
    for tool in ("autodown_status", "merge_queue_status", "node_paused",
                 "node_pause", "node_resume", "orch_message"):
        assert f"`{tool}`" in section, f"{tool} is not mapped to a job"


@pytest.mark.parametrize("tool", sorted(_registered_tools()))
def test_every_registered_tool_is_named_or_deliberately_excluded(tool):
    """The section covers the registry surface.

    Exclusion is allowed but must be argued: a tool the section skips is a tool
    the main will not think to call, so the exclusion list is deliberately short
    and any NEW registered tool must either be mapped or be reasoned about here.
    """
    excluded = {
        "pick_node",       # a helper inside provision_model, not a job on its own
        "list_recipes",    # recipe browsing; provisioning names the recipe path
    }
    section = _section_only(orchestrators.main_cluster_control_block())
    assert tool in excluded or f"`{tool}`" in section, (
        f"{tool} is registered but neither mapped in the section nor listed in "
        "its exclusion set — add the row, or say here why the main never needs it")


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
    """The guarantee is main/orchestrator-scoped; widening it is the bug class."""
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
