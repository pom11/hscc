"""Unit tests for the live gateway bridge (hscc-api/gateway_bridge.py, t_29e033a4).

Covers the bridge's two responsibilities and, crucially, its HERMETIC
default: the mount functions must be inert (no serve, no driver, no subprocess,
no real-home probe) unless the serve sidecar is actually running — so the live
WS/history routes stay testable without (and never leak) a real serve.

Tests fake the process/socket boundaries (subprocess.Popen, _probe_tcp and the
GatewayDriver class) so nothing touches the operator's home or spawns a real
hermes serve. The exact mount/reconnect manager logic is asserted directly.
"""

import types

import pytest

import gateway_bridge as bridge
import routes_session
import routes_ws


@pytest.fixture(autouse=True)
def reset_bridge():
    bridge._reset_for_tests()
    yield
    bridge._reset_for_tests()


def _fake_driver_factory():
    """Return (factory, created_list) — factory() yields fake alive drivers.

    The fake drivers are instances of a real class so ``start`` can be patched
    on the class (monkeypatch.setattr) exactly like a real GatewayDriver.
    """
    created = []

    class _FakeDriver:
        def __init__(self, cfg=None, *args, **kwargs):
            self._alive = False  # mirrors real GatewayDriver: only True after start()
            self.config = cfg if cfg is not None else kwargs.get("config")
            self.stop = lambda: None
            created.append(self)

        def start(self):
            self._alive = True

    return _FakeDriver, created


# --------------------------------------------------------------------------- #
# Hermetic default: everything inert with no serve up
# --------------------------------------------------------------------------- #

def test_ensure_mounted_is_noop_without_serve(monkeypatch):
    """With no serve: ensure_mounted must not create a driver, not raise, and
    leave nothing mounted — even for a random project name."""
    monkeypatch.setattr(bridge, "GatewayDriver", lambda *a, **k: pytest.fail(
        "must not construct a driver with no serve up"))
    bridge.ensure_mounted("any_project")
    assert bridge._drivers == {}
    assert bridge.is_serve_up() is False
    assert bridge.mounted("any_project") is False


def test_shutdown_is_idempotent_and_safe_without_serve():
    """shutdown() with no serve / no drivers is a clean no-op (never raises)."""
    bridge.shutdown()
    bridge.shutdown()  # twice, still clean
    assert bridge.is_serve_up() is False


def test_serve_endpoint_none_without_serve():
    assert bridge.serve_endpoint() is None


def test_routes_import_and_mount_call_is_lazy():
    """The WS + history routes import gateway_bridge lazily; with no serve the
    call is a pure no-op (no driver constructed, no exception)."""
    assert hasattr(routes_ws, "ensure_mounted") is False  # not bound at module
    assert hasattr(routes_session, "ensure_mounted") is False


# --------------------------------------------------------------------------- #
# Serve sidecar gating
# --------------------------------------------------------------------------- #

