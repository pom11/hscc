"""Tests for the engine-wedge probe (check_engine_wedge, E stream).

Coverage maps 1:1 to the task's proof requirements:
  * a FAKE unit returning HTTP 200 but no generated text is detected as WEDGED
  * a healthy unit streaming real text is NOT flagged
  * a slow-but-eventually-successful response does NOT trip the alert
    (consecutive-failure debounce — never alert on a single slow response)
  * a still-loading unit is NOT a wedge candidate
  * probing is SKIPPED entirely during an intentional autodown
  * one wedged unit does not mark a healthy sibling unhealthy
  * alerting rides the trigger-engine path (state.engine_wedge.degraded)

All tests isolate I/O by monkeypatching serving.load_serving and
health._probe_unit_generation — no test ever makes a real network request or
writes to the operator's live ~/.hscc (the autouse _isolate_hscc fixture in
conftest redirects every ~/.hscc path to a per-test tmp dir).
"""

import json
import http.server

import pytest

from hscc_daemon import health
from hscc_daemon import serving


@pytest.fixture(autouse=True)
def _reset_engine_wedge_state():
    """Each test starts with a clean in-memory per-unit wedge tracker.

    ``health._engine_wedge_units`` is deliberately module-level (it outlives a
    single frame so consecutive-failure streaks accumulate across daemon ticks).
    Without resetting it per test, one test's streak leaks into the next and a
    later test sees stale consecutive-failure credit.
    """
    health._engine_wedge_units.clear()
    yield
    health._engine_wedge_units.clear()


UNIT_ORCH = {
    "id": "orch", "role": "orchestrator", "model": "m1",
    "nodes": ["10.0.0.244", "10.0.0.246"], "port": 8000, "tp": 2,
}
UNIT_WORKER = {
    "id": "worker-247", "role": "worker", "keepalive": True, "model": "m2",
    "nodes": ["10.0.0.247", "10.0.0.248"], "port": 8000, "tp": 2,
}


def _patch_serving(monkeypatch, units):
    monkeypatch.setattr(serving, "load_serving",
                        lambda: {"version": 2, "units": units})


def _probe_ok(node, port):
    return {"ok": True, "status": 200, "text": "ok", "error": ""}


def _probe_wedged(node, port):
    # HTTP 200 but no generated text — the wedge signature the probe detects.
    return {"ok": False, "status": 200, "error": "wedged"}


def _probe_down(node, port):
    return {"ok": False, "status": None, "error": "down"}


# A probe that returns text only on the SECOND attempt from a unit, simulating
# a slow-but-eventually-successful response: the first call timeouts, the next
# streams fine. Never alerts on that single slow response.
def _probe_slow_first_ok(node, port):
    state = _probe_slow_first_ok.__dict__
    state["calls"] = state.get("calls", 0) + 1
    attempt = state["calls"]
    if attempt == 1:
        return _probe_wedged(node, port)
    return _probe_ok(node, port)


# Healthy on the FIRST call, then wedged forever after. Simulates a unit whose
# engine regresses mid-flight: it streamed fine, then wedged.
def _probe_ok_then_wedged(node, port):
    state = _probe_ok_then_wedged.__dict__
    state["calls"] = state.get("calls", 0) + 1
    if state["calls"] == 1:
        return _probe_ok(node, port)
    return _probe_wedged(node, port)


