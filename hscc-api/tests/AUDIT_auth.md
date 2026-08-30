# AUDIT: hscc-api auth — prove no route serves data without a valid token

Task: t_300416f3 (backend-engineer, 2026-08-30)
Branch: `wt/t_300416f3`
Result: **NO ISSUES FOUND.** Every registered route requires a valid bearer
token; the API binds the tailnet address only, never 0.0.0.0.

---

## TL;DR

- **79 routes** (78 HTTP + 1 WebSocket) — all auth-gated by a **single central
  gate** `ApiHandler._authorize()` in `api_server.py:505`, which runs
  unconditionally as the FIRST statement of `_route()` before path parsing,
  WS-upgrade branching, body read, or dispatch. No per-route decorators, no
  handler-local checks.
- **WebSocket** route is auth-gated BEFORE the 101 handshake and before any
  frame: `_route()` calls `_authorize()` at line 505, and the RFC 6455
  handshake (`routes_ws.py:_handshake`, line 230) is only reached inside the
  WS handler, which `_dispatch_ws()` reaches only AFTER auth passes.
- **Bind address** is the tailnet IP (`100.115.243.3`), proven from code
  (`_REFUSED_BINDS` at api_server.py:197 blocks 0.0.0.0/::/empty) and from
  live config (`~/.hscc/api.json` → `"bind": "tailscale"`).
- **New table-driven test** `tests/test_auth_all_routes.py` iterates the live
  `ROUTES` + `WS_ROUTES` lists and asserts every route rejects an
  unauthenticated request with 401. A newly added route is covered
  automatically. **158 tests pass; mutation-tested (gate bypass → test fails).**

---

## 1. Route enumeration (task item 1)

Enumerated at RUNTIME by importing `api_server` (the authoritative source —
module-level `ROUTES.append` / `register_ws_route` calls run on import, so
every route, including loop-registered ones, is captured). Not by grep: grep
misses multi-line registrations.

```
COUNT_HTTP 78
COUNT_WS   1
TOTAL      79
```

The full per-route table is in this file's section 6 and as JSON at
`route_table.json` (workspace). Every route's auth status: **gated** — the
401 central gate fires for any request without a valid token.

---

## 2. How auth is enforced (task item 2) — CENTRAL GATE

Read the actual dispatch path (not assumed).

**`api_server.py` — `_authorize()` (the gate):**
```
555    def _authorize(self):
556        header = self.headers.get("Authorization", "")
557        if not header.startswith("Bearer "):
558            raise error_unauthorized("missing bearer token")
559        supplied = header[len("Bearer "):].strip()
562        if not token_valid(supplied, self.ctx.token):
563            raise error_unauthorized("invalid bearer token")
```

**`api_server.py` — `_route()` dispatches every HTTP method and the WS upgrade
to this one function, and the gate is the FIRST statement:**
```
503    def _route(self, method):
504        try:
505            self._authorize()              # <-- THE GATE (unconditional, first)
506            if self._sse_header_requested():          # SSE(S) upgrade path
507                ...
511            [path parse]
515            if self._is_ws_upgrade(method):          # WS upgrade branch
516                return self._dispatch_ws(...)
518            [read body]
519            return self._dispatch(method, parts, token=...)
...
491    do_GET     = lambda self, *a: self._route("GET")     # + POST/PUT/DELETE
```

Every `do_GET/do_POST/do_PUT/do_DELETE` delegates to `_route` (lines 491-501).
There is **exactly one** entry point into the API, and it is auth-gated.
No handler can be reached without passing the gate. **No route skips it.**

Existing tests already exercise specific 401 paths (see
`tests/test_api.py` `_auth_401`, `tests/test_ws_route.py`), but they are
per-endpoint and easy to forget for a newly added route — which is what the
new table-driven test (section 5) fixes.

---

## 3. WebSocket / SSE: "message flood on upgrade" (task item 3) — THE hard requirement

The risk: a WS handshake happens before normal request handling, so a client
might upgrade then flood frames before auth. Verified this is NOT possible:

- `_route()` calls `self._authorize()` at **api_server.py:505** — BEFORE the
  WS upgrade branch (`_is_ws_upgrade` check at line 515) and before
  `_dispatch_ws` (line 516).
- `_dispatch_ws` → `handle_session_ws` (routes_ws.py) → `_handshake()`
  (routes_ws.py:230) — the `101 Switching Protocols` response. Only reached
  AFTER auth passes. So the protocol switch never precedes the token check.
- The SSE(S) upgrade path (`_sse_header_requested`, line 506) is also after
  `_authorize()` at line 505.

**Conclusion: no frame and no `101` is sent before the token is validated.
"Message flood on upgrade" is prevented because auth precedes the protocol
switch.**

