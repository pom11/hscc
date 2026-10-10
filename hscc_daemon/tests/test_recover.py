"""Tests for the guarded auto-recovery of a wedged serving unit (recover.py).

Coverage maps 1:1 to the task's proof requirements:
  * a unit wedged for N consecutive probes triggers exactly ONE recovery
  * N-1 consecutive wedges trigger NOTHING
  * a healthy sibling unit is never stopped when one unit is wedged
  * the cooldown suppresses a second recovery inside the window
  * the attempt cap stops acting and keeps alerting
  * no recovery fires during an intentional autodown block, or during load
  * recovery is skipped when the exclusivity lock is held
  * NO real subprocess or HTTP call in the suite; no writes to live operator
    state

Every test injects the recovery's side-effects (stream verdict, container-id
resolver, hscc stop/up wrappers, autodown lock acquire/release, intentional
check, clock, state file) as fakes, so nothing ever shells out to sparkrun or
touches the operator's live ~/.hscc. The autouse `_isolate_hscc` fixture
(conftest.py) additionally redirects every ~/.hscc path to a per-test tmp dir,
so even a leaked call cannot reach live state.
"""

import json

import pytest

from hscc_daemon import recover


@pytest.fixture(autouse=True)
def _reset_recover_state():
    """Each test starts with a clean in-memory consecutive-wedge streak.

    ``recover._recover_units`` is deliberately module-level so the
    N-consecutive-detection streak accumulates across daemon ticks. Without
    resetting per test, one test's streak leaks into the next.
    """
    recover._recover_units.clear()
    yield
    recover._recover_units.clear()


UNIT_A = {"unit": "worker-a", "node": "10.0.0.10", "port": 8000,
          "model": "qwen-a", "recipe": "qwen-a.yaml"}
UNIT_B = {"unit": "worker-b", "node": "10.0.0.11", "port": 8000,
          "model": "qwen-b", "recipe": "qwen-b.yaml"}


def _stream(wedged=None, ok_units=None, loading=None):
    """A synthetic engine_wedge stream dict (the probe's verdict)."""
    def norm(lst):
        return [dict(u) for u in (lst or [])]
    return {"wedged": norm(wedged), "ok_units": norm(ok_units),
            "loading": norm(loading), "down": [], "ok": not bool(wedged)}


class Recorders:
    """Tracks calls to the injected cluster wrappers / lock for assertions."""

    def __init__(self):
        self.stops = []          # container ids passed to `hscc cluster stop`
        self.up_calls = 0
        self.lock_acquires = 0
        self.lock_releases = 0
        self.stop_ok = True
        self.up_ok = True

    # hscc cluster stop <cid>
    def stop_fn(self, cid):
        self.stops.append(cid)
        return {"success": self.stop_ok, "output": f"stopped {cid}"}

    # hscc cluster up
    def up_fn(self):
        self.up_calls += 1
        return {"success": self.up_ok, "output": "relaunched"}

    # autodown O_EXCL lock (mirror of _acquire_lock/_release_lock)
    def lock_acquire(self, **kw):
        self.lock_acquires += 1
        return True

    def lock_release(self):
        self.lock_releases += 1


def _resolve_containers(_status, unit):
    """Deterministic unit → container_id map (mirrors the wrapper resolver)."""
    return {"worker-a": "cid-a", "worker-b": "cid-b"}.get(unit.get("unit"))


def _intentional_false():
    return False


def _run(stream, rec=None, now=0.0, state_file=None, **kw):
    """Convenience: run one recovery pass with all fakes injected.

    ``state_file`` defaults to None so the isolated ``RECOVER_STATE_FILE``
    (redirected by conftest's ``_isolate_hscc`` to a per-test tmp dir) is used
    — each test gets its own cooldown/attempt state, never the shared real one.
    ``**kw`` overrides any single hook (e.g. ``resolve_container``,
    ``status_fn``) when a test needs a non-default behaviour.
    """
    rec = rec or Recorders()
    resolver = kw.pop("resolve_container", _resolve_containers)
    return recover.recover_engine_wedge(
        stream_state=stream,
        status_fn=kw.pop("status_fn",
                         lambda: {"workloads": [], "raw_output": ""}),
        resolve_container=resolver,
        stop_fn=rec.stop_fn,
        up_fn=rec.up_fn,
        lock_acquire=rec.lock_acquire,
        lock_release=rec.lock_release,
        intentional_fn=_intentional_false,
        now=now,
        state_file=state_file,
        **kw,
    )


