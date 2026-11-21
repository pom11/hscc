"""The heal breaker must not treat a parked node as an outage.

WHY THIS EXISTS
---------------
``hscc node pause`` (the overlay, ``node_pauses.py``) parks ONE node on purpose
and edits nothing the cluster is built from, so a paused node's units are down
with no record anywhere the repair paths already look that says "deliberate".
The breaker is the last of those paths that still reads a pause as a fault:
every down-tick called ``note_heal()``, which appended a strike, and after
``HEAL_BREAKER_MAX`` strikes inside the window it latched, sent a ``critical``
desktop notification and emitted ``heal_breaker.tripped`` — for a node the
operator parked. Two wrongs in one: the operator gets paged about their own
command, and the parked node picks up a slate of strikes that outlives the
pause and keeps it unhealable after the resume.

THE BOTH-HALVES RULE
--------------------
A skip that always skips is worthless, so every property here is pinned from
both sides: a paused node accrues nothing, and an unpaused node (or a node
beside the one that is parked) accrues and trips EXACTLY as before.
Over-suppression is the same class of bug as the false page it removes.

ASK ``covering``, NEVER ``paused``
----------------------------------
``node_pauses.paused(host)`` is host-keyed; the other nodes of a
tensor-parallel unit appear only in the pause record's ``span_nodes``. A seam
that asked ``paused()`` would keep striking the rest of a span the operator
stopped as one thing. ``test_span_sibling_node_pause_suppresses_the_strike``
pins the choice by asserting the two APIs answer DIFFERENTLY for the sibling
node and that the behaviour follows the ``covering`` answer, so swapping
``covering()`` for ``paused()`` reddens this file. It is the same pin the
watchdog seam makes (t_4ecbd9d0) — every skip seam owes it.

THE BREAKER ITSELF IS NOT DISARMED
---------------------------------
Only *accrual* is suppressed. ``suspended()`` stays truthful, so a node that
was already in timeout when it got parked stays in timeout. Making
``suspended()`` answer 0 for a paused node would hand a repair path that never
asks the overlay a green light ON a parked node — over-suppression, and the
opposite of what the breaker exists for.

WHAT THIS SEAM DOES NOT DO
--------------------------
It does not stop a heal from being *attempted* on a parked node: that belongs
to the seam on the relaunch path itself (a sibling card in the pause family),
not to the counter. This file pins the counter's contract only — no strike, no
trip, no page, for a node the overlay is holding.

SAFETY
------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24, documentation space, never
this fleet), every overlay lives in a per-test temp dir passed through
node_pauses' own ``directory=`` seam, and the strike ledger is redirected to a
tmp path. No node, sparkrun, ssh or real ``~/.hscc`` is touched; the real-caller
test plants its pause at the conftest-sealed root and guard-asserts that root
is inside the tmp dir before writing to it.
"""

import json
import os

import pytest

from hscc_daemon import heal_breaker, node_pauses

# TEST-NET-1 (RFC 5737) — documentation space, never routable, never this fleet.
PAUSED_NODE = "192.0.2.10"
SPAN_SIBLING = "192.0.2.11"
UNRELATED_NODE = "192.0.2.20"
WORKER_NODE = "192.0.2.2"


@pytest.fixture
def ledger(tmp_hfcc_dir, monkeypatch):
    """Point the strike ledger at this test's tmp dir (conftest's autouse seal
    already does; explicit so the on-disk assertions below are meaningful)."""
    path = str(tmp_hfcc_dir / "heal-breaker-pause-test.json")
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_FILE", path)
    if os.path.exists(path):
        os.unlink(path)
    return path


@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root for this test's pauses (node_pauses' only seam)."""
    d = tmp_path / "overlay"
    d.mkdir()
    return str(d)


@pytest.fixture
def small_threshold(monkeypatch):
    """Lower the cap so tests need 3 actions, not 7 (production cap is
    deliberately above a legitimate ``cluster up`` fan-out)."""
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_MAX", 3)
    return 3


def park(host, directory, **kw):
    """Record a pause in this test's overlay; returns the stored entry."""
    return node_pauses.pause(host, directory=directory, **kw)