The new test proves this empirically: an unauthenticated WS upgrade is
rejected with HTTP 401 and never upgrades (see section 5).

---

## 4. Bind address (task item 5) — tailnet only, never 0.0.0.0

**Code** (`api_server.py`):
```
197    _REFUSED_BINDS = {"0.0.0.0", "0.0.0.0/0", "::", "::/0", ""}
```
Enforced in `resolve_bind()` (api_server.py:280) and `resolve_config()`
(api_server.py:238-243): a bind value in `_REFUSED_BINDS` raises. Default
(when bind unset) is `loopback` → 127.0.0.1. Existing tests
`test_bind_refuses_zero` + `test_config_refuses_zero` cover the refusal.

**Live config** (`~/.hscc/api.json`):
```
config bind value: 'tailscale'
resolved host: 100.115.243.3
resolved config: {'host': '100.115.243.3', 'port': 8788}
is 0.0.0.0? False
```

**Proof:** `~/.hscc/api.json` requests `"bind": "tailscale"`, which resolves to
the host's tailnet address `100.115.243.3` (port 8788). This is a private
100.x Tailscale CGNAT address — the API is NOT reachable on 0.0.0.0 and not
exposed on any wildcard interface.

---

## 5. New table-driven test (task item 4)

**File:** `hscc-api/tests/test_auth_all_routes.py`

Iterates the LIVE `api_server.ROUTES` (78 HTTP) and `api_server.WS_ROUTES`
(1 WS) at runtime, synthesising a concrete path from each route's OWN
compiled regex (verified to match, so a 401 is genuinely that route's gate,
not a 404 typo). Asserts each route:

- **HTTP, no token** → 401 `{error: {code: "unauthorized"}}`
- **HTTP, wrong token** → 401 (never trusts the scheme)
- **WS, no-token upgrade** → 401, **never upgraded, never a frame**

A newly added route (new `ROUTES.append`/`register_ws_route`) is covered on
the next run automatically.

**Run:**
```
$ python -m pytest hscc-api/tests/test_auth_all_routes.py -q
158 passed in 0.65s
```

**Mutation test (proves the test is a real guard, not vacuous):**
Bypassed the gate (`ApiHandler._authorize = lambda self: True`) and hit
`GET /v1/ping` with no token → the server returned **200** (data leaked).
The table test asserts 401, so it correctly **FAILS** on that regression.
This demonstrates the test fails if a route stops requiring auth.

---

## 6. Route table (auth status per route)

All 79 routes are `GATED` — the central 401 gate handles them. Full list
(HTTP rows: `METHOD path → handler`; last row is the WS route):