class TestConsecutiveWedgeThreshold:
    def test_N_consecutive_wedges_trigger_exactly_one_recovery(self):
        """A unit wedged for N=3 consecutive probes -> EXACTLY ONE stop."""
        rec = Recorders()
        s = _stream(wedged=[UNIT_A])

        # Wedges 1 and 2: below N -> nothing fires, no stop, streak accumulates.
        r1 = _run(s, rec, now=10)
        assert r1["result"] == "none-wedged"
        assert rec.stops == []
        r2 = _run(s, rec, now=20)
        assert r2["result"] == "none-wedged"
        assert rec.stops == []

        # Wedge 3: crosses N -> recovery fires once.
        r3 = _run(s, rec, now=30)
        assert r3["result"] == "recovered"
        assert rec.stops == ["cid-a"]
        assert rec.up_calls == 1
        stop_action = [a for a in r3["actions"] if a.get("action") == "stop"][0]
        assert stop_action["unit"] == "worker-a"
        assert stop_action["attempt"] == 1

        # Wedge 4: still wedged, but inside cooldown -> suppressed, NO second
        # stop. Exactly one recovery total.
        r4 = _run(s, rec, now=40)
        assert rec.stops == ["cid-a"], "cooldown must suppress a 2nd stop"
        assert any(a.get("reason") == "cooldown" for a in r4["actions"])

    def test_N_minus_1_consecutive_wedges_trigger_nothing(self):
        """N-1=2 consecutive wedges never fire a recovery (N=3)."""
        rec = Recorders()
        s = _stream(wedged=[UNIT_A])
        for i in range(1, 3):
            r = _run(s, rec, now=i * 10)
            assert r["result"] == "none-wedged", f"tick {i} must not fire"
        assert rec.stops == []
        assert rec.up_calls == 0
        assert recover._recover_units["worker-a"]["wedge_streak"] == 2

    def test_streak_resets_on_healthy(self):
        """A unit that recovers mid-streak resets the N-consecutive counter."""
        rec = Recorders()
        # One wedge, then a healthy check, then one wedge again.
        _run(_stream(wedged=[UNIT_A]), rec, now=10)
        _run(_stream(wedged=[UNIT_A]), rec, now=20)
        _run(_stream(ok_units=[UNIT_A]), rec, now=30)
        assert recover._recover_units["worker-a"]["wedge_streak"] == 0
        # One more wedge -> streak back to 1, still below N.
        _run(_stream(wedged=[UNIT_A]), rec, now=40)
        assert recover._recover_units["worker-a"]["wedge_streak"] == 1
        assert rec.stops == []


