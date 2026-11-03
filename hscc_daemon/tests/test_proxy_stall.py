"""Proxy-stall watchdog (t_fc6926fe, incident 2026-11-02).

Fakes ONLY — every external touch is an injected seam (pipeline_watchdog's
check_dgx_fn pattern): socket-count fn, node-metrics fn, probe fn, restart fn,
intentional fn, state/event writers, clocks. No live sparkrun, lsof or HTTP
ever runs here (and the conftest sparkrun fence backs that up globally).

The card's matrix:
  (a) all three conditions true for >=120 s -> exactly one restart call,
      cooldown written, event recorded;
  (b) nodes busy (sum>0)                 -> nothing;
  (c) probe fails                        -> nothing (UNKNOWN or known-no);
  (d) sockets idle                       -> nothing;
  (e) second stall inside 10 min         -> suppressed, logged, no restart;
  (f) intentional autodown               -> tick inert;
  (g) stale stall_since reset when condition clears;
  (h) mutation proofs: each of the three condition guards removed must fail
      exactly its own test, and the cooldown guard removed must fail (e) alone.
"""

import pytest

from hscc_daemon import proxy_stall as ps


# ── harness ─────────────────────────────────────────────────────────────────

class Harness:
    """Drives ticks with pinned clocks and recorded side effects."""

    def __init__(self, sockets=2, fleet_sum=0, probe=True, restart_ok=True,
                 intentional=False, prior_state=None, probe_seq=None,
                 metrics_unknown=None, sockets_unknown=False):
        self.calls = {"restart": 0, "events": [], "states": [], "logs": []}
        self.sockets = sockets
        self.fleet_sum = fleet_sum
        self.probe = probe
        self.probe_seq = list(probe_seq) if probe_seq else None
        self.metrics_unknown = metrics_unknown
        self.sockets_unknown = sockets_unknown
        self.restart_ok = restart_ok
        self.intentional = intentional
        self.prior_state = dict(prior_state or {})
        self.detector = {"since": None}
        self._wall = 1_000_000.0

    def socket_count(self):
        return None if self.sockets_unknown else self.sockets

    def node_metrics(self):
        if self.metrics_unknown is not None:
            return {"unknown": self.metrics_unknown}
        return {"sum": self.fleet_sum}

    def probe_fn(self):
        if self.probe_seq:
            return self.probe_seq.pop(0)
        return self.probe

    def restart_fn(self):
        self.calls["restart"] += 1
        return {"ok": self.restart_ok}

    def intentional_fn(self):
        return self.intentional

    def write_state(self, stream, data):
        assert stream == "proxy_stall"
        self.calls["states"].append(dict(data))
        self.prior_state = dict(data)  # daemon loop reads back what it wrote

    def read_state(self, stream):
        return dict(self.prior_state)

    def emit(self, event_type, payload, severity="info"):
        self.calls["events"].append((event_type, payload, severity))

    def tick(self, now):
        from hscc_daemon import proxy_stall as _ps
        real_log = _ps.log
        _ps.log = lambda msg, level="INFO": self.calls["logs"].append(
            (level, msg))
        try:
            return ps.check_proxy_stall(
                socket_count_fn=self.socket_count,
                node_metrics_fn=self.node_metrics,
                probe_fn=self.probe_fn,
                restart_fn=self.restart_fn,
                intentional_fn=self.intentional_fn,
                write_state_fn=self.write_state,
                read_state_fn=self.read_state,
                emit_fn=self.emit,
                detector=self.detector,
                now=now, wall_now=self._wall + now)
        finally:
            _ps.log = real_log

    # ── helpers ──
    def last_state(self):
        return self.calls["states"][-1]


# ── (a) the incident shape: all three conditions >=120 s -> one restart ────

