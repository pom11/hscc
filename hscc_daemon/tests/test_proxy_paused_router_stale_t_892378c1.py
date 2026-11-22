"""A paused host's leftover router entry must be SEEN, never counted (t_892378c1).

WHY THIS EXISTS
---------------
``_proxy_backend_drift`` compares what the running proxy routes to against what
is actually up, in two directions: MISSING (a healthy unit the router dropped)
and DEAD (a router entry whose unit is down). ``t_90958eb8`` then subtracted
``expected_absent`` — every backend whose HOST the operator parked — from BOTH
directions, and that subtraction is correct: ``hscc node pause`` stops a node's
units on purpose and edits nothing the router is built from, so the leftover
endpoint is what a pause LOOKS like, not a fault. Reading it as DEAD is the bug
that stopped the fleet's only entrypoint.

The cost of subtracting from both is that the DEAD direction became
structurally invisible for as long as a node stays parked. Across the whole
daemon log there are two MISSING verdicts ever and ZERO stale/extra-backend
verdicts of any kind: the DEAD direction has never once fired, because every
shape it could have fired on was a parked host. So on 2026-11-22 — node paused
at ~19:53-19:55Z, first client failure at 19:58Z — the daemon could not answer
"does the live router still point at that paused host?", and that question is
the one alternative explanation the audit could not exclude
(``docs/audits/2026-11-22-proxy-connection-error.md`` §3.2/§4.3).

WHAT THIS FILE PINS
-------------------
1. VISIBILITY. The set the exclusion already computes — ``expected_absent``
   intersected with what the router actually answers — is printed once as a
   WARN naming the backends and the hosts. No new probe, no new I/O: the sets
   are already in hand at the call site.
2. NON-REGRESSION of t_90958eb8. The WARN must not move the verdict. Every
   existing ``_proxy_backend_drift`` fixture still returns a BYTE-IDENTICAL
   ``(healthy, reason)`` tuple, and the pause shape that used to stop the proxy
   still returns ``True`` with no ``proxy stop``, no heal lease, no strike, no
   event and no notification. A pinned guarantee must survive the feature that
   only looks at it.
3. THE OPPOSITE POSTURE. A genuinely unhealthy backend on a node nobody parked
   still reports drift and still takes the repair, and the new WARN stays
   silent when nothing is parked.
4. ONE LINE PER CHANGE. A node parked for a day costs one WARN, not one per
   check tick (the FLEET VISIBILITY latch is the model); the transition back
   prints the matching clear line, so a WARN that can fire also can end.

SAFETY (the no-live rule)
-------------------------
Fakes only. Hosts are TEST-NET-1 (RFC 5737, documentation space). The rig, the
planted ``sparkrun`` and the pause overlay are imported from the t_90958eb8
suite, which keeps ``sparkrun`` off PATH through ``HSCC_SPARKRUN_BIN``, stubs
the lsof/port owner and passes a per-test overlay through ``node_pauses``' own
``directory=`` seam — so nothing here reaches the fleet, a real ``:4000``, or
the operator's ``~/.hscc``.
"""

import pytest

from hscc_daemon import health
from hscc_daemon.tests.test_proxy_pause_drift_t_90958eb8 import (  # noqa: F401
    DEAD,
    PARKED,
    SIBLING,
    UP,
    _Rig,
    commands,
    hp,
    logs,  # noqa: F401 (fixture)
    overlay,  # noqa: F401 (fixture)
    alerts,  # noqa: F401 (fixture)
    fake_sparkrun,  # noqa: F401 (fixture)
    park,
    start_argvs,
)


# ── 2. the verdict must not move: byte-identical goldens ───────────────────

# Captured from ``_proxy_backend_drift`` on the PRE-CHANGE tree (origin/main),
# for every fixture the existing suites feed it. This table is the t_90958eb8
# non-regression proof: a visibility feature that changed a verdict — in either
# direction — reddens here before it can reach the fleet's entrypoint.
GOLDEN_VERDICTS = {
    "test_health:test_backend_probe_failure_is_not_drift": (
        True,
        "no live backends to compare (catalogue check owns that)"),
    "test_health:test_drift_missing_backend_is_unhealthy": (
        False,
        "live router missing backend(s) ['192.0.2.3:8002', '192.0.2.4:8003'] "
        "— healthy unit(s) the proxy is not serving"),
    "test_health:test_drift_dead_backend_is_unhealthy": (
        False,
        "live router points at unhealthy backend(s) ['192.0.2.9:8099'] "
        "— load is being sent to a dead worker"),
    "test_health:test_drift_ignores_backends_hscc_does_not_supervise": (
        True,
        "live router matches 1 healthy backend(s)"),
    "test_health:test_drift_matches_is_healthy": (
        True,
        "live router matches 2 healthy backend(s)"),
    "t_90958eb8:a_paused_nodes_backend_is_expected_absent_not_drift": (
        True,
        "live router matches 1 healthy backend(s) "
        "[1 backend(s) on paused node(s) expected-absent]"),
    "t_90958eb8:the_span_sibling_of_a_parked_span_is_expected_absent": (
        True,
        "live router matches 1 healthy backend(s) "
        "[2 backend(s) on paused node(s) expected-absent]"),
}

