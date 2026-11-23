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

     The spawn goes through :mod:`gateway_serve_watchdog` (same dir), which is
     the piece that makes the sidecar die with an API that never got to run
     :func:`shutdown` — the SIGKILL escalation in ``hscc api stop``, a crash, or
     a blocked teardown. ``shutdown()`` alone was the only protection, and it is
     the door a killed parent cannot open (t_ed2f1abe).

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
import sys
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
# Per-project mount-failure backoff (iOS-chat-e2e B): after a failed native
# mount, the next attempt waits this long — one honest warning per window
# instead of one per WS connect (the 102-line log flood of 2026-10-13).
_MOUNT_RETRY_BACKOFF_S = 60.0
_MOUNT_FAIL_HW: dict = {}
# Seconds between TCP accept probes while health-checking the serve boot.
_SERVE_PROBE_INTERVAL_S = 0.5

# --- sidecar supervision (t_ed2f1abe) -------------------------------------- #
# ``hermes serve`` is spawned under gateway_serve_watchdog.py, which is what
# makes the sidecar die when this process dies by anything OTHER than a clean
# return through ``shutdown()`` — the SIGKILL escalation in ``hscc api stop``, a
# crash, or a teardown that blocks. macOS has no PR_SET_PDEATHSIG, so the
# watchdog's primary signal is a pipe: this process holds the write end, the
# watchdog holds the read end, and the kernel returns EOF to it the instant this
# process's fd table is destroyed (measured 0.8-1.0 ms; the reparenting-poll
# fallback it degrades to costs the poll interval instead — see the watchdog's
# module docstring for the measurement table).
_WATCHDOG_NAME = "gateway_serve_watchdog.py"
#: Env contract with the watchdog (read by it, popped before the serve spawns).
ENV_LIVENESS_FD = "HSCC_GATEWAY_LIVENESS_FD"
#: Seconds the API waits for the watchdog to finish tearing the serve down, on
#: both the clean stop and the failed-readiness path. MUST stay above the
#: watchdog's own TERM + KILL budget (2.0 s + 1.0 s): an API that stops waiting
#: too early kills the watchdog mid-teardown and rebuilds the orphan out of the
#: fix, which is the failure mode this whole module exists to close.
_SHUTDOWN_WAIT_S = 5.0

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
#: Write end of the parent-liveness pipe, held for the life of the process (and
#: across the serve's). Only this process may hold it — ``os.pipe()`` marks both
#: ends close-on-exec, so no descendant inherits it and an EOF at the watchdog
#: therefore means "this API process is gone", not "nobody in the tree wants
#: it". None when no supervised serve was ever spawned (or after a release).
_serve_liveness_write_fd: Optional[int] = None


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
        _MOUNT_FAIL_HW.clear()
    _release_liveness_write_fd()


def _release_liveness_write_fd() -> None:
    """Close the held write end, if any. Idempotent, never raises.

    Closing it is NOT a teardown mechanism (the watchdog would take that as
    "the parent is gone") — it is bookkeeping, so a reset or a re-spawn cannot
    leave a stale fd behind and let a dead serve's watchdog keep reading a pipe
    nobody owns. Callers must have already stopped the supervised process.
    """
    global _serve_liveness_write_fd
    with _LOCK:
        fd, _serve_liveness_write_fd = _serve_liveness_write_fd, None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


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


def _probe_tcp_occupied(port: int) -> bool:
    """Is 127.0.0.1:port already taken BEFORE we spawn our sidecar?

    Separate seam from the readiness probe _probe_tcp (which answers "did OUR
    child bind?"): occupancy is a different question, and tests fake them
    independently.
    """
    return _probe_tcp("127.0.0.1", port)


