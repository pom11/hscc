"""HSCC `api` CLI verb — start / stop / status for the HSCC HTTP API server.

Thin lifecycle wrapper (Phase A5). It reuses the EXISTING daemon PID/log
conventions from ``daemon_ops`` (parameterized to the API's own ``api.pid`` /
``api.log`` files) and calls into A1's bind/config/token resolution in
``hscc-api/api_server.py`` — it does NOT re-implement or weaken the bind rules
(loopback default, tailnet opt-in, NEVER 0.0.0.0). All HTTP handling lives in
the server module; this file only manages the background process lifecycle.

Reference: docs/DESIGN-api.md §C (Operational shape, `hscc api` verb group).
"""

import os
import signal
import sys
import time
from pathlib import Path

from hscc_daemon import daemon_ops
from hscc_daemon import qr_code

# The API server is a SEPARATE background process from the monitoring daemon,
# so it gets its own PID + log files (distinct from daemon.pid / daemon.log).
API_PID_FILE = os.path.expanduser("~/.hscc/api.pid")
API_LOG_FILE = os.path.expanduser("~/.hscc/api.log")

VALID_SUBCOMMANDS = ("start", "stop", "status")

HELP_TEXT = """\
HSCC API server — HTTP API for external apps (e.g. the iOS client).

Usage: hscc api <subcommand> [args]

  hscc api start [--tailscale] [--bind <ip>] [--port <n>] [--no-qr]   Start the API server in the background
  hscc api stop                                                       Stop the running API server
  hscc api status [--no-qr]                                           Show running/stopped + bound host:port
  hscc api --help                                                     This help

Bind defaults to loopback (127.0.0.1). '--tailscale' or '--bind <ip>' opt in
to exposing it on the tailnet / a specific IP. 0.0.0.0 is always refused.
The auth token lives at ~/.hscc/api-token and is ENCODED into the QR, so the
QR must be treated like a password — do not show it on a stream or
screen-share.
'--no-qr' suppresses the scannable connection QR shown by start/status."""


def _resolve_api_dir() -> Path:
    """Locate the hscc-api plugin dir (a sibling of hscc_daemon)."""
    return Path(__file__).resolve().parent.parent / "hscc-api"


def _load_api_server():
    """Import the A1 server module from the hscc-api plugin dir.

    The server imports ``routes_cluster`` / ``routes_project`` by bare name,
    so the plugin dir must be on ``sys.path`` (we put it there once, mirroring
    how ``_handle_project`` inserts ``hscc-project/``). Returns the loaded
    module; raises RuntimeError with a clear message if the plugin is missing.
    """
    api_dir = _resolve_api_dir()
    if not api_dir.is_dir():
        raise RuntimeError(f"hscc-api plugin not found at {api_dir}")
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))
    try:
        import api_server  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            f"could not import hscc-api server from {api_dir}: {exc}"
        ) from exc
    return api_server


def _has_flag(argv, name):
    """True if ``name`` appears in ``argv`` (a bare boolean flag)."""
    return name in argv


def _is_loopback(host) -> bool:
    """True if ``host`` is a loopback address a phone cannot reach."""
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return host.startswith("127.") or host == "[::1]"


def _build_qr_payload(host, port, token) -> str:
    """Connection-settings JSON for the QR.

    Single line, NO trailing newline, and key order fixed (v, host, port,
    token) — the iOS scanner depends on this byte-for-byte. No spaces after
    the colons. ``token`` is the real live credential read from the token
    file; the caller prints it ONLY to stdout and never to a log/error.
    """
    return '{"v":1,"host":"%s","port":%d,"token":"%s"}' % (host, port, token)


def _print_api_qr(host, port, token, *, force_ascii=False):
    """Print the scannable connection-settings QR with a security warning.

    The warning is printed unconditionally (it accompanies the QR itself, not
    the flag state): whoever scans this QR is handed a live credential. When
    the bind is loopback-only we additionally warn that a phone cannot reach
    it. `--no-qr` suppresses this entire block at the call site.
    """
    payload = _build_qr_payload(host, port, token)
    matrix = qr_code.make_qr(payload.encode("utf-8"))
    print("  Scan to connect: grants API access to this host.")
    if _is_loopback(host):
        print("  Warning: bound to loopback — a phone cannot reach this host.")
    print(qr_code.render_text(matrix, force_ascii=force_ascii))
    print()