def test_start_serve_requires_probe(monkeypatch):
    """start_serve returns False when the serve never accepts TCP (timed out).

    No subprocess is actually spawned in this branch because _probe_tcp is
    faked to always fail; a fake Popen stands in so nothing real is started.
    """
    calls = {"probe": 0, "terminated": False}

    def fake_probe(host, port, timeout=1.0):
        calls["probe"] += 1
        return False

    class _FakeProc:
        poll = lambda self: None
        terminate = lambda self: calls.__setitem__("terminated", True)
        returncode = None

    monkeypatch.setattr(bridge, "_probe_tcp", fake_probe)
    monkeypatch.setattr(bridge.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())
    monkeypatch.setattr(bridge, "_SERVE_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bridge, "_SERVE_PROBE_INTERVAL_S", 0.01)

    ok = bridge.start_serve(port=9123, bin_path="/bin/echo")
    assert ok is False
    assert bridge.is_serve_up() is False
    assert calls["probe"] > 0
    assert calls["terminated"] is True  # orphan serve was cleaned up


def test_start_serve_success_sets_up_and_token(monkeypatch):
    """start_serve returns True and records endpoint + a token when the probe
    succeeds; ensure_mounted then constructs + starts a driver."""

    def fake_probe(host, port, timeout=1.0):
        return True

    class _FakeProc:
        poll = lambda self: None
        terminate = lambda self: None
        wait = lambda self, timeout=1: None
        returncode = None
        pid = 999

    monkeypatch.setattr(bridge, "_probe_tcp", fake_probe)
    monkeypatch.setattr(bridge.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())

    ok = bridge.start_serve(port=9123, bin_path="/bin/echo")
    assert ok is True
    assert bridge.is_serve_up() is True
    assert bridge.serve_endpoint() == ("127.0.0.1", 9123)
    assert bridge._serve_token_value() is not None
    # The token is injected into the serve env.
    env = bridge._serve_env({})
    assert env.get("HERMES_DASHBOARD_SESSION_TOKEN") == bridge._serve_token_value()
    # A public_url override to a LOOPBACK host is forced into the serve env so
    # the real operator home's non-loopback dashboard.public_url does NOT gate
    # the serve (gated serves reject the driver's legacy ?token= auth with 403
    # — proven live on t_29e033a4). Loopback keeps it a pure bridge with no
    # wider network surface.
    assert env.get("HERMES_DASHBOARD_PUBLIC_URL") == "http://127.0.0.1:1"


def test_serve_env_forces_loopback_public_url():
    """A missing or non-loopback dashboard URL is overridden to the loopback
    sentinel (so the serve is NOT ticket-gated and the driver's ?token= auth
    is accepted); an explicitly-loopback URL is left as-is."""
    assert bridge._serve_env({}).get("HERMES_DASHBOARD_PUBLIC_URL") \
        == "http://127.0.0.1:1"
    assert bridge._serve_env(
        {"HERMES_DASHBOARD_PUBLIC_URL": "http://127.0.0.1:3000"}
    ).get("HERMES_DASHBOARD_PUBLIC_URL") == "http://127.0.0.1:3000"


# --------------------------------------------------------------------------- #
# Mount manager logic (driver construction/reuse/reconnect)
# --------------------------------------------------------------------------- #

def test_ensure_mounted_constructs_and_starts_driver(monkeypatch):
    """With the serve up, ensure_mounted builds a GatewayDriver with the serve
    endpoint + token and starts it (fake driver)."""
    monkeypatch.setattr(bridge, "_probe_tcp", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(
        bridge.subprocess, "Popen",
        lambda *a, **k: types.SimpleNamespace(
            poll=lambda: None, terminate=lambda: None,
            wait=lambda timeout=1: None, returncode=None, pid=100))

    captured = {}
    factory, created = _fake_driver_factory()
    monkeypatch.setattr(bridge, "GatewayDriver", factory)

    class _FakeDriver:
        pass

    seen = {"token": None}

    def _fake_start(driver_self):
        # The manager passed a real GatewayConfig with the serve token.
        seen["token"] = driver_self.config.token
        driver_self._alive = True

    monkeypatch.setattr(bridge.GatewayDriver, "start", _fake_start)

    bridge.start_serve(port=9119, bin_path="/bin/echo")
    bridge.ensure_mounted("hscc", registry_path="/tmp/reg.yaml")

    assert len(created) == 1
    d = created[0]
    assert d._alive is True
    assert bridge.mounted("hscc") is True
    # The manager passed the serve endpoint + token into the config.
    assert seen["token"] == bridge._serve_token_value()


def test_ensure_mounted_reuses_live_driver(monkeypatch):
    """A still-alive driver is reused, not reconstructed, on the next mount."""
    monkeypatch.setattr(bridge, "_probe_tcp", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(
        bridge.subprocess, "Popen",
        lambda *a, **k: types.SimpleNamespace(
            poll=lambda: None, terminate=lambda: None,
            wait=lambda timeout=1: None, returncode=None, pid=100))
    factory, created = _fake_driver_factory()
    monkeypatch.setattr(bridge, "GatewayDriver", factory)
    monkeypatch.setattr(
        bridge.GatewayDriver, "start",
        lambda driver_self: setattr(driver_self, "_alive", True))

    bridge.start_serve(port=9119, bin_path="/bin/echo")
    bridge.ensure_mounted("hscc")
    first = created[0]
    bridge.ensure_mounted("hscc")  # second mount
    assert len(created) == 1, "alive driver must be reused, not re-created"
    assert bridge.mounted("hscc") is True


def test_ensure_mounted_remounts_dead_driver(monkeypatch):
    """A driver that died is replaced on the next mount (reconnect semantics)."""
    monkeypatch.setattr(bridge, "_probe_tcp", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(
        bridge.subprocess, "Popen",
        lambda *a, **k: types.SimpleNamespace(
            poll=lambda: None, terminate=lambda: None,
            wait=lambda timeout=1: None, returncode=None, pid=100))
    factory, created = _fake_driver_factory()
    monkeypatch.setattr(bridge, "GatewayDriver", factory)
    monkeypatch.setattr(
        bridge.GatewayDriver, "start",
        lambda driver_self: setattr(driver_self, "_alive", True))

    bridge.start_serve(port=9119, bin_path="/bin/echo")
    bridge.ensure_mounted("hscc")
    assert len(created) == 1
    # Simulate the driver dying (threads exited, _alive False).
    created[0]._alive = False
    bridge.ensure_mounted("hscc")
    assert len(created) == 2, "a dead driver must be recreated on next mount"
    assert bridge.mounted("hscc") is True


def test_ensure_mounted_failed_start_is_fail_safe(monkeypatch):
    """A driver whose start() raises is NOT fatal: ensure_mounted returns
    quietly, no driver is marked mountable, and the sequence can retry."""
    monkeypatch.setattr(bridge, "_probe_tcp", lambda host, port, timeout=1.0: True)
    monkeypatch.setattr(
        bridge.subprocess, "Popen",
        lambda *a, **k: types.SimpleNamespace(
            poll=lambda: None, terminate=lambda: None,
            wait=lambda timeout=1: None, returncode=None, pid=100))
    factory, created = _fake_driver_factory()
    monkeypatch.setattr(bridge, "GatewayDriver", factory)

    def _boom(driver_self):
        raise ConnectionError("serve went away mid-mount")

    monkeypatch.setattr(bridge.GatewayDriver, "start", _boom)

    bridge.start_serve(port=9119, bin_path="/bin/echo")
    bridge.ensure_mounted("hscc")  # must not raise
    assert bridge.mounted("hscc") is False
