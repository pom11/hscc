"""Gateway-only watchdog failures must accumulate toward the 3-failure breaker.

The bug (t_b0811ce0, found while working t_48206b9d / proxy-stall c8-3):
`pipeline_watchdog` records the failure entry and computes
`recent = [f for f in block["failures"] if not f.get("dgx", True) or
not f.get("gateway", True)]` — a filter that explicitly counts a
gateway-only failure — and then blocks when `len(recent) >= 3`. But the
branch a gateway-only tick takes (`dgx_ok` fine, `gw_ok` False) only called
`write_state("watchdog", ...)`. The appended entry and the recomputed
`failed_count` lived only in the in-memory `block` and were dropped when the
cycle returned, so every fresh tick reloaded an empty history from disk.

Net effect: a persistent gateway outage stamped `ok: False` every 30 s for
as long as it lasted and could NEVER trip the breaker and NEVER send the
"PIPELINE BLOCKED" critical notification — the one signal an operator needs
when the gateway stays down.

Intent was settled before changing anything (this card's DECIDE FIRST step).
Three independent statements say gateway-only failures are *meant* to count:

1. `changelog.d/archive/2.14.0/t_1c545ebb.md` describes the breaker as
   "the 3-failures-in-10min pipeline breaker" that `check_gateway()` "copies
   [its verdict] into" — the gateway leg is named as an input to the breaker.
2. The `recent` filter above already counts the gateway leg. A design where
   "gateway alone must never block the pipeline" would not compute that
   filter at all.
3. `reason` on the block path prints `DGX=... GW=...` for both legs, and the
   block message is "PIPELINE BLOCKED" — not "DGX BLOCKED".

So the missing `save_watchdog_block` is an oversight, not a policy.

The one case that must NOT accumulate is the intentional-autodown window:
§5 / C2 pinned that a deliberately-down serving layer may never be recorded
as a pipeline fault, and `check_gateway()` returns False for exactly that
reason (its `ok` is `job_ok and vllm_ok`, and vLLM is stopped by design).
Counting it would block the pipeline and fire a critical desktop alarm during
a deliberate power-save — the false-alarm class that work exists to kill.
`test_intentional_autodown_gateway_failure_does_not_accumulate` pins that
carve-out so the fix cannot be widened into it later.
"""

import datetime
import json

import pytest


@pytest.fixture
def wd(tmp_hfcc_dir, monkeypatch):
    """Isolated watchdog workspace: patched block file + state dir, no real
    desktop notifications, no restarts, no shell-outs."""
    from hscc_daemon import lifecycle, state as state_mod

    state_dir = tmp_hfcc_dir / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE",
                        str(tmp_hfcc_dir / "watchdog-block.json"))

    captured = {"logs": [], "notifications": [], "restarts": []}
    monkeypatch.setattr(
        lifecycle, "log",
        lambda msg, lvl="INFO": captured["logs"].append((lvl, msg)))
    return captured


def tick(wd, dgx=True, gateway=False, restarts=None, restart_result=None):
    """One watchdog cycle with both legs injected — never the real checks.

    `restart_result` defaults to a REFUSAL, which is the cleanest way to
    isolate the breaker from the restart machinery: a refusal means no launch
    happened, so no load-grace window is set (that window legitimately
    suppresses failure counting for 20 min after a VERIFIED serving restart,
    which would mask the accumulation under test) and no hollow-launch alarm
    can fire. The restart call is still recorded, so tests that care whether a
    restart was attempted keep working.
    """
    from hscc_daemon import lifecycle

    calls = wd["restarts"] if restarts is None else restarts
    result = {"ok": False, "refused": True} if restart_result is None \
        else restart_result
    return lifecycle.pipeline_watchdog(
        check_dgx_fn=lambda: dgx,
        check_gateway_fn=lambda: gateway,
        restart_vllm_fn=lambda: calls.append(1) or dict(result),
        send_macos_notification_fn=lambda title, body, **kw: wd[
            "notifications"].append((title, body, kw.get("priority"))),
    )


def block_on_disk():
    from hscc_daemon import lifecycle
    return lifecycle.load_watchdog_block()


def state_on_disk():
    """The watchdog STREAM file (what `hscc verify` and `hscc status` read) —
    distinct from the block file, which is the breaker's own history."""
    from hscc_daemon import state as state_mod
    with open(f"{state_mod.STATE_DIR}/watchdog.json") as f:
        return json.load(f)