class TestHealthySiblingNeverStopped:
    def test_healthy_sibling_not_stopped_short(self):
        """Only the wedged unit's container id lands in the stop set (N=1)."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A], ok_units=[UNIT_B])
        r = _run(s, rec, now=0.0)
        assert r["result"] == "recovered"
        assert rec.stops == ["cid-a"], (
            "the STOP must target ONLY the wedged unit; healthy sibling cid-b "
            "must never be stopped")
        assert "cid-b" not in rec.stops


class TestCooldown:
    def test_cooldown_suppresses_second_recovery_inside_window(self):
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A])

        # First recovery at t=100.
        r1 = _run(s, rec, now=100.0)
        assert r1["result"] == "recovered"
        assert rec.stops == ["cid-a"]

        # Another wedged check at t=500 (inside the 1800s cooldown) -> no action.
        r2 = _run(s, rec, now=500.0)
        assert rec.stops == ["cid-a"], "cooldown must suppress a 2nd stop"
        assert r2["result"] == "suppressed"
        assert any(a.get("reason") == "cooldown" for a in r2["actions"])

        # At t=1900+ (>1800 since t=100) the cooldown has expired -> may fire.
        r3 = _run(s, rec, now=2000.0)
        assert r3["result"] == "recovered"
        assert rec.stops == ["cid-a", "cid-a"]


class TestAttemptCap:
    def test_attempt_cap_stops_acting_and_keeps_alerting(self):
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A])

        # Attempts 1..3: recover each time (advancing past the cooldown).
        for t in (0.0, 2000.0, 4000.0):
            r = _run(s, rec, now=t)
            assert r["result"] == "recovered"
        assert rec.stops == ["cid-a", "cid-a", "cid-a"]

        # 4th wedged pass: attempt cap (3) reached -> GIVE UP, no stop.
        r4 = _run(s, rec, now=6000.0)
        assert r4["result"] == "gave-up"
        assert rec.stops == ["cid-a", "cid-a", "cid-a"], \
            "attempt cap must stop acting"
        gave_up_actions = [a for a in r4["actions"]
                           if a.get("reason") == "max_attempts"]
        assert len(gave_up_actions) == 1
        assert gave_up_actions[0]["attempt"] == 3
        # The probe still reports the wedge (keeps alerting); recovery no longer
        # acts. Persisted gave_up is recorded.
        state = json.load(open(recover.RECOVER_STATE_FILE))
        assert state["units"]["worker-a"]["gave_up"] is True

    def test_recovered_unit_resets_attempt_budget(self):
        """After a gave-up unit heals, a LATER distinct wedge gets fresh attempts."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s_w = _stream(wedged=[UNIT_A])
        s_ok = _stream(ok_units=[UNIT_A])

        # Burn through the attempt cap -> gave up.
        for t in (0.0, 2000.0, 4000.0):
            assert _run(s_w, rec, now=t)["result"] == "recovered"
        assert _run(s_w, rec, now=6000.0)["result"] == "gave-up"
        assert rec.stops == ["cid-a", "cid-a", "cid-a"]

        # Unit heals -> attempt budget + gave_up reset.
        _run(s_ok, rec, now=8000.0)
        state = json.load(open(recover.RECOVER_STATE_FILE))
        assert state["units"]["worker-a"]["attempts"] == 0
        assert state["units"]["worker-a"]["gave_up"] is False

        # Wedges again later -> fresh recovery starts at attempt 1.
        _run(s_w, rec, now=10000.0)
        state = json.load(open(recover.RECOVER_STATE_FILE))
        assert state["units"]["worker-a"]["attempts"] == 1
        assert rec.stops == ["cid-a", "cid-a", "cid-a", "cid-a"]


class TestIntentionalAutodown:
    def test_no_recovery_during_intentional_autodown(self):
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A])
        r = recover.recover_engine_wedge(
            stream_state=s,
            status_fn=lambda: {"workloads": []},
            resolve_container=_resolve_containers,
            stop_fn=rec.stop_fn, up_fn=rec.up_fn,
            lock_acquire=rec.lock_acquire, lock_release=rec.lock_release,
            intentional_fn=lambda: True,  # intentional autodown in effect
            now=0.0,
        )
        assert r["result"] == "skipped"
        assert r["reason"] == "intentional autodown"
        assert rec.stops == []
        assert rec.lock_acquires == 0, \
            "must not even take the lock during an intentional autodown"


