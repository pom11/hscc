"""Trigger engine — rule evaluation, cooldowns, and event-based firing."""

import json
import os
import re
import time

from . import node_pauses
from .state import read_state, read_all_states, now_iso, write_state
from .lifecycle import save_watchdog_block
from .desktop import send_macos_notification, emit_event
from .daemon_ops import log


EVENTS_FILE = os.path.expanduser("~/.hscc/events.jsonl")
TRIGGERS_FILE = os.path.expanduser("~/.hscc/triggers.json")
COOLDOWN_FILE = os.path.expanduser("~/.hscc/cooldowns.json")

# Repeat-suppression applied to a rule that NEVER declared a cooldown_seconds,
# for matches that reflect a persisting STATE rather than a discrete event.
# The trigger loop runs every tick of the `triggers` stream (cadence =
# PERIODIC_INTERVALS["triggers"], the SOT since GAP-B-6; 15 s) and a state
# check stays ok:False for as long as the fault lasts, so a rule with no
# cooldown re-notified once per tick: the 2026-10-08 lost NAS mount fired 178
# macOS notifications in 11 minutes. Shipped defaults now carry cooldowns
# (hscc-bootstrap/triggers.default.json), but bootstrap only ever ADDS missing
# rule ids — it cannot retro-fit a cooldown into a live triggers.json that
# already has them — so this floor is what actually dampens an existing
# install. Explicit `cooldown_seconds: 0` remains the operator's deliberate
# "notify every cycle" and is honoured; only an absent/unparseable value falls
# back to the floor, so a typo cannot re-open the storm. Only STATE-derived
# matches are floored (see _is_state_signal): a discrete event is never lost.
STATE_NOTIFY_FLOOR_SECONDS = 1800

# Metrics that evaluate_trigger() answers by reading a stream-state file
# instead of the event in hand: every tick the bad state matches again, so a
# match on one is a STATE signal, not an event.
_STATE_METRICS = ("failed_dgx", "vllm_down", "watchdog_blocked", "nas_down")

# The SERVING-LAYER signals — the three notify rules in
# hscc-bootstrap/triggers.default.json whose condition says "the inference
# stack is not serving": `orch-dgx-down` (failed_dgx), `vllm-down`
# (vllm_down) and `engine-wedge-detected` (state.engine_wedge.degraded).
# Their notify stands down while the fleet is deliberately down (STAY-DOWN-
# NOTIFY, t_50c6edc4), because a deliberate teardown makes all three true by
# construction and paging about it contradicts the instruction that caused it.
# `watchdog_blocked` and `nas_down` are NOT here on purpose: a tripped pipeline
# watchdog and a lost NAS mount are not fleet-serving signals and must keep
# paging while the fleet is down.
_OPERATOR_DOWN_SUPPRESSED_METRICS = {
    "failed_dgx",
    "vllm_down",
    "state.engine_wedge.degraded",
}


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


def _effective_cooldown(rule):
    """Declared ``cooldown_seconds`` as an int, or None when not declared.

    ``None`` means the key is absent, ``null``, or holds something that is not
    a number — the case the state-notify floor exists for. An explicit
    ``0`` parses to ``0`` and stays ``0``: that is an operator's deliberate
    "notify every cycle", not an omission.
    """
    raw = rule.get("cooldown_seconds")
    if raw is None:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _is_state_signal(rule, target):
    """True when a (rule, target) match reflects a PERSISTING STATE rather than
    a discrete event.

    Either the target is one of the ``state.<stream>.degraded`` entries the
    engine re-derives from live stream files on EVERY tick (nothing else ever
    emits that event_type, so the name alone identifies it — an operator rule
    matching on it, like the shipped ``engine-wedge-detected``, is matched
    against a persisting condition either way), or the rule's metric is one
    ``evaluate_trigger`` answers by reading a stream-state file, in which case
    the same bad state matches again on every tick regardless of which target
    produced the match.
    """
    if str(target.get("event_type", "")).startswith("state."):
        return True
    metric = (rule.get("condition") or {}).get("metric", "")
    return metric in _STATE_METRICS or metric.startswith("state.")


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
    elif metric == "nas_down":
        # The nas check writes ok=False the moment the share stops being a real
        # mount. Without this metric there was no rule that could fire on it, so
        # a lost NAS was recorded and never surfaced: the 2026-10-08 NAS reboot
        # left all five clients unmounted and nas.json ok=False for ~2h with
        # nobody alerted. Mirrors failed_dgx (absent state => not down, so a
        # daemon that has not run the check yet does not alert).
        state = read_state("nas")
        event_value = not state.get("ok", True) if state else False
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