def _parse_start_flags(argv):
    """Parse ``start`` flags into (bind_override, port_override, no_qr).

    Supports ``--tailscale`` (→ bind 'tailscale'), ``--bind <ip>``, ``--port
    <n>``, and ``--no-qr`` (suppress the QR), matching the design §C config
    precedence (flags are the highest precedence). Unknown flags are ignored.
    """
    bind_override = None
    port_override = None
    no_qr = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--tailscale":
            bind_override = "tailscale"
        elif arg == "--no-qr":
            no_qr = True
        elif arg == "--bind":
            if i + 1 < len(argv):
                bind_override = argv[i + 1]
                i += 1
        elif arg == "--port":
            if i + 1 < len(argv):
                try:
                    port_override = int(argv[i + 1])
                except ValueError:
                    pass
                i += 1
        i += 1
    return bind_override, port_override, no_qr


def _serve(api, bind_override, port_override):
    """Run the API server loop in the foreground (grandchild of `start`).

    Resolves config + token again here (the child has the same disk state) via
    A1's ``create_server`` — which honors the bind rules and fails closed on an
    unreadable/empty token. Cleans up the PID file on exit.
    """
    try:
        server = api.create_server(
            bind_override=bind_override,
            port_override=port_override,
        )
    except RuntimeError as exc:
        daemon_ops.log(
            f"HSCC API start failed: {exc}", "ERROR",
            log_file=API_LOG_FILE, pid_file=API_PID_FILE,
        )
        daemon_ops.write_stopped(API_PID_FILE)
        os._exit(1)

    addr = server.ctx.config
    daemon_ops.log(
        f"HSCC API listening on {addr['host']}:{addr['port']}",
        log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    try:
        server.serve_forever()
    except Exception as exc:
        daemon_ops.log(
            f"HSCC API crashed: {exc}", "ERROR",
            log_file=API_LOG_FILE, pid_file=API_PID_FILE,
        )
    finally:
        daemon_ops.write_stopped(API_PID_FILE)


def _sigterm_handler(signum, frame):
    """Graceful shutdown on SIGTERM/SIGINT: remove PID file, exit."""
    daemon_ops.log(
        f"Received signal {signum}, shutting down...",
        log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    daemon_ops.write_stopped(API_PID_FILE)
    os._exit(0)


def _handle_start(argv):
    """`hscc api start` — resolve bind/config + token, fork into background.

    Returns 0 on start (or already-running), non-zero on a fail-closed error
    (refused bind, empty/unreadable token, plugin missing).
    """
    api = _load_api_server()
    bind_override, port_override, no_qr = _parse_start_flags(argv)

    existing = daemon_ops.get_pid(API_PID_FILE)
    if existing:
        print(f"HSCC API already running (PID {existing})")
        return 0

    # Fail-closed BEFORE forking: resolve bind/port + validate/generate the
    # token. We never fork a child that would then fail on disk state we can
    # check now, and we never bind 0.0.0.0 (A1's resolve_config refuses it).
    try:
        config = api.resolve_config(
            bind_override=bind_override,
            port_override=port_override,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        token_server = api.load_token()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Starting HSCC API on {config['host']}:{config['port']}...")

    # Print the scannable connection QR unless the user suppressed it. We do
    # this only after config+token are known to be valid, so the user gets a
    # useful QR of the endpoint they are about to start. The token in the QR
    # is the real live credential printed only to stdout, never logged.
    if not no_qr:
        _print_api_qr(config["host"], config["port"], token_server)

    daemon_ops.log(
        "HSCC API starting", log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )

    # Fork into the background using the same double-fork pattern as the daemon
    # (cli.cmd_start): fork, setsid, re-fork, write PID, run the serve loop.
    pid = os.fork()
    if pid > 0:
        # Parent: record the child PID immediately (grandchild re-saves its own).
        try:
            daemon_ops.save_pid(API_PID_FILE)
            print(f"HSCC API started (PID {pid})")
        except Exception:
            print(f"HSCC API started (child PID {pid})")
        return 0

    # Child — become a daemon, no controlling terminal.
    os.setsid()
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
    os.chdir(os.path.expanduser("~"))
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — write the real PID and run the serve loop.
    daemon_ops.save_pid(API_PID_FILE)
    _serve(api, bind_override, port_override)
    os._exit(0)


def _handle_stop(argv):
    """`hscc api stop` — read PID, SIGTERM, wait, remove PID file."""
    pid = daemon_ops.get_pid(API_PID_FILE)
    if not pid:
        print("HSCC API is not running")
        daemon_ops.write_stopped(API_PID_FILE)
        return 0

    print(f"Stopping HSCC API (PID {pid})...")
    daemon_ops.log(
        "HSCC API stop requested", log_file=API_LOG_FILE, pid_file=API_PID_FILE,
    )
    try:
        os.kill(pid, signal.SIGTERM)
        for _i in range(10):
            time.sleep(1)
            try:
                os.kill(pid, 0)
            except OSError:
                print(f"HSCC API stopped (PID {pid})")
                return 0
        os.kill(pid, signal.SIGKILL)
        print(f"HSCC API force-killed (PID {pid})")
    except ProcessLookupError:
        print("HSCC API already stopped")
    except Exception as exc:
        print(f"Error stopping HSCC API: {exc}")
        return 1
    finally:
        daemon_ops.write_stopped(API_PID_FILE)
    return 0


def _handle_status(argv):
    """`hscc api status` — report running/stopped + the bound host:port.

    NEVER prints the auth token. Uses A1's resolve_config only to report the
    configured bind address (loopback default honored); does not bind anything.
    Prints a scannable connection QR (suppressed by ``--no-qr``) when a token
    exists — if the token is missing/unreadable, says so and exits cleanly.
    """
    no_qr = _has_flag(argv, "--no-qr")

    api = None
    try:
        api = _load_api_server()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    host = port = None
    try:
        cfg = api.resolve_config()
        host, port = cfg["host"], cfg["port"]
    except RuntimeError as exc:
        # Config issues shouldn't stop the status report entirely.
        print(f"Warning: could not resolve bind address: {exc}", file=sys.stderr)

    pid = daemon_ops.get_pid(API_PID_FILE)
    if pid:
        print(f"HSCC API is running (PID {pid})")
    elif os.path.exists(API_PID_FILE):
        print("HSCC API status: stale PID file (not running)")
    else:
        print("HSCC API status: not running")

    if host is not None:
        print(f"Listening:     {host}:{port}")

    # The QR is only useful when there is an endpoint AND a valid token. If
    # the token is missing/unreadable, say so and exit cleanly (no QR). The
    # token is passed straight into the QR payload (stdout only, never logged).
    if not no_qr and host is not None:
        try:
            token = api.load_token()
        except RuntimeError as exc:
            print(f"No connection QR: could not read auth token ({exc})")
        else:
            _print_api_qr(host, port, token)
    return 0


def cmd_api(argv):
    """Dispatch `hscc api <subcommand>`; returns an exit code (never raises).

    With no subcommand (or ``--help``/``-h``) prints the group help and exits 0
    — matching how the other group verbs handle their no-subcommand/``--help``
    case. Unknown subcommands exit non-zero.
    """
    if not argv or argv[0] in ("--help", "-h"):
        print(HELP_TEXT)
        return 0

    sub = argv[0]
    rest = argv[1:]
    if sub == "start":
        return _handle_start(rest)
    if sub == "stop":
        return _handle_stop(rest)
    if sub == "status":
        return _handle_status(rest)

    print(f"Error: unknown api subcommand: {sub}")
    print(f"Valid subcommands: {', '.join(VALID_SUBCOMMANDS)}")
    return 1
