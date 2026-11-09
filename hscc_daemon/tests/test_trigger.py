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


class TestOperatorDownNotifySuppression:
    """STAY-DOWN-NOTIFY (t_50c6edc4): the serving-layer notify rules must stand
    down while the fleet is deliberately down.

    The bug (daemon.log 2026-11-08, addresses redacted): `hscc cluster down`
    cleared the expected-up marker at 17:51:23 and at 17:52:19 the daemon still
    sent `macOS notification sent: HSCC: orchestrator/DGX unhealthy` — the title
    of the shipped `orch-dgx-down` rule. Tearing the fleet down makes
    `failed_dgx`, `vllm_down` and the engine wedge true BY CONSTRUCTION, so the
    page contradicted the instruction that caused it. `fire_trigger_action`
    already suppressed `auto_restart` for an intentional autodown; it had no
    gate on `notify` at all.

    The fix under test: the notify branch consults the durable record from
    ``post_boot.deliberately_down()`` through the ``deliberately_down_fn`` seam
    (``trigger._operator_down``), and skips the page for exactly the three
    serving-layer metrics named by ``_OPERATOR_DOWN_SUPPRESSED_METRICS``.

    Proof shape (the card's acceptance list):
      * failed_dgx / vllm_down / engine-wedge rule + record -> NO notification
        and the ONE suppression INFO line exactly once;
      * nas_down / watchdog_blocked + record -> STILL notify;
      * failed_dgx with no record (injected None, and the REAL predicate over an
        absent/corrupt marker) -> notifies exactly as before;
      * a raising predicate -> notifies (fail-safe toward PAGING);
      * both sanctioned producer wordings honoured, no substring matching;
      * emit_event / block_pipeline never suppressed; the auto_restart
        autodown gate unaffected and demonstrably independent.
    Rules are loaded from the SHIPPED hscc-bootstrap/triggers.default.json, so
    these tests fail if the constant drifts from what actually ships.
    """

    # Verbatim from the two sanctioned callers of clear_expected_up(reason):
    REASON_OPERATOR = "hscc cluster down"                    # hscc-cluster/hscc.py
    REASON_AUTODOWN = "autodown: intentional idle teardown"  # hscc_daemon/autodown.py

    @pytest.fixture
    def marker(self, tmp_path, monkeypatch):
        """The expected-up marker. conftest already redirects post_boot's
        constant suite-wide; pin it here explicitly (test_post_boot.py's
        convention) so the tests that use the REAL predicate say which file they
        are about — nothing in this class can touch ~/.hscc."""
        path = str(tmp_path / "expected-up.json")
        from hscc_daemon import post_boot as pb
        monkeypatch.setattr(pb, "EXPECTED_UP_FILE", path)
        return path

    def _env(self, tmp_hfcc_dir, monkeypatch):
        """Wire state + notifications + log capture to tmp paths.

        Returns (trigger_module, notification_titles, (level, message) lines).
        """
        from hscc_daemon import trigger
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        sent = []
        monkeypatch.setattr(
            trigger, "send_macos_notification",
            lambda title, body, priority="normal": sent.append(title))
        logs = []
        monkeypatch.setattr(trigger, "log",
                            lambda msg, level="INFO", **kw: logs.append((level, msg)))
        emitted = []
        monkeypatch.setattr(trigger, "emit_event",
                            lambda event_type, payload, source="": emitted.append(event_type))
        return trigger, sent, logs, emitted

    def _default_rule(self, rule_id):
        """The SHIPPED rule, so the constant and the defaults cannot drift."""
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        defaults = os.path.join(here, "..", "..", "hscc-bootstrap",
                                "triggers.default.json")
        rules = json.load(open(os.path.normpath(defaults)))["rules"]
        match = [r for r in rules if r["id"] == rule_id]
        assert len(match) == 1, f"{rule_id} not in triggers.default.json"
        return match[0]

    @staticmethod
    def _record(reason, set_at="2026-11-08T17:51:23+02:00"):
        """The record shape post_boot.clear_expected_up stamps."""
        return lambda: {"set_at": set_at, "reason": reason}

    @staticmethod
    def _line(rule_id, reason):
        return (f"Trigger {rule_id}: notify suppressed — "
                f"fleet deliberately down ({reason})")

    def _fire(self, monkeypatch, tmp_hfcc_dir, rule, reason=REASON_OPERATOR,
              event=None):
        """Fire one rule with the stay-down record set. Returns (titles, logs)."""
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            rule, event or {"event_type": "state.dgx.degraded"},
            deliberately_down_fn=self._record(reason))
        return sent, logs

    # ── the three serving-layer rules are silenced ─────────────────────────

    def test_constant_covers_exactly_the_shipped_serving_rules(self):
        """The constant must name the three serving-layer rules in
        triggers.default.json and NOTHING else — watchdog_blocked and nas_down
        are not fleet-serving signals and stay out."""
        from hscc_daemon import trigger
        serving = ("orch-dgx-down", "vllm-down", "engine-wedge-detected")
        other = ("nas-down", "watchdog-blocked")
        for rule_id in serving:
            assert trigger._serving_layer_notify(self._default_rule(rule_id)), rule_id
        for rule_id in other:
            assert not trigger._serving_layer_notify(self._default_rule(rule_id)), rule_id
        assert trigger._OPERATOR_DOWN_SUPPRESSED_METRICS == {
            "failed_dgx", "vllm_down", "state.engine_wedge.degraded"}

    def test_failed_dgx_notify_suppressed(self, tmp_hfcc_dir, monkeypatch):
        """The incident itself: orch-dgx-down while deliberately down ⇒ no page,
        ONE INFO line, exact wording."""
        rule = self._default_rule("orch-dgx-down")
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir, rule)
        assert sent == []
        assert logs == [("INFO", self._line("orch-dgx-down",
                                            self.REASON_OPERATOR))]

    def test_vllm_down_notify_suppressed(self, tmp_hfcc_dir, monkeypatch):
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir,
                                self._default_rule("vllm-down"))
        assert sent == []
        assert logs == [("INFO", self._line("vllm-down", self.REASON_OPERATOR))]

    def test_engine_wedge_notify_suppressed(self, tmp_hfcc_dir, monkeypatch):
        """The shipped wedge rule keys on metric==event_type and carries
        state.engine_wedge.degraded in condition.value, so it only reaches the
        gate if the value is consulted too (see _serving_layer_notify)."""
        rule = self._default_rule("engine-wedge-detected")
        assert rule["condition"]["metric"] == "event_type"
        assert rule["condition"]["value"] == "state.engine_wedge.degraded"
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir, rule,
                                event={"event_type": "state.engine_wedge.degraded"})
        assert sent == []
        assert logs == [("INFO", self._line("engine-wedge-detected",
                                            self.REASON_OPERATOR))]

    # ── non-serving signals must KEEP paging ───────────────────────────────

    def test_nas_down_still_fires_while_down(self, tmp_hfcc_dir, monkeypatch):
        """A lost NAS mount is not a fleet-serving signal: it must page even
        when the fleet is deliberately down."""
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir,
                                self._default_rule("nas-down"))
        assert sent == ["HSCC: NAS mount lost"]
        assert logs == []

    def test_watchdog_blocked_still_fires_while_down(self, tmp_hfcc_dir,
                                                     monkeypatch):
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir,
                                self._default_rule("watchdog-blocked"))
        assert sent == ["HSCC: pipeline watchdog blocked"]
        assert logs == []

    def test_serving_rule_without_metric_still_fires(self, tmp_hfcc_dir,
                                                     monkeypatch):
        """Never suppress notify globally: an operator rule on some other
        metric pages normally even with the record set."""
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            {"id": "backup-failed", "trigger_type": "notify",
             "condition": {"metric": "event_type", "op": "==",
                           "value": "backup.failed"},
             "trigger_params": {"title": "HSCC: backup failed", "body": "x"}},
            {"event_type": "backup.failed"},
            deliberately_down_fn=self._record(self.REASON_OPERATOR))
        assert sent == ["HSCC: backup failed"]
        assert logs == []

    # ── no record / unreadable record => today's behaviour (page) ──────────

    def test_no_record_fires_exactly_as_today(self, tmp_hfcc_dir, monkeypatch):
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            self._default_rule("orch-dgx-down"), {"event_type": "x"},
            deliberately_down_fn=lambda: None)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []

    @pytest.mark.parametrize("shape", ["absent", "corrupt", "not-a-dict",
                                       "empty-record"])
    def test_real_predicate_absent_or_broken_marker_fires(
            self, tmp_hfcc_dir, monkeypatch, marker, shape):
        """The DEFAULT wiring (no injected fn) reads the real
        post_boot.deliberately_down. Absent / corrupt / non-dict marker, or a
        present-but-empty record, must all leave the page exactly as it was
        before the record existed — fail-safe toward supervision, never quiet."""
        from hscc_daemon import post_boot as pb
        if shape == "absent":
            assert not os.path.exists(marker)
        elif shape == "corrupt":
            with open(marker, "w") as f:
                f.write("{not json")
        elif shape == "not-a-dict":
            with open(marker, "w") as f:
                json.dump(["units"], f)
        else:
            with open(marker, "w") as f:
                json.dump({"units": [], pb.DELIBERATE_DOWN_KEY: {}}, f)
        assert pb.deliberately_down(marker) is None

        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            self._default_rule("orch-dgx-down"), {"event_type": "x"})
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []

    def test_real_record_written_by_the_real_producer_suppresses(
            self, tmp_hfcc_dir, monkeypatch, marker):
        """End-to-end on the default path: the real clear_expected_up stamps the
        record, the real predicate reads it back, the page is skipped. Written
        by the producer, not hand-planted JSON, so the key shape cannot drift
        from post_boot without this failing."""
        from hscc_daemon import post_boot as pb
        pb.clear_expected_up(self.REASON_OPERATOR, path=marker)
        record = pb.deliberately_down(marker)
        assert record is not None and record["reason"] == self.REASON_OPERATOR

        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            self._default_rule("orch-dgx-down"), {"event_type": "x"})
        assert sent == []
        assert logs == [("INFO", self._line("orch-dgx-down",
                                            self.REASON_OPERATOR))]

    # ── the decision about WHICH writers count, pinned on both wordings ────

    @pytest.mark.parametrize("reason", [REASON_OPERATOR, REASON_AUTODOWN])
    def test_both_producer_wordings_suppress(self, tmp_hfcc_dir, monkeypatch,
                                             reason):
        """clear_expected_up has TWO sanctioned callers, so the record means
        "someone deliberately cleared the footprint" — BOTH are honoured as a
        stay-down, and the reason is echoed verbatim into the log line.
        (hscc-cluster/hscc.py stamps the first, hscc_daemon/autodown.py the
        second; the wordings here are copied verbatim from those call sites.)"""
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir,
                                self._default_rule("orch-dgx-down"),
                                reason=reason)
        assert sent == []
        assert logs == [("INFO", self._line("orch-dgx-down", reason))]

    def test_arbitrary_reason_also_suppresses_no_string_matching(
            self, tmp_hfcc_dir, monkeypatch):
        """Negative control on the decision above: the gate must NOT test the
        reason TEXT (that would make an operator-chosen string a safety
        property), so any other wording suppresses too."""
        sent, logs = self._fire(monkeypatch, tmp_hfcc_dir,
                                self._default_rule("vllm-down"),
                                reason="planned maintenance window — desk off")
        assert sent == []
        assert logs == [("INFO", self._line("vllm-down",
                                            "planned maintenance window — desk off"))]

    @pytest.mark.parametrize("record", [
        {"set_at": "2026-11-08T17:51:23+02:00"},   # no reason
        {"reason": "hscc cluster down"},           # no set_at
        {"junk": True},                            # neither
        "not-a-dict", None, 0, [],
    ])
    def test_incomplete_record_fails_toward_paging(self, tmp_hfcc_dir,
                                                   monkeypatch, record):
        """A record this gate cannot vouch for is NOT evidence of intent, and
        missing evidence must never silence an operator."""
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            self._default_rule("orch-dgx-down"), {"event_type": "x"},
            deliberately_down_fn=lambda: record)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []

    def test_raising_predicate_still_notifies(self, tmp_hfcc_dir, monkeypatch):
        """Fail-safe toward PAGING: a predicate that blows up (unreadable home,
        a post_boot too old to have the key) must never swallow an alert."""
        def boom():
            raise RuntimeError("disk gone")
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            self._default_rule("orch-dgx-down"), {"event_type": "x"},
            deliberately_down_fn=boom)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []

    def test_helper_never_raises(self, monkeypatch):
        from hscc_daemon import trigger
        assert trigger._operator_down(lambda: (_ for _ in ()).throw(OSError)) is None
        assert trigger._operator_down(lambda: "junk") is None
        assert trigger._operator_down(lambda: None) is None

    # ── the other action types, and the auto_restart gate, are untouched ───

    def test_emit_event_never_suppressed(self, tmp_hfcc_dir, monkeypatch):
        """D3: suppression is notify-only. A serving-layer emit_event keeps
        emitting while the fleet is deliberately down."""
        trigger, sent, logs, emitted = self._env(tmp_hfcc_dir, monkeypatch)
        trigger.fire_trigger_action(
            {"id": "dgx-event", "trigger_type": "emit_event",
             "condition": {"metric": "failed_dgx", "op": "==", "value": True},
             "trigger_params": {"event_type": "custom.dgx"}},
            {"event_type": "x"},
            deliberately_down_fn=self._record(self.REASON_OPERATOR))
        assert emitted == ["custom.dgx"]
        assert logs == []

    def test_block_pipeline_never_suppressed(self, tmp_hfcc_dir, monkeypatch):
        """Same for block_pipeline: the block is written and the critical page
        goes out (it is not a serving-layer notify)."""
        from hscc_daemon import lifecycle
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        blocks = []
        monkeypatch.setattr(trigger, "save_watchdog_block",
                            lambda b: blocks.append(dict(b)))
        trigger.fire_trigger_action(
            {"id": "dgx-block", "trigger_type": "block_pipeline",
             "condition": {"metric": "failed_dgx", "op": "==", "value": True}},
            {"event_type": "x"},
            watchdog_block_fn=lambda: {"blocked": False},
            deliberately_down_fn=self._record(self.REASON_OPERATOR))
        assert len(blocks) == 1 and blocks[0]["blocked"] is True
        assert any("Pipeline Blocked" in t for t in sent)
        assert logs == []

    def test_auto_restart_gate_unchanged_and_independent(self, tmp_hfcc_dir,
                                                         monkeypatch):
        """The two gates answer different questions and must not be merged:
        (a) the stay-down record alone must NOT stop an auto_restart (the
        auto_restart gate is still solely the intentional autodown block);
        (b) an intentional autodown block alone must NOT silence a notify
        (the notify gate is solely the stay-down record)."""
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        restarts = []
        # (a) record set, block NOT intentional -> restart still happens.
        trigger.fire_trigger_action(
            {"id": "r-restart", "trigger_type": "auto_restart",
             "condition": {"metric": "vllm_down", "op": "==", "value": True},
             "trigger_params": {}},
            {"event_type": "x"},
            watchdog_block_fn=lambda: {"blocked": False, "reason": ""},
            restart_vllm_fn=lambda: restarts.append(1) or {"ok": True},
            deliberately_down_fn=self._record(self.REASON_OPERATOR))
        assert len(restarts) == 1, "stay-down must not gate auto_restart"
        after_restart = len(sent)  # the auto_restart branch pages too

        # (b) intentional autodown block, NO record -> notify still fires.
        trigger.fire_trigger_action(
            self._default_rule("orch-dgx-down"), {"event_type": "x"},
            watchdog_block_fn=lambda: {"blocked": True,
                                       "intentional": "autodown"})
        assert sent[after_restart:] == ["HSCC: orchestrator/DGX unhealthy"]

    # ── the seam is threaded through the engine, which the loop uses ───────

    def _engine_env(self, tmp_hfcc_dir, monkeypatch, state_files, rules):
        from hscc_daemon import trigger
        trigger, sent, logs, _ = self._env(tmp_hfcc_dir, monkeypatch)
        state_dir = tmp_hfcc_dir / "state"
        for name, payload in state_files.items():
            (state_dir / name).write_text(json.dumps(payload))
        monkeypatch.setattr(trigger, "TRIGGERS_FILE",
                            str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE",
                            str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE",
                            str(tmp_hfcc_dir / "events.jsonl"))
        (tmp_hfcc_dir / "events.jsonl").write_text("")
        (tmp_hfcc_dir / "triggers.json").write_text(
            json.dumps({"rules": rules}))
        return trigger, sent, logs

    def test_engine_threads_the_seam(self, tmp_hfcc_dir, monkeypatch):
        """Real loop path: the degraded dgx state produces the
        `state.dgx.degraded` pseudo-event, the serving rule matches it, and the
        engine passes the stay-down predicate down to fire_trigger_action.
        cooldown_seconds: 0 keeps the cooldown out of the way so the control
        case fires on the same tick."""
        rule = dict(self._default_rule("orch-dgx-down"), cooldown_seconds=0)
        states = {"dgx.json": {"ok": False, "vllm_healthy": False}}

        trigger, sent, logs = self._engine_env(tmp_hfcc_dir, monkeypatch,
                                               states, [rule])
        assert trigger.trigger_engine(
            deliberately_down_fn=self._record(self.REASON_AUTODOWN)) is True
        assert sent == []
        assert logs == [("INFO", self._line("orch-dgx-down",
                                            self.REASON_AUTODOWN))]

        # Control: same tick, no record -> the page goes out.
        trigger, sent, logs = self._engine_env(tmp_hfcc_dir, monkeypatch,
                                               states, [rule])
        assert trigger.trigger_engine(
            deliberately_down_fn=lambda: None) is True
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]

    def test_engine_default_predicate_suppresses(self, tmp_hfcc_dir,
                                                 monkeypatch, marker):
        """The engine called with NO seam (exactly how daemon_ops'
        run_trigger_loop calls it) still honours the on-disk record."""
        from hscc_daemon import post_boot as pb
        pb.clear_expected_up(self.REASON_OPERATOR, path=marker)
        rule = dict(self._default_rule("orch-dgx-down"), cooldown_seconds=0)
        trigger, sent, logs = self._engine_env(
            tmp_hfcc_dir, monkeypatch,
            {"dgx.json": {"ok": False, "vllm_healthy": False}}, [rule])
        assert trigger.trigger_engine() is True
        assert sent == []
        assert logs == [("INFO", self._line("orch-dgx-down",
                                            self.REASON_OPERATOR))]

    def test_engine_still_pages_nas_while_down(self, tmp_hfcc_dir, monkeypatch,
                                               marker):
        """And the same engine tick, with the record on disk, still pages a lost
        NAS mount."""
        from hscc_daemon import post_boot as pb
        pb.clear_expected_up(self.REASON_OPERATOR, path=marker)
        rules = [dict(self._default_rule("nas-down"), cooldown_seconds=0),
                 dict(self._default_rule("orch-dgx-down"), cooldown_seconds=0)]
        trigger, sent, logs = self._engine_env(
            tmp_hfcc_dir, monkeypatch,
            {"nas.json": {"ok": False}, "dgx.json": {"ok": False}}, rules)
        assert trigger.trigger_engine() is True
        assert sent == ["HSCC: NAS mount lost"]
        assert logs == [("INFO", self._line("orch-dgx-down",
                                            self.REASON_OPERATOR))]