def _ts(minutes_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago)).isoformat()


def _gateway_entries(block):
    """Persisted entries whose gateway leg failed."""
    return [f for f in block.get("failures", []) if not f.get("gateway", True)]


# ---------------------------------------------------------------------------
# The bug: a gateway-only outage can never reach the breaker.
# ---------------------------------------------------------------------------

class TestGatewayOnlyFailuresAccumulate:

    def test_one_gateway_only_failure_is_persisted(self, wd, tmp_hfcc_dir):
        """The tick must WRITE its failure to the block file, not just stamp
        the state file. This is the exact line that was missing — a tick that
        only calls write_state() leaves `failures` empty on disk and every
        later tick reloads amnesia."""
        assert tick(wd) is False

        block = block_on_disk()
        assert len(_gateway_entries(block)) == 1, (
            "a gateway-only failure must survive the tick it was recorded in")
        assert block["failures"][0]["dgx"] is True
        assert block["failures"][0]["gateway"] is False

    def test_two_gateway_only_failures_accumulate_but_do_not_block(
            self, wd, tmp_hfcc_dir):
        """2 of 3: the history has to GROW across ticks. The old code stayed
        at 0 forever, so the count itself — not just the final block — is the
        thing under test."""
        tick(wd)
        tick(wd)

        block = block_on_disk()
        assert len(_gateway_entries(block)) == 2
        assert block.get("blocked") is not True
        assert wd["notifications"] == [], (
            "below the threshold no critical notification is owed")

    def test_three_gateway_only_failures_trip_the_breaker(self, wd,
                                                          tmp_hfcc_dir):
        """THE ACCEPTANCE CASE: three gateway-only ticks inside the 10-minute
        window must latch the breaker and raise the critical notification."""
        tick(wd)
        tick(wd)
        assert tick(wd) is False

        block = block_on_disk()
        assert block.get("blocked") is True, (
            "3 gateway-only failures in 10 min must block the pipeline")
        assert "3 consecutive failures in 10min" in block.get("reason", "")
        # The reason names the leg that actually failed, and must not blame
        # the healthy one.
        assert "DGX=OK" in block["reason"] and "GW=FAIL" in block["reason"]

        critical = [n for n in wd["notifications"] if n[2] == "critical"]
        assert len(critical) == 1, (
            "the breaker owes exactly one critical desktop notification")
        assert "Blocked" in critical[0][0]

        entry = state_on_disk()
        assert entry["ok"] is False
        assert entry["blocked"] is True
        assert "PIPELINE BLOCKED" in entry["message"]

    def test_gateway_only_failure_never_restarts_vllm(self, wd, tmp_hfcc_dir):
        """A gateway-only fault is now fatal-but-persisted; it is still NOT a
        serving-layer fault, so the vLLM restart gate (dgx_ok is False) must
        stay shut. The breaker trip must not smuggle a stop/relaunch in."""
        tick(wd)
        tick(wd)
        tick(wd)
        assert wd["restarts"] == [], (
            "a gateway-only outage must never stop or relaunch vLLM, "
            "including on the tick that trips the breaker")

    def test_breaker_blocked_tick_before_threshold_is_still_stamped(
            self, wd, tmp_hfcc_dir):
        """The state-file stamp the old code already produced must survive the
        fix: ok False, no intentional marker, and the degraded message — so
        `hscc verify` still sees a red stream and nobody mistakes a gateway
        outage for a healthy-but-quiet cycle."""
        tick(wd)

        entry = state_on_disk()
        assert entry["ok"] is False
        assert entry.get("intentional") != "autodown"
        assert "gateway" in entry["message"].lower()
        assert entry["dgx"] is True and entry["gateway"] is False


# ---------------------------------------------------------------------------
# The counter's window and reset semantics still hold once the history is
# real. Before the fix these were unfalsifiable: the history was always empty.
# ---------------------------------------------------------------------------