class TestLoadingGuard:
    def test_no_recovery_for_a_loading_unit(self):
        """A unit still loading is NOT a wedge candidate -> never recovered."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        # The probe reports the unit as still loading (not wedged).
        s = _stream(loading=[UNIT_A])
        r = _run(s, rec, now=0.0)
        assert r["result"] == "none-wedged"
        assert rec.stops == []
        assert rec.up_calls == 0

    def test_belt_and_braces_loading_suppresses_wedged_candidate(self):
        """Even if a unit appeared in wedged AND loading, loading wins."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        # Contradictory stream (shouldn't happen from the probe) — belt and
        # braces: a loading marker must suppress the recovery. It does so at
        # the STREAK level: a unit reported loading has its wedge streak reset,
        # so it never becomes a candidate at all -> no recovery.
        s = {"wedged": [UNIT_A], "loading": [UNIT_A], "ok_units": [],
             "down": [], "ok": False}
        r = _run(s, rec, now=0.0)
        assert r["result"] == "none-wedged"
        assert rec.stops == []
        assert rec.up_calls == 0
        assert recover._recover_units["worker-a"]["wedge_streak"] == 0, \
            "a loading unit must not accumulate wedge credit"


class TestExclusivityLock:
    def test_recovery_skipped_when_exclusivity_lock_held(self):
        """When autodown's O_EXCL lock is held, recovery is skipped."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A])

        def locked_acquire(**kw):
            rec.lock_acquires += 1
            return False  # another teardown/wake holds the lock

        r = recover.recover_engine_wedge(
            stream_state=s,
            status_fn=lambda: {"workloads": []},
            resolve_container=_resolve_containers,
            stop_fn=rec.stop_fn, up_fn=rec.up_fn,
            lock_acquire=locked_acquire, lock_release=rec.lock_release,
            intentional_fn=_intentional_false,
            now=0.0,
        )
        assert r["result"] == "skipped"
        assert r["reason"] == "lock held"
        assert rec.stops == []
        assert rec.up_calls == 0


class TestContainerIdResolution:
    """`_resolve_container_id` pinned (t_873590dd): identity (node+port) is
    the production path — the probe's wedged entry carries node+port and NO
    model/recipe, and a uniform-pool fleet shares one recipe across every
    unit, so the legacy name-substring cannot resolve it (measured 2/2 inert,
    docs/audits/t_873590dd_engine-wedge-inert.md). The unique-match fail-safe
    is pinned for BOTH strategies: zero OR multiple matches -> None.
    """

    ID = [  # normalised identity entries (uniform pool: same recipe/model)
        {"container_id": "cid-node1-8000", "hosts": ["10.0.0.11"],
         "port": 8000, "model": "m", "recipe": "r.yaml"},
        {"container_id": "cid-node2-8001", "hosts": ["10.0.0.12"],
         "port": 8001, "model": "m", "recipe": "r.yaml"},
    ]

    def _status(self, entries=None, workloads=None):
        s = {}
        if entries is not None:
            s["identity"] = entries
        if workloads is not None:
            s["workloads"] = workloads
        return s

    # ── (a) unique match ──────────────────────────────────────────────────
    def test_unique_identity_match_resolves(self):
        unit = {"unit": "worker-a", "node": "10.0.0.12", "port": 8001}
        assert recover._resolve_container_id(
            self._status(entries=self.ID), unit) == "cid-node2-8001"

    def test_unique_identity_match_string_port(self):
        """serve_cmd ports are strings; the probe's may be int — must agree."""
        unit = {"unit": "worker-a", "node": "10.0.0.11", "port": "8000"}
        assert recover._resolve_container_id(
            self._status(entries=self.ID), unit) == "cid-node1-8000"

    # ── (b) zero match ────────────────────────────────────────────────────
    def test_zero_identity_match_is_none(self):
        unit = {"unit": "ghost", "node": "10.0.0.99", "port": 9999}
        assert recover._resolve_container_id(
            self._status(entries=self.ID), unit) is None

    def test_node_match_wrong_port_is_none(self):
        unit = {"unit": "worker-a", "node": "10.0.0.11", "port": 8001}
        assert recover._resolve_container_id(
            self._status(entries=self.ID), unit) is None

    # ── (c) multi match -> ambiguous -> fail-safe ─────────────────────────
    def test_multi_identity_match_is_ambiguous_none(self):
        dup = [
            {"container_id": "cid-x", "hosts": ["10.0.0.13"], "port": 8000},
            {"container_id": "cid-y", "hosts": ["10.0.0.13"], "port": 8000},
        ]
        unit = {"unit": "worker-a", "node": "10.0.0.13", "port": 8000}
        assert recover._resolve_container_id(
            self._status(entries=dup), unit) is None

    def test_identity_entry_with_missing_cid_is_none(self):
        e = [{"container_id": "?", "hosts": ["10.0.0.11"], "port": 8000}]
        unit = {"unit": "worker-a", "node": "10.0.0.11", "port": 8000}
        assert recover._resolve_container_id(
            self._status(entries=e), unit) is None

    # ── legacy name-substring contract preserved ──────────────────────────
    def test_legacy_unique_name_match_still_works(self):
        status = {"workloads": [
            {"name": "qwen-a-serve", "container_id": "cid-a"},
            {"name": "qwen-b-serve", "container_id": "cid-b"}]}
        assert recover._resolve_container_id(
            status, {"model": "qwen-a"}) == "cid-a"
        assert recover._resolve_container_id(
            status, {"recipe": "/x/qwen-b.yaml"}) == "cid-b"

    def test_legacy_zero_and_multi_name_match_are_none(self):
        status = {"workloads": [
            {"name": "qwen-a", "container_id": "cid-a"},
            {"name": "qwen-a2", "container_id": "cid-b"}]}
        assert recover._resolve_container_id(status, {"model": "qwen-z"}) is None
        assert recover._resolve_container_id(status, {"model": "qwen-a"}) is None

    def test_unit_without_node_or_port_falls_to_legacy(self):
        """Identity cannot decide without node+port — legacy contract rules."""
        status = self._status(
            entries=self.ID,
            workloads=[{"name": "qwen-a-serve", "container_id": "cid-a"}])
        assert recover._resolve_container_id(
            status, {"model": "qwen-a"}) == "cid-a"

    def test_raw_structured_to_dict_shape_normalised(self):
        """The resolver consumes a raw classify_cluster_status to_dict()."""
        data = {
            "groups": {"cid_group": {
                "meta": {"hosts": ["10.0.0.20", "10.0.0.21"],
                         "overrides": {"port": 8000}}}},
            "solo_entries": [{
                "cluster_id": "cid_solo", "host": "10.0.0.22",
                "meta": {"port": 8002, "overrides": {"port": 8002}}}],
        }
        assert recover._resolve_container_id(
            data, {"node": "10.0.0.21", "port": 8000}) == "cid_group"
        assert recover._resolve_container_id(
            data, {"node": "10.0.0.22", "port": 8002}) == "cid_solo"

    # ── (d) the REAL orch case ────────────────────────────────────────────
    def test_real_orch_case_resolves(self):
        """Verbatim live-fleet shape (t_873590dd, 2026-10-10): four units,
        ONE model + ONE recipe, four distinct node+port solo entries; the
        wedged entry is the probe's (unit/node/port, no model, no recipe).
        Pre-fix this resolved to None 2/2 in production; post-fix it must
        resolve to the container on the unit's own node+port."""
        recipe = "/home/op/.sparkrun-local/recipes/local-fixed/qwen3.8.yaml"
        model = "local-inference-lab/Qwen3.8-Flash-Next-NVFP4"
        entries = recover._normalise_identity_entries({
            "groups": {},
            "solo_entries": [
                {"cluster_id": f"sparkrun_{h}_tok", "host": h,
                 "meta": {"hosts": [h], "port": p,
                          "overrides": {"port": p},
                          "model": model, "recipe": recipe}}
                for h, p in (("10.0.0.244", 8000), ("10.0.0.246", 8001),
                             ("10.0.0.247", 8002), ("10.0.0.248", 8003))],
        })
        orch_wedged = {"unit": "orch", "node": "10.0.0.244", "port": 8000,
                       "status": "wedged",
                       "message": "HTTP 200 but no generated tokens",
                       "last_success": 1791600000.0, "stalled_for_s": 240}
        status = {"workloads": [  # the legacy text shape the wrapper also has
            {"name": recipe, "container_id": "03fae933e3df92bf_de6e280b97b0"}
            for _ in range(4)], "raw_output": ""}
        status["identity"] = entries
        assert recover._resolve_container_id(status, orch_wedged) == \
            "sparkrun_10.0.0.244_tok"
        # And the legacy branch ALONE (identity absent) still fails on this
        # shape — proof the identity path, not luck, is what fixed it.
        del status["identity"]
        assert recover._resolve_container_id(status, orch_wedged) is None


