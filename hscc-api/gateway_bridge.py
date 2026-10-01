"""HSCC API — live gateway bridge (t_29e033a4).

Closes the LIVE DEPLOYMENT GAP in the session-continuity bridge: the live API
server previously NEVER mounted a ``GatewayDriver`` and no ``hermes serve``
ran on the driver's default port 9119 — so the app showed "connecting to live
stream" forever and the per-project store stayed empty even while the operator
actively streamed a project's CLI session.

This module owns the two pieces that were missing:

  1. Serve sidecar — a supervised ``hermes serve`` spawned as a child of the
     API process (via ``_serve`` in hscc_daemon/api_cli.py), on the documented
     port 9119 against the operator's REAL home/profile, killed on API
     shutdown. Smallest correct deployment: ``hscc api start`` gets you the
     whole thing, ``hscc api stop`` tears it down — no launchd plist, no
     separate management.

  2. Driver mounts — a lazily-mounted :class:`GatewayDriver` per project when
     the project's session is viewed/opened (WS connect from routes_ws, or a
     history read from routes_session). Mounting backfills the store from the
     project's named session history (design §3.2) and translates live events
     into store frames (§3.3), so the history endpoint AND the WS relay serve
     the backfilled + live events (§3.2/§3.3 — the API's acceptance gate).

HERMETIC BY DEFAULT: the module's functions are inert unless the serve sidecar
is actually running (``_serve_up`` True, set only by :func:`start_serve`). Unit
tests never spawn a serve, so :func:`ensure_mounted` is a no-op and the WS /
history routes behave exactly as before — no leaked subprocesses, no real-home
probes, no port guesses in the test suite.

Token handling: the serve's session token is minted here at runtime and passed
to the serve via ``HERMES_DASHBOARD_SESSION_TOKEN`` in its env (the seam
``hermes_cli.web_server._resolve_session_token`` reads). The SAME token is
passed to every driver's :class:`GatewayConfig` so a driver can authenticate to
``/api/pty`` + ``/api/events``. The token is kept only in memory, never logged
and never written to disk by this module.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from gateway_driver import GatewayConfig, GatewayDriver  # noqa: E402
from session_event import get_store  # noqa: E402

log = logging.getLogger("hscc-api.gateway-bridge")

# Documented default serve port (hermes serve --port default, and the port the
# gateway driver probes by default in GatewayConfig). Overridable via config.
DEFAULT_SERVE_PORT = 9119

# Seconds to wait for the serve to accept TCP before declaring startup failed.
_SERVE_READY_TIMEOUT_S = 20.0
# Seconds between TCP accept probes while health-checking the serve boot.
_SERVE_PROBE_INTERVAL_S = 0.5

# The serve binary (authoritative prior: the isolated probes ran
# ~/.hermes/hermes-agent/venv/bin/hermes serve ...). Overridable via config.
DEFAULT_SERVE_BIN = os.path.join(
    os.path.expanduser("~/.hermes/hermes-agent/venv/bin"), "hermes")


# --------------------------------------------------------------------------- #
# Module state (guarded by _LOCK)
# --------------------------------------------------------------------------- #

_LOCK = threading.Lock()          # guards all module state below
_serve_up = False                 # True once a serve is verified reachable
_serve_token: Optional[str] = None
_serve_proc: Optional[subprocess.Popen] = None
_serve_port: int = DEFAULT_SERVE_PORT
_serve_bin: str = DEFAULT_SERVE_BIN
_drivers: dict[str, GatewayDriver] = {}   # project -> mounted driver


def _reset_for_tests() -> None:
    """Drop all module state (test isolation only)."""
    global _serve_up, _serve_token, _serve_proc, _serve_port, _serve_bin
    global _drivers
    with _LOCK:
        _serve_up = False
        _serve_token = None
        _serve_proc = None
        _serve_port = DEFAULT_SERVE_PORT
        _serve_bin = DEFAULT_SERVE_BIN
        _drivers = {}


# --------------------------------------------------------------------------- #
# Serve sidecar (supervised child of the API process)
# --------------------------------------------------------------------------- #

def _probe_tcp(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if something accepts TCP on host:port (cheap reachability probe)."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _serve_env(env) -> dict:
    """Env for the spawned serve: inherit parent, add the session token.

    The token is the one secret a driver needs to reach ``/api/pty`` and
    ``/api/events``. It is minted here (or reused across drivers for the same
    serve) and passed ONLY via the subprocess env + the in-memory module state
    — never logged, never written to disk.
    """
    e = dict(env)
    if _serve_token:
        e["HERMES_DASHBOARD_SESSION_TOKEN"] = _serve_token
    return e


def start_serve(port: Optional[int] = None, bin_path: Optional[str] = None,
                serve_bin: Optional[str] = None) -> bool:
    """Spawn + health-check the supervised ``hermes serve`` sidecar.

    Returns True when the serve is up (newly started or already running),
    False when it could not be started. NEVER raises into the API startup —
    a serve that fails to boot must not stop the API from serving (the WS
    relay's ``_default_relay`` REST fallback stays authoritative until the
    serve is back).

    ``port`` / ``bin_path`` (alias ``serve_bin``) override the defaults
    (9119 and ``~/.hermes/hermes-agent/venv/bin/hermes``) — used by config
    and tests. ``--skip-build`` is always passed: these bridges need only the
    headless WS endpoints (``/api/pty``, ``/api/events``), never the web UI
    build, so we never require npm.
    """
    global _serve_up, _serve_token, _serve_proc, _serve_port, _serve_bin
    with _LOCK:
        if _serve_up:
            # Already running and verified — nothing to do.
            return True
        port = int(port) if port is not None else _serve_port
        # bin_path takes precedence; serve_bin is an alias for callers that
        # use the more descriptive name.
        binary = bin_path or serve_bin or _serve_bin
        _serve_port = port
        _serve_bin = binary
        if _serve_token is None:
            # Mint one token for this serve; shared by every driver that
            # connects to it (kept in memory only).
            _serve_token = secrets.token_urlsafe(32)

    binary = os.path.expanduser(binary)
    if not os.path.exists(binary):
        log.warning("gateway-bridge: serve binary not found at %s; serving "
                    "without the live gateway sidecar", binary)
        return False

    cmd = [binary, "serve", "--port", str(port), "--skip-build"]
    try:
        proc = subprocess.Popen(
            cmd, env=_serve_env(os.environ),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("gateway-bridge: failed to start hermes serve: %r", exc)
        return False

    # Health-check: wait for TCP accept on host:port (bounded).
    host = "127.0.0.1"
    deadline = time.time() + _SERVE_READY_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            log.warning("gateway-bridge: hermes serve exited early (rc=%s)",
                        proc.returncode)
            return False
        if _probe_tcp(host, port):
            break
        time.sleep(_SERVE_PROBE_INTERVAL_S)
    else:
        # Timed out waiting — terminate the orphan so we don't leak a process.
        try:
            proc.terminate()
        except OSError:
            pass
        log.warning("gateway-bridge: hermes serve did not accept on "
                    "127.0.0.1:%s within %ss", port, _SERVE_READY_TIMEOUT_S)
        return False

    with _LOCK:
        _serve_up = True
        _serve_proc = proc
    log.info("gateway-bridge: live hermes serve up on 127.0.0.1:%s (PID %s)",
             port, proc.pid)
    return True


def is_serve_up() -> bool:
    """True when the live gateway sidecar is verified reachable."""
    with _LOCK:
        return _serve_up


def serve_endpoint():
    """The (host, port) of the live sidecar, or None if not up."""
    with _LOCK:
        if not _serve_up:
            return None
        return ("127.0.0.1", _serve_port)


def _serve_token_value() -> Optional[str]:
    """The in-memory serve token (the driver secret), or None if not up."""
    with _LOCK:
        if not _serve_up:
            return None
        return _serve_token


# --------------------------------------------------------------------------- #
# Driver mounts (lazy per-project, gated on the serve being up)
# --------------------------------------------------------------------------- #

def ensure_mounted(project: str, registry_path: Optional[str] = None) -> None:
    """Mount (or remount) a GatewayDriver for ``project`` (idempotent).

    Called when a project's session is viewed/opened — the WS connect
    (routes_ws.handle_session_ws) and the history read
    (routes_session.handle_session_events). Gated: a no-op unless the live
    serve sidecar is up, so idle projects never spawn anything and unit tests
    (which never start a serve) are untouched.

    On mount the driver resolves the project's NAMED session (§3.1), backfills
    the store from its history (§3.2) and connects the live feed (§3.3), so
    the history endpoint and the app's WS relay serve the full conversation.

    Fail-safe: NEVER raises — a failed mount must not break the WS stream or
    the history read (the REST fallback relay remains authoritative). Logs the
    reason honestly.
    """
    if not is_serve_up():
        return
    endpoint = serve_endpoint()
    token = _serve_token_value()
    if endpoint is None or token is None:
        return

    with _LOCK:
        existing = _drivers.get(project)
        if existing is not None and existing._alive:
            # Still connected and streaming — reuse it (reconnect-safe: the
            # thread loops keep it alive; a dead one falls through to recreate).
            return
        if existing is not None:
            # Stale/dead driver from a previous mount — tear it down cleanly
            # before creating a fresh one (reconnect semantics). Best-effort.
            try:
                existing.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("gateway-bridge: stop stale driver %s: %r",
                          project, exc)

        host, port = endpoint
        cfg = GatewayConfig(
            host=host,
            port=port,
            token=token,
            project=project,
            registry_path=registry_path,
        )
        driver = GatewayDriver(cfg)
        _drivers[project] = driver

    try:
        driver.start()
    except Exception as exc:  # noqa: BLE001 — fail-safe: never break the caller
        # A connect that fails (e.g. the serve just died mid-mount) leaves the
        # driver recorded but not alive; the next ensure_mounted retries. The
        # history/WS stream continues on whatever it has (possibly empty until
        # the serve comes back). Honest log, not a silent swallow.
        log.warning("gateway-bridge: mount driver for %s failed: %r",
                    project, exc)
        return

    log.info("gateway-bridge: mounted live GatewayDriver for project %s",
             project)


def mounted(project: str) -> bool:
    """True if a live driver is currently mounted for ``project``."""
    with _LOCK:
        d = _drivers.get(project)
        return d is not None and d._alive


def shutdown() -> None:
    """Stop every mounted driver and the serve sidecar.

    Called on API teardown (``api_cli._serve`` finally after serve_forever) so
    no driver thread or serve subprocess outlives the API. Idempotent; never
    raises. Drivers are stopped first (each releases the serve's turn lease on
    its pinned session), then the serve process is terminated.
    """
    global _drivers
    with _LOCK:
        drivers = list(_drivers.values())
        _drivers = {}
        proc = _serve_proc
    for d in drivers:
        try:
            d.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway-bridge: stop driver during shutdown: %r", exc)
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError as exc:
            log.warning("gateway-bridge: kill serve during shutdown: %r", exc)
    with _LOCK:
        global _serve_up
        _serve_up = False
