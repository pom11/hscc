"""Trigger engine — rule evaluation, cooldowns, and event-based firing."""

import json
import os
import re
import time

from .state import read_state, read_all_states, now_iso, write_state
from .lifecycle import save_watchdog_block
from .desktop import send_macos_notification, emit_event
from .daemon_ops import log


EVENTS_FILE = os.path.expanduser("~/.hscc/events.jsonl")
TRIGGERS_FILE = os.path.expanduser("~/.hscc/triggers.json")
COOLDOWN_FILE = os.path.expanduser("~/.hscc/cooldowns.json")


def load_triggers():
    """Load trigger rules from triggers.json."""
    try:
        with open(TRIGGERS_FILE) as f:
            data = json.load(f)
        return data.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_cooldowns():
    """Load cooldown timestamps."""
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cooldowns(data):
    """Save cooldown timestamps."""
    tmp = COOLDOWN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, COOLDOWN_FILE)


def read_events_tail(limit=100):
    """Read last N lines from events.jsonl."""
    try:
        with open(EVENTS_FILE) as f:
            lines = f.readlines()
        return [l.strip() for l in lines[-limit:] if l.strip()]
    except (FileNotFoundError, IOError):
        return []


def evaluate_trigger(rule, event):
    """Check if a trigger rule matches an event."""
    trigger_type = rule.get("trigger_type", "")
    condition = rule.get("condition", {})
    metric = condition.get("metric", "")
    op = condition.get("op", "")
    value = condition.get("value")

    # Determine which field to check
    event_value = None
    if metric == "severity":
        event_value = event.get("severity", "info")
    elif metric == "event_type":
        event_value = event.get("event_type", "")
    elif metric == "source":
        event_value = event.get("source", "")
    elif metric == "failed_dgx":
        state = read_state("dgx")
        event_value = not state.get("ok", True) if state else False
    elif metric == "vllm_down":
        state = read_state("dgx")
        event_value = not state.get("vllm_healthy", True) if state else False
    elif metric == "watchdog_blocked":
        state = read_state("watchdog")
        event_value = state.get("blocked", False) if state else False
    elif metric.startswith("state."):
        stream = metric[6:]
        state = read_state(stream)
        if state:
            detail = state.get("details", {})
            event_value = detail.get(metric[6:], None)
        else:
            event_value = None

    if event_value is None:
        return False

    # Evaluate comparison
    try:
        if op == "==":
            return str(event_value) == str(value)
        elif op == "!=":
            return str(event_value) != str(value)
        elif op == ">":
            return float(event_value) > float(value)
        elif op == ">=":
            return float(event_value) >= float(value)
        elif op == "<":
            return float(event_value) < float(value)
        elif op == "<=":
            return float(event_value) <= float(value)
        elif op == "contains":
            return str(value) in str(event_value)
        elif op == "matches":
            return bool(re.match(str(value), str(event_value)))
    except (ValueError, TypeError):
        return False

    return False


def _intentional_autodown(watchdog_block_fn=None):
    """True when an intentional autodown is in effect (§5 C2).

    The serving layer's orchestrator is deliberately down by autodown when the
    watchdog block carries ``intentional == \"autodown\"``. Consult the SAME
    single source of truth as the watchdog's intentional-aware fork
    (lifecycle.pipeline_watchdog), never a parallel rule. Fail-safe: an
    unreadable/absent block (or a None loader) is NOT treated as intentional —
    only a positively-asserted ``intentional: \"autodown\"`` marker suppresses
    an automated restart.
    """
    if watchdog_block_fn is None:
        from .lifecycle import load_watchdog_block
        watchdog_block_fn = load_watchdog_block
    try:
        block = watchdog_block_fn()
    except Exception:
        return False
    return bool(block) and block.get("intentional") == "autodown"


