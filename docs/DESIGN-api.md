# HSCC HTTP API — design for external apps (stdlib, Tailscale-bound, token-auth, `speak` field)

**Date:** 2026-08-20
**Status:** Draft — design doc only. No implementation code is produced by this card.
**Branch:** `feat/hscc-api` (off `main`, unmerged)
**Consumes:** the existing hscc CLI dispatch chain and the flightdeck (hscc-project) command modules as *libraries* — never a shell-out + text-parse of our own CLI output.

## Purpose

Today HSCC is CLI-only: `hscc ...` → `hscc_daemon/hscc.py:main()`, with no HTTP surface
anywhere (verified: no `http.server` / `BaseHTTPRequestHandler` / `flask` / `fastapi`
in the repo). This design adds a proper HTTP API layer so OTHER APPS (starting with a
private iOS app over Tailscale) can query cluster state and dispatch/merge work.

This doc is the contract. Implementation lands in separate small cards that must be
implementable by reading this doc and the named backing functions.

## Hard constraints (non-negotiable, restated for implementation)

1. **Pure stdlib only.** `http.server`, `socketserver`, `json`, `secrets`, `hmac`
   from the Python 3.13 stdlib. NO flask/fastapi/uvicorn.
2. **Never bind 0.0.0.0.** Default bind is loopback; the Tailscale interface IP is
   opt-in via explicit config. This API can start/stop GPU workloads and dispatch agent
   work, so it must never be reachable from a coffee-shop LAN by accident. When no
   tailnet IP exists, fail with a *clear error*, never a crash and never a fallback to
   an unauthenticated/wide bind.
3. **Bearer-token auth on every endpoint**, including reads. Token at
   `~/.hscc/api-token`, generated on first start with `secrets.token_urlsafe`, file mode
   0600. Compare with `hmac.compare_digest` (constant-time), never `==`. A missing or
   unreadable token file must fail closed (refuse to start), never fall back to
   "no auth".
4. **Reuse hscc logic as libraries.** Import and call the same functions the CLI
   handlers use (documented per endpoint below). Do NOT shell out to `hscc ...` and
   parse text — that is exactly the fragility v1.8.3 removed from the DGX check.
5. **Read-only by default.** Read endpoints are safe/idempotent. Every MUTATING action
   is a distinct endpoint requiring an explicit `confirm: true` field in the request
   body, mirroring the CLI's `--apply` / `--confirm` gate. A GET never mutates.

---

## A. Endpoint contract

**Versioning:** all paths are under `/v1/...` so the iOS app can pin. Unknown versions
→ 404 (see error contract).

**Auth:** every request must carry `Authorization: Bearer <token>`. Token comparison via
`hmac.compare_digest`. All endpoints require it, reads included.

**Response envelope:** every success response is a JSON object. Every READ response
additionally carries a first-class `"speak"` string (section B) plus the structured
payload. Mutating responses carry a human `"message"` plus structured fields. Errors use
the unified error shape in section C.

### Conventions shared by all endpoints

- JSON bodies are parsed with `json.loads` on the decoded body; a malformed body → 400.
- Unknown JSON fields are ignored (forward compat); required fields are validated.
- Request bodies are limited in size (see section C, 400).
- All reads are synchronous and may be slow (cluster commands invoke `sparkrun` over
  SSH with timeouts). No streaming/async in scope (section D).

---

### Cluster

Backing library: the hscc-cluster engine, loaded exactly like `_handle_cluster` does via
`hscc_daemon/hscc.py:_load_cluster_engine()` (hscc.py:379) — insert `hscc-cluster/` on
`sys.path`, import its `hscc.py` as a module, call `cmd_*` directly. Each function
returns a plain dict (see `hscc-cluster/hscc.py`).

#### `GET /v1/cluster/status`

Read-only. Backed by `cmd_cluster_status()` (`hscc-cluster/hscc.py:71`).

Request: none (no body).
Response:
```json
{
  "workloads": [{"name": "...", "tp": "1", "pp": "1", "container_id": "..."}],
  "idle_hosts": ["192.0.2.11"],
  "total_hosts": 4,
  "speak": "4 hosts up. 2 workloads running, 1 idle."
}
```
The `workloads` / `idle_hosts` / `total_hosts` fields are copied verbatim from the
engine's return dict; `speak` is derived (section B).

#### `GET /v1/cluster/hosts`

