import copy
import json
import os
import re
import yaml
import pytest
import rolelib
import generator


def test_compose_soul_has_all_three_layers():
    spec = {"name": "reviewer", "identity": "You review code.\n", "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE-CHAR-MARKER")
    # Layer 1 base
    assert "BASE-CHAR-MARKER" in soul
    # Layer 2 role
    assert "You review code." in soul
    # Layer 3 operational (thin: mentions role + worktree/kanban)
    assert "reviewer" in soul
    assert "worktree" in soul.lower() or "kanban" in soul.lower()


def test_compose_soul_orchestrator_skips_worker_ops():
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n", "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE")
    # Orchestrator is not a kanban worker — must NOT claim to run in a worktree.
    assert "your own git worktree" not in soul.lower()
    # ...but it MUST get its own gateway/authority operational block.
    assert "gateway node" in soul.lower()


def test_project_orch_work_rules_carry_the_orch_message_nudge_rule():
    """t_1d284d82: `orch_message` is a NUDGE channel, not a work order.

    The rule has to live INSIDE the generated "## How you manage work" list (an
    orch reads that list as its work-management contract), so it is asserted on
    the text that follows the heading — not merely somewhere in the SOUL.
    """
    spec = {"name": "demo-orch", "identity": "You orchestrate demo.\n",
            "preload_skills": []}
    soul = generator.compose_soul(spec, base_identity="BASE")
    assert "## How you manage work" in soul
    section = soul.split("## How you manage work", 1)[1]
    assert "`orch_message`" in section
    assert "11. **`orch_message` is a nudge, not a work order.**" in section
    assert "never by commenting on DONE cards" in section
    assert "on a CARD on the right board (`--board` explicit)" in section
    # Workers cannot send orch_message at all (the tool refuses a non-orch
    # sender), so the rule belongs to the orchestrator block only.
    worker = generator.compose_soul(
        {"name": "coder", "identity": "You build.\n", "preload_skills": []},
        base_identity="BASE")
    assert "orch_message" not in worker


# --- t_88ec1cb2: rule 5 routes merge ownership through the card -------------
#
# Rule 5 used to read "You review and merge your board's PRs." — an
# unconditional claim. On a board whose finish rail has the WORKER merge its
# own PR into the integration branch once its gate is green, with review
# happening once, on the branch, that sentence tells the orchestrator to redo
# work the worker already did: duplicated review, a second merge attempt, or a
# wait for a merge that never arrives. The fix keeps the orchestrator as the
# DEFAULT and makes the card's `HANDOFF` the authority, so no board name has
# to be baked into text every orchestrator shares.

_PROJECT_AND_BOARD_NAMES = (
    "pontaje", "gateclerk", "ecofire", "flightdeck", "hscc", "ios-app",
    "grid", "kronos", "pickolo", "flosana", "efsdriver", "micro-notify",
)


def _work_rule(number):
    """One numbered rule of the generated 'How you manage work' list."""
    soul = generator.compose_soul(
        {"name": "demo-orch", "identity": "You orchestrate demo.\n",
         "preload_skills": []},
        base_identity="BASE")
    assert "## How you manage work" in soul
    text = soul.split("## How you manage work", 1)[1]
    tail = text[text.index(f"{number}. **"):]
    nxt = re.search(r"\n(?=\d+\. \*\*)", tail)
    return tail if nxt is None else tail[: nxt.start()]


def test_rule5_makes_the_card_handoff_the_merge_authority():
    rule = _work_rule(5)
    assert "`HANDOFF`" in rule
    # An orchestrator reading it cold is told WHERE the answer lives.
    assert "card body" in rule
    # Both override shapes a real rail uses are named, explicitly as overrides.
    assert "worker merging its own PR" in rule
    assert "integration branch" in rule
    assert "overrides the default" in rule
    # The three failure modes the old sentence produced are now forbidden.
    for phrase in ("review the same diff twice", "merge it a second time",
                   "never assigned to you"):
        assert phrase in rule
    # The review half of the old rule survived the rewrite.
    assert "0 errors" in rule and "all green" in rule


def test_rule5_keeps_orchestrator_review_and_merge_as_the_default():
    """The default is retained — but stated ONLY as a default.

    The old bug was not that the orchestrator was the default owner, it was
    that the default was stated unconditionally, so a card with a different
    owner read as a contradiction. Every sentence here that hands the merge to
    the orchestrator has to mark itself the default.
    """
    rule = _work_rule(5)
    assert "Default when the card says nothing" in rule
    said_default = False
    for sentence in re.split(r"(?<=[.!?])\s+", rule):
        if "review and merge" in sentence:
            said_default = True
            assert "default" in sentence.lower(), sentence
    assert said_default, "the default merge policy is no longer stated at all"


def test_rule5_names_no_project_or_board():
    """The block is shared by every orchestrator on the fleet."""
    rule = _work_rule(5)
    for name in _PROJECT_AND_BOARD_NAMES:
        pattern = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
        assert re.search(pattern, rule, re.I) is None, f"rule 5 names {name!r}"


def test_merge_ownership_rule_stays_out_of_worker_souls():
    """Merge ownership is an orchestration policy; workers get the HANDOFF
    through their card, not through their identity."""
    worker = generator.compose_soul(
        {"name": "coder", "identity": "You build.\n", "preload_skills": []},
        base_identity="BASE")
    assert "HANDOFF" not in worker
    assert "review and merge" not in worker


def test_card_budget_rules_reach_workers_and_orchs():
    """Operator 2026-11-01: cards ran 6h+ because workers ground on at 100-200K
    context (93-100% of wall time inside model calls). Workers carry the
    hand-off rule; project orchs carry the sizing budget and act on handoffs.
    """
    worker = generator.compose_soul(
        {"name": "coder", "identity": "You build.\n", "preload_skills": []},
        base_identity="BASE")
    assert "## Stay small — commit slices, hand off early" in worker
    rules = worker.split("## Stay small", 1)[1]
    assert "`[budget]` note" in rules and "about 40 tool calls" in rules
    assert "DONE (the shas), LEFT (file:line" in rules
    assert "wip: ..." in rules

    orch = generator.compose_soul(
        {"name": "demo-orch", "identity": "You orchestrate demo.\n",
         "preload_skills": []},
        base_identity="BASE")
    section = orch.split("## How you manage work", 1)[1]
    assert "about 40 model calls" in section and "~120K tokens" in section
    assert "**Handoffs are normal.**" in section
    assert "**Rebase-only cards get one attempt.**" in section
    # The orchestrator is not a kanban worker: no worker hand-off section.
    assert "## Stay small" not in orch


