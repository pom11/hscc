"""F5 — per-node heal circuit breaker (HEAL_CASCADE 2026-10-14).

The lease (F2) made repairs sequential. This makes them FINITE per node, which
is what actually stopped a cascade: on 2026-10-14 four repair paths fired every
~60s for ~35 min against degrading hardware until two nodes hard-hung and
needed a manual power-cycle.

Properties pinned here:

  * COUNTING — each heal action strikes its node;
  * TRIP — the (max+1)-th action inside the window is refused;
  * SUSPEND — refusal continues while strikes remain in the window;
  * SELF-RECOVERY — the suspension expires by itself (a latch without an
    expiry would strand a node forever, the opposite failure);
  * DURABILITY — the ledger is a file, so a daemon restart (which the healing
    itself can cause) does NOT reset the count: this is precisely the flaw of
    the in-memory ``_worker_down_streak`` in the incident;
  * ALERT ONCE — the first trip notifies, suppressed ticks do not (the
    2026-10-08 NAS rule fired 178 notifications in 11 minutes);
  * per-node isolation — a tripped node does not disable healing fleet-wide;
  * INTEGRATION — a real ``check_workers()`` storm stops calling
    ``_autoheal_worker_fn``, and a stopped storm leaves the lease RELEASED
    (a breaker that leaked the lease would disable all healing fleet-wide).

No node, sparkrun or network is touched; the ledger is redirected into the
per-test tmp dir by conftest's autouse fixture.
"""

import json
import os

import pytest

from hscc_daemon import heal_breaker


@pytest.fixture
def ledger(tmp_hfcc_dir, monkeypatch):
    """Point the ledger at this test's tmp dir (autouse already does; explicit
    so the on-disk assertions below are meaningful)."""
    path = str(tmp_hfcc_dir / "heal-breaker-test.json")
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_FILE", path)
    if os.path.exists(path):
        os.unlink(path)
    return path


@pytest.fixture
def small_threshold(monkeypatch):
    """Lower the cap so tests need 3 actions, not 7 (the production cap is
    deliberately above a legitimate `cluster up` fan-out — see its comment)."""
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_MAX", 3)
    return 3


class TestCountingAndTrip:
    def test_actions_below_the_cap_are_allowed(self, ledger, small_threshold):
        for _ in range(small_threshold):
            res = heal_breaker.note_heal("10.0.0.2")
            assert res["tripped"] is False
        assert heal_breaker.suspended("10.0.0.2") == 0.0

    def test_action_beyond_the_cap_trips(self, ledger, small_threshold):
        for _ in range(small_threshold):
            heal_breaker.note_heal("10.0.0.2")
        res = heal_breaker.note_heal("10.0.0.2")
        assert res["tripped"] is True
        assert res["count"] == small_threshold + 1
        assert res["suspended_s"] > 0

    def test_suspension_blocks_further_heals(self, ledger, small_threshold):
        for _ in range(small_threshold + 1):
            heal_breaker.note_heal("10.0.0.2")
        assert heal_breaker.suspended("10.0.0.2") > 0

    def test_strikes_are_per_node(self, ledger, small_threshold):
        """A tripped node must NOT disable healing on the rest of the fleet —
        that would turn a targeted breaker into a cluster-wide outage."""
        for _ in range(small_threshold + 1):
            heal_breaker.note_heal("10.0.0.2")
        assert heal_breaker.suspended("10.0.0.2") > 0
        assert heal_breaker.suspended("10.0.0.3") == 0.0
        assert heal_breaker.note_heal("10.0.0.3")["tripped"] is False

    def test_a_storm_of_different_nodes_does_not_trip_any_single_one(
            self, ledger, small_threshold):
        """The cap is per node, so spreading across nodes does not add up to a
        trip. (Deliberate: a fleet-wide fault deserves fleet-wide healing; the
        breaker stops the case where one host is being destroyed.)"""
        for i in range(small_threshold * 3):
            assert heal_breaker.note_heal(f"10.0.0.{10 + i}")["tripped"] is False


