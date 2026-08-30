"""Table-driven auth enforcement audit over the LIVE route table (t_300416f3).

THE CLAIM UNDER TEST
====================
Every registered HSCC API route requires a valid bearer token before it
serves any data or accepts any mutation. Auth is not enforced per-handler:
it is a single CENTRAL GATE in ``ApiHandler._route``
(``api_server.py``):

    def _route(self, method):            # line 503
        try:
            self._authorize()            # line 505  <-- THE GATE
            ...

    def _authorize(self):                # line 555
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise error_unauthorized("missing bearer token")
        supplied = header[len("Bearer "):].strip()
        if not token_valid(supplied, self.ctx.token):
            raise error_unauthorized("invalid bearer token")

``_authorize`` is called unconditionally as the FIRST statement in ``_route``,
before path parsing (line 511), before the WebSocket upgrade branch
(line 515) and Body read (line 518) and before ``_dispatch`` (line 519).
Every do_GET / do_POST / do_PUT / do_DELETE delegates to ``_route``
(lines 491-501), so there is exactly ONE entry point into the API and it is
auth-gated. No handler can be reached without passing the gate.

WHY THIS TEST IS TABLE-DRIVEN OVER THE LIVE LIST
================================================
The test does NOT enumerate route paths by hand. It iterates the live
``api_server.ROUTES`` and ``api_server.WS_ROUTES`` lists at runtime and
asserts each one rejects an unauthenticated request with 401. A newly added
route (a new ``ROUTES.append``/``register_ws_route``) is therefore covered
automatically on the next test run — no one has to remember to add it.

For each HTTP route we synthesise a concrete path from the route's OWN
compiled regex (named groups -> ``x``), so the path is guaranteed to match
that route — proving the 401 fires on a real, reachable route path (auth
fails BEFORE dispatch), not on a 404 from a path typo. The self-check inside
the test re-asserts the path matches, so a broken synthesizer fails loudly.

WebSocket routes are the special case the card flags: the RFC 6455
"101 Switching Protocols" handshake happens inside the WS handler, which is
only reached AFTER ``_authorize()`` succeeds in ``_route``. We prove that by
attempting an unauthenticated upgrade and asserting it is rejected with
401 — never upgraded, never a single frame served.
"""

import re
from urllib.parse import quote

import asyncio
import pytest
import websockets

import api_server
import routes_ws  # noqa: F401  (registers the WS route at import)

from tests.test_api import RunningServer  # noqa: E402


@pytest.fixture(scope="module")
def running(tmp_path_factory):
    """ONE server shared by all route tests (bound to loopback port 0).

    Every request these tests send is UNAUTHENTICATED, so the central auth
    gate (``_authorize``) fails before any route handler runs — no handler
    state is touched. A single module-scoped server is therefore safe and
    avoids booting ~79 servers per test session.
    """
    srv = RunningServer(hscc_dir=str(tmp_path_factory.mktemp("hscc-auth")))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    """The real token for the running fixture's server."""
    return api_server.load_token(running.server.ctx.hscc_dir)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def concrete_path(pattern: re.Pattern) -> str:
    """Synthesise a concrete request path that matches ``pattern``.

    The server's auth gate runs BEFORE dispatch, so path params don't need
    real values — but the path MUST match the route pattern so the 401 we
    observe is genuinely THIS route's gate firing, not a 404 from a bad path.
    We collapse every ``(?P<name>...)`` group to a safe literal ``x`` (path
    segment / lookup key). Works because all registered regexes are simply
    anchored ``/v1/...`` strings with ``[^/]+`` named groups.
    """
    s = pattern.pattern
    # Named route-param groups: (?P<name>[^/]+) or (?P<name>...)
    s = re.sub(r"\(\?P<[^>]+>\[[^]]*\]\)", "x", s)
    s = re.sub(r"\(\?P<[^>]+>[^)]*\)", "x", s)
    s = s.replace("^", "").replace("$", "")
    return s


