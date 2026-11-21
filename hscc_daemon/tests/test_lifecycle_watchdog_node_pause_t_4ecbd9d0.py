"""The watchdog must skip a paused node: no relaunch, no strike, no page.

WHY THIS EXISTS
---------------
``hscc node pause`` (the overlay t_28caea71) parks ONE node on purpose and
touches no cluster setting — templates, ``serving.json``, the applied-template
record and the keepalive config all stay byte-identical, which is the whole
point of making it an OVERLAY instead of an edit. So the node's serving unit is
stopped with nothing recording that the stop was deliberate anywhere the
watchdog already looks. From ``pipeline_watchdog``'s seat that is
indistinguishable from the fault it exists to catch, and every tick it stays
that way. Concretely, before this seam:

  1. the tick appended a failure entry to ``WATCHDOG_BLOCK_FILE`` — the
     breaker's durable memory, so the pause polluted it across ticks;
  2. on the third such tick it latched ``PIPELINE BLOCKED`` and sent a
     ``critical`` desktop notification for a node the operator parked — the
     false-page class §5/C2 spent a whole workstream killing for autodown;
  3. and it called ``restart_vllm()``, whose rule-1..4 contract is stop-then
     -launch: a paused node would get its model relaunched on top of whatever
     the operator put on that GPU. For the first real use of this feature
     (handing a worker GPU to a training run) that is the destructive one.

THE BOTH-HALVES RULE
--------------------
A skip that always skips is worthless, so every guard here is pinned from both
sides: the paused node is NOT healed (and records nothing), and an unpaused
node IS still healed exactly as before — including a pause recorded against a
DIFFERENT node, which must not disarm the breaker at all.

ASK ``covering``, NEVER ``paused``
----------------------------------
``node_pauses.paused(host)`` is host-keyed: a pause issued against one node of
a tensor-parallel unit lists the rest of that span only in the record's
``span_nodes``. A seam that asked ``paused()`` would relaunch the other half of
a unit the operator stopped as one thing.
``test_span_sibling_node_pause_skips_the_heal`` pins the choice by asserting
the two API answers DIFFER for the sibling node and that the behaviour follows
the ``covering`` answer — so swapping in ``paused()`` reddens this file.

THE OVERLAY IS READ, NEVER WRITTEN, AND ONLY UNDER A TEMP ROOT
---------------------------------------------------------------
Every test here passes ``pause_directory=`` (node_pauses' only relocation seam)
pointed at a tmp dir, and every host is TEST-NET-1 (192.0.2.0/24). No real
``~/.hscc`` is read or written, no sparkrun, no ssh: the restart surface is
faked at ``run_cmd``/``heal_lease`` and both watchdog legs are injected.
"""

import json

import pytest

# TEST-NET-1 (RFC 5737) — documentation space, never routable, never this fleet.
PAUSED_NODE = "192.0.2.10"
SPAN_SIBLING = "192.0.2.11"
UNRELATED_NODE = "192.0.2.20"


@pytest.fixture
def seam(tmp_hfcc_dir, tmp_path, monkeypatch):
    """Isolated watchdog tick: block file, stream dir, overlay root, fake legs.

    Mirrors the harness the other watchdog suites use
    (test_watchdog_gateway_only_breaker_b0811ce0.py) so this file's ticks are
    the same kind of tick everywhere: the block file and the stream dir are
    tmp paths, ``log`` is captured, no desktop notification can fire, and no
    restart can reach a shell.
    """
    from hscc_daemon import lifecycle, serving
    from hscc_daemon import state as state_mod

    state_dir = tmp_hfcc_dir / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    block_file = tmp_hfcc_dir / "watchdog-block.json"
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(block_file))

    # The overlay root seam. node_pauses' conftest seal already keeps the
    # DEFAULT root out of reach; passing the directory explicitly is what makes
    # each test's overlay its own, and it is the seam the card asked for.
    overlay_dir = tmp_path / "overlay"
    overlay_dir.mkdir()

    monkeypatch.setattr(serving, "PRIMARY_NODE", PAUSED_NODE)

    captured = {"logs": [], "notifications": [], "restarts": [],
                "overlay": str(overlay_dir), "block_file": block_file,
                "state_dir": state_dir}
    monkeypatch.setattr(
        lifecycle, "log",
        lambda msg, lvl="INFO": captured["logs"].append((lvl, msg)))
    return captured