class TestPausedNodeAccruesNothing:
    """The card's first sentence: while paused, failures must not accrue."""

    def test_no_strikes_and_no_trip_while_paused(self, ledger, overlay,
                                                 small_threshold):
        park(PAUSED_NODE, overlay, reason="training run")
        results = [heal_breaker.note_heal(PAUSED_NODE, directory=overlay)
                   for _ in range(small_threshold + 5)]
        assert [r["tripped"] for r in results] == [False] * len(results)
        assert results[-1]["count"] == 0
        assert heal_breaker.strike_count(PAUSED_NODE) == 0
        assert heal_breaker.suspended(PAUSED_NODE) == 0.0

    def test_the_suppressed_path_writes_no_ledger_at_all(self, ledger,
                                                         overlay):
        """A pause must not even touch the ledger. The file is the breaker's
        durable memory across daemon restarts; a suppressed tick that opened it
        would leave a ``{}``-shaped record and, worse, would be a write on every
        parked tick."""
        park(PAUSED_NODE, overlay, reason="training run")
        for _ in range(3):
            heal_breaker.note_heal(PAUSED_NODE, directory=overlay)
        assert os.path.exists(ledger) is False

    def test_the_pause_is_reported_to_the_caller(self, ledger, overlay):
        """The caller logs the skip, so the reason has to travel with the
        result — the reader renders it instead of re-deciding it."""
        park(PAUSED_NODE, overlay, reason="training run")
        res = heal_breaker.note_heal(PAUSED_NODE, directory=overlay)
        assert res["tripped"] is False
        assert res["paused"] is True
        assert res["paused_reason"] == "training run"
        assert res["suspended_s"] == 0.0
        # Shape stays stable in both directions, so a caller can read the keys
        # without a presence check.
        plain = heal_breaker.note_heal(UNRELATED_NODE, directory=overlay)
        assert plain["paused"] is False
        assert plain["paused_reason"] == ""
        assert plain["tripped"] is False

    def test_a_pause_without_a_reason_reports_an_empty_reason(self, ledger,
                                                              overlay):
        park(PAUSED_NODE, overlay)
        res = heal_breaker.note_heal(PAUSED_NODE, directory=overlay)
        assert res["paused"] is True
        assert res["paused_reason"] == ""

    def test_accrual_resumes_on_resume_and_keeps_earlier_strikes(
            self, ledger, overlay, small_threshold):
        """Both halves again: the pause suppresses only what happens DURING it.
        Strikes earned before the pause still count afterwards — a pause is not
        an amnesty for a node that was really failing."""
        assert heal_breaker.note_heal(PAUSED_NODE, directory=overlay)["count"] == 1
        assert heal_breaker.note_heal(PAUSED_NODE, directory=overlay)["count"] == 2
        park(PAUSED_NODE, overlay, reason="training run")
        for _ in range(small_threshold):
            assert heal_breaker.note_heal(
                PAUSED_NODE, directory=overlay)["count"] == 2
        node_pauses.clear(PAUSED_NODE, directory=overlay)
        # 3rd strike: still under the cap...
        assert heal_breaker.note_heal(
            PAUSED_NODE, directory=overlay)["tripped"] is False
        # ...4th trips, counting the two that predate the pause.
        assert heal_breaker.note_heal(
            PAUSED_NODE, directory=overlay)["tripped"] is True