def fire_trigger_action(rule, event, watchdog_block_fn=None, restart_vllm_fn=None):
    """Fire the action defined by a trigger rule."""
    rule_id = rule.get("id", "?")
    trigger_params = rule.get("trigger_params", {})
    action_type = rule.get("trigger_type", "")

    if action_type == "notify":
        title = trigger_params.get("title", f"Trigger: {rule_id}")
        body = trigger_params.get("body", f"Rule {rule_id} fired")
        send_macos_notification(title, body, priority="normal")

    elif action_type == "emit_event":
        event_type = trigger_params.get("event_type", f"trigger.{rule_id}")
        payload = {**trigger_params.get("payload", {}), "trigger_rule": rule_id,
                    "source_event": event}
        emit_event(event_type, payload, source="trigger_engine")

    elif action_type == "auto_restart":
        # §5 C2: an intentional autodown must suppress automated restarts from
        # EVERY path. This trigger engine runs every 15s and its ``vllm_down``
        # / ``failed_dgx`` metrics read True while the orchestrator is down, so
        # an auto_restart rule on those would otherwise resurrect a
        # deliberately-down layer — independent of the watchdog. Gate on the
        # same intentional marker the watchdog's fork consults.
        if _intentional_autodown(watchdog_block_fn):
            log(f"Trigger {rule_id}: auto_restart suppressed — intentional autodown")
            return
        if restart_vllm_fn:
            restart_result = restart_vllm_fn()
            send_macos_notification(
                "HSCC Auto-Restart",
                f"Trigger {rule_id} triggered vLLM restart: {'OK' if restart_result.get('ok') else 'FAILED'}",
                priority="high"
            )

    elif action_type == "block_pipeline":
        if watchdog_block_fn:
            block = watchdog_block_fn()
            block["blocked"] = True
            block["blocked_at"] = now_iso()
            block["reason"] = f"Trigger rule {rule_id} triggered block"
            save_watchdog_block(block)
            send_macos_notification(
                "HSCC Pipeline Blocked",
                f"Trigger rule {rule_id} blocked the pipeline: {block['reason']}",
                priority="critical"
            )


def trigger_engine(check_dgx_fn=None, check_gateway_fn=None,
                   pipeline_watchdog_fn=None, watchdog_block_fn=None,
                   restart_vllm_fn=None):
    """Evaluate all trigger rules against recent events and state checks."""
    rules = load_triggers()
    if not rules:
        write_state("triggers", {
            "ok": True,
            "rules_evaluated": 0,
            "actions_fired": 0,
            "last_check": now_iso(),
            "message": "No trigger rules configured",
        })
        return True

    cooldowns = load_cooldowns()
    actions_fired = 0

    # 1. Check recent events
    event_lines = read_events_tail(limit=50)
    recent_events = []
    for line in event_lines:
        try:
            recent_events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # 2. Also check current state for state-based triggers
    state_snapshots = read_all_states()

    targets = list(recent_events)

    # Add state-based "pseudo-events" for state-triggered rules
    for stream, state_data in state_snapshots.items():
        if state_data and "ok" in state_data and not state_data.get("ok"):
            targets.append({
                "event_type": f"state.{stream}.degraded",
                "severity": "warning",
                "source": "daemon_trigger_engine",
                "stream": stream,
                "state": state_data,
                "_source_event": False,
            })

    for rule in rules:
        if not rule.get("enabled", True):
            continue

        rule_id = rule.get("id", "")
        cooldown = rule.get("cooldown_seconds", 0)
        now = time.time()

        # Check cooldown
        if cooldown > 0 and rule_id in cooldowns:
            last_fired = cooldowns[rule_id]
            if now - last_fired < cooldown:
                continue

        # Evaluate against each target
        for target in targets:
            if evaluate_trigger(rule, target):
                fire_trigger_action(
                    rule, target,
                    watchdog_block_fn=watchdog_block_fn,
                    restart_vllm_fn=restart_vllm_fn,
                )
                if cooldown > 0:
                    cooldowns[rule_id] = now
                    save_cooldowns(cooldowns)
                actions_fired += 1
                break  # one match per rule per cycle

    write_state("triggers", {
        "ok": True,
        "rules_evaluated": len(rules),
        "events_checked": len(recent_events),
        "actions_fired": actions_fired,
        "last_check": now_iso(),
        "message": f"Evaluated {len(rules)} rules, fired {actions_fired} actions",
    })
    return True