class TestEngineWedgeProbe:
    def test_healthy_unit_not_flagged(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_ok)

        ok = health.check_engine_wedge()
        assert ok is True
        state = _read("engine_wedge")
        assert state["ok"] is True
        assert state["wedged"] == []
        assert len(state["ok_units"]) == 1
        assert state["ok_units"][0]["unit"] == "orch"

    def test_fake_200_no_text_detected_as_wedged(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_WORKER])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 1)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_wedged)

        ok = health.check_engine_wedge()
        assert ok is False
        state = _read("engine_wedge")
        assert state["ok"] is False
        assert len(state["wedged"]) == 1
        assert state["wedged"][0]["unit"] == "worker-247"
        # Actionable detail: which unit, why, and how long since it last
        # streamed successfully. Here the unit was wedged from its FIRST probe
        # (no prior success), so stalled_for_s is None (unknown) and last_success
        # is absent — the stream still names the unit, node, port and reason.
        w = state["wedged"][0]
        assert w["node"].endswith(".247")
        assert "no generated tokens" in w["message"]
        assert w.get("last_success") is None
        assert w.get("stalled_for_s") is None

    def test_single_slow_response_does_not_trip(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 2)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_wedged)

        # First slow/empty response: debouncing, unit in "checking", still ok.
        ok = health.check_engine_wedge()
        assert ok is True
        state = _read("engine_wedge")
        assert state["ok"] is True
        assert state["wedged"] == []
        assert len(state["loading"]) == 1
        assert state["loading"][0]["status"] == "checking"

        # Second consecutive empty response crosses the threshold -> wedged.
        ok = health.check_engine_wedge()
        assert ok is False
        state = _read("engine_wedge")
        assert len(state["wedged"]) == 1

    def test_slow_but_eventually_successful_recovers(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 2)
        _probe_slow_first_ok.__dict__.pop("calls", None)
        monkeypatch.setattr(health, "_probe_unit_generation",
                            _probe_slow_first_ok)

        # One slow response (below threshold) -> still ok, unit "checking/ok".
        ok = health.check_engine_wedge()
        assert ok is True
        # Next tick streams fine -> recovers, no wedge ever fireable here.
        ok = health.check_engine_wedge()
        assert ok is True
        state = _read("engine_wedge")
        assert state["wedged"] == []

    def test_stalled_detail_reports_last_success(self, monkeypatch):
        """A unit that streamed fine, then wedged, records when it last
        succeeded and how long it has been stalled — the detail an operator
        needs to act."""
        _patch_serving(monkeypatch, [UNIT_WORKER])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 1)
        _probe_ok_then_wedged.__dict__.pop("calls", None)
        monkeypatch.setattr(health, "_probe_unit_generation",
                            _probe_ok_then_wedged)

        # Tick 1: streams fine -> last_success recorded.
        assert health.check_engine_wedge() is True
        # Tick 2: wedged -> stream reports the wedge with last-succeful detail.
        assert health.check_engine_wedge() is False
        state = _read("engine_wedge")
        w = state["wedged"][0]
        assert w["unit"] == "worker-247"
        assert w["status"] == "wedged"
        assert w["last_success"] is not None          # had streamed before
        assert w["stalled_for_s"] is not None         # time since that success
        assert w["stalled_for_s"] >= 0
        assert "no generated tokens" in w["message"]

    def test_loading_unit_is_not_wedge_candidate(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH])
        # LOAD_GRACE stays at a positive default: a fresh unit (first_seen=now)
        # is inside the grace and must NOT be probed.
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 240)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_wedged)

        # Even though the probe WOULD wedge, the unit is still loading -> ok.
        ok = health.check_engine_wedge()
        assert ok is True
        state = _read("engine_wedge")
        assert state["ok"] is True
        assert state["wedged"] == []
        assert len(state["loading"]) == 1
        assert state["loading"][0]["status"] == "loading"

    def test_wedged_unit_does_not_mark_healthy_sibling(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH, UNIT_WORKER])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 1)

        def probe(node, port):
            # worker-247 (primary .247) wedged; orch (.244) healthy
            return _probe_wedged(node, port) if node.endswith(".247") \
                else _probe_ok(node, port)

        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        ok = health.check_engine_wedge()
        assert ok is False
        state = _read("engine_wedge")
        # The wedged one is reported...
        assert [w["unit"] for w in state["wedged"]] == ["worker-247"]
        # ...and the healthy sibling stays healthy (not swept into wedged).
        assert [o["unit"] for o in state["ok_units"]] == ["orch"]

    def test_down_unit_not_reported_as_wedge(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_down)

        ok = health.check_engine_wedge()
        # A down (unreachable) unit is NOT a wedge-stream failure — that is the
        # workers/gateway streams' job. Recorded in details only.
        assert ok is True
        state = _read("engine_wedge")
        assert state["ok"] is True
        assert state["wedged"] == []
        assert len(state["down"]) == 1

    def test_intentional_autodown_skips_probe_entirely(self, monkeypatch):
        _patch_serving(monkeypatch, [UNIT_ORCH, UNIT_WORKER])
        monkeypatch.setattr(health, "_intentional_window", lambda: True)
        # A probe that MUST NOT be called during the skip — if the skip is
        # correct, this never fires.
        probe_calls = []

        def probe(node, port):
            probe_calls.append(node)
            return _probe_wedged(node, port)

        monkeypatch.setattr(health, "_probe_unit_generation", probe)

        ok = health.check_engine_wedge()
        assert ok is True
        assert probe_calls == []  # probing skipped entirely
        state = _read("engine_wedge")
        assert state["ok"] is True
        assert state.get("intentional") == "autodown"
        assert state["skipped"] == "intentional autodown"

    def test_no_units(self, monkeypatch):
        _patch_serving(monkeypatch, [])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_wedged)
        ok = health.check_engine_wedge()
        assert ok is True
        state = _read("engine_wedge")
        assert state["ok"] is True