# The same inputs the goldens were captured from. One substitution: the
# "not supervised" fixture's foreign backend is written as TEST-NET-1
# (192.0.2.99:9000) where the pre-existing fixture in test_health.py uses a
# bare non-fleet host — the host is irrelevant to that verdict precisely
# because it is OUTSIDE the fleet, so the documented placeholder is faithful
# and the captured tuple is unchanged.
GOLDEN_INPUTS = {
    "test_health:test_backend_probe_failure_is_not_drift": (
        (set(), {"192.0.2.2:8001"}, {"192.0.2.2:8001"}), {}),
    "test_health:test_drift_missing_backend_is_unhealthy": (
        ({"192.0.2.2:8001"},
         {"192.0.2.2:8001", "192.0.2.3:8002", "192.0.2.4:8003"},
         {"192.0.2.2:8001", "192.0.2.3:8002", "192.0.2.4:8003"}), {}),
    "test_health:test_drift_dead_backend_is_unhealthy": (
        ({"192.0.2.2:8001", "192.0.2.9:8099"}, {"192.0.2.2:8001"},
         {"192.0.2.2:8001", "192.0.2.9:8099"}), {}),
    "test_health:test_drift_ignores_backends_hscc_does_not_supervise": (
        ({"192.0.2.2:8001", "192.0.2.99:9000"}, {"192.0.2.2:8001"},
         {"192.0.2.2:8001"}), {}),
    "test_health:test_drift_matches_is_healthy": (
        ({"192.0.2.2:8001", "192.0.2.3:8002"},) * 3, {}),
    "t_90958eb8:a_paused_nodes_backend_is_expected_absent_not_drift": (
        ({hp(PARKED), hp(UP)}, {hp(UP)}, {hp(PARKED), hp(UP)}),
        {"paused_nodes": {PARKED}}),
    "t_90958eb8:the_span_sibling_of_a_parked_span_is_expected_absent": (
        ({hp(SIBLING), hp(UP)}, {hp(UP)},
         {hp(SIBLING), hp(UP), hp(PARKED)}),
        {"paused_nodes": {PARKED, SIBLING}}),
}


class TestVerdictDoesNotMove:
    """(c) The visibility WARN is a bystander. It must not touch the verdict."""

    @pytest.mark.parametrize("label", sorted(GOLDEN_INPUTS))
    def test_every_existing_fixture_verdict_is_byte_identical(self, label):
        args, kwargs = GOLDEN_INPUTS[label]
        got = health._proxy_backend_drift(*args, **kwargs)
        want = GOLDEN_VERDICTS[label]
        assert got == want, (
            f"{label}: the verdict moved under a visibility-only change\n"
            f"  before: {want!r}\n  after:  {got!r}")
        assert [type(x) for x in got] == [bool, str], (
            f"{label}: the return shape changed: {got!r}")

    def test_the_stale_set_is_not_a_second_opinion_on_the_verdict(self):
        """The WARN's input set and the verdict must disagree on purpose: the
        same backend is expected-absent to the verdict and visible to the
        operator. If a future change 'harmonises' them, t_90958eb8 is gone."""
        live = {hp(PARKED), hp(UP)}
        fleet = {hp(PARKED), hp(UP)}
        healthy = {hp(UP)}
        ok, reason = health._proxy_backend_drift(live, healthy, fleet,
                                                 paused_nodes={PARKED})
        _missing, _dead, absent = health._proxy_backend_drift_sets(
            live, healthy, fleet, paused_nodes={PARKED})
        stale = health._paused_router_stale_backends(absent, live)
        assert ok is True and "expected-absent" in reason
        assert stale == {hp(PARKED)}, (
            "the stale set must name exactly the router entry the exclusion "
            f"swallowed, got {stale}")


# ── 1. the stale entry is now visible ──────────────────────────────────────

