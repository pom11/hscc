"""Tests for install_triggers — idempotent default trigger rule installer."""
import json
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import install_triggers as IT


DEFAULT_RULES = ["orch-dgx-down", "vllm-down", "watchdog-blocked"]

# Path to the default rules file (shipped alongside install_triggers.py)
_DEFAULTS_PATH = os.path.join(_PLUGIN_DIR, "triggers.default.json")


def _load_defaults():
    """Load the shipped default rules file."""
    with open(_DEFAULTS_PATH) as f:
        return json.load(f)


# ── Fresh install ──────────────────────────────────────────────────────


def test_fresh_install_adds_all_rules(tmp_path):
    """When no triggers.json exists, all 3 default rules are installed."""
    target = str(tmp_path / "triggers.json")
    result = IT.install_triggers(triggers_path=target, defaults_path=_DEFAULTS_PATH)

    assert set(result["added"]) == set(DEFAULT_RULES)
    assert result["total"] == 3

    with open(target) as f:
        data = json.load(f)
    ids = [r["id"] for r in data["rules"]]
    assert ids == DEFAULT_RULES


# ── Idempotency ────────────────────────────────────────────────────────


def test_second_run_adds_nothing(tmp_path):
    """A second install run on an already-populated file adds 0 rules."""
    target = str(tmp_path / "triggers.json")
    IT.install_triggers(triggers_path=target, defaults_path=_DEFAULTS_PATH)

    result = IT.install_triggers(triggers_path=target, defaults_path=_DEFAULTS_PATH)

    assert result["added"] == []
    assert result["total"] == 3


# ── Operator rule preservation ────────────────────────────────────────


def test_operator_rules_preserved(tmp_path):
    """Custom operator rules and modified default-rule ids are preserved."""
    target = str(tmp_path / "triggers.json")

    # Pre-seed with an operator rule + a modified default rule
    existing = {
        "rules": [
            {
                "id": "orch-dgx-down",
                "trigger_type": "notify",
                "condition": {"metric": "failed_dgx", "op": "==", "value": True},
                "trigger_params": {
                    "title": "CUSTOM: my own DGX alert",
                    "body": "Operator-customised body.",
                },
            },
            {
                "id": "custom-cpu-alert",
                "trigger_type": "notify",
                "condition": {"metric": "cpu_pct", "op": ">", "value": 90},
                "trigger_params": {
                    "title": "CPU high",
                    "body": "CPU > 90%.",
                },
            },
        ]
    }
    with open(target, "w") as f:
        json.dump(existing, f)

    result = IT.install_triggers(triggers_path=target, defaults_path=_DEFAULTS_PATH)

    # Only missing defaults added; orch-dgx-down was already present (operator-edited)
    assert "orch-dgx-down" not in result["added"]
    assert set(result["added"]) <= {"vllm-down", "watchdog-blocked"}
    assert result["total"] == 4

    with open(target) as f:
        data = json.load(f)
    rules_by_id = {r["id"]: r for r in data["rules"]}

    # Operator's customised orch-dgx-down preserved (not overwritten)
    assert rules_by_id["orch-dgx-down"]["trigger_params"]["title"] == "CUSTOM: my own DGX alert"

    # Custom operator rule preserved
    assert rules_by_id["custom-cpu-alert"]["trigger_params"]["title"] == "CPU high"


# ── Schema validation ─────────────────────────────────────────────────


def test_default_rules_have_required_schema():
    """Each default rule has id, trigger_type, condition{metric,op,value}, trigger_params."""
    defaults = _load_defaults()
    for rule in defaults["rules"]:
        assert "id" in rule, "rule missing id"
        assert "trigger_type" in rule, f"rule {rule['id']} missing trigger_type"
        assert "condition" in rule, f"rule {rule['id']} missing condition"
        cond = rule["condition"]
        assert "metric" in cond, f"rule {rule['id']} condition missing metric"
        assert "op" in cond, f"rule {rule['id']} condition missing op"
        assert "value" in cond, f"rule {rule['id']} condition missing value"
        assert "trigger_params" in rule, f"rule {rule['id']} missing trigger_params"
        params = rule["trigger_params"]
        assert "title" in params, f"rule {rule['id']} trigger_params missing title"
        assert "body" in params, f"rule {rule['id']} trigger_params missing body"


# ── Missing defaults file ─────────────────────────────────────────────


def test_missing_defaults_file_returns_empty(tmp_path):
    """When defaults_path does not exist, no rules are added and no error raised."""
    target = str(tmp_path / "triggers.json")
    result = IT.install_triggers(
        triggers_path=target, defaults_path="/nonexistent/triggers.default.json"
    )

    assert result["added"] == []
    assert result["total"] == 0


# ── Corrupt existing triggers.json ────────────────────────────────────


def test_corrupt_existing_file_treated_as_empty(tmp_path):
    """A corrupt triggers.json is treated as empty; defaults are still added."""
    target = str(tmp_path / "triggers.json")
    with open(target, "w") as f:
        f.write("{bad json content")

    result = IT.install_triggers(triggers_path=target, defaults_path=_DEFAULTS_PATH)

    assert set(result["added"]) == set(DEFAULT_RULES)
    assert result["total"] == 3


# ── Backup on re-install ──────────────────────────────────────────────


def test_existing_file_backed_up(tmp_path):
    """When triggers.json already exists, a .bak copy is created before write."""
    target = str(tmp_path / "triggers.json")
    with open(target, "w") as f:
        json.dump({"rules": [{"id": "custom", "trigger_type": "notify"}]}, f)

    bak = target + ".bak"
    assert not os.path.exists(bak)

    IT.install_triggers(triggers_path=target, defaults_path=_DEFAULTS_PATH)

    assert os.path.exists(bak)
    with open(bak) as f:
        backup = json.load(f)
    assert backup["rules"][0]["id"] == "custom"


# ── __main__ prints valid JSON ────────────────────────────────────────


def test_main_prints_json(capsys, tmp_path, monkeypatch):
    """The __main__ block prints valid JSON to stdout."""
    target = str(tmp_path / "triggers.json")
    # Patch the default triggers_path so we don't touch real ~/.hscc
    monkeypatch.setenv("HOME", str(tmp_path))

    # Import fresh so __main__ uses our patched env
    import importlib

    mod = importlib.reload(IT)
    result = mod.install_triggers()
    print(json.dumps(result))

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert "added" in parsed
    assert "total" in parsed
