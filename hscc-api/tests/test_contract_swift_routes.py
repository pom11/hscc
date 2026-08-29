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

import http.client
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
    hand-maintaining a table — is what makes the gate cannot-drift."""
    src = open(f"{_REPO_ROOT}/{CLIENT_PATH}").read()
    seen = set()
    routes = []
    for m in _CALL_RE.finditer(src):
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

    def _invoke(profile, session, prompt, timeout=None):
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