def test_generate_profile_writes_files(tmp_path, monkeypatch):
    # PROFILES_DIR covers the fallback path; HERMES_HOME covers the native API
    # path (create_profile reads HERMES_HOME, not PROFILES_DIR), so the test is
    # isolated in BOTH modes and never touches the real ~/.hermes/profiles.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": ["test-driven-development"]}
    changed = generator.generate_profile(spec, base_identity="BASE")
    pdir = os.path.join(str(tmp_path / "profiles"), "coder")
    assert os.path.isdir(pdir)
    assert os.path.exists(os.path.join(pdir, "SOUL.md"))
    with open(os.path.join(pdir, "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert "hscc-cluster" not in cfg["toolsets"]
    assert "hermes-cli" in cfg["toolsets"]
    with open(os.path.join(pdir, "profile.yaml")) as f:
        prof = yaml.safe_load(f)
    assert prof["description_auto"] is False
    assert changed is True  # first write reports changed


def test_generate_profile_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n", "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False  # unchanged content → no rewrite


# -- SOUL regeneration keeps operator-owned trailing sections (t_8695aa1a) --

def test_regen_preserves_operator_soul_section(tmp_path, monkeypatch):
    """An operator-appended `## ` section survives a regeneration."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "demo-orch", "identity": "You orchestrate demo.\n",
            "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    soul_path = tmp_path / "profiles" / "demo-orch" / "SOUL.md"
    operator = ("## OpenCode routing (operator 2026-11-05)\n\n"
                "Keep this operator block.\n")
    soul_path.write_text(soul_path.read_text() + "\n\n" + operator)

    changed = generator.generate_profile(spec, base_identity="BASE")

    text = soul_path.read_text()
    assert changed is True
    assert operator.strip() in text
    assert text.count("## OpenCode routing (operator 2026-11-05)") == 1


def test_regen_operator_section_is_idempotent(tmp_path, monkeypatch):
    """Two regenerations with an operator section yield the same bytes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "demo-orch", "identity": "You orchestrate demo.\n",
            "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    soul_path = tmp_path / "profiles" / "demo-orch" / "SOUL.md"
    soul_path.write_text(
        soul_path.read_text()
        + "\n\n## OpenCode routing (operator 2026-11-05)\n\nKeep this.\n")
    generator.generate_profile(spec, base_identity="BASE")
    first = soul_path.read_text()

    assert generator.generate_profile(spec, base_identity="BASE") is False
    assert soul_path.read_text() == first
    assert first.count("## OpenCode routing (operator 2026-11-05)") == 1


def test_regen_updates_generated_section_not_duplicated(tmp_path, monkeypatch):
    """A stale copy of a GENERATED section is replaced, never appended twice."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n", "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    soul_path = tmp_path / "profiles" / "coder" / "SOUL.md"
    stale = soul_path.read_text().replace(
        "## Operational\n\n", "## Operational\n\nSTALE GENERATED BODY.\n\n", 1)
    assert "STALE GENERATED BODY." in stale
    soul_path.write_text(stale)

    generator.generate_profile(spec, base_identity="BASE")

    text = soul_path.read_text()
    assert "STALE GENERATED BODY." not in text
    assert text.count("## Operational") == 1


def test_regen_without_operator_sections_is_byte_identical(tmp_path, monkeypatch):
    """No operator sections -> exactly today's compose_soul() output."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n", "preload_skills": []}
    generator.generate_profile(spec, base_identity="BASE")
    soul_path = tmp_path / "profiles" / "coder" / "SOUL.md"
    assert soul_path.read_text() == generator.compose_soul(spec, "BASE")


# -- orchestrator cluster-control toolsets (t_ef4a6c1d) --
#
# An orchestrator is supposed to have FULL control — its profiles must carry the
# cluster tools (hscc-cluster, sparkrun), while worker roles keep the restricted
# set. Regeneration must only ADD missing entries, never clobber an operator's
# hand-edited per-profile toolsets.


def test_cluster_orchestrator_gets_cluster_toolsets(tmp_path, monkeypatch):
    """The cluster-wide `orchestrator` profile gets hscc-cluster + sparkrun."""
    cfg = _gen_config(tmp_path, monkeypatch, {
        "name": "orchestrator", "identity": "You orchestrate.\n",
        "preload_skills": [], "model_tier": "strong"})
    ts = cfg["toolsets"]
    assert "hscc-cluster" in ts
    assert "sparkrun" in ts
    assert "kanban" in ts and "delegation" in ts  # subagent/task work already present
    # still has its strong gateway model block unchanged
    assert cfg["model"]["base_url"] == generator.STRONG_URL


def test_project_orch_gets_cluster_toolsets(tmp_path, monkeypatch):
    """A <project>-orch profile gets hscc-cluster + sparkrun too."""
    cfg = _gen_config(tmp_path, monkeypatch, {
        "name": "ecofire-app-orch", "identity": "You orch.\n",
        "preload_skills": [], "model_tier": "strong"})
    ts = cfg["toolsets"]
    assert "hscc-cluster" in ts
    assert "sparkrun" in ts


def test_worker_still_excludes_cluster_toolsets(tmp_path, monkeypatch):
    """A worker role must NOT get cluster control — the restriction is
    deliberate and must not widen. The existing worker toolset is unchanged."""
    cfg = _gen_config(tmp_path, monkeypatch, {
        "name": "coder", "identity": "You build.\n",
        "preload_skills": [], "model_tier": "fast"})
    ts = cfg["toolsets"]
    assert "hscc-cluster" not in ts
    assert "sparkrun" not in ts
    expected_worker = generator.rolelib.role_toolsets()
    assert ts == expected_worker  # byte-identical to the worker default


def test_regen_orch_only_adds_missing_not_clobber_hand_edits(tmp_path, monkeypatch):
    """Regenerating an orchestrator preserves an operator's hand-added toolsets
    and only ADDS the newly-required cluster entries (never removes/drops)."""
    spec = {"name": "ecofire-app-orch", "identity": "You orch.\n",
            "preload_skills": [], "model_tier": "strong"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "ecofire-app-orch"))
    # Operator hand-adds a toolset to the generated profile.
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["toolsets"].append("operator-custom-tool")
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)
    # Regenerate — the hand-added toolset must survive, cluster tools present,
    # and nothing dropped.
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    ts2 = cfg2["toolsets"]
    assert "operator-custom-tool" in ts2          # hand-edit preserved
    assert "hscc-cluster" in ts2 and "sparkrun" in ts2  # cluster tools present
    expected = generator.rolelib.orchestrator_toolsets()
    for t in expected:
        assert t in ts2                           # nothing lost
    # Dedup: regenerating again with the same hand-edit present is byte-stable.
    cfg3 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg3["toolsets"] == ts2                # idempotent, no dup cluster tools


def test_regen_idempotent_with_cluster_toolsets(tmp_path, monkeypatch):
    """A regenerated orchestrator profile reports changed=False on the next run —
    the merged config is byte-stable so a no-op regeneration never rewrites."""
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n",
            "preload_skills": [], "model_tier": "strong"}
    _gen_config(tmp_path, monkeypatch, spec)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False


import subprocess
import sys


def test_cli_generate_all_runs(tmp_path):
    """End-to-end: `hscc.py generate` builds all 24 role profiles into a temp home."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    plugin_dir = os.path.dirname(os.path.abspath(generator.__file__))
    py = sys.executable  # use system python, works in sandboxed tests
    result = subprocess.run(
        [py, os.path.join(plugin_dir, "hscc.py"), "generate"],
        capture_output=True, text=True, env=env, cwd=plugin_dir,
    )
    assert result.returncode == 0, result.stderr
    for role in ("orchestrator", "architect", "coder", "reviewer", "qa"):
        assert os.path.exists(os.path.join(str(tmp_path), "profiles", role, "SOUL.md"))
    # Verify routing_description landed as description in profile.yaml
    import yaml
    coder_profile = os.path.join(str(tmp_path), "profiles", "coder", "profile.yaml")
    with open(coder_profile) as f:
        pdata = yaml.safe_load(f)
    assert "routing_description" in pdata or "description" in pdata
    # The decomposer-facing description IS the routing_description
    assert "Claim tasks" in str(pdata.get("description", ""))


# -- model_tier generator tests --


def test_model_tier_fast_uses_worker_proxy(tmp_path, monkeypatch):
    """Fast-tier (default) roles use the worker proxy endpoint."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://localhost:4000/v1"
    assert cfg["model"]["default"] == "worker-model"


def test_model_tier_strong_uses_orch_endpoint(tmp_path, monkeypatch):
    """Strong-tier roles use the orchestrator endpoint, not the worker proxy.

    Scoped to a PINNED orchestrator deliberately. Under `uniform_pool` the
    strong tier IS the worker proxy by design — every unit serves the
    orchestrator alias — so the "never the proxy" guard only holds here, and
    the pooled case is covered by
    ``test_live_orchestrator_endpoint_uses_pool_when_uniform``.

    ``STRONG_URL`` is pinned explicitly rather than read from the module
    constant, which is computed at import from the OPERATOR's live
    serving.json: on a pooled fleet that constant legitimately equals the
    proxy, so the old assertion failed depending on whose machine ran the
    suite. The scrub guard that this test originally carried now lives in the
    two ``_live_orchestrator_endpoint`` tests, which assert the derivation from
    serving.json directly instead of comparing two values the scrub rewrote
    together.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(generator, "STRONG_URL", "http://10.9.9.7:8123/v1")
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == generator.STRONG_URL
    assert cfg["model"]["base_url"] != generator.WORKER_PROXY_BASE_URL
    assert cfg["model"]["default"] == "orchestrator-model"


def test_live_orchestrator_endpoint_read_from_serving(tmp_path, monkeypatch):
    """The endpoint is derived from serving.json, mirroring orchestrator_head:
    the head is nodes[0] of the first orchestrator unit."""
    serving = tmp_path / "serving.json"
    serving.write_text(json.dumps([
        {"role": "worker", "nodes": ["10.9.9.1", "10.9.9.2"], "port": 8000},
        {"role": "orchestrator", "nodes": ["10.9.9.7", "10.9.9.8"], "port": 8123},
    ]))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(serving) if p.endswith("serving.json") else p)
    assert generator._live_orchestrator_endpoint() == "http://10.9.9.7:8123/v1"


def test_live_orchestrator_endpoint_absent_is_none(tmp_path, monkeypatch):
    """No serving.json -> None, so the caller falls back rather than crashing."""
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(tmp_path / "nope.json") if p.endswith("serving.json") else p)
    assert generator._live_orchestrator_endpoint() is None


def test_model_tier_override_via_env(tmp_path, monkeypatch):
    """HSCC_STRONG_URL env var overrides the strong-tier endpoint."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(generator, "STRONG_URL", "http://custom:9999/v1")
    monkeypatch.setattr(generator, "STRONG_MODEL", "custom/model")
    spec = {"name": "designer", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "designer"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://custom:9999/v1"
    assert cfg["model"]["default"] == "custom/model"


def test_model_tier_strong_idempotent(tmp_path, monkeypatch):
    """Generating a strong-tier role twice reports changed=False on second run."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False


def test_model_tier_strong_still_has_compaction(tmp_path, monkeypatch):
    """Strong-tier roles still route compaction to the orchestrator."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    # Compaction auxiliary should still be present
    assert "auxiliary" in cfg
    assert "compression" in cfg["auxiliary"]


def test_short_desc_identity_no_double_period():
    """_short_desc_identity should not produce '..' when identity sentence already ends with '.'."""
    spec = {"name": "reviewer", "identity": "You review code.\n", "preload_skills": []}
    desc = generator._short_desc_identity(spec)
    assert not desc.endswith(".."), f"got: {desc!r}"
    assert desc == "You review code."


def test_write_if_changed_handles_non_utf8(tmp_path):
    """_write_if_changed should overwrite a non-UTF-8 file without raising."""
    path = tmp_path / "test.txt"
    path.write_bytes(b"\xff\xfe garbage binary \xff")
    content = "valid utf-8 content"
    changed = generator._write_if_changed(str(path), content)
    assert changed is True
    assert path.read_text() == content


# -- model_endpoint / model_name generator tests --


def test_model_endpoint_and_name_in_config(tmp_path, monkeypatch):
    """a) spec with model_endpoint+model_name -> config.yaml uses that base_url + model."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1",
            "model_name": "Qwen/Code-32B"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://coding:5000/v1"
    assert cfg["model"]["default"] == "Qwen/Code-32B"


def test_model_endpoint_only_falls_back_to_tier_model(tmp_path, monkeypatch):
    """b) spec with model_endpoint only -> uses that base_url, model falls back to tier default."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://coding:5000/v1"
    assert cfg["model"]["default"] == "worker-model"