class TestWindowAndReset:

    def test_gateway_failures_older_than_the_window_are_pruned(
            self, wd, tmp_hfcc_dir):
        """Two gateway failures from 30 min ago are outside the 10-minute
        window: they must be dropped, not counted toward a 3rd failure. Without
        persistence this test could never fail; with it, a broken prune would
        block a pipeline on a stale outage."""
        from hscc_daemon import lifecycle
        lifecycle.save_watchdog_block({
            "blocked": False, "reason": "", "blocked_at": None,
            "auto_restart_count": 0,
            "failures": [
                {"timestamp": _ts(30), "dgx": True, "gateway": False},
                {"timestamp": _ts(25), "dgx": True, "gateway": False},
            ],
        })

        tick(wd)  # the 3rd gateway failure, but only the 1st inside the window

        block = block_on_disk()
        assert block.get("blocked") is not True, (
            "stale failures outside the 10-min window must not trip the breaker")
        assert len(_gateway_entries(block)) == 1, (
            "the two expired entries must have been pruned on write")

    def test_healthy_tick_does_not_erase_the_windowed_history_either_leg(
            self, wd, tmp_hfcc_dir):
        """A healthy tick resets `failed_count` but does NOT delete the
        failure entries — `cleanup_old_failures` prunes by TIME only, so a
        failure still inside the 10-minute window keeps counting toward the
        breaker. Probed on the pre-existing DGX path (fail, fail, HEALTHY, fail
        -> blocked), so this is the breaker's real semantics on BOTH legs and
        the gateway leg must now match it rather than invent its own reset.

        Corrected while making the suite green: the first draft of this test
        asserted a healthy tick *cleared the streak*, which the code never did
        on either path — see the follow-up card raised from this one for
        whether "3 consecutive failures in 10min" *should* mean consecutive.
        """
        tick(wd)
        tick(wd)
        assert len(_gateway_entries(block_on_disk())) == 2

        assert tick(wd, dgx=True, gateway=True) is True
        assert block_on_disk()["failed_count"] == 0

        tick(wd)
        # The windowed failures still count: 3 gateway failures inside 10 min,
        # success interleaved — exactly what the DGX leg does today.
        assert block_on_disk().get("blocked") is True, (
            "the gateway leg must share the breaker's windowed semantics")

    def test_dgx_leg_shows_the_same_windowed_behaviour_as_the_gateway_leg(
            self, wd, tmp_hfcc_dir):
        """Parity control for the test above, run on the path this card did NOT
        touch: DGX failure, DGX failure, HEALTHY, DGX failure blocks too. If
        someone later makes a healthy tick clear the history, this and the
        gateway case must move together — the two legs are one breaker.
        """
        tick(wd, dgx=False, gateway=True)
        tick(wd, dgx=False, gateway=True)
        assert tick(wd, dgx=True, gateway=True) is True
        assert block_on_disk()["failed_count"] == 0

        tick(wd, dgx=False, gateway=True)
        assert block_on_disk().get("blocked") is True, (
            "the DGX leg's windowed behaviour is the reference the gateway "
            "leg was aligned to")

    def test_healthy_streak_after_a_gateway_outage_reports_zero(
            self, wd, tmp_hfcc_dir):
        """What the success path DOES owe: once the pipeline is healthy again
        the counter reads 0 and the stream says healthy, so a recovered gateway
        is not reported as still-degraded.
        """
        tick(wd)
        assert tick(wd, dgx=True, gateway=True) is True

        block = block_on_disk()
        assert block["failed_count"] == 0
        assert block.get("blocked") is not True
        assert state_on_disk()["ok"] is True
        assert state_on_disk()["message"] == "Pipeline healthy"

    def test_three_gateway_failures_spaced_beyond_the_window_do_not_block(
            self, wd, tmp_hfcc_dir):
        """Cadence check against the real numbers: the watchdog ticks every
        30 s, so 3 failures must arrive inside 10 min to count. Seeding two
        aged-out entries then ticking once pins that spacing is honoured."""
        from hscc_daemon import lifecycle
        lifecycle.save_watchdog_block({
            "blocked": False, "reason": "", "blocked_at": None,
            "auto_restart_count": 0,
            "failures": [
                {"timestamp": _ts(11), "dgx": True, "gateway": False},
                {"timestamp": _ts(10.5), "dgx": True, "gateway": False},
            ],
        })
        tick(wd)
        assert block_on_disk().get("blocked") is not True


# ---------------------------------------------------------------------------
# The deliberate carve-out: an intentional autodown is not a fault.
# ---------------------------------------------------------------------------

