"""A parked node must not page anyone: the trigger engine's notify gate.

WHY THIS EXISTS
---------------
``hscc node pause`` parks ONE node on purpose and edits nothing the cluster is
built from, so that node's units are down with no record anywhere the notify
paths look saying "deliberate". The serving-layer rules
(``hscc-bootstrap/triggers.default.json``: ``orch-dgx-down``, ``vllm-down``,
``engine-wedge-detected``) answer for a node, and while that node is parked
their metrics are true BY CONSTRUCTION — the exact shape STAY-DOWN-NOTIFY
(t_50c6edc4) was written for, one level finer. The stay-down record cannot cover
it: it is a FLEET-WIDE instruction (``expected-up.json`` from
``clear_expected_up``), and a per-node pause must never write it, because
parking one worker must not silence a gateway page for a node the pause did not
cause. Without this gate the operator's own command pages them.

THE BOTH-HALVES RULE
--------------------
A skip that always skips is worthless, so every property here is pinned from
both sides: the parked node's absence/down is silent, AND an unpaused node, a
node beside the parked one, or a signal that names no node pages EXACTLY as
before. Over-suppression in a notify path is the same class of bug as the false
page it removes — an operator who parked a worker would silently lose the
NAS-mount page.

ASK ``covering``, NEVER ``paused``
----------------------------------
``node_pauses.paused(host)`` is host-keyed; the other nodes of a
tensor-parallel unit appear only in the record's ``span_nodes``, and their
health degrades too. ``test_span_sibling_pause_silences_the_serving_page``
asserts the two APIs answer DIFFERENTLY for the parked node and that the
behaviour follows the ``covering`` answer, so swapping ``covering()`` for
``paused()`` reddens this file. Same pin the watchdog seam (t_4ecbd9d0) and the
heal-breaker seam (t_e6ee7e60) make.

WHAT THIS SEAM DELIBERATELY DOES NOT TOUCH
------------------------------------------
Only the ``notify`` branch. ``auto_restart`` and ``block_pipeline`` keep their
own (separate) relationships with the overlay — ``restart_vllm``'s rule 0
(t_4ecbd9d0) and the heal breaker (#568) — and an operator rule that blocks the
pipeline still blocks it while a node is parked. Both boundaries are pinned
below so a later "harden it too" edit has to argue with a test.

THE COOLDOWN IS NOT THIS GATE'S SILENCE
--------------------------------------
``trigger_engine`` stamps a rule's cooldown after ``fire_trigger_action``
returns whether or not the notify was suppressed. That is pre-existing, shared
with the stay-down gate, and it is what dampened the 2026-10-08 storm of 178
notifications in 11 minutes. The pause therefore adds no state of its own and
no silence beyond the rule's own declared cooldown — pinned by
``test_the_suppressed_tick_still_stamps_the_rule_cooldown`` so nobody later
reads the cooldown as stale pause state, or deletes the stamp and re-opens the
storm.

SAFETY
------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24, documentation space, never
this fleet), every overlay lives in a per-test temp dir passed through
``node_pauses``' own ``directory=`` seam, and serving's topology is the
autouse-hermetic default (``PRIMARY_NODE == 192.0.2.10``), so no test resolves
a real topology. No node, sparkrun, ssh or real ``~/.hscc`` is touched; the two
default-root tests plant their pause at the conftest-sealed root and
guard-assert that root is inside the tmp dir before writing to it.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

from hscc_daemon import node_pauses, trigger

# TEST-NET-1 (RFC 5737) — documentation space, never routable, never this fleet.
# PRIMARY_NODE doubles as PAUSED_NODE because conftest's _hermetic_topology
# pins serving's lazy topology to the documented default, which is the node
# the shipped serving rules answer for.
PAUSED_NODE = "192.0.2.10"
SPAN_SIBLING = "192.0.2.11"
UNRELATED_NODE = "192.0.2.30"


@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root for this test's pauses (node_pauses' only seam)."""
    d = tmp_path / "overlay"
    d.mkdir()
    return str(d)