def test_model_endpoint_only_strong_falls_back_to_strong_model(tmp_path, monkeypatch):
    """b) spec with model_endpoint only on strong tier -> falls back to strong model."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong",
            "model_endpoint": "http://arch:6000/v1"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://arch:6000/v1"
    assert cfg["model"]["default"] == "orchestrator-model"


def test_no_override_uses_tier_logic(tmp_path, monkeypatch):
    """c) spec WITHOUT override -> identical to current tier behaviour."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["base_url"] == "http://localhost:4000/v1"
    assert cfg["model"]["default"] == "worker-model"


def test_model_endpoint_idempotent(tmp_path, monkeypatch):
    """e) generation stays idempotent with an override present."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1",
            "model_name": "Qwen/Code-32B"}
    generator.generate_profile(spec, base_identity="BASE")
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False


def test_model_endpoint_still_has_compaction(tmp_path, monkeypatch):
    """model_endpoint override does not remove compaction config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast",
            "model_endpoint": "http://coding:5000/v1",
            "model_name": "Qwen/Code-32B"}
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert "auxiliary" in cfg
    assert "compression" in cfg["auxiliary"]


# -- logical-alias model defaults (v1.5.1) --
#
# Constants above evaluate at import time, so these tests reload the module
# under a controlled HSCC_*_MODEL environment to assert both the alias defaults
# and the env-override path. Each reload test restores clean alias defaults at
# the end so the rest of the suite stays deterministic.
import importlib


_MODEL_ENV_VARS = ("HSCC_WORKER_MODEL", "HSCC_STRONG_MODEL", "HSCC_COMPACT_MODEL")


def _clear_model_env(monkeypatch):
    """Drop all model-alias env vars so constants evaluate to their defaults."""
    for var in _MODEL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _reload_with_model_env(monkeypatch, overrides):
    """Set env overrides (monkeypatched), reload generator so constants recompute."""
    for var, val in overrides.items():
        monkeypatch.setenv(var, val)
    return importlib.reload(generator)


def _restore_model_defaults(monkeypatch):
    """Clear env overrides and reload so the module is back to alias defaults."""
    _clear_model_env(monkeypatch)
    importlib.reload(generator)


def test_model_defaults_are_aliases_without_env(monkeypatch):
    """With no HSCC_*_MODEL env set, every model default is a logical alias."""
    _clear_model_env(monkeypatch)
    mod = importlib.reload(generator)
    assert mod.WORKER_MODEL == "worker-model"
    assert mod.STRONG_MODEL == "orchestrator-model"
    assert mod.COMPACT_MODEL == "orchestrator-model"


def test_worker_profile_model_and_compaction_are_aliases(tmp_path, monkeypatch):
    """A generated worker (fast) profile uses the worker-model alias for its
    model block and the orchestrator-model alias for compaction."""
    _clear_model_env(monkeypatch)
    mod = importlib.reload(generator)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    mod.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "coder"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["default"] == mod.WORKER_MODEL == "worker-model"
    assert cfg["auxiliary"]["compression"]["model"] == \
        mod.COMPACT_MODEL == "orchestrator-model"


def test_strong_profile_model_is_alias(tmp_path, monkeypatch):
    """A generated strong-tier profile uses the orchestrator-model alias."""
    _clear_model_env(monkeypatch)
    mod = importlib.reload(generator)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(mod.rolelib, "PROFILES_DIR", str(tmp_path / "profiles"))
    spec = {"name": "architect", "identity": "You design.\n",
            "preload_skills": [], "model_tier": "strong"}
    mod.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / "architect"), "config.yaml")) as f:
        cfg = yaml.safe_load(f)
    assert cfg["model"]["default"] == mod.STRONG_MODEL == "orchestrator-model"


def test_env_override_strong_model_forces_concrete(monkeypatch):
    """HSCC_STRONG_MODEL env override forces a concrete id; worker alias stays."""
    mod = _reload_with_model_env(
        monkeypatch, {"HSCC_STRONG_MODEL": "deepseek-ai/DeepSeek-V4-Flash-0731"})
    assert mod.STRONG_MODEL == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert mod.WORKER_MODEL == "worker-model"  # untouched remains alias
    _restore_model_defaults(monkeypatch)


def test_env_override_worker_model_forces_concrete(monkeypatch):
    """HSCC_WORKER_MODEL env override forces a concrete id; strong alias stays."""
    mod = _reload_with_model_env(
        monkeypatch, {"HSCC_WORKER_MODEL": "Qwen/Qwen3.6-27B-FP8"})
    assert mod.WORKER_MODEL == "Qwen/Qwen3.6-27B-FP8"
    assert mod.STRONG_MODEL == "orchestrator-model"  # untouched remains alias
    _restore_model_defaults(monkeypatch)


# -- compression.threshold_tokens regression (t_f2c2dbb5) --
#
# Bootstrap regenerating a profile must NOT drop the operator/API-set
# ``compression.threshold_tokens``. The 2026-08-27 incident: the generator
# wrote the compression block as a fresh dict and nulled this key on every
# profile, wedging sessions at the 196608 ratio floor. These tests pin the
# merge behaviour. They all run against an isolated fake profile root
# (tmp_path + monkeypatched HERMES_HOME / PROFILES_DIR) — never
# ~/.hermes/profiles.


def _gen_config(tmp_path, monkeypatch, spec):
    """Generate a profile and return its config.yaml dict (isolated root)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    generator.generate_profile(spec, base_identity="BASE")
    with open(os.path.join(str(tmp_path / "profiles" / spec["name"]),
                           "config.yaml")) as f:
        return yaml.safe_load(f)


def test_new_profile_has_threshold_tokens(tmp_path, monkeypatch):
    """A brand-new worker profile is generated WITH threshold_tokens set."""
    cfg = _gen_config(tmp_path, monkeypatch, {
        "name": "coder", "identity": "You build.\n",
        "preload_skills": [], "model_tier": "fast"})
    comp = cfg["compression"]
    assert comp["threshold"] == generator.COMPACT_THRESHOLD
    assert comp["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS == 200000


def test_regen_keeps_the_current_threshold_tokens(tmp_path, monkeypatch):
    """Regenerating must not null the token cap (the 2026-08-27 regression)."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    cfg = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS
    # Regenerate — must NOT null the token cap.
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS


def test_legacy_cap_migrates_to_the_current_constant():
    """A cap this generator wrote in an earlier version must be RAISED.

    100000 on disk is our own old default, not an operator choice, and it is
    indistinguishable from one. Without the `_LEGACY_COMPACTION_CAPS` carve-out
    the "never raise a lower cap" rule pins every already-generated profile at
    the old value forever, so raising the constant reaches only NEW profiles —
    which is exactly how the live fleet ended up with ~39 profiles compacting
    at 38% of the window.
    """
    for legacy in generator._LEGACY_COMPACTION_CAPS:
        out = generator._compression_block({"threshold_tokens": legacy})
        assert out["threshold_tokens"] == \
            generator.SESSION_COMPACTION_THRESHOLD_TOKENS, \
            f"legacy cap {legacy} must migrate, not persist"


