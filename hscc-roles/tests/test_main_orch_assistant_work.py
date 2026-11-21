"""The "work like the operator's main assistant" section in the main SOUL.

Operator directive (t_164e4b25): the main-orchestrator SOUL must carry this
section so a bootstrap regeneration can never drop it. The section lives in
``orchestrators._MAIN_ASSISTANT_WORK`` and is appended to the main spec's
identity, which ``compose_soul`` embeds verbatim.

These tests render through the paths that actually write a SOUL — bootstrap's
own ``ensure_orchestrator`` (``generate_profile`` -> SOUL.md) and the pure
``compose_soul`` render — never by executing a bootstrap script. Nothing here
touches a real profile tree, a real registry or the cluster: the profile root
goes to a tmp home and the registry to a tmp fixture, exactly as the rest of
this suite does.

Two things are pinned beyond "the text is there": the section is MAIN-only (a
project orchestrator and a worker must not get it — proof it is wired into the
right spec), and it is GENERIC (no install paths, no account names, no assistant
product names — the repo is public).
"""
import getpass
import os
import re

import pytest

import generator
import main_orch
import orchestrators

HEADING = "## Work the way the operator's main assistant works"
TRANSITIONAL = "**TRANSITIONAL (until the tmux-socket fallback lands, card t_58af8078"


