"""HSCC HTTP API — server skeleton, token auth, bind/config resolution, and
the JSON error contract (Phase A1).

Pure stdlib only (``http.server`` / ``socketserver`` / ``json`` / ``secrets`` /
``hmac``) — no flask/fastapi/uvicorn. See docs/DESIGN-api.md for the full
contract; this module implements the A1 slice of it.

Design notes for the cards that follow (A2/A3/A4):
  * Register a new endpoint by ADDING a ``(method, path_regex, handler)``
    tuple to ``ROUTES``. Handlers are plain functions
    ``(server, ctx, query, body) -> (status, payload_dict)`` — you never edit
    the handler internals to add a route.
  * The ``ctx`` object passed to every handler holds the resolved config +
    auth token (captured once at server construction, so threads don't re-read
    disk). ``ctx`` is an instance of :class:`ApiContext`.
  * Raise :class:`ApiError` to emit a JSON error with the contract shape; the
    dispatcher maps it to the right HTTP status. Unhandled exceptions become a
    500 ``internal_error`` (traceback logged server-side only, never leaked).
"""

from __future__ import annotations

import hmac
import http.server
import ipaddress
import json
import logging
import os
import re
import sys
import secrets
import socket
import subprocess
from pathlib import Path

# Single source of truth for the plugin version (this module may be imported
# bare as `api_server` when the plugin dir is on sys.path — no relative import).
__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Constants / defaults (design §Hard constraints, §C config)
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8787
DEFAULT_BIND = "loopback"          # resolves to 127.0.0.1
LOOPBACK_IP = "127.0.0.1"
MAX_BODY_BYTES = 1 * 1024 * 1024   # 1 MiB request-body cap (design §C)
DEFAULT_HSCC_DIR = os.path.expanduser("~/.hscc")
CONFIG_FILE = "api.json"
TOKEN_FILE = "api-token"

# Tailscale is the App Store build here: the CLI is NOT on PATH, it lives at
# this absolute path. Probe that first, then fall back to a bare `tailscale`.
TAILSCALE_CLI_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "tailscale",
)

log = logging.getLogger("hscc-api")

# `python -m api_server serve` (the launchd unit's argv — see
# hscc_daemon/api_launchd.py) executes this file under the name `__main__`.
# Every routes_*.py at the bottom does `from api_server import ...`; without
# the alias below, Python does not find `api_server` in sys.modules (the file
# is registered only as `__main__`) and imports the SAME file a SECOND time as
# `api_server`. The routes then register into that second instance's ROUTES
# while the server serves from the `__main__` instance — which knows only the
# inline /v1/ping route. The result is an API that answers /v1/ping and 404s
# every /v1 route (t_37948685 e2e pins it). Aliasing before those imports run
# makes the module name resolve to the instance that is actually serving.
# Under a normal `import api_server` this block never runs (name != __main__) —
# zero change to every in-process caller, including the whole test suite.
if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    sys.modules.setdefault("api_server", sys.modules[__name__])


# ---------------------------------------------------------------------------
# Auth: token load/create, fail-closed (design §Hard constraints #3, §C)
# ---------------------------------------------------------------------------

