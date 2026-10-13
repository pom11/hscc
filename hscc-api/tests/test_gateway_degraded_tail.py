"""Regression tests for the DEGRADED (tail-only) GatewayDriver (iOS-chat-e2e).

THE BUG these pin (2026-10-13, operator evidence): every ``hermes serve``
mount 403'd (stale-token orphan serve), ``GatewayDriver.start`` raised on the
mount, the driver died BEFORE the store-tail thread started, and NOTHING the
operator's terminal REPL wrote ever reached the store or a connected app WS —
a total read-path outage while the write path (tmux) worked.

Contract now:
  * start() never raises; a mount failure leaves the driver alive DEGRADED
    (tail-only) with the store-tail poller running;
  * a new assistant row in state.db reaches the store and a connected WS
    client within ~one poll interval, even though the mount 403'd;
  * native_attached() is False while degraded (so the bridge re-mounts a
    native driver once the serve is truly back).

Hermetic: real temp state.db + real WS server on loopback; the serve mount
fails for real (nothing listens on port 1) — no tmux, no serve, no real home.
"""
import asyncio
import json
import socket
import time
import types

import pytest
import websockets

import api_server
import gateway_driver as gd
import routes_ws  # noqa: F401  (registers the WS route at import)
from gateway_driver import GatewayConfig, GatewayDriver
from session_event import get_store, reset_stores
from tests.test_api import RunningServer

try:
    from hermes_state import SessionDB  # noqa: F401
    _HAS_HERMES_STATE = True
except Exception:
    _HAS_HERMES_STATE = False


@pytest.fixture
def hs_state():
    if not _HAS_HERMES_STATE:
        pytest.skip("hermes_state not importable on this interpreter "
                    "(needs the hermes-agent runtime / hermes venv)")
    yield


@pytest.fixture(autouse=True)
def clean_state():
    reset_stores()
    gd.reset_backfill_state()
    gd.reset_tail_state()
    yield
    reset_stores()
    gd.reset_backfill_state()
    gd.reset_tail_state()


# --------------------------------------------------------------------------- #
# state.db fixtures (same seam pattern as test_gateway_store_tail.py)
# --------------------------------------------------------------------------- #

def _registry(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        "projects:\n"
        "  - name: hscc\n"
        "    repo: /tmp/hscc\n"
        "    board: hscc\n")
    return str(reg)


def _seed(state_path, session_id, messages):
    from hermes_state import SessionDB
    db = SessionDB(db_path=state_path)
    try:
        db.create_session(session_id, source="cli", model="m",
                          profile_name="hscc-orch")
        db.set_session_title(session_id, "hscc")
        if messages:
            db.append_messages_batch(session_id, messages)
    finally:
        db.close()


def _append(state_path, session_id, messages):
    from hermes_state import SessionDB
    db = SessionDB(db_path=state_path)
    try:
        db.append_messages_batch(session_id, messages)
    finally:
        db.close()


def _wire_seams(monkeypatch, state_path):
    import routes_orchestrator
    monkeypatch.setattr(
        routes_orchestrator, "_open_profile_session_db",
        lambda profile, read_only=False: SessionDB(db_path=state_path))