class TestWindowAndSelfRecovery:
    def test_old_actions_age_out_and_do_not_count(self, ledger, small_threshold):
        """Only strikes inside the window count, so the breaker measures a
        RATE rather than a lifetime total (a healthy cluster's lifetime count
        would be large)."""
        w = heal_breaker.HEAL_BREAKER_WINDOW_S
        base = 1_000_000.0
        for _ in range(small_threshold):
            heal_breaker.note_heal("10.0.0.2", now=base - w - 10)
        # All strikes are outside the window: this next one is the FIRST of a
        # fresh window, so it must not trip.
        assert heal_breaker.note_heal("10.0.0.2", now=base)["tripped"] is False
        assert heal_breaker.suspended("10.0.0.2", now=base) == 0.0

    def test_suspension_expires_on_its_own(self, ledger, small_threshold):
        """CRITICAL: self-recovery. A latch with no expiry strands a node
        forever — the opposite failure mode, and one nobody can operate."""
        w = heal_breaker.HEAL_BREAKER_WINDOW_S
        base = 1_000_000.0
        for _ in range(small_threshold + 1):
            heal_breaker.note_heal("10.0.0.2", now=base)
        assert heal_breaker.suspended("10.0.0.2", now=base) > 0
        # The oldest strike leaving the window drops the count back under the
        # cap, and healing resumes with no operator action.
        assert heal_breaker.suspended("10.0.0.2", now=base + w + 1) == 0.0

    def test_suspension_shortens_as_strikes_age(self, ledger, small_threshold):
        w = heal_breaker.HEAL_BREAKER_WINDOW_S
        base = 1_000_000.0
        for i in range(small_threshold + 1):
            heal_breaker.note_heal("10.0.0.2", now=base + i)
        half = heal_breaker.suspended("10.0.0.2", now=base + w / 2)
        assert 0 < half < w

    def test_operator_clear_restores_healing_immediately(self, ledger,
                                                         small_threshold):
        for _ in range(small_threshold + 1):
            heal_breaker.note_heal("10.0.0.2")
        assert heal_breaker.suspended("10.0.0.2") > 0
        assert heal_breaker.clear("10.0.0.2", "investigated, fixed") is True
        assert heal_breaker.suspended("10.0.0.2") == 0.0
        # The clear is itself recorded, so the ledger is auditable.
        assert "10.0.0.2" in json.load(open(ledger))["_cleared"]

    def test_clear_of_an_unknown_node_is_a_noop(self, ledger):
        assert heal_breaker.clear("10.0.0.9") is False


class TestDurability:
    """The incident's in-memory flaw, pinned directly.

    ``_worker_down_streak`` and ``_worker_last_autoheal`` were plain dicts, so
    a daemon that crash-looped — exactly what an escalating heal storm can
    cause — reset its strike count on every restart and never reached the
    threshold. The ledger is a file, so it survives.
    """

    def test_count_survives_a_daemon_restart(self, tmp_hfcc_dir, monkeypatch,
                                              small_threshold):
        """A REAL restart: HOME is moved into the tmp dir and the module is
        re-imported, so the new module object recomputes its own path and finds
        the strikes left by the old one. (An in-memory counter would be empty
        here — which is exactly how the incident's debounce reset itself.)"""
        home = tmp_hfcc_dir / "fake-home"
        (home / ".hscc").mkdir(parents=True, exist_ok=True)
        live_path = str(home / ".hscc" / "heal-breaker.json")
        monkeypatch.setenv("HOME", str(home))
        # Set the cap via ENV, not monkeypatch: reload() re-executes the module
        # and recomputes HEAL_BREAKER_MAX from the environment, so a patched
        # attribute would silently revert to the production cap on restart.
        monkeypatch.setenv("HSCC_HEAL_BREAKER_MAX", str(small_threshold))
        monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_FILE", live_path)
        for _ in range(small_threshold):
            heal_breaker.note_heal("10.0.0.2")
        assert os.path.exists(live_path)

        import importlib
        import hscc_daemon.heal_breaker as hb
        importlib.reload(hb)          # fresh module, fresh constants
        try:
            assert hb.HEAL_BREAKER_FILE == live_path   # same file, new process
            assert hb.strike_count("10.0.0.2") == small_threshold
            # Not yet tripped; the NEXT action is the one that trips — proving
            # the restarted process inherited the count instead of starting over.
            assert hb.suspended("10.0.0.2") == 0.0
            assert hb.note_heal("10.0.0.2")["tripped"] is True
        finally:
            importlib.reload(hb)      # restore real constants for other tests

    def test_ledger_is_readable_json_a_human_can_inspect(self, ledger,
                                                         small_threshold):
        heal_breaker.note_heal("10.0.0.2")
        data = json.load(open(ledger))
        assert "10.0.0.2" in data
        assert isinstance(data["10.0.0.2"], list)


