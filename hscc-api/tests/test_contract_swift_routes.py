"""Contract tests: every route the iOS app calls, with the app's exact payloads.

This is the drift-proof gate for the ios-app <-> hscc-api seam. The route list
is derived FROM the Swift client (HSCCClient.swift) at import time, so a new or
renamed route in the app is caught here the moment this suite runs — it cannot
drift silently the way last night's chat route/flag mismatch did.

What it asserts:
  * Every (method, path) the Swift client calls is registered in
    ``api_server.ROUTES`` with a matching method and a path regex the app's
    path actually matches. A route the app calls that the server doesn't
    register (e.g. a `/v1/projects/{name}/chat` that 404s) fails here.
  * Every mutating POST the app sends carries ``confirm: true`` — the exact
    flag whose absence caused last night's 409-on-the-operator's-phone. If the
    app ever drops the confirm flag, or the server ever starts requiring it on
    a route the app still sends confirm-less, this fails.
  * The critical chat seam is exercised over real HTTP with the app's exact
    payload and the status codes the app relies on.

Hermetic: the conftest redirects ~/.hscc off the live home dir; heavy backing
seams are monkeypatched (same style as the rest of the suite). Never touches
live operator state.
"""

import ast
import http.client
import inspect
import json
import re

import pytest

import api_server
import routes_orchestrator


# --------------------------------------------------------------------------- #
# Derive the route list FROM the Swift client (the anti-drift source of truth)
# --------------------------------------------------------------------------- #

CLIENT_PATH = "ios-app/Sources/HSCC/HSCCClient.swift"
# Repo root is three levels up from this test file: tests/ -> hscc-api/ -> root.
_REPO_ROOT = str(__file__.rsplit("/", 3)[0])

# A Swift call site: `get("/v1/...")`, `post("/v1/...")`, or the generic
# `read("/v1/...")` (GET). The literal string may contain Swift interpolation
# and `\.` escapes, which we normalize below.
_CALL_RE = re.compile(r'\b(get|post|read)\(\s*"((?:[^"\\]|\\.)*)"', re.S)
# The LABELED form the query-string GETs use: `get(path: "/v1/...", queryItems:)`.
# The direct `_CALL_RE` above requires a literal immediately after `get(` —
# it does NOT match `get(path: "<lit>", ...)` because `path:` sits between the
# paren and the literal. That silent gap dropped EVERY query-param route
# (fleet/stats, sessions, memory, kanban/stale, activity/feed, project
# session/events) from the derived route set — which is precisely how the
# `?profile=\(encoded)` escaped-backslash defect on /v1/sessions and /v1/memory
# sailed past `test_every_swift_route_is_registered`: those routes were never
# in SWIFT_ROUTES to be checked. Match the labeled form too so the register
# gate (and the parameter gate below) actually covers them.
_LABELED_CALL_RE = re.compile(
    r'\b(get|post|read)\(\s*path:\s*"((?:[^"\\]|\\.)*)"', re.S)
# Swift `\( ... )` interpolation -> `{param}` placeholder.
_INTERP_RE = re.compile(r'\\\(.*?\)')


def _normalize_swift_path(lit):
    """Turn a Swift string literal (with interpolation/escapes) into a
    canonical path with ``{param}`` placeholders for interpolated segments."""
    out = _INTERP_RE.sub("{param}", lit)
    # Swift escapes: `\.` -> `.`, `\/` -> `/`. (In the source these are written
    # as `\.` because the raw literal uses a doubled backslash in the file.)
    out = out.replace("\\.", ".")
    return out


def _swift_routes():
    """Parse HSCCClient.swift and return the ordered unique list of
    ``(method, normalized_path)`` the client calls. Deriving here — not
    hand-maintaining a table — is what makes the gate cannot-drift.

    Iterates BOTH the direct-literal form (`get("/v1/x")`) and the labeled
    query-string form (`get(path: "/v1/x", queryItems:...)`), so no client
    call is silently dropped from the derived set.
    """
    src = open(f"{_REPO_ROOT}/{CLIENT_PATH}").read()
    seen = set()
    routes = []
    for regex in (_CALL_RE, _LABELED_CALL_RE):
        for m in regex.finditer(src):
            method = m.group(1).upper()
            method = "GET" if method == "READ" else method
            path = _normalize_swift_path(m.group(2))
            if (method, path) not in seen:
                seen.add((method, path))
                routes.append((method, path))
    return routes