class TestIntentionalAutodownCarveOut:

    def test_intentional_autodown_gateway_failure_does_not_accumulate(
            self, wd, tmp_hfcc_dir):
        """§5 / C2: while an intentional autodown is in effect the serving
        layer is down BY DESIGN, so the gateway leg fails for a reason that is
        not a fault. Three such ticks must not block the pipeline and must not
        alarm — and must still leave the block's history empty, the way the
        pre-fix code behaved on this path."""
        from hscc_daemon import lifecycle
        lifecycle.save_watchdog_block({
            "blocked": False, "reason": "", "blocked_at": None,
            "intentional": "autodown",
            "auto_restart_count": 0, "failures": [],
        })

        for _ in range(3):
            assert tick(wd) is False

        block = block_on_disk()
        assert block.get("blocked") is not True, (
            "a deliberate power-save must never trip the pipeline breaker")
        assert _gateway_entries(block) == [], (
            "an expected-down gateway during autodown is not a failure record")
        assert [n for n in wd["notifications"] if n[2] == "critical"] == []
        assert wd["restarts"] == []

        entry = state_on_disk()
        assert entry["ok"] is False
        assert entry.get("intentional") == "autodown", (
            "the stamp keeps the marker verify needs to excuse a down stream")

    def test_waking_window_records_nothing(self, wd, tmp_hfcc_dir):
        """The WAKE half of the intentional window must also stay out of the
        history. `health._intentional_window()` (which is what tags a failing
        `check_gateway()` as a non-fault) requires the block to be BOTH
        `intentional == "autodown"` AND latched, and a latched block returns from
        the backoff fork at the top of the cycle — before any check runs. So a
        wake in progress cannot feed the breaker at all. This pins that, because
        the fix newly makes gateway failures persist and the wake is the window
        where a false alarm would be loudest.
        """
        from hscc_daemon import lifecycle
        lifecycle.save_watchdog_block({
            "blocked": True, "reason": "intentional autodown",
            "blocked_at": (datetime.datetime.now(datetime.timezone.utc)
                           + datetime.timedelta(minutes=30)).isoformat(),
            "intentional": "autodown",
            "auto_restart_count": 0, "failures": [],
        })

        for _ in range(3):
            assert tick(wd) is False

        assert _gateway_entries(block_on_disk()) == []
        assert [n for n in wd["notifications"] if n[2] == "critical"] == []

    def test_a_loading_layer_routes_to_the_dgx_leg_not_the_gateway_leg(
            self, wd, tmp_hfcc_dir):
        """Blast-radius pin for the fix. A gateway failure during a model LOAD
        must not be charged to the new gateway-only accumulation: while the
        layer is loading the DGX leg fails too, so the tick takes the pre-existing
        DGX branch (restart attempted) and the gateway entry merely rides along on
        the path that already persisted. `check_gateway()`'s `ok` is
        `job_ok and vllm_ok`, so a gateway-only failure means the serving layer
        IS answering — a real gateway fault, not a wake in progress.
        """
        tick(wd, dgx=False, gateway=False)

        assert len(wd["restarts"]) == 1, (
            "a down serving layer is a DGX-path fault: the restart gate must open")
        block = block_on_disk()
        entry = block["failures"][-1]
        assert entry["dgx"] is False and entry["gateway"] is False


# ---------------------------------------------------------------------------
# Negative control: the DGX path (already persisted before this card) is not
# disturbed, and a mixed history still counts legs correctly.
# ---------------------------------------------------------------------------

class TestDgxPathUnchanged:

    def test_dgx_only_failures_still_trip_the_breaker_and_restart(self, wd,
                                                                  tmp_hfcc_dir):
        """The path that already persisted must keep working exactly as it
        did: DGX-only failures restart vLLM on the 1-2 ticks and block on the
        3rd, with the reason naming the DGX leg."""
        tick(wd, dgx=False, gateway=True)
        tick(wd, dgx=False, gateway=True)
        assert len(wd["restarts"]) == 2
        tick(wd, dgx=False, gateway=True)

        block = block_on_disk()
        assert block.get("blocked") is True
        assert "DGX=FAIL" in block["reason"] and "GW=OK" in block["reason"]

    def test_mixed_history_counts_a_gateway_only_tick(self, wd, tmp_hfcc_dir):
        """DGX failure + gateway-only failure + DGX failure = 3 recent
        failures in the window. The gateway-only one only counts because it
        is now persisted; before the fix the streak would have been 2 and the
        pipeline would keep cycling."""
        tick(wd, dgx=False, gateway=True)
        tick(wd, dgx=True, gateway=False)
        tick(wd, dgx=False, gateway=True)

        assert block_on_disk().get("blocked") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