@pytest.fixture
def env(tmp_hfcc_dir, monkeypatch):
    """Wire state + notifications + log capture to tmp paths.

    Returns (notification_titles, (level, message) lines, emitted_event_types).
    """
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
    return sent, logs, emitted


def default_rule(rule_id):
    """The SHIPPED rule, so the gate and what actually ships cannot drift."""
    here = os.path.dirname(os.path.abspath(__file__))
    defaults = os.path.join(here, "..", "..", "hscc-bootstrap",
                            "triggers.default.json")
    rules = json.load(open(os.path.normpath(defaults)))["rules"]
    match = [r for r in rules if r["id"] == rule_id]
    assert len(match) == 1, f"{rule_id} not in triggers.default.json"
    return match[0]


def pause_line(rule_id, host, reason=""):
    label = f" ({reason})" if reason else ""
    return (f"Trigger {rule_id}: notify suppressed — node {host} "
            f"is paused{label}")


def past_the_deadline():
    """An ISO instant strictly past any pause deadline this file creates.

    DERIVED, not pinned. ``pause(until="1h")`` resolves its deadline against
    ``node_pauses``' OWN clock, so a hard-coded far-future witness (this file passed
    ``2099-01-01T00:00:00+00:00`` twice) is only "past due" while the real clock sits
    below it. t_0389093a measured ``test_expiry_then_the_daemons_clear_restores_the_page``
    red at frozen 2200-01-01 and at ``HSCC_WALL_SHIFT_DAYS=+63225`` — at such a clock the
    pause is genuinely NOT yet expired and the product is right to keep suppressing.
    ±400 days (the shipped ``test_single_clock_rule.py`` travel) never reaches it, and
    this file owns no ``datetime(20xx,`` literal, so the rule never ran it at any shift.
    """
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


class TestThePastDueWitnessIsDerived:
    """Why the expiry witness must be DERIVED here (t_0389093a).

    The shipped clock rule travels ±400 days and cannot see a premise that only breaks
    in 2099, so the bite is a source guard: a pinned instant may not be handed to the
    product as its clock. Re-pinning either expiry call to
    ``2099-01-01T00:00:00+00:00`` reddens this class on any host, immediately.
    """

    def test_no_pinned_instant_is_handed_to_the_product_as_now(self):
        with open(__file__) as fh:
            pinned = [ln.strip() for ln in fh
                      if re.search(r'(now|as_of)\s*=\s*"2\d{3}-', ln)]
        assert not pinned, (
            "a hard-coded far-future instant is being used as the product's clock — "
            "the case is then green only while the real clock sits below it "
            f"(derive it, see past_the_deadline): {pinned}")

    def test_the_witness_is_far_past_any_deadline_this_file_writes(self):
        # `until="1h"` is resolved by the producer against its own clock, so the
        # MARGIN is what makes a pause expired — never the calendar year.
        assert node_pauses._utc(past_the_deadline()) > (
            node_pauses._utc(None) + timedelta(hours=12))


def pause(host, directory, **kw):
    """Record a pause through the real producer, not hand-planted JSON."""
    return node_pauses.pause(host, directory=directory, **kw)


# ── the three serving-layer rules go quiet while their node is parked ──────