def test_confirmed_stall_restarts_once_writes_cooldown_records_event():
    h = Harness(sockets=4, fleet_sum=0, probe=True)
    # tick 0: streak starts (suspect) — no action yet
    assert h.tick(0.0) is True
    assert h.last_state()["status"] == "suspect"
    assert h.calls["restart"] == 0
    # ticks at 30/60/90 s: still suspect (120 s not reached) — nothing fires
    for t in (30.0, 60.0, 90.0):
        assert h.tick(t) is True
        assert h.last_state()["status"] == "suspect"
    assert h.calls["restart"] == 0
    # tick at 120 s: 4th consecutive qualifying tick -> CONFIRMED -> restart
    assert h.tick(120.0) is True
    assert h.calls["restart"] == 1
    st = h.last_state()
    assert st["status"] == "healthy" and st["ok"] is True
    # cooldown persisted IN the written state (daemon-restart survivable)
    assert st["restart_times"] and st["last_restart_ts"] == h._wall + 120.0
    assert st["restarts_last_10m"] == 1
    # events: exactly one confirmed + one heal
    kinds = [e[0] for e in h.calls["events"]]
    assert kinds == ["proxy.stall.confirmed", "proxy.stall.healed"]
    # state carries the verify-not-checked-safe shape
    for key in ("ok", "last_check", "stall_since", "restarts_last_10m"):
        assert key in st, key


def test_sustained_requires_four_consecutive_ticks_at_30s_cadence():
    # 30 s cadence x 120 s sustain => the first action tick is the 5th call
    # (tick0 starts the streak, ticks 30/60/90 observe, tick 120 confirms).
    h = Harness()
    for t in (0.0, 30.0, 60.0, 90.0):
        h.tick(t)
    assert h.calls["restart"] == 0
    h.tick(120.0)
    assert h.calls["restart"] == 1


# ── (b) fleet busy -> nothing ───────────────────────────────────────────────

def test_fleet_busy_never_restarts():
    h = Harness(sockets=4, fleet_sum=3, probe=True)
    for t in (0.0, 120.0, 300.0):
        assert h.tick(t) is True
    assert h.calls["restart"] == 0
    assert h.last_state()["status"] == "healthy"
    # busy fleet mid-suspect also RESETS the streak (condition b broken)
    h2 = Harness(sockets=4, fleet_sum=0, probe=True)
    h2.tick(0.0)
    assert h2.detector["since"] is not None
    h2.fleet_sum = 1
    h2.tick(30.0)
    assert h2.detector["since"] is None
    h2.fleet_sum = 0
    h2.tick(60.0)  # streak restarts from zero, not resumed
    h2.tick(150.0)  # 90 s since restart — NOT yet a stall
    assert h2.calls["restart"] == 0


# ── (c) probe not OK -> nothing ─────────────────────────────────────────────

def test_probe_known_negative_never_restarts():
    # proxy answers but generates nothing for our 5-token request (False):
    # condition c fails — this is NOT the held-stream shape (proxy fully sick;
    # check_proxy owns it). No restart, stream stays green (fail open).
    h = Harness(probe=False)
    for t in (0.0, 120.0, 300.0):
        assert h.tick(t) is True
    assert h.calls["restart"] == 0
    assert h.last_state()["status"] == "healthy"


def test_probe_unknown_fails_open_named():
    # transport failure (None) is UNKNOWN: fail open with a NAMED degradation.
    h = Harness(probe=None)
    assert h.tick(0.0) is True
    st = h.last_state()
    assert st["ok"] is True and st["status"] == "unknown"
    assert st["degradation"] == "probe_unreachable"
    assert h.calls["restart"] == 0


# ── (d) sockets idle -> nothing ─────────────────────────────────────────────

def test_no_held_socket_never_restarts():
    h = Harness(sockets=0, fleet_sum=0, probe=True)
    for t in (0.0, 120.0, 300.0):
        assert h.tick(t) is True
    assert h.calls["restart"] == 0
    assert h.last_state()["status"] == "healthy"


# ── (e) second stall inside 10 min -> suppressed ───────────────────────────