def _operator_down(deliberately_down_fn=None):
    """The durable stay-down record {set_at, reason}, or None when there is none.

    STAY-DOWN-NOTIFY (t_50c6edc4): `hscc cluster down` is an INSTRUCTION, not a
    fault, yet daemon.log 2026-11-08 17:52:19 shows the daemon paging "HSCC:
    orchestrator/DGX unhealthy" ~1 min after the operator tore the fleet down.
    The gate reads the ONE predicate D1 established —
    :func:`post_boot.deliberately_down`, a record inside
    ``~/.hscc/expected-up.json`` — through the same injectable-seam shape as
    this function's neighbours already use for :func:`fire_trigger_action`
    (``watchdog_block_fn`` / ``restart_vllm_fn``), so tests never touch
    ``~/.hscc``. No parallel flag, nothing cached in memory: the record is on
    disk or it does not exist.

    WHICH WRITERS THIS TREATS AS A STAY-DOWN (decided here, never by string
    match): ``post_boot.clear_expected_up(reason)`` has TWO sanctioned callers —
    ``hscc-cluster/hscc.py`` (``"hscc cluster down"``, the operator) and
    ``hscc_daemon/autodown.py`` (``"autodown: intentional idle teardown"``, the
    fleet went idle) — so the record means "someone deliberately cleared the
    footprint", NOT "an operator typed the verb". BOTH are honoured: a
    serving-layer page is noise on either, and the idle-teardown path — unlike
    the healing checks in health.py, which get there first through
    ``_intentional_window()`` — has no other notify gate. The ``reason`` TEXT is
    carried into the log line and nowhere else; no substring of it is ever
    tested, because that would make an operator-chosen string a safety
    property. Both producer wordings are pinned verbatim in
    tests/test_trigger.py::test_both_producer_wordings_suppress.

    Fail-safe toward PAGING — the deliberate opposite direction from
    :func:`_intentional_autodown` (which fails toward restarting, because there
    a wrong guess costs a resurrection and here it costs an alert): a raising
    predicate, a non-dict, or a record missing either field reads as NO
    evidence and the notification goes out exactly as it did before the record
    existed. Absent evidence must never be able to silence an operator.
    """
    if deliberately_down_fn is None:
        from . import post_boot
        deliberately_down_fn = post_boot.deliberately_down
    try:
        record = deliberately_down_fn()
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    # Both fields are stamped by every sanctioned writer
    # (post_boot.clear_expected_up); a record without them is not a record this
    # gate can vouch for, so it fails toward paging rather than quiet.
    if not record.get("set_at") or not record.get("reason"):
        return None
    return record


def _serving_layer_notify(rule):
    """True when a rule's condition is one of the three serving-layer signals.

    Reads the metric from ``rule["condition"]["metric"]`` as D3 specifies, with
    one documented addition: the shipped ``engine-wedge-detected`` rule keys on
    ``metric == "event_type"`` and carries its signal in ``condition.value``, so
    for that rule the serving-layer name lives in the VALUE, not the metric.
    Matching the metric alone would let the wedge rule page straight through the
    gate while claiming to be one of the three rules the constant names.

    Nothing else can match: ``nas_down`` / ``watchdog_blocked`` are excluded by
    the constant itself, and this is only ever consulted from the ``notify``
    branch, so ``emit_event`` and ``block_pipeline`` are structurally untouched.
    """
    condition = rule.get("condition") or {}
    metric = condition.get("metric", "")
    if metric in _OPERATOR_DOWN_SUPPRESSED_METRICS:
        return True
    return (metric == "event_type"
            and str(condition.get("value")) in _OPERATOR_DOWN_SUPPRESSED_METRICS)