SWIFT_ROUTES = _swift_routes()


def _instantiate(path):
    """Fill ``{param}`` placeholders with a concrete, safe value so the path
    can be matched against a compiled route regex and issued over HTTP."""
    return re.sub(r"\{param\}", "x", path)


def _path_only(path):
    """The server's dispatcher strips the query string before matching the
    route regex (api_server._parse_path: `path, qs = raw.split("?", 1)`). Mirror
    that so `/v1/fleet/stats?days=7` maps to the registered `^/v1/fleet/stats$`."""
    return path.split("?", 1)[0]


# --------------------------------------------------------------------------- #
# The drift gate — every app route is registered on the server
# --------------------------------------------------------------------------- #

def _registered():
    """Build {(method, compiled-pattern)} from api_server.ROUTES."""
    return [(m, p) for (m, p, _h) in api_server.ROUTES]


def test_every_swift_route_is_registered():
    """The heart of the gate: each (method, path) the app calls must have a
    registered route that its concrete path matches. Mirrors the real
    dispatcher (ApiHandler._dispatch: first matching (method, regex))."""
    registered = _registered()
    unresolved = []
    for method, path in SWIFT_ROUTES:
        concrete = _path_only(_instantiate(path))
        found = None
        for (rm, rp) in registered:
            if rm == method and rp.match(concrete):
                found = rp.pattern
                break
        if found is None:
            unresolved.append((method, path, concrete))
    assert not unresolved, (
        "Route(s) the iOS app calls are NOT registered on the server:\n"
        + "\n".join(f"  {m} {p}  (concrete: {c})" for m, p, c in unresolved)
    )


def test_every_mutating_post_carries_confirm():
    """Last night's root cause: a mutating POST that reached the phone without
    `confirm` and 409'd. Every mutating call the Swift client makes must send
    `confirm: true`. We assert by scanning the enclosing function of each POST
    call site for a `"confirm": true` payload key (set either in a `payload`
    dict built before the call, or inline in the call args)."""
    src = open(f"{_REPO_ROOT}/{CLIENT_PATH}").read()
    offending = []
    for m in _CALL_RE.finditer(src):
        if m.group(1).upper() != "POST":
            continue
        # The enclosing function body: back to the nearest `func ` boundary,
        # forward to the next `func ` boundary (each endpoint is one function
        # with exactly one POST call) — so a `"confirm": true` anywhere in the
        # endpoint's function counts, whether set in a payload dict before the
        # call or inline in the call's own `body:` argument.
        fn_start = src.rfind("\n    func ", 0, m.start()) + 1
        fn_end = src.find("\n    func ", m.end())
        if fn_end == -1:
            fn_end = len(src)
        body = src[fn_start:fn_end]
        if re.search(r'"confirm"\s*:\s*true', body) is None:
            offending.append(_normalize_swift_path(m.group(2)))
    assert not offending, (
        "Mutating POST(s) in HSCCClient.swift omit `\"confirm\": true` — a "
        "409-on-the-phone waiting to happen:\n"
        + "\n".join(f"  POST {p}" for p in offending)
    )


# --------------------------------------------------------------------------- #
# Bidirectional cross-check: the server must ACTUALLY gate each client POST
# --------------------------------------------------------------------------- #
#
# The client-side test above proves the app always SENDS `confirm: true`. That
# is only half the contract. The other, worse half: a mutating endpoint the
# client calls whose server handler never checks `confirm` is an UNGATED
# mutation — the app's confirm UI is theatre and any caller (not just this app)
# can mutate without confirmation. So for every POST route the Swift client
# derives, resolve the server handler via `api_server.ROUTES` and prove it
# really calls a confirm-gating helper (`_require_confirm(...)` or the
# `_action_fields(...)` preamble that wraps `_require_confirm`).
#
# A regression in EITHER direction fails the suite:
#   * client drops confirm while the server still requires it -> existing
#     `test_every_mutating_post_carries_confirm` fails;
#   * server stops checking confirm on a route the client POSTs -> the new
#     test below fails.