def tick(seam, dgx=False, gateway=False, restart_result=None):
    """One watchdog cycle with both legs injected — never the real checks.

    ``restart_result`` defaults to a REFUSAL (the same isolation the sibling
    suite uses): a refusal launches nothing, so the load-grace window cannot
    mask a strike and no hollow-launch alarm can fire. The call is still
    recorded, so "was a heal attempted at all" stays observable — which is
    exactly the question this card is about.
    """
    from hscc_daemon import lifecycle

    result = ({"ok": False, "refused": True} if restart_result is None
              else dict(restart_result))
    return lifecycle.pipeline_watchdog(
        check_dgx_fn=lambda: dgx,
        check_gateway_fn=lambda: gateway,
        restart_vllm_fn=lambda: seam["restarts"].append(1) or dict(result),
        send_macos_notification_fn=lambda title, body, **kw: seam[
            "notifications"].append((title, body, kw.get("priority"))),
        pause_directory=seam["overlay"],
    )


def block_bytes(seam):
    f = seam["block_file"]
    return f.read_bytes() if f.exists() else b""


def block_on_disk(seam):
    from hscc_daemon import lifecycle
    return lifecycle.load_watchdog_block()


def stream_on_disk(seam):
    """The watchdog STREAM (what verify/status read) — not the block file."""
    with open(f"{seam['state_dir']}/watchdog.json") as f:
        return json.load(f)


def pause(host, **kw):
    from hscc_daemon import node_pauses
    return node_pauses.pause(host, directory=kw.pop("directory", None), **kw)


# ── The two halves the card asks for ───────────────────────────────────────

class TestSkipBothDirections:
    def test_paused_node_is_not_healed_and_records_no_strike(self, seam):
        """ACCEPT half 1: a paused node gets no relaunch, no strike, no page."""
        pause(PAUSED_NODE, reason="training on this GPU", directory=seam["overlay"])
        before = block_bytes(seam)          # no block file yet

        assert tick(seam, dgx=False, gateway=False) is False

        assert seam["restarts"] == []          # never even attempted
        assert seam["notifications"] == []     # a pause is not allowed to page
        # "No heal strike recorded" is a claim about the BLOCK FILE, so that is
        # what gets compared: byte-identical, not merely "no new failures".
        assert block_bytes(seam) == before
        assert block_on_disk(seam)["failures"] == []
        assert block_on_disk(seam)["blocked"] is False

        stamp = stream_on_disk(seam)
        # The stream stays truthful — the serving layer really is not answering
        # — and carries the marker the reader cards key on.
        assert stamp["ok"] is False
        assert stamp["blocked"] is False
        assert stamp["paused"] is True
        assert stamp["paused_node"] == PAUSED_NODE
        assert stamp["paused_reason"] == "training on this GPU"
        assert stamp["auto_restart"] is False
        assert "not counted" in stamp["message"]
        # and the tick said so out loud, at WARN, with the operator's reason
        assert any(lvl == "WARN" and "is paused" in msg and "training" in msg
                   for lvl, msg in seam["logs"])

    def test_unpaused_node_is_still_healed(self, seam):
        """ACCEPT half 2: with no pause recorded, the heal path is unchanged."""
        result = tick(seam, dgx=False, gateway=False,
                      restart_result={"ok": True, "verified": True})

        assert result is False                # both legs failed; bool contract
        assert seam["restarts"] == [1]        # the restart WAS attempted
        assert block_on_disk(seam)["auto_restart_count"] == 1
        assert len(block_on_disk(seam)["failures"]) == 1   # the strike is recorded
        # a verified serving restart notifies the operator (existing behaviour)
        assert seam["notifications"]

    def test_pause_on_an_unrelated_node_does_not_disarm_the_watchdog(
            self, seam):
        """The skip is about the node this pass can heal, not about any pause.

        Blanket "skip whenever the overlay is non-empty" would be the same
        fail-open mistake in a new costume: a gateway that dies for real while
        an unrelated node is parked would then never latch and never page.
        """
        pause(UNRELATED_NODE, reason="something else", directory=seam["overlay"])

        tick(seam, dgx=False, gateway=False)

        assert seam["restarts"] == [1]
        assert len(block_on_disk(seam)["failures"]) == 1


# ── Span awareness: the reason the seam asks `covering` ────────────────────

