"""Engine-wedge false-positive tests (t_e37a74c7).

Two defects produced 19 `WEDGED unit(s) detected` ERRORs on idle-healthy units
on 2026-10-24 while the operator's own metrics read 0 running / 0 waiting:

  (A) DOUBLE-PROBE PER TICK — two full daemon loops ran in two processes
      (launchd `start-daemon` + an orphaned `hscc start` fork; both writers
      hold ~/.hscc/daemon.log open per lsof). Each loop probes every unit on
      its own 60 s tick ~1.6-2.6 s apart, so one "tick" hits every engine
      twice with two independent streak memories — the source of the
      contradictory verdicts (3/3 ok and WEDGED 1.6 s apart).
      Fix: a per-unit probe result recorded under the state dir is shared —
      any caller whose OWN last probe of that unit is older than a recorded
      sibling result within the TTL consumes it instead of re-probing
      (cross-process by design: an in-process memo cannot cover two
      processes, and a same-process repeat must keep re-probing so the
      multi-tick debounce tests keep their meaning).

  (B) MISCLASSIFICATION — every false wedge probe returned in 2.2-4.0 s for
      all 3 units together (a true stall burns the full >=10 s bound per
      unit), i.e. the HTTP 200 SSE stream CLOSED EARLY with zero tokens
      (peer close / EOF — swallowed by the reader's bare except) and was
      classified "wedged" exactly like a genuine stalled engine. The
      load-aware guard cannot save that: /metrics is idle, so the failure
      counts, and the sibling caller's near-simultaneous re-probe defeats
      the 2-tick debounce.
      Fix: "200 + stream ended on its own before the budget" is INCONCLUSIVE
      (own streak; the unit is neither wedged nor healthy), and only a
      sustained run of inconclusive ticks escalates into wedge credit. A
      probe that held the full window with zero tokens remains the genuine
      wedge signature, unchanged.

All tests are hermetic: probes are monkeypatched fakes or in-process
http.server fixtures, and the autouse _isolate_hscc conftest fixture keeps
every state write (including the probe-share files) inside a per-test tmp dir.
"""

import hashlib
import http.server
import json
import os
import threading
import time

import pytest

from hscc_daemon import health
from hscc_daemon import serving


@pytest.fixture(autouse=True)
def _reset_engine_wedge_state():
    health._engine_wedge_units.clear()
    yield
    health._engine_wedge_units.clear()


UNIT = {
    "id": "unit-247", "role": "worker", "keepalive": True, "model": "m2",
    "nodes": ["10.0.0.247"], "port": 8001, "tp": 1,
}


def _patch_serving(monkeypatch, units):
    monkeypatch.setattr(serving, "load_serving",
                        lambda: {"version": 2, "units": units})


def _idle(monkeypatch):
    """Operator-verified state: /metrics reachable, 0 running, 0 waiting."""
    monkeypatch.setattr(health, "_unit_busy", lambda node, port: False)


def _capture_logs(monkeypatch):
    lines = []
    monkeypatch.setattr(health, "log",
                        lambda msg, level="INFO": lines.append((level, msg)))
    return lines


def _counting_probe(result_fn):
    calls = []

    def probe(node, port, timeout=None, max_tokens=None):
        calls.append((node, port))
        return result_fn(node, port)

    probe.calls = calls
    return probe


def _ok_result(node, port, timeout=None, max_tokens=None):
    return {"ok": True, "status": 200, "text": "ok", "error": ""}


def _closed_early_result(node, port, timeout=None, max_tokens=None):
    # The 2026-10-24 signature: 200, the stream ended by itself well inside
    # the bound, zero tokens.
    return {"ok": False, "status": 200, "error": "closed_early"}


def _share_path(node, port):
    from hscc_daemon import state
    digest = hashlib.sha1(("%s:%s" % (node, port)).encode()).hexdigest()[:16]
    return os.path.join(state.STATE_DIR, f"engine-wedge-probe-{digest}.probe")