Read-only. Backed by `cmd_hosts()` (`hscc-cluster/hscc.py:141`).

Response carries `{"hosts": [...], "saved_clusters": {...}, "live_status": {...}}`
verbatim from the engine, plus `speak`.

#### `GET /v1/cluster/monitor`

Read-only. Backed by `cmd_monitor()` (`hscc-cluster/hscc.py:182`). One CPU/RAM/GPU
snapshot across the fleet. Response carries the engine dict verbatim plus `speak`.

#### `GET /v1/cluster/jobs`

Read-only. Backed by `cmd_jobs()` (`hscc-cluster/hscc.py:203`). Lists all sparkrun jobs.
Response is the raw `run_cmd` dict (`success`/`returncode`/`output`), plus `speak`.

#### `GET /v1/cluster/info`

Read-only. Backed by `cmd_info()` (`hscc-cluster/hscc.py:213`). Detailed resolved
cluster config. Response carries the engine dict verbatim plus `speak`.

#### `POST /v1/cluster/stop` — MUTATING, confirm-gated

Backed by `cmd_stop(container_id)` (`hscc-cluster/hscc.py:208`).

Request body:
```json
{ "container_id": "1b6e77192e59", "confirm": true }
```
`confirm` must be `true`, else 409. `container_id` is required (400 when missing). On
success returns `{"message": "...", "success": true, "returncode": 0}`.

---

### Fleet / health

Backing libraries: `hscc_daemon.verify`, `hscc_daemon.stats`, `hscc_daemon.throughput`,
and the daemon state files (`~/.hscc/state/*.json`).

#### `GET /v1/health`

Read-only. Backed by `verify.run_all()` (`hscc_daemon/verify.py:262`) — the same 5-check
smoke test as `hscc verify`.

Response:
```json
{
  "ok": true,
  "checks": [{"name": "...", "ok": true, "detail": "..."}],
  "speak": "All 5 checks passed. Cluster health is good."
}
```

#### `GET /v1/fleet/stats?days=7`

Read-only. Backed by `compute_stats(since_days=days)` (`hscc_daemon/stats.py:43`). The
`days` query param is optional, default 7, clamped ≥ 0 (mirror `_handle_stats` at
hscc.py:531). Response carries the computed stats dict verbatim plus `speak`.

#### `GET /v1/fleet/throughput`

Read-only. Backed by `compute_throughput()` (`hscc_daemon/throughput.py:92`). Response
carries the throughput dict verbatim plus `speak`.

#### `GET /v1/fleet/streams`

Read-only. **Needs new logic** — there is no single existing function that returns all
daemon stream states as one dict; the daemon's `cmd_status`/`cmd_check` print them
rather than returning them. Back it with
`hscc_daemon.state:read_all_states()` (returns the `~/.hscc/state/*.json` dicts) plus a
small aggregation helper the API server owns. This is a thin read of existing state
files, not new monitoring logic. Response:
```json
{
  "streams": {"dgx": {"ok": true, "timestamp": "..."}, "gateway": {...}},
  "speak": "Daemon streams: dgx ok, gateway ok — nothing blocked."
}
```

#### `GET /v1/autoscale`

Read-only. Backed by `autoscale.decide_scale(tp, current_workers=...)` as composed in
`_handle_autoscale` (hscc.py:571) — always a *decision*, never an action. Response:
`{"action": "none|scale_up|scale_down", "reason": "...", "target": N?, "speak": "..."}`.

---

### Projects / kanban (via hscc-project, the relocated flightdeck)

