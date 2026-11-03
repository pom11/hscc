"""The pipeline breaker is a 10-minute TIME WINDOW, not a consecutive streak.

Found while fixing t_b0811ce0, which had to pin this behaviour rather than
change it. This card (t_49013b1b) settles the policy question it raised.

WHAT THE CODE DOES. `pipeline_watchdog` counts failures with

    recent = [f for f in block["failures"]
              if not f.get("dgx", True) or not f.get("gateway", True)]
    if len(recent) >= 3: ...block...

and `block["failures"]` is pruned only by TIME (`cleanup_old_failures`,
timestamp cutoff). The success path appends a success entry and sets
`failed_count = 0` but removes NO failure entries. So a failure that is still
inside the 10-minute window keeps counting toward the breaker even when the
pipeline healed in between. Probed on main @ 9bfe6779 (DGX leg): fail, fail,
HEALTHY, fail -> `blocked: True`. Three failures that were never consecutive.

WHAT WAS WRONG. Only the word "consecutive" in the breaker's reason string,
which reaches the critical desktop notification, the daemon log and `hscc
status`. It described a streak the counter never required — on the sequence
above it announced "3 consecutive failures" for a history in which one of the
four ticks was healthy. The alarm text now says what the counter measures.

THE DECISION (the card's DECIDE FIRST step): keep the WINDOW; fix the WORD.
Three reasons, in the order they were weighed:

1. Nothing else in the codebase describes this breaker as a streak, and three
   independent places describe it as a window: this function's own docstring
   ("block on 3 failures"), the 2.14.0 changelog ("the 3-failures-in-10min
   pipeline breaker", changelog.d/archive/2.14.0/t_1c545ebb.md), and the
   `len(recent) >= 3` gate. "consecutive" appears for this breaker in exactly
   one place — the user-facing string.
2. The latch is a BACKOFF, not a dead-end: `WATCHDOG_BACKOFF_MINUTES`
   auto-clears it and the watchdog resumes. So escalating on a pipeline that
   flapped and self-healed costs one notification plus 10 quiet minutes, while
   the alternative — requiring 3 back-to-back failing ticks — lets a gateway
   that dies for 30 s every 40 s (never failing twice in a row at the 30 s
   cadence) stay un-escalated forever. That is the worse failure mode for an
   alarm whose whole job is to be heard.
3. The windowed counter is not hypothetical: it escalated correctly on a real
   outage — docs/audits/false-fail-streams-t_3fe0cd05.md records a 9.5 h
   backend outage that the breaker held blocked across ("Restarts: 122").

PARITY. Both legs are ONE breaker: they append to the same `block["failures"]`
list and are counted by the same `recent` filter, so this card's change — one
shared string plus its pins — moves the DGX leg and the gateway leg together
by construction. `test_both_legs_feed_the_same_counter` pins that shared
history so a future reset rule cannot be applied to one leg alone.

NOTE FOR THE REVIEWER / for PR #187 (t_b0811ce0, gateway-only persistence):
that PR is still OPEN, so on this base a GATEWAY-only outage cannot reach the
breaker at all (its failure entry is never saved — `blocked` stays False), and
its suite carries the cross-leg parity control
`test_dgx_leg_shows_the_same_windowed_behaviour_as_the_gateway_leg`. These
cases therefore assert on the DGX leg, which can trip today. When #187 lands,
re-run both suites: the gateway leg inherits this reason string unchanged,
because the two legs share the one line that builds it.
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


def tick(wd, dgx=False, gateway=True):
    """One watchdog cycle with both legs injected — never the real checks.

    Defaults to a DGX-only failure: on this base that is the leg that can
    actually reach the breaker (see the module docstring on PR #187).
    `restart_result` is a REFUSAL so no load-grace window is opened — a
    verified restart would legitimately suppress failure counting for 20 min
    and mask the accumulation under test.
    """
    from hscc_daemon import lifecycle

    return lifecycle.pipeline_watchdog(
        check_dgx_fn=lambda: dgx,
        check_gateway_fn=lambda: gateway,
        restart_vllm_fn=lambda: wd["restarts"].append(1) or {
            "ok": False, "refused": True},
        send_macos_notification_fn=lambda title, body, **kw: wd[
            "notifications"].append((title, body, kw.get("priority"))),
    )


def block_on_disk():
    from hscc_daemon import lifecycle
    return lifecycle.load_watchdog_block()


def _ts(minutes_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=minutes_ago)).isoformat()


def _seed(block_obj):
    from hscc_daemon import lifecycle
    lifecycle.save_watchdog_block(block_obj)


def _seeded(failures):
    return {"blocked": False, "reason": "", "blocked_at": None,
            "auto_restart_count": 0, "failures": failures}


class TestReasonStringMatchesTheCounter:
    """The alarm must describe the measurement that fired it."""

    def test_reason_says_within_and_never_consecutive(self, wd, tmp_hfcc_dir):
        """THE FIX. Three DGX failures inside the window with a healthy tick
        between them trip the breaker, and the reason must not claim a
        streak."""
        tick(wd, dgx=False)
        tick(wd, dgx=False)
        assert tick(wd, dgx=True) is True          # pipeline self-heals
        tick(wd, dgx=False)

        block = block_on_disk()
        assert block.get("blocked") is True, (
            "3 failures inside the 10-min window trip the breaker even with a "
            "healthy tick between them — this is the intended window")
        reason = block.get("reason", "")
        assert "consecutive" not in reason.lower(), (
            f"the counter never required a streak; the alarm may not claim "
            f"one: {reason!r}")
        assert "failures within 10min" in reason, (
            f"expected the window wording, got {reason!r}")
        # The legs are still named truthfully: DGX failed, gateway was fine.
        assert "DGX=FAIL" in reason and "GW=OK" in reason

    def test_the_reason_is_what_the_operator_is_sent(self, wd, tmp_hfcc_dir):
        """The same string is the critical notification body and the log line
        — pin the operator-facing surfaces, not just the file."""
        tick(wd)
        tick(wd)
        tick(wd)

        critical = [n for n in wd["notifications"] if n[2] == "critical"]
        assert len(critical) == 1
        assert "consecutive" not in critical[0][1].lower()
        assert "failures within 10min" in critical[0][1]

        block_logs = [m for _, m in wd["logs"] if "BLOCKING pipeline" in m]
        assert len(block_logs) == 1
        assert "consecutive" not in block_logs[0].lower(), (
            "`hscc-api/routes_history.py` classifies on the 'Watchdog: "
            "BLOCKING pipeline' prefix — that prefix must survive, and the "
            "text after it must be honest")
        assert "failures within 10min" in block_logs[0]

    def test_reason_reports_the_actual_windowed_count(self, wd, tmp_hfcc_dir):
        """The count in the alarm is the window's count, not a hardcoded 3.

        Seeded with two in-window failures plus one expired one: only the
        live ones may be reported. A regression back to a literal "3" fails
        here.
        """
        _seed(_seeded([
            {"timestamp": _ts(2), "dgx": False, "gateway": True},
            {"timestamp": _ts(1), "dgx": False, "gateway": True},
            {"timestamp": _ts(30), "dgx": False, "gateway": True},  # expired
        ]))

        tick(wd, dgx=False)  # the 3rd live failure -> trips

        reason = block_on_disk().get("reason", "")
        assert "3 failures within 10min" in reason, (
            f"two live + this tick = 3 live (the 30-min-old one is pruned); "
            f"got {reason!r}")
        assert "4 failures" not in reason, "the expired entry must not be counted"

    def test_count_shown_when_more_than_three_in_window(self, wd, tmp_hfcc_dir):
        """4 live failures -> the alarm says 4. Honest overflow."""
        _seed(_seeded([
            {"timestamp": _ts(4), "dgx": False, "gateway": True},
            {"timestamp": _ts(3), "dgx": False, "gateway": True},
            {"timestamp": _ts(2), "dgx": False, "gateway": True},
        ]))

        tick(wd, dgx=False)

        reason = block_on_disk().get("reason", "")
        assert "4 failures within 10min" in reason, (
            f"3 seeded + this tick = 4 in the window; got {reason!r}")


class TestWindowIsThePolicy:
    """Pin the semantics the wording now describes, so a future edit cannot
    quietly move the threshold in either direction."""

    def test_interleaved_healthy_tick_does_not_reset_the_window(
            self, wd, tmp_hfcc_dir):
        """fail, fail, HEALTHY, fail -> blocked. The reference behaviour of
        the DGX leg and the reason the word 'consecutive' was wrong."""
        assert tick(wd, dgx=False) is False
        assert tick(wd, dgx=False) is False
        assert tick(wd, dgx=True) is True
        assert block_on_disk()["failed_count"] == 0, (
            "the healthy tick DOES reset the display counter")
        assert tick(wd, dgx=False) is False

        assert block_on_disk().get("blocked") is True, (
            "a self-healed tick must not erase in-window faults — if this is "
            "ever changed, the gateway leg must move with it (one breaker) "
            "and PR #187's parity control must be updated deliberately")

    def test_two_failures_split_by_a_recovery_do_not_trip(self, wd,
                                                          tmp_hfcc_dir):
        """NEGATIVE CONTROL — the pin above is not 'anything blocks'. Two
        in-window failures with a healthy tick between them stay below the
        threshold, and nothing is escalated."""
        assert tick(wd, dgx=False) is False
        assert tick(wd, dgx=True) is True
        assert tick(wd, dgx=False) is False

        block = block_on_disk()
        assert block.get("blocked") is not True
        assert [n for n in wd["notifications"] if n[2] == "critical"] == [], (
            "below the threshold no critical alarm is owed")

    def test_failures_older_than_the_window_are_not_counted(self, wd,
                                                             tmp_hfcc_dir):
        """The other edge of the window: 3 failures spaced beyond 10 min must
        NOT block, so the window cannot be read as an unbounded counter."""
        _seed(_seeded([
            {"timestamp": _ts(40), "dgx": False, "gateway": True},
            {"timestamp": _ts(35), "dgx": False, "gateway": True},
        ]))

        tick(wd, dgx=False)

        assert block_on_disk().get("blocked") is not True, (
            "only failures inside the 10-min window may trip the breaker")

    def test_both_legs_feed_the_same_counter(self, wd, tmp_hfcc_dir):
        """PARITY. The breaker has ONE history list, fed by both legs: a
        gateway-only fault and a DGX-only fault sitting in it together reach
        the threshold. There is no per-leg reset rule to give one leg a
        different policy.

        Both entries are seeded on disk so this does not depend on PR #187's
        gateway-side persistence — it pins the shared counter itself."""
        _seed(_seeded([
            {"timestamp": _ts(2), "dgx": True, "gateway": False},   # gateway leg
            {"timestamp": _ts(1), "dgx": False, "gateway": True},   # DGX leg
        ]))

        tick(wd, dgx=False, gateway=True)

        block = block_on_disk()
        assert block.get("blocked") is True, (
            "one gateway fault + two DGX faults inside the window is three "
            "failures of THIS breaker")
        assert wd["restarts"] == [], (
            "the tick that TRIPS the breaker never restarts, even when a DGX "
            "fault is in the history — the block branch returns before the "
            "restart branch ('don't do more checks or restarts'); below the "
            "threshold the DGX leg does restart, pinned in test_lifecycle.py")


class TestReasonSurvivesVerificationPlumbing:
    """`hscc verify` reads the stream, not the reason — pin that the reworded
    text keeps the stream verdict and the RED alarm exactly as they were."""

    def test_reworded_block_still_fails_verify(self, wd, tmp_hfcc_dir):
        from hscc_daemon import state as state_mod
        from hscc_daemon.verify import check_daemon_streams

        tick(wd)
        tick(wd)
        tick(wd)

        with open(f"{state_mod.STATE_DIR}/watchdog.json") as f:
            entry = json.load(f)
        assert entry["ok"] is False
        assert entry["blocked"] is True
        assert "PIPELINE BLOCKED" in entry["message"]
        assert entry["reason"] == block_on_disk()["reason"], (
            "the stream and the block file must carry the same text")

        result = check_daemon_streams(state_dir=str(state_mod.STATE_DIR))
        assert result["ok"] is False, (
            "a blocked watchdog must still read RED — rewording must not "
            "soften the verdict")