def test_regen_preserves_lower_operator_threshold_tokens(tmp_path, monkeypatch):
    """A LOWER operator value (<= the constant) survives a regeneration — raising
    it would clobber a deliberate, stricter cap."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    # Simulate an operator lowering the token cap to 50000.
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["compression"]["threshold_tokens"] = 50000
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)
    # Regenerate; the lower operator value must be preserved.
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["threshold_tokens"] == 50000


def test_regen_preserves_other_compression_keys(tmp_path, monkeypatch):
    """OTHER keys in an existing compression block are not dropped by the merge."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["compression"]["operator_key"] = "keep-me"
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["operator_key"] == "keep-me"
    assert cfg2["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS


def test_project_orch_gets_threshold_tokens(tmp_path, monkeypatch):
    """A <project>-orch profile is emitted WITH threshold_tokens too — these
    are the 11 idle profiles bootstrap nulled on 2026-08-27 (they previously
    got NO compression block at all)."""
    spec = {"name": "ecofire-app-orch", "identity": "You orch.\n",
            "preload_skills": [], "model_tier": "strong"}
    cfg = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg["model"]["base_url"]  # still has its strong model block
    comp = cfg["compression"]
    assert comp["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS
    # Regeneration keeps it (not nulled).
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS


def test_project_orch_preserves_operator_threshold_tokens(tmp_path, monkeypatch):
    """A project-orch profile's LOWER operator threshold_tokens survives a
    regeneration (same merge rule as worker roles)."""
    spec = {"name": "ecofire-app-orch", "identity": "You orch.\n",
            "preload_skills": [], "model_tier": "strong"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "ecofire-app-orch"))
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["compression"]["threshold_tokens"] = 40000
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["threshold_tokens"] == 40000


# -- per-profile memory block (t_e287cfb6 / ROADMAP profile-provisioning item 1) --
#
# Bootstrap/generator never owned per-profile `memory:` — a regeneration
# rewrote config.yaml WITHOUT the block, silently dropping a hand-set
# memory_char_limit / provider back to Hermes' built-in default. These tests
# pin the merge (operator-choices-survive): a fresh profile gets
# memori_byodb + 4000, a second run is a no-op, and a hand-set value survives
# regeneration. All hermetic — tmp_path + monkeypatched HERMES_HOME /
# PROFILES_DIR, never ~/.hermes/profiles.

def _mem_config(tmp_path, monkeypatch, spec):
    """Generate a profile and return its config.yaml memory dict."""
    return _gen_config(tmp_path, monkeypatch, spec)["memory"]


def test_fresh_profile_gets_default_memory_block(tmp_path, monkeypatch):
    """A brand-new worker profile is generated WITH a memory block:
    provider=memori_byodb, memory_char_limit=4000, memory_enabled=true."""
    mem = _mem_config(tmp_path, monkeypatch, {
        "name": "coder", "identity": "You build.\n",
        "preload_skills": [], "model_tier": "fast"})
    assert mem["memory_enabled"] is True
    assert mem["memory_char_limit"] == \
        generator.PROFILE_MEMORY_CHAR_LIMIT == 4000
    assert mem["provider"] == \
        generator.PROFILE_MEMORY_PROVIDER == "memori_byodb"


def test_every_profile_kind_gets_memory_block(tmp_path, monkeypatch):
    """memory: is emitted for EVERY role kind — orchestrator, project-orch,
    worker, strong-tier — not just the worker path."""
    for spec in (
        {"name": "orchestrator", "identity": "You orchestrate.\n",
         "preload_skills": [], "model_tier": "strong"},
        {"name": "ecofire-app-orch", "identity": "You orch.\n",
         "preload_skills": [], "model_tier": "strong"},
        {"name": "architect", "identity": "You design.\n",
         "preload_skills": [], "model_tier": "strong"},
        {"name": "coder", "identity": "You build.\n",
         "preload_skills": [], "model_tier": "fast"},
    ):
        mem = _mem_config(tmp_path, monkeypatch, spec)
        assert mem["memory_char_limit"] == \
            generator.PROFILE_MEMORY_CHAR_LIMIT
        assert mem["provider"] == generator.PROFILE_MEMORY_PROVIDER


def test_regen_memory_is_idempotent_noop(tmp_path, monkeypatch):
    """A SECOND generate_profile run is byte-identical (no-op): the emitted
    memory block is stable, so changed does not flip on regeneration."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    changed_second = generator.generate_profile(spec, base_identity="BASE")
    assert changed_second is False


def test_hand_set_memory_char_limit_survives_regen(tmp_path, monkeypatch):
    """THE operator property: a pre-set memory_char_limit: 6000 survives a
    regeneration — the block keeps 6000, never reverting to the default."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    cfg = _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    current = yaml.safe_load(open(os.path.join(pdir, "config.yaml")))
    current["memory"]["memory_char_limit"] = 6000
    # Also let the operator set a user_char_limit — must be preserved verbatim.
    current["memory"]["user_char_limit"] = 2000
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(current, f, sort_keys=False)
    mem2 = _mem_config(tmp_path, monkeypatch, spec)
    assert mem2["memory_char_limit"] == 6000          # hand-set survives
    assert mem2["provider"] == "memori_byodb"         # default provider still filled
    assert mem2["user_char_limit"] == 2000            # other key preserved verbatim


def test_hand_set_provider_survives_regen(tmp_path, monkeypatch):
    """A hand-set provider string (different from the default) survives a
    regeneration — never force memori_byodb over an operator choice."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    current = yaml.safe_load(open(os.path.join(pdir, "config.yaml")))
    current["memory"]["provider"] = "operator-chosen-provider"
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(current, f, sort_keys=False)
    mem2 = _mem_config(tmp_path, monkeypatch, spec)
    assert mem2["provider"] == "operator-chosen-provider"
    assert mem2["memory_char_limit"] == \
        generator.PROFILE_MEMORY_CHAR_LIMIT


def test_memory_block_preserves_lower_operator_limit_never_lowers(tmp_path, monkeypatch):
    """A LOWER operator memory_char_limit (< 4000, e.g. 2500) is RAISED toward
    the default but never lowered — the default is the floor. And a HIGH
    operator value is never lowered. Both are operator-choices-survive."""
    # Direct unit: 6000 stays 6000 (never lowered to 4000).
    out = generator._memory_block({"memory_char_limit": 6000})
    assert out["memory_char_limit"] == 6000
    assert out["provider"] == "memori_byodb"
    # Direct unit: a low operator value is raised to the floor.
    out2 = generator._memory_block({"memory_char_limit": 2500})
    assert out2["memory_char_limit"] == \
        generator.PROFILE_MEMORY_CHAR_LIMIT
    # Direct unit: an operator who disabled memory keeps it disabled.
    out3 = generator._memory_block({"memory_enabled": False})
    assert out3["memory_enabled"] is False
    assert out3["memory_char_limit"] == \
        generator.PROFILE_MEMORY_CHAR_LIMIT


# -- cluster-wide `orchestrator` regression (t_1d7c9c34) --
#
# The v1.14.2 fix (t_f2c2dbb5) covered worker roles and <project>-orch profiles
# but MISSED the cluster-wide `orchestrator`. Because `_is_orchestrator(name)`
# is True but `_is_project_orchestrator(name)` is False, its branch set NO
# compression block, so a regeneration rewrote config.yaml WITHOUT one — then
# enable_plugins._ensure_compaction re-created a threshold-only block and
# threshold_tokens came back NULL (2026-08-28 incident). A test that only
# asserted on the worker/project-orch or only on `threshold` would have passed
# through BOTH incidents. These tests pin the orchestrator SPECIFICALLY. They
# run against an isolated fake profile root (tmp_path + monkeypatched
# HERMES_HOME / PROFILES_DIR) — never ~/.hermes/profiles.


def test_cluster_orchestrator_gets_threshold_tokens(tmp_path, monkeypatch):
    """The cluster-wide `orchestrator` is generated WITH threshold_tokens —
    the one profile the t_f2c2dbb5 fix missed."""
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n",
            "preload_skills": [], "model_tier": "strong"}
    cfg = _gen_config(tmp_path, monkeypatch, spec)
    # Keeps its strong gateway-node model block.
    assert cfg["model"]["base_url"]
    comp = cfg["compression"]
    assert comp["threshold"] == generator.COMPACT_THRESHOLD
    assert comp["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS


def test_cluster_orchestrator_regen_keeps_threshold_tokens(tmp_path, monkeypatch):
    """THE regression for THIS incident: regenerating an `orchestrator` profile
    that already has threshold_tokens: 100000 must NOT null it. Before the fix
    the orchestrator branch emitted no compression block, so the value was
    dropped on every bootstrap."""
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n",
            "preload_skills": [], "model_tier": "strong"}
    _gen_config(tmp_path, monkeypatch, spec)
    # Regenerate — must NOT null the token cap.
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS
    assert cfg2["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS


def test_cluster_orchestrator_preserves_lower_operator_threshold_tokens(tmp_path, monkeypatch):
    """A LOWER operator threshold_tokens on the cluster-wide orchestrator
    survives a regeneration — never raise a deliberate stricter cap."""
    spec = {"name": "orchestrator", "identity": "You orchestrate.\n",
            "preload_skills": [], "model_tier": "strong"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "orchestrator"))
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["compression"]["threshold_tokens"] = 50000
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)
    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["compression"]["threshold_tokens"] == 50000


# -- every HSCC-owned worker profile has a role spec (t_5ca88538) --
#
# ios-engineer had NO roles/ios-engineer.yaml, so `hscc-roles generate` (which
# loops only over role specs) silently skipped it — every base-identity
# improvement had to be hand-patched into its live SOUL.md. This test catches
# the next hand-made profile: any profile dir under PROFILES_DIR whose SOUL
# carries the hscc-generated worker signature (base identity + `## Role:` + the
# worker operational block that mentions "your own git worktree") MUST have a
# matching roles/<name>.yaml spec. Per-project <project>-orch profiles are
# excluded deliberately — they are created dynamically by orchestrators.py, not
# from a roles/*.yaml spec, and carry the gateway operational block, not the
# worker one.


def _spec_names():
    """Set of role names that have a spec under roles/*.yaml."""
    names = set()
    for spec_path in rolelib.list_spec_files():
        spec = yaml.safe_load(open(spec_path)) or {}
        names.add(spec.get("name"))
    return names


def test_every_hscc_owned_worker_profile_has_role_spec():
    """A profile whose SOUL carries the hscc-generated worker signature MUST
    have a roles/<name>.yaml spec, so a hand-made profile is caught."""
    worker_marker = "your own git worktree"  # worker ops block; orchestrators lack it
    spec_names = _spec_names()
    missing = []
    # PROFILES_DIR may not exist (e.g. when HERMES_HOME points at a profile dir
    # rather than the global ~/.hermes); then there are no profiles to check and
    # the invariant holds vacuously — don't crash on listdir.
    if not os.path.isdir(rolelib.PROFILES_DIR):
        return
    for name in sorted(os.listdir(rolelib.PROFILES_DIR)):
        pdir = os.path.join(rolelib.PROFILES_DIR, name)
        if not os.path.isdir(pdir):
            continue
        soul = os.path.join(pdir, "SOUL.md")
        if not os.path.isfile(soul):
            continue
        text = open(soul, encoding="utf-8").read()
        if "## Role:" in text and worker_marker in text and name not in spec_names:
            missing.append(name)
    assert not missing, (
        "HSCC-owned worker profile(s) with no role spec (`hscc-roles generate` "
        "will skip them): " + ", ".join(missing)
    )


def test_live_orchestrator_endpoint_uses_pool_when_uniform(tmp_path, monkeypatch):
    """A uniform pool serves the orchestrator alias from EVERY unit, so the
    strong tier must address the worker proxy, not one node.

    Without this, `hscc-roles generate` (and therefore bootstrap) re-pins every
    *-orch profile to the orchestrator node on the next run, undoing what
    `template apply` did — all orchestrator work on one GPU while the rest idle,
    and every orchestrator profile dies with that single node.
    """
    serving = tmp_path / "serving.json"
    serving.write_text(json.dumps({
        "version": 2,
        "uniform_pool": True,
        "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.9.9.7"], "port": 8000},
            {"id": "w1", "role": "worker", "nodes": ["10.9.9.8"], "port": 8001},
        ],
    }))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(serving) if p.endswith("serving.json") else p)
    assert generator._live_orchestrator_endpoint() == generator.WORKER_PROXY_BASE_URL


def test_live_orchestrator_endpoint_still_pins_when_not_uniform(tmp_path, monkeypatch):
    """Omission preserves the pinned behaviour exactly — the role split is real
    on a non-uniform plan, where the orchestrator is one node."""
    serving = tmp_path / "serving.json"
    serving.write_text(json.dumps({
        "version": 2,
        "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ["10.9.9.7"], "port": 8123},
            {"id": "w1", "role": "worker", "nodes": ["10.9.9.8"], "port": 8000},
        ],
    }))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(serving) if p.endswith("serving.json") else p)
    assert generator._live_orchestrator_endpoint() == "http://10.9.9.7:8123/v1"


def test_live_orchestrator_endpoint_uniform_false_is_pinned(tmp_path, monkeypatch):
    """An explicit `uniform_pool: false` is not a pool."""
    serving = tmp_path / "serving.json"
    serving.write_text(json.dumps({
        "version": 2, "uniform_pool": False,
        "units": [{"id": "orch", "role": "orchestrator",
                   "nodes": ["10.9.9.7"], "port": 8123}],
    }))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(serving) if p.endswith("serving.json") else p)
    assert generator._live_orchestrator_endpoint() == "http://10.9.9.7:8123/v1"


# ── generate must be LOSSLESS for a spec-carried project block (t_0f2ee405) ──
#
# `ecofire-pontaje-engineer`'s live SOUL carried a hand-appended `## Project
# override: ecofire-pontaje` block. compose_soul() emits only base + spec identity
# + ops, so anything in that block that is NOT in the spec identity is deleted by
# the next `hscc.py generate` — silently, because the generator reports success.
# t_8d22ec81 folded the branch/ID/prefix/publish constraints into the spec
# identity; t_0f2ee405 measured (whole-document positional diff, all 25
# worker-signature profiles) that exactly ONE line was still lost: the pointer to
# the module's spec/plan docs. These tests pin the CONTENT of the block against
# the composed text of the REAL tracked spec.
#
# Token-level on purpose: compose_soul reflows the identity to its own line
# wrapping, so a line-level pin would fail on a rewrap that loses nothing. A
# token-level pin fails only when the information itself is gone — the exact
# regression class (spec text narrowed by an unrelated edit, generate then drops
# a constraint) this guards.
#
# Deliberately NO read of ~/.hermes/profiles here (PR #115's ruling): live state
# belongs to the doctor, not pytest — a live-reading test is red on the operator
# machine and vacates in CI. The spec is the tracked source of truth; the doctor
# is the live-state surface.

_PONTAJE_SPEC = os.path.join(rolelib.ROLES_DIR, "ecofire-pontaje-engineer.yaml")

# The constraints the live `## Project override:` block carried (measured, lines
# 130-133 of that SOUL), minus the named shared/prod sandboxes, which t_8d22ec81
# ruled must NOT be reproduced in-tree ("the constraint is preserved, the
# inventory is not" — changelog.d/t_8d22ec81.md).
_PONTAJE_LOSSLESS_TOKENS = (
    "feat/pontaje",      # branch: from AND into, never main
    "70600-70699",       # BC object ID range
    "Pontaje/",          # file prefix
    "POM_PONTAJE",       # the project's own sandbox
    "docs/superpowers",  # the module's spec/plan live here
    "2026-10-26-pontaje",
)


def test_compose_soul_pontaje_carries_every_project_override_token():
    """Nothing the hand-appended block carried is dropped by `generate`.

    If this fails, `hscc.py generate` would delete a live constraint or the doc
    pointer from the pontaje worker's SOUL — the failure the operator would only
    discover after the profile was re-stamped.
    """
    base = open(rolelib.BASE_IDENTITY_PATH, encoding="utf-8").read()
    soul = generator.compose_soul(rolelib.load_spec(_PONTAJE_SPEC), base)
    missing = [t for t in _PONTAJE_LOSSLESS_TOKENS if t not in soul]
    assert not missing, (
        "generate would DROP " + ", ".join(missing) + " from "
        "ecofire-pontaje-engineer's SOUL — the live profile carried these in a "
        "hand-appended `## Project override:` block that compose_soul() cannot "
        "re-emit; fold them back into the spec's `identity` (compose_soul emits "
        "it verbatim)."
    )


def test_compose_soul_pontaje_keeps_publish_constraint_without_naming_others():
    """The publish guardrail is a CONSTRAINT, not a sandbox INVENTORY.

    Two halves, both measured failures of a plausible 'fix':
      - drop the guardrail -> the worker may publish to a shared/prod sandbox;
      - 'restore' the live block verbatim -> the named shared/prod inventory
        lands in a PUBLIC repo (the leak class scripts/address_guard.py
        documents; no tracked file names them today).

    The guardrail is pinned as the CONTIGUOUS normalized phrase, not as the
    words 'shared'/'production': mutation M3 (guardrail sentence deleted from
    the spec) SURVIVED the first version of this test, because base-identity.md
    carries 'shows up in production' and the identity's own closing carries
    'shared values' — word-level assertions were fail-open. Whitespace
    normalization keeps it robust against compose_soul's reflow, which raw
    substring-on-source would not be.
    """
    base = open(rolelib.BASE_IDENTITY_PATH, encoding="utf-8").read()
    soul = generator.compose_soul(rolelib.load_spec(_PONTAJE_SPEC), base)
    flat = " ".join(soul.split())
    assert "POM_PONTAJE" in soul
    assert "never a shared or production sandbox" in flat, (
        "the publish guardrail (never a shared or production sandbox) was lost "
        "from the composed SOUL — generate would drop the constraint that keeps "
        "this worker off shared/prod BC sandboxes")


def test_role_specs_name_no_shared_sandbox_inventory():
    """Structural guard for 'constraint, not inventory' — no blocklist needed.

    A blocklist naming the forbidden sandboxes would itself reproduce the leak
    class it polices, so the rule is inverted: across every tracked role spec,
    the ONLY `POM_`-prefixed sandbox token allowed is the pontaje project's OWN
    sandbox. Any other sandbox name appearing in-tree fails here, whatever it
    is called, without this file ever naming it.
    """
    offenders = {}
    for spec_path in rolelib.list_spec_files():
        text = open(spec_path, encoding="utf-8").read()
        bad = {t for t in re.findall(r"POM_[A-Za-z0-9_]+", text)
               if t != "POM_PONTAJE"}
        if bad:
            offenders[os.path.basename(spec_path)] = sorted(bad)
    assert not offenders, (
        "role specs must carry the publish CONSTRAINT, never the sandbox "
        "INVENTORY (t_8d22ec81 ruling; the named shared/prod sandboxes are "
        "deliberately not reproduced in-tree — see changelog.d/t_8d22ec81.md "
        "and scripts/address_guard.py's leak-class docs): " + repr(offenders)
    )


# -- reviewer follow-up routing rule (t_58cd36e8) ---------------------------
#
# Operator standing rule: a reviewer that finds a follow-up comments it on the
# card it is reviewing and returns THAT card to `todo` — it never mints new
# cards. Decomposition is the orchestrator's job. The rule must therefore be
# part of the generated reviewer SOUL, and pinned in pytest exactly the way the
# pontaje override tokens are: asserted on the text `compose_soul()` emits from
# the tracked spec (the tracked source of truth), never on ~/.hermes/profiles
# (PR #115's ruling; live state belongs to the doctor).

_REVIEWER_SPEC = os.path.join(rolelib.ROLES_DIR, "reviewer.yaml")


def _reviewer_role_section():
    """The `## Role: reviewer` section of the SOUL `generate` would write,
    whitespace-normalized. Position-pinned on purpose: the rule is asserted
    inside the role identity, not merely 'somewhere in the SOUL' — an
    orchestrator-ops mention would not reach a worker reviewer as its contract.
    """
    base = open(rolelib.BASE_IDENTITY_PATH, encoding="utf-8").read()
    soul = generator.compose_soul(rolelib.load_spec(_REVIEWER_SPEC), base)
    start = soul.index("## Role: reviewer")
    end = soul.index("## Operational", start)
    return " ".join(soul[start:end].split())


def test_reviewer_soul_routes_followups_to_the_reviewed_card():
    """The positive half: comment on the reviewed card, return it to todo."""
    section = _reviewer_role_section()
    assert "Comment it on the card you are reviewing and return that card to " \
           "`todo`" in section, (
        "the reviewer SOUL no longer tells the reviewer where a follow-up "
        "goes — the operator rule (comment on the reviewed card + return it "
        "to todo) must live in roles/reviewer.yaml's `identity`")


def test_reviewer_soul_forbids_card_spawning_from_a_review():
    """The negative half, as a contiguous phrase (mutation M3's lesson:
    word-level assertions survive deleting the whole sentence, because
    base-identity.md already carries words like 'card' and 'kanban')."""
    section = _reviewer_role_section()
    assert "Spawning new cards from a review is forbidden" in section, (
        "the prohibition against kanban_create from a review was lost from "
        "the reviewer spec")
    assert "`kanban_create` is not a reviewer's tool" in section


# -- top-level onboarding.* preservation (t_7ef65bf3 / 12:24 fleet freeze) --
#
# `hscc bootstrap --yes` regenerates every profile config.yaml through
# generate_profile, which rebuilds the file from the role spec. Until the
# onboarding merge existed, the top-level `onboarding` block was NOT part of
# that rebuild, so a regeneration deleted `onboarding.seen.tool_progress_prompt`
# from all 47 profiles at once: every worker's first tool call re-fired the
# one-time progress prompt, hung at "preparing terminal", and the dispatcher
# stalled (operator restored 47 configs by hand). These tests pin the
# merge-never-drop contract. Hermetic — tmp_path + monkeypatched HERMES_HOME /
# PROFILES_DIR, never ~/.hermes/profiles.

_ONBOARDING_SEED = {
    "seen": {"tool_progress_prompt": True},
    "extra_custom": 7,
}


def test_regen_preserves_onboarding_block(tmp_path, monkeypatch):
    """THE incident property: an existing `onboarding.seen.*` flag AND an
    unrelated custom onboarding key both survive a regeneration verbatim."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    # Seed the block the fleet had on disk (what the regen deleted).
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["onboarding"] = copy.deepcopy(_ONBOARDING_SEED)
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)

    cfg2 = _gen_config(tmp_path, monkeypatch, spec)
    assert cfg2["onboarding"]["seen"]["tool_progress_prompt"] is True
    assert cfg2["onboarding"]["extra_custom"] == 7


def test_regen_onboarding_preserves_rest_of_config(tmp_path, monkeypatch):
    """Companion to the incident test: the merge is ADDITIVE ONLY — with an
    onboarding block on disk, every other generated key is byte-for-byte what
    a profile without one gets. (Deliberately does NOT assert the block's
    contents: that is `test_regen_preserves_onboarding_block`'s job, so a
    reverted merge fails that test alone and this one pins only the blast
    radius.)"""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    # Baseline: profile with NO onboarding block, regenerated twice.
    base = _gen_config(tmp_path, monkeypatch, spec)
    base_cfg = _gen_config(tmp_path, monkeypatch, spec)
    base_cfg.pop("onboarding", None)

    # Same profile, now carrying an onboarding block, regenerated.
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    with open(os.path.join(pdir, "config.yaml")) as f:
        cur = yaml.safe_load(f)
    cur["onboarding"] = copy.deepcopy(_ONBOARDING_SEED)
    with open(os.path.join(pdir, "config.yaml"), "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)
    with_onb = _gen_config(tmp_path, monkeypatch, spec)
    with_onb.pop("onboarding", None)

    assert with_onb == base_cfg, (
        "the onboarding merge changed generated keys outside `onboarding` — it "
        "must be additive only (compression/memory/model/toolsets untouched)")
    assert base == base_cfg  # regen on a fresh profile is byte-stable too


def test_fresh_profile_writes_no_onboarding_key(tmp_path, monkeypatch):
    """A profile with no config.yaml on disk behaves exactly as before the
    merge: the generator emits no `onboarding` key at all (it does not own
    that block, so it must not invent one)."""
    cfg = _gen_config(tmp_path, monkeypatch, {
        "name": "coder", "identity": "You build.\n",
        "preload_skills": [], "model_tier": "fast"})
    assert "onboarding" not in cfg


def test_onboarding_merge_is_deep_and_existing_wins():
    """Unit: the merge fills ONLY missing keys, at any depth, and never
    overwrites or deletes what is on disk — including a falsy operator value
    (False / 0), which a `if not existing`-style check would clobber."""
    out = generator._onboarding_block(
        {"seen": {"tool_progress_prompt": False}, "keep": None},
        defaults={"seen": {"tool_progress_prompt": True, "other_prompt": True},
                  "fresh": 1})
    assert out["seen"]["tool_progress_prompt"] is False  # existing wins, even falsy
    assert out["seen"]["other_prompt"] is True           # missing key filled
    assert out["keep"] is None                           # null survives, not dropped
    assert out["fresh"] == 1                             # top-level default added


def test_onboarding_merge_never_mutates_the_existing_block():
    """Unit: the caller's dict is not mutated AND not aliased (same contract as
    _compression_block / _memory_block)."""
    existing = copy.deepcopy(_ONBOARDING_SEED)
    out = generator._onboarding_block(existing, defaults={"seen": {"x": 1}, "d": 2})
    assert out is not None and existing == _ONBOARDING_SEED
    assert out is not existing
    assert out["seen"] is not existing["seen"]
    # A default for an ABSENT key must not make the merge share the caller's
    # nested dicts: without this the generator would hand back references into
    # the dict it read from disk, and a later config edit would write through.
    out2 = generator._onboarding_block(existing, defaults={"brand_new": 1})
    assert out2 is not None
    assert out2["seen"] is not existing["seen"]
    assert out2["seen"] is not out["seen"]


def test_onboarding_block_returns_none_when_there_is_nothing_to_write():
    """Unit: no existing block AND no defaults -> None, which is what keeps the
    fresh-profile output byte-identical to the pre-merge generator."""
    assert generator._onboarding_block(None) is None
    assert generator._onboarding_block({}) is None
    assert generator._ONBOARDING_DEFAULTS == {}  # generator owns no onboarding state


# ━━━ foreign keys survive the whole-file write (t_10ccaf80) ━━━━━━━━━━━━━━━━━
#
# Every merge above is PER BLOCK, so it only ever protected a key the generator
# already names. `generate_profile` then wrote a dict it had built from scratch,
# and any key outside that set was not in the file afterwards. Measured on the
# operator's pre-2.15.10-bootstrap snapshot (49 profiles): `_config_version`
# lost on 45, the whole `browser:` section lost on 25. A profile with no
# `_config_version` makes Hermes run its silent config migration on next start,
# so the loss was 45 unreviewed migrations, not cosmetics.
#
# These tests therefore assert on FOREIGN keys — keys HSCC has never heard of —
# not on a whitelist of the two that got noticed, because the next foreign key
# must not be lost the same way. All of them run against an isolated fake
# profile root (tmp_path + monkeypatched HERMES_HOME / PROFILES_DIR), never
# ~/.hermes/profiles, and none of them needs the network.

#: A config payload holding every shape of foreign key the failure had: a
#: foreign top-level scalar (`_config_version`), a foreign top-level SECTION
#: (`browser`, the one that vanished whole), a foreign sub-key under a section
#: the generator DOES emit (`browser`-shaped failures inside `auxiliary`,
#: `memory`, `model`, `skills`), and a foreign tree nested four deep.
_FOREIGN_KEYS = {
    "_config_version": 52,
    "browser": {"executable_path": "/Applications/Google Chrome.app/"
                                         "Contents/MacOS/Google Chrome",
                "headless": False},
    "operator_nest": {"a": {"b": {"c": {"keep": "four deep"}}},
                      "list": [{"x": 1}, {"y": [2, 3]}]},
}

#: Foreign sub-keys living INSIDE sections the generator emits and rewrites.
_FOREIGN_SUBKEYS = {
    "auxiliary": {"goal_judge": {"model": "operator-picked"}},
    "memory": {"user_char_limit": 999},
    "model": {"request_timeout": 90},
    "skills": {"operator_flag": True},
}


def _read_raw(pdir):
    with open(os.path.join(pdir, "config.yaml"), encoding="utf-8") as f:
        return f.read()


def _write_raw(pdir, text):
    with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(text)


def _all_paths(node, prefix=()):
    """Every dotted key path in a nested mapping, at every depth."""
    out = set()
    if isinstance(node, dict):
        for k, v in node.items():
            p = prefix + (str(k),)
            out.add(".".join(p))
            out |= _all_paths(v, p)
    return out


def _value_at(node, dotted):
    cur = node
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def _assert_survives(before, after, label=""):
    """Every key path in *before* exists in *after* with an equal value.

    Type-strict, because `True == 1` in Python and a `true` that round-trips as
    `1` is a real change for every YAML reader downstream.
    """
    for path in _all_paths(before):
        assert path in _all_paths(after), f"{label}lost key {path!r}"
        want, got = _value_at(before, path), _value_at(after, path)
        assert got == want and type(got) is type(want), (
            f"{label}{path!r} changed value: {want!r} ({type(want).__name__}) "
            f"-> {got!r} ({type(got).__name__})")


def _regen(tmp_path, monkeypatch, spec):
    """Re-run the generator over an existing profile; return (cfg, raw, changed)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(generator.rolelib, "PROFILES_DIR",
                        str(tmp_path / "profiles"))
    pdir = os.path.join(str(tmp_path / "profiles" / spec["name"]))
    before = _read_raw(pdir)
    changed = generator.generate_profile(spec, base_identity="BASE")
    after = _read_raw(pdir)
    return yaml.safe_load(after), after, changed, before


def test_regen_preserves_foreign_top_level_keys_and_sections(tmp_path, monkeypatch):
    """The card's headline case: a foreign top-level key and a foreign whole
    section must survive a regeneration untouched."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    cur = yaml.safe_load(_read_raw(pdir))
    cur.update(_FOREIGN_KEYS)
    _write_raw(pdir, yaml.safe_dump(cur, sort_keys=False))

    cfg, _, _, _ = _regen(tmp_path, monkeypatch, spec)
    assert cfg["_config_version"] == 52, "lost _config_version -> silent migration"
    assert cfg["browser"] == _FOREIGN_KEYS["browser"], "lost the whole browser:"
    assert cfg["operator_nest"] == _FOREIGN_KEYS["operator_nest"]
    # and the generator still did its own job
    assert cfg["compression"]["threshold_tokens"] == \
        generator.SESSION_COMPACTION_THRESHOLD_TOKENS


def test_regen_preserves_foreign_subkeys_under_generated_sections(
        tmp_path, monkeypatch):
    """The shape that actually failed: a section the generator DOES emit also
    holds operator-owned content. `auxiliary:` carried the generator's
    `compression:` AND the operator's own sub-key — the merge must recurse into
    sections the generator owns instead of replacing them."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    cur = yaml.safe_load(_read_raw(pdir))
    for key, extra in _FOREIGN_SUBKEYS.items():
        cur.setdefault(key, {})
        cur[key].update(extra)
    _write_raw(pdir, yaml.safe_dump(cur, sort_keys=False))

    cfg, _, _, _ = _regen(tmp_path, monkeypatch, spec)
    for key, extra in _FOREIGN_SUBKEYS.items():
        for sub, want in extra.items():
            got = cfg[key][sub]
            assert got == want and type(got) is type(want), (
                f"{key}.{sub} lost or altered: {want!r} -> {got!r}")
    # the generator-owned siblings are still the generator's
    assert cfg["auxiliary"]["compression"]["model"] == generator.COMPACT_MODEL
    assert cfg["memory"]["memory_enabled"] is True


def test_regen_preserves_every_foreign_path_byte_for_value(tmp_path, monkeypatch):
    """Whole-tree assertion, not a named-key whitelist: walk the pre-regeneration
    document and require every path — generator-owned or not — to come back with
    the same value AND the same type. Falsy operator values (False / 0 / "" /
    None) and bool-vs-int are the cases a `if not existing` merge gets wrong."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    seed = {
        "_config_version": 52,
        "browser": {"executable_path": "/Applications/Google Chrome.app/x",
                    "headless": False, "timeout": 0},
        "operator_falsy": {"zero": 0, "no": False, "empty": "", "null": None,
                           "list": []},
        "operator_types": {"int": 1, "bool": True, "float": 1.5,
                           "unicode": "čeština — 日本語", "yes_str": "yes",
                           "block": "line one\nline two\n",
                           "nested": [{"k": [1, {"deep": True}]}]},
    }
    cur = yaml.safe_load(_read_raw(pdir))
    before = dict(cur)
    before.update(seed)
    _write_raw(pdir, yaml.safe_dump(before, sort_keys=False, allow_unicode=True))

    cfg, _, _, _ = _regen(tmp_path, monkeypatch, spec)
    _assert_survives(before, cfg)


def test_regen_keeps_the_operators_key_order(tmp_path, monkeypatch):
    """Key ORDER belongs to the file, not to the generator: a regeneration that
    reshuffles an organised config is a diff the operator never asked for. A key
    the generator newly emits is appended, never inserted."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    cur = yaml.safe_load(_read_raw(pdir))
    ordered = {"browser": {"executable_path": "/x"}, "_config_version": 52}
    # Operator puts their own keys FIRST, generator keys after them.
    merged = dict(ordered)
    merged.update(cur)
    _write_raw(pdir, yaml.safe_dump(merged, sort_keys=False))

    cfg, _, _, _ = _regen(tmp_path, monkeypatch, spec)
    keys = list(cfg.keys())
    assert keys[:2] == ["browser", "_config_version"], (
        f"generator reordered the file: {keys}")
    assert set(merged) <= set(keys), "a key left the document"


def test_regen_is_a_byte_identical_no_op_on_a_real_generated_profile(
        tmp_path, monkeypatch):
    """Style must not churn either. The generator's writer used to re-indent
    every block sequence hermes had written (`  - item` -> `- item`), so even a
    regeneration with nothing to change rewrote the whole file: 1 of the
    operator's 49 real configs came back byte-identical. Two runs in a row now
    have to leave the bytes — and the mtime — alone."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    first = _read_raw(pdir)
    assert "  - " in first, "expected the hermes-house indented sequence style"

    cfg, second, changed, _ = _regen(tmp_path, monkeypatch, spec)
    assert second == first, "a no-op regeneration restyled / rewrote the file"
    assert changed is False
    # "leaves the mtime alone" is a claim, so it is an assertion: the merge is
    # skipped before the write, not written and then found identical.
    before_ns = os.stat(os.path.join(pdir, "config.yaml")).st_mtime_ns
    _regen(tmp_path, monkeypatch, spec)
    assert os.stat(os.path.join(pdir, "config.yaml")).st_mtime_ns == before_ns, \
        "a no-op regeneration rewrote the file"


def test_regen_does_not_restyle_a_column_zero_config(tmp_path, monkeypatch):
    """The other house style is equally the operator's: a file written with
    column-0 sequences (`- item`) must come back with column-0 sequences.
    Forced onto the fallback writer so this pins the sniffer, not ruamel."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    cur = yaml.safe_load(_read_raw(pdir))
    cur.update(_FOREIGN_KEYS)
    _write_raw(pdir, yaml.safe_dump(cur, sort_keys=False))
    monkeypatch.setattr(generator, "_hermes_atomic_config_write", lambda: None)

    cfg, after, _, before = _regen(tmp_path, monkeypatch, spec)
    # The style of the FIRST block sequence is what the sniffer decides, and a
    # NESTED sequence is indented in both houses, so the assertion is on the
    # style itself (a nested `- 2` under `y:` is not a restyle).
    assert generator._sniff_sequence_indent(before) is False
    assert generator._sniff_sequence_indent(after) is False, \
        "fallback writer restyled a column-0 file"
    assert after == before, "no-op regeneration still rewrote the file"
    _assert_survives(cur, cfg, label="fallback writer: ")


def test_fallback_writer_preserves_foreign_keys_without_hermes(tmp_path, monkeypatch):
    """The key-preservation property must not DEPEND on hermes being importable:
    the thin interpreter in scripts/run_tests.sh has no hermes and no ruamel, so
    the local writer carries the same contract on the same merged payload."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    cur = yaml.safe_load(_read_raw(pdir))
    cur.update(_FOREIGN_KEYS)
    for key, extra in _FOREIGN_SUBKEYS.items():
        cur.setdefault(key, {}).update(extra)
    _write_raw(pdir, yaml.safe_dump(cur, sort_keys=False))
    monkeypatch.setattr(generator, "_hermes_atomic_config_write", lambda: None)

    cfg, _, _, _ = _regen(tmp_path, monkeypatch, spec)
    _assert_survives(cur, cfg, label="fallback writer: ")


def _hermes_writer_available():
    return generator._hermes_atomic_config_write() is not None


@pytest.mark.skipif(not _hermes_writer_available(),
                    reason="hermes' round-trip writer is not importable here")
def test_operator_comments_survive_a_real_change(tmp_path, monkeypatch):
    """Where hermes writes, its round-trip writer keeps what PyYAML cannot: the
    operator's own comments. This is the reason the write is delegated instead
    of hand-rolled, so it is pinned rather than assumed."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    text = _read_raw(pdir)
    _write_raw(pdir, text.replace(
        "toolsets:", "# my own note about this file\ntoolsets:", 1))
    assert "# my own note" in _read_raw(pdir)

    _, after, _, _ = _regen(tmp_path, monkeypatch, spec)
    assert "# my own note about this file" in after, \
        "the writer destroyed an operator comment"


@pytest.mark.skipif(not _hermes_writer_available(),
                    reason="hermes' round-trip writer is not importable here")
def test_a_write_that_would_lose_keys_fails_loudly(tmp_path, monkeypatch):
    """Fail CLOSED, and check the payload at the writer boundary.

    Hermes' writer refuses a payload that drops a mapping path; swallowing that
    refusal into the fallback writer would put the silent wipe back, so it has to
    escape `generate_profile` and leave the file alone. The same probe also
    asserts what the writer is actually handed: the merged document, foreign keys
    included — that is the fix, seen at the seam rather than through the file.
    """
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    cur = yaml.safe_load(_read_raw(pdir))
    cur.update(_FOREIGN_KEYS)
    # Something the generator MUST change, or the write is skipped as a no-op
    # (see test_no_op_regeneration_never_reaches_the_writer) and there is no
    # payload left to inspect.
    cur["compression"] = {"threshold": 0.01, "threshold_tokens": 5}
    _write_raw(pdir, yaml.safe_dump(cur, sort_keys=False))
    before = _read_raw(pdir)

    captured = {}

    def _refusing_writer(path, data):
        # Stands in for hermes_cli.config.atomic_config_write's refusal.
        captured["data"] = data
        raise ValueError("omitted config paths: browser, _config_version")

    monkeypatch.setattr(generator, "_hermes_atomic_config_write",
                        lambda: _refusing_writer)
    with pytest.raises(ValueError):
        generator.generate_profile(spec, base_identity="BASE")
    assert _read_raw(pdir) == before, "the file was replaced despite the refusal"
    payload = captured["data"]
    assert payload["browser"] == _FOREIGN_KEYS["browser"]
    assert payload["_config_version"] == 52
    assert payload["operator_nest"] == _FOREIGN_KEYS["operator_nest"]
    # ...while the generator still owns the value it came to change.
    assert payload["compression"]["threshold"] == generator.COMPACT_THRESHOLD
    assert payload["compression"]["threshold_tokens"] == 5  # operator's lower cap


@pytest.mark.skipif(not _hermes_writer_available(),
                    reason="hermes' round-trip writer is not importable here")
def test_no_op_regeneration_never_reaches_the_writer(tmp_path, monkeypatch):
    """Nothing to change = no write at all, so nothing can be lost even in
    principle. Measured here rather than assumed, because a writer that is only
    reached on a real change is exactly what makes the fail-closed refusal safe
    to leave un-caught."""
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    calls = []
    monkeypatch.setattr(generator, "_hermes_atomic_config_write",
                        lambda: (lambda path, data: calls.append(path)))
    _regen(tmp_path, monkeypatch, spec)
    assert calls == [], "a no-op regeneration went to the writer anyway"


def test_unparseable_config_is_not_silently_replaced(tmp_path, monkeypatch):
    """A corrupt config.yaml is recoverable operator data. The per-block readers
    all yield None for it (so the generator would rebuild from defaults), and the
    old hand-written dump happily overwrote the file. The delegated writer reads
    it first and refuses, so the operator keeps what they had."""
    if not _hermes_writer_available():
        pytest.skip("the refusal lives in hermes' writer")
    spec = {"name": "coder", "identity": "You build.\n",
            "preload_skills": [], "model_tier": "fast"}
    _gen_config(tmp_path, monkeypatch, spec)
    pdir = os.path.join(str(tmp_path / "profiles" / "coder"))
    corrupt = "toolsets: [hermes-cli\n  broken: : :\n\t- tabbed\n"
    _write_raw(pdir, corrupt)

    with pytest.raises(Exception):
        generator.generate_profile(spec, base_identity="BASE")
    assert _read_raw(pdir) == corrupt, "a corrupt config was silently overwritten"


# -- merge_preserving: the unit behind the write path --

def test_merge_preserving_existing_owns_order_and_foreign_keys():
    out = generator.merge_preserving(
        {"z": 1, "shared": {"keep": "me"}, "a": 2},
        {"a": 3, "shared": {"new": "yours"}, "b": 4})
    assert list(out) == ["z", "shared", "a", "b"], "existing order, new keys appended"
    assert out["z"] == 1 and out["shared"]["keep"] == "me"
    assert out["a"] == 3 and out["shared"]["new"] == "yours"


def test_merge_preserving_recurses_to_any_depth_and_replaces_leaves():
    existing = {"deep": {"l1": {"l2": {"l3": {"mine": True, "theirs": "old"}}}}}
    out = generator.merge_preserving(
        existing, {"deep": {"l1": {"l2": {"l3": {"theirs": "new"}}},
                            "other": 1}})
    assert out["deep"]["l1"]["l2"]["l3"] == {"mine": True, "theirs": "new"}
    # a list or scalar from the generator replaces: the block builders already
    # merged the existing value into their own output, see the docstring.
    out2 = generator.merge_preserving({"toolsets": ["a", "operator-added"]},
                                      {"toolsets": ["a", "operator-added", "b"]})
    assert out2["toolsets"] == ["a", "operator-added", "b"]


def test_merge_preserving_never_mutates_or_aliases_either_side():
    """Same contract as _compression_block / _memory_block / _onboarding_block:
    the dict read off disk must stay a read-only snapshot."""
    existing = {"browser": {"executable_path": "/x"}, "n": {"deep": [1, 2]}}
    generated = {"memory": {"provider": "memori_byodb"}}
    snapshot = copy.deepcopy(existing)
    out = generator.merge_preserving(existing, generated)
    assert existing == snapshot and generated == {"memory": {"provider": "memori_byodb"}}
    assert out is not existing
    assert out["browser"] is not existing["browser"]
    assert out["n"]["deep"] is not existing["n"]["deep"]
    out["browser"]["executable_path"] = "/mutated"
    out["n"]["deep"].append(3)
    assert existing == snapshot, "caller's dict was written through"


def test_merge_preserving_tolerates_no_existing_document():
    """A fresh profile has nothing to preserve: the generated payload comes back
    as-is, so a brand-new profile is unchanged by this fix."""
    gen = {"toolsets": ["a"], "model": {"default": "m"}}
    assert generator.merge_preserving(None, gen) == gen
    assert generator.merge_preserving({}, gen) == gen
    assert generator.merge_preserving({"x": 1}, None) == {"x": 1}


def test_sniff_sequence_indent_ignores_block_scalars_and_inline_values():
    """The sniffer decides the fallback writer's style; prose inside a block
    scalar that happens to start with `- ` must not decide it, and a key with an
    inline value must not either."""
    assert generator._sniff_sequence_indent("a:\n  - one\n") is True
    assert generator._sniff_sequence_indent("a:\n- one\n") is False
    assert generator._sniff_sequence_indent(
        "note: |\n  - not a sequence\na:\n- one\n") is False
    assert generator._sniff_sequence_indent("a: 1\nb: 2\n") is True  # default
    assert generator._sniff_sequence_indent(None) is True