def load_token(hscc_dir: str | os.PathLike | None = None) -> str:
    """Return the auth token, generating it on first run.

    Fail-closed: if the token file EXISTS but is unreadable, empty, or a
    permission error, raise — never fall back to "no auth", never silently
    regenerate (that would strand any existing client).

    Returns only the raw token string; callers must take care never to log it.
    """
    hscc_dir = hscc_dir or DEFAULT_HSCC_DIR
    token_path = Path(hscc_dir) / TOKEN_FILE
    if token_path.exists():
        try:
            token = token_path.read_text().strip()
        except OSError as exc:  # unreadable / permission denied
            raise RuntimeError(
                f"api-token exists but is unreadable ({exc!r}); refusing to "
                "start — fix permissions on {token_path}"
            ) from exc
        if not token:
            raise RuntimeError(
                f"api-token exists but is empty at {token_path}; refusing to "
                "start unauthenticated — remove the file to regenerate a new "
                "token, or restore the previous one"
            )
        return token

    # First run: generate and write with mode 0600, atomically (tmp +
    # os.replace so the file is never briefly world-readable).
    token = secrets.token_urlsafe(32)
    Path(hscc_dir).mkdir(parents=True, exist_ok=True)
    tmp = token_path.with_name(token_path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(token + "\n")
        os.replace(str(tmp), str(token_path))
    except BaseException:
        # Never leave a partial tmp behind.
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    return token


def token_valid(supplied, expected) -> bool:
    """Constant-time comparison of a supplied token against the expected one."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


# ---------------------------------------------------------------------------
# Autodown activity stamp (§1d.1) — authenticated requests reset the idle timer
# ---------------------------------------------------------------------------

def _do_stamp_http_activity():
    """Write the autodown HTTP-activity signal for the daemon to observe.

    The daemon's idle timer (hscc_daemon/autodown.py) polls this file each
    cycle and, on a newer timestamp, calls ``record_activity(\"http\")`` —
    resetting the idle window. Writing here is CPU-side and needs no model.

    The signal lives at ``~/.hscc/activity.json`` — OUTSIDE ``~/.hscc/state/``
    — because activity is EVENT-DRIVEN (stale by definition between requests
    and carries no ``ok`` key), so it must not sit in the daemon-streams dir
    that ``verify.py::check_daemon_streams`` requires to be fresh ok streams.
    Deliberately NOT written via ``state.write_state``: that funnel targets the
    periodic-streams dir. We compute the path at runtime through
    ``os.path.expanduser`` so the test isolation fixture redirects it to a tmp
    dir (the ``hscc_daemon`` import is deferred and needs the repo root on
    sys.path, which ``routes_cluster`` already installs at import, but we
    re-assert so this helper is robust standalone).
    """
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    import json
    import os
    # Not state.write_state — that writes into the periodic-streams dir. Runtime
    # expanduser so tests redirect it under the autouse _isolate_hscc fixture.
    path = os.path.expanduser("~/.hscc/activity.json")
    entry = {
        "timestamp": _now_utc_iso(),
        "stream": "activity",
        "source": "http",
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(entry, f, indent=2)
    except (OSError, IOError):
        # A stamp failure is swallowed upstream (defensive wrap) — but never
        # fabricate a signal. Log and drop.
        log.debug("autodown activity stamp failed (ignored)")
        return


def _now_utc_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stamp_http_activity():
    """Best-effort autodown activity stamp on an AUTHENTICATED request (§1d.1).

    Called by ``_route`` only AFTER ``_authorize()`` succeeds, so
    unauthenticated / failed-auth requests (which raise in ``_authorize``) never
    reach it — a port scanner cannot keep the cluster awake. Deliberately
    DEFENSIVE: a failure to stamp (import error, disk error, anything) is
    swallowed and must NEVER break the request being served.
    """
    try:
        _do_stamp_http_activity()
    except Exception:
        # A stamp failure must never take the API down or fail a request. The
        # only cost of a silent miss is the idle timer not resetting for this
        # one request — acceptable and self-healing on the next successful one.
        log.debug("autodown activity stamp failed (ignored)")
        return


# ---------------------------------------------------------------------------
# Bind / config resolution (design §Hard constraints #2, §C config)
# ---------------------------------------------------------------------------

# Bind values that are ALWAYS refused, whatever the source.
_REFUSED_BINDS = {"0.0.0.0", "0.0.0.0/0", "::", "::/0", ""}


def _read_config_file(hscc_dir: str | os.PathLike) -> dict:
    """Read ``~/.hscc/api.json`` if present; return {} otherwise.

    A malformed config file is a hard error (user asked for something and we
    can't honour it) — refuse to start rather than guess.
    """
    path = Path(hscc_dir) / CONFIG_FILE
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"cannot read api config at {path}: {exc!r}"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"api config at {path} must be a JSON object")
    return raw


def resolve_config(
    hscc_dir: str | os.PathLike | None = None,
    bind_override: str | None = None,
    port_override: int | None = None,
) -> dict:
    """Resolve (host, port) from defaults <- api.json <- explicit overrides.

    Lower-precedence values lose to higher ones (defaults are lowest, explicit
    overrides highest). Never binds ``0.0.0.0`` — that is a hard error.

    Returns a dict with keys ``host`` (resolved IP string) and ``port`` (int).
    """
    hscc_dir = hscc_dir or DEFAULT_HSCC_DIR
    cfg = _read_config_file(hscc_dir)

    bind_value = bind_override if bind_override is not None else cfg.get("bind", DEFAULT_BIND)
    host = resolve_bind(bind_value, hscc_dir)

    if host in _REFUSED_BINDS:
        raise RuntimeError(
            f"refusing to bind {host!r} — the HSCC API must never be reachable "
            "from an untrusted network. Set bind to 'loopback' or a specific "
            "tailnet/explicit IP."
        )

    port = port_override if port_override is not None else cfg.get("port", DEFAULT_PORT)
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid port {port!r} in api config") from exc
    if not (0 <= port <= 65535):
        raise RuntimeError(f"port {port} out of range 0-65535")

    return {"host": host, "port": port}


def resolve_bind(bind_value: str, hscc_dir: str | os.PathLike | None = None) -> str:
    """Resolve a bind config value to a concrete IPv4/IPv6 host string.

    Accepted values (design §C):
      * ``"loopback"``  -> 127.0.0.1 (the default)
      * ``"tailscale"`` -> the host's tailnet IPv4 (hard error if not found;
        never widens the bind)
      * an explicit IP string, used as-is — unless it is ``0.0.0.0`` / ``::``
        (always refused)

    Raises RuntimeError on any refusal, with a clear actionable message.
    """
    if bind_value == "loopback":
        return LOOPBACK_IP
    if bind_value == "tailscale":
        ip = _find_tailnet_ip()
        if ip is None:
            raise RuntimeError(
                "bind is 'tailscale' but no tailnet IP could be found. "
                "Install/enable Tailscale, or set bind to 'loopback' (or an "
                "explicit IP). Refusing to widen the bind to an "
                "unauthenticated/wide address."
            )
        return ip
    if bind_value in _REFUSED_BINDS:
        raise RuntimeError(
            f"refusing to bind {bind_value!r} — the HSCC API must never be "
            "reachable from an untrusted network."
        )
    # Explicit IP string: validate it is a real address.
    try:
        ipaddress.ip_address(bind_value)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid bind value {bind_value!r} — expected 'loopback', "
            "'tailscale', or an explicit IP address"
        ) from exc
    return bind_value


def _find_tailnet_ip() -> str | None:
    """Return the host's tailnet IPv4 (the 100.x range), or None.

    Probes in order:
      1. A running Tailscale CLI at the App Store build's absolute path, then
         a bare ``tailscale`` (in case the CLI is on PATH elsewhere).
      2. The host's network interfaces for a 100.x address.
    """
    for exe in TAILSCALE_CLI_PATHS:
        try:
            result = subprocess.run(
                [exe, "ip", "-4"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                ip = line.strip()
                if ip.startswith("100."):
                    return ip

    # Fall back to scanning the interfaces for a 100.x address.
    try:
        for line in _ifconfig_lines():
            ip = _extract_ip_from_ifconfig(line)
            if ip and ip.startswith("100."):
                return ip
    except Exception:  # defensively never crash on the fallback path
        return None
    return None


def _ifconfig_lines():
    """Yield stdout lines of ``ifconfig`` (best-effort; empty on failure)."""
    try:
        result = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            yield line


def _extract_ip_from_ifconfig(line: str) -> str | None:
    """Best-effort parse of an ``inet`` address from an ifconfig line."""
    m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", line)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# JSON error contract (design §C)
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """Raise to emit a JSON error with the unified contract shape.

    ``status``  -> HTTP status code.
    ``code``    -> machine-readable slug (e.g. ``unauthorized``).
    ``message`` -> one human sentence, safe to log / send to the client.
    ``speak``   -> TTS-safe one-liner (part of the error object, §B).
    """

    def __init__(self, status, code, message, speak=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.speak = speak if speak is not None else message

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "speak": self.speak,
            }
        }


# Standard errors (A1). A4 adds 409 confirm_required instances per endpoint.
def error_unauthorized(reason="missing or invalid bearer token"):
    return ApiError(401, "unauthorized", reason)


def error_not_found(reason="unknown route"):
    return ApiError(404, "not_found", reason)


def error_bad_request(reason="bad request"):
    return ApiError(400, "bad_request", reason)


def error_method_not_allowed(method):
    return ApiError(405, "method_not_allowed", f"method {method} not allowed")


def error_internal():
    return ApiError(
        500, "internal_error",
        "an unexpected error occurred — check ~/.hscc/api.log",
    )


# ---------------------------------------------------------------------------
# Route table (design §A) — A2/A3/A4 add their endpoints here
# ---------------------------------------------------------------------------

# Each route is (method, compiled path regex, handler).
# handler(server, ctx, query: dict, body: bytes) -> (status, payload_dict)
# A handler raises ApiError (or any exception -> 500) rather than returning one.
ROUTES = []


def handle_ping(server, ctx, query, body):
    """GET /v1/ping — the API's OWN liveness (NOT the fleet health check).

    The design reserves ``GET /v1/health`` for the fleet check backed by
    ``verify.run_all()`` (A2), so the API's own liveness lives at ``/v1/ping``
    to avoid the collision. Returns a small JSON object confirming the API is up.
    """
    return 200, {
        "ok": True,
        "service": "hscc-api",
        "version": __version__,
        "speak": "HSCC API is up.",
    }


ROUTES.append(("GET", re.compile(r"^/v1/ping$"), handle_ping))

# WebSocket route table (the live session stream). Each entry is
# (compiled path regex, ws_handler). ws_handler(server, ctx, query, handler)
# performs the RFC 6455 handshake against `handler` (the ApiHandler, which
# carries the request headers) then runs the WS session on handler.connection.
# Registered at import like ROUTES; see routes_ws.py.
WS_ROUTES = []


def register_ws_route(pattern, handler):
    """Append a WebSocket route to WS_ROUTES (module-level registration)."""
    WS_ROUTES.append((re.compile(pattern), handler))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ApiContext:
    """Resolved config + auth token captured once, before the server runs.

    Handlers receive this and never re-read disk for auth/port (design §C:
    the parent resolves these before forking so threads authorize fast).
    """

    def __init__(self, config: dict, token: str, hscc_dir: str):
        self.config = config
        self.token = token
        self.hscc_dir = hscc_dir


class _ApiServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server carrying shared ApiContext for handler threads."""

    daemon_threads = True

    def __init__(self, server_address, handler_cls, context: ApiContext):
        super().__init__(server_address, handler_cls)
        self.ctx = context

    def handle_error(self, request, client_address):
        """Do not print a stack trace when a client simply hung up.

        socketserver prints a full traceback for ANY exception escaping a
        handler, including the entirely routine case of the app backgrounding,
        the phone leaving wifi, or a socket being closed mid-response. The
        operator sees an alarming ConnectionResetError trace in the API log for
        something that is not a fault and needs no action.

        Genuine errors still print — this only filters the three disconnect
        exceptions, and logs a single quiet line so the event is not invisible.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError)):
            host = client_address[0] if client_address else "?"
            sys.stderr.write("client %s disconnected mid-request (%s)\n"
                             % (host, type(exc).__name__))
            return
        super().handle_error(request, client_address)


class ApiHandler(http.server.BaseHTTPRequestHandler):
    """Request handler: auth gate -> route dispatch -> envelope/error wrap."""

    server_version = "hscc-api"
    # HTTP/1.1 so keep-alive works and, vitally, so the WebSocket client
    # (websockets / iOS) accepts the response status line — the websockets
    # handshake parser rejects an "HTTP/1.0" reply, which BaseHTTPRequestHandler
    # emits by default. Our WS upgrade response already writes "HTTP/1.1 101".
    protocol_version = "HTTP/1.1"
    # Silence the default "logged to stderr" request line; we manage our own log.
    def log_message(self, format, *args):
        log.debug("(%s) %s", self.address_string(), format % args)

    @property
    def ctx(self) -> ApiContext:
        """The shared ApiContext carried by our server (see _ApiServer)."""
        return self.server.ctx  # type: ignore[attr-defined]

    # -- dispatch ---------------------------------------------------------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_PATCH(self):
        self._route("PATCH")

    def _route(self, method):
        try:
            self._authorize()
            # Authenticated ⇒ stamp autodown activity (§1d.1). Wrapped so a
            # stamp failure can NEVER break the request. Placed AFTER
            # _authorize() so unauthenticated requests (which raise above) do
            # not count — a port scanner can't keep the cluster awake.
            _stamp_http_activity()
            path, query = self._parse_path()
            # WebSocket upgrade (GET with Upgrade: websocket) — hand the raw
            # connection to the WS route, which owns the rest of the session.
            # No JSON body/response follows, so return after it completes.
            if method == "GET" and self._is_ws_upgrade():
                if self._dispatch_ws(path, query):
                    return
            body = self._read_body()
            status, payload = self._dispatch(method, path, query, body)
        except ApiError as exc:
            self._send_json(exc.status, exc.to_dict())
            return
        except Exception:  # never leak a traceback to the client
            log.exception("unhandled error handling %s %s", method, self.path)
            self._send_json(500, error_internal().to_dict())
            return
        self._send_json(status, payload)

    def _is_ws_upgrade(self) -> bool:
        """True when this GET requests an RFC 6455 WebSocket upgrade."""
        upgrade = (self.headers.get("Upgrade", "") or "").lower()
        connection = (self.headers.get("Connection", "") or "").lower()
        has_key = bool(self.headers.get("Sec-WebSocket-Key"))
        return upgrade == "websocket" and "upgrade" in connection and has_key

    def _dispatch_ws(self, path, query):
        """Run a matching WS route on this connection; False if path is unhandled.

        The full WS lifecycle runs inside the matched handler. On return the
        connection is closed (never keep-alive-reused after a protocol switch).
        """
        for pattern, handler in WS_ROUTES:
            m = pattern.match(path)
            if m:
                merged = dict(query)
                merged.update(
                    {k: v for k, v in m.groupdict().items() if v is not None}
                )
                handler(self.server, self.ctx, merged, self)
                return True
        # A WebSocket upgrade for a path we don't serve → reject before any
        # upgrade handshake, so the client sees a clean HTTP error.
        raise error_not_found("unknown websocket route")

    def _authorize(self):
        """Reject the request unless a valid Bearer token is supplied."""
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise error_unauthorized("missing bearer token")
        supplied = header[len("Bearer "):].strip()
        if not token_valid(supplied, self.ctx.token):
            raise error_unauthorized("invalid bearer token")
        return True

    def _parse_path(self):
        """Split the request target into (path, query-dict)."""
        raw = self.path
        path = raw
        query = {}
        if "?" in raw:
            path, qs = raw.split("?", 1)
            from urllib.parse import parse_qs
            query = {k: v[-1] for k, v in parse_qs(qs).items()}
        return path, query

    def _read_body(self) -> bytes:
        """Read the request body, enforcing the 1 MiB cap (400 when exceeded)."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise error_bad_request("invalid Content-Length")
        if length < 0:
            raise error_bad_request("invalid Content-Length")
        if length > MAX_BODY_BYTES:
            # Reject on the DECLARED length before reading the body (never
            # buffer unbounded input). We did not consume the body, so tell
            # the connection layer to close after we respond rather than
            # reusing a socket that still has unread bytes.
            self.close_connection = True
            raise ApiError(
                400, "bad_request", "request body too large",
                "Request body too large.",
            )
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _dispatch(self, method, path, query, body):
        """Match (method,path) against ROUTES and call the handler.

        Named path-parameter groups (``(?P<name>...)``) from the matched route
        are merged into the ``query`` dict handed to the handler, so endpoints
        with ``{card_id}``-style path segments receive them like any other
        query/filter parameter. Existing query keys are preserved; a matched
        group overrides only its own name (path params are more specific than
        the query string).
        """
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                merged = dict(query)
                merged.update(
                    {k: v for k, v in m.groupdict().items() if v is not None}
                )
                return handler(self.server, self.ctx, merged, body)
        # Path matched nothing. Unknown/unsupported -> method_not_allowed if the
        # path exists for another method, else not_found.
        for route_method, pattern, _handler in ROUTES:
            if pattern.match(path):
                raise error_method_not_allowed(method)
        raise error_not_found("unknown route")

    # -- response helpers ---------------------------------------------------

    def _send_json(self, status: int, payload: dict):
        # Binary escape hatch: a handler can serve raw bytes (not JSON) by
        # returning {"__raw_bytes__": <bytes>, "__raw_content_type__": str}.
        # Used by GET /v1/profile/export/{file} to download an exported
        # archive. Kept out of the JSON envelope entirely so the bytes are
        # delivered verbatim (no base64 inflation).
        raw = payload.pop("__raw_bytes__", None) if isinstance(payload, dict) else None
        if raw is not None:
            content_type = payload.pop("__raw_content_type__", "application/octet-stream")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ---------------------------------------------------------------------------
# Server orchestration (used by /v1/* tests and, later, the api_cli `start`)
# ---------------------------------------------------------------------------

def create_server(
    hscc_dir: str | os.PathLike | None = None,
    bind_override: str | None = None,
    port_override: int | None = None,
    addr: tuple | None = None,
):
    """Resolve config + token, then bind and return a running server.

    ``addr`` overrides the (host, port) computed from config — used by tests to
    bind loopback port 0 (ephemeral) for a hermetic, parallel-safe suite.
    """
    hscc_dir = hscc_dir or DEFAULT_HSCC_DIR
    config = resolve_config(hscc_dir, bind_override, port_override)
    token = load_token(hscc_dir)
    ctx = ApiContext(config, token, str(hscc_dir))

    if addr is None:
        addr = (config["host"], config["port"])
    server = _ApiServer(addr, ApiHandler, ctx)
    return server


# A2: load cluster + fleet read routes. routes_cluster.py's load() appends to
# ROUTES; imported last so ROUTES/ApiError exist before it runs.
from routes_cluster import load as _load_cluster_routes  # noqa: E402
_load_cluster_routes()

# A3: registering this module wires the project/kanban READ routes into ROUTES
# (its module-level `ROUTES.append(...)` calls run at import). Import must come
# after ROUTES is defined above.
import routes_project  # noqa: E402,F401  (registers /v1 project+kanban read routes)

# A4: the mutating, confirm-gated POST endpoints. Module-level ROUTES.append()
# calls register them at import. Imported last so ROUTES/ApiError exist first.
import routes_actions  # noqa: E402,F401  (registers /v1 mutating POST routes)

# C2: the conversational orchestrator-chat endpoint (confirm-gated mutation).
# Module-level ROUTES.append() registers it at import; the C1 resolver in
# hscc-roles/orchestrators.py is loaded via sys.path inside the module.
import routes_orchestrator  # noqa: E402,F401  (registers /v1/orchestrator/chat)

# t_69979dd1: expose the full HSCC surface. Each module registers its routes
# at import (module-level ROUTES.append). Imported last so ROUTES/ApiError
# exist first, matching the A3/A4/C2 pattern above.
import routes_autodown  # noqa: E402,F401  (registers /v1/autodown/*)
import routes_ops  # noqa: E402,F401  (registers /v1/{verify,daemon/status,triggers,escalate,profiles,cluster/up,cluster/down})
import routes_bootstrap  # noqa: E402,F401  (registers /v1/projects/new/{plan,})
import routes_kanban  # noqa: E402,F401  (registers /v1/kanban/{blocked,blocked/{id}/recover,stale})
import routes_template  # noqa: E402,F401  (registers /v1/template/{list,status,preview/{name}})
import routes_profile  # noqa: E402,F401  (registers /v1/profile/{list,install,export,export/{file}})
import routes_profiles  # noqa: E402,F401  (registers /v1/profiles/* lifecycle + config)
import routes_profile_editor  # noqa: E402,F401  (registers /v1/profile/editor/{profile} read + update)
import routes_sessions  # noqa: E402,F401  (registers /v1/sessions list + retire/compact)
import routes_memory  # noqa: E402,F401  (registers /v1/memory list + delete/edit)
import routes_activity  # noqa: E402,F401  (registers /v1/activity/feed — live agent activity feed)
import routes_commands  # noqa: E402,F401  (registers /v1/commands — slash-command catalog)
import routes_cron  # noqa: E402,F401  (registers /v1/cron/list — read-only scheduled-jobs roster)
import routes_logs  # noqa: E402,F401  (registers /v1/logs — bounded redacted log tail)
import routes_history  # noqa: E402,F401  (registers /v1/daemon/history — automated-action timeline)

# t_47f51a71: session-event wire contract — history read endpoint over the
# per-project chat event store (the bridge's live WS half appends to the same
# store). Module-level ROUTES.append registers it at import.
import routes_session  # noqa: E402,F401  (registers /v1/projects/{name}/session/events)

# t_47f51a71: the live WebSocket relay — upgrade to a WS stream over the same
# seq space as the history endpoint. Module-level register_ws_route() runs at
# import, after WS_ROUTES is defined above.
import routes_ws  # noqa: E402,F401  (registers /v1/projects/{name}/session/ws)


# ---------------------------------------------------------------------------
# Command-line entrypoint — `python -m api_server serve`
#
# Why this exists (t_37948685): this argv is what the launchd unit execs
# (hscc_daemon/api_launchd.build_api_plist writes it into ProgramArguments,
# with WorkingDirectory = this plugin dir). The module had no `__main__` block,
# so the command imported, registered its routes and EXITED 0 — no bind, no log
# line, nothing listening. Under `KeepAlive` launchd keeps relaunching a
# process that exits 0, so a boot-loaded unit would have looked healthy in
# `launchctl print` while the API stayed offline. `hscc api start` never hit
# this because it forks and calls create_server().serve_forever() in-process
# (api_cli._serve). tests/test_api_server_entrypoint.py runs THIS command as a
# subprocess and is the only place the plist text meets a listening socket.
#
# Scope, deliberately: NO PID file. The documented asymmetry ("`hscc api
# status` reads the PID file, so a launchd-run instance looks stopped there —
# trust launchctl") is kept on purpose. Writing api.pid here would make
# `hscc api stop` SIGTERM a launchd-supervised process, which KeepAlive then
# resurrects — a stop that silently un-stops is worse than the honest
# "looks stopped, trust launchctl" note. If the operator wants `status`/`stop`
# to work under launchd, that is a separate change to BOTH verbs.
# ---------------------------------------------------------------------------

def _parse_serve_argv(argv):
    """Parse the `serve` argv. Returns (subcommand, bind_override, port_override).

    Accepts the same flags `hscc api start` takes (api_cli._parse_start_flags),
    including `--no-qr`, which is accepted and ignored here: a launchd unit has
    no terminal to print a QR to, and accepting it means the operator can paste
    their existing `hscc api start --tailscale --no-qr` flag tail into the unit
    without the boot failing on an unknown argument. Unknown flags are a hard
    error (exit 2) — a hand-edited unit should fail loudly, not quietly serve
    something else.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m api_server",
        description="HSCC read-only HTTP API (the launchd unit runs 'serve').")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="bind and serve the API in the foreground")
    serve.add_argument("--tailscale", action="store_true",
                       help="bind the tailnet address instead of loopback")
    serve.add_argument("--bind", default=None, metavar="<ip>",
                       help="bind this specific address (0.0.0.0 is always refused)")
    serve.add_argument("--port", default=None, type=int, metavar="<n>",
                       help=f"override the configured port (default {DEFAULT_PORT})")
    serve.add_argument("--no-qr", action="store_true",
                       help="accepted for parity with 'hscc api start'; has no "
                            "effect when run under launchd")
    ns = parser.parse_args(argv)
    bind = "tailscale" if ns.tailscale else ns.bind
    return ns.cmd, bind, ns.port


def serve_main(bind_override=None, port_override=None) -> int:
    """Bind + serve in the foreground; returns a process exit code.

    Uses create_server() — the same resolution `hscc api start` runs, so the
    bind rules (loopback default, tailnet opt-in, 0.0.0.0 refused) and the
    fail-closed token load are not re-implemented here and cannot drift. Every
    startup failure exits NON-ZERO: that is what tells launchd the unit failed
    instead of letting an unbound process look like a served API.

    The live-gateway sidecar is started best-effort, exactly as api_cli._serve
    does (a serve that cannot boot must not stop the API — the WS relay's REST
    fallback stays authoritative), and torn down on exit so no supervised
    `hermes serve` child is orphaned holding port 9119 with this process's
    token (the stale-token defect gateway_bridge's port-occupancy heal exists
    to clean up).
    """
    logging.basicConfig(
        level=os.environ.get("HSCC_API_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        server = create_server(bind_override=bind_override,
                               port_override=port_override)
    except RuntimeError as exc:
        # Refused bind / unreadable-or-empty token / malformed api.json.
        print(f"hscc-api: refusing to start: {exc}", file=sys.stderr)
        return 1

    addr = server.ctx.config
    print(f"hscc-api: listening on {addr['host']}:{addr['port']}", flush=True)

    bridge = None
    try:
        import gateway_bridge
        bridge = gateway_bridge
        if not gateway_bridge.start_serve():
            log.warning("gateway sidecar NOT started (bridge inert; WS relay "
                        "REST fallback stays authoritative)")
    except Exception as exc:      # never let a sidecar failure stop the API
        log.warning("gateway sidecar startup skipped: %r", exc)
        bridge = None

    # launchd stops the unit with SIGTERM. Without a handler the default action
    # kills us before bridge.shutdown(), orphaning the supervised serve on
    # 9119 with our token — so the next boot pays the stale-token heal.
    import signal as _signal
    import threading as _threading

    def _on_term(signum, frame):
        # server.shutdown() blocks until serve_forever() returns, so it runs on
        # its own thread: calling it from the handler would deadlock against the
        # accept loop it is trying to stop.
        print(f"hscc-api: signal {signum}, shutting down", flush=True)
        _threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        _signal.signal(_signal.SIGTERM, _on_term)
        _signal.signal(_signal.SIGINT, _on_term)
    except (ValueError, OSError):
        pass                      # not the main thread / not installable

    try:
        server.serve_forever()
    except Exception as exc:
        print(f"hscc-api: crashed: {exc}", file=sys.stderr)
        return 1
    finally:
        if bridge is not None:
            try:
                bridge.shutdown()
            except Exception:
                pass
        print("hscc-api: stopped", flush=True)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # A hand-edited unit that lost its `serve` word would otherwise get
        # argparse's generic "arguments are required: cmd", which names nothing
        # the operator can act on. Say what the unit must run.
        print("hscc-api: nothing to do — the unit must run "
              "'python -m api_server serve' (see 'hscc api install-service')",
              file=sys.stderr)
        return 2
    cmd, bind_override, port_override = _parse_serve_argv(argv)
    if cmd != "serve":            # add_subparsers(required=True) keeps this defensive
        print(f"hscc-api: unknown command {cmd!r}", file=sys.stderr)
        return 2
    return serve_main(bind_override=bind_override, port_override=port_override)


if __name__ == "__main__":
    sys.exit(main())