def _write_share(node, port, ts, result, pid=None):
    """Fabricate a sibling process's recorded probe result."""
    path = _share_path(node, port)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"ts": ts, "pid": pid if pid is not None else os.getpid() + 7777,
             "probe_timeout": float(health.ENGINE_WEDGE_TIMEOUT),
             "result": result}
    tmp = path + ".fab.tmp"
    with open(tmp, "w") as f:
        json.dump(entry, f)
    os.replace(tmp, path)


# ── (B) classification: early-closed 200 is NOT a stalled engine ─────────


class _FakeEngine(http.server.BaseHTTPRequestHandler):
    """Fake vLLM endpoint. Modes per-server:
      'healthy'     streams a token then [DONE];
      'stalled'     200 + SSE headers, holds the connection with zero tokens
                    until the client gives up (the genuine wedge signature);
      'close_early' 200 + SSE headers then a CLEAN close with zero tokens
                    (the 2026-10-24 flake: peer close mid-handshake).
    """

    def do_POST(self):
        import socket as _socket
        body = b""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)  # drain so our close never RSTs
        mode = self.server.mode
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if mode == "healthy":
            chunk = ("data: %s\n\n" % json.dumps(
                {"id": "x", "choices": [{"index": 0,
                                         "delta": {"content": "ok"},
                                         "finish_reason": None}]})).encode()
            try:
                self.wfile.write(chunk)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            self._hold(_socket)
        elif mode == "stalled":
            self._hold(_socket)
        else:  # close_early — clean FIN with zero tokens.
            return

    def _hold(self, _socket):
        try:
            self.connection.settimeout(5)
            while not self.server.stop_event.is_set():
                if not self.connection.recv(1024):
                    return
        except Exception:
            pass

    def do_GET(self):  # /v1/models — the probe asks which model is served
        payload = json.dumps({"data": [{"id": "fake-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:
            pass

    def log_message(self, format, *args):
        pass


@pytest.fixture()
def live_engine():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeEngine)
    server.daemon_threads = True
    server.mode = "healthy"
    server.stop_event = threading.Event()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.stop_event.set()
        server.shutdown()
        server.server_close()


class TestEarlyCloseClassification:
    def test_closed_early_stream_is_not_wedged(self, live_engine):
        """200 + clean close + zero tokens well inside the bound is
        INCONCLUSIVE, not the wedge signature (RED today: returns wedged)."""
        live_engine.mode = "close_early"
        r = health._probe_unit_generation(
            "127.0.0.1", live_engine.server_address[1], timeout=10)
        assert r["ok"] is False
        assert r["status"] == 200
        assert r["error"] == "closed_early"

    def test_full_window_stall_remains_wedged(self, live_engine):
        """Regression pin the other way: a stream that holds the FULL bound
        with zero tokens is still classified 'wedged' (detection preserved)."""
        live_engine.mode = "stalled"
        t0 = time.monotonic()
        r = health._probe_unit_generation(
            "127.0.0.1", live_engine.server_address[1], timeout=1)
        elapsed = time.monotonic() - t0
        assert r["ok"] is False
        assert r["error"] == "wedged"
        assert elapsed >= 0.8  # held (roughly) the whole window


# ── (B) check-level: idle-healthy unit + early-close blips never ERROR ───


class TestIdleHealthyNoWedge:
    def test_idle_healthy_never_wedges_never_errors(self, monkeypatch):
        """Card bar: fabricated idle-healthy metrics + chat 200 with text on
        every unit => no WEDGED verdict and no ERROR line, across ticks."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 0)
        monkeypatch.setattr(health, "_probe_unit_generation", _ok_result)
        _idle(monkeypatch)
        lines = _capture_logs(monkeypatch)

        for _ in range(3):
            assert health.check_engine_wedge() is True

        state = _read("engine_wedge")
        assert state["wedged"] == []
        assert state["ok"] is True
        assert not [lvl for lvl, msg in lines
                    if lvl == "ERROR" and "WEDGED" in msg]

    def test_isolated_early_close_blips_never_wedge(self, monkeypatch):
        """The 2026-10-24 pattern: one early-closed probe per engine window,
        healthy between => never WEDGED, never an ERROR line."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 2)
        monkeypatch.setattr(health, "ENGINE_WEDGE_INCONCLUSIVE_THRESHOLD", 3)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 0)
        _idle(monkeypatch)
        lines = _capture_logs(monkeypatch)

        seq = [_closed_early_result, _ok_result, _closed_early_result,
               _ok_result, _closed_early_result, _ok_result]
        it = iter(seq)

        def probe(node, port, timeout=None):
            return next(it)(node, port)

        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        for _ in range(len(seq)):
            assert health.check_engine_wedge() is True
        state = _read("engine_wedge")
        assert state["wedged"] == []
        assert not [lvl for lvl, msg in lines
                    if lvl == "ERROR" and "WEDGED" in msg]
        # The blips are still reported honestly (inconclusive, not silently
        # dropped, and NOT counted as wedge credit).
        st = health._engine_wedge_units["unit-247"]
        assert st["consecutive_failures"] == 0

    def test_sustained_early_close_still_gets_caught(self, monkeypatch):
        """No false-negative regression: a unit whose stream closes early on
        EVERY tick escalates into wedge credit and is declared wedged once
        the wedge debounce passes."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 2)
        monkeypatch.setattr(health, "ENGINE_WEDGE_INCONCLUSIVE_THRESHOLD", 2)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 0)
        _idle(monkeypatch)
        lines = _capture_logs(monkeypatch)
        monkeypatch.setattr(health, "_probe_unit_generation",
                            _closed_early_result)

        # tick 1: inconclusive, below its own threshold -> no escalation.
        assert health.check_engine_wedge() is True
        # tick 2: sustained -> escalates to the FIRST wedge failure (debounce
        # still applies: no ERROR yet).
        assert health.check_engine_wedge() is True
        # tick 3: second consecutive escalated failure crosses the wedge
        # threshold -> declared wedged, ERROR now legitimate.
        assert health.check_engine_wedge() is False
        state = _read("engine_wedge")
        assert [w["unit"] for w in state["wedged"]] == ["unit-247"]
        assert [lvl for lvl, msg in lines
                if lvl == "ERROR" and "WEDGED" in msg]


# ── (A) one probe result per tick, shared across caller processes ────────


class TestProbeSharedPerTick:
    def test_sibling_result_within_ttl_is_not_reprobed(self, monkeypatch):
        """Call-count pin (cross-process form): a sibling caller already
        probed this unit for the current tick; our caller consumes the shared
        result and does NOT touch the engine again."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 20)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        # Caller 1 probes once for this tick...
        assert health.check_engine_wedge() is True
        assert len(probe.calls) == 1
        # ...caller 2 (its own streak memory has NOT seen this unit probed —
        # exactly the sibling daemon's state) runs its tick ~1.6 s later:
        # the shared result is consumed, the engine is NOT probed again.
        health._engine_wedge_units["unit-247"]["last_probe_ts"] = 0.0
        assert health.check_engine_wedge() is True
        assert len(probe.calls) == 1, "sibling's probe must be reused"
        state = _read("engine_wedge")
        assert [o["unit"] for o in state["ok_units"]] == ["unit-247"]

    def test_own_newer_result_beats_older_sibling_record(self, monkeypatch):
        """A caller that already probed for this tick must not consume a
        sibling record written BEFORE its own probe — freshness wins both
        directions, and two callers still can never both be 'newest'."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 20)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        assert health.check_engine_wedge() is True
        assert len(probe.calls) == 1
        mine = health._engine_wedge_units["unit-247"]["last_probe_ts"]
        # A sibling record from BEFORE our probe: not newer than our own
        # last probe => we keep our fresh data and probe for our next tick.
        _write_share("10.0.0.247", 8001, mine - 5, _ok_result(None, None))
        assert health.check_engine_wedge() is True
        assert len(probe.calls) == 2

    def test_sibling_result_beyond_ttl_is_not_reused(self, monkeypatch):
        """TTL bounds staleness: a sibling result older than the TTL is not
        'this tick's data' — probe afresh."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 20)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        _write_share("10.0.0.247", 8001, time.time() - 60,
                     _ok_result(None, None))
        assert health.check_engine_wedge() is True
        assert len(probe.calls) == 1

    def test_fresh_claim_waits_for_sibling_result_not_double_probe(
            self, monkeypatch):
        """True concurrency: a sibling holds the claim (probe in flight). We
        wait for its result instead of firing a second streaming probe at
        the same engine (the collision that closed the 2026-10-24 probes)."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 20)
        monkeypatch.setattr(health, "ENGINE_WEDGE_TIMEOUT", 1)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        path = _share_path("10.0.0.247", 8001)
        claim = path + ".claim"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(claim, "w") as f:
            json.dump({"pid": 999999, "ts": time.time()}, f)

        def sibling_finishes():
            time.sleep(0.3)
            _write_share("10.0.0.247", 8001, time.time(), _ok_result(None, None))
            try:
                os.unlink(claim)
            except OSError:
                pass

        t = threading.Thread(target=sibling_finishes, daemon=True)
        t.start()
        try:
            assert health.check_engine_wedge() is True
        finally:
            t.join(timeout=5)
        assert len(probe.calls) == 0, "must wait for the sibling, not re-probe"
        state = _read("engine_wedge")
        assert [o["unit"] for o in state["ok_units"]] == ["unit-247"]

    def test_dead_claim_is_stolen_and_probe_runs(self, monkeypatch):
        """Crash safety: a claim whose holder died (stale) is stolen and the
        unit IS probed — sharing can never silence the check forever."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 20)
        monkeypatch.setattr(health, "ENGINE_WEDGE_TIMEOUT", 1)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        path = _share_path("10.0.0.247", 8001)
        claim = path + ".claim"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(claim, "w") as f:
            json.dump({"pid": 999999, "ts": time.time() - 999}, f)

        t0 = time.monotonic()
        assert health.check_engine_wedge() is True
        assert time.monotonic() - t0 < 5, "stale steal must not wait out the horizon"
        assert len(probe.calls) == 1
        assert not os.path.exists(claim), "claim must not leak"

    def test_share_ttl_zero_disables_sharing(self, monkeypatch):
        """Escape hatch: TTL=0 (env-tunable) returns to probe-per-call."""
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 0)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        assert health.check_engine_wedge() is True
        health._engine_wedge_units["unit-247"]["last_probe_ts"] = 0.0
        assert health.check_engine_wedge() is True
        assert len(probe.calls) == 2

    def test_cli_check_run_neither_shares_nor_publishes(self, monkeypatch):
        """`hscc check engine_wedge` (persist_disabled) probes FRESH on the
        operator's explicit request and publishes no probe-share of its own —
        a manual run must not consume or overwrite the daemon's per-tick
        state (same contract state.write_state already honours)."""
        from hscc_daemon import state as _state
        _patch_serving(monkeypatch, [UNIT])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_PROBE_SHARE_TTL", 20)
        _idle(monkeypatch)
        probe = _counting_probe(_ok_result)
        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        # A daemon-published fresh result exists — the CLI must ignore it...
        _write_share("10.0.0.247", 8001, time.time(),
                     {"ok": False, "status": 200, "error": "wedged"})
        with _state.persist_disabled():
            assert health.check_engine_wedge() is True
        # ...and probe for itself (a wedged share must not fake the CLI's OK).
        assert len(probe.calls) == 1
        # The CLI published NO share of its own (its ok result did not
        # replace the daemon's wedged one).
        entry = health._engine_wedge_share_read(
            _share_path("10.0.0.247", 8001))
        assert entry["result"]["error"] == "wedged"


def _read(stream):
    from hscc_daemon.state import read_state
    state = read_state(stream)
    assert state is not None, f"no state written for stream {stream!r}"
    return state
