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

    # -- auto_restart gated on intentional autodown (§5 C2, audit finding 5) --

    def _restart_rule(self):
        return {
            "id": "r-restart",
            "trigger_type": "auto_restart",
            "trigger_params": {},
        }

    def test_auto_restart_suppressed_while_intentional(self, tmp_hfcc_dir, monkeypatch):
        """§5 C2: an intentional autodown must suppress automated restarts from
        EVERY path, including the trigger engine. auto_restart on vllm_down while
        the block carries intentional:autodown ⇒ restart_vllm_fn NOT called."""
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "send_macos_notification",
                            lambda *a, **kw: None)
        monkeypatch.setattr(trigger, "log", lambda *a, **kw: None)

        restart_calls = []
        # watchdog_block_fn returns a block carrying intentional autodown.
        def intentional_block():
            return {"blocked": True, "intentional": "autodown",
                    "reason": "autodown: intentional idle teardown"}
        trigger.fire_trigger_action(
            self._restart_rule(), {"severity": "critical"},
            watchdog_block_fn=intentional_block,
            restart_vllm_fn=lambda: restart_calls.append(1) or {"ok": True},
        )
        assert restart_calls == []      # resurrect suppressed

    def test_auto_restart_allowed_when_not_intentional(self, tmp_hfcc_dir, monkeypatch):
        """§5 C2 negative control: auto_restart on vllm_down WITHOUT an
        intentional block ⇒ restart_vllm_fn IS called (ordinary healing)."""
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "send_macos_notification",
                            lambda *a, **kw: None)
        monkeypatch.setattr(trigger, "log", lambda *a, **kw: None)

        restart_calls = []
        # watchdog_block_fn returns a plain (non-intentional) block.
        def plain_block():
            return {"blocked": False, "reason": "", "failures": []}
        trigger.fire_trigger_action(
            self._restart_rule(), {"severity": "critical"},
            watchdog_block_fn=plain_block,
            restart_vllm_fn=lambda: restart_calls.append(1) or {"ok": True},
        )
        assert len(restart_calls) == 1   # ordinary healing still fires


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


class TestStateNotifyFloor:
    """A rule that never declared `cooldown_seconds` must not re-notify a
    PERSISTING degraded state on every 15 s loop tick.

    The 2026-10-08 lost NAS mount fired 178 macOS notifications in 11 minutes
    because no shipped rule set a cooldown and the engine only suppressed
    re-fires when `cooldown_seconds > 0`. Two layers now stop that: the shipped
    defaults carry cooldowns, and the engine has a floor for rules that declare
    none. These exercise the engine layer against a persistent state.
    """

    LOOP_TICKS = 3  # three consecutive trigger-loop cycles

    def _env(self, tmp_hfcc_dir, monkeypatch, nas_ok=False):
        """Wire the engine to tmp paths with `nas` degraded, notifications
        captured. Returns (trigger_module, list_of_notification_titles)."""
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        (state_dir / "nas.json").write_text(json.dumps(
            {"ok": nas_ok,
             "details": {"message": "/Volumes/NAS is not an NFS mount"}}))

        monkeypatch.setattr(trigger, "TRIGGERS_FILE",
                            str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE",
                            str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE",
                            str(tmp_hfcc_dir / "events.jsonl"))
        (tmp_hfcc_dir / "events.jsonl").write_text("")

        sent = []
        monkeypatch.setattr(
            trigger, "send_macos_notification",
            lambda title, body, priority="normal": sent.append(title))
        monkeypatch.setattr(trigger, "emit_event", lambda *a, **kw: None)
        return trigger, sent

    def _write_rules(self, trigger_mod, tmp_hfcc_dir, rules):
        (tmp_hfcc_dir / "triggers.json").write_text(
            json.dumps({"rules": rules}))

    def _nas_rule(self, **extra):
        rule = {
            "id": "nas-down",
            "trigger_type": "notify",
            "condition": {"metric": "nas_down", "op": "==", "value": True},
            "trigger_params": {"title": "HSCC: NAS mount lost",
                               "body": "mount lost"},
        }
        rule.update(extra)
        return rule

    def test_declared_cooldown_suppresses_repeats(self, tmp_hfcc_dir,
                                                  monkeypatch):
        """The card's headline case: a persistent degraded state notifies ONCE
        across several loop cycles when the rule declares a cooldown."""
        trigger, sent = self._env(tmp_hfcc_dir, monkeypatch)
        self._write_rules(trigger, tmp_hfcc_dir,
                          [self._nas_rule(cooldown_seconds=3600)])

        for _ in range(self.LOOP_TICKS):
            assert trigger.trigger_engine() is True

        assert sent == ["HSCC: NAS mount lost"], (
            f"expected exactly one notify across {self.LOOP_TICKS} cycles, "
            f"got {len(sent)}")

    def test_missing_cooldown_falls_back_to_floor(self, tmp_hfcc_dir,
                                                 monkeypatch):
        """The layer that actually fixes EXISTING installs: bootstrap only ever
        ADDS missing rule ids, so a live triggers.json keeps its cooldown-less
        rules — the engine floor is what dampens them."""
        trigger, sent = self._env(tmp_hfcc_dir, monkeypatch)
        self._write_rules(trigger, tmp_hfcc_dir, [self._nas_rule()])
        assert "cooldown_seconds" not in self._nas_rule()

        for _ in range(self.LOOP_TICKS):
            assert trigger.trigger_engine() is True

        assert sent == ["HSCC: NAS mount lost"], (
            f"a rule with no cooldown_seconds must be floored, got "
            f"{len(sent)} notifies")

    def test_unparseable_cooldown_gets_floor_too(self, tmp_hfcc_dir,
                                                 monkeypatch):
        """A typo (`"cooldown_seconds": "1h"`) must not re-open the storm — an
        unreadable value is treated as undeclared, i.e. floored."""
        trigger, sent = self._env(tmp_hfcc_dir, monkeypatch)
        self._write_rules(trigger, tmp_hfcc_dir,
                          [self._nas_rule(cooldown_seconds="1h")])

        for _ in range(self.LOOP_TICKS):
            assert trigger.trigger_engine() is True

        assert sent == ["HSCC: NAS mount lost"]

    def test_explicit_zero_is_operator_intent_and_still_spams(self,
                                                             tmp_hfcc_dir,
                                                             monkeypatch):
        """Negative control: `cooldown_seconds: 0` is a deliberate "notify every
        cycle", NOT an omission. The floor must not silently override it."""
        trigger, sent = self._env(tmp_hfcc_dir, monkeypatch)
        self._write_rules(trigger, tmp_hfcc_dir,
                          [self._nas_rule(cooldown_seconds=0)])

        for _ in range(self.LOOP_TICKS):
            assert trigger.trigger_engine() is True

        assert len(sent) == self.LOOP_TICKS

    def test_discrete_event_fire_is_never_suppressed(self, tmp_hfcc_dir,
                                                     monkeypatch):
        """The floor covers PERSISTING STATES only. Suppressing a match on a
        real event line could drop a genuine one-off alert, so an event rule
        with no cooldown keeps firing — which is precisely why the shipped
        defaults must carry cooldowns (layer 1, asserted in
        hscc-bootstrap/tests/test_install_triggers.py)."""
        trigger, sent = self._env(tmp_hfcc_dir, monkeypatch)
        (tmp_hfcc_dir / "events.jsonl").write_text(json.dumps(
            {"event_type": "backup.failed", "severity": "warning"}) + "\n")
        self._write_rules(trigger, tmp_hfcc_dir, [{
            "id": "backup-failed",
            "trigger_type": "notify",
            "condition": {"metric": "event_type", "op": "==",
                          "value": "backup.failed"},
            "trigger_params": {"title": "HSCC: backup failed",
                               "body": "backup did not run"},
        }])

        for _ in range(self.LOOP_TICKS):
            assert trigger.trigger_engine() is True

        assert len(sent) == self.LOOP_TICKS, (
            "a discrete event match must not be floored")

    def test_healthy_state_stays_silent(self, tmp_hfcc_dir, monkeypatch):
        """The floor must not manufacture alerts: with nas ok:True nothing
        fires at all, in any cycle."""
        trigger, sent = self._env(tmp_hfcc_dir, monkeypatch, nas_ok=True)
        self._write_rules(trigger, tmp_hfcc_dir, [self._nas_rule()])

        for _ in range(self.LOOP_TICKS):
            assert trigger.trigger_engine() is True

        assert sent == []


