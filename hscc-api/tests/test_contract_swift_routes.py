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
#: daemon-host sidecar + the recorder's history read + the alert feed). Kept
#: explicit so deleting a path from the Swift source is a visible change here,
#: and adding one without registering it fails the register gate above.
#:
#: "Extension client", not "widget": the last entry is an app-only read that
#: happens to live in the same shared GET-only client file, so the derived
#: scanner sees it and the audited set must name it. The set is what the
#: scanner is checked against, so it is the union of both, whatever surface
#: renders the read.
EXPECTED_EXTENSION_ROUTES = {
    ("GET", "/v1/projects"),
    ("GET", "/v1/mobile/summary"),
    ("GET", "/v1/kanban/running"),
    ("GET", "/v1/kanban/blocked"),
    ("GET", "/v1/kanban/stale"),
    ("GET", "/v1/cluster/monitor"),
    ("GET", "/v1/cluster/status"),
    ("GET", "/v1/cluster/history"),
    ("GET", "/v1/daemon/host"),
    ("GET", "/v1/autodown/status"),
    # t_5ae8cfcf: the metrics-history read the history chart draws from. Its
    # shape half of the contract is `test_metrics_history_shape_is_declared`
    # below, because a route whose keys drift from the decoder fails silently —
    # the chart reads nil and nothing anywhere errors.
    ("GET", "/v1/metrics/history"),
    # t_c55be8e1 (PR #446, card N4): the operator alert feed. NOT a widget
    # dataset — the phone receives alerts through APNs and the macOS menu bar
    # app polls this route every 5 s — but the read lives in
    # `WidgetDataClient.swift` because that file is the one GET-only client the
    # app and the extensions share, and a path must be in `WidgetDataPath.all`
    # to be reachable at all. The derived scanner reads the whole file, so this
    # set has to name it: the entry is what turns the `GET /v1/alerts` row
    # already present in `WIDGET_ROUTES.md` into audited contract instead of
    # prose the gates never read. #446 shipped the read and its doc row but not
    # this entry, which is what made the leg red.
    ("GET", "/v1/alerts"),
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


# --------------------------------------------------------------------------- #
# The STATED exemptions to the confirm contract
# --------------------------------------------------------------------------- #
#
# `confirm` belongs to the GUARDED-OPS plane: provision/stop/restart, cluster
# up/down, card dispatch/merge/comment/block/close, autodown wake/cancel, memory
# edit/delete, profile writes — an action that changes cluster, workload or board
# state and must never fire just because a screen got tapped. Every mutating POST
# on that plane carries `"confirm": true` and its handler calls
# `_require_confirm` / `_action_fields`; the two gates below prove neither half of
# that pair can drift alone.
#
# The push-device roster is the one mutating surface the app calls that this
# contract deliberately does NOT cover. The exemption lives here as a stated,
# per-route reason rather than as a route skipped by the regex, because an
# unstated exemption is indistinguishable from the exact bug these gates exist to
# catch, and because BOTH gates read this one dict: a route can therefore never
# land in the theatre shape the reverse gate names (client sends `confirm`, server
# ignores it). Adding an entry is audited by
# `test_confirm_exemption_is_closed_and_justified` — read it first.
CONFIRM_EXEMPT_POSTS = {
    "/v1/devices": (
        "Registers or refreshes the CALLER'S OWN APNs token (idempotent per "
        "(platform, token)), behind the server's single bearer-token gate, and "
        "changes no cluster, workload or board state. The app calls it on every "
        "launch and foreground with no operator decision in front of it, so a "
        "confirm flag here could only be a hardcoded `true` that no handler "
        "checks — theatre, not a gate."
    ),
    "/v1/devices/test": (
        "The operator's own one-push APNs self-check: the same route "
        "`hscc notify test` drives with an empty body, and that CLI requires no "
        "confirm either (hscc_daemon/notify_cli.py:post_test). It delivers at "
        "most one test notification, stores nothing, and a failed delivery is an "
        "honest 200 with sent=false rather than a mutation."
    ),
}


def test_every_mutating_post_carries_confirm():
    """Last night's root cause: a mutating POST that reached the phone without
    `confirm` and 409'd. Every mutating call the Swift client makes must send
    `confirm: true`. We assert by scanning the enclosing function of each POST
    call site for a `"confirm": true` payload key (set either in a `payload`
    dict built before the call, or inline in the call args).

    A route is excused ONLY if it is a key of `CONFIRM_EXEMPT_POSTS` — a named
    route with a stated reason, not a pattern, and the same list the server-side
    gate below consults, so the two halves cannot disagree."""
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
        path = _normalize_swift_path(m.group(2))
        if (re.search(r'"confirm"\s*:\s*true', body) is None
                and path not in CONFIRM_EXEMPT_POSTS):
            offending.append(path)
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
    Checks the CALL SITE, not prose, by stripping the handler docstring first.

    The same `CONFIRM_EXEMPT_POSTS` list excuses a route here as on the client
    half, so the pair cannot drift apart; and the exemption is only valid while
    the server really is ungated — that direction is pinned by
    `test_confirm_exemption_is_closed_and_justified`, not by this gate.
    """
    ungated = []
    not_registered = []
    for method, path in SWIFT_ROUTES:
        if method != "POST":
            continue
        handler, pattern = _server_handler_for(method, path)
        if handler is None:
            not_registered.append(path)
            continue
        if path in CONFIRM_EXEMPT_POSTS:
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


# The minimum a stated exemption must prove to be allowed to exist. Both gates
# above skip anything named in CONFIRM_EXEMPT_POSTS, so that dict is the single
# place where the whole confirm contract could be deleted by adding one line.
_MIN_EXEMPTION_REASON_CHARS = 80


def test_confirm_exemption_is_closed_and_justified():
    """The exemption list is a closed, audited set — never an escape hatch.

    Four directions are pinned, one per failure this list could cause:

    1. **staleness** — every entry names a POST the Swift client actually calls
       today. A route the app stopped calling loses its entry, the same rule
       `test_matrices_name_routes_the_client_still_calls` enforces on the two
       matrices: a stale exemption reads as "still audited" for a surface that
       no longer exists.
    2. **an exemption with no reason** — the reason must be a rationale, not a
       placeholder. An unstated exemption is indistinguishable from deleting the
       assertion, which is the failure mode these gates were written for.
    3. **scope creep, both ways.** (a) Each exempt route's server handler must
       still live in the module the exemption was written for —
       `routes_devices.py`, the push-device roster — so a later rewrite that
       points one of these paths at a guarded-ops handler cannot inherit the
       exemption. (b) Each one must still NOT call a confirm helper. That is the
       case neither confirm gate can see, because both gates skip the route: an
       exempt client POST against a server that started requiring `confirm` is
       the literal 409-on-the-phone, and without this pin the two gates would go
       green while it happened.
    4. **exempt from confirm is never exempt from auth** — the reason a
       self-registration needs no confirm is that it sits behind the token
       gate. `ApiHandler` authorizes once, unconditionally, in `_route`, which
       every verb method funnels through; there is no per-route allowlist to
       weaken. If that shape ever changes, this pin fails and the exemption has
       to be re-argued rather than inherited.
    """
    stale, thin, off_plane, secretly_gated = [], [], [], []

    derived_posts = {p for (m, p) in SWIFT_ROUTES if m == "POST"}
    for path, reason in CONFIRM_EXEMPT_POSTS.items():
        if path not in derived_posts:
            stale.append(path)
        if not isinstance(reason, str) or len(reason.strip()) < _MIN_EXEMPTION_REASON_CHARS:
            thin.append(path)
        handler, pattern = _server_handler_for("POST", path)
        if handler is None:
            off_plane.append((path, "no registered POST route"))
            continue
        module = os.path.basename(inspect.getsourcefile(handler) or "")
        if module != "routes_devices.py":
            off_plane.append((path, f"{handler.__name__} lives in {module}"))
            continue
        src = _strip_handler_docstring(inspect.getsource(handler))
        if _CONFIRM_HELPER_CALL.search(src) is not None:
            secretly_gated.append((path, handler.__name__, pattern))

    verbs = sorted(n for n in vars(api_server.ApiHandler) if n.startswith("do_"))
    unauthenticated = []
    if verbs:
        for name in verbs:
            if "self._route(" not in inspect.getsource(getattr(api_server.ApiHandler, name)):
                unauthenticated.append(name)
        if "self._authorize()" not in inspect.getsource(api_server.ApiHandler._route):
            unauthenticated.append("_route (no _authorize call)")

    report = (
        [f"  STALE exemption: {p} — HSCCClient.swift no longer POSTs it. "
         f"Delete the entry (and its reason) with the call site."
         for p in stale]
        + [f"  THIN exemption: {p} — the reason is under "
           f"{_MIN_EXEMPTION_REASON_CHARS} characters. Say why the route is "
           f"outside the confirm contract, or gate it."
           for p in thin]
        + [f"  OFF-PLANE exemption: {p} -> {why}. The exemption covers the "
           f"push-device roster only; a mutating POST elsewhere must carry "
           f"confirm on both halves."
           for p, why in off_plane]
        + [f"  CONTRADICTED exemption: POST {p} -> {h} (registered {rp}) DOES "
           f"call a confirm helper while the client is exempted from sending it "
           f"— that call 409s on the phone. Either send confirm and drop the "
           f"exemption, or un-gate the handler deliberately."
           for p, h, rp in secretly_gated]
        + [f"  AUTH SHAPE CHANGED: ApiHandler.{n} does not route through the "
           f"single unconditional _authorize() gate. The exemption's central "
           f"premise (every device call is authenticated) no longer holds and "
           f"has to be re-argued."
           for n in unauthenticated]
    )
    assert not report, (
        "CONFIRM_EXEMPT_POSTS is no longer a closed, justified, same-plane "
        "exemption:\n" + "\n".join(report)
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
#   POST /v1/devices                 platform, token, bundle_id, env   routes_devices.py:72-75 (handle_register_device -> devices.register normalises all four)
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
    # t_1af3ef07 (PR #443, cards N2/N3): the APNs token registration the app
    # sends on every launch/foreground. All four fields are REQUIRED — the route
    # maps `devices.register`'s ValueError to a 400 (routes_devices.py:72-79),
    # and `test_invalid_registration_is_400_and_stores_nothing` already proves an
    # empty body 400s — so a client that dropped one would silently stop
    # registering. It carries no `confirm`, deliberately; see
    # CONFIRM_EXEMPT_POSTS above.
    ("POST", "/v1/devices"): ["platform", "token", "bundle_id", "env"],
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


# --------------------------------------------------------------------------- #
# The metrics-history route's shape (t_5ae8cfcf)
# --------------------------------------------------------------------------- #
#
# The register gate above proves the route EXISTS; it cannot prove the SHAPE.
# That gap is the expensive one here: every metric on this route is optional in
# Swift by design (the server writes `null` for "this node did not report it"),
# so a key the route renames, drops or adds decodes to `nil` on the phone and
# the chart quietly draws a hole — no exception, no failed test, no 4xx. The
# three assertions below are what close it:
#
#   1. every key the route emits in a row is declared by the Swift struct,
#   2. every field sparkrun's `MonitorSample` carries survives the route
#      (a drop here is a chart that silently lacks a metric),
#   3. the peak columns ride alongside their averages, and the widget client
#      sends `tier` as a real URLQueryItem — the same defect class
#      `test_every_required_query_param_is_sent` exists for, applied to the
#      widget client, which that matrix does not cover (it parses HSCCClient).

METRICS_SHARED_MODELS = Path(_REPO_ROOT) / "ios-app/Sources/Shared/SharedModels.swift"
METRICS_WIDGET_CLIENT = Path(_REPO_ROOT) / "ios-app/Sources/Shared/WidgetDataClient.swift"
_METRICS_STRUCT_RE = re.compile(
    r"struct\s+(\w+):\s*Decodable[^{]*\{(.*?)\n\}", re.S)


def _swift_decodable_fields(source):
    """{struct name: {field names}} for every `Decodable` struct in a file.

    Derived from the Swift source rather than mirrored as a Python literal, so
    the gate cannot drift the way a hand-copied list does: deleting a field from
    the decoder narrows the set here and the route's key becomes undeclared.
    """
    out = {}
    for name, body in _METRICS_STRUCT_RE.findall(source):
        out[name] = {m.group(1) for m in
                     re.finditer(r"^\s*let\s+(\w+)\s*:", body, re.M)}
    return out


def _metrics_history_rows():
    """Populate the store through the production writer path, then read tier 1
    back through the route. Returns the response payload.

    The ticks are the ones `test_routes_metrics.py::populated` uses: host A
    answers twice inside the :60 bucket at 60 °C then 90 °C (so the bucket's
    average and its peak are different numbers), host B fails in the :120
    bucket (so its row is nulls + an error, not zeros), and the tick at 200 s
    closes both buckets. Rows therefore arrive via
    `MetricsRecorder.tick` -> `rollup` -> `write_rollup`, as in production.
    """
    import sys
    daemon = Path(_REPO_ROOT) / "hscc_daemon"
    if str(daemon) not in sys.path:
        sys.path.insert(0, str(daemon))
    import metrics_history as mh
    import routes_metrics
    from test_routes_metrics import HOST_A, HOST_B, _frame, _serving_rows

    rec = mh.MetricsRecorder()          # call-time HOME, redirected by conftest
    rec.tick(_frame(100), _serving_rows(100), 100)
    rec.tick(_frame(100, host=HOST_B, cpu=20.0, gpu_temp=70.0,
                    gpu_power=140.0), None, 100)
    rec.tick(_frame(118, gpu_temp=90.0, gpu_power=200.0), _serving_rows(118), 118)
    rec.tick(_frame(130, host=HOST_B, error="collect failed", sample=False),
             None, 130)
    rec.tick(_frame(200), _serving_rows(200), 200)     # closes :60 and :120

    # The handler reads a FLAT query dict (`_parse_tier` takes `query["tier"]`
    # as a scalar — api_server flattens parse_qs to its last value at
    # api_server.py:806) and answers `(status, payload)` like every other route.
    status, payload = routes_metrics.handle_metrics_history(
        None, None, {"tier": "1"}, b"")
    assert status == 200, payload
    return payload, HOST_A, HOST_B


def _host_row(payload, host, ts):
    rows = [r for r in payload["hosts"] if r.get("host") == host and r.get("ts") == ts]
    assert rows, f"no {host} row at bucket {ts} in %r" % (payload["hosts"],)
    return rows[0]


def test_metrics_history_row_keys_are_declared_in_swift():
    """Every key the route puts in a row must exist in the Swift decoder."""
    structs = _swift_decodable_fields(METRICS_SHARED_MODELS.read_text())
    host_fields = structs.get("MetricsHostRow", set())
    engine_fields = structs.get("MetricsEngineRow", set())
    assert host_fields and engine_fields, (
        "the metrics-history decoders vanished from SharedModels.swift — the "
        "widget can no longer decode the route at all")

    payload, host_a, host_b = _metrics_history_rows()
    assert payload["hosts"] and payload["engines"], (
        "fixture produced no rows, so this test would pass on nothing: %r" % payload)

    undeclared = sorted({k for row in payload["hosts"] for k in row
                         if k not in host_fields})
    assert not undeclared, (
        "the route emits host key(s) the Swift decoder does not declare, so "
        "they decode to nil on the phone and the chart draws a hole with no "
        "error anywhere: %s" % ", ".join(undeclared))
    undeclared_engines = sorted({k for row in payload["engines"] for k in row
                                 if k not in engine_fields})
    assert not undeclared_engines, (
        "undeclared engine key(s): %s" % ", ".join(undeclared_engines))


def test_metrics_history_keeps_every_sparkrun_field_and_both_peak_halves():
    """No metric is dropped on the way out, and a temperature/power row carries
    its peak next to its average.

    The peak half is the one that matters: a bucket's mean is exactly what hides
    a throttle spike, which is why the recorder stores `*_max` at all. A route
    that folded peaks away would still return valid-looking rows.
    """
    import sys
    daemon = Path(_REPO_ROOT) / "hscc_daemon"
    if str(daemon) not in sys.path:
        sys.path.insert(0, str(daemon))
    import metrics_history as mh

    payload, host_a, host_b = _metrics_history_rows()
    row = _host_row(payload, host_a, 60)
    # `HOST_KEYS` / `ENGINE_KEYS` are the recorder's own vocabulary — the
    # sparkrun `MonitorSample` names it stores under. Asserting against THOSE
    # (rather than a list copied into this file) is what makes the gate
    # two-sided: a field the recorder starts keeping, or a route that stops
    # passing one through, fails here instead of showing up as a hole in a chart.
    missing = sorted(k for k in mh.HOST_KEYS if k not in row)
    assert not missing, "route dropped host field(s): %s" % ", ".join(missing)
    engine = payload["engines"][0]
    missing_engine = sorted(k for k in mh.ENGINE_KEYS if k not in engine)
    assert not missing_engine, "route dropped engine field(s): %s" % ", ".join(
        missing_engine)

    for key in mh.MAX_KEYS:
        peak = f"{key}_max"
        assert peak in row, f"peak column {peak} is missing from the row"
        assert peak in payload["fields"]["host_peaks"], (
            f"{peak} is in the rows but not named by fields.host_peaks, so a "
            "surface checking the vocabulary would not draw it")
    assert payload["fields"]["host_peaks"] == [f"{k}_max" for k in mh.MAX_KEYS]
    # The two halves are DISTINCT facts, and the fixture makes them differ (two
    # samples at 60 °C then 90 °C): a route that copied the average into the
    # peak column, or dropped the peak, fails here — not merely here:
    assert row["gpu_temp_c"] == 75.0, row["gpu_temp_c"]
    assert row["gpu_temp_c_max"] == 90.0, row["gpu_temp_c_max"]
    assert row["gpu_power_w"] == 160.0 and row["gpu_power_w_max"] == 200.0
    # ...and a host that never answered stays null-valued, never zero-valued.
    failed = _host_row(payload, host_b, 120)
    assert failed["error"] == "collect failed"
    assert failed["gpu_temp_c"] is None and failed["cpu_usage_pct"] is None


def test_metrics_history_tier_reaches_the_server_as_a_query_item():
    """The widget client must send `tier` as a real URLQueryItem.

    `tier` is the one parameter the route 400s without, and the widget client is
    not covered by `REQUIRED_QUERY_PARAMS` (that matrix parses HSCCClient.swift).
    Welding it into the path instead — `"/v1/metrics/history?tier=1"` — is the
    exact defect that hid the `profile` filter on /v1/sessions: URLComponents
    percent-encodes the `?`, the handler never sees the query, and every refresh
    400s while the call still compiles.
    """
    src = METRICS_WIDGET_CLIENT.read_text()
    assert 'static let metricsHistory = "/v1/metrics/history"' in src, (
        "the history path must stay a bare path with no query welded into it")
    body = src[src.index("func metricsHistory("):]
    body = body[:body.index("\n    func ") + 1] if "\n    func " in body[10:] else body
    sent = set(re.findall(r'URLQueryItem\(name:\s*"([^"]+)"', body))
    for param in ("tier", "host", "node", "since", "until"):
        assert param in sent, (
            f"metricsHistory() does not transmit `{param}` as a URLQueryItem "
            f"(sends {sorted(sent)})")
    assert "WidgetDataPath.metricsHistory" in body, (
        "the read must go through the allowlisted path constant, not a literal")


# --------------------------------------------------------------------------- #
# The CANONICAL history route's shape (t_b611b56e)
# --------------------------------------------------------------------------- #
#
# The same three assertions as the section above, re-expressed against
# `ClusterHistory*` — which is what `t_447cc7fc` ruled canonical and what
# `t_2c629557` leaves standing. They are deliberately independent of
# `test_routes_metrics.py` and of the metrics-history fixtures: the day the
# losing route's tests are deleted, these must still be the gate, so nothing here
# imports from that file or names its path.
#
# Why the same three, and not new ones: a metric on this route is optional in
# Swift by design (the server writes `null` for "this node did not report it"),
# so a key the route renames, drops or adds decodes to `nil` on the phone. No
# exception, no 4xx, no red test — just a chart with a hole in it. These three
# are what make that class of drift fail loudly:
#
#   1. every key the route emits for a row is declared by the Swift decoder (or
#      provably unrequestable),
#   2. a bucket's average and its peak are different numbers, and a host that
#      never answered is null and not zero,
#   3. `clusterHistory()` transmits every parameter it supports as a real
#      `URLQueryItem` through the allowlisted path constant — welding a query into
#      the path is the defect class that hid the `/v1/sessions` `profile` filter,
#      and it compiles fine while every refresh 400s.

CLUSTER_HISTORY_BASE = 1_794_999_900        # aligned to 2 s, 60 s AND 300 s
CH_HOST_A, CH_HOST_B = "192.0.2.10", "192.0.2.11"    # TEST-NET-1, never a node
CH_PORT = "8000"
CH_MODEL = "org/Qwen-Test"

#: Row keys that are bucket metadata rather than a fact a chart plots: the tier
#: and the bucket width, both of which the response already states at the top
#: level. Everything else in a row has to reach the phone to be worth sending.
CH_ROW_METADATA_KEYS = {"tier", "res"}


def _ch_sample(ts, host, **over):
    s = {"timestamp": str(int(ts)), "hostname": host,
         "cpu_usage_pct": "10.0", "cpu_temp_c": "44",
         "gpu_util_pct": "80", "gpu_temp_c": "60", "gpu_power_w": "120.0",
         "mem_used_pct": "30", "swap_used_mb": "100", "swap_total_mb": "1000",
         "swap_used_pct": "", "gpu_mem_used_mb": "40000",
         "gpu_clock_mhz": "1800", "net_recv_bytes_s": "1000",
         "net_send_bytes_s": "2000", "sparkrun_jobs": "1"}
    s.update(over)
    return s


def _ch_frame(ts, host=CH_HOST_A, gpu_temp="60", gpu_power="120.0",
              error=None, sample=True):
    """A `sparkrun cluster monitor --json` frame, in the collector's CSV names.

    An empty string is "not reported", never a zero — which is the half the
    null-preservation assertion is about.
    """
    entry = {"host": host, "error": error, "sample": None,
             "workloads": [], "used_slots": 0, "free_slots": 1}
    if sample:
        entry["sample"] = _ch_sample(ts, host, gpu_temp_c=gpu_temp,
                                     gpu_power_w=gpu_power)
    return {"timestamp": float(ts), "hosts": [entry]}


def _ch_serving(ts):
    return [{"node": CH_HOST_A, "port": CH_PORT, "model": CH_MODEL,
             "running": 2, "waiting": 1, "kv": 0.125, "gen_tps": 31.5,
             "prompt_tps": 12.0}]


def _cluster_history_rows(tmp_path):
    """Fill the store through the recorder's OWN write path, then read tier 1
    back through the canonical route. Returns the payload.

    The ticks are the whole point: host A answers twice inside ONE 60 s bucket at
    60 °C / 120 W and then 90 °C / 200 W, so that bucket's average (75 °C, 160 W)
    and its peak (90 °C, 200 W) are different numbers and no route can confuse one
    for the other; host B fails in the same bucket, so its row is nulls plus an
    error. Rows arrive via `MetricsRecorder.tick` -> `rollup` -> `write_rollup`,
    as in production, and the route's clock is pinned so the default tier-1 span
    cannot drift past the fixture.
    """
    import sys
    import types
    daemon = Path(_REPO_ROOT) / "hscc_daemon"
    if str(daemon) not in sys.path:
        sys.path.insert(0, str(daemon))
    import metrics_history as mh
    import routes_cluster

    base = CLUSTER_HISTORY_BASE
    rec = mh.MetricsRecorder(hscc_dir=str(tmp_path))
    rec.tick(_ch_frame(base + 10, gpu_temp="60", gpu_power="120.0"),
             _ch_serving(base + 10), base + 10)
    rec.tick(_ch_frame(base + 10, host=CH_HOST_B, error="collect failed",
                       sample=False), None, base + 10)
    rec.tick(_ch_frame(base + 40, gpu_temp="90", gpu_power="200.0"),
             _ch_serving(base + 40), base + 40)
    rec.tick(_ch_frame(base + 70), _ch_serving(base + 70), base + 70)  # closes :00
    rec.store.close()

    original = routes_cluster._history_now
    routes_cluster._history_now = lambda: float(base + 700)
    try:
        ctx = types.SimpleNamespace(hscc_dir=str(tmp_path))
        status, payload = routes_cluster.handle_cluster_history(
            None, ctx, {"tier": "1"}, b"")
    finally:
        routes_cluster._history_now = original
    assert status == 200, payload
    return payload


def _cluster_host_row(payload, host, ts):
    rows = [r for r in payload["hosts"]
            if r.get("host") == host and r.get("ts") == ts]
    assert rows, f"no {host} row at bucket {ts} in %r" % (payload["hosts"],)
    return rows[0]


def _cluster_history_requestable():
    """The metric names a widget can ask for — the raw values of Swift's
    `ClusterHistoryMetric`, parsed from the source rather than copied.

    This set, not the recorder's full column list, is the honest boundary for
    "must the decoder declare it?": a key no case names can never reach a chart,
    while a key a case names is one `metrics=` request away from being plotted.
    """
    src = (Path(_REPO_ROOT) / "ios-app/Sources/Shared/ClusterHistorySeries.swift"
           ).read_text()
    start = src.index("enum ClusterHistoryMetric:")
    end = src.find("\n    var ", start)
    region = src[start:end if end > start else len(src)]
    return set(re.findall(r'\bcase\s+\w+\s*=\s*"([a-z0-9_]+)"', region))


def _swift_struct_field_types(source, struct):
    """{field: declared type} for one Decodable struct, straight from the source.

    Derived rather than copied so the gate cannot drift, and it answers the
    question `_swift_decodable_fields` cannot: whether a field is optional.
    """
    for name, body in _METRICS_STRUCT_RE.findall(source):
        if name == struct:
            return {m.group(1): m.group(2).strip()
                    for m in re.finditer(r"^\s*let\s+(\w+)\s*:\s*([^{;\n]+)",
                                         body, re.M)}
    return {}


def test_cluster_history_row_keys_are_declared_in_swift(tmp_path):
    """1/3 — every key the route puts in a row must exist in the Swift decoder.

    `ClusterHistoryPoint` is deliberately sparse: the recorder carries every field
    it samples and a model that declared all of them would fail to decode the
    first time the recorder adds one. So the assertion is two-sided rather than
    blanket — a key a widget CAN request must be declared, and a key the route
    emits must be either declared or provably unrequestable. That is the version
    with teeth: an undeclared `gpu_temp_c` fails, and so does a new recorder
    column that someone adds to `ClusterHistoryMetric` without adding to the
    model.
    """
    import sys
    daemon = Path(_REPO_ROOT) / "hscc_daemon"
    if str(daemon) not in sys.path:
        sys.path.insert(0, str(daemon))
    import metrics_history as mh

    structs = _swift_decodable_fields(METRICS_SHARED_MODELS.read_text())
    declared = structs.get("ClusterHistoryPoint", set())
    assert declared, (
        "ClusterHistoryPoint vanished from SharedModels.swift — the graph widget "
        "can no longer decode the canonical history route at all")

    requestable = _cluster_history_requestable()
    assert requestable, (
        "ClusterHistoryMetric's raw values could not be parsed — the gate would "
        "be comparing an empty set and passing on nothing")
    advertised_but_undeclared = sorted(requestable - declared)
    assert not advertised_but_undeclared, (
        "the widget can request %s but the decoder does not declare it, so "
        "metrics=%s plots a flat empty series with no error anywhere"
        % (", ".join(advertised_but_undeclared), advertised_but_undeclared[0]))

    payload = _cluster_history_rows(tmp_path)
    assert payload["hosts"] and payload["engines"], (
        "fixture produced no rows, so this test would pass on nothing: %r"
        % payload)
    # A recorder column no `ClusterHistoryMetric` case names cannot reach a chart,
    # so the sparse model is allowed to omit it. Anything else is a hole.
    never_requestable = (set(mh.HOST_COLUMNS) | set(mh.ENGINE_KEYS)) - requestable
    for label, rows in (("host", payload["hosts"]), ("engine", payload["engines"])):
        emitted = {k for row in rows for k in row}
        unexplained = sorted(emitted - declared - CH_ROW_METADATA_KEYS
                             - never_requestable)
        assert not unexplained, (
            "the route emits %s key(s) the Swift decoder does not declare, so "
            "they decode to nil on the phone and the chart draws a hole with no "
            "error anywhere: %s" % (label, ", ".join(unexplained)))


def test_cluster_history_average_is_not_its_peak_and_a_silent_host_stays_null(
        tmp_path):
    """2/3 — the peak is the fact a chart is usually drawn to show, and `null` is
    a missing reading rather than an idle node.

    The fixture makes average and peak differ on purpose (60 °C then 90 °C in one
    bucket): a route that copied the average into the peak column, or dropped the
    peak, fails HERE rather than in a screenshot. And host B never answered, so a
    `0.0` where its temperature should be would draw a dead node as a quiet one.
    """
    payload = _cluster_history_rows(tmp_path)
    row = _cluster_host_row(payload, CH_HOST_A, CLUSTER_HISTORY_BASE)

    assert row["gpu_temp_c"] == 75.0, row["gpu_temp_c"]
    assert row["gpu_temp_c_max"] == 90.0, row["gpu_temp_c_max"]
    assert row["gpu_temp_c"] != row["gpu_temp_c_max"], (
        "the fixture stopped making average and peak differ, so this assertion "
        "would pass on a route that copies one into the other")
    assert row["gpu_power_w"] == 160.0 and row["gpu_power_w_max"] == 200.0

    # The peak columns are also named, not merely present: a surface that checks
    # the vocabulary before drawing would otherwise skip them.
    assert payload["max_fields"] == ["cpu_temp_c_max", "gpu_temp_c_max",
                                     "gpu_power_w_max"]
    for peak in payload["max_fields"]:
        assert peak in row, "%s is named but not carried" % peak
        assert peak in row and row[peak] is not None

    failed = _cluster_host_row(payload, CH_HOST_B, CLUSTER_HISTORY_BASE)
    assert failed["error"] == "collect failed"
    for key in ("gpu_temp_c", "gpu_temp_c_max", "gpu_power_w", "cpu_usage_pct",
                "mem_used_pct"):
        assert failed[key] is None, "%s arrived as %r, not null" % (key,
                                                                    failed[key])

    # ...and the decoder half of the same fact: every field the recorder can
    # legitimately leave null is declared OPTIONAL. A non-optional `Double` there
    # is worse than a missing key — it either fails the decode for the whole
    # payload or, with a default, turns "no answer" into a plotted 0.
    declared = _swift_struct_field_types(METRICS_SHARED_MODELS.read_text(),
                                         "ClusterHistoryPoint")
    assert declared, "ClusterHistoryPoint has no fields to inspect"
    for field in ("gpu_temp_c", "gpu_temp_c_max", "gpu_power_w",
                  "gpu_power_w_max", "cpu_temp_c", "cpu_usage_pct",
                  "mem_used_pct", "gpu_mem_used_mb", "kv", "gen_tps",
                  "prompt_tps", "run", "wait", "n", "error"):
        assert field in declared, "%s is not declared in ClusterHistoryPoint" % field
        assert declared[field].endswith("?"), (
            "%s is declared `%s`: the recorder stores null for a reading the node "
            "never gave, and a non-optional field turns that into a zero on the "
            "chart" % (field, declared[field]))


def test_cluster_history_sends_every_parameter_as_a_real_query_item():
    """3/3 — `clusterHistory()` transmits each of its six parameters properly.

    `tier` and `since`/`until` are the ones a 400 is waiting for, and `window` is
    the one the tier axis must NOT send (the route rejects both axes at once). The
    widget client is outside `REQUIRED_QUERY_PARAMS`, which parses HSCCClient, so
    this is the only place the class of defect gets caught: a query welded into
    the path is percent-encoded by URLComponents into `%3F`, the handler never
    sees it, and the call compiles and 400s on every refresh forever.
    """
    src = METRICS_WIDGET_CLIENT.read_text()
    assert 'static let clusterHistory = "/v1/cluster/history"' in src, (
        "the canonical history path must stay a bare path with no query welded "
        "into it")
    assert not re.search(r'static let clusterHistory\s*=\s*"[^"]*\?', src), (
        "a `?` in the path constant is the escaped-query defect class")

    start = src.index("func clusterHistory(")
    rest = src[start:]
    end = rest.find("\n    func ", 10)
    body = rest[:end] if end > 0 else rest
    sent = set(re.findall(r'URLQueryItem\(name:\s*"([^"]+)"', body))
    for param in ("window", "tier", "since", "until", "node", "metrics"):
        assert param in sent, (
            "clusterHistory() does not transmit `%s` as a URLQueryItem "
            "(sends %s)" % (param, sorted(sent)))
    assert "WidgetDataPath.clusterHistory" in body, (
        "the read must go through the allowlisted path constant, not a literal")
    assert '"/v1/cluster/history"' not in body, (
        "the call site must use WidgetDataPath.clusterHistory; a literal path "
        "here bypasses the closed allowlist that makes 'no mutation calls' a "
        "property of this file")
    # The axes are mutually exclusive on the route, so the client must be able to
    # drop the defaulted `window` rather than send both and 400.
    assert re.search(r"if let tier", body) and re.search(r"} else if let window",
                                                        body), (
        "clusterHistory() must send `tier` (with since/until) OR `window`, never "
        "both — the route 400s the pair")