class TestThePauseRecordIsReadOnly:
    """The breaker consults the overlay; it never edits it. Only the CLI and the
    daemon's resume path end a pause."""

    def test_suppressed_ticks_leave_the_overlay_byte_identical(self, overlay):
        park(PAUSED_NODE, overlay, reason="training run",
             units=["vllm-27b"], span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        path = os.path.join(overlay, node_pauses.OVERLAY_NAME)
        before = open(path, "rb").read()
        for _ in range(4):
            heal_breaker.note_heal(PAUSED_NODE, directory=overlay)
            heal_breaker.suspended(PAUSED_NODE)
        assert open(path, "rb").read() == before


class TestSpanAwareness:
    """""`covering()`, never `paused()` — the pin every skip seam owes."""""

    def test_span_sibling_node_pause_suppresses_the_strike(self, ledger,
                                                           overlay):
        """The unit was stopped as ONE thing across two hosts. The sibling is
        parked too, even though no pause is keyed to it."""
        park(PAUSED_NODE, overlay, reason="tp2 training run",
             span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        # The two APIs genuinely disagree here — that is the whole point, and
        # it is what makes this assertion bite.
        assert node_pauses.paused(SPAN_SIBLING, directory=overlay) is None
        assert node_pauses.covering(SPAN_SIBLING, directory=overlay) is not None
        res = heal_breaker.note_heal(SPAN_SIBLING, directory=overlay)
        assert res["tripped"] is False
        assert res["paused"] is True
        assert heal_breaker.strike_count(SPAN_SIBLING) == 0

    def test_an_unrelated_pause_does_not_disarm_the_breaker(
            self, ledger, overlay, small_threshold):
        """A real fault on a node nobody parked must still count, still trip
        and still alert — over-suppression would trade a false page for a real
        cascade, which is the incident this breaker exists for."""
        park(PAUSED_NODE, overlay, reason="training run")
        counts = [heal_breaker.note_heal(UNRELATED_NODE,
                                         directory=overlay)["count"]
                  for _ in range(small_threshold + 1)]
        assert counts == [1, 2, 3, 4]
        assert heal_breaker.suspended(UNRELATED_NODE) > 0


class TestTheBreakerIsNotDisarmedByAPause:
    """Accrual is suppressed; the refusal decision stays truthful."""

    def test_a_suspension_that_predates_the_pause_still_holds(self, ledger,
                                                               overlay,
                                                               small_threshold):
        base = 1_000_000.0
        window = heal_breaker.HEAL_BREAKER_WINDOW_S
        for i in range(small_threshold + 1):
            heal_breaker.note_heal(PAUSED_NODE, now=base + i,
                                   directory=overlay)
        assert heal_breaker.suspended(PAUSED_NODE, now=base) > 0
        park(PAUSED_NODE, overlay, reason="training run")
        # Parked: still in timeout, still the same strike slate.
        remaining = heal_breaker.suspended(PAUSED_NODE, now=base)
        assert remaining > 0
        assert heal_breaker.strike_count(PAUSED_NODE, now=base) == (
            small_threshold + 1)

    def test_a_pause_cannot_extend_a_suspension(self, ledger, overlay,
                                                small_threshold):
        """The self-clearing property the module docstring promises must not
        become pause-dependent: suppressed ticks write nothing, so the oldest
        strike — the one the expiry is computed from — never moves."""
        base = 1_000_000.0
        window = heal_breaker.HEAL_BREAKER_WINDOW_S
        for i in range(small_threshold + 1):
            heal_breaker.note_heal(PAUSED_NODE, now=base + i,
                                   directory=overlay)
        park(PAUSED_NODE, overlay, reason="training run")
        remaining = heal_breaker.suspended(PAUSED_NODE, now=base)
        for i in range(small_threshold + 5):
            res = heal_breaker.note_heal(PAUSED_NODE, now=base + i,
                                         directory=overlay)
            assert res["tripped"] is False
        assert heal_breaker.suspended(PAUSED_NODE, now=base) == remaining
        # And it still ages out on its own, pause or no pause.
        assert heal_breaker.suspended(
            PAUSED_NODE, now=base + window + 1) == 0.0

    def test_the_suspension_still_holds_when_the_pause_is_at_the_default_root(
            self, ledger, tmp_hfcc_dir, small_threshold):
        """The daemon's own configuration, and the only one ``suspended()`` can
        see: it takes no ``directory`` (deliberately — it reads the ledger, not
        the overlay), so this is the test that catches someone "hardening" it to
        answer 0 while a pause is in force. A parked node that was already in
        timeout must STILL report the timeout: a repair path that never consults
        the overlay would otherwise walk straight up to a parked node and heal
        it, and the breaker would look armed while it was gutted."""
        root = str(tmp_hfcc_dir)
        assert os.path.realpath(node_pauses.HSCC_DIR) == os.path.realpath(root), (
            "conftest's node_pauses.HSCC_DIR seal moved; refusing to write a "
            "pause anywhere but the per-test tmp root")
        base = 1_000_000.0
        for i in range(small_threshold + 1):
            heal_breaker.note_heal(PAUSED_NODE, now=base + i)
        assert heal_breaker.suspended(PAUSED_NODE, now=base) > 0
        park(PAUSED_NODE, root, reason="training run")
        # Parked at the root the daemon reads: still in timeout, same slate.
        assert heal_breaker.suspended(PAUSED_NODE, now=base) > 0
        assert heal_breaker.strike_count(PAUSED_NODE, now=base) == (
            small_threshold + 1)
        # And the tick is still free — suppression and truthfulness at once.
        assert heal_breaker.note_heal(PAUSED_NODE, now=base)["tripped"] is False

    def test_the_operator_can_clear_a_parked_node_and_the_clear_sticks(
            self, ledger, overlay, small_threshold):
        """``hscc heal-clear`` on a parked node: no strikes are held, and the
        pause does not silently refill them."""
        base = 1_000_000.0
        for i in range(small_threshold + 1):
            heal_breaker.note_heal(PAUSED_NODE, now=base + i,
                                   directory=overlay)
        park(PAUSED_NODE, overlay, reason="training run")
        assert heal_breaker.clear(PAUSED_NODE, reason="parked") is True
        assert heal_breaker.suspended(PAUSED_NODE, now=base) == 0.0
        assert heal_breaker.note_heal(
            PAUSED_NODE, now=base, directory=overlay)["count"] == 0


class TestFailOpenDirection:
    """A broken overlay must never freeze the fleet's ability to repair itself.
    ``node_pauses.load()`` reads "no pauses" for a missing or malformed file
    (loudly WARNed) and this seam inherits that direction."""

    def test_missing_overlay_is_not_a_pause(self, ledger, overlay,
                                            small_threshold):
        res = heal_breaker.note_heal(UNRELATED_NODE, directory=overlay)
        assert res["paused"] is False
        assert res["count"] == 1

    def test_a_malformed_overlay_fails_open_and_can_still_trip(
            self, ledger, overlay, small_threshold):
        path = os.path.join(overlay, node_pauses.OVERLAY_NAME)
        with open(path, "w") as f:
            f.write('{"pauses": [this is not an object')
        results = [heal_breaker.note_heal(UNRELATED_NODE, directory=overlay)
                   for _ in range(small_threshold + 1)]
        assert results[-1]["tripped"] is True
        assert heal_breaker.suspended(UNRELATED_NODE) > 0

    def test_a_pause_written_by_another_process_is_honoured(self, ledger,
                                                            overlay,
                                                            small_threshold):
        """Durability in the direction that matters here: the writer is the CLI,
        the reader is the daemon loop, and there is no in-process cache between
        them. Park AFTER a strike has already landed and the very next tick
        must see the pause."""
        assert heal_breaker.note_heal(
            PAUSED_NODE, directory=overlay)["count"] == 1
        park(PAUSED_NODE, overlay, reason="training run")
        assert heal_breaker.note_heal(
            PAUSED_NODE, directory=overlay)["count"] == 1


class TestDefaultRootIsTheOneTheDaemonUses:
    """``health._note_heal_action()`` calls ``note_heal(node)`` with NO
    directory — production reads the default root. A seam that only worked when
    a caller threaded a directory would be a seam nobody uses, so the default
    path is pinned too, including the guard that the root under test is the
    conftest-sealed tmp root and not the operator's real ``~/.hscc``."""

    def test_a_pause_at_the_default_root_suppresses_accrual(self, ledger,
                                                            tmp_hfcc_dir,
                                                            small_threshold):
        root = str(tmp_hfcc_dir)
        assert os.path.realpath(node_pauses.HSCC_DIR) == os.path.realpath(root), (
            "conftest's node_pauses.HSCC_DIR seal moved; refusing to write a "
            "pause anywhere but the per-test tmp root")
        park(PAUSED_NODE, root, reason="training run")
        results = [heal_breaker.note_heal(PAUSED_NODE)
                   for _ in range(small_threshold + 3)]
        assert [r["tripped"] for r in results] == [False] * len(results)
        assert heal_breaker.strike_count(PAUSED_NODE) == 0


class TestThroughTheRealCaller:
    """Integration: the contract has to hold through ``check_workers()``, which
    is the path that actually strikes — the same harness
    ``test_heal_breaker.TestBreakerStopsARealStorm`` uses."""

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        units = [{"id": "orch", "role": "orchestrator", "nodes": ["192.0.2.1"],
                  "recipe": "~/r/orch.yaml", "model": "M"}]
        for n in nodes:
            units.append({"id": f"w-{n}", "role": "worker", "keepalive": True,
                          "nodes": [n], "recipe": "~/r/27b.yaml", "model": "W"})
        monkeypatch.setattr(serving, "ORCH_NODES", {"192.0.2.1"})
        monkeypatch.setattr(serving, "load_serving",
                            lambda: {"version": 1, "units": units})
        health._worker_relaunch_at.clear()
        health._worker_down_streak.clear()
        health._worker_last_autoheal.clear()
        health._worker_container_state.clear()
        import time as _t
        _old = _t.time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: _old)
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": False})
        monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": True})
        monkeypatch.setattr(health.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 1)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_COOLDOWN_MINUTES", 0)
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: {"status": "ok"})
        notes, events = [], []
        monkeypatch.setattr(health, "send_macos_notification",
                            lambda title, body, **k: notes.append((title, body))
                            or True)
        monkeypatch.setattr(health, "emit_event",
                            lambda t, p, **k: events.append((t, p)))
        return health, notes, events

    def _park_worker(self, tmp_hfcc_dir):
        """Pause the worker node at the root the daemon reads by default.
        Guard-asserted: this must be the per-test tmp root, never the real one."""
        root = str(tmp_hfcc_dir)
        assert os.path.realpath(node_pauses.HSCC_DIR) == os.path.realpath(root), (
            "conftest's node_pauses.HSCC_DIR seal moved; refusing to write a "
            "pause anywhere but the per-test tmp root")
        park(WORKER_NODE, root, reason="training run")

    def test_a_parked_worker_costs_no_strikes_and_no_page(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        health, notes, events = self._setup(tmp_hfcc_dir, monkeypatch,
                                            nodes=[WORKER_NODE])
        self._park_worker(tmp_hfcc_dir)
        for _ in range(small_threshold + 5):
            health.check_workers()
        assert heal_breaker.strike_count(WORKER_NODE) == 0
        assert heal_breaker.suspended(WORKER_NODE) == 0.0
        assert notes == [], f"a pause paged the operator: {notes}"
        assert events == [], f"a pause tripped the breaker: {events}"

    def test_the_same_node_with_no_pause_still_trips_and_still_pages(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        """The control. Without it every assertion above is satisfied by a
        breaker that simply never fires."""
        health, notes, events = self._setup(tmp_hfcc_dir, monkeypatch,
                                            nodes=[WORKER_NODE])
        for _ in range(small_threshold + 5):
            health.check_workers()
        assert heal_breaker.strike_count(WORKER_NODE) > small_threshold
        assert heal_breaker.suspended(WORKER_NODE) > 0
        assert [n[0] for n in notes] == ["HSCC: heal breaker tripped"]
        assert [e[0] for e in events] == ["heal_breaker.tripped"]

    def test_a_pause_on_one_node_leaves_a_real_fault_on_another_counting(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        """Per-node isolation THROUGH the real path, with a pause in play: the
        parked worker accrues nothing, the unparked one still trips."""
        health, _notes, _events = self._setup(tmp_hfcc_dir, monkeypatch,
                                              nodes=[WORKER_NODE, "192.0.2.3"])
        self._park_worker(tmp_hfcc_dir)
        for _ in range(small_threshold + 5):
            health.check_workers()
        assert heal_breaker.strike_count(WORKER_NODE) == 0
        assert heal_breaker.strike_count("192.0.2.3") > small_threshold
        assert heal_breaker.suspended("192.0.2.3") > 0


class TestLedgerShapeUnchanged:
    """The ledger format is a human-read artefact (``hscc heal-status``), and a
    suppressed tick must not add a key or a node to it."""

    def test_the_ledger_holds_only_the_nodes_that_were_actually_struck(
            self, ledger, overlay):
        park(PAUSED_NODE, overlay, reason="training run")
        heal_breaker.note_heal(PAUSED_NODE, directory=overlay)
        heal_breaker.note_heal(UNRELATED_NODE, directory=overlay)
        data = json.load(open(ledger))
        assert list(data) == [UNRELATED_NODE]
        assert isinstance(data[UNRELATED_NODE], list)