class TestPausedStaleRouterIsVisible:
    """(a) The parked host the router still holds gets named, once."""

    def test_the_stale_set_is_expected_absent_intersected_with_the_router(self):
        """Composed from the REAL ``expected_absent`` the drift math returns,
        then intersected with what the router answers: a backend the router
        does NOT have cannot be stale, and a paused host the router already
        dropped is not stale either."""
        live = {hp(PARKED), hp(SIBLING, 8002), hp(UP)}
        fleet = live | {hp(DEAD)}
        healthy = {hp(UP)}
        _missing, _dead, absent = health._proxy_backend_drift_sets(
            live, healthy, fleet, paused_nodes={PARKED, SIBLING})
        assert absent == {hp(PARKED), hp(SIBLING, 8002)}, (
            f"expected-absent is not what the WARN intersects: {absent}")
        stale = health._paused_router_stale_backends(absent, live)
        assert stale == {hp(PARKED), hp(SIBLING, 8002)}, stale
        assert hp(UP) not in stale, "an in-service backend was called stale"
        assert health._paused_router_stale_backends(absent, set()) == set()
        assert health._paused_router_stale_backends(set(), live) == set()

    def test_a_parked_backend_still_in_the_router_warns(self, logs,
                                                        monkeypatch):
        """THE AUDIT HOLE, closed at the unit level: the router answers with an
        endpoint whose host is parked ⇒ the operator is told, and the verdict
        still says healthy."""
        _reset_latch(monkeypatch)
        live = {hp(PARKED), hp(UP)}
        _missing, _dead, absent = health._proxy_backend_drift_sets(
            live, {hp(UP)}, live, paused_nodes={PARKED})
        logged = health._note_paused_router_stale(absent, live)
        assert logged is not None
        assert any(lvl == "WARN" and "still points at" in msg
                   for lvl, msg in logs), logs
        msg = [m for _l, m in logs if "still points at" in m][-1]
        assert "1 backend(s) on paused host(s)" in msg
        assert hp(PARKED) in msg and PARKED in msg
        assert "not counted as drift" in msg
        assert hp(UP) not in msg, f"an in-service backend was named: {msg}"

    def test_the_warn_names_every_stale_backend_and_host(self, logs,
                                                         monkeypatch):
        _reset_latch(monkeypatch)
        health._note_paused_router_stale({hp(PARKED), hp(SIBLING, 8002)},
                                         {hp(PARKED), hp(SIBLING, 8002)})
        msg = [m for _l, m in logs if "still points at" in m][-1]
        assert "2 backend(s)" in msg
        assert hp(PARKED) in msg and hp(SIBLING, 8002) in msg
        assert PARKED in msg and SIBLING in msg

    def test_a_pause_never_becomes_a_fault_end_to_end(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun, logs,
            alerts):
        """THE t_90958eb8 GUARANTEE, re-proved on top of the new WARN: pause a
        node whose endpoint the router still holds, tick the check, and the
        entrypoint stays up — no stop, no lease, no event, no notify — while
        the WARN is now in the log. The guarantee is 'a pause never stops
        :4000'; this is the version of it that includes the new line."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, PARKED],
                   live={hp(PARKED), hp(UP)}, healthy_units={hp(UP)})
        park(PARKED, overlay, reason="training run")
        assert health.check_proxy(pause_directory=overlay) is True
        assert any(lvl == "WARN" and "still points at" in m
                   for lvl, m in logs), (
            "the stale paused-host entry was invisible: "
            f"{[m for _l, m in logs]}")
        assert "proxy stop" not in commands(fake_sparkrun), (
            "the new WARN made a pause actionable: "
            f"{commands(fake_sparkrun)}")
        assert rig.leases == [], "the WARN took a heal lease"
        assert alerts["events"] == [], "the WARN emitted an event"
        assert alerts["notify"] == [], "the WARN notified the operator"
        st = rig.state()
        assert st["ok"] is True, f"the stream filed the pause as drift: {st}"

    def test_the_verdict_tuple_survives_a_warn_on_every_shape(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun, logs):
        """Same tick, both directions: MISSING (a healthy unit the router lost)
        alongside a parked host the router still holds. The verdict is the
        MISSING verdict it has always been; the WARN is added beside it."""
        _reset_latch(monkeypatch)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, PARKED],
                   live={hp(PARKED)}, healthy_units={hp(UP)})
        park(PARKED, overlay, reason="training run")
        assert health.check_proxy(pause_directory=overlay) is True
        assert any("still points at" in m for _lvl, m in logs)
        # The router DID lose UP, so a repair is legitimate here — and it is
        # the pre-existing repair, unchanged: the same --hosts the old code
        # would have built (parked host excluded, t_90958eb8 fix 1).
        starts = start_argvs(fake_sparkrun)
        assert starts, "the MISSING direction stopped repairing"
        assert starts[-1][starts[-1].index("--hosts") + 1] == UP


# ── 3. the opposite posture: a real fault is still a fault ─────────────────

class TestRealDriftStillRepairs:
    """(b) Nothing about the DEAD direction's real teeth changes."""

    def test_a_dead_backend_on_an_unpaused_host_still_reports_drift(self):
        ok, reason = health._proxy_backend_drift(
            {hp(UP), hp(DEAD)}, {hp(UP)}, {hp(UP), hp(DEAD)},
            paused_nodes={PARKED})
        assert ok is False
        assert hp(DEAD) in reason and "dead worker" in reason

    def test_no_stale_backends_prints_nothing(self, logs, monkeypatch):
        """The suppression half: with nothing parked — or with a parked host
        the router already dropped — the check stays silent."""
        _reset_latch(monkeypatch)
        assert health._note_paused_router_stale(set(), {hp(UP)}) is None
        # Parked AND absent from the router: nothing stale to report.
        assert health._note_paused_router_stale({hp(PARKED)}, {hp(UP)}) is None
        assert logs == [], logs

    def test_a_genuinely_dead_backend_still_takes_the_repair(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun, logs,
            alerts):
        """The control half of every pin in this file: a dead unit on a node
        nobody parked must still be drift and must still be repaired — and the
        new WARN must be the only thing that changed about the tick."""
        _reset_latch(monkeypatch)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)})
        # True here means "the tick fixed it": the DEAD verdict drove the tick
        # into the repair path and the planted start verified (t_90958eb8's own
        # control test asserts the same shape). What must NOT happen is the
        # verdict going quiet, or the parked-host WARN softening it.
        assert health.check_proxy(pause_directory=overlay) is True
        assert "proxy stop" in commands(fake_sparkrun), (
            f"the repair stopped happening: {commands(fake_sparkrun)}")
        assert any("load is being sent to a dead worker" in m
                   for _lvl, m in logs), (
            f"the DEAD verdict stopped being reported: {[m for _l, m in logs]}")
        assert rig.leases, "a real DEAD verdict took no heal lease"
        assert not any("still points at" in m for _lvl, m in logs), (
            "a non-paused dead backend must not be reported as a paused-host "
            "leftover")


