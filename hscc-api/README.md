# hscc-api

The HSCC HTTP API — a pure-stdlib, bearer-token-authenticated, Tailscale-optional
JSON API that exposes HSCC cluster state, project/kanban dispatch, and operator
surfaces (template, profiles, sessions, memory, logs, activity, orchestration)
to external apps such as the private iOS companion app.

It runs in the background via the `hscc api` CLI verb, as its own process with
its own PID + log files (`~/.hscc/api.pid`, `~/.hscc/api.log`) separate from the
monitoring daemon.

This started as **Phase A1** (server skeleton, token auth, bind/config
resolution, the JSON error contract, and the API's own liveness endpoint) and
has since grown the A2/A3/A4/C2+ route surface. The authoritative listing of
every live endpoint is the route table itself: each `/v1/...` endpoint is a
`(method, path_regex, handler)` tuple registered into `api_server.ROUTES`
(the `routes_*.py` modules append to it at import). The families:

| Area | Example endpoints |
|---|---|
| cluster | `/v1/cluster/status`, `/v1/cluster/up`, `/v1/cluster/down`, `/v1/cluster/stop` |
| project + kanban (read) | `/v1/projects`, `/v1/cards`, `/v1/standup`, `/v1/review/queue`, `/v1/qa/queue`, `/v1/kanban/blocked`, `/v1/kanban/stale`, `/v1/kanban/running` |
| actions (mutations, confirm-gated) | `POST /v1/cards`, `/v1/review/{id}/merge`, `/v1/template/apply`, `/v1/cards/{id}/comment`, `/v1/cards/{id}/block|close` |
| orchestrator chat | `POST /v1/orchestrator/chat` (+ `/stop`, `/{id}` poll) |
| template | `/v1/template/list`, `/v1/template/status`, `/v1/template/preview/{name}` |
| profiles / sessions / memory | `/v1/profiles/*`, `/v1/profile/*`, `/v1/sessions`, `/v1/memory` (list/delete/edit) |
| ops + observability | `/v1/verify`, `/v1/daemon/status`, `/v1/triggers`, `/v1/escalate`, `/v1/logs`, `/v1/daemon/history`, `/v1/activity/feed`, `/v1/cron/list`, `/v1/commands` |
| session events | `/v1/projects/{name}/session/events` (history) + WebSocket `/v1/projects/{name}/session/ws` (live) |

Phases: cluster read (A2), project/kanban read (A3), mutating confirm-gated
actions (A4), the `hscc api` CLI verb (A5), conversational orchestrator chat
(C2), and the full surface/observability routes shipped since.

## Layout

- `api_server.py` — the server core:
  - `ThreadingHTTPServer` (`_ApiServer`) + `ApiHandler(BaseHTTPRequestHandler)`,
    `HTTP/1.1`, WebSocket-upgrade aware.
  - `ROUTES` — the `(method, path_regex, handler)` table; a handler is a plain
    function `(server, ctx, query, body) -> (status, payload_dict)`.
  - `load_token()` / `token_valid()` — 0600 token file at `~/.hscc/api-token`,
    generated on first run, compared with `hmac.compare_digest`, fail-closed.
  - `resolve_config()` / `resolve_bind()` / `_find_tailnet_ip()` — bind
    resolution. Loopback by default; tailnet is opt-in; `0.0.0.0` is always
    refused.
  - `ApiError` + the `error_*` constructors — the unified JSON error shape.
- `routes_*.py` — one module per endpoint family (cluster, project, kanban,
  actions, orchestrator, template, profiles, sessions, memory, logs, activity,
  commands, cron, history, autodown, ops, bootstrap, ws…). Each registers its
  routes into `ROUTES` at import.
- `gateway_driver.py`, `session_event.py`, `ws_frame.py` — supporting modules.
- `tests/` — hermetic unit tests (bind loopback port 0, never a fixed public
  port); `tests_gateway/` — the gateway-driver tests.

## CLI — `hscc api`

```
hscc api start [--tailscale] [--bind <ip>] [--port <n>] [--no-qr]  start in the background
hscc api stop                                                      stop it
hscc api status [--no-qr]                                          running/stopped + bound host:port
```

`start`/`status` print a scannable connection QR (suppressed by `--no-qr`).
The token is **encoded into that QR**, so the QR must be treated like a
password — never show it on a stream or screen-share. `start` also runs the
repo-vs-installed payload-drift check and warns loudly on drift.

## Auth

Every request — reads included — must carry `Authorization: Bearer <token>`.
The token lives at `~/.hscc/api-token`, generated on first start with
`secrets.token_urlsafe(32)` and written with mode 0600 (never briefly
world-readable). If the token file exists but is unreadable or empty, the
server REFUSES TO START (fail-closed) — it never falls back to "no auth" and
never silently regenerates (which would strand an existing client). The token
value is never logged or echoed.

## Bind / config

Precedence (lowest → highest): defaults → `~/.hscc/api.json` → explicit
overrides passed to `create_server()` / the CLI flags.

- `bind`: `"loopback"` (default → `127.0.0.1`) | `"tailscale"` (resolve the
  tailnet IPv4; hard error if none found) | an explicit IP string.
  **`0.0.0.0` / `::` are ALWAYS refused** — the API can start/stop GPU work and
  must never be reachable from an untrusted LAN.
- `port`: default **8787**.
- Tailscale here is the App Store build, so the CLI is *not* on PATH — it lives
  at `/Applications/Tailscale.app/Contents/MacOS/Tailscale`. Both that path and
  a bare `tailscale` are probed, plus the interfaces for a `100.x` address.

## Error contract

Every error response is:

```json
{ "error": { "code": "...", "message": "...", "speak": "..." } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | malformed body, missing field, body too large |
| 401 | `unauthorized` | missing/wrong Bearer token |
| 404 | `not_found` | unknown route / version |
| 405 | `method_not_allowed` | valid path, wrong method |
| 409 | `confirm_required` | mutating call without `confirm: true` |
| 500 | `internal_error` | unhandled exception (traceback logged server-side only) |

Errors never leak the token or a raw traceback. Request bodies over 1 MiB are
rejected with 400. A 500 logs the traceback to the API log and returns a neutral
message pointing at `~/.hscc/api.log`.

## Tests

```bash
# From the hscc repo root:
scripts/run_tests.sh
```

(The suite is hermetic: servers bind loopback port 0 (ephemeral), tokens are
generated in `tmp_path` dirs, and no fixed public port or live tailnet is used.)
`hscc-api` is in `scripts/run_tests.sh`'s `DIRS=()`, so it runs with every other
plugin in the repo's standard gate.