def test_second_stall_within_cooldown_is_suppressed_not_restarted():
    h = Harness()
    for t in (0.0, 30.0, 60.0, 90.0, 120.0):
        h.tick(t)                      # first stall -> restart #1
    assert h.calls["restart"] == 1
    # the stall comes back immediately (restart did not stick; heal probe was
    # fine but litellm re-freezes): the SECOND confirmation must NOT restart.
    h.probe_seq = None
    h.restart_ok = True
    for t in (150.0, 180.0, 210.0, 240.0, 270.0):
        h.tick(t)                      # 2nd sustained stall at t=270
    assert h.calls["restart"] == 1, "cooldown must suppress the second restart"
    st = h.last_state()
    assert st["status"] == "suppressed" and st["ok"] is False
    assert st["degradation"] == "restart_suppressed_cooldown"
    kinds = [e[0] for e in h.calls["events"]]
    assert kinds.count("proxy.stall.confirmed") == 2  # both stalls observed
    assert kinds.count("proxy.stall.suppressed") == 1  # second one suppressed
    assert any("SUPPRESSED" in m for _, m in h.calls["logs"])


def test_cooldown_expires_and_allows_next_restart():
    h = Harness()
    for t in (0.0, 30.0, 60.0, 90.0, 120.0):
        h.tick(t)
    assert h.calls["restart"] == 1
    # stall re-declares >10 min after the restart stamp -> allowed again
    for t in (300.0, 330.0, 360.0, 390.0, 420.0, 450.0, 480.0, 510.0, 540.0,
              660.0, 750.0):
        h.tick(t)
    assert h.calls["restart"] == 2


def test_failed_repair_still_spends_the_cooldown():
    # A restart that does NOT verify must not be retryable 30 s later — the
    # bounce-kill protection is the whole point of the 1/10min limit.
    h = Harness(restart_ok=False)
    for t in (0.0, 30.0, 60.0, 90.0, 120.0):
        h.tick(t)
    assert h.calls["restart"] == 1
    assert h.last_state()["status"] == "stall" and h.last_state()["ok"] is False
    for t in (150.0, 180.0, 210.0, 240.0, 270.0):
        h.tick(t)
    assert h.calls["restart"] == 1


# ── (f) intentional autodown -> inert ───────────────────────────────────────

def test_intentional_autodown_tick_is_inert():
    h = Harness(intentional=True, sockets=4, fleet_sum=0, probe=True)
    for t in (0.0, 120.0, 300.0):
        assert h.tick(t) is True
    assert h.calls["restart"] == 0
    assert h.detector["since"] is None
    st = h.last_state()
    assert st["status"] == "inert" and st["ok"] is True
    assert "autodown" in st["message"]
    # gates BEFORE any seam runs: nothing was even queried
    assert h.calls["events"] == []


# ── (g) stale stall_since resets when the condition clears ─────────────────

def test_stall_since_resets_when_condition_clears():
    h = Harness(sockets=4, fleet_sum=0, probe=True)
    h.tick(0.0)
    assert h.detector["since"] == 0.0
    h.sockets = 0            # the held stream drained on its own
    h.tick(30.0)
    assert h.detector["since"] is None
    # a re-freeze starts the window from zero again, not from t=0
    h.sockets = 4
    h.tick(60.0)
    assert h.detector["since"] == 60.0
    h.tick(150.0)            # 90 s of the NEW window — not a stall
    assert h.calls["restart"] == 0
    h.tick(180.0)            # 120 s of the new window — stall
    assert h.calls["restart"] == 1


# ── UNKNOWN reads fail open with named degradations ─────────────────────────

def test_socket_query_unknown_fails_open_named():
    h = Harness(sockets_unknown=True)
    assert h.tick(0.0) is True
    st = h.last_state()
    assert st["ok"] is True and st["degradation"] == "socket_query_failed"


def test_node_metrics_unreachable_fails_open_never_idle():
    h = Harness(metrics_unknown="metrics unreachable: 100.64.0.1:8000")
    assert h.tick(0.0) is True
    st = h.last_state()
    assert st["ok"] is True and st["status"] == "unknown"
    assert "metrics unreachable" in st["degradation"]
    # and it resets a running suspect streak (observation gap != confirmation)
    h2 = Harness()
    h2.tick(0.0)
    assert h2.detector["since"] is not None
    h2.metrics_unknown = "metrics unreachable: 100.64.0.9:8000"
    h2.tick(130.0)  # would have confirmed if metrics had answered idle
    assert h2.detector["since"] is None
    assert h2.calls["restart"] == 0