# ── NODE-PAUSE NOTIFY GATE (t_81de1863) ────────────────────────────────────
#
# WHY THIS GATE EXISTS
#
# ``hscc node pause`` parks ONE node and edits nothing the cluster is built
# from, so the node's units are down with nothing the notify paths look at
# saying "deliberate". The serving-layer metrics go true BY CONSTRUCTION while
# its node is parked — exactly the shape STAY-DOWN-NOTIFY was written for — but
# the stay-down record is a FLEET-WIDE instruction (``expected-up.json`` written
# by ``clear_expected_up``), and a per-node pause deliberately never writes it:
# parking one worker must not silence a gateway page for an unrelated node.
# Without this gate the operator's own command pages them ("HSCC:
# orchestrator/DGX unhealthy"), which is the same contradiction t_50c6edc4
# closed for ``hscc cluster down``, one level finer.
#
# WHY THE DECISION LIVES HERE AND NOT IN THE RULES
#
# A rule cannot express it: the shipped rules match metrics, not hosts, and
# ``hscc-bootstrap`` only ever ADDS missing rule ids — it cannot retro-fit a
# condition into a live ``triggers.json``. So the skip belongs in the one place
# every notify passes through, beside the gate this one is modelled on.
#
# WHICH NODE — AND WHY THIS IS NOT "stay quiet whenever anything is parked"
#
# See :func:`_signal_host`. A signal that does not name a node is never
# suppressed by it, and a pause on a node the signal is NOT about never
# silences it. Over-suppression here would be the same class of bug as the
# fail-open liveness checks this package keeps removing: an operator who parked
# a worker would silently lose the NAS-mount page.
#
# ASK ``covering``, NEVER ``paused``
#
# A tensor-parallel unit stops as one thing, so the nodes named only in the
# pause record's ``span_nodes`` are just as parked as the keyed host — and their
# health check degrades too. Same pin the watchdog seam (t_4ecbd9d0) and the
# heal-breaker seam (t_e6ee7e60) make.
#
# FAIL-SAFE TOWARD PAGING — the same direction as :func:`_operator_down`
#
# A missing or malformed overlay makes ``node_pauses`` answer "no pauses" (and
# WARN), so the page goes out exactly as it did before the overlay existed;
# unparseable ``until`` is read as open-ended by that module, so an expired
# pause keeps suppressing until the daemon's own auto-resume clears it —
# conservative in the only direction that cannot cost an operator an alert.
# There is deliberately no try/except around the read: ``node_pauses``' read
# path is documented never to raise, and heal_breaker.py makes the same call
# for the same reason.
#
# NO STALE SUPPRESSION
#
# The overlay on disk is the only state this gate has — nothing is cached in
# memory, nothing is written to the feed or the cooldown ledger by the skip, so
# ``node_pauses.clear(host)`` (or the daemon's expiry pass) restores alerting
# on the next call, and a daemon restart cannot lose or invent a suppression.
# The rule's own cooldown is a SEPARATE, pre-existing silence: ``trigger_engine``
# stamps it after ``fire_trigger_action`` returns whether or not the notify was
# suppressed (that is what dampened the 2026-10-08 storm of 178 notifications in
# 11 minutes), and this gate leaves that behaviour untouched — see
# test_the_suppressed_tick_still_stamps_the_rule_cooldown.

#: The field names a health record uses to say which node it is about.
#: ``node`` is this package's own spelling for a per-node/unit record
#: (health.py's unit rows, ``heal_breaker.tripped``'s payload); ``host`` is the
#: spelling a client/mount record uses. Nothing else is scraped: a title or a
#: body that happens to contain an IP is NOT evidence of what a signal is about.
_PAUSE_HOST_KEYS = ("node", "host")