def _serve_accepts_token(port: int, token: Optional[str]) -> bool:
    """True when the serve on 127.0.0.1:port completes the /api/events WS
    upgrade for ``token`` — the SAME handshake the driver does.

    A real handshake is the only faithful probe: serve 401s a wrong/bad token
    and answers 101 for the right one (verified against live serve,
    iOS-chat-e2e 2026-10-13). No HTTP GET substitutes: /api/status is public
    (200 regardless of token), so it cannot distinguish.
    """
    if not token:
        return False
    from gateway_driver import GatewayConfig, _WSClient  # local: cycle guard
    ws = _WSClient("127.0.0.1", port,
                   GatewayConfig(token=token).ws_path("/api/events"))
    try:
        ws.connect()
        return True
    except Exception:  # noqa: BLE001 — any rejection/refusal = not ours
        return False
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def _listener_cmdline(port: int) -> Optional[str]:
    """Cmdline of the process LISTENING on 127.0.0.1:port, or None."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5).stdout
        pids = [p for p in out.split() if p.strip().isdigit()]
        if not pids:
            return None
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", pids[0]],
            capture_output=True, text=True, timeout=5).stdout
        return cmd.strip() or None
    except Exception:  # noqa: BLE001 — probe failure = unknown occupant
        return None


def _terminate_listener(port: int) -> bool:
    """Terminate the (already-cmdline-vetted) listener on ``port``.

    Callers MUST run the hermes-serve cmdline guard first — this function
    kills, so the guard is the blast-radius contract. Returns True once the
    port is actually free.
    """
    import signal
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5).stdout
        pid = next((int(p) for p in out.split() if p.strip().isdigit()), None)
        if pid is None:
            return True   # raced away — port already free
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _probe_tcp("127.0.0.1", port):
                return True
            time.sleep(0.2)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        return not _probe_tcp("127.0.0.1", port)
    except Exception as exc:  # noqa: BLE001
        log.warning("gateway-bridge: could not terminate listener on %s: %r",
                    port, exc)
        return False


def _serve_env(env) -> dict:
    """Env for the spawned serve: inherit parent, add the session token and
    force a LOOPBACK-only public URL.

    Two reasons both matter — confirmed empirically against the real operator
    home (which declares ``dashboard.public_url: http://10.0.0.x:3000``):

      * the session token: the one secret a driver needs to reach ``/api/pty``
        and ``/api/events`` (minted here, passed ONLY via subprocess env +
        in-memory module state — never logged, never written to disk);
      * ``HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:1``: the operator's
        REAL public_url makes the serve engage the TICKET-ONLY auth gate even
        on a loopback bind, which REJECTS the driver's legacy ``?token=``
        auth (the GatewayDriver only supports it — proven on the isolated
        probe, absent on the live deploy). Overriding the public_url to a
        loopback host keeps ``auth_required=False`` so the ``?token=`` path is
        accepted, while the serve itself stays bound to 127.0.0.1 with no
        wider network surface. It still runs on the REAL default home
        (``HERMES_HOME`` is not changed) so it resolves the project's real
        profile (``~/.hermes/profiles/<name>``) and its real sessions.
    """
    e = dict(env)
    if _serve_token:
        e["HERMES_DASHBOARD_SESSION_TOKEN"] = _serve_token
    # Loopback public URL -> non-gated serve (driver's ?token= auth accepted).
    # If the operator already set a loopback URL we leave it; else force ours.
    e.setdefault("HERMES_DASHBOARD_PUBLIC_URL", "http://127.0.0.1:1")
    return e


def _watchdog_script() -> Optional[str]:
    """Path of the parent-liveness watchdog, or None when it is not available.

    Seam, defaulting to the honest answer. When it returns None the serve is
    spawned DIRECTLY — the pre-t_ed2f1abe shape, with no parent-liveness
    protection — so a test that patches this to None pins the fallback, and the
    suite-wide conftest default keeps every other unit test off the real spawn.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        _WATCHDOG_NAME)
    return path if os.path.exists(path) else None