class TestIdentityWiring:
    """The identity overlay reaching the resolver through the guarded pass —
    the e2e that pins the LIVE defect (stop was never issued because the
    resolver never saw node+port-matchable entries)."""

    def _wedged_stream(self):
        return {"wedged": [{"unit": "orch", "node": "10.0.0.11", "port": 8000,
                            "status": "wedged"}],
                "ok_units": [], "loading": [], "down": [], "ok": False}

    def test_identity_overlay_drives_a_real_stop(self):
        """Uniform pool, no fakes for resolution: the REAL resolver +
        injected identity entries -> the wedged unit's OWN container stops."""
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        rec = Recorders()
        r = recover.recover_engine_wedge(
            stream_state=self._wedged_stream(),
            status_fn=lambda: {"workloads": [
                {"name": "shared-recipe.yaml", "container_id": "wrong"},
                {"name": "shared-recipe.yaml", "container_id": "also-wrong"}],
                "raw_output": ""},
            stop_fn=rec.stop_fn, up_fn=rec.up_fn,
            identity_fn=lambda: [
                {"container_id": "cid-orch", "hosts": ["10.0.0.11"],
                 "port": 8000},
                {"container_id": "cid-sibling", "hosts": ["10.0.0.12"],
                 "port": 8001}],
            lock_acquire=rec.lock_acquire, lock_release=rec.lock_release,
            intentional_fn=_intentional_false, now=0.0)
        assert r["result"] == "recovered"
        assert rec.stops == ["cid-orch"], (\
            "ONLY the wedged unit's container stops; the ambiguous legacy "
            "name match and the healthy sibling must not")

    def test_identity_unavailable_falls_back_to_legacy_shape(self):
        """identity_fn returning [] keeps the pre-fix contract: the ambiguous
        shared-name fleet resolves to None -> fail-safe, no stop."""
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        rec = Recorders()
        r = recover.recover_engine_wedge(
            stream_state=self._wedged_stream(),
            status_fn=lambda: {"workloads": [
                {"name": "shared.yaml", "container_id": "a"},
                {"name": "shared.yaml", "container_id": "b"}]},
            stop_fn=rec.stop_fn, up_fn=rec.up_fn,
            identity_fn=lambda: [],
            lock_acquire=rec.lock_acquire, lock_release=rec.lock_release,
            intentional_fn=_intentional_false, now=0.0)
        assert r["result"] == "suppressed"
        assert rec.stops == []
        assert any(a.get("reason") == "no_container_id" for a in r["actions"])

    def test_identity_ambiguity_still_fail_safe(self):
        """Two identity entries on the SAME node+port -> ambiguous -> None."""
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        rec = Recorders()
        r = recover.recover_engine_wedge(
            stream_state=self._wedged_stream(),
            status_fn=lambda: {"workloads": []},
            stop_fn=rec.stop_fn, up_fn=rec.up_fn,
            identity_fn=lambda: [
                {"container_id": "x", "hosts": ["10.0.0.11"], "port": 8000},
                {"container_id": "y", "hosts": ["10.0.0.11"], "port": 8000}],
            lock_acquire=rec.lock_acquire, lock_release=rec.lock_release,
            intentional_fn=_intentional_false, now=0.0)
        assert r["result"] == "suppressed"
        assert rec.stops == []

    def test_injected_fakes_never_trigger_identity_fetch(self):
        """A caller that injects status/stop/up (the tests) must not pull the
        production identity fetch — no shelling out from tests."""
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        rec = Recorders()
        r = _run(_stream(wedged=[UNIT_A]), rec, now=0.0)
        assert r["result"] == "recovered"
        assert rec.stops == ["cid-a"]


