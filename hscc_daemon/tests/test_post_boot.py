"""Tests for hscc_daemon/post_boot.py — post-reboot relaunch (t_576964d5).

Proof shape (the card's acceptance list):
  * post-boot condition (fresh boot id + expected_up + unit not serving)
    -> relaunch fires EXACTLY ONCE, through the cluster-up surface;
  * intentional down -> NEVER fires;
  * the pre-existing intentional / autodown-lock / heal-lease gates still win;
  * a unit that was down before the reboot stays down (baseline unchanged);
  * the marker is set by cluster up / wake and cleared by cluster down /
    teardown (the two verbs + autodown hooks, pinned separately in their own
    suites — here the marker layer itself is pinned);
  * missing evidence (boot sweep failed, node unseen) -> no action.

All side-effects are injected (boot fetcher, probe, up_fn, lock fns, notifier,
marker path) so the pass never shells out. The autouse _isolate_hscc fixture
redirects every ~/.hscc path anyway; EXPECTED_UP_FILE is additionally patched
per test for explicitness.

Address doctrine: sanctioned 10.0.0.x placeholder range only.
"""

import json
import os
import time

import pytest

from hscc_daemon import post_boot as pb

HOST_A = "10.0.0.244"
HOST_B = "10.0.0.247"
BOOT_OLD_A = "aaaaaaaa-0000-0000-0000-000000000000"
BOOT_NEW_A = "bbbbbbbb-1111-1111-1111-111111111111"
BOOT_OLD_B = "cccccccc-2222-2222-2222-222222222222"
BOOT_NEW_B = "dddddddd-3333-3333-3333-333333333333"


@pytest.fixture
def marker_path(tmp_path, monkeypatch):
    p = str(tmp_path / "expected-up.json")
    monkeypatch.setattr(pb, "EXPECTED_UP_FILE", p)
    return p


@pytest.fixture(autouse=True)
def _env_clean(monkeypatch):
    """Hermetic clock/monotonic + a guaranteed-ON off-switch default."""
    monkeypatch.delenv("HSCC_POST_BOOT_RELAUNCH", raising=False)
    monkeypatch.setattr(pb, "POST_BOOT_ENABLED", True)


def _marker(with_units=True, baseline=None, attempts=None):
    m = {"units": [], "boot_baseline": baseline or {},
         "boot_attempts": attempts or {}}
    if with_units:
        m["units"] = [
            {"unit_id": "orch", "role": "orchestrator", "nodes": [HOST_A],
             "port": 8000},
            {"unit_id": "wk1", "role": "worker", "nodes": [HOST_B],
             "port": 8001},
        ]
    return m