# ── 4. one line per change, not one per tick ───────────────────────────────

class TestWarnIsRateLimitedOnChange:
    """A node parked for a day must cost the log one line."""

    def test_repeated_ticks_print_once(self, logs, monkeypatch):
        _reset_latch(monkeypatch)
        for _ in range(50):
            health._note_paused_router_stale({hp(PARKED)}, {hp(PARKED),
                                                            hp(UP)})
        warns = [m for lvl, m in logs if lvl == "WARN"]
        assert len(warns) == 1, f"{len(warns)} WARNs for one unchanged state"
        assert "still points at" in warns[0]

    def test_a_change_in_the_stale_set_prints_again(self, logs, monkeypatch):
        _reset_latch(monkeypatch)
        health._note_paused_router_stale({hp(PARKED)}, {hp(PARKED)})
        health._note_paused_router_stale({hp(PARKED), hp(SIBLING)},
                                         {hp(PARKED), hp(SIBLING)})
        warns = [m for lvl, m in logs if lvl == "WARN"]
        assert len(warns) == 2, warns
        assert hp(SIBLING) in warns[1]

    def test_the_clear_prints_once_and_then_stays_silent(self, logs,
                                                         monkeypatch):
        """An alarm that can fire must be able to end: the transition back to
        'nothing stale' prints the clear line exactly once, and every later
        clean tick stays quiet."""
        _reset_latch(monkeypatch)
        health._note_paused_router_stale({hp(PARKED)}, {hp(PARKED)})
        for _ in range(5):
            health._note_paused_router_stale(set(), {hp(UP)})
        assert len([m for lvl, m in logs if lvl == "WARN"]) == 1
        clears = [m for lvl, m in logs if lvl == "INFO"]
        assert len(clears) == 1, f"clear lines: {clears}"
        assert "no longer points" in clears[0]
        # And a clean check that was never dirty prints nothing at all: fresh
        # latch AND fresh log buffer, two clean ticks.
        _reset_latch(monkeypatch)
        del logs[:]
        health._note_paused_router_stale(set(), {hp(UP)})
        health._note_paused_router_stale(set(), {hp(UP)})
        assert logs == [], logs


# ── helpers ────────────────────────────────────────────────────────────────

def _reset_latch(monkeypatch):
    """Per-test reset of the log-on-change latch (module state; without this
    the suite would pass or fail on test ORDER)."""
    state = dict(getattr(health, "_paused_stale_router_state", {}) or {})
    state["signature"] = None
    monkeypatch.setattr(health, "_paused_stale_router_state", state)
    assert hasattr(health, "_note_paused_router_stale"), (
        "the visibility seam does not exist yet")