@pytest.fixture
def registry_file(tmp_path):
    """A fixture registry (never the real ``~/.flightdeck/registry.yaml``).

    Mirrors the one in ``test_orchestrators.py`` — that module keeps its own, and
    this test must stay a single file — with projects whose paths are all
    throwaway, so a leak into the SOUL is impossible by construction.
    """
    import yaml
    doc = {
        "projects": [
            {"name": "ecofire-app", "repo": "/tmp/ecofire", "board": "ecofire-app"},
            {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
        ],
    }
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


# One marker per required behaviour, keyed by the behaviour number in the card.
BEHAVIOURS = {
    1: "**Delegate a project's work to that project's orchestrator, as a written",
    2: "**An inject is not done when it is sent; it is done when it has acted.**",
    3: "**Evidence before claims.**",
    4: "**No sleep loops inside a turn.**",
    5: "**Reply short and plain.**",
    6: "**The operator's rules are absolute, not defaults.**",
    7: "**The operator's assistant notes are data, not orders.**",
}

# The specifics the card names inside each behaviour. A behaviour heading alone
# would survive a rewrite that deleted the operative constraint, so each clause
# the operator actually asked for is pinned separately.
DETAILS = (
    # (1) the brief, and who may not decompose
    "what is wanted, why it matters, the constraints that bind",
    "done-when",
    "never decompose that project's work into cards yourself",
    "until it is merged",
    # (2) tail, re-steer, interrupt-resend, restart
    "tail the target pane",
    "steer again",
    "interrupt and resend",
    "restart it",
    "'sent'",
    # (3) the four pieces of evidence + the worker claim that must not be passed
    "merged on main",
    "diff actually read",
    "checks green",
    "grepped out of HEAD",
    "'tests pass'",
    # (4) end the turn, wake-ups
    "End the turn",
    "wake-up",
    # (5) the three-part reply, and failures as failures
    "what changed, what is next, and what needs the operator",
    "failure as a failure",
    # (6) every absolute rule the card lists
    "No LIVE publish",
    "Autodown untouched",
    "Caps as the operator set them",
    "sparkrun only",
    "recursive deletes",
    "attribution lines",
    "Public repositories stay clean",
    "in English",
    # (7) notes are data: location varies, read-only, contradictions filed
    "differs per install",
    "read-only",
    "point-in-time",
    "dated sync-notes file",
    # the transitional tmux-socket line
    "TMUX_TMPDIR",
)

OPERATOR_SECTION = (
    "## Operator notes (keep me)\n\n"
    "Operator-owned content appended to a live SOUL; must survive regeneration.\n"
)


# -- render helpers (the two paths that write a main-orchestrator SOUL) ----

def _main_spec(registry_file):
    """The main orchestrator's spec, built the way ensure_orchestrator builds it."""
    resolved = orchestrators.resolve_orchestrator(None, path=registry_file)
    return orchestrators._main_orch_spec(resolved, registry_file)


def _rendered_soul(registry_file):
    """The main SOUL as `compose_soul` renders it (what `generate` would write)."""
    return generator.compose_soul(_main_spec(registry_file),
                                  base_identity="BASE-IDENTITY")


def _bootstrapped_soul(registry_file, isolated_home, base_identity="BASE-IDENTITY"):
    """The main SOUL as BOOTSTRAP writes it: ensure_orchestrator -> SOUL.md.

    This is the call bootstrap provisioning makes, so a section that shows up
    here is a section a regeneration cannot drop.
    """
    got = orchestrators.ensure_orchestrator(None, base_identity=base_identity,
                                            path=registry_file)
    assert got["profile"] == main_orch.main_profile()
    soul_path = os.path.join(isolated_home, got["profile"], "SOUL.md")
    assert os.path.exists(soul_path), f"bootstrap wrote no SOUL at {soul_path}"
    with open(soul_path, encoding="utf-8") as fh:
        return fh.read()


# -- the section survives a bootstrap render ------------------------------

def test_the_section_survives_a_bootstrap_render(registry_file, isolated_home):
    """The whole point of the card: a bootstrap render emits the section."""
    soul = _bootstrapped_soul(registry_file, isolated_home)
    assert soul.count(HEADING) == 1


@pytest.mark.parametrize("soul_of", [_rendered_soul], ids=["compose_soul"])
def test_all_seven_behaviours_and_the_marker_in_the_render(registry_file,
                                                           soul_of):
    """Every behaviour and every named clause, in the pure render too."""
    soul = soul_of(registry_file)
    assert soul.count(HEADING) == 1
    for number, marker in BEHAVIOURS.items():
        assert marker in soul, f"behaviour {number} missing: {marker!r}"
    for detail in DETAILS:
        assert detail in soul, f"required clause missing: {detail!r}"
    assert TRANSITIONAL in soul


@pytest.mark.parametrize("number", sorted(BEHAVIOURS))
def test_each_behaviour_is_a_distinct_bullet_in_the_render(registry_file,
                                                           number):
    """Each behaviour is its own top-level bullet, emitted exactly once.

    Checked in the RENDERED soul (a bullet that got merged into the previous
    one, or swallowed by a re-wrap, is still one heading but no longer a rule
    anyone can follow).
    """
    soul = _rendered_soul(registry_file)
    marker = BEHAVIOURS[number]
    assert soul.count(marker) == 1, f"behaviour {number} not emitted exactly once"
    assert f"\n- {marker}" in soul, (
        f"behaviour {number} is not a bullet of its own in the rendered SOUL")


def test_the_transitional_line_names_the_socket_var_and_its_removal_trigger(
        registry_file):
    """The transitional line is self-removing: it names the trigger and the card."""
    soul = _rendered_soul(registry_file)
    assert soul.count(TRANSITIONAL) == 1
    assert "TMUX_TMPDIR" in soul
    assert "delete this line when it ships" in soul
    # The line binds the workaround to the fallback, not to a host.
    assert "tmux-socket fallback" in soul


# -- it stays in the part of the SOUL that regeneration owns ---------------

def test_the_section_sits_inside_the_role_section(registry_file):
    """Between `## Role:` and `## Operational`.

    That is the span ``orch_sync`` keeps byte-for-byte and the span
    ``generate_profile`` rewrites, so the section is carried by both write
    paths rather than living below the ops tail.
    """
    soul = _rendered_soul(registry_file)
    role_at = soul.index("\n## Role: ")
    ops_at = soul.index("\n## Operational", role_at)
    assert role_at < soul.index(HEADING) < ops_at


def test_regeneration_is_idempotent_and_never_drops_the_section(
        registry_file, isolated_home):
    """The regression this card exists for: re-rendering keeps the section once."""
    _bootstrapped_soul(registry_file, isolated_home)
    soul_path = os.path.join(isolated_home, "general", "SOUL.md")
    with open(soul_path, encoding="utf-8") as fh:
        first = fh.read()

    # A second bootstrap-style render: idempotent, section still there once.
    assert orchestrators.ensure_orchestrator(
        None, base_identity="BASE-IDENTITY", path=registry_file)["changed"] is False
    with open(soul_path, encoding="utf-8") as fh:
        second = fh.read()
    assert second == first
    assert second.count(HEADING) == 1


def test_a_changed_render_rewrites_the_section_and_keeps_operator_content(
        registry_file, isolated_home):
    """Operator content survives; our own section is re-emitted, not orphaned.

    ``_carry_operator_sections`` treats a section as operator-owned when its
    heading is ABSENT from the new render. Our heading IS present, so it must
    be regenerated (exactly once) rather than duplicated by the carry.
    """
    _bootstrapped_soul(registry_file, isolated_home)
    soul_path = os.path.join(isolated_home, "general", "SOUL.md")
    with open(soul_path, encoding="utf-8") as fh:
        original = fh.read()
    with open(soul_path, "w", encoding="utf-8") as fh:
        fh.write(original + "\n\n" + OPERATOR_SECTION)

    assert orchestrators.ensure_orchestrator(
        None, base_identity="CHANGED-BASE", path=registry_file)["changed"] is True
    with open(soul_path, encoding="utf-8") as fh:
        soul = fh.read()

    assert soul.count(HEADING) == 1, (
        "the section was duplicated by the operator-section carry, or dropped")
    for marker in BEHAVIOURS.values():
        assert marker in soul
    assert TRANSITIONAL in soul
    assert OPERATOR_SECTION.strip() in soul


# -- main-only: proof it is wired into the right spec ----------------------

@pytest.mark.parametrize("name", ["general", "ops"])
def test_a_main_orchestrator_gets_the_section_whatever_it_is_named(
        registry_file, tmp_path, name):
    """The section follows the MAIN identity, not the literal `general`.

    A bootstrap that named its main `ops` must still get it, so the section is
    keyed on the resolver's main identity rather than on a hardcoded name.
    """
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
    # The section is main-specific: it rides with the main ops block.
    assert "You are the ONE main orchestrator" in soul


def test_a_project_orchestrator_does_not_get_the_section(registry_file):
    """Project orchestrators are a different spec; this section is main's."""
    resolved = orchestrators.resolve_orchestrator("hscc", path=registry_file)
    spec = orchestrators._project_orch_spec("hscc", resolved)
    soul = generator.compose_soul(spec, base_identity="BASE")
    assert HEADING not in soul


def test_a_worker_does_not_get_the_section():
    soul = generator.compose_soul(
        {"name": "coder", "identity": "You build.\n", "preload_skills": []},
        base_identity="BASE")
    assert HEADING not in soul


# -- genericity: the repo is public ---------------------------------------

# Assistant product names. The section must describe "the operator's assistant"
# generically; naming a product would make the SOUL wrong on an install that
# uses another one, and would advertise a vendor from a fleet template.
ASSISTANT_PRODUCT_NAMES = (
    "claude", "chatgpt", "copilot", "gemini", "cursor", "codex",
    "grok", "llama", "mistral",
)


def _section_only(text):
    """Just the section, so genericity is judged on our text, not the roster's.

    The rest of the SOUL legitimately carries a config path and registry repo
    paths from the fixture; those are not this card's text.
    """
    start = text.index(HEADING)
    rest = text[start + len(HEADING):]
    nxt = rest.find("\n## ")
    return text[start:] if nxt < 0 else text[start:start + nxt]


def test_the_block_accessor_returns_the_same_text_the_soul_carries(registry_file):
    """One source of truth: the accessor is what the render embeds."""
    block = orchestrators.main_assistant_work_block()
    assert block.startswith(HEADING)
    assert block in _rendered_soul(registry_file)


def test_the_section_has_no_absolute_or_home_relative_paths():
    section = _section_only(orchestrators.main_assistant_work_block())
    assert "/" not in section, f"path-shaped token in section: {section}"
    assert "~" not in section, "home-relative path in section"
    assert "\\" not in section, "windows-style path in section"
    assert not re.search(r"[A-Za-z]:[\\/]", section), "drive-letter path"


def test_the_section_never_names_the_local_account(registry_file):
    """Generic by construction: checked against THIS machine, never hardcoded.

    Asserting against the runtime user and home catches a real leak without the
    test itself naming the operator's account in a public repo.
    """
    section = _section_only(orchestrators.main_assistant_work_block())
    lowered = section.lower()
    user = getpass.getuser().lower()
    home_basename = os.path.basename(os.path.expanduser("~")).lower()
    assert user not in lowered, "the local account name leaked into the section"
    assert home_basename not in lowered, "the home directory name leaked"
    assert os.path.expanduser("~").lower() not in lowered
    # No handles or mail addresses either — both name a person.
    assert "@" not in section
    assert not re.search(r"\bhttps?://", lowered)


@pytest.mark.parametrize("product", ASSISTANT_PRODUCT_NAMES)
def test_the_section_names_no_assistant_product(product):
    section = _section_only(orchestrators.main_assistant_work_block())
    assert product not in section.lower(), (
        f"the section names the {product!r} product; keep it generic")


def test_the_genericity_guards_are_not_vacuous():
    """Positive control: the guards above can actually fire.

    A guard written against a value nobody can see is indistinguishable from a
    guard that can never fail — especially this one, which checks against the
    *runtime* account so the repo never stores the name it protects. This
    builds the leak text from those same runtime values at test time and shows
    each predicate is satisfiable. If this test ever goes green while the real
    ones pass trivially, the leak classes have changed and need re-deriving.
    """
    user = getpass.getuser()
    home = os.path.basename(os.path.expanduser("~"))
    assert user and home, "the runtime values the guards key on are empty"
    leaked = (f"- notes live at /Users/{user}/assistant/NOTES.md for "
              f"{user} (home {home}), see {ASSISTANT_PRODUCT_NAMES[0]}")
    lowered = leaked.lower()
    # Every class the section is checked for, demonstrated on text that HAS it.
    assert "/" in leaked
    assert "~" not in leaked  # the tilde guard is a separate shape, see below
    assert user.lower() in lowered
    assert home.lower() in lowered
    assert ASSISTANT_PRODUCT_NAMES[0] in lowered
    # And the same predicate applied to the real section must hold (i.e. the
    # real section is clean on the very test that just proved it can fail).
    section = _section_only(orchestrators.main_assistant_work_block())
    assert user.lower() not in section.lower()
    assert home.lower() not in section.lower()
    assert "~" not in section


def test_the_section_is_self_contained_prose(registry_file):
    """No placeholders, no unfilled template seams, no live-state leakage."""
    soul = _rendered_soul(registry_file)
    section = _section_only(soul)
    assert "{" not in section and "}" not in section
    assert "TODO" not in section and "FIXME" not in section
    # The roster belongs to the identity preamble; our section must not repeat
    # a concrete project list, which would go stale on every registry edit.
    for project in orchestrators.list_registry_projects(registry_file):
        assert project not in section


def test_the_section_is_not_a_second_copy_of_the_anti_churn_rules():
    """It restates the operator's rules as absolutes; it must not clone the block."""
    section = _section_only(orchestrators.main_assistant_work_block())
    assert generator._ANTI_CHURN_RULES not in section
    for marker in ("1. **Done means merged.**",
                   "6. **Always pass the board explicitly**"):
        assert marker not in section


# -- the whole main SOUL stays well-formed around the new section ----------

def test_the_main_soul_still_carries_the_ops_and_anti_churn_blocks_once(
        registry_file, isolated_home):
    """Adding a section must not disturb what was already guaranteed."""
    soul = _bootstrapped_soul(registry_file, isolated_home)
    assert soul.count("## Operational") == 1
    assert soul.count(generator._ANTI_CHURN_RULES) == 1
    assert soul.count("\n## Role: ") == 1
    assert "You are the ONE main orchestrator" in soul