class TestAlertOnce:
    def test_first_trip_alerts(self, ledger):
        assert heal_breaker.should_alert("10.0.0.2") is True

    def test_repeated_trips_inside_the_cooldown_stay_silent(self, ledger):
        """The suppressed ticks must not re-notify. The 2026-10-08 NAS rule
        fired 178 macOS notifications in 11 minutes by conflating the strike
        ledger with the notify cooldown."""
        assert heal_breaker.should_alert("10.0.0.2") is True
        for _ in range(20):
            assert heal_breaker.should_alert("10.0.0.2") is False

    def test_alert_is_per_node(self, ledger):
        assert heal_breaker.should_alert("10.0.0.2") is True
        assert heal_breaker.should_alert("10.0.0.3") is True

    def test_alert_cooldown_expires(self, ledger):
        base = 1_000_000.0
        assert heal_breaker.should_alert("10.0.0.2", now=base) is True
        assert heal_breaker.should_alert("10.0.0.2", now=base + 1) is False
        later = base + heal_breaker.HEAL_BREAKER_NOTIFY_COOLDOWN_S + 1
        assert heal_breaker.should_alert("10.0.0.2", now=later) is True

    def test_recovering_then_tripping_again_can_alert_again(self, ledger):
        """A node that heals and later fails afresh must be able to raise a
        NEW alert — otherwise one trip silences the breaker forever."""
        assert heal_breaker.should_alert("10.0.0.2") is True
        assert heal_breaker.reset_alert("10.0.0.2") is True
        assert heal_breaker.should_alert("10.0.0.2") is True


class TestCorruptionSafety:
    """The breaker can only ever be trusted if a bad file cannot wedge the
    cluster. Every failure mode here must FAIL OPEN (allow healing)."""

    def test_missing_file_is_not_tripped(self, ledger):
        assert heal_breaker.suspended("10.0.0.2") == 0.0

    def test_garbage_file_fails_open(self, ledger):
        with open(ledger, "w") as f:
            f.write("this is not json {{{")
        assert heal_breaker.suspended("10.0.0.2") == 0.0
        # And a write still works after the bad read (it re-initialises).
        assert heal_breaker.note_heal("10.0.0.2")["tripped"] is False

    def test_corrupt_timestamp_drops_that_node_rather_than_locking_it(
            self, ledger):
        """A non-numeric strike must NOT be interpretable as 'suspended
        forever'."""
        with open(ledger, "w") as f:
            json.dump({"10.0.0.2": ["nonsense", 123]}, f)
        assert heal_breaker.suspended("10.0.0.2") == 0.0

    def test_top_level_list_fails_open(self, ledger):
        with open(ledger, "w") as f:
            json.dump(["not", "a", "mapping"], f)
        assert heal_breaker.suspended("10.0.0.2") == 0.0


