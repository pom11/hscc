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
hscc api install-service [--tailscale] [--bind <ip>] [--port <n>]  OPT-IN: write the launchd unit (boot-resilient API; loading stays manual)
hscc api uninstall-service                                         remove the launchd unit written by install-service
```

`start`/`status` print a scannable connection QR (suppressed by `--no-qr`).
The token is **encoded into that QR**, so the QR must be treated like a
password — never show it on a stream or screen-share. `start` also runs the
repo-vs-installed payload-drift check and warns loudly on drift.

`install-service` takes the same bind flags as `start` and writes them into the
unit's `ProgramArguments`, so a tailnet-served API can move onto launchd
without losing its bind; with no flags the unit is the plain loopback one.
The unit stores the intent (`--tailscale`, not a resolved address), so the
tailnet IP is re-resolved at every boot. `--no-qr` is accepted and ignored
(a launchd unit has no terminal). `hscc api status` consults
`launchctl print gui/$UID/com.hscc.api` as well as `~/.hscc/api.pid`, so an API
that launchd started at boot reads as **running**, and `hscc api stop` runs
`launchctl bootout gui/$UID/com.hscc.api` when the unit is loaded rather than
SIGTERM-ing a `KeepAlive`-supervised process that launchd would resurrect. A
bootout is a **session-scoped** stop — the unit file stays installed and
`RunAtLoad` brings the API back at the next login; `uninstall-service` removes
it for good. The unit also carries `ThrottleInterval 30` / `ExitTimeOut 10`
(like the daemon's own unit) so a tailnet unit that legitimately fails while
Tailscale is down backs off instead of respawn-storming `api.err.log`.

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

## Profile-root intent (`HSCC_API_PROFILES_ROOT`)

Every mutating route in this server writes a profile directory, and three
different resolvers answer "which one": hermes' own `get_profile_dir`, this
plugin's deliberately call-time ladder (`routes_profile._hermes_profiles_dir`),
and `rolelib.PROFILES_DIR`. They are not normally in disagreement — but
`hermes_constants._hermes_root_of` returns the NATIVE home whenever
`HERMES_HOME` resolves *under* `~/.hermes`, so a server that believes a scratch
home inside the live root sandboxed it writes (and `POST
/v1/profiles/{name}/delete` DELETES) inside the operator's real
`~/.hermes/profiles/**` and answers 200. That is the t_d39c720e class; the CLI
got its guard in PR #301/#302, this section is the same control over the HTTP
transport.

State the intent once, at server start, as an env var — the **only** channel.
There is deliberately no flag for it on an HTTP route, on `serve`'s argv, or on
`hscc api start`: `serve`'s argv is baked into a generated launchd unit, so a
flag there would be a second and less visible way to say the same thing.

```console
HSCC_API_PROFILES_ROOT=~/.hermes/profiles hscc api start   # or: python -m api_server serve
```

The value is the **profiles dir** — `<hermes-home>/profiles`, the directory
that directly contains `<profile>/config.yaml` — which is the same contract as
`rolelib.assert_profile_root(expected)`, so a CLI refusal and an API refusal are
configured and worded the same way.

| Situation | Behaviour |
|---|---|
| unset, empty or whitespace | today's behaviour, unchanged (a `VAR=` in a plist cannot brick the server) |
| set, equal to every writable root | boots; the intent is carried on `ApiContext` and printed at startup |
| set, a relative path | refused at boot — "must be an absolute path" |
| set, any writable root differs | refused at boot, **exit 2** on both entry paths (`python -m api_server serve` and the daemon's `hscc api start`): nothing bound, no token read, nothing written |
| a root drifts after boot | the write is refused per request — **503 `profile_root_mismatch`**, nothing written |
| the write is *incidental* (the compaction-threshold ensure on the chat path) | **not** a 503: the write is skipped, the divergence is logged at ERROR, and the chat proceeds on the config the operator already has |

Two gates, one implementation (`profile_root.py`): the boot assert is the primary
gate — a mis-rooted server never serves, so it never writes — and the write choke
point is what keeps "nothing written" true afterwards, because the ladder
re-points at call time by design. A refused boot exits **2** where a busy port or
an unreadable token exits **1**, so an operator can tell a misconfigured root from
a transport problem. Every boot also prints the root it will write:

```console
hscc-api: profiles root /Users/you/.hermes/profiles (intent: none)
```

Reads are deliberately not guarded: a wrong-root read degrades to an honest empty
list, it cannot corrupt a tree. The generated launchd unit carries neither
`HERMES_HOME` nor `HSCC_API_PROFILES_ROOT` — a hand-added `EnvironmentVariables`
key is honoured, the generator never adds one.

## Chat delivery: tmux-first, serve fallback (what the app must show)

A `send` on `/v1/projects/{name}/session/ws` is delivered tmux-first: if the
project's orchestrator REPL (`hermes -p <name>-orch chat`) runs in a tmux
pane, the message is typed **into that pane** (bracketed paste; `/queue`d
automatically while a turn is busy, so a running `/goal` is never cut off)
and the reply streams back via the session store-tail — one REPL is the
source of truth for terminal and phone alike. The `working` system frame
carries the receipt: `details.via="tmux"` plus `pane`, `busy`, `queued`.

Every non-tmux outcome is labelled honestly in the same frames:

| Situation | Frame |
|---|---|
| REPL in a tmux pane | `working` `via="tmux"` (+ pane/busy/queued) |
| injection itself failed | `tmux_delivery_failed` (never a silent hang) |
| which pane is the REPL is unprovable | `tmux_ambiguous` — refused, never guessed |
| **no** tmux REPL exists (serve fallback) | `working` `via="serve"` + `details.notice`: *"Your terminal REPL isn't in tmux, so this went to a background session…"* — **the app should surface `notice` verbatim**; it is the operator's only clue the terminal stayed dark |

`hscc project doctor` (its "iOS chat reachability (tmux/REPL)" panel) and
bootstrap print the same in-tmux / outside-tmux / idle split up front, so the
fallback should be rare: `hscc project chat <project>` always opens the chat
where the app can see it.

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
