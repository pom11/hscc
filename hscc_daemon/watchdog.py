"""Watchdog + triggers — block state, pipeline, rule engine."""

import json
import datetime
import time
import os

from hscc import read_state, read_all_states, log, now_iso, write_state, send_macos_notification, notify_operations


def load_watchdog_block():
    """Load the block state file."""
    from hscc import WATCHDOG_BLOCK_FILE
    try:
        with open(WATCHDOG_BLOCK_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"blocked": False, "reason": "", "blocked_at": None, "failures": [], "auto_restart_count": 0}


def save_watchdog_block(data):
    """Save the block state file."""
    from hscc import WATCHDOG_BLOCK_FILE
    tmp = WATCHDOG_BLOCK_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, WATCHDOG_BLOCK_FILE)


def cleanup_old_failures(failures, window_minutes=10):
    """Keep only failures within the last window_minutes."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=window_minutes)
    result = []
    for f in failures:
        ts = f.get("timestamp", "")
        if not ts:
            result.append(f)
            continue
        try:
            entry_time = datetime.datetime.fromisoformat(ts).replace(tzinfo=datetime.timezone.utc)
            if entry_time > cutoff:
                result.append(f)
        except (ValueError, TypeError):
            result.append(f)
    return result


FAILURE_HISTORY_KEY = "watchdog_failures"


def pipeline_watchdog():
    """Watchdog cycle (every 30s): check DGX+gateway, auto-restart vLLM, block on 3 failures."""
    log("Running PipelineWatchdog")

    block = load_watchdog_block()

    # If currently blocked, don't run checks, just report
    if block.get("blocked"):
        log("Watchdog: blocked, skipping checks")
        write_state("watchdog", {
            "ok": False,
            "blocked": True,
            "reason": block.get("reason", ""),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "last_check": now_iso(),
            "message": f"Pipeline blocked: {block['reason']}",
        })
        return False

    # Run DGX + gateway checks
    try:
        from hscc import check_dgx, check_gateway, restart_vllm, VLLM_LOAD_GRACE_MINUTES
        from hscc import PRIMARY_NODE
    except ImportError:
        log("Watchdog: dependencies unavailable", "ERROR")
        return False

    dgx_ok = check_dgx()
    gw_ok = check_gateway()

    if dgx_ok and gw_ok:
        # Success — reset failure history if within window
        success_entry = {"timestamp": now_iso(), "dgx": True, "gateway": True}
        failures = block.get("failures", [])
        failures.append(success_entry)
        block["failures"] = cleanup_old_failures(failures, window_minutes=10)
        block["failed_count"] = 0
        block.pop("restart_cooldown_until", None)
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": True,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "message": "Pipeline healthy",
            "auto_restart_count": block.get("auto_restart_count", 0),
        })
        log("Watchdog: pipeline healthy")
        return True

    # Failure detected. If a restart is still within its load-grace window
    cooldown_until = block.get("restart_cooldown_until")
    if cooldown_until and now_iso() < cooldown_until:
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "message": f"vLLM restarting — model loading (grace until {cooldown_until})",
        })
        log(f"Watchdog: in restart grace window until {cooldown_until}, model still loading — not counting failure")
        return False

    # Failure — record it
    failure_entry = {"timestamp": now_iso(), "dgx": dgx_ok, "gateway": gw_ok}
    failures = block.get("failures", [])
    failures.append(failure_entry)
    block["failures"] = cleanup_old_failures(failures, window_minutes=10)
    block["failed_count"] = len(block["failures"])

    recent = [f for f in block["failures"] if not f.get("dgx", True) or not f.get("gateway", True)]

    if len(recent) >= 3:
        # BLOCK
        block["blocked"] = True
        block["blocked_at"] = now_iso()
        reason = f"3 consecutive failures in 10min: DGX={'OK' if dgx_ok else 'FAIL'} GW={'OK' if gw_ok else 'FAIL'}"
        block["reason"] = reason
        log(f"Watchdog: BLOCKING pipeline — {reason}")
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": True,
            "reason": reason,
            "last_check": now_iso(),
            "message": "PIPELINE BLOCKED — manual intervention required",
            "auto_restart_count": block.get("auto_restart_count", 0),
        })
        send_macos_notification(
            "🚨 HSCC Pipeline Blocked",
            reason,
            priority="critical",
        )
        return False

    # 1-2 failures — try auto-restart vLLM
    if not dgx_ok:
        log("Watchdog: attempting vLLM auto-restart via sparkrun")
        restart_result = restart_vllm()
        restart_ok = restart_result.get("success", False)
        count = block.get("auto_restart_count", 0) + 1
        block["auto_restart_count"] = count
        block["last_restart"] = now_iso()
        if restart_ok:
            block["restart_cooldown_until"] = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(minutes=VLLM_LOAD_GRACE_MINUTES)
            ).isoformat()
        save_watchdog_block(block)
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "auto_restart": True,
            "restart_result": restart_result.get("success", False),
            "restart_output": restart_result.get("output", "")[:200],
            "auto_restart_count": count,
            "last_check": now_iso(),
            "message": f"Auto-restart #{count} attempted",
        })
        log(f"Watchdog: vLLM auto-restart #{count}: {'success' if restart_ok else 'failed'}")
        send_macos_notification(
            "⚠️ HSCC vLLM Auto-Restart",
            f"Auto-restart #{count} of vLLM attempted on {PRIMARY_NODE}: {'OK' if restart_ok else 'FAILED'}",
            priority="high",
        )
    else:
        write_state("watchdog", {
            "ok": False,
            "blocked": False,
            "dgx": dgx_ok,
            "gateway": gw_ok,
            "last_check": now_iso(),
            "auto_restart_count": block.get("auto_restart_count", 0),
            "message": "Degraded — gateway not reachable",
        })

    return not (not dgx_ok and not gw_ok)


# ── Trigger Engine ─────────────────────────────────────────────────────────

TRIGGERS_FILE = None  # set at module load from hscc


def load_triggers():
    """Load trigger rules from triggers.json."""
    from hscc import TRIGGERS_FILE
    try:
        with open(TRIGGERS_FILE) as f:
            data = json.load(f)
        return data.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_cooldowns():
    """Load cooldown timestamps."""
    from hscc import COOLDOWN_FILE
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cooldowns(data):
    """Save cooldown timestamps."""
    from hscc import COOLDOWN_FILE
    tmp = COOLDOWN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, COOLDOWN_FILE)


def read_events_tail(limit=100):
    """Read last N lines from events.jsonl."""
    from hscc import EVENTS_FILE
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
            import re
            return bool(re.match(str(value), str(event_value)))
    except (ValueError, TypeError):
        return False

    return False


def fire_trigger_action(rule, event):
    """Fire the action defined by a trigger rule."""
    from hscc import send_macos_notification, emit_event, notify_operations, restart_vllm
    from hscc import load_watchdog_block, save_watchdog_block, now_iso

    rule_id = rule.get("id", "?")
    trigger_params = rule.get("trigger_params", {})
    action_type = rule.get("trigger_type", "")

    if action_type == "notify":
        title = trigger_params.get("title", f"Trigger: {rule_id}")
        body = trigger_params.get("body", f"Rule {rule_id} fired")
        send_macos_notification(title, body, priority="normal")
        log(f"Trigger {rule_id}: notification sent — {title}")

    elif action_type == "emit_event":
        event_type = trigger_params.get("event_type", f"trigger.{rule_id}")
        payload = {**trigger_params.get("payload", {}), "trigger_rule": rule_id,
                    "source_event": event}
        emit_event(event_type, payload, source="trigger_engine")
        log(f"Trigger {rule_id}: event emitted — {event_type}")

    elif action_type == "auto_restart":
        restart_result = restart_vllm()
        log(f"Trigger {rule_id}: auto-restart vLLM {'success' if restart_result.get('success') else 'failed'}")
        send_macos_notification("⚠️ HSCC Auto-Restart",
                                f"Trigger {rule_id} triggered vLLM restart: {'OK' if restart_result.get('success') else 'FAILED'}",
                                priority="high")

    elif action_type == "block_pipeline":
        block = load_watchdog_block()
        block["blocked"] = True
        block["blocked_at"] = now_iso()
        block["reason"] = f"Trigger rule {rule_id} triggered block"
        save_watchdog_block(block)
        log(f"Trigger {rule_id}: pipeline BLOCKED")
        send_macos_notification("🚨 HSCC Pipeline Blocked",
                                f"Trigger rule {rule_id} blocked the pipeline: {block['reason']}",
                                priority="critical")


def trigger_engine():
    """Evaluate all trigger rules against recent events and state checks."""
    log("Running TriggerEngine")

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

    log(f"TriggerEngine: evaluating {len(rules)} rules against {len(targets)} events/states")

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
                log(f"TriggerEngine: rule {rule_id} on cooldown ({int(now - last_fired)}/{cooldown}s)")
                continue

        # Evaluate against each target
        for target in targets:
            if evaluate_trigger(rule, target):
                log(f"TriggerEngine: rule {rule_id} matched!")
                fire_trigger_action(rule, target)
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
    log(f"TriggerEngine complete: {actions_fired} actions fired")
    return True