def _clean_host(value):
    """A usable host string, or ``None``. Anything non-string or blank is
    "this record does not name a node" — never a guess."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _signal_host(rule, target, primary_node_fn=None):
    """The node a ``(rule, target)`` match is ABOUT, or ``None`` when it names none.

    Precedence, and nothing else:

    1. the target's own ``node``/``host``, then the same keys inside
       ``payload`` — the envelope :func:`desktop.emit_event` writes, which is
       where a discrete per-node event puts its host;
    2. otherwise, when the rule's condition is one of the serving-layer signals
       in :data:`_OPERATOR_DOWN_SUPPRESSED_METRICS`, the serving node
       (``serving.PRIMARY_NODE``) — that is the node those checks answer for,
       the same node the watchdog's heal seam asks about;
    3. otherwise ``None``: a signal that names no node is never suppressed.

    Never raises: a non-dict target, a non-dict ``payload`` or a junk host
    value all answer "no host named here" (fail-safe toward paging). Step 2 is
    deliberately NOT gated on the target being readable: a state-metric rule
    like ``failed_dgx`` answers itself from the stream-state file and never
    looks at the target at all, and the degraded-state pseudo-event the engine
    builds for it carries a stream name, not a host — the metric is what
    answers for the serving node. ``primary_node_fn`` is the seam for step 2 —
    the real loop reads the live topology, tests hand a fake, and it is called
    only when the rule really is a serving-layer signal, so a notify rule on
    some other metric never triggers topology resolution.
    """
    if isinstance(target, dict):
        for container in (target, target.get("payload")):
            if not isinstance(container, dict):
                continue
            for key in _PAUSE_HOST_KEYS:
                host = _clean_host(container.get(key))
                if host:
                    return host
    if _serving_layer_notify(rule if isinstance(rule, dict) else {}):
        if primary_node_fn is None:
            from . import serving
            return _clean_host(serving.PRIMARY_NODE)
        try:
            return _clean_host(primary_node_fn())
        except Exception:
            # Fail-safe toward paging: a topology that cannot be read names no
            # node, so the page goes out instead of hiding behind a guess.
            return None
    return None


def _node_pause_for_signal(rule, target, directory=None, primary_node_fn=None):
    """``(host, entry)`` when the node this signal is about is parked.

    ``(None, None)`` when nothing is suppressed: no node attributable, or the
    overlay does not cover it. ``directory`` is ``node_pauses``' own
    explicit-root seam — the only relocation seam that module has, and the
    reason this takes a parameter rather than an env var (an env root is
    per-process, so it is not a root the CLI that wrote the pause can share).
    """
    host = _signal_host(rule, target, primary_node_fn=primary_node_fn)
    if host is None:
        return None, None
    entry = node_pauses.covering(host, directory=directory)
    if entry is None:
        return None, None
    return host, entry


def _pause_label(entry):
    """``" (reason)"`` for a pause entry, ``""`` when it carries no reason."""
    reason = ((entry or {}).get("reason") or "").strip()
    return f" ({reason})" if reason else ""


def fire_trigger_action(rule, event, watchdog_block_fn=None, restart_vllm_fn=None,
                        *, deliberately_down_fn=None, pauses_directory=None,
                        primary_node_fn=None):
    """Fire the action defined by a trigger rule.

    ``deliberately_down_fn`` is the STAY-DOWN-NOTIFY seam (see
    :func:`_operator_down`): keyword-only and defaulted, so every existing
    caller keeps its behaviour and the real loop gets the real predicate.

    ``pauses_directory`` / ``primary_node_fn`` are the node-pause notify seam
    (see the block above :func:`_signal_host`): keyword-only and defaulted like
    every other seam here, so the real loop passes nothing and reads the live
    overlay and the live topology.
    """
    rule_id = rule.get("id", "?")
    trigger_params = rule.get("trigger_params", {})
    action_type = rule.get("trigger_type", "")

    if action_type == "notify":
        # STAY-DOWN-NOTIFY (t_50c6edc4): a deliberate teardown makes the
        # serving-layer metrics true BY CONSTRUCTION, so paging on them
        # contradicts the instruction that caused it. Gate BEFORE the
        # notification, and only for the serving-layer metrics — never notify
        # globally. This is NOT the auto_restart gate below and the two are
        # deliberately kept separate: that one asks "is an automated restart
        # wanted right now?" (the autodown watchdog block), this one asks "did
        # someone deliberately take the fleet down?" (the stay-down record).
        if _serving_layer_notify(rule):
            down = _operator_down(deliberately_down_fn)
            if down:
                log(f"Trigger {rule_id}: notify suppressed — "
                    f"fleet deliberately down ({down.get('reason')})")
                return
        # NODE-PAUSE (t_81de1863): the node this signal is about is parked by
        # the operator, so its absence/down is the instruction, not a fault.
        # AFTER the stay-down gate on purpose — a fleet-wide stay-down is the
        # broader reason and keeps its wording — and ONLY on this notify
        # branch: auto_restart and block_pipeline are not this seam's business
        # (restart_vllm's rule 0 and the heal breaker already honour the
        # overlay), so an operator rule that blocks the pipeline still does.
        paused_host, pause = _node_pause_for_signal(
            rule, event, directory=pauses_directory,
            primary_node_fn=primary_node_fn)
        if pause is not None:
            log(f"Trigger {rule_id}: notify suppressed — node {paused_host} "
                f"is paused{_pause_label(pause)}")
            return
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
        # EVERY path. This trigger engine runs every `triggers` tick
        # (PERIODIC_INTERVALS["triggers"]) and its ``vllm_down``
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


def trigger_engine(check_gateway_fn=None,
                   pipeline_watchdog_fn=None, watchdog_block_fn=None,
                   restart_vllm_fn=None, *, deliberately_down_fn=None,
                   pauses_directory=None, primary_node_fn=None):
    """Evaluate all trigger rules against recent events and state checks.

    ``deliberately_down_fn`` is the STAY-DOWN-NOTIFY seam, threaded straight to
    :func:`fire_trigger_action` — the real loop (`daemon_ops.run_trigger_loop`)
    passes nothing and therefore reads the real
    :func:`post_boot.deliberately_down`. Keyword-only and defaulted like every
    other seam here, so existing callers are unaffected.

    ``pauses_directory`` / ``primary_node_fn`` are the node-pause notify seam
    (t_81de1863), threaded straight to :func:`fire_trigger_action` the same way
    — the real loop passes neither and therefore reads the real
    ``~/.hscc/node_pauses.json`` and the real topology.

    Runs NO health check. Its DGX/gateway/NAS signals (`failed_dgx`,
    `vllm_down`, `watchdog_blocked`, `nas_down`, `state.<stream>`) are answered
    by `evaluate_trigger` reading the stream STATE FILES, which the checks' own
    threads stamp — so this 15 s loop needs no probe of its own and must never
    gain one. It used to carry a `check_dgx_fn` parameter that
    `daemon_ops.run_trigger_loop` dutifully wired to the real `check_dgx`; the
    body never invoked it (0 call sites, which is why removing it changes no
    behaviour), but the wiring is what led the 2026-10-31 proxy-stall audit to
    count a fourth in-process caller of the most expensive probe in the daemon.
    See `_DGX_VERDICT` in health.py for the de-multiplication and why DGX must
    have exactly one caller. `check_gateway_fn` / `pipeline_watchdog_fn` are
    likewise unused here (kept: out of scope for this card).
    """
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
        declared = _effective_cooldown(rule)
        # `declared is None` == the rule never set cooldown_seconds. Such a
        # rule still gets the state-notify floor, but ONLY for state-derived
        # matches (see _is_state_signal), so a discrete event is never lost.
        floor = STATE_NOTIFY_FLOOR_SECONDS if declared is None else 0
        cooldown = declared if declared is not None else 0
        now = time.time()

        # Check cooldown
        if cooldown > 0 and rule_id in cooldowns:
            last_fired = cooldowns[rule_id]
            if now - last_fired < cooldown:
                continue

        # Evaluate against each target
        for target in targets:
            if not evaluate_trigger(rule, target):
                continue
            state_repeat = _is_state_signal(rule, target)
            if floor and state_repeat:
                last_fired = cooldowns.get(rule_id)
                if last_fired is not None and now - last_fired < floor:
                    continue  # repeat of a persisting state — keep looking:
                    # a genuine (non-state) match must still fire
            fire_trigger_action(
                rule, target,
                watchdog_block_fn=watchdog_block_fn,
                restart_vllm_fn=restart_vllm_fn,
                deliberately_down_fn=deliberately_down_fn,
                pauses_directory=pauses_directory,
                primary_node_fn=primary_node_fn,
            )
            # Timestamp what the suppression logic consults, and only that: a
            # discrete event fire must not later silence a degradation alert
            # through the floor, and a rule with an explicit 0 has nothing to
            # consult, so leave cooldowns.json alone (pre-fix behaviour).
            if cooldown > 0 or (floor and state_repeat):
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