class TestFetchIdentityEntries:
    """`fetch_identity_entries` — the sanctioned structured query adapter.
    The transport itself (health._SPARKRUN_STATUS_SCRIPT under sparkrun's own
    venv python) is health's, proven in t_b543e530; here only the adapter's
    parsing + fail-soft behaviour is pinned, with run_cmd faked (no real
    subprocess)."""

    _UNSET = object()

    def _patch_transport(self, monkeypatch, payload=None, ok=True,
                         venv=_UNSET):
        from hscc_daemon import health as health_mod
        from hscc_daemon import util as util_mod
        resolved = "/fake/venv/bin/python" if venv is TestFetchIdentityEntries._UNSET else venv
        monkeypatch.setattr(health_mod, "_sparkrun_venv_python",
                            lambda: resolved)

        def fake_run(args, **kw):
            return {"ok": ok, "output": payload if payload is not None else ""}
        monkeypatch.setattr(util_mod, "run_cmd", fake_run)

    def test_parses_solo_entries(self, monkeypatch):
        import json as _json
        self._patch_transport(monkeypatch, _json.dumps({"groups": {},
            "solo_entries": [{"cluster_id": "cid_1", "host": "10.0.0.11",
                              "meta": {"overrides": {"port": 8000}}}]}))
        entries = recover.fetch_identity_entries()
        assert entries == [{"container_id": "cid_1", "hosts": ["10.0.0.11"],
                            "port": 8000, "model": "", "recipe": ""}]

    def test_unresolvable_venv_returns_empty(self, monkeypatch):
        self._patch_transport(monkeypatch, venv=None)
        assert recover.fetch_identity_entries() == []

    def test_failed_or_unparsable_query_returns_empty(self, monkeypatch):
        self._patch_transport(monkeypatch, ok=False, payload="")
        assert recover.fetch_identity_entries() == []
        self._patch_transport(monkeypatch, payload="{not json")
        assert recover.fetch_identity_entries() == []