def _write_marker(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


class Fakes:
    """Injectable seams + call recorder (no subprocess, no HTTP)."""

    def __init__(self, boots=None, serving=(), up_ok=True, lock_ok=True,
                 lease_ok=True, intentional=False):
        self.boots = boots
        self.serving = set(serving)          # (node, port) answering healthy
        self.up_ok = up_ok
        self.lock_ok = lock_ok
        self.lease_ok = lease_ok
        self.intentional = intentional
        self.up_calls = []                    # nodes lists passed to up_fn
        self.boot_calls = 0
        self.lock_acquires = 0
        self.lock_releases = 0
        self.notices = []

    def boot_fn(self, nodes):
        self.boot_calls += 1
        return dict(self.boots) if self.boots is not None else None

    def probe_fn(self, url, timeout=5):
        for (node, port) in self.serving:
            if url == f"http://{node}:{port}/health":
                return {"ok": True}
        return {"ok": False, "output": "down"}

    def up_fn(self, nodes):
        self.up_calls.append(list(nodes))
        return {"success": self.up_ok, "output": "ok" if self.up_ok else "boom"}

    def lock_acquire(self, now=None):
        self.lock_acquires += 1
        return self.lock_ok

    def lock_release(self):
        self.lock_releases += 1

    def intentional_fn(self):
        return self.intentional

    def notify(self, title, msg, priority="normal"):
        self.notices.append((title, msg, priority))


def _pass(fx, path, lease_ok=None, monkeypatch=None):
    """Run the guarded pass with fakes; lease patched to fx.lease_ok."""
    from hscc_daemon import heal_lease
    ok = fx.lease_ok if lease_ok is None else lease_ok
    if monkeypatch is not None:
        monkeypatch.setattr(heal_lease, "acquire",
                            lambda *a, **k: (ok, {"who": "other",
                                                  "subject": "x"}))
        monkeypatch.setattr(heal_lease, "release", lambda *a, **k: None)
    return pb.run_post_boot_pass(
        boot_fn=fx.boot_fn, probe_fn=fx.probe_fn, up_fn=fx.up_fn,
        lock_acquire=fx.lock_acquire, lock_release=fx.lock_release,
        intentional_fn=fx.intentional_fn, notify_fn=fx.notify, path=path)


# ── The post-boot relaunch itself ────────────────────────────────────────
class TestRelaunch:
    def test_fresh_boot_expected_up_down_unit_relaunches_exactly_once(
            self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B},
                   serving=set())  # orch down after the .244 reboot
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "launched"
        assert res["actions"] == ["orch"]
        assert fx.up_calls == [[HOST_A]]          # one cluster-up call...
        # ...scoped to the REBOOTED node — the healthy node's unit untouched.
        res2 = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res2["result"] == "steady"          # EXACTLY-ONCE
        assert fx.up_calls == [[HOST_A]]
        # The new boot was accepted as baseline before the launch.
        m = json.load(open(marker_path))
        assert m["boot_baseline"][HOST_A] == BOOT_NEW_A
        assert m["boot_attempts"][HOST_A]["count"] == 1

    def test_serving_unit_on_rebooted_node_is_not_relaunched(
            self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B},
                   serving={(HOST_A, 8000)})   # came back by itself
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "steady"
        assert fx.up_calls == []
        # baseline advanced anyway (no re-deciding next tick)
        assert json.load(open(marker_path))["boot_baseline"][HOST_A] == BOOT_NEW_A

    def test_unit_down_before_reboot_stays_down(self, marker_path, monkeypatch):
        """wk1 was already down pre-reboot (its node's baseline is unchanged)
        — it must NOT be dragged up by a sibling node's reboot."""
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "launched"
        assert res["actions"] == ["orch"]          # wk1 NOT relaunched
        assert HOST_B not in (fx.up_calls[0] if fx.up_calls else [])

    def test_first_sight_node_records_baseline_without_acting(
            self, marker_path, monkeypatch):
        """Empty marker on the daemon's first pass after a fresh cluster up:
        never self-triggers, just records what it sees."""
        _write_marker(marker_path, _marker(baseline={}))
        fx = Fakes(boots={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "steady"
        assert fx.up_calls == []
        m = json.load(open(marker_path))
        assert m["boot_baseline"][HOST_A] == BOOT_OLD_A
        assert m["boot_baseline"][HOST_B] == BOOT_OLD_B

    def test_no_marker_never_launches(self, marker_path, monkeypatch):
        fx = Fakes(boots={HOST_A: BOOT_NEW_A}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "no-marker"
        assert fx.up_calls == [] and fx.lock_acquires == 0

    def test_clear_expected_up_makes_a_reboot_inert(self, marker_path,
                                                    monkeypatch):
        """cluster down / teardown cleared the marker -> even a fresh boot
        with the unit down launches nothing (the card's sacred gate)."""
        _write_marker(marker_path, _marker(baseline={HOST_A: BOOT_OLD_A}))
        pb.clear_expected_up("hscc cluster down", path=marker_path)
        fx = Fakes(boots={HOST_A: BOOT_NEW_A}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "no-marker"
        assert fx.up_calls == []

    def test_boot_sweep_unavailable_is_fail_closed(self, marker_path,
                                                   monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots=None, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "boot-unavailable"
        assert fx.up_calls == [] and fx.lock_acquires == 0

    def test_unreachable_node_boot_unknown_is_never_a_trigger(
            self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: None, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert fx.up_calls == []
        assert res["result"] == "steady"

    def test_missing_node_in_sweep_is_no_evidence(self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_B: BOOT_OLD_B}, serving=set())  # A absent
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "steady"
        assert fx.up_calls == []


# ── The sacred gates ──────────────────────────────────────────────────────
class TestGates:
    def test_intentional_autodown_never_resurrected(self, marker_path,
                                                    monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B},
                   serving=set(), intentional=True)
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "suppressed"
        assert fx.up_calls == [] and fx.lock_acquires == 0

    def test_autodown_lock_held_skips_without_touching(self, marker_path,
                                                       monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B},
                   serving=set(), lock_ok=False)
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "skipped"
        assert fx.up_calls == []
        # Lost the lock race → we NEVER release (not ours to release), and
        # the lease is never touched.
        assert fx.lock_acquires == 1 and fx.lock_releases == 0

    def test_heal_lease_held_defers(self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, lease_ok=False, monkeypatch=monkeypatch)
        assert res["result"] == "skipped"
        assert fx.up_calls == []
        assert fx.lock_releases == 1               # the lock was still freed

    def test_lock_released_even_when_up_fn_raises(self, marker_path,
                                                  monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())

        def raising_up(nodes):
            fx.up_calls.append(list(nodes))
            raise RuntimeError("boom")
        from hscc_daemon import heal_lease
        monkeypatch.setattr(heal_lease, "acquire", lambda *a, **k: (True, None))
        monkeypatch.setattr(heal_lease, "release", lambda *a, **k: None)
        res = pb.run_post_boot_pass(
            boot_fn=fx.boot_fn, probe_fn=fx.probe_fn, up_fn=raising_up,
            lock_acquire=fx.lock_acquire, lock_release=fx.lock_release,
            intentional_fn=fx.intentional_fn, notify_fn=fx.notify,
            path=marker_path)
        assert res["result"] == "launch-failed"
        assert fx.lock_releases == 1

    def test_marker_cleared_under_lock_wins(self, marker_path, monkeypatch):
        """A cluster down landing between the pre-lock gate and the launch:
        the under-lock re-read of the (now cleared) marker must win."""
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        real_acquire = fx.lock_acquire

        def acquire_then_clear(now=None):
            ok = real_acquire(now=now)
            pb.clear_expected_up("hscc cluster down", path=marker_path)
            return ok
        fx.lock_acquire = acquire_then_clear
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "no-marker"
        assert fx.up_calls == []

    def test_operator_off_switch_disables_everything(self, marker_path,
                                                     monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        monkeypatch.setattr(pb, "POST_BOOT_ENABLED", False)
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "disabled"
        assert fx.up_calls == []
        # nothing at all ran: no boot sweep, no lock (zero I/O).
        assert fx.boot_calls == 0 and fx.lock_acquires == 0

    def test_cooldown_blocks_a_second_launch_same_window(self, marker_path,
                                                         monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        # force the retry-decidable state: attempt happened, baseline restored
        # (what a launch-failed leaves behind) — cooldown must gate the retry.
        m = _marker(baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B})
        m["last_launch_attempt_iso"] = pb.now_iso()
        _write_marker(marker_path, m)
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "cooldown"
        assert fx.up_calls == []

    def test_attempt_cap_gives_up_for_the_boot(self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B},
            attempts={HOST_A: {"boot_id": BOOT_NEW_A,
                               "count": pb.POST_BOOT_MAX_ATTEMPTS}}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "gave-up"
        assert fx.up_calls == []
        # the exhausted boot was ACCEPTED (baseline advanced) -> next tick
        # needs no action at all until the node boots again
        assert json.load(open(marker_path))["boot_baseline"][HOST_A] == BOOT_NEW_A

    def test_launch_failure_keeps_retry_decidable_within_cap(
            self, marker_path, monkeypatch):
        _write_marker(marker_path, _marker(
            baseline={HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B}))
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set(),
                   up_ok=False)
        monkeypatch.setattr(pb, "POST_BOOT_COOLDOWN_S", 0.0)
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "launch-failed"
        # baseline restored to the PRE-boot id so the next pass can re-decide
        assert json.load(open(marker_path))["boot_baseline"][HOST_A] == BOOT_OLD_A
        assert fx.notices  # the operator was told


# ── TP span + partial-reboot scoping ─────────────────────────────────────
class TestScoping:
    def test_tp_span_partial_reboot_scopes_launch_to_rebooted_node(
            self, marker_path, monkeypatch):
        _write_marker(marker_path, {
            "units": [{"unit_id": "tp2", "role": "worker",
                       "nodes": [HOST_A, HOST_B], "port": 8002}],
            "boot_baseline": {HOST_A: BOOT_OLD_A, HOST_B: BOOT_OLD_B},
            "boot_attempts": {},
        })
        fx = Fakes(boots={HOST_A: BOOT_NEW_A, HOST_B: BOOT_OLD_B}, serving=set())
        res = _pass(fx, marker_path, monkeypatch=monkeypatch)
        assert res["result"] == "launched"
        assert res["actions"] == ["tp2"]
        assert fx.up_calls == [[HOST_A]]   # scoped to the rebooted host


# ── Marker layer ──────────────────────────────────────────────────────────
class TestMarker:
    def test_record_then_clear_roundtrip(self, marker_path):
        plan = [{"kind": "orchestrator", "nodes": [HOST_A], "port": 8000,
                 "unit_id": "orch", "keepalive": False, "cmd": ["x"]}]
        assert pb.record_expected_up(plan, "hscc cluster up", path=marker_path)
        m = json.load(open(marker_path))
        assert m["units"][0]["unit_id"] == "orch"
        assert m["reason"] == "hscc cluster up"
        assert pb.clear_expected_up("hscc cluster down", path=marker_path)
        m = json.load(open(marker_path))
        assert m["units"] == [] and m["reason"] == "hscc cluster down"

    def test_record_preserves_existing_baselines(self, marker_path):
        _write_marker(marker_path, _marker(baseline={HOST_A: BOOT_NEW_A}))
        pb.record_expected_up([{"kind": "worker", "nodes": [HOST_B],
                                "port": 8001, "unit_id": "wk1"}],
                              "autodown wake", path=marker_path)
        m = json.load(open(marker_path))
        assert m["boot_baseline"][HOST_A] == BOOT_NEW_A   # adjudicated boot kept

    def test_corrupt_marker_reads_empty(self, marker_path):
        with open(marker_path, "w") as f:
            f.write("{not json")
        assert pb.load_marker(marker_path)["units"] == []

    def test_record_ignores_entries_without_nodes(self, marker_path):
        pb.record_expected_up([{"unit_id": "x"}, {"unit_id": "y",
                                                  "nodes": []},
                               {"unit_id": "ok", "nodes": [HOST_A],
                                "port": 8000}], "t", path=marker_path)
        m = json.load(open(marker_path))
        assert [u["unit_id"] for u in m["units"]] == ["ok"]


# ── Boot sweep (the sanctioned sparkrun transport) ───────────────────────
class TestBootSweep:
    def test_script_reads_boot_id_through_sparkrun_apis(self):
        """The executed script must use sparkrun's own resolution + ssh
        primitive (sparkrun owns the fleet; never a hand-rolled transport)."""
        s = pb._BOOT_ID_SCRIPT
        for needle in ("resolve_hosts", "build_ssh_kwargs",
                       "run_remote_command", "/proc/sys/kernel/random/boot_id"):
            assert needle in s, needle
        assert "subprocess" not in s and "ssh -o" not in s

    def test_fetch_maps_and_flags_none(self, monkeypatch):
        calls = []

        def run_fn(argv, timeout=None):
            calls.append(argv)
            return {"ok": True,
                    "output": json.dumps({HOST_A: BOOT_NEW_A, HOST_B: None})}
        monkeypatch.setattr(pb, "_BOOT_ID_SCRIPT", pb._BOOT_ID_SCRIPT)
        got = pb.fetch_boot_ids([HOST_A, HOST_B], run_fn=run_fn,
                                venv_python_fn=lambda: "/venv/bin/python")
        assert got == {HOST_A: BOOT_NEW_A, HOST_B: None}
        assert calls[0][0] == "/venv/bin/python"
        assert json.loads(calls[0][3]) == [HOST_A, HOST_B]

    def test_fetch_unresolvable_venv_returns_none(self, monkeypatch):
        assert pb.fetch_boot_ids([HOST_A], run_fn=lambda *a, **k: {"ok": True},
                                 venv_python_fn=lambda: None) is None

    def test_fetch_failed_run_returns_none(self):
        assert pb.fetch_boot_ids(
            [HOST_A], run_fn=lambda *a, **k: {"ok": False, "output": ""},
            venv_python_fn=lambda: "/venv/bin/python") is None


# ── Stream wrapper ────────────────────────────────────────────────────────
class TestStream:
    def test_pass_failure_writes_red_stream(self, tmp_path, monkeypatch):
        from hscc_daemon import state as state_mod
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(pb, "write_state", state_mod.write_state)

        def failing_pass():
            raise RuntimeError("probe exploded")
        ok = pb.check_post_boot_relaunch(pass_fn=failing_pass)
        assert ok is False
        st = state_mod.read_state(pb.POST_BOOT_STREAM)
        assert st is not None
        assert st["ok"] is False and "probe exploded" in st["message"]

    def test_no_action_outcomes_are_ok_streams(self, monkeypatch):
        written = {}

        def fake_write(stream, data):
            written[stream] = data
        monkeypatch.setattr(pb, "write_state", fake_write)
        for outcome, want_ok in (("suppressed", True), ("skipped", True),
                                 ("cooldown", True), ("boot-unavailable", True),
                                 ("launch-failed", False), ("gave-up", False),
                                 ("launched", True)):
            ok = pb.check_post_boot_relaunch(
                pass_fn=lambda o=outcome: {"result": o, "message": o,
                                           "actions": [], "observed": {}})
            assert ok is want_ok, outcome
        assert written[pb.POST_BOOT_STREAM]["result"] == "launched"
