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
import os
import re
from pathlib import Path

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


# --------------------------------------------------------------------------- #
# Extension surfaces — widgets + Live Activities
# --------------------------------------------------------------------------- #
#
# HSCCClient.swift is the APP's route list. Widgets and Live Activities read
# through WidgetDataClient.swift (its closed `WidgetDataPath.all` allowlist)
# plus two direct `client.get` calls in MonitorWidget.swift. Those were never
# in the derived set, so a widget route could 404 on the live server while this
# gate stayed green — exactly the class this contract test exists to stop.
#
# The extension client is GET-only by construction, so every route found in
# these sources is asserted as GET. String literals are extracted with a real
# scanner (line/block comments skipped), not a bare regex over the file, so a
# route quoted in prose cannot invent a requirement either.

WIDGET_CLIENT_PATH = "ios-app/Sources/Shared/WidgetDataClient.swift"
EXTENSION_SOURCE_DIRS = (
    "ios-app/Sources/HSCCWidgets",
    "ios-app/Sources/HSCCLiveActivityMonitor",
)
EXTENSION_SOURCE_FILES = (WIDGET_CLIENT_PATH,)


def _swift_string_literals(src):
    """Every Swift string literal in ``src``, comments excluded."""
    out = []
    i = 0
    n = len(src)
    while i < n:
        if src.startswith("//", i):
            end = src.find("\n", i + 2)
            if end == -1:
                break
            i = end + 1
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        if src[i] == '"':
            j = i + 1
            buf = []
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if src[j] == '"':
                    break
                buf.append(src[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        i += 1
    return out


def _extension_routes():
    """Every ``/v1/...`` literal the widget/Live-Activity sources can fetch."""
    paths = [Path(_REPO_ROOT) / rel for rel in EXTENSION_SOURCE_FILES]
    for rel_dir in EXTENSION_SOURCE_DIRS:
        paths.extend(sorted((Path(_REPO_ROOT) / rel_dir).glob("*.swift")))
    routes = []
    seen = set()
    for path in paths:
        if not path.exists():
            continue
        for lit in _swift_string_literals(path.read_text()):
            route = _normalize_swift_path(lit)
            if not route.startswith("/v1/"):
                continue
            key = ("GET", route)       # the extension client is GET-only
            if key not in seen:
                seen.add(key)
                routes.append(key)
    return routes


EXTENSION_ROUTES = _extension_routes()
for _route in EXTENSION_ROUTES:
    if _route not in SWIFT_ROUTES:
        SWIFT_ROUTES.append(_route)


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


#: The extension client's audited read set (WIDGET_DATA_CONTRACT §3 + the
#: daemon-host sidecar). Kept explicit so deleting a path from the Swift source
#: is a visible change here, and adding one without registering it fails the
#: register gate above.
EXPECTED_EXTENSION_ROUTES = {
    ("GET", "/v1/projects"),
    ("GET", "/v1/mobile/summary"),
    ("GET", "/v1/kanban/running"),
    ("GET", "/v1/kanban/blocked"),
    ("GET", "/v1/kanban/stale"),
    ("GET", "/v1/cluster/monitor"),
    ("GET", "/v1/cluster/status"),
    ("GET", "/v1/daemon/host"),
    ("GET", "/v1/autodown/status"),
}


def test_extension_route_set_is_the_audited_widget_reads():
    """The widget/Live-Activity route list is parsed from their own sources.

    If this fails because a path was added, register it on the server and add
    it to ``WIDGET_ROUTES.md`` (and the contract set if it is a new dataset);
    if it fails because a path disappeared, the surface lost a read it needs.
    """
    assert set(EXTENSION_ROUTES) == EXPECTED_EXTENSION_ROUTES


def test_widget_routes_doc_names_every_extension_route():
    """The human route map must not fall behind the derived widget set."""
    doc_path = Path(_REPO_ROOT) / "hscc-api/docs/WIDGET_ROUTES.md"
    assert doc_path.exists(), "hscc-api/docs/WIDGET_ROUTES.md is missing"
    doc = doc_path.read_text()
    missing = [path for _method, path in EXTENSION_ROUTES if path not in doc]
    assert not missing, (
        "Widget/Live-Activity route(s) absent from hscc-api/docs/WIDGET_ROUTES.md:\n"
        + "\n".join(f"  {p}" for p in missing)
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
#   GET /v1/sessions                  profile                routes_sessions.py:202 (400 without it)
#
# PRUNED (client stopped calling the route; see
# test_matrices_name_routes_the_client_still_calls):
#   GET /v1/memory                    profile                routes_memory.py:252-255
#     HSCCClient.swift lost `memories(profile:)` in be154aec ("delete the
#     operator console screens — approvals, memory, ops, autodown, template
#     detail, debug harness", t_0f3f70e6 console retirement), so there is no
#     iOS call site left to hold this row. The SERVER route and its required
#     `profile` param are unchanged and still gated by
#     tests/test_routes_memory.py::test_list_missing_profile_400 — only the
#     iOS-client half of the contract went away.
#
# Query params the server treats as OPTIONAL (days, older_than, limit, before)
# are not listed here — the client transmitting them is a real behavior (the
# responses differ), but their absence is not a 400. They are still exercised
# by the derived-route register gate above.
REQUIRED_QUERY_PARAMS = {
    ("GET", "/v1/sessions"): ["profile"],
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
#   POST /v1/cards                   board, title           routes_actions.py:230  (handle_create_card -> _action_fields "board","title")
#   POST /v1/cluster/stop            container_id           routes_actions.py:334  (handle_cluster_stop -> _action_fields "container_id")
#   POST /v1/sessions/{id}/retire    profile                routes_sessions.py:254 (handle_sessions_retire)
#   POST /v1/sessions/{id}/compact   profile                routes_sessions.py:293 (handle_sessions_compact)
#   POST /v1/orchestrator/chat       prompt                 routes_orchestrator.py:1970-1975 (handle_orchestrator_chat -> _validate_prompt)
#
#   (Line anchors in the kept rows re-verified against these handlers on
#   2026-11-18 (t_80e185d2); handler names carry the citation if they drift
#   again. The audit doc `hscc-api/docs/audit-client-server-params.md` mirrors
#   both matrices — keep the two in step when a row changes.)
#
# PRUNED (client stopped calling the route; see
# test_matrices_name_routes_the_client_still_calls). All three left
# HSCCClient.swift in be154aec ("delete the operator console screens —
# approvals, memory, ops, autodown, template detail, debug harness",
# t_0f3f70e6 console retirement). Cited by handler, not line number, since
# line anchors in this file have already drifted once:
#   POST /v1/template/apply          name                   routes_actions.handle_template_apply (_action_fields "name")
#     -> still server-gated: test_routes_actions.py::test_template_missing_name_400
#   POST /v1/memory/{node}/delete    profile                routes_memory.handle_memory_delete
#     -> still server-gated: test_routes_memory.py::test_delete_requires_profile_in_body
#   POST /v1/memory/{node}/edit      profile, content       routes_memory.handle_memory_edit
#     -> still server-gated, both halves: test_routes_memory.py::test_edit_requires_profile
#        (the missing-`profile` 400) and ::test_edit_requires_content (the
#        missing-`content` 400). The profile half was untested until t_7430b28a
#        added it; before that this row was the only place the requirement was
#        named in a test at all.
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
    ("POST", "/v1/cluster/stop"): ["container_id"],
    ("POST", "/v1/sessions/{param}/retire"): ["profile"],
    ("POST", "/v1/sessions/{param}/compact"): ["profile"],
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


def test_matrices_name_routes_the_client_still_calls():
    """Table-staleness guard — the half of the matrix contract neither gate
    above can check, and the one that produced the two reds this card fixes.

    Both matrices are hand-maintained while the route set they describe is
    derived from HSCCClient.swift. So when the app STOPS calling a route, the
    row does not disappear: `_client_query_params_for` /
    `_client_body_params_for` find no call site, return an EMPTY set, and the
    gates report "the client sends none of the required fields" — a real-looking
    red whose actual cause is a dead row. That is exactly what happened after
    be154aec deleted the memory viewer and the template detail screen: four
    rows for routes the app no longer calls sat in both matrices and have
    failed the two gates above ever since.

    Asserting the rows name live routes is a GATE, not a filter: the point is
    to name the stale row precisely, and to forbid "fixing" such a red by
    skipping routes the client no longer calls — which would silently shrink
    the comparison these gates exist to perform. If this test fails, either
    delete the row (and its line in the audit comment), or the client method
    was dropped by mistake and must come back.
    """
    derived = set(SWIFT_ROUTES)
    stale = []
    for label, table in (("REQUIRED_QUERY_PARAMS", REQUIRED_QUERY_PARAMS),
                         ("REQUIRED_BODY_PARAMS", REQUIRED_BODY_PARAMS)):
        for (method, path) in table:
            if (method, path) not in derived:
                stale.append((label, method, path))
    assert not stale, (
        "Stale matrix row(s): the table names a route HSCCClient.swift no "
        "longer calls, so the send-gates can only ever see an empty payload "
        "for it (that is the 'client sends none of the required fields' red, "
        "not a real client bug). Delete the row and its audit-comment line, "
        "or restore the deleted client method if the app still needs the "
        "call:\n"
        + "\n".join(f"  {label}: {m} {p}" for label, m, p in stale)
    )


# --------------------------------------------------------------------------- #
# The audit doc must MIRROR the matrices
# --------------------------------------------------------------------------- #
#
# The two matrices above are the source of truth; the human-facing deliverable
# they point readers at is `hscc-api/docs/audit-client-server-params.md`. t_80e185d2
# found the pair had silently diverged: t_f09436f1 (PR #351) pruned four retired
# rows out of the matrices while the doc still presented them as live contract.
# The stale doc then mis-led that card's own brief into blaming the wrong commit
# (PR #303) for the red. PR #355 re-pointed the doc — that repaired the symptom.
# THIS is what stops the recurrence.
#
# Same shape as test_matrices_name_routes_the_client_still_calls, and deliberately
# so: a GATE, not a filter. The doc's two LIVE tables must equal the matrices —
# every matrix row present with the same required set, and no doc row naming a
# route absent from a matrix. Neither half is optional; the half that went stale
# was "doc has rows the matrix no longer has", and a one-directional check would
# have passed it.
#
# Retired rows are NOT a failure: they belong in the doc's "Rows retired with
# their client call site" section, which the parser never reads because it stops
# at the next heading. That section is intentionally a superset — it documents
# server requirements whose client call site is gone.

AUDIT_DOC_PATH = "hscc-api/docs/audit-client-server-params.md"

# (doc section heading, the matrix that section must mirror). Prefix match: the
# doc's headings carry a " (GET routes)" / " (POST routes)" qualifier.
_LIVE_DOC_TABLES = (
    ("## Required-query-param matrix", REQUIRED_QUERY_PARAMS),
    ("## Required-body-field matrix", REQUIRED_BODY_PARAMS),
)

# The doc writes `/v1/sessions/{id}/retire`, the matrix writes
# `/v1/sessions/{param}/retire`. Same route, different placeholder spelling —
# the route KEY must not hinge on which word the author reached for.
_PLACEHOLDER_SEG = re.compile(r"\{[^{}]*\}")


def _canonical_route(method, path):
    """Normalize one route key: upper-case verb, `{...}` segments unified."""
    return (method.upper(), _PLACEHOLDER_SEG.sub("{param}", path))


def _doc_table_cells(doc_lines, heading_prefix):
    """Yield the stripped cell list of each DATA row of the markdown table under
    ``heading_prefix``.

    Pipe-split, not prose-matched. The header row and the ``|---|`` separator row
    are skipped, and the scan STOPS at the next heading — that is what keeps the
    doc's retired-rows table (an intentional superset) out of every comparison
    built on this.
    """
    in_section = False
    for raw in doc_lines:
        line = raw.strip()
        if not in_section:
            in_section = line.startswith(heading_prefix)
            continue
        if line.startswith("#"):
            return
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: "):
            continue
        if cells[0].lower() == "client route":
            continue
        yield cells


def _doc_table_rows(doc_lines, heading_prefix):
    """``{canonical_route: frozenset(required_fields)}`` for that table, using
    the pipe-split contract: cell 0 is ``METHOD /path``, cell 1 is the
    comma-separated required fields.

    A row whose first cell is not a single ``METHOD /path`` pair is kept under a
    sentinel key so it surfaces as a doc-only row rather than being skipped: the
    doc is evidence, and a row this parser cannot read is a row the gate cannot
    clear.
    """
    rows = {}
    for cells in _doc_table_cells(doc_lines, heading_prefix):
        parts = cells[0].split()
        if len(parts) != 2:
            route = ("?", cells[0])
        else:
            route = _canonical_route(parts[0], parts[1])
        rows[route] = frozenset(
            f.strip() for f in cells[1].split(",") if f.strip())
    return rows


def test_audit_doc_live_tables_mirror_the_matrices():
    """The doc-mirror gate: each of the doc's two LIVE tables must state exactly
    the rows of the matrix it mirrors — same routes, same required sets.

    Fails when the doc falls behind the matrices (the t_80e185d2 class: a pruned
    matrix row still advertised as live contract), when the doc runs ahead of
    them (a row naming a route no matrix has, i.e. a requirement the gates do not
    actually enforce), or when a shared row disagrees on its field set.

    Fixing a red here means editing the doc's live table to match the matrix —
    the matrices are the source of truth — or moving a retired row down into the
    "Rows retired with their client call site" section, which this gate skips.
    """
    doc_path = f"{_REPO_ROOT}/{AUDIT_DOC_PATH}"
    assert os.path.exists(doc_path), (
        f"Audit doc {AUDIT_DOC_PATH} is gone; the matrices name it as the "
        f"deliverable readers are sent to."
    )
    doc_lines = open(doc_path).read().split("\n")

    missing, extra, differ = [], [], []
    for heading, matrix in _LIVE_DOC_TABLES:
        doc = _doc_table_rows(doc_lines, heading)
        want = {_canonical_route(m, p): frozenset(v)
                for (m, p), v in matrix.items()}
        assert doc or not want, (
            f"{AUDIT_DOC_PATH}: no table rows parsed under {heading!r}, but "
            f"{len(want)} matrix row(s) should be mirrored there. Either the "
            f"heading was renamed or the table was deleted — the gate cannot "
            f"read what is not there, so it fails rather than passing vacuously."
        )
        missing += [(heading, r) for r in sorted(set(want) - set(doc))]
        extra += [(heading, r, sorted(doc[r]))
                  for r in sorted(set(doc) - set(want))]
        differ += [(heading, r, sorted(want[r]), sorted(doc[r]))
                   for r in sorted(set(want) & set(doc))
                   if want[r] != doc[r]]

    report = (
        [f"  MISSING from the doc (matrix row not in {h!r}): {m} {p}"
         for h, (m, p) in missing]
        + [f"  EXTRA in the doc (row in {h!r} with no matrix row): "
           f"{m} {p} -> {fields}. If the client call site is retired, move "
           f"this row to the doc's 'Rows retired with their client call "
           f"site' section; otherwise add the matrix row."
           for h, (m, p), fields in extra]
        + [f"  DIFFERS ({m} {p} in {h!r}): matrix requires {want_f}, "
           f"doc states {doc_f}"
           for h, (m, p), want_f, doc_f in differ]
    )
    assert not report, (
        f"{AUDIT_DOC_PATH} no longer mirrors the contract matrices. The "
        f"matrices in this file are the source of truth; the doc mirrors them.\n"
        + "\n".join(report)
    )


# The optional half the card named: the citation anchors themselves.
#
# `file.py:<line> (handler)` is the required citation form here, and it is the
# form because the anchors had ALREADY drifted once by the time t_80e185d2 read
# them (`:199`/`:303`/`1304-1310` were all pre-drift) — a wrong anchor is worse
# than no anchor, because it sends the next reader to the wrong code carrying the
# authority of a citation. The mirror gate above proves the ROWS agree; it says
# nothing about whether the line numbers in them still point anywhere.
#
# Asserting "the cited line contains the handler NAME" — the card's wording —
# would be wrong and would fail a correct doc: the name sits on the `def`, not on
# the 400 site. `routes_actions.py:230` reads `data = _action_fields(...)` while
# `handle_create_card` starts at :228. What "this anchor still points into that
# handler" actually means is that the cited line/range falls inside the named
# handler's def..end span, and `ast` gives that span exactly, with no
# line-number guessing.
#
# Live tables only. The retired table's anchors rot identically but its cells are
# compound (`routes_memory.py:342, :346`; `routes_actions.py:434
# handle_card_close, :464 handle_card_edit`) — parsing those is its own change
# with its own false-positive risk, deliberately not taken here.

_CITATION_RE = re.compile(r"\b([A-Za-z0-9_]+\.py):(\d+)(?:-(\d+))?")
# The handler a citation names: ``(`_validate_prompt`)`` / ``(handle_create_card)``.
_CITED_HANDLER_RE = re.compile(r"\(\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*\)")


def _def_span(path, name):
    """``(first_line, last_line)`` of the def named ``name`` in ``path``, else None."""
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            if node.lineno is None or node.end_lineno is None:
                return None
            return node.lineno, node.end_lineno
    return None


def test_audit_doc_citations_still_point_inside_their_handler():
    """Every ``file.py:<line> (`handler`)`` in the doc's two LIVE tables must
    still resolve: the file exists, the cited line(s) are inside it, the handler
    it names still exists, and the cited range falls inside that handler's
    def..end span.

    A citation with no handler name fails too — the parenthesised handler is part
    of the required citation form, and without it the anchor is unverifiable
    (which is exactly how the pre-#355 anchors stayed quietly wrong).
    """
    doc_lines = open(f"{_REPO_ROOT}/{AUDIT_DOC_PATH}").read().split("\n")
    problems = []
    for heading, _matrix in _LIVE_DOC_TABLES:
        for cells in _doc_table_cells(doc_lines, heading):
            route, cite = cells[0], (cells[2] if len(cells) > 2 else "")
            found = _CITATION_RE.search(cite)
            if not found:
                problems.append((route, f"no `file.py:<line>` citation in {cite!r}"))
                continue
            fname, first = found.group(1), int(found.group(2))
            last = int(found.group(3)) if found.group(3) else first
            modpath = f"{_REPO_ROOT}/hscc-api/{fname}"
            if not os.path.exists(modpath):
                problems.append((route, f"{fname} does not exist"))
                continue
            total = len(open(modpath).read().split("\n"))
            if last > total:
                problems.append((route, f"{fname}:{last} is past EOF "
                                         f"({total} lines)"))
                continue
            named = _CITED_HANDLER_RE.search(cite)
            if not named:
                problems.append((route, f"{fname}:{first} names no handler — "
                                         f"required form is "
                                         f"`file.py:LINE (`handler`)`"))
                continue
            handler = named.group(1)
            span = _def_span(modpath, handler)
            if span is None:
                problems.append((route, f"no def named {handler!r} in {fname}"))
                continue
            if not (span[0] <= first and last <= span[1]):
                problems.append((route, f"{fname}:{first}-{last} falls OUTSIDE "
                                         f"{handler}() (def at {span[0]}-"
                                         f"{span[1]}) — the anchor drifted"))
    assert not problems, (
        f"{AUDIT_DOC_PATH} has citation anchor(s) that no longer point into the "
        f"handler they name. Re-point the line (through api_server.ROUTES, not "
        f"by guessing) — do not delete the citation to make this pass:\n"
        + "\n".join(f"  {route}: {why}" for route, why in problems)
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