class TestContainerResolutionSafety:
    def test_no_container_id_resolved_is_fail_safe(self):
        """If a unit's container cannot be resolved uniquely, NO stop is issued."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A])
        # status has NO matching workload for the unit -> resolver returns None.
        r = _run(s, rec, now=0.0, resolve_container=lambda _s, u: None)
        assert r["result"] == "suppressed"
        assert rec.stops == []
        assert any(a.get("reason") == "no_container_id" for a in r["actions"])

    def test_ambiguous_match_is_fail_safe(self):
        """Two workloads matching one unit -> ambiguous -> no stop."""
        rec = Recorders()

        def resolve_ambiguous(_status, unit):
            return None  # simulate ambiguity / bare resolve

        r = recover.recover_engine_wedge(
            stream_state=_stream(wedged=[UNIT_A]),
            status_fn=lambda: {"workloads": []},
            resolve_container=lambda _s, u: None,
            stop_fn=rec.stop_fn, up_fn=rec.up_fn,
            lock_acquire=rec.lock_acquire, lock_release=rec.lock_release,
            intentional_fn=_intentional_false,
            now=0.0,
        )
        # N is 3 by default -> first pass is below threshold, no candidate.
        assert rec.stops == []

    def test_each_fired_unit_gets_its_own_stop(self):
        """Two independent wedged units each get their own container stopped."""
        rec = Recorders()
        recover.RECOVER_CONSECUTIVE_WEDGES = 1
        s = _stream(wedged=[UNIT_A, UNIT_B])
        r = _run(s, rec, now=0.0)
        assert r["result"] == "recovered"
        assert sorted(rec.stops) == ["cid-a", "cid-b"]
        assert rec.up_calls == 1  # one shared fleet `up` after both stops