Backing library: the `hscc-project/` package, put on `sys.path` exactly as
`_handle_project` does (hscc.py:644), then import `flightdeck.*` directly and call the
command *data* functions (not `flightdeck.cli.main`'s stdout). The registry path can be
overridden via the same `--registry` default as flightdeck
(`flightdeck.core.registry.DEFAULT_REGISTRY`) — the API server reads config for it.

Note on reuse: several flightdeck commands (review `--apply`, qa) currently *print*
rather than *return* structured results from their `run()` entry. For the API we call
their **data-gathering internals** (`gather_data`, `_collect`, `_enrich_project_cards`,
`review_queue`, `create_task`) directly and render JSON ourselves. Where a mutation's
logic only exists inside a printing command (review `--apply`), the implementation card
must add a thin return-value seam — flagged explicitly at each such endpoint.

#### `GET /v1/standup`

Read-only. Backed by `flightdeck.commands.standup.gather_data(registry_path)`
(`hscc-project/flightdeck/commands/standup.py:53`) — the same one call `flightdeck
standup` uses. Response is that dict verbatim plus `speak`.

#### `GET /v1/cards?board=default&status=running`

Read-only. Backed by `flightdeck.core.kanban.list_cards(board=..., include_archived=...)`
(`hscc-project/flightdeck/core/kanban.py:277`), the same call `standup`/`review`/`qa`
use. Query params optional; when omitted, `list_cards()` with no board reads every
board (matching the CLI default). `status` filters client-side (optional). Response:
`{"cards": [...], "count": N, "speak": "..."}`. Cards are the full flightdeck card dicts.

#### `GET /v1/cards/{card_id}`

Read-only. Backed by `flightdeck.core.kanban.find_card(card_id)`
(`hscc-project/flightdeck/core/kanban.py:596`) across all boards. Response is the card
dict verbatim plus `speak`. 404 if not found (`{"error": {...}, "speak": "..."}`).

#### `GET /v1/review/queue`

Read-only. The review queue. Data gathering = `flightdeck.commands.review._enrich_project_cards(projects, _run)`
(`review.py:555`) then `flightdeck.core.review.review_queue(enriched, now=...)`
(`hscc-project/flightdeck/core/review.py` — see `cmd_queue` at review.py:588). Response:
`{"queue": [{project, card_id, branch, age_seconds, title}...], "count": N, "speak": "..."}`.

#### `GET /v1/qa/queue`

Read-only. The manual-QA queue. Data gathering = `flightdeck.commands.qa._collect(cards, projects)`
(`qa.py:196`) plus `qa._load_manual()` (`qa.py:450`). Response:
`{"queue": [...], "manual_qa": [...], "speak": "..."}` — the same two lists
`qa._render_json` (`qa.py:346`) produces.

#### `GET /v1/review/{card_id}`

Read-only. Resolve + show one card's review facts. Data gathering =
`flightdeck.commands.review._resolve` + `_branch_facts` + `_verify_line`
(review.py:182/218/123), the same path `cmd_review` dry-run uses (review.py:409). A dry
run mutates nothing. Response mirrors `_render_json` (review.py:378) plus `speak`.
404 when the card does not resolve.

---

### Actions (mutating, confirm-gated)

Every action below requires `"confirm": true` in the body or returns 409. None is
reachable via GET.

#### `POST /v1/cards` — dispatch a card

Backed by `flightdeck.core.kanban.create_task(board, title, assignee=..., body=..., ...)`
(`hscc-project/flightdeck/core/kanban.py:966`) — the same call the decompose `--apply`
path and `migrate-card` use. **Direct dispatch as a first-class action is flagged as
mostly-existing, thin seam:** `create_task` already creates a card; what the CLI lacks is
a plain `hscc project dispatch` verb, but the library function is the API seam and needs
no new logic.

Request body:
```json
{ "board": "default", "title": "...", "assignee": "researcher-a", "body": "...", "confirm": true }
```
`board` and `title` required (400 otherwise); `assignee`/`body` optional. `confirm: true`
required (409). Response: `{"id": "<new card id>", "message": "dispatched ...", "speak": "..."}`.

#### `POST /v1/review/{card_id}/merge` — MUTATING, confirm-gated

Merges the card's branch into `main` AND closes the card — the `--apply` path.
**Needs new logic / integration seam:** the logic lives in
`flightdeck.commands.review.cmd_review` (`review.py:409`) which prints; its building
blocks are `_do_apply(repo, branch, base)` (`review.py:272`) and `_real_close_card(card_id, board)`
(`review.py:289`). Implementation card: call `_do_apply` then `_real_close_card` directly
(matching the ordering + close-on-landed-only rule in `cmd_review` at review.py:476-529),
or add a thin `review_merge(card_id, ...) -> dict` seam. Response: `{"message": "...", "merged": true, "card_closed": true}`.

Request body: `{ "confirm": true }` (the merge base is `main`; no other required field).

#### `POST /v1/template/apply` — MUTATING, confirm-gated

Backed by the template engine's own handler, exactly as `_handle_template` does at
hscc.py:459: put `hscc-cluster/` on `sys.path`, `from cluster_template_cli import
cmd_cluster_template`, call `cmd_cluster_template(["apply", name, "--confirm"])`.
This returns a dict (see the `--apply` path at hscc.py:502). The `--confirm` flag is
the CLI's own confirm gate; the API ALSO requires HTTP-level `"confirm": true`.

Request body: `{ "name": "<template name>", "force_recreate": false, "confirm": true }`
(`force_recreate` → appends `--force-recreate`).

#### `POST /v1/cluster/stop` — see Cluster section above (MUTATING).

#### `POST /v1/workloads/start` — **Needs new logic (flagged).**

There is no existing hscc function to *start* a workload — the CLI starts workloads via
`template apply` (a whole-fleet operation) or by driving sparkrun by hand. A single
workload-start endpoint is NOT in scope unless explicitly requested, because the CLI has
no matching verb to mirror and its semantics would be ambiguous (which template/recipe,
single node vs fleet). Design note only, no endpoint specified. Workload *stop* is the
`POST /v1/cluster/stop` endpoint above.

#### `POST /v1/qa/manual` — MUTATING, confirm-gated (optional, flagged)

`flightdeck.commands.qa` has `_new_manual_id` + `_save_manual` (qa.py:488/474) for the
manual-QA store. A "note a manual verification" endpoint could be added, but there is no
CLI verb today (`flightdeck qa` only lists). **Not specified — out of scope** unless a
later card asks for it (section D note).

---

## B. The `speak` field (voice / in-car path)

**Purpose:** the iOS client's in-car/voice mode (Siri App Intents) needs something
speakable without re-deriving prose from raw JSON on-device. Every READ response carries
`"speak"` — a one-to-two-sentence plain-language summary. It is spoken aloud, so it must
be short, factual, and never fabricate numbers.

**Authoring rule:** `speak` is derived from the actual computed data in the same request,
never hardcoded. It is produced by the API server (one place per endpoint), so the client
never re-derives. Keep sentences ≤ ~20 words. Do not decorate with emoji.

Endpoint-specific contracts:

| Endpoint | `speak` content |
|---|---|
| `GET /v1/cluster/status` | "`{total_hosts}` hosts up. `{len(workloads)}` workload(s) running, `{len(idle_hosts)}` idle." — or "cluster status unavailable" if the engine returned an error. |
| `GET /v1/cluster/hosts` | "`{len(hosts)}` hosts registered. `{N}` cluster(s) saved." Degrade to "host list unavailable" on error. |
| `GET /v1/cluster/monitor` | "Fleet snapshot: `{json}`" is too raw. Use a compact aggregate from the JSON if present, else "fleet monitor unavailable". Keep to one clause. |
| `GET /v1/cluster/jobs` | "`{count}` job(s) running." from the output, else "job list unavailable". |
| `GET /v1/cluster/info` | "Cluster configuration loaded." / "cluster info unavailable". |
| `GET /v1/health` | `ok` ? "All checks passed." : "`{N}` of `{total}` checks have problems." Name the failing checks in one clause if space allows. |
| `GET /v1/fleet/stats` | "Last `{days}` days: `{key stat}`." e.g. "About 1.2k work items across the last 7 days." Keep to the most useful single number. |
| `GET /v1/fleet/throughput` | `ok`/nodes: "`{nodes_ok}` of `{nodes_total}` nodes healthy." Refer to the dict's `fleet.nodes_ok`/`by_node`. |
| `GET /v1/fleet/streams` | "Daemon streams: all ok." or enumerate the blocked/failed ones: "`{n}` stream(s) blocked: dgx, gateway." |
| `GET /v1/autoscale` | Humanize the decision: "Autoscale suggests scaling up by 1." / "Autoscale: nothing to change." |
| `GET /v1/standup` | The headline: "`{N}` cards need review, `{M}` are running, `{K}` failing." Only mention sections that are non-empty, else "Nothing needs attention." |
| `GET /v1/cards` | "`{count}` card(s)." Optionally "`{r}` running." |
| `GET /v1/cards/{card_id}` | "Card `{id}`: `{title}`. Status `{status}`." |
| `GET /v1/review/queue` | `count == 0` ? "Nothing awaiting review." : "`{count}` card(s) await review." |
| `GET /v1/qa/queue` | "`{queue_len}` card(s) need manual testing." + "`{manual_len}` need manual verification." |
| `GET /v1/review/{card_id}` | One clause: subject + merges-cleanly verdict. "Card `{id}` — `{subject}`, merges cleanly." / "`{conflicts}` conflict(s) to resolve." |

Rules for ALL of the above:
- When the backing call raised or returned an error dict, `speak` must say so plainly
  (e.g. "Status unavailable."), never invent values.
- `speak` is ALWAYS present on a read response, even on a 200-with-partial-data.
- On 4xx/5xx the `speak` lives inside the error object (section C) and states the
  failure in one human sentence.

Implementers: write a `_speak_<endpoint>(data) -> str` helper per endpoint. Keep them
pure — they take the computed dict and return the sentence. Unit-testable with no I/O.

---

## C. Operational shape

### `hscc api` verb group

A new group routed in `hscc_daemon/hscc.py:main()` (hscc.py:700), matching the existing
flat green dispatch style. Following the established pattern (compare `_handle_cluster`
at hscc.py:417 / `_handle_project` at hscc.py:644), add:

```python
# in main(), after the 'project' group branch (hscc.py:733)
if cmd == "api":
    from hscc_daemon.api_cli import cmd_api
    rc = cmd_api(args[1:])
    sys.exit(rc)
```

`hscc_daemon/api_cli.py` (new file) exposes `cmd_api(argv) -> int` handling:
`start` / `stop` / `status` / `--help`. The api server logic lives in
`hscc_daemon/api_server.py` (new file) with the request handler; `api_cli` is only the
thin start/stop/status wrapper (mirroring how `cli.py` wraps the daemon). This keeps the
HTTP code out of the flat dispatch file and testable on its own.

**Verb semantics:**

- `hscc api start` — resolve bind (see config), resolve/validate token (fail closed if
  absent), fork into background daemon, write PID, log startup.
- `hscc api stop` — read PID from file, SIGTERM, wait for exit, remove PID file.
- `hscc api status` — read PID file; report running-with-PID / stale / stopped; report
  the bound address from config.
- `hscc api --help` — print the group help (add to the flat `main()` help text).

### PID + log (reuse `hscc_daemon/daemon_ops.py` conventions)

Reuse the EXISTING daemon-ops mechanism rather than inventing a parallel one:

- **PID file:** `~/.hscc/api.pid` (distinct from `daemon.pid` — the API server is a
  separate process from the monitoring daemon). Implement with
  `daemon_ops.save_pid()` / `daemon_ops.get_pid()` / `daemon_ops.write_stopped()`
  generalized to accept a pid-file path, or add thin `api_ops` helpers that call the same
  `~/.hscc` dir and the same read/write/kill logic (daemon_ops.py:17-42). Prefer
  parameterizing the existing functions.
- **Log:** `~/.hscc/api.log`, written with `daemon_ops.log(msg, level)` (daemon_ops.py:140)
  generalized to a log path, or a thin wrapper. Reuse the timestamp format
  (`[ISO] [LEVEL] msg`).
- **Token:** `~/.hscc/api-token`, mode 0600. Generated on first `start` (when the file is
  absent) with `secrets.token_urlsafe(32)`. Written atomically (tmp + `os.replace`) with
  mode 0600. If the file exists but is unreadable/unparseable (empty, or a permission
  error), `start` FAILS CLOSED with a clear error — never generates a new token silently
  (that would strand the iOS client) and never starts unauthenticated.

`start` forks into the background using the same double-fork pattern as
`cli.cmd_start` (cli.py:29-61): fork, `setsid`, re-fork, write PID, run serve loop. All
request handling lives in the child.

### Config

Order of precedence (lowest → highest): defaults → `~/.hscc/api.json` if present →
`hscc api start` CLI flags. A minimal `api.json`:

```json
{
  "bind": "tailscale",        // "loopback" (default) | "tailscale" | "0.0.0.0" (REFUSED) | explicit IP
  "port": 8787,
  "registry": "~/.flightdeck/registry.yaml"
}
```

- **`bind`:** default `"loopback"` → `127.0.0.1`. `"tailscale"` → resolve the tailnet IP
  (below). An explicit IP string is allowed only if it is NOT `0.0.0.0` / `::` —
  configuring those is a hard error at start. `"0.0.0.0"` as a value is REFUSED.
- **`port`:** default `8787`. Bound as `(bind_ip, port)`. A port already in use → clear
  error at start (address-in-use), not a silent bind failure.
- **Tailscale IP resolution:** `subprocess.run(["tailscale", "ip", "-4"])` and take the
  first line. **Graceful degradation** (constraint #2): if `tailscale` is not installed
  (FileNotFoundError), the command fails, or returns no IPv4, then when bind is
  explicitly `"tailscale"`, `start` prints a clear actionable error and exits non-zero —
  it does NOT crash with a traceback, does NOT fall back to loopback silently, and does
  NOT fall back to `0.0.0.0`. The error says: install/enable Tailscale or set `bind` to
  loopback. (Bind defaults to loopback, so a fresh install is reachable locally and the
  iOS app operator opts in to tailnet exposure with full knowledge.)
- `hscc api start --tailscale` / `--bind <ip>` / `--port <n>` are optional flags that
  override config for that invocation.

### Server loop

- `http.server.ThreadingHTTPServer` (stdlib, threaded — handles concurrent reads
  without blocking a long cluster command) bound to `(bind_ip, port)`.
- One request handler class (`api_server.ApiHandler(BaseHTTPRequestHandler)`) that:
  1. extracts + validates the Bearer token for EVERY route (constant-time);
  2. routes `(method, path)` against the `/v1/...` table;
  3. dispatches to the backing function; wraps its return into the response envelope;
  4. catches exceptions and maps them to the error contract below;
  5. sets `Content-Type: application/json` on every response.
- The handler holds the resolved token + config at construction time (from the parent
  process before forking), so each thread authorizes without re-reading disk.

### Error contract

Every error response is a JSON object with a consistent shape:

```json
{
  "error": {
    "code": "unauthorized",        // machine-readable slug
    "message": "missing bearer token",  // one human sentence, safe for logging
    "speak": "You are not authorized."  // TTS-safe one-liner (section B)
  }
}
```

HTTP status codes:

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | Malformed JSON body, missing required field, invalid query param, body too large |
| 401 | `unauthorized` | Missing or wrong Bearer token |
| 404 | `not_found` | Unknown route, unknown version, unknown card id |
| 405 | `method_not_allowed` | Valid path with wrong method (e.g. GET on a POST-only route) |
| 409 | `confirm_required` | Mutating call without `confirm: true`, or failed mutation precondition (e.g. review already landed) |
| 500 | `internal_error` | Unhandled exception in the backing function |

**Never leak:** the token is never echoed in a message; raw tracebacks are never sent to
the client (they go to `~/.hscc/api.log` only). A 500 logs the traceback server-side and
returns the generic `internal_error` slug with a neutral message — e.g.
"an unexpected error occurred — check ~/.hscc/api.log". A backing function's `error`
key (cluster engine commands return `{"error": ...}` on failure) maps to a 409 or 500
per endpoint semantics and its message is sanitized (strip any token-like content, add a
server-side note to check the log).

**Query/body limits:** reject request bodies over 1 MiB with 400. Unknown routes that
match `/v1/*` → 404; routes outside `/v1/*` → 404.

---

## D. Explicitly OUT of scope

- Web UI / dashboard.
- WebSockets / streaming / server-sent events.
- Multi-user accounts or RBAC (single bearer token, operator-level).
- TLS termination — Tailscale provides the encrypted transport; loopback needs none.
- The iOS app's internals (separate work; this doc is the contract it implements against).
- A single-workload "start" endpoint and a manual-QA "note" endpoint (flagged in section
  A as not backed by an existing CLI verb — not specified unless a later card asks).

---

## Implementation-card scaffolding notes

This design is ONE doc — but it sets up the implementation cards. For those
cards, the concrete touchpoints are:

1. New files: `hscc_daemon/api_server.py`, `hscc_daemon/api_cli.py`.
2. Edited: `hscc_daemon/hscc.py` (add `api` branch to `main()`, ~hscc.py:733), plus the
   top-level help text; `hscc_daemon/daemon_ops.py` (generalize PID/log helpers to an
   explicit path — optional, or a thin wrapper).
3. Reuse: `_load_cluster_engine()` (hscc.py:379) patterns; flightdeck via
   `sys.path` insert (hscc.py:644 pattern); `daemon_ops` PID/log.
4. Tests live under `hscc_daemon/tests/` following the existing suite layout, using
   Python's `unittest.mock` to stub `sparkrun`, the kanban library, and tailscale.

Verify via `scripts/run_tests.sh` / `pytest` (the repo's established verify).