def test_raising_seam_fails_open_never_restarts():
    def boom():
        raise RuntimeError("seam exploded")
    from hscc_daemon import daemon_ops
    real_log = daemon_ops.log
    daemon_ops.log = lambda *a, **k: None
    try:
        assert ps.check_proxy_stall(
            socket_count_fn=boom, node_metrics_fn=lambda: {"sum": 0},
            probe_fn=lambda: True, restart_fn=lambda: {"ok": True},
            intentional_fn=lambda: False,
            write_state_fn=lambda s, d: None, read_state_fn=lambda s: {},
            emit_fn=lambda *a, **k: None, detector={"since": None},
            now=0.0, wall_now=1.0) is True
    finally:
        daemon_ops.log = real_log


# ── (h) mutation proofs: each guard guards exactly its own test ────────────

def _mutate(monkeypatch, target, replacement):
    monkeypatch.setattr(ps, target, replacement, raising=True)


def _drive_full_stall(h):
    for t in (0.0, 30.0, 60.0, 90.0, 120.0):
        h.tick(t)
    return h.calls["restart"]


def test_mutant_condition_a_removed_still_restarts_when_sockets_idle(monkeypatch):
    """Removing the `sockets >= 1` guard must make (d) fail and ONLY (d)."""
    _mutate(monkeypatch, "_cond_a", lambda sockets: True)
    h = Harness(sockets=0, fleet_sum=0, probe=True)
    assert _drive_full_stall(h) == 1          # (d) fails: restarted with no socket
    # (b) still holds: fleet busy still stops it...
    h = Harness(sockets=4, fleet_sum=3, probe=True)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0
    # ...and (c) still holds: probe-negative still stops it.
    h = Harness(probe=False)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0


def test_mutant_condition_b_removed_still_restarts_when_fleet_busy(monkeypatch):
    """Removing the `sum == 0` guard must make (b) fail and ONLY (b)."""
    _mutate(monkeypatch, "_cond_b", lambda metrics: True)
    h = Harness(sockets=4, fleet_sum=3, probe=True)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 1            # (b) fails
    h = Harness(sockets=0, fleet_sum=0, probe=True)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0            # (d) still holds
    h = Harness(probe=False)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0            # (c) still holds


def test_mutant_condition_c_removed_still_restarts_when_probe_fails(monkeypatch):
    """Removing the `probe is True` guard must make (c) fail and ONLY (c)."""
    _mutate(monkeypatch, "_cond_c", lambda probe: True)
    h = Harness(probe=False)
    for t in (0.0, 120.0):
        h.tick(t)
    assert h.calls["restart"] == 1            # (c) fails
    h = Harness(sockets=0, fleet_sum=0, probe=True)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0            # (d) still holds
    h = Harness(sockets=4, fleet_sum=3, probe=True)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0            # (b) still holds


def test_mutant_cooldown_removed_fails_only_test_e(monkeypatch):
    """Removing the cooldown gate must make (e) fail and ONLY (e)."""
    _mutate(monkeypatch, "_cond_cooldown", lambda in_window, wall_now: False)
    h = Harness()
    for t in (0.0, 30.0, 60.0, 90.0, 120.0):
        h.tick(t)
    assert h.calls["restart"] == 1
    for t in (150.0, 180.0, 210.0, 240.0, 270.0):
        h.tick(t)
    assert h.calls["restart"] == 2            # (e) fails: bounce-killed twice
    # every other scenario is untouched by the cooldown gate
    h = Harness(sockets=0)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0
    h = Harness(fleet_sum=3)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0
    h = Harness(intentional=True)
    for t in (0.0, 120.0, 300.0):
        h.tick(t)
    assert h.calls["restart"] == 0


# ── seam defaults are wired to the sanctioned paths (no reimplementation) ──