# Confirm-gating helpers, by name. `_action_fields` always calls
# `_require_confirm` first (routes_actions.py:181) before validating fields, so
# an actions handler that delegates to it IS confirm-gated.
_CONFIRM_HELPER_CALL = re.compile(r"\b_(?:require_confirm|action_fields)\s*\(")


def _strip_handler_docstring(src):
    """Remove the handler's module docstring statement so a confirm helper ONLY
    mentioned in prose (never actually called) can't pass the gate."""
    try:
        tree = ast.parse(src)
        fn = tree.body[0] if tree.body else None
        if (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)):
            lines = src.split("\n")
            del lines[fn.body[0].lineno - 1: fn.body[0].end_lineno]
            return "\n".join(lines)
    except (SyntaxError, IndexError):
        pass
    return src


def _server_handler_for(method, path):
    """Resolve a client route to its registered server handler, or None."""
    concrete = _path_only(_instantiate(path))
    for (rm, rp, handler) in api_server.ROUTES:
        if rm == method and rp.match(concrete):
            return handler, rp.pattern
    return None, None


def test_every_client_post_server_handler_is_confirm_gated():
    """The reverse half of the confirm contract. For each POST route the Swift
    client calls, the server handler must actually call a confirm-gating helper
    (`_require_confirm(...)` or `_action_fields(...)`, which wraps it). If a
    server handler stops checking confirm on a route the app POSTs to, the
    mutation becomes ungated — worse than a 409 — and this must fail the suite.
    Checks the CALL SITE, not prose, by stripping the handler docstring first."""
    ungated = []
    not_registered = []
    for method, path in SWIFT_ROUTES:
        if method != "POST":
            continue
        handler, pattern = _server_handler_for(method, path)
        if handler is None:
            not_registered.append(path)
            continue
        src = _strip_handler_docstring(inspect.getsource(handler))
        if _CONFIRM_HELPER_CALL.search(src) is None:
            ungated.append((path, handler.__name__, pattern))
    assert not not_registered, (
        "Client POST route(s) have no registered server route:\\n"
        + "\\n".join(f"  POST {p}" for p in not_registered)
    )
    assert not ungated, (
        "Client POST route(s) map to a server handler that does NOT call a "
        "confirm-gating helper (_require_confirm / _action_fields) — an "
        "UNGATED mutation. The app sends `confirm: true` but the server never "
        "checks it:\\n"
        + "\\n".join(f"  POST {p} -> {h}  (registered {rp})"
                     for p, h, rp in ungated)
    )



# Real-HTTP exercise of the chat seam (the exact failure class from last night)
# --------------------------------------------------------------------------- #
#
# Bidirectional query-parameter contract: every route the client calls must
# send, as a real URLQueryItem, every query parameter its server handler
# REQUIRES (i.e. one whose absence raises a 400 "missing required ... query
# param"). This is the exact defect class on this card: /v1/sessions and
# /v1/memory both require `profile` and return 400 without it, yet the client
# used to send the literal `?profile=\(encoded)` — an escaped backslash that
# URLComponents percent-encoded into the path, so the server never saw the
# query and the filter silently never applied. That call COMPILED and all
# tests passed because these two routes were not even in SWIFT_ROUTES.
#
# The matrix below is the audit deliverable: every row cites the server
# handler (file:line) that REQUIRES the param and the client method (file:line)
# that must transmit it. Each required param is asserted to be sent as a real
# `URLQueryItem` in the client's function for that route. A regression in
# either direction — the server starts requiring a query param the client
# doesn't send, or the client stops routing a required param through
# URLQueryItem (falling back to a path/`?`-literal that never reaches the
# handler) — fails the suite.
#
# (GET routes only: POST routes carry their params in the JSON body, covered
# by the confirm + register gates above.)
#
#   client route                      required query param   server handler (requires it)
#   --------------------------------  ---------------------  --------------------------------------------
#   GET /v1/sessions                  profile                routes_sessions.py:200-202 (400 without it)
#   GET /v1/memory                    profile                routes_memory.py:252-255   (400 without it)
#
# Query params the server treats as OPTIONAL (days, older_than, limit, before)
# are not listed here — the client transmitting them is a real behavior (the
# responses differ), but their absence is not a 400. They are still exercised
# by the derived-route register gate above.
REQUIRED_QUERY_PARAMS = {
    ("GET", "/v1/sessions"): ["profile"],
    ("GET", "/v1/memory"): ["profile"],
}