class TestServingRulesSilentWhileParked:
    @pytest.mark.parametrize("rule_id", ["orch-dgx-down", "vllm-down"])
    def test_serving_rule_silent_while_its_node_is_parked(self, env, overlay,
                                                          rule_id):
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            default_rule(rule_id), {"event_type": "state.dgx.degraded"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == []
        assert logs == [("INFO", pause_line(rule_id, PAUSED_NODE,
                                            "training run"))]

    def test_engine_wedge_silent_while_its_node_is_parked(self, env, overlay):
        """The shipped wedge rule keys on metric==event_type and carries the
        signal in condition.value, so it only reaches the gate through the
        same value-consulting path _serving_layer_notify uses."""
        rule = default_rule("engine-wedge-detected")
        assert rule["condition"]["metric"] == "event_type"
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            rule, {"event_type": "state.engine_wedge.degraded"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == []
        assert logs == [("INFO", pause_line("engine-wedge-detected",
                                            PAUSED_NODE, "training run"))]

    def test_default_path_reads_the_live_topology(self, env, overlay,
                                                 monkeypatch):
        """No primary_node_fn injected: the gate reads serving.PRIMARY_NODE
        (the autouse-hermetic documented default) — the node those checks
        answer for, exactly as the watchdog's heal seam asks about."""
        from hscc_daemon import serving
        assert serving.PRIMARY_NODE == PAUSED_NODE, (
            "the hermetic topology default moved; this test's premise is the "
            "node the serving rules answer for")
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            pauses_directory=overlay)
        assert sent == []
        assert logs == [("INFO", pause_line("orch-dgx-down", PAUSED_NODE,
                                            "training run"))]

    def test_span_sibling_pause_silences_the_serving_page(self, env, overlay):
        """covering() vs paused(): the pause was issued against the span
        sibling and names PRIMARY_NODE only in span_nodes, so the two APIs
        DISAGREE here — and the behaviour must follow covering()."""
        pause(SPAN_SIBLING, overlay, reason="span parked as one unit",
              span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        assert node_pauses.paused(PAUSED_NODE, directory=overlay) is None
        assert node_pauses.covering(PAUSED_NODE,
                                    directory=overlay) is not None
        sent, logs, _ = env
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == []
        assert logs == [("INFO", pause_line("orch-dgx-down", PAUSED_NODE,
                                            "span parked as one unit"))]

    def test_pause_reason_absent_leaves_no_dangling_parenthesis(self, env,
                                                                overlay):
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay)  # no reason at all
        trigger.fire_trigger_action(
            default_rule("vllm-down"), {"event_type": "x"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == []
        assert logs == [("INFO", pause_line("vllm-down", PAUSED_NODE))]


# ── what must KEEP paging ──────────────────────────────────────────────────


class TestNothingElseIsSilenced:
    def test_pause_on_an_unrelated_node_still_pages(self, env, overlay):
        """The over-suppression pin: parking one worker must not disarm the
        page for the node the serving rules answer for."""
        sent, logs, _ = env
        pause(UNRELATED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []

    def test_rule_naming_no_node_is_never_suppressed(self, env, overlay):
        """A signal that attributes to no node is not the parked node's
        business — even with the overlay holding a node."""
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            default_rule("nas-down"),
            {"event_type": "state.nas.degraded", "severity": "warning"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == ["HSCC: NAS mount lost"]
        assert logs == []

    def test_watchdog_blocked_still_pages_while_a_node_is_parked(self, env,
                                                                 overlay):
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay)
        trigger.fire_trigger_action(
            default_rule("watchdog-blocked"), {"event_type": "x"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == ["HSCC: pipeline watchdog blocked"]
        assert logs == []

    def test_topology_that_cannot_be_read_fails_toward_paging(self, env,
                                                              overlay):
        """Attribution failure is NOT evidence of a pause."""
        def boom():
            raise RuntimeError("topology gone")
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            pauses_directory=overlay, primary_node_fn=boom)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []

    def test_malformed_overlay_fails_toward_paging(self, env, tmp_path,
                                                   monkeypatch):
        """Absent evidence must never silence an operator: node_pauses answers
        'no pauses' (loudly WARNed) for a corrupt file, and the page goes out
        exactly as it did before the overlay existed."""
        root = tmp_path / "broken-overlay"
        root.mkdir()
        path = root / node_pauses.OVERLAY_NAME
        path.write_text("{not json at all")
        # The WARN is node_pauses' own, routed through the daemon's log
        # convention (daemon_ops.log), not through trigger's — capture it where
        # it is actually written.
        from hscc_daemon import daemon_ops
        warns = []
        monkeypatch.setattr(daemon_ops, "log",
                            lambda msg, level="INFO", **kw:
                            warns.append((level, msg)))
        sent, logs, _ = env
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            pauses_directory=str(root),
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert logs == []
        assert any("unreadable" in m for _lvl, m in warns), (
            "the corrupt overlay must still be WARNed about — that warning is "
            "node_pauses' own contract and the operator's only clue")

    @pytest.mark.parametrize("target", [
        None,
        "not-a-dict",
        [],
        {"payload": "not-a-dict"},
        {"node": 1234},                    # a port is not a host
        {"node": "   "},                   # blank is not a host
        {"payload": {"node": None}},
        {"host": ["192.0.2.10"]},          # a list is not a host
    ])
    def test_junk_targets_never_attribute_a_host_and_never_raise(
            self, env, overlay, target):
        """A rule that answers for no node cannot be silenced by a record shape
        it cannot read: junk is "this signal names no node", never a guess."""
        pause(PAUSED_NODE, overlay)
        sent, logs, _ = env
        trigger.fire_trigger_action(
            WORKER_DOWN_RULE, target, pauses_directory=overlay)
        assert sent == ["HSCC: worker down"]
        assert logs == []
        assert trigger._signal_host(WORKER_DOWN_RULE, target) is None

    def test_a_serving_signal_keeps_its_node_even_when_the_target_is_junk(
            self, env, overlay):
        """Deliberate, and the reason attribution is not "whatever the event
        says": a serving-layer metric ANSWERS FOR the serving node, so an
        unreadable or empty target does not turn it into "no node named" and
        re-open the page for a parked node."""
        pause(PAUSED_NODE, overlay, reason="training run")
        sent, logs, _ = env
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), None,
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert trigger._signal_host(default_rule("orch-dgx-down"), None,
                                    primary_node_fn=lambda: PAUSED_NODE) \
            == PAUSED_NODE
        assert sent == []
        assert logs == [("INFO", pause_line("orch-dgx-down", PAUSED_NODE,
                                            "training run"))]

    def test_helper_resolves_topology_only_for_a_serving_signal(self, overlay):
        """A notify rule on some other metric must not drag in a topology
        resolution (the trigger loop runs every 15 s)."""
        calls = []

        def spy():
            calls.append(1)
            return PAUSED_NODE
        rule = {"id": "backup-failed", "trigger_type": "notify",
                "condition": {"metric": "event_type", "op": "==",
                              "value": "backup.failed"},
                "trigger_params": {"title": "HSCC: backup failed", "body": "x"}}
        assert trigger._signal_host(rule, {"event_type": "backup.failed"},
                                    primary_node_fn=spy) is None
        assert calls == []
        assert trigger._signal_host(default_rule("orch-dgx-down"),
                                    {"event_type": "x"},
                                    primary_node_fn=spy) == PAUSED_NODE
        assert calls == [1]


# ── discrete per-node events: attribution through the emit_event envelope ──


WORKER_DOWN_RULE = {
    "id": "worker-down", "trigger_type": "notify",
    "condition": {"metric": "event_type", "op": "==", "value": "worker.down"},
    "trigger_params": {"title": "HSCC: worker down", "body": "a worker is down"},
}


class TestDiscreteEventAttribution:
    def test_event_naming_the_parked_node_is_silent(self, env, overlay):
        """The envelope desktop.emit_event writes puts the node in payload —
        that is where a per-node event says which node it is about."""
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            WORKER_DOWN_RULE,
            {"event_type": "worker.down", "source": "hscc_daemon",
             "payload": {"node": PAUSED_NODE, "port": 8000}},
            pauses_directory=overlay)
        assert sent == []
        assert logs == [("INFO", pause_line("worker-down", PAUSED_NODE,
                                            "training run"))]

    def test_event_naming_another_node_still_pages(self, env, overlay):
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay)
        trigger.fire_trigger_action(
            WORKER_DOWN_RULE,
            {"event_type": "worker.down",
             "payload": {"node": UNRELATED_NODE, "port": 8000}},
            pauses_directory=overlay)
        assert sent == ["HSCC: worker down"]
        assert logs == []

    def test_host_spelling_is_honoured_too(self, env, overlay):
        """``host`` is the spelling a client/mount record uses."""
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            WORKER_DOWN_RULE,
            {"event_type": "worker.down", "host": "  " + PAUSED_NODE + " "},
            pauses_directory=overlay)
        assert sent == []
        assert logs == [("INFO", pause_line("worker-down", PAUSED_NODE,
                                            "training run"))]

    def test_top_level_host_wins_over_the_payload(self, env, overlay):
        """Precedence is pinned, not incidental: the record's own field is the
        stronger statement of what the signal is about."""
        sent, logs, _ = env
        pause(UNRELATED_NODE, overlay, reason="the outer field")
        trigger.fire_trigger_action(
            WORKER_DOWN_RULE,
            {"event_type": "worker.down", "node": UNRELATED_NODE,
             "payload": {"node": PAUSED_NODE}},
            pauses_directory=overlay)
        assert sent == []
        assert logs == [("INFO", pause_line("worker-down", UNRELATED_NODE,
                                            "the outer field"))]

    def test_event_naming_no_node_at_all_keeps_its_page(self, env, overlay):
        """A rule on a metric that answers for no host is untouched by the
        overlay — even the same worker-down rule with the node dropped."""
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay)
        trigger.fire_trigger_action(
            WORKER_DOWN_RULE, {"event_type": "worker.down"},
            pauses_directory=overlay)
        assert sent == ["HSCC: worker down"]
        assert logs == []