def test_default_restart_uses_the_existing_proxy_repair_recipe():
    """_default_restart must route through health._proxy_repair_orphan (the
    stop -> TERM/KILL -> start recipe with port-owner verification) under the
    heal lease — NOT reimplement the lifecycle (sparkrun owns it)."""
    import inspect
    src = inspect.getsource(ps._default_restart)
    assert "_proxy_repair_orphan" in src
    assert "heal_lease" in src
    # and the repair itself is the existing single implementation
    from hscc_daemon import health
    assert "proxy" in inspect.getsource(health._proxy_repair_orphan)


def test_default_node_metrics_uses_shared_metrics_parser():
    """Fleet facts via serving.json + throughput.fetch_node_metrics — never
    ssh, never local docker ps."""
    import inspect
    src = inspect.getsource(ps._default_node_metrics)
    assert "fetch_node_metrics" in src
    assert "load_serving" in src
    assert "ssh" not in src and "docker" not in src


def test_intentional_gate_reuses_the_shared_classify_verdict():
    import inspect
    src = inspect.getsource(ps._default_intentional)
    assert "_intentional_autodown_down" in src


# ── registration: cadence SOT + CHECKS row (the card's step 1) ─────────────

def test_cadence_is_registered_in_the_sot():
    from hscc_daemon import daemon_ops, verify
    assert daemon_ops.PERIODIC_INTERVALS["proxy_stall"] == 30
    assert verify._live_stream_intervals().get("proxy_stall") == 30


def test_stream_starts_a_supervised_check_thread_from_the_sot(monkeypatch):
    """proxy_stall must get exactly ONE supervised thread at the SOT cadence
    (a CHECKS row + no private sleep loop — the kanban_enforce precedent)."""
    from hscc_daemon import daemon_ops, daemon_singleton as ds
    seen = []

    def fake_supervised(stop_event, check_fn, interval, stream_name):
        seen.append((stream_name, interval, check_fn))
        stop_event.set()

    for path in ("hscc_daemon.autodown.resume_from_restart_defensive",
                 "hscc_daemon.serving.resolve_cluster_config",
                 "hscc_daemon.sparkrun_bin.ensure_on_path",
                 "hscc_daemon.recover.recover_engine_wedge",
                 "hscc_daemon.dispatcher_wedge.recover_dispatcher_wedge",
                 "hscc_daemon.lifecycle.pipeline_watchdog",
                 "hscc_daemon.trigger.trigger_engine"):
        monkeypatch.setattr(path, lambda *a, **k: None)
    monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                        lambda **k: (True, ""))
    monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(daemon_ops, "run_autodown_loop", lambda ev, **k: None)
    monkeypatch.setattr(daemon_ops, "_run_supervised_periodic", fake_supervised)
    monkeypatch.setattr(daemon_ops, "run_heartbeat_loop",
                        lambda ev, **k: ev.wait(2))
    monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
    monkeypatch.setattr(daemon_ops, "prune_dead_files",
                        lambda *a, **k: {"removed_dead": 0, "pruned_bak": 0})
    daemon_ops.run_daemon_loop()
    rows = [s for s in seen if s[0] == "proxy_stall"]
    assert len(rows) == 1, seen
    assert rows[0][1] == 30
    assert rows[0][2] is ps.check_proxy_stall


def test_stream_state_file_is_verify_not_checked_safe(tmp_path, monkeypatch):
    """verify must never report the proxy_stall stream as `not-checked` for a
    missing ok/timestamp: the tick's stamps carry both on every path."""
    from hscc_daemon import state, verify
    monkeypatch.setattr(state, "STATE_DIR", str(tmp_path))
    d = {"since": None}
    ps.check_proxy_stall(
        socket_count_fn=lambda: 0, node_metrics_fn=lambda: {"sum": 0},
        probe_fn=lambda: True, restart_fn=lambda: {"ok": True},
        intentional_fn=lambda: False, detector=d, now=0.0, wall_now=1.0)
    res = verify.check_daemon_streams(state_dir=str(tmp_path))
    names = [i for i in res.get("detail", "") + str(res)]
    assert "not-checked" not in str(res) or "proxy_stall" not in str(res), names
