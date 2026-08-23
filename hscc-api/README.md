# hscc-api

The HSCC HTTP API — a pure-stdlib, bearer-token-authenticated, Tailscale-optional
JSON API that exposes HSCC cluster state (and, in later phases, project/kanban
dispatch) to external apps such as the private iOS companion app.

This directory is **Phase A1**: the server skeleton, token auth, bind/config
resolution, the JSON error contract, and the API's own liveness endpoint.
No cluster/project/kanban endpoints yet (those are A2/A3/A4) and no `hscc api`
CLI verb yet (that's A5).

## Layout

- `api_server.py` — the whole A1 surface:
  - `ThreadingHTTPServer` (`_ApiServer`) + `ApiHandler(BaseHTTPRequestHandler)`.
  - `ROUTES` table — a list of `(method, path_regex, handler)` tuples. A2/A3/A4
    register their endpoints by ADDING to this table; a handler is a plain
    function `(server, ctx, query, body) -> (status, payload_dict)`.
  - `load_token()` / `token_valid()` — 0600 token file at `~/.hscc/api-token`,
    generated on first run, compared with `hmac.compare_digest`, fail-closed.
  - `resolve_config()` / `resolve_bind()` / `_find_tailnet_ip()` — bind
    resolution. Loopback by default; tailnet is opt-in; `0.0.0.0` is always
    refused.
  - `ApiError` + the `error_*` constructors — the unified JSON error shape.
- `tests/` — hermetic unit tests (bind loopback port 0, never a fixed public port).

## Endpoints (A1)

| Method | Path | Description |
|---|---|---|
| GET | `/v1/ping` | The API's OWN liveness. (The fleet health check backed by `hscc verify` is `/v1/health` in A2, so the API's own liveness lives at `/v1/ping` to avoid the collision.) |

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
overrides passed to `create_server()`.

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
| 409 | `confirm_required` | mutating call without `confirm: true` (A4) |
| 500 | `internal_error` | unhandled exception (traceback logged server-side only) |

Errors never leak the token or a raw traceback. Request bodies over 1 MiB are
rejected with 400. A 500 logs the traceback to the API log and returns a neutral
message pointing at `~/.hscc/api.log`.

## Tests

```bash
# From the hscc repo root:
HSCC_TEST_PY=/Users/desac/miniconda3/envs/p313/bin/python scripts/run_tests.sh
```

The suite is hermetic: servers bind loopback port 0 (ephemeral), tokens are
generated in `tmp_path` dirs, and no fixed public port or live tailnet is used.

## Verify

The repo's standard gate is `scripts/run_tests.sh` — ALL plugins must be green.
`hscc-api` is added to that script's `DIRS=()` so this suite actually runs.