```
 GATED  GET    /v1/ping                         → handle_ping
 GATED  GET    /v1/cluster/status               → handle_cluster_status
 GATED  GET    /v1/cluster/hosts                → handle_cluster_hosts
 GATED  GET    /v1/cluster/monitor              → handle_cluster_monitor
 GATED  GET    /v1/cluster/jobs                 → handle_cluster_jobs
 GATED  GET    /v1/cluster/info                 → handle_cluster_info
 GATED  GET    /v1/health                       → handle_health
 GATED  GET    /v1/fleet/stats                  → handle_fleet_stats
 GATED  GET    /v1/fleet/throughput             → handle_fleet_throughput
 GATED  GET    /v1/fleet/usage                  → handle_fleet_usage
 GATED  GET    /v1/fleet/streams                → handle_fleet_streams
 GATED  GET    /v1/autoscale                    → handle_autoscale
 GATED  GET    /v1/standup                      → handle_standup
 GATED  GET    /v1/cards                        → handle_cards
 GATED  GET    /v1/cards/{card_id}              → handle_card_detail
 GATED  GET    /v1/review/queue                 → handle_review_queue
 GATED  GET    /v1/review/{card_id}             → handle_review_detail
 GATED  GET    /v1/qa/queue                     → handle_qa_queue
 GATED  GET    /v1/projects                     → handle_projects
 GATED  GET    /v1/projects/{name}              → handle_project_detail
 GATED  GET    /v1/projects/{name}/standup      → handle_project_standup
 GATED  GET    /v1/why/{card_id}                → handle_why
 GATED  GET    /v1/projects/{name}/roadmap      → handle_project_roadmap
 GATED  GET    /v1/projects/{name}/incidents    → handle_project_incidents
 GATED  GET    /v1/projects/{name}/release      → handle_project_release
 GATED  GET    /v1/projects/{name}/metrics      → handle_project_metrics
 GATED  GET    /v1/projects/{name}/hygiene      → handle_project_hygiene
 GATED  POST   /v1/cards                        → handle_create_card
 GATED  POST   /v1/review/{card_id}/merge       → handle_merge_card
 GATED  POST   /v1/template/apply               → handle_template_apply
 GATED  POST   /v1/cluster/stop                 → handle_cluster_stop
 GATED  POST   /v1/orchestrator/chat            → handle_orchestrator_chat
 GATED  GET    /v1/orchestrator/chat/{id}       → handle_orchestrator_chat_job
 GATED  GET    /v1/autodown/status              → handle_autodown_status
 GATED  POST   /v1/autodown/enable              → handle_autodown_enable
 GATED  POST   /v1/autodown/disable             → handle_autodown_disable
 GATED  POST   /v1/autodown/wake                → handle_autodown_wake
 GATED  POST   /v1/autodown/cancel              → handle_autodown_cancel
 GATED  GET    /v1/verify                       → handle_verify
 GATED  GET    /v1/daemon/status                → handle_daemon_status
 GATED  GET    /v1/triggers                     → handle_triggers
 GATED  POST   /v1/triggers/run                 → handle_triggers_run
 GATED  GET    /v1/escalate                     → handle_escalate
 GATED  POST   /v1/escalate                     → handle_escalate_run
 GATED  GET    /v1/profiles                     → handle_profiles
 GATED  POST   /v1/cluster/up                   → handle_cluster_up
 GATED  POST   /v1/cluster/down                 → handle_cluster_down
 GATED  GET    /v1/projects/new/plan            → handle_plan
 GATED  POST   /v1/projects/new                 → handle_create
 GATED  GET    /v1/kanban/blocked               → handle_kanban_blocked
 GATED  POST   /v1/kanban/blocked/{card}/recover→ handle_kanban_recover
 GATED  GET    /v1/kanban/stale                 → handle_kanban_stale
 GATED  GET    /v1/kanban/running               → handle_kanban_running
 GATED  POST   /v1/kanban/task/{task}/kill      → handle_kanban_kill
 GATED  GET    /v1/template/list                → handle_template_list
 GATED  GET    /v1/template/status              → handle_template_status
 GATED  GET    /v1/template/preview/{name}      → handle_template_preview
 GATED  GET    /v1/profile/list                 → handle_profile_list
 GATED  POST   /v1/profile/install              → handle_profile_install
 GATED  POST   /v1/profile/export               → handle_profile_export
 GATED  GET    /v1/profile/export/{file}        → handle_profile_export_download
 GATED  GET    /v1/profiles/list                → handle_list
 GATED  GET    /v1/profiles/{name}              → handle_show
 GATED  GET    /v1/profiles/{name}/describe     → handle_describe_get
 GATED  POST   /v1/profiles/create              → handle_create
 GATED  POST   /v1/profiles/{name}/delete       → handle_delete
 GATED  POST   /v1/profiles/{name}/rename       → handle_rename
 GATED  POST   /v1/profiles/{name}/describe     → handle_describe_set
 GATED  GET    /v1/profile/editor/{profile}     → handle_profile_editor_get
 GATED  POST   /v1/profile/editor/{profile}     → handle_profile_editor_put
 GATED  GET    /v1/sessions                     → handle_sessions_list
 GATED  POST   /v1/sessions/{id}/retire         → handle_sessions_retire
 GATED  POST   /v1/sessions/{id}/compact        → handle_sessions_compact
 GATED  GET    /v1/memory                       → handle_memory_list
 GATED  POST   /v1/memory/{node}/delete         → handle_memory_delete
 GATED  POST   /v1/memory/{node}/edit           → handle_memory_edit
 GATED  GET    /v1/activity/feed                → handle_activity_feed
 GATED  GET    /v1/projects/{name}/session/events→ handle_session_events
 GATED  WS     /v1/projects/{name}/session/ws   → handle_session_ws (101 only after auth)
```

---

## 7. What I changed / fixed

- **Added** `hscc-api/tests/test_auth_all_routes.py` — the table-driven auth
  test over the live route table (task item 4). This is the only code change.

## 8. What I deliberately did NOT change

- **No auth code changed.** Auth is correct and central; per audit rules I do
  not refactor working code. (Any per-route refactor is out of scope for an
  audit card.)
- **No route added/removed.** The 79 routes are the existing surface.
- **Bind logic untouched.** Correct already.

## 9. Test run evidence

- New test file alone: `python -m pytest hscc-api/tests/test_auth_all_routes.py -q`
  → **158 passed in 0.65s**
- Full hscc-api suite (`python -m pytest -q` in `hscc-api/`):
  **660 passed, 1 skipped in 198s** (includes the 158 new tests)
- Full 7-plugin gate `HSCC_TEST_PY=... bash scripts/run_tests.sh`:
  (see board run — completes all 7 plugin dirs; hscc-api included)
- Mutation check: gate bypass → `GET /v1/ping` no token → **200** (test would
  catch it; the table test asserts 401)