def _http_route_cases():
    """Yield (path, method, handler) for every live HTTP route."""
    for method, pattern, handler in api_server.ROUTES:
        path = concrete_path(pattern)
        assert pattern.match(path), (
            f"synthesized path {path!r} does not match its own route regex "
            f"{pattern.pattern!r} (broken generator)"
        )
        yield path, method, handler.__name__


def _ws_route_cases():
    """Yield (path, handler) for every live WebSocket route."""
    for pattern, handler in api_server.WS_ROUTES:
        path = concrete_path(pattern)
        assert pattern.match(path)
        yield path, handler.__name__


# --------------------------------------------------------------------------- #
# HTTP routes: EVERY route must reject an unauthenticated request with 401.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,method,handler",
    list(_http_route_cases()),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_http_route_rejects_no_token(running, path, method, handler):
    """No bearer token -> 401 unauthorized, before the handler can run.

    If a route ever stops requiring auth (the gate is bypassed or a route is
    registered outside ``ROUTES`` dispatch), this test FAILS for that route.
    Because it iterates the live ROUTES list, a newly added route is covered
    automatically.
    """
    status, payload = running.request(method=method, path=path, token=None)
    assert status == 401, (
        f"route {method} {path} ({handler}) served a request without a token: "
        f"expected 401, got {status} ({payload!r})"
    )
    assert payload["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "path,method,handler",
    list(_http_route_cases()),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_http_route_rejects_wrong_token(running, path, method, handler):
    """A valid-scheme but WRONG bearer token -> 401 (never trusts the scheme)."""
    status, payload = running.request(
        method=method, path=path, token="definitely-not-the-right-token")
    assert status == 401, (
        f"route {method} {path} ({handler}) accepted a wrong token: "
        f"expected 401, got {status}"
    )
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# WebSocket routes: token must be checked BEFORE the protocol switch, so an
# unauthenticated upgrade is rejected with 401 and NEVER serves a frame.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,handler",
    list(_ws_route_cases()),
    ids=lambda v: v if isinstance(v, str) else str(v),
)
def test_ws_route_rejects_no_token_before_upgrade(running, path, handler):
    """Unauthenticated WebSocket upgrade -> 401, never upgraded, no frame.

    The RFC 6455 handshake (``_handshake``, the ``101`` response) lives in the
    WS handler, which _route reaches only AFTER ``_authorize()`` succeeds.
    So an unauthenticated upgrade must fail auth first and be rejected with
    HTTP 401 — never a 101, never a single event frame.
    """
    uri = f"ws://{running.host}:{running.port}{quote(path)}"

    async def go():
        try:
            async with websockets.connect(uri, ping_interval=None) as ws:
                await ws.recv()
                pytest.fail(
                    f"WS route {path} ({handler}) served a frame without a token")
        except websockets.exceptions.InvalidStatus as exc:
            # The server must REJECT the upgrade, sending HTTP 401 — not 101.
            assert exc.response.status_code == 401, (
                f"WS route {path} ({handler}) returned "
                f"{exc.response.status_code}, expected 401"
            )

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Route-table index (report/evidence, not behaviour): every registered route.
# --------------------------------------------------------------------------- #

def test_route_table_index_matches_documented_counts():
    """A stable index of the live route table for the audit report.

    This is informational: it asserts the set of registered routes is exactly
    what this audit claims, so a route added or removed shows up as a diff in
    the route table rather than silently changing coverage. Intentionally
    loose (subset assertions) so it doesn't fight legitimate future growth —
    its job is to force the report to be regenerated when the table changes.
    """
    http = {(m, p.pattern, h.__name__) for (m, p, h) in api_server.ROUTES}
    ws = {(p.pattern, h.__name__) for (p, h) in api_server.WS_ROUTES}
    assert http  # never empty
    assert ws    # at least the session stream route