class TestBreakerStopsARealStorm:
    """Integration: the gate must work through ``check_workers()``, not just in
    the module. This is the property the incident actually needed."""

    def _setup(self, tmp_hfcc_dir, monkeypatch, nodes):
        from hscc_daemon import health, serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        units = [{"id": "orch", "role": "orchestrator", "nodes": ["10.0.0.1"],
                  "recipe": "~/r/orch.yaml", "model": "M"}]
        for n in nodes:
            units.append({"id": f"w-{n}", "role": "worker", "keepalive": True,
                          "nodes": [n], "recipe": "~/r/27b.yaml", "model": "W"})
        monkeypatch.setattr(serving, "ORCH_NODES", {"10.0.0.1"})
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
        return health

    def test_heal_storm_is_stopped_and_the_node_is_left_alone(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        """Force-recreate the same node repeatedly and the breaker must cut it
        off: after the cap, ``_autoheal_worker_fn`` is never called again."""
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 1)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_COOLDOWN_MINUTES", 0)
        heals = []
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a) or {"status": "ok"})
        # More ticks than the cap, each one eligible to auto-heal.
        for _ in range(small_threshold + 4):
            health.check_workers()
        # Capped: we got at most (max) heals, then the breaker held the door.
        assert len(heals) == small_threshold
        assert heal_breaker.suspended("10.0.0.2") > 0
        # And the node is reported DOWN (not silently marked online) — the
        # breaker pauses repair, it does not pretend the unit recovered.
        from hscc_daemon.state import read_state
        st = read_state("workers")
        assert st is not None and "w-10.0.0.2" in json.dumps(st)

    def test_the_lease_is_released_even_when_the_breaker_says_no(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        """A breaker that leaked the lease would disable ALL healing on every
        node until the TTL expired — strictly worse than no breaker."""
        from hscc_daemon import heal_lease
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 1)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_COOLDOWN_MINUTES", 0)
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: {"status": "ok"})
        for _ in range(small_threshold + 3):
            health.check_workers()
        # Tripped...
        assert heal_breaker.suspended("10.0.0.2") > 0
        # ...yet nothing is held: another mechanism can still lease immediately.
        ok, holder = heal_lease.acquire("worker-relaunch", "anything")
        assert ok is True, f"lease leaked by the breaker path: {holder}"
        heal_lease.release("worker-relaunch")

    def test_trips_notify_exactly_once_through_the_real_path(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        """The storm must alert the operator ONCE, not once per tick."""
        health = self._setup(tmp_hfcc_dir, monkeypatch, nodes=["10.0.0.2"])
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 1)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_COOLDOWN_MINUTES", 0)
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: {"status": "ok"})
        notes = []
        monkeypatch.setattr(health, "send_macos_notification",
                            lambda title, body, **k: notes.append((title, body))
                            or True)
        events = []
        monkeypatch.setattr(health, "emit_event",
                            lambda t, p, **k: events.append((t, p)))
        for _ in range(small_threshold + 5):
            health.check_workers()
        assert len(notes) == 1, f"alert storm: {len(notes)} notifications"
        assert notes[0][0] == "HSCC: heal breaker tripped"
        assert len(events) == 1
        assert events[0][0] == "heal_breaker.tripped"
        assert events[0][1]["node"] == "10.0.0.2"

    def test_a_tripped_node_does_not_strand_a_healthy_one(
            self, tmp_hfcc_dir, monkeypatch, small_threshold):
        """Per-node isolation THROUGH the real check path: node A already has a
        full slate of strikes (it trips on the first tick), node B starts clean.
        After A is suspended, B must KEEP being repaired — a breaker that
        stopped the whole fleet would be worse than no breaker."""
        health = self._setup(tmp_hfcc_dir, monkeypatch,
                             nodes=["10.0.0.2", "10.0.0.3"])
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE", 1)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_COOLDOWN_MINUTES", 0)
        # A enters the loop one action short of tripping.
        for _ in range(small_threshold):
            heal_breaker.note_heal("10.0.0.2")
        heals = []
        # NB: _autoheal_worker_fn(key, label, node, port) — the NODE is a[2].
        # (Asserting on a[0] compares against the (node, port) key tuple and
        # passes vacuously, which is worse than failing.)
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: heals.append(a[2]) or {"status": "ok"})
        for _ in range(3):
            health.check_workers()
        # A tripped on the first tick (its 4th action) and was refused on the
        # rest, so its destructive recreate NEVER RAN — even though the strike
        # was counted. The count records the ATTEMPT; the action is withheld.
        assert [n for n in heals if n == "10.0.0.2"] == []
        # ...while B, still under its own cap, was healed every tick.
        assert len([n for n in heals if n == "10.0.0.3"]) == 3