def _wait_until(pred, timeout=8.0, msg="condition"):
    """Poll until ``pred()`` or fail with ``msg`` (real timing, no mocks)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {msg}")


SID = "20261013_degraded_aaa"


def _degraded_driver(monkeypatch, tmp_path, fast_poll=True):
    """A driver whose serve mount REALLY fails (port 1: connection refused),
    over a temp state.db with 1 user message. Returns (driver, state_path)."""
    reg = _registry(tmp_path)
    state = tmp_path / "state.db"
    _seed(state, SID, [
        {"role": "user", "content": "from terminal", "timestamp": 1759200000},
    ])
    _wire_seams(monkeypatch, state)
    if fast_poll:
        monkeypatch.setattr(gd, "_TAIL_POLL_INTERVAL_S", 0.05)
    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t",
                        project="hscc", registry_path=reg)
    drv = GatewayDriver(cfg)
    drv.start()          # must NOT raise despite the refused mount
    return drv, state, reg


# --------------------------------------------------------------------------- #
# start() survives a failed mount and the tail keeps streaming
# --------------------------------------------------------------------------- #

def test_start_survives_failed_mount_and_stays_alive_degraded(monkeypatch,
                                                               tmp_path,
                                                               hs_state):
    """The 403-equivalent (connection refused) no longer kills the driver:
    _alive True, sockets None, native_attached False, and start() returned."""
    drv, _state, _reg = _degraded_driver(monkeypatch, tmp_path)
    try:
        assert drv._alive is True
        assert drv._pty is None and drv._events is None
        assert drv.native_attached() is False
        # Threads: events/pty loops exit immediately on None sockets; the
        # tail loop is the one that must be running.
        tail = [t for t in drv._threads if t.name == "gateway-store-tail"]
        assert len(tail) == 1 and tail[0].is_alive()
    finally:
        drv.stop()
    assert drv._alive is False


def test_degraded_tail_delivers_assistant_reply_to_store(monkeypatch, tmp_path,
                                                          hs_state):
    """The reported symptom's server half: mount fails, THEN the terminal REPL
    writes an assistant reply to state.db — it must land in the store."""
    drv, state, _reg = _degraded_driver(monkeypatch, tmp_path)
    try:
        store = get_store("hscc")
        _wait_until(lambda: store.next_seq >= 2, msg="backfill of seed row")

        # The operator's REPL answers (exactly like the "received" reply).
        _append(state, SID, [
            {"role": "assistant", "content": "received",
             "timestamp": time.time()},
        ])
        _wait_until(lambda: store.next_seq >= 3, msg="assistant reply in store")

        evs = store.history()["events"]
        asst = [e for e in evs if e["type"] == "message"
                and e["payload"].get("role") == "assistant"]
        assert asst and asst[-1]["payload"]["delta"] == "received"
        assert asst[-1]["payload"]["done"] is True
    finally:
        drv.stop()


def test_degraded_tail_reaches_connected_ws_client(monkeypatch, tmp_path,
                                                   hs_state):
    """Full read path with the mount broken: a WS client connected exactly
    like the app RECEIVES the assistant frame the tail translated."""
    # ensure_mounted must stay inert in this test (no tmux in CI) — the
    # driver under test is constructed directly.
    import gateway_bridge as bridge
    monkeypatch.setattr(bridge, "ensure_mounted", lambda *a, **k: None)
    # Registry + busy-owner seams faked exactly like test_ws_route so the WS
    # route never reads the real operator registry.
    fake_registry = types.SimpleNamespace(
        get_project=lambda name, path=None: types.SimpleNamespace(name=name),
        ensure_session=lambda name, path=None: name,
        ProjectNotFoundError=type("ProjectNotFoundError", (Exception,), {}),
    )
    monkeypatch.setattr(routes_ws, "_registry", fake_registry)
    monkeypatch.setattr(
        routes_ws, "_registry_path", lambda ctx: "/tmp/fake-registry.yaml")
    import routes_orchestrator as _ro
    monkeypatch.setattr(
        _ro, "detect_active_cli_owner", lambda project, registry_path=None: None)

    srv = RunningServer(hscc_dir=str(tmp_path / "hsccdir"))
    drv = None
    try:
        token = api_server.load_token(srv.server.ctx.hscc_dir)
        drv, state, _reg = _degraded_driver(monkeypatch, tmp_path)
        store = get_store("hscc")
        _wait_until(lambda: store.next_seq >= 2, msg="backfill")

        async def go():
            headers = {"Authorization": f"Bearer {token}"}
            async with websockets.connect(
                    f"ws://{srv.host}:{srv.port}/v1/projects/hscc/session/ws",
                    additional_headers=headers, ping_interval=None) as ws:
                hello = json.loads(await ws.recv())
                assert hello["type"] == "hello"
                # The seed user row replays first.
                first = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert first["payload"]["delta"] == "from terminal"
                # NOW the REPL answers; the frame must arrive live.
                _append(state, SID, [
                    {"role": "assistant", "content": "received",
                     "timestamp": time.time()},
                ])
                while True:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if (ev["type"] == "message"
                            and ev["payload"].get("role") == "assistant"):
                        assert ev["payload"]["delta"] == "received"
                        return
        asyncio.run(go())
    finally:
        if drv is not None:
            drv.stop()
        srv.close()
