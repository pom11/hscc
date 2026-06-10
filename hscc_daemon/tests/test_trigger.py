"""Unit tests for trigger.py - trigger engine.

Tests load/save triggers, evaluate_trigger, fire_trigger_action, trigger_engine,
and read_events_tail. All I/O isolated via monkeypatch.
"""
import json
import os
import pytest
from pathlib import Path


class TestLoadTriggers:
    """load_triggers() reads trigger rules from triggers.json."""

    def test_load_empty(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        (tmp_hfcc_dir / "triggers.json").write_text(json.dumps({"rules": []}))
        assert trigger.load_triggers() == []

    def test_load_rules(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        rules = [
            {"id": "r1", "enabled": True, "trigger_type": "notify", "cooldown_seconds": 60},
            {"id": "r2", "enabled": False, "trigger_type": "auto_restart", "cooldown_seconds": 300},
        ]
        (tmp_hfcc_dir / "triggers.json").write_text(json.dumps({"rules": rules}))
        result = trigger.load_triggers()
        assert len(result) == 2
        assert result[0]["id"] == "r1"

    def test_missing_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        assert trigger.load_triggers() == []

    def test_malformed_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        (tmp_hfcc_dir / "triggers.json").write_text("{bad json")
        assert trigger.load_triggers() == []


class TestLoadSaveCooldowns:
    """load_cooldowns() and save_cooldowns() persist cooldown state."""

    def test_load_empty(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        assert trigger.load_cooldowns() == {}

    def test_save_and_load(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        trigger.save_cooldowns({"r1": 1000.0, "r2": 2000.0})
        result = trigger.load_cooldowns()
        assert result["r1"] == 1000.0
        assert result["r2"] == 2000.0

    def test_missing_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        assert trigger.load_cooldowns() == {}


class TestReadEventsTail:
    """read_events_tail() reads last N lines from events.jsonl."""

    def test_no_events_file(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        assert trigger.read_events_tail() == []

    def test_reads_events(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        events = [
            json.dumps({"event_type": "test1"}),
            json.dumps({"event_type": "test2"}),
            json.dumps({"event_type": "test3"}),
        ]
        (tmp_hfcc_dir / "events.jsonl").write_text("\n".join(events) + "\n")
        result = trigger.read_events_tail(limit=2)
        assert len(result) == 2
        assert "test2" in result[0]
        assert "test3" in result[1]

    def test_skips_empty_lines(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        (tmp_hfcc_dir / "events.jsonl").write_text("{}\n\n{}\n")
        result = trigger.read_events_tail()
        assert len(result) == 2


class TestEvaluateTrigger:
    """evaluate_trigger() checks if a rule matches an event."""

    def test_severity_equality(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "severity", "op": "==", "value": "critical"}}
        event = {"severity": "critical"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_severity_inequality(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "severity", "op": "==", "value": "critical"}}
        event = {"severity": "warning"}
        assert trigger.evaluate_trigger(rule, event) is False

    def test_numeric_greater_than(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "severity", "op": ">", "value": "2"}}
        event = {"severity": "5"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_contains_operator(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "event_type", "op": "contains", "value": "error"}}
        event = {"event_type": "system.error"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_matches_regex(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "event_type", "op": "matches", "value": "sys.*"}}
        event = {"event_type": "system.alert"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_source_match(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "source", "op": "==", "value": "daemon"}}
        event = {"source": "daemon"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_event_type_not_equals(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "event_type", "op": "!=", "value": "heartbeat"}}
        event = {"event_type": "alert"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_unknown_metric_returns_false(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "nonexistent", "op": "==", "value": "x"}}
        event = {"severity": "info"}
        assert trigger.evaluate_trigger(rule, event) is False

    def test_state_based_failed_dgx(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        # Write a failing DGX state
        (state_dir / "dgx.json").write_text(json.dumps({"ok": False}))

        rule = {"condition": {"metric": "failed_dgx", "op": "==", "value": "True"}}
        event = {"event_type": "state.dgx.degraded"}
        assert trigger.evaluate_trigger(rule, event) is True

    def test_type_error_returns_false(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        rule = {"condition": {"metric": "severity", "op": ">", "value": "not_a_number"}}
        event = {"severity": "also_not_a_number"}
        assert trigger.evaluate_trigger(rule, event) is False


class TestFireTriggerAction:
    """fire_trigger_action() executes the action defined by a rule."""

    def test_notify_action(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        notifications_sent = []
        def fake_notify(title, body, priority="normal"):
            notifications_sent.append({"title": title, "body": body})

        monkeypatch.setattr(trigger, "send_macos_notification", fake_notify)

        rule = {
            "id": "r1",
            "trigger_type": "notify",
            "trigger_params": {"title": "Alert", "body": "DGX down"},
        }
        event = {"severity": "critical"}
        trigger.fire_trigger_action(rule, event)

        assert len(notifications_sent) == 1
        assert notifications_sent[0]["title"] == "Alert"

    def test_emit_event_action(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        events_emitted = []
        def fake_emit(event_type, payload, source=""):
            events_emitted.append({"type": event_type, "payload": payload})

        monkeypatch.setattr(trigger, "emit_event", fake_emit)

        rule = {
            "id": "r1",
            "trigger_type": "emit_event",
            "trigger_params": {"event_type": "custom.alert", "payload": {"key": "val"}},
        }
        event = {"severity": "warning"}
        trigger.fire_trigger_action(rule, event)

        assert len(events_emitted) == 1
        assert events_emitted[0]["type"] == "custom.alert"


class TestTriggerEngine:
    """trigger_engine() evaluates all rules against events."""

    def test_no_rules(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        monkeypatch.setattr(trigger, "send_macos_notification", lambda *a, **kw: None)
        monkeypatch.setattr(trigger, "emit_event", lambda *a, **kw: None)

        result = trigger.trigger_engine()
        assert result is True  # no rules -> OK

    def test_disabled_rule_skipped(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        monkeypatch.setattr(trigger, "send_macos_notification", lambda *a, **kw: None)
        monkeypatch.setattr(trigger, "emit_event", lambda *a, **kw: None)

        (tmp_hfcc_dir / "triggers.json").write_text(json.dumps({"rules": [
            {"id": "r1", "enabled": False, "trigger_type": "notify",
             "condition": {"metric": "severity", "op": "==", "value": "critical"},
             "trigger_params": {"title": "X", "body": "Y"},
             "cooldown_seconds": 0},
        ]}))
        (tmp_hfcc_dir / "events.jsonl").write_text(json.dumps({"severity": "critical"}) + "\n")

        result = trigger.trigger_engine()
        assert result is True

    def test_cooldown_prevents_rerun(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        import time
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        monkeypatch.setattr(trigger, "send_macos_notification", lambda *a, **kw: None)
        monkeypatch.setattr(trigger, "emit_event", lambda *a, **kw: None)

        # Set cooldown to recent time
        (tmp_hfcc_dir / "cooldowns.json").write_text(json.dumps({"r1": time.time()}))
        (tmp_hfcc_dir / "triggers.json").write_text(json.dumps({"rules": [
            {"id": "r1", "enabled": True, "trigger_type": "notify",
             "condition": {"metric": "severity", "op": "==", "value": "critical"},
             "trigger_params": {"title": "X", "body": "Y"},
             "cooldown_seconds": 3600},
        ]}))
        (tmp_hfcc_dir / "events.jsonl").write_text(json.dumps({"severity": "critical"}) + "\n")

        fired = []
        def track_fire(*a, **kw):
            fired.append(True)

        monkeypatch.setattr(trigger, "send_macos_notification", track_fire)

        trigger.trigger_engine()
        assert len(fired) == 0  # cooldown active -> no fire


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
