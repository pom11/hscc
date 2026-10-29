"""Reasoning tiers: role spec field, generator policy merge, reasoning-sync.

Offline only — every profile tree / policy file lives under tmp_path.
"""

import json
import os

import pytest
import yaml

import generator
import reasoning_sync
import rolelib

POLICY = {"max": "xhigh", "standard": "medium", "light": "low"}


def _cfg(pdir, data):
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def _load(pdir):
    return yaml.safe_load((pdir / "config.yaml").read_text())


def test_spec_reasoning_tier_validated(tmp_path):
    base = {"name": "x", "identity": "i", "routing_description": "r"}
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump({**base, "reasoning_tier": "MAX"}))
    assert rolelib.load_spec(str(ok))["reasoning_tier"] == "max"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({**base, "reasoning_tier": "xhigh"}))   # an effort, not a tier
    with pytest.raises(ValueError, match="reasoning_tier"):
        rolelib.load_spec(str(bad))
    none = tmp_path / "none.yaml"
    none.write_text(yaml.safe_dump(base))
    assert rolelib.load_spec(str(none))["reasoning_tier"] is None


def test_tier_defaults():
    assert generator.reasoning_tier("sphoin-orch") == "max"
    assert generator.reasoning_tier("orchestrator") == "max"
    assert generator.reasoning_tier("backend-engineer") == "standard"
    assert generator.reasoning_tier({"name": "reviewer", "reasoning_tier": "max"}) == "max"
    assert generator.reasoning_tier({"name": "coder", "reasoning_tier": None}) == "standard"


def test_load_policy_ignores_junk(tmp_path):
    p = tmp_path / "policy.json"
    assert generator.load_reasoning_policy(str(p)) == {}
    p.write_text("not json")
    assert generator.load_reasoning_policy(str(p)) == {}
    p.write_text(json.dumps({"tiers": {"max": "xhigh", "bogus": "x", "light": "", "standard": 3}}))
    assert generator.load_reasoning_policy(str(p)) == {"max": "xhigh"}


def test_reasoning_blocks_merge_and_leave_undeclared_tiers():
    agent, deleg = generator.reasoning_blocks("standard", POLICY, {"max_turns": 9}, {"max_iterations": 5})
    assert agent == {"max_turns": 9, "reasoning_effort": "medium"}
    assert deleg == {"max_iterations": 5, "reasoning_effort": "low"}
    agent, deleg = generator.reasoning_blocks("max", {"standard": "medium"},
                                              {"reasoning_effort": "keep"}, {})
    assert agent == {"reasoning_effort": "keep"} and deleg == {}


def test_sync_sets_only_the_two_keys_and_is_idempotent(tmp_path):
    root = tmp_path / "profiles"
    _cfg(root / "backend-engineer", {"model": {"default": "worker-model"}, "agent": {"max_turns": 9}})
    _cfg(root / "demo-orch", {"toolsets": ["kanban"]})
    _cfg(root / "reviewer", {})
    (root / "no-config").mkdir()
    reps = reasoning_sync.sync_all(str(root), POLICY, tiers={"reviewer": "max"})
    by = {r["profile"]: r for r in reps}
    assert set(by) == {"backend-engineer", "demo-orch", "reviewer"}
    be = _load(root / "backend-engineer")
    assert be["model"] == {"default": "worker-model"}
    assert be["agent"] == {"max_turns": 9, "reasoning_effort": "medium"}
    assert be["delegation"] == {"reasoning_effort": "low"}
    assert _load(root / "demo-orch")["agent"]["reasoning_effort"] == "xhigh"
    assert _load(root / "demo-orch")["toolsets"] == ["kanban"]
    assert by["reviewer"]["tier"] == "max"
    mtime = os.path.getmtime(root / "backend-engineer" / "config.yaml")
    again = reasoning_sync.sync_all(str(root), POLICY, tiers={"reviewer": "max"})
    assert all(not r["changed"] for r in again)
    assert os.path.getmtime(root / "backend-engineer" / "config.yaml") == mtime


def test_sync_without_policy_touches_nothing(tmp_path):
    root = tmp_path / "profiles"
    _cfg(root / "coder", {"agent": {"reasoning_effort": "xhigh"}})
    assert reasoning_sync.sync_all(str(root), {}) == []
    assert _load(root / "coder")["agent"]["reasoning_effort"] == "xhigh"


def test_sync_dry_run_and_bad_config(tmp_path):
    root = tmp_path / "profiles"
    _cfg(root / "coder", {"agent": {}})
    (root / "broken").mkdir()
    (root / "broken" / "config.yaml").write_text("- just\n- a list\n")
    reps = {r["profile"]: r for r in reasoning_sync.sync_all(str(root), POLICY, tiers={}, dry_run=True)}
    assert reps["coder"]["changed"] and _load(root / "coder") == {"agent": {}}
    assert reps["broken"]["status"].startswith("skipped")


def test_generate_profile_writes_policy_efforts(tmp_path, monkeypatch):
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps({"template": "t", "tiers": POLICY}))
    monkeypatch.setattr(rolelib, "REASONING_POLICY_PATH", str(pol))
    assert generator.load_reasoning_policy() == POLICY