# ── resume: alerting comes back, with nothing left behind ──────────────────


class TestResumeRestoresAlerting:
    def test_clearing_the_pause_restores_the_page_on_the_next_call(self, env,
                                                                   overlay):
        """The overlay on disk is the ONLY state this gate has: no cache, and
        nothing the suppressed calls wrote, so the resume is immediate."""
        rule = default_rule("orch-dgx-down")
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            rule, {"event_type": "x"}, pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == [] and len(logs) == 1

        assert node_pauses.clear(PAUSED_NODE, directory=overlay) is not None
        trigger.fire_trigger_action(
            rule, {"event_type": "x"}, pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]
        assert len(logs) == 1  # the suppressed call left no other trace

    def test_expiry_then_the_daemons_clear_restores_the_page(self, env,
                                                             overlay):
        """A pause past its until is STILL a pause (paused() is
        truth-accurate), so it keeps suppressing until the daemon's own resume
        path clears it — then alerting is normal again."""
        rule = default_rule("vllm-down")
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, until="1h", reason="training run")
        fired = trigger._node_pause_for_signal(
            rule, {"event_type": "x"}, directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert fired[0] == PAUSED_NODE

        # The deadline passes; the record still holds the node.
        expired = node_pauses.pending_resume(
            now=past_the_deadline(), directory=overlay)
        assert [h for h, _e in expired] == [PAUSED_NODE]
        trigger.fire_trigger_action(
            rule, {"event_type": "x"}, pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == []

        node_pauses.expiry_pass(now=past_the_deadline(),
                                directory=overlay)
        assert node_pauses.covering(PAUSED_NODE, directory=overlay) is None
        trigger.fire_trigger_action(
            rule, {"event_type": "x"}, pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == ["HSCC: vLLM backend down"]

    def test_suppression_writes_nothing_anywhere_of_its_own(self, env, overlay,
                                                            tmp_hfcc_dir):
        """No stale suppression is possible if the gate writes nothing: the
        overlay (read-only here), the cooldown ledger and the state dir are all
        untouched by a suppressed notify."""
        pause(PAUSED_NODE, overlay, reason="training run")
        overlay_file = os.path.join(overlay, node_pauses.OVERLAY_NAME)
        before = {name: os.path.getmtime(os.path.join(tmp_hfcc_dir, name))
                  for name in os.listdir(tmp_hfcc_dir)}
        stamp = os.path.getmtime(overlay_file)
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert os.path.getmtime(overlay_file) == stamp
        assert {name: os.path.getmtime(os.path.join(tmp_hfcc_dir, name))
                for name in os.listdir(tmp_hfcc_dir)} == before


# ── the boundary with the neighbouring gates and branches ──────────────────


class TestBoundaryWithStayDownAndOtherBranches:
    def test_stay_down_gate_answers_first_when_both_apply(self, env, overlay):
        """Two gates, two questions, one line: the fleet-wide instruction is
        the broader reason and keeps its exact wording."""
        sent, logs, _ = env
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            default_rule("orch-dgx-down"), {"event_type": "x"},
            deliberately_down_fn=lambda: {"set_at": "2026-11-08T17:51:23+02:00",
                                          "reason": "hscc cluster down"},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert sent == []
        assert logs == [("INFO", "Trigger orch-dgx-down: notify suppressed — "
                                "fleet deliberately down (hscc cluster down)")]

    def test_auto_restart_branch_is_untouched_by_the_pause(self, env, overlay,
                                                           monkeypatch):
        """Not this seam's business: restart_vllm's rule 0 owns the relaunch
        skip (t_4ecbd9d0). Pinning it here means a later 'also gate restart'
        edit has to change this test on purpose."""
        restarted = []
        monkeypatch.setattr(trigger, "send_macos_notification",
                            lambda title, body, priority="normal":
                            restarted.append(title))
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.fire_trigger_action(
            {"id": "restart-on-dgx-down", "trigger_type": "auto_restart",
             "condition": {"metric": "failed_dgx", "op": "==", "value": True},
             "trigger_params": {}},
            {"event_type": "x"},
            restart_vllm_fn=lambda: {"ok": True},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert restarted == ["HSCC Auto-Restart"]

    def test_block_pipeline_branch_is_untouched_by_the_pause(self, env, overlay,
                                                             monkeypatch):
        """An operator rule that BLOCKS the pipeline still blocks it while a
        node is parked — the gate is on the notify branch only."""
        saved = []
        # Patch the name trigger.py actually calls (it imports the symbol, not
        # the module), or this test would assert on a function nobody invokes.
        monkeypatch.setattr(trigger, "save_watchdog_block",
                            lambda data: saved.append(dict(data)))
        assert pause(PAUSED_NODE, overlay, reason="training run") is not None
        trigger.fire_trigger_action(
            {"id": "block-on-wedge", "trigger_type": "block_pipeline",
             "condition": {"metric": "event_type", "op": "==",
                           "value": "x"},
             "trigger_params": {}},
            {"event_type": "x"},
            watchdog_block_fn=lambda: {"blocked": False},
            pauses_directory=overlay,
            primary_node_fn=lambda: PAUSED_NODE)
        assert len(saved) == 1 and saved[0]["blocked"] is True


# ── the real loop: engine-level, default overlay root ─────────────────────


class TestTriggerEngineEndToEnd:
    """The full tick, through trigger_engine() — the path the daemon's 15 s
    loop actually runs, with the DEFAULT overlay root (conftest-sealed)."""

    def _engine(self, tmp_hfcc_dir, monkeypatch, rules, events=()):
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(trigger, "TRIGGERS_FILE",
                            str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE",
                            str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE",
                            str(tmp_hfcc_dir / "events.jsonl"))
        (tmp_hfcc_dir / "triggers.json").write_text(json.dumps({"rules": rules}))
        (tmp_hfcc_dir / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events))
        sent = []
        monkeypatch.setattr(trigger, "send_macos_notification",
                            lambda title, body, priority="normal":
                            sent.append(title))
        logs = []
        monkeypatch.setattr(trigger, "log",
                            lambda msg, level="INFO", **kw:
                            logs.append((level, msg)))
        monkeypatch.setattr(trigger, "emit_event",
                            lambda *a, **kw: None)
        return sent, logs

    # The rule is on an event_type and its event names a node, so the gate's
    # attribution is exercised without a health check of any kind.
    _RULE = {"id": "worker-down", "enabled": True, "trigger_type": "notify",
             "cooldown_seconds": 0,
             "condition": {"metric": "event_type", "op": "==",
                           "value": "worker.down"},
             "trigger_params": {"title": "HSCC: worker down", "body": "x"}}
    _EVENT = {"event_type": "worker.down", "severity": "warning",
              "source": "hscc_daemon", "payload": {"node": PAUSED_NODE}}

    def test_parked_node_is_quiet_and_resume_is_loud_at_the_default_root(
            self, tmp_hfcc_dir, monkeypatch):
        root = str(tmp_hfcc_dir)
        assert os.path.realpath(node_pauses.HSCC_DIR) == os.path.realpath(root), (
            "conftest's node_pauses.HSCC_DIR seal moved; refusing to write a "
            "pause anywhere else")
        sent, logs = self._engine(tmp_hfcc_dir, monkeypatch, [self._RULE],
                                  [self._EVENT])

        node_pauses.pause(PAUSED_NODE, reason="training run")  # default root
        trigger.trigger_engine()
        assert sent == []
        assert logs == [("INFO", pause_line("worker-down", PAUSED_NODE,
                                            "training run"))]

        node_pauses.clear(PAUSED_NODE)                        # default root
        trigger.trigger_engine()
        assert sent == ["HSCC: worker down"]

    def test_the_suppressed_tick_still_stamps_the_rule_cooldown(
            self, tmp_hfcc_dir, monkeypatch):
        """Pre-existing, shared with the stay-down gate, and load-bearing: it
        is what keeps a persisting bad state from re-notifying every 15 s
        (2026-10-08: 178 notifications in 11 minutes). The pause adds no
        silence of its own beyond this, so it must not be mistaken for stale
        pause state — and the stamp must not be removed to 'make resume
        instant'."""
        rule = dict(self._RULE, cooldown_seconds=3600)
        sent, _logs = self._engine(tmp_hfcc_dir, monkeypatch, [rule],
                                    [self._EVENT])
        node_pauses.pause(PAUSED_NODE, reason="training run")
        trigger.trigger_engine()
        assert sent == []
        cooldowns = json.loads(
            (tmp_hfcc_dir / "cooldowns.json").read_text())
        assert cooldowns.get("worker-down"), (
            "a suppressed notify must still stamp the rule's own cooldown")

        node_pauses.clear(PAUSED_NODE)
        trigger.trigger_engine()
        assert sent == [], "the rule's cooldown is honoured, not bypassed"

    def test_engine_threads_the_overlay_seam(self, tmp_hfcc_dir, monkeypatch,
                                              overlay):
        """pauses_directory= reaches the gate through trigger_engine, so the
        whole tick can be driven against a temp overlay."""
        sent, logs = self._engine(tmp_hfcc_dir, monkeypatch, [self._RULE],
                                  [self._EVENT])
        pause(PAUSED_NODE, overlay, reason="training run")
        trigger.trigger_engine(pauses_directory=overlay)
        assert sent == []
        assert logs == [("INFO", pause_line("worker-down", PAUSED_NODE,
                                            "training run"))]
        node_pauses.clear(PAUSED_NODE, directory=overlay)
        trigger.trigger_engine(pauses_directory=overlay)
        assert sent == ["HSCC: worker down"]

    def test_degraded_state_pseudo_event_is_quiet_while_the_serving_node_is_parked(
            self, tmp_hfcc_dir, monkeypatch):
        """The daemon's real serving path: check_dgx stamps dgx.json ok=False,
        the engine turns that into a ``state.dgx.degraded`` pseudo-event that
        names NO node, the shipped ``orch-dgx-down`` rule matches it, and the
        metric is what attributes it to the serving node — silent while that
        node is parked, loud again the moment the pause is cleared."""
        from hscc_daemon import state as state_mod
        state_mod.write_state("dgx", {"ok": False, "vllm_healthy": False})
        sent, logs = self._engine(tmp_hfcc_dir, monkeypatch,
                                  [default_rule("orch-dgx-down")], [])
        node_pauses.pause(PAUSED_NODE, reason="training run")   # sealed root
        trigger.trigger_engine()
        assert sent == []
        assert logs == [("INFO", pause_line("orch-dgx-down", PAUSED_NODE,
                                            "training run"))]
        node_pauses.clear(PAUSED_NODE)
        # The shipped rule declares cooldown_seconds: 1800, and the suppressed
        # tick stamped it (see the engine test above), so the resume is quiet
        # for the RULE'S OWN window — not because of the pause. Rewind the stamp
        # to "that window has passed" and the page goes out.
        cooldowns = json.loads((tmp_hfcc_dir / "cooldowns.json").read_text())
        (tmp_hfcc_dir / "cooldowns.json").write_text(json.dumps(
            {k: v - 1801 for k, v in cooldowns.items()}))
        trigger.trigger_engine()
        assert sent == ["HSCC: orchestrator/DGX unhealthy"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