def _client_query_params_for(method, path):
    """Parse the enclosing Swift function of a call site and return the set of
    query param NAMES the client transmits via `URLQueryItem(name: "...")`.

    Checks only the function body that owns the call so we don't mistake an
    unrelated route's params for this one. Returns an empty set when the route
    uses the direct-literal form (no query params).
    """
    src = open(f"{_REPO_ROOT}/{CLIENT_PATH}").read()
    # Find the call site in EITHER form (direct literal or labeled `path:`).
    for regex in (_CALL_RE, _LABELED_CALL_RE):
        for m in regex.finditer(src):
            if (_normalize_swift_path(m.group(2)) == path
                    and m.group(1).upper().replace("READ", "GET") == method):
                fn_start = src.rfind("\n    func ", 0, m.start()) + 1
                fn_end = src.find("\n    func ", m.end())
                if fn_end == -1:
                    fn_end = len(src)
                body = src[fn_start:fn_end]
                return set(re.findall(
                    r'URLQueryItem\(name:\s*"([^"]+)"', body))
    return set()


def test_every_required_query_param_is_sent():
    """The parameter matrix gate. For every route whose server handler REQUIRES
    a query param (400 without it), the client must transmit that param as a
    real `URLQueryItem` in the route's own function — never as a path-embedded
    `?x=` literal (which URLComponents percent-encodes to %3F and hides from
    the handler) and never omitted (which would make the call always 400)."""
    failures = []
    for (method, path), required in REQUIRED_QUERY_PARAMS.items():
        sent = _client_query_params_for(method, path)
        for param in required:
            if param not in sent:
                failures.append((path, param, sent))
    assert not failures, (
        "Client fails to transmit a server-REQUIRED query param as a real "
        "URLQueryItem — the route would always 400 (or, if embedded in the "
        "path via `?x=`, silently no-op like the escaped-backslash profile "
        "bug):\n"
        + "\n".join(
            f"  {p} requires query param '{param}' but the client sends "
            f"queryItems={sorted(sent)}"
            for p, param, sent in failures
        )
    )


# Required BODY fields (the same defect class, one layer down).
# This card's brief is "every call sends the parameters its handler requires"
# and names BOTH halves — "query params, body fields". The matrix above covers
# query params (400 on a GET without them). POSTs carry their params in the
# JSON body, and several handlers 400 on a missing/non-empty body field
# (routes_actions._action_fields) — a client that omits one makes that call
# all-ways fail, exactly the class of silent-regex-drop the labeled-query bug
# hid. So mirror the query gate for the body fields the handlers REQUIRES.
#
#   client route                     required body fields   server handler (requires it)
#   -------------------------------  ---------------------  --------------------------------------------------
#   POST /v1/cards                   board, title           routes_actions.py:199  (_action_fields "board","title")
#   POST /v1/template/apply          name                   routes_actions.py:281  (_action_fields "name")
#   POST /v1/cluster/stop            container_id           routes_actions.py:303  (_action_fields "container_id")
#   POST /v1/sessions/{id}/retire    profile                routes_sessions.py:252-254 (400 without it)
#   POST /v1/sessions/{id}/compact   profile                routes_sessions.py:291-293 (400 without it)
#   POST /v1/memory/{node}/delete    profile                routes_memory.py:280-282  (400 without it)
#   POST /v1/memory/{node}/edit      profile, content       routes_memory.py:307-314  (400 w/o profile / content)
#   POST /v1/orchestrator/chat       prompt                 routes_orchestrator.py:1304-1310 (400 without it)
#
# (Path params like card_id / node_id / session_id are embedded in the URL the
# client itself builds, so they are inherently transmitted — no gate needed.
# Handlers that require NO body field beyond confirm — merge, autodown
# enable/disable/wake/cancel, kanban recover, cluster up/down, triggers run,
# escalate — are excluded: their only body requirement is confirm, already
# covered by test_every_mutating_post_carries_confirm + the server-side
# confirm-gate test. Profile-editor PUT is excluded: it requires "at least one
# of many optional fields", not a fixed name.)
REQUIRED_BODY_PARAMS = {
    ("POST", "/v1/cards"): ["board", "title"],
    ("POST", "/v1/template/apply"): ["name"],
    ("POST", "/v1/cluster/stop"): ["container_id"],
    ("POST", "/v1/sessions/{param}/retire"): ["profile"],
    ("POST", "/v1/sessions/{param}/compact"): ["profile"],
    ("POST", "/v1/memory/{param}/delete"): ["profile"],
    ("POST", "/v1/memory/{param}/edit"): ["profile", "content"],
    ("POST", "/v1/orchestrator/chat"): ["prompt"],
}