class TestNasDownMetric:
    """The `nas_down` metric, and the default rule that fires on it.

    Before this there was NO metric that could read the nas check, so no rule
    could alert on a lost share. The 2026-10-08 NAS reboot left all five
    clients unmounted and `nas.json` ok=False for ~2h with nobody notified:
    detection worked, the actor was missing.
    """

    def _state_dir(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        return state_dir

    def _rule(self):
        return {"condition": {"metric": "nas_down", "op": "==",
                              "value": "True"}}

    def test_fires_when_nas_check_failing(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        state_dir = self._state_dir(tmp_hfcc_dir, monkeypatch)
        (state_dir / "nas.json").write_text(json.dumps({
            "ok": False,
            "details": {"message": "/Volumes/NAS exists but is not an NFS "
                                   "mount (export/mount lost)"}}))
        assert trigger.evaluate_trigger(
            self._rule(), {"event_type": "state.nas.degraded"}) is True

    def test_silent_when_nas_healthy(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import trigger
        state_dir = self._state_dir(tmp_hfcc_dir, monkeypatch)
        (state_dir / "nas.json").write_text(json.dumps({"ok": True}))
        assert trigger.evaluate_trigger(
            self._rule(), {"event_type": "heartbeat"}) is False

    def test_absent_state_does_not_alert(self, tmp_hfcc_dir, monkeypatch):
        """A daemon that has not run the nas check yet must not alert — same
        fail-open shape as failed_dgx, so a fresh start is quiet."""
        from hscc_daemon import trigger
        self._state_dir(tmp_hfcc_dir, monkeypatch)
        assert trigger.evaluate_trigger(
            self._rule(), {"event_type": "heartbeat"}) is False

    def test_default_rules_ship_a_nas_rule(self):
        """The metric is useless without a rule that uses it, and bootstrap
        only ever ADDS missing default ids — so the rule has to be in the
        shipped defaults to reach an existing install."""
        import os
        import json as _json
        here = os.path.dirname(os.path.abspath(__file__))
        defaults = os.path.join(
            here, "..", "..", "hscc-bootstrap", "triggers.default.json")
        rules = _json.load(open(os.path.normpath(defaults)))["rules"]
        nas = [r for r in rules if r["id"] == "nas-down"]
        assert len(nas) == 1, [r["id"] for r in rules]
        assert nas[0]["condition"]["metric"] == "nas_down"
        assert nas[0]["condition"]["value"] is True
        assert nas[0]["trigger_type"] == "notify"
