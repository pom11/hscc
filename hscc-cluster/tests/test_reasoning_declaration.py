"""v4 `reasoning:` declaration — parse, validate, apply (tmp paths + fakes only).

No live config, no live profiles, no subprocess: the profile sync is injected.
"""

import json
import os
import sys

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import cluster_template as ct              # noqa: E402
import template_intent as ti               # noqa: E402

TIERS = {"max": "xhigh", "standard": "medium", "light": "low"}


def _tpl(version=4, **extra):
    d = {"name": "rz", "version": version, "orchestrator": {"recipe": "same.yaml"},
         "families": [{"name": "coding", "models": [{"recipe": "same.yaml"}],
                       "workers": "remaining", "proxy": True}]}
    d.update(extra)
    return ti.ClusterTemplate.from_dict(d)


def test_reasoning_round_trips_and_is_v4_only():
    tpl = _tpl(reasoning=dict(TIERS))
    assert tpl.reasoning == TIERS
    assert tpl.to_dict()["reasoning"] == TIERS
    assert "reasoning" not in _tpl().to_dict()
    with pytest.raises(ti.TemplateIntentError):
        _tpl(version=3, reasoning=dict(TIERS))


@pytest.mark.parametrize("block, needle", [
    ("xhigh", "expected a mapping"),
    ({"max": "xhigh", "ultra": "x"}, "unknown tier 'ultra'"),
    ({"max": ""}, "reasoning.max: expected a non-empty string"),
    ({"standard": 3}, "reasoning.standard: expected a non-empty string"),
])
def test_validate_reasoning_errors(block, needle):
    errors, warnings = [], []
    ct._validate_reasoning_block(_tpl(reasoning=block), errors, warnings)
    assert any(needle in e for e in errors), errors


def test_validate_reasoning_warns_on_missing_tier():
    errors, warnings = [], []
    ct._validate_reasoning_block(_tpl(reasoning={"max": "xhigh"}), errors, warnings)
    assert not errors
    assert any("standard, light not declared" in w for w in warnings)


def test_apply_records_policy_updates_root_and_syncs_profiles(tmp_path):
    conf = tmp_path / "config.yaml"
    conf.write_text(yaml.safe_dump({
        "agent": {"reasoning_overrides": {"orchestrator-model": "xhigh",
                                          "worker-model": "xhigh",
                                          "some/concrete-id": "xhigh"},
                  "max_turns": 120},
        "auxiliary": {"compression": {"timeout": 300}, "title_generation": {"timeout": 90}},
        "kanban": {"max_in_progress": 14}}))
    pol = tmp_path / "state" / "reasoning_policy.json"
    calls = []
    out = ct._apply_reasoning_declaration(
        _tpl(reasoning=dict(TIERS)), config_yaml=conf, policy_path=pol,
        _sync=lambda: calls.append(1) or {"ok": True, "profiles": 3, "changed": 2})
    assert json.loads(pol.read_text()) == {"template": "rz", "tiers": TIERS}
    d = yaml.safe_load(conf.read_text())
    ov = d["agent"]["reasoning_overrides"]
    assert ov == {"orchestrator-model": "xhigh", "worker-model": "medium",
                  "some/concrete-id": "xhigh"}            # unknown keys left alone
    assert d["agent"]["max_turns"] == 120 and d["kanban"]["max_in_progress"] == 14
    assert d["auxiliary"]["compression"] == {"timeout": 300, "reasoning_effort": "medium"}
    assert d["auxiliary"]["curator"]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in d["auxiliary"]["title_generation"]
    assert out["changed"] and calls == [1] and out["profiles"]["changed"] == 2


def test_apply_without_block_or_tier_writes_nothing(tmp_path):
    conf = tmp_path / "config.yaml"
    conf.write_text(yaml.safe_dump({"agent": {"reasoning_overrides": {"worker-model": "xhigh"}}}))
    pol = tmp_path / "p.json"
    out = ct._apply_reasoning_declaration(_tpl(), config_yaml=conf, policy_path=pol,
                                          _sync=lambda: pytest.fail("must not sync"))
    assert out["tiers"] == {} and not pol.exists()
    out = ct._apply_reasoning_declaration(_tpl(reasoning={"max": "xhigh"}), config_yaml=conf,
                                          policy_path=pol, _sync=lambda: {"changed": 0})
    d = yaml.safe_load(conf.read_text())
    assert d["agent"]["reasoning_overrides"]["worker-model"] == "xhigh"   # standard undeclared
    assert "auxiliary" not in d or not any(
        isinstance(v, dict) and "reasoning_effort" in v for v in d["auxiliary"].values())


def test_shipped_flash_next_template_declares_its_vocabulary():
    path = os.path.join(os.path.dirname(_HERE), "templates", "4node", "flash-next-all.yaml")
    d = yaml.safe_load(open(path))
    assert d["version"] == 4
    assert d["reasoning"] == TIERS