def _spawn_serve(cmd: list) -> subprocess.Popen:
    """Spawn ``cmd`` (the ``hermes serve`` argv) under the liveness watchdog.

    Returns the Popen of the process the API supervises — the WATCHDOG, not the
    serve, which is one level down with its own process group. Raises OSError if
    neither spawn works, which is how ``start_serve``'s existing failure path
    reports it.

    The liveness pipe: ``os.pipe()`` marks both ends close-on-exec, so the write
    end stays exclusively in this process (a ``Popen`` never hands it sideways)
    and the watchdog's read returns EOF exactly when this process's fd table is
    destroyed — SIGKILL, a crash, or an interpreter death all land there. The
    read end is what goes down via ``pass_fds``, which is also why its number
    survives the exec and can be named in the env.
    """
    watchdog = _watchdog_script()
    env = _serve_env(os.environ)
    if watchdog is None:
        log.warning("gateway-bridge: %s not available; spawning the serve "
                    "directly — it will NOT survive a kill that skips "
                    "shutdown()", _WATCHDOG_NAME)
        return subprocess.Popen(
            cmd, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)

    # Release any previous hold BEFORE the new pipe, so two write ends can never
    # be live at once (a stale one would keep a retired watchdog waiting).
    _release_liveness_write_fd()
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable or "python3", watchdog] + list(cmd),
            env=dict(env, **{ENV_LIVENESS_FD: str(read_fd)}),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(read_fd,))
    except OSError as exc:
        os.close(read_fd)
        os.close(write_fd)
        # A watchdog that cannot start must not cost the operator the sidecar
        # (the WS relay's REST fallback is the whole reason this is optional):
        # degrade to the direct spawn, loudly.
        log.warning("gateway-bridge: watchdog spawn failed (%r); spawning the "
                    "serve directly — it will NOT survive a kill that skips "
                    "shutdown()", exc)
        return subprocess.Popen(
            cmd, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    os.close(read_fd)                    # the parent keeps ONLY the write end
    global _serve_liveness_write_fd
    with _LOCK:
        _serve_liveness_write_fd = write_fd
    return proc


def _stop_supervised(proc: subprocess.Popen) -> None:
    """Stop the supervised process and wait for its teardown to finish.

    The wait is the point, and it must outlast the watchdog's own TERM+KILL
    budget (see ``_SHUTDOWN_WAIT_S``): SIGKILLing the watchdog while it is still
    waiting on the serve is how the orphan comes back through the fix.
    """
    try:
        proc.terminate()
    except OSError as exc:
        log.warning("gateway-bridge: could not signal the supervised serve: %r",
                    exc)
        return
    try:
        proc.wait(timeout=_SHUTDOWN_WAIT_S)
    except subprocess.TimeoutExpired:
        log.warning("gateway-bridge: supervised serve still up after %ss; "
                    "killing it", _SHUTDOWN_WAIT_S)
        try:
            proc.kill()
        except OSError:
            pass


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

    # Port-occupancy heal (iOS-chat-e2e 2026-10-13): the readiness probe below
    # cannot tell OUR serve from an ORPHANED one. A previous API instance
    # spawns serve with start_new_session=True, so when that API died (crash /
    # kill / restart) the serve survived, reparented to launchd, holding port
    # 9119 with the DEAD API's token. The old code probed TCP-accept, called
    # that "serve up", and every driver mount then 403'd forever (102 lines in
    # one operator session; the app read-path died with it — the tmux tail was
    # also gated on mount success, see the degraded-driver fix).
    # Contract: 127.0.0.1:9119 belongs to the API-managed sidecar. If the
    # occupant authenticates with OUR freshly-minted token it is adoptable
    # (same token in this process's env below); if not, it is stale — terminate
    # it (guard: a hermes serve process we can identify by cmdline) and spawn
    # our own. An occupant we refuse to kill → honest False, not a lie.
    if _probe_tcp_occupied(port):
        if _serve_accepts_token(port, _serve_token):
            log.info("gateway-bridge: adopting serve already listening on "
                     "127.0.0.1:%s (token matches)", port)
            with _LOCK:
                _serve_up = True
                _serve_proc = None      # adopted, not supervised by us
            return True
        victim = _listener_cmdline(port)
        if victim is None or "serve" not in victim or "hermes" not in victim:
            log.warning("gateway-bridge: port %s is held by a non-hermes "
                        "process (%r); not killing it, serving without the "
                        "live gateway sidecar", port, (victim or "")[:120])
            return False
        log.warning("gateway-bridge: orphaned serve (stale token) holds port "
                    "%s — terminating it to spawn a fresh one", port)
        if not _terminate_listener(port):
            return False
        # fall through: port is free, spawn below

    cmd = [binary, "serve", "--port", str(port), "--skip-build"]
    try:
        proc = _spawn_serve(cmd)
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
            _release_liveness_write_fd()
            return False
        if _probe_tcp(host, port):
            break
        time.sleep(_SERVE_PROBE_INTERVAL_S)
    else:
        # Timed out waiting — stop what we started so we don't leak a process,
        # and only then release the liveness fd (releasing it first would hand
        # the watchdog an EOF and start a second teardown racing this one).
        _stop_supervised(proc)
        _release_liveness_write_fd()
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

def _tmux_repl_present(project: str) -> bool:
    """True when a tmux pane provably runs ``project``'s orchestrator REPL.

    Injected seam for the degraded-mount decision (iOS-chat-e2e 2026-10-13):
    unit tests patch THIS so the bridge never shells out to a real tmux
    server. Lazy import of routes_ws avoids the module-level cycle (routes_ws
    imports gateway_bridge lazily too). Ambiguity counts as "present": the
    tail is keyed to the named SESSION, not to a pane, so an ambiguous pane
    set still streams the right session — the ambiguity only gates where to
    TYPE (routes_ws surfaces ``tmux_ambiguous`` there).
    """
    try:
        from routes_ws import _tmux_pane_for  # lazy: import cycle guard
        pane, ambiguous = _tmux_pane_for(project)
        return pane is not None or ambiguous is not None
    except Exception:  # noqa: BLE001 — discovery failure = absent
        return False


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

    No-serve case (iOS-chat-e2e 2026-10-13): when the sidecar is DOWN but the
    project's orchestrator REPL is provably in a tmux pane, the store-tail
    poller is still the app's only reply source (the tmux REPL writes replies
    straight to state.db), so a DEGRADED (tail-only) driver is mounted —
    native serve attach is skipped, the tail runs. Projects without a tmux
    REPL keep the old no-op (the REST fallback relay stays authoritative —
    mounting a dead driver would shadow it, see routes_ws._default_relay).
    """
    serve_up = is_serve_up()
    if not serve_up:
        if not _tmux_repl_present(project):
            return
        with _LOCK:
            existing = _drivers.get(project)
            if existing is not None and existing._alive:
                return   # degraded or otherwise live — already streaming the tail
            cfg = GatewayConfig(project=project, registry_path=registry_path,
                                degraded=True)
            driver = GatewayDriver(cfg)
            _drivers[project] = driver
        try:
            driver.start()
        except Exception as exc:  # noqa: BLE001 — fail-safe
            log.warning("gateway-bridge: degraded mount for %s failed: %r",
                        project, exc)
            return
        log.info("gateway-bridge: mounted DEGRADED (tail-only) driver for %s "
                 "(serve down; REPL is in tmux — tail is the reply source)",
                 project)
        return

    endpoint = serve_endpoint()
    token = _serve_token_value()
    if endpoint is None or token is None:
        return

    with _LOCK:
        existing = _drivers.get(project)
        if existing is not None and existing._alive and existing.native_attached():
            # Still connected and streaming NATIVELY — reuse it (reconnect-safe:
            # the thread loops keep it alive; a dead one falls through to
            # recreate). A DEGRADED (tail-only) driver does NOT count once the
            # serve is back: fall through and remount with native attach.
            return
        # Mount-failure backoff (iOS-chat-e2e B): a serve that is *up* but
        # refuses the mount (the 403 shape) used to be retried on EVERY WS
        # connect/history read — 102 identical warning lines in one operator
        # session. Remount attempts per project are now rate-limited; healthy
        # mounts clear the record so a recovery is immediate next time.
        last_fail = _MOUNT_FAIL_HW.get(project, 0.0)
        if time.time() - last_fail < _MOUNT_RETRY_BACKOFF_S:
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
        # Legacy path: a start() that raises (pre-2.7.1 drivers / injected
        # test doubles) leaves the driver recorded but not alive. Rate-limit
        # the next attempt; the stream continues on whatever it has.
        log.warning("gateway-bridge: mount driver for %s failed: %r",
                    project, exc)
        with _LOCK:
            _MOUNT_FAIL_HW[project] = time.time()
        return

    if driver._alive and not driver.native_attached() and not cfg.degraded:
        # serve was up but the ATTACH failed (403 / stale-token shape): the
        # driver is alive tail-only. start() logged it once; here we only
        # rate-limit the next remount attempt so a broken serve costs one
        # warning per backoff window per project, not one per WS connect.
        with _LOCK:
            _MOUNT_FAIL_HW[project] = time.time()
        return

    with _LOCK:
        _MOUNT_FAIL_HW.pop(project, None)   # healthy — next recovery is instant
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
    its pinned session), then the supervised process is stopped and waited for.

    This is the COOPERATIVE door. It is not the guarantee: a teardown that never
    runs (the SIGKILL escalation in ``hscc api stop``, a crash, a blocked
    ``finally:``) is covered by gateway_serve_watchdog.py instead, which notices
    this process is gone and takes the serve with it.
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
        _stop_supervised(proc)
    _release_liveness_write_fd()
    with _LOCK:
        global _serve_up
        _serve_up = False