class TestEngineWedgeTrigger:
    def test_degraded_pseudo_event_reaches_trigger_engine(self, monkeypatch):
        """Alerting rides the trigger-engine path: an ok:False engine_wedge
        stream must show up as a state.engine_wedge.degraded pseudo-event that
        a rule can fire on — no new notification channel."""
        from hscc_daemon import trigger

        _patch_serving(monkeypatch, [UNIT_WORKER])
        monkeypatch.setattr(health, "ENGINE_WEDGE_LOAD_GRACE", 0)
        monkeypatch.setattr(health, "ENGINE_WEDGE_THRESHOLD", 1)
        monkeypatch.setattr(health, "_probe_unit_generation", _probe_wedged)

        # Make the stream wedged (ok:False).
        assert health.check_engine_wedge() is False

        # A rule targeted at the wedge pseudo-event must fire.
        rule = {
            "id": "engine-wedge-detected",
            "trigger_type": "notify",
            "condition": {"metric": "event_type", "op": "==",
                          "value": "state.engine_wedge.degraded"},
            "trigger_params": {"title": "HSCC: inference engine wedged",
                               "body": "A unit answers 200 but never streams."},
        }
        monkeypatch.setattr(trigger, "load_triggers", lambda: [rule])
        monkeypatch.setattr(trigger, "load_cooldowns", lambda: {})
        monkeypatch.setattr(trigger, "save_cooldowns", lambda d: None)
        monkeypatch.setattr(trigger, "read_events_tail", lambda limit=100: [])

        fired = {}
        monkeypatch.setattr(trigger, "send_macos_notification",
                            lambda title, body, priority="normal":
                            fired.setdefault("notify", True))

        # Rebuild _probe_wedged closed __dict__ so the fresh probe wedges again
        # if needed; already wedged from above.
        ok = trigger.trigger_engine()
        # trigger_engine itself returns True when evaluation succeeds.
        assert ok is True
        assert fired.get("notify") is True, "wedge rule should have fired"


def _read(stream):
    """Read a stream's state written by check_engine_wedge (tmp-isolated)."""
    from hscc_daemon.state import read_state
    # read_state uses the patched STATE_DIR (tmp) via conftest isolation
    state = read_state(stream)
    assert state is not None, f"no state written for stream {stream!r}"
    return state


# ── Direct probe end-to-end (real urllib + SSE parsing) ─────────────────
# These tests exercise _probe_unit_generation itself against an in-process fake
# HTTP server, so the ACTUAL streaming read / bounded-timeout / wedge-signature
# logic is verified — not just the monkeypatched entry point. No network leaves
# the process; no ~/.hscc is touched.

class _StreamingHandler(http.server.BaseHTTPRequestHandler):
    """Configurable fake vLLM /v1/chat/completions endpoint.

    ``mode`` (class attr): 'healthy' streams real tokens then [DONE];
    'wedged' returns HTTP 200 with Content-Type text/event-stream but never
    emits a token (the wedge signature); 'slow' stalls then finally streams.
    """

    mode = "healthy"

    def do_POST(self):
        body = {"id": "x", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]}
        chunk = ("data: %s\n\n" % json.dumps(body)).encode()
        done = b"data: [DONE]\n\n"
        if self.mode == "wedged":
            # 200 but never any data line — answers the handshake, no tokens.
            # Sleep well past the probe's outer timeout (1s) so the client's
            # bounded timeout is what fires, then return normally. Deliberately
            # NOT an infinite loop: a spinning handler thread would keep the
            # test process alive past teardown.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                import time as _t
                _t.sleep(30)
            except Exception:
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if self.mode == "slow":
                import time as _t
                _t.sleep(3)  # slow-but-eventually-successful: beyond 1s but
                             # within the probe's 10s outer bound
            self.wfile.write(chunk)
            self.wfile.flush()
            self.wfile.write(done)
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, format, *args):
        pass


class TestProbeEndToEnd:
    @pytest.fixture()
    def live_server(self):
        import threading as _t

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                 _StreamingHandler)
        # Handler threads must not keep the test process alive past teardown
        # (e.g. the wedged-mode handler sleeping out its stall).
        server.daemon_threads = True
        t = _t.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            yield server.server_address[1]  # port
        finally:
            server.shutdown()

    def test_healthy_stream_returns_text(self, live_server):
        _StreamingHandler.mode = "healthy"
        r = health._probe_unit_generation("127.0.0.1", live_server, timeout=10)
        assert r["ok"] is True
        assert r["text"] == "ok"
        assert r["status"] == 200

    def test_wedged_200_no_text_detected(self, live_server):
        _StreamingHandler.mode = "wedged"
        r = health._probe_unit_generation("127.0.0.1", live_server, timeout=1)
        assert r["ok"] is False
        assert r["status"] == 200
        assert r["error"] == "wedged"

    def test_slow_but_eventually_successful_ok(self, live_server):
        _StreamingHandler.mode = "slow"
        r = health._probe_unit_generation("127.0.0.1", live_server, timeout=10)
        assert r["ok"] is True
        assert r["text"] == "ok"

    def test_unreachable_is_down_not_wedge(self):
        # Nothing listening on an ephemeral port -> connection error -> "down"
        r = health._probe_unit_generation("127.0.0.1", 1, timeout=2)
        assert r["ok"] is False
        assert r["error"] == "down"