class TestSpanAwareness:
    def test_span_sibling_node_pause_skips_the_heal(self, seam, monkeypatch):
        """A pause issued against the OTHER node of the span must still skip.

        The unit spans PAUSED_NODE + SPAN_SIBLING and the operator parked it by
        naming one host; the healed node here is the sibling, which appears in
        the record only under ``span_nodes``.
        """
        from hscc_daemon import node_pauses, serving

        pause(PAUSED_NODE, reason="tp span parked", directory=seam["overlay"],
              span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        monkeypatch.setattr(serving, "PRIMARY_NODE", SPAN_SIBLING)

        # The two APIs genuinely disagree for this node — that is the whole
        # trap, and it is why the seam must not reach for paused().
        assert node_pauses.paused(SPAN_SIBLING, directory=seam["overlay"]) is None
        assert node_pauses.covering(SPAN_SIBLING,
                                    directory=seam["overlay"]) is not None

        tick(seam, dgx=False, gateway=False)

        assert seam["restarts"] == []
        assert seam["notifications"] == []
        assert block_bytes(seam) == b""

    def test_span_member_reports_the_record_it_was_parked_by(self, seam,
                                                             monkeypatch):
        """The stamp names the pause that covers it, not the host it was keyed to."""
        from hscc_daemon import serving

        pause(PAUSED_NODE, reason="tp span parked", directory=seam["overlay"],
              span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        monkeypatch.setattr(serving, "PRIMARY_NODE", SPAN_SIBLING)

        tick(seam, dgx=False, gateway=False)

        stamp = stream_on_disk(seam)
        assert stamp["paused"] is True
        assert stamp["paused_node"] == SPAN_SIBLING
        assert stamp["paused_reason"] == "tp span parked"


# ── The breaker must not accumulate across a long pause ────────────────────

class TestNoStrikeAcrossTicks:
    def test_three_paused_ticks_never_latch_while_unpaused_ones_do(self, seam):
        """The pair that makes "no heal strike" decisive rather than aspirational.

        The breaker is a 3-failures-in-10min WINDOW, so a single quiet tick
        proves nothing — the skip has to hold across the whole window. The
        control half runs the same three ticks with no overlay and must latch
        and page, which is the only way this test shows the seam is doing
        something rather than describing a tick that never escalates anyway.
        """
        pause(PAUSED_NODE, directory=seam["overlay"])
        for _ in range(3):
            assert tick(seam, dgx=False, gateway=False) is False

        assert seam["restarts"] == []
        assert seam["notifications"] == []
        assert block_on_disk(seam)["blocked"] is False
        assert block_on_disk(seam)["failures"] == []
        assert block_bytes(seam) == b""

    def test_control_three_unpaused_ticks_still_latch_and_page(self, seam):
        for _ in range(3):
            tick(seam, dgx=False, gateway=False)

        assert block_on_disk(seam)["blocked"] is True
        titles = [t for t, _b, _p in seam["notifications"]]
        assert "HSCC Pipeline Blocked" in titles


# ── The second guard: the mutating surface itself ──────────────────────────

class TestRestartSurfaceRefusesPaused:
    """restart_vllm guards too, because the watchdog is not its only caller.

    ``trigger.fire_trigger_action`` (trigger.py) calls ``restart_vllm_fn()``
    straight from the 15 s trigger loop, so a watchdog-only guard leaves that
    door open. These two tests pin rule 0 and, crucially, that a skip takes no
    heal lease either — a skipped repair must not advertise itself to every
    other repair path as "a repair is in flight".
    """

    @pytest.fixture
    def lease_fakes(self, monkeypatch):
        from hscc_daemon import heal_lease
        hits = {"read": [], "acquire": [], "release": []}
        monkeypatch.setattr(heal_lease, "read_lease",
                            lambda *a, **kw: hits["read"].append(1) or None)
        monkeypatch.setattr(
            heal_lease, "acquire",
            lambda *a, **kw: hits["acquire"].append(1) or (False, "other"))
        monkeypatch.setattr(heal_lease, "release",
                            lambda *a, **kw: hits["release"].append(1))
        monkeypatch.setattr(heal_lease, "describe", lambda *_a, **_kw: "other")
        return hits

    @pytest.fixture
    def cmd_fakes(self, monkeypatch):
        """run_cmd is the last stop before a real sparkrun argv — never run it."""
        from hscc_daemon import lifecycle
        hits = []
        monkeypatch.setattr(lifecycle, "run_cmd",
                            lambda *a, **kw: hits.append(a) or {"ok": True})
        return hits

    def test_paused_node_is_refused_before_the_lease_and_any_command(
            self, seam, lease_fakes, cmd_fakes):
        from hscc_daemon import lifecycle

        pause(PAUSED_NODE, reason="training", directory=seam["overlay"])

        result = lifecycle.restart_vllm(
            verify_fn=lambda *a, **kw: True, pause_directory=seam["overlay"])

        assert result["paused"] is True
        assert result["ok"] is False
        assert result["verified"] is None
        assert "is paused" in result["output"] and "training" in result["output"]
        assert cmd_fakes == []            # no stop, no launch
        assert lease_fakes["read"] == []  # rule 0 sits before rule 1
        assert lease_fakes["acquire"] == []
        assert lease_fakes["release"] == []
        assert any(lvl == "WARN" and "skipped" in msg
                   for lvl, msg in seam["logs"])

    def test_unpaused_node_reaches_the_lease_rule(self, seam, lease_fakes,
                                                  cmd_fakes):
        """Control: with an empty overlay, rule 0 does not fire at all.

        The heal lease IS consulted (rule 1 ran), which is only possible past
        rule 0 — and the call then aborts on its own pre-existing rule 3 (no
        explicit stop TARGET in this sandbox, so no command is ever issued).
        Asserting the abort shape too, so this control cannot quietly become
        "the guard fired and nothing happened anyway".
        """
        from hscc_daemon import lifecycle

        result = lifecycle.restart_vllm(
            verify_fn=lambda *a, **kw: True, pause_directory=seam["overlay"])

        assert result["paused"] is False
        assert result["stop_failed"] is True
        assert lease_fakes["read"] == [1]     # got past rule 0, into rule 1
        assert cmd_fakes == []                # nothing was ever executed
        assert not any("is paused" in msg for _lvl, msg in seam["logs"])


# ── Boundaries and failure directions ──────────────────────────────────────

class TestBoundaries:
    def test_gateway_only_failure_while_paused_is_not_a_strike(self, seam):
        """A gateway leg that fails because its backend is parked is not a fault.

        ``check_gateway``'s verdict is ``job_ok and vllm_ok`` — the vLLM backend
        probe is the very model the operator stopped, so while the node is
        parked this leg fails for the pause too. Deliberate boundary: the skip
        covers the whole failing tick, and the reason is the backend probe, not
        a blanket amnesty (the unrelated-node test above is what keeps that
        amnesty from widening past the node this pass heals).
        """
        pause(PAUSED_NODE, directory=seam["overlay"])

        assert tick(seam, dgx=True, gateway=False) is False

        assert seam["restarts"] == []
        assert seam["notifications"] == []
        assert block_bytes(seam) == b""

    def test_a_pause_whose_deadline_passed_still_skips_the_heal(self, seam):
        """Expiry is the daemon tick's job, never the watchdog's.

        ``paused``/``covering`` are clock-independent on purpose: a pause whose
        ``until`` has passed has not been resumed yet, and a watchdog that
        decided the node was free the moment the deadline went by would
        relaunch it seconds before the daemon's own auto-resume did.
        """
        import datetime

        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=5)).isoformat()
        pause(PAUSED_NODE, until=past, directory=seam["overlay"])

        tick(seam, dgx=False, gateway=False)

        assert seam["restarts"] == []
        assert block_bytes(seam) == b""

    def test_malformed_overlay_fails_open_and_the_heal_still_runs(self, seam):
        """A broken overlay must not freeze the fleet's ability to heal itself.

        node_pauses' documented direction: absence is silence, corruption is
        "no pauses" PLUS a WARN. The seam inherits it — here that means the
        restart still happens, exactly as it would with no overlay at all.
        """
        from pathlib import Path

        from hscc_daemon import node_pauses

        path = Path(seam["overlay"]) / node_pauses.OVERLAY_NAME
        path.write_text("{ this is not json")

        assert node_pauses.list_pauses(directory=seam["overlay"]) == {}

        tick(seam, dgx=False, gateway=False)

        assert seam["restarts"] == [1]
        assert len(block_on_disk(seam)["failures"]) == 1

    def test_tick_never_writes_the_overlay(self, seam):
        """The watchdog is a reader. If it ever wrote here, it could pause —
        or un-pause — a node behind the operator's back."""
        from pathlib import Path

        pause(PAUSED_NODE, reason="training", directory=seam["overlay"])
        path = Path(seam["overlay"]) / "node_pauses.json"
        before = path.read_bytes()

        for _ in range(3):
            tick(seam, dgx=False, gateway=False)

        assert path.read_bytes() == before

    def test_default_root_is_untouched_when_a_directory_is_given(self, seam):
        """A sandboxed tick must decide against the temp overlay, not ~/.hscc.

        Plants a pause in the DEFAULT (conftest-sealed) root only: if the seam
        ignored ``pause_directory`` and read the default root, the paused node
        would be skipped and this test would see zero restarts.
        """
        from hscc_daemon import node_pauses

        node_pauses.pause(PAUSED_NODE, reason="default-root decoy")
        assert node_pauses.paused(PAUSED_NODE) is not None

        tick(seam, dgx=False, gateway=False)

        assert seam["restarts"] == [1]