def _client_body_params_for(method, path):
    """Parse the enclosing Swift function of a POST call site and return the
    set of BODY key names the client puts in its JSON payload.

    Catches both payload shapes the client uses:
      * inline dictionary literal: `["board": board, "title": title, ...]`
      * subscript assignment after building a payload dict: `payload["assignee"] = ...`
    A required body field whose name appears in NEITHER form is a call that
    always 400s — this is the body-field analog of the escaped-backslash
    query defect (a field the client never actually transmits).
    """
    src = open(f"{_REPO_ROOT}/{CLIENT_PATH}").read()
    for regex in (_CALL_RE, _LABELED_CALL_RE):
        for m in regex.finditer(src):
            if (_normalize_swift_path(m.group(2)) == path
                    and m.group(1).upper().replace("READ", "GET") == method):
                fn_start = src.rfind("\n    func ", 0, m.start()) + 1
                fn_end = src.find("\n    func ", m.end())
                if fn_end == -1:
                    fn_end = len(src)
                body = src[fn_start:fn_end]
                subscripts = set(re.findall(
                    r'payload\s*\[\s*"([^"]+)"\s*\]', body))
                inline = set(re.findall(
                    r'"([A-Za-z0-9_]+)"\s*:', body))
                return subscripts | inline
    return set()


def test_every_required_body_field_is_sent():
    """Mirror of the query-param gate for body fields. For every POST route
    whose handler REQUIRES a body field (400 without it), the client must
    actually put that field's name in its JSON payload. A regression in either
    direction — the server starts requiring a body field the client never
    sends (silent 400, the board/title/container_id/prompt class), or the
    client stops sending one — fails the suite."""
    failures = []
    for (method, path), required in REQUIRED_BODY_PARAMS.items():
        sent = _client_body_params_for(method, path)
        for field in required:
            if field not in sent:
                failures.append((path, field, sent))
    assert not failures, (
        "Client fails to transmit a server-REQUIRED body field in its POST "
        "payload — that call always 400s (or omits a semantic the handler "
        "needs):\n"
        + "\n".join(
            f"  {p} requires body field '{field}' but the client payload keys "
            f"are {sorted(sent)}"
            for p, field, sent in failures
        )
    )


# Real-HTTP exercise of the chat seam (the exact failure class from last night)
# --------------------------------------------------------------------------- #

class RunningServer:
    """A live server bound to loopback port 0, torn down by the fixture."""

    def __init__(self, hscc_dir, **kwargs):
        self.server = api_server.create_server(
            hscc_dir=hscc_dir, addr=("127.0.0.1", 0), **kwargs
        )
        self.host, self.port = self.server.server_address[:2]
        import threading
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method="GET", path="/v1/ping", token=None, body=None,
                content_type="application/json"):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = content_type
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        else:
            data = None
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw
        return resp.status, payload


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture
def fakes(monkeypatch):
    """Neutralize the orchestrator backing seams so a chat submit resolves +
    runs WITHOUT touching live project registry / hermes / kanban state. Same
    contract as test_routes_orchestrator.py::fakes: ``_backing_resolve`` returns
    a resolution dict, ``_backing_invoke`` returns ``(reply, profile, session)``
    (the exact 3-tuple ``_run_job`` unpacks at routes_orchestrator.py:1113)."""

    def _resolve(project, registry_path):
        name = project or "general"
        return {
            "name": name,
            "profile": f"{name}-orch",
            "session": name,
            "board": "default",
        }

    def _invoke(profile, session, prompt, timeout=None,
                image_data=None, image_mime=None,
                cancel_evt=None, on_spawn=None):
        return ("hello from the contract-test orchestrator", profile, session)

    monkeypatch.setattr(routes_orchestrator, "_backing_resolve", _resolve)
    monkeypatch.setattr(routes_orchestrator, "_backing_invoke", _invoke)
    # Redirect the registry lookups to a path that cannot exist on the live
    # home dir; the fake resolve above never reads it anyway.
    monkeypatch.setattr(routes_orchestrator, "_registry_path",
                        lambda ctx: "/nonexistent-registry.json")


# The app's exact payload for POST /v1/orchestrator/chat (HSCCClient.swift:
# orchestratorChatStart). Mirrors what Swift sends.
def _app_chat_payload(project):
    payload = {"prompt": "what's the cluster status?", "confirm": True}
    if project:
        payload["project"] = project
    return payload


def test_chat_post_app_payload_202_job_shape(running, token, fakes):
    """The app's POST /v1/orchestrator/chat returns 202 Accepted immediately
    with a `job_id`, `status`, `elapsed`, `speak` — NOT a 404 on a
    `/v1/projects/{name}/chat` shape, and NOT a 409 confirm miss."""
    status, payload = running.request(
        "POST", "/v1/orchestrator/chat", token=token,
        body=_app_chat_payload(None),
    )
    assert status == 202, payload
    assert payload["job_id"]
    assert payload["status"] == "queued"
    assert "elapsed" in payload
    assert payload["speak"]


def test_chat_post_missing_confirm_409(running, token, fakes):
    """Without `confirm`, the app's own chat route 409s — the exact failure
    that reached the operator's phone. Lock it in so the contract is explicit:
    the app MUST send confirm and the server MUST 409 without it."""
    body = _app_chat_payload(None)
    del body["confirm"]
    status, payload = running.request(
        "POST", "/v1/orchestrator/chat", token=token, body=body,
    )
    assert status == 409


def test_chat_poll_with_app_job_id(running, token, fakes):
    """Start a chat with the app's payload, then GET the job with the returned
    id — the two-step app flow — and confirm the poll shape has `status` +
    `elapsed` (and eventually a `reply`)."""
    _, created = running.request(
        "POST", "/v1/orchestrator/chat", token=token,
        body=_app_chat_payload("general"),
    )
    job_id = created["job_id"]
    status, payload = running.request(
        "GET", f"/v1/orchestrator/chat/{job_id}", token=token,
    )
    assert status == 200, payload
    assert payload["job_id"] == job_id
    assert "status" in payload
    assert "elapsed" in payload
    # The fake resolve gives project name "general"; a finished invocation with
    # the _invoke fake above reaches `done`.
    assert payload["status"] == "done"
    assert "reply" in payload


def test_chat_post_unknown_project_400(running, token, fakes, monkeypatch):
    """A named project the server can't resolve is a clean 400, surfaced as a
    failure — the app treats it as such (never a fake reply)."""
    def _raise(project, registry_path):
        from api_server import ApiError
        raise ApiError(400, "unknown_project", "no such project", "no such")
    monkeypatch.setattr(routes_orchestrator, "_backing_resolve", _raise)
    status, payload = running.request(
        "POST", "/v1/orchestrator/chat", token=token,
        body=_app_chat_payload("nope"),
    )
    assert status == 400
    assert payload["error"]["code"] == "unknown_project"


# --------------------------------------------------------------------------- #
# 404 for a route shape the app no longer calls (the regression guard)
# --------------------------------------------------------------------------- #

def test_no_legacy_projects_name_chat_404_shape():
    """The 404 from last night was a `/v1/projects/{name}/chat` shape the
    server never registered. The Swift client does NOT call it anymore (it
    moved to POST /v1/orchestrator/chat). Assert no such path is registered and
    it is absent from the derived app routes — the drift gate would otherwise
    not know what to forbid."""
    assert not any(p.match("/v1/projects/x/chat")
                   for _m, p in _registered() if p)
    # And the app must be exercising the orchestrator route instead.
    assert ("POST", "/v1/orchestrator/chat") in SWIFT_ROUTES
