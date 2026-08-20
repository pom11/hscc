# HSCC HTTP API (`hscc api`)

The HSCC HTTP API exposes HSCC cluster state (and project/kanban dispatch) to
**external applications** — primarily the private companion iOS client. It is
NOT a human-facing CLI: it is a machine-consumable JSON API for apps to read
fleet health and drive actions programmatically.

- Pure-stdlib server (`http.server.ThreadingHTTPServer`), no flask/fastapi.
- Bearer-token authenticated on **every** call, including reads.
- Loopback-bound by default; tailnet is an explicit opt-in; `0.0.0.0` is
  refused by design (the API can start/stop GPU work).
- Lifecycle is managed with the `hscc api` CLI verb group.

---

## How to run it

```bash
hscc api start                 # bind loopback (127.0.0.1), port 8787
hscc api start --tailscale     # bind to this host's tailnet IP
hscc api stop
hscc api status                # running/stopped + the bound host:port
```

`start` forks the server into the background (own PID/log at
`~/.hscc/api.pid` / `~/.hscc/api.log`). `status` reports the resolved bind
host:port so a client knows where to point.

### Config

Precedence (lowest → highest): defaults → `~/.hscc/api.json` → flags.

```jsonc
// ~/.hscc/api.json
{
  "bind": "loopback",   // "loopback" | "tailscale" | "<explicit ip>"
  "port": 8787
}
```

- `bind: "loopback"` (default) → `127.0.0.1`.
- `bind: "tailscale"` → the host's tailnet IPv4; a hard error if none is found
  (never widens the bind).
- An explicit IP string is used as-is, **unless** it is `0.0.0.0` / `::` —
  those are **always refused**.
- `port`: default **8787**.

---

## Auth (bearer token)

The API requires `Authorization: Bearer <token>` on **every** request —
reads included. There is no anonymous access and no unauthenticated health
probe.

**Token file:** `~/.hscc/api-token`, auto-generated on first `start` with
`secrets.token_urlsafe(32)` and written with mode **0600** (via an atomic
tmp + `os.replace`, so it is never briefly world-readable). The token value
is never logged, never echoed, and never printed by any command.

**How a client authenticates:**

```text
Authorization: Bearer <token>
```

**How to read the token** (to configure a client). From the API host shell:

```bash
cat ~/.hscc/api-token
```

The token is a single line of random text with a trailing newline. Configure
your client to send that exact value as the bearer token. (This doc
deliberately does not print any real token value.)

**To rotate the token:** stop the API, then regenerate:

```bash
hscc api stop
rm ~/.hscc/api-token
hscc api start            # writes a fresh token to ~/.hscc/api-token
```

Every existing client must be updated with the new token (older ones are
rejected with `401 unauthorized`). The server never silently regenerates a
token behind a running client's back.

---

## Binding & pointing a client at this host

The API binds **loopback by default**, so only local processes (on the same
host) can reach it. To reach it from an **external app (the iOS client)**, the
operator opts in to tailnet exposure, and the client sets the **host and port
manually** — there is no discovery/registration; the app is configured with a
specific `host:port`.

**On the API host** — opt in to the tailnet bind and start:

```bash
hscc api start --tailscale
hscc api status        # shows the bound tailnet host:port, e.g. 100.64.0.1:8787
```

**On the client (iOS app)** — set the base URL manually to this host's tailnet
address. Using this host's tailnet IP as the worked example:

```text
https://100.64.0.1:8787
```

(replace with the actual tailnet IP printed by `hscc api status` on this host).

> **Tailscale CLI note:** on this host Tailscale is the macOS App Store build,
> so its CLI is **not on PATH**. It lives at
> `/Applications/Tailscale.app/Contents/MacOS/Tailscale` (e.g.
> `/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4`). The API's own
> bind resolution probes that path first, then falls back to a bare
> `tailscale`, then to the network interfaces for a `100.x` address.

---

## `speak` field

Every **read** response (and every error object) carries a first-class
`speak` string: a TTS-safe one-liner derived from the **actual data** in that
response — never hardcoded, never fabricated, never noisy. Voice-first clients
(e.g. a hands-free iOS app) should **read the `speak` field aloud** and show
the structured fields on screen.

```json
{
  "total_hosts": 3,
  "idle_hosts": [],
  "speak": "3 hosts up. 2 workloads running, 0 idle."
}
```

Mutating responses do **not** carry `speak` (they return a human `message`).

---

## Endpoint reference

All paths are under `/v1`. All responses are JSON objects with UTF-8
Content-Type. `*` marks **read-only** endpoints; `⚠` marks **mutating**
endpoints (confirm-gated, see below).

### Liveness & health

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/ping` | The **API's own** liveness — confirms the API process is up. Returns `{ok, service, version, speak}`. This is NOT the fleet health check. | read |
| GET | `/v1/health` | **Fleet** health — runs `verify.run_all()` across the whole stack. Returns `{ok, checks: [...], speak}`. `ok` is true only when every check passes; `checks` is an array of per-check `{name, ok, ...}`. | read |

> **`/v1/ping` vs `/v1/health`:** `/v1/ping` answers "is the API server
> itself alive?"; `/v1/health` answers "is the fleet healthy?". The API's own
> liveness lives at `/v1/ping` specifically to avoid colliding with the fleet
> health check at `/v1/health`.

### Cluster (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/cluster/status` | Running workloads + idle hosts. Returns `{workloads, idle_hosts, total_hosts, speak}`. | read |
| GET | `/v1/cluster/hosts` | Registered hosts + saved clusters + live status. Returns `{hosts, saved_clusters, live_status, speak}`. | read |
| GET | `/v1/cluster/monitor` | Fleet monitor snapshot (aggregate metrics). | read |
| GET | `/v1/cluster/jobs` | Spark job list. | read |
| GET | `/v1/cluster/info` | Cluster configuration summary. | read |

### Fleet (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/fleet/stats?days=N` | Fleet completions & tool activity over the last `N` days (default `7`). | read |
| GET | `/v1/fleet/throughput` | vLLM token throughput + per-node queue depth. Returns `{fleet: {nodes_ok, nodes_total, ...}, by_node, ...}`. | read |
| GET | `/v1/fleet/streams` | Daemon stream health — a map of stream name → status; `ok: true` per stream. Returns `{streams, speak}`. | read |
| GET | `/v1/autoscale` | Scaling advice from current queue depth (read-only — it only *advises*). | read |

### Project / kanban (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/standup` | The daily fleet digest (`NEEDS YOU` / `RUNNING` / `FAILING` / …). | read |
| GET | `/v1/cards?board=&status=` | Cards across the given board (or all), optionally filtered by `status`. Returns `{cards, count, speak}`. | read |
| GET | `/v1/cards/{card_id}` | One card's full detail. `404 not_found` if unknown. | read |
| GET | `/v1/review/queue` | Cards genuinely awaiting review, newest first. Returns `{queue, count, speak}`. | read |
| GET | `/v1/review/{card_id}` | **Dry-run** review facts for one card (branch state, merge conflicts, the VERIFY line). Read-only — never merges, never closes. `404` if the card does not resolve to a reviewable branch. | read |
| GET | `/v1/qa/queue` | The pre-merge QA queue + the manual-QA store. Returns `{queue, manual_qa, speak}`. | read |

### Actions (mutating — confirm-gated)

All mutating endpoints require `"confirm": true` in the JSON body and return
**409 `confirm_required`** otherwise. They are registered as **POST only** — a
GET to the same path returns `405 method_not_allowed`. Mutating responses
carry a human `message` (and structured fields) but **no** `speak`.

| Method | Path | Required body | Description |
|---|---|---|---|
| POST | `/v1/cards` | `board`, `title`, `confirm` | Dispatch a new card. Optional `assignee`, `body`. Returns `{id, message}`. |
| POST | `/v1/review/{card_id}/merge` | `confirm` | Merge the card's branch into `main` and close the card. `404` if unresolvable; `409 already_landed` if already merged; `502 merge_failed` if the merge didn't land (card stays open). |
| POST | `/v1/template/apply` | `name`, `confirm` | Apply a cluster template. Optional `force_recreate`. `502 apply_failed` if not fully applied. |
| POST | `/v1/cluster/stop` | `container_id`, `confirm` | Stop a running workload by container id. `502 stop_failed` if it could not be stopped. |

**Curl example of a confirm-gated call** (dispatches a card):

```bash
curl -sS -X POST http://127.0.0.1:8787/v1/cards \
  -H "Authorization: Bearer $(cat ~/.hscc/api-token)" \
  -H "Content-Type: application/json" \
  -d '{"board": "default", "title": "Write login screen", "confirm": true}'
```

Without `"confirm": true` the same call returns:

```json
{ "error": { "code": "confirm_required", "message": "this action is destructive and requires \"confirm\": true in the request body to dispatch a card", "speak": "Confirmation required to dispatch a card." } }
```

---

## Error contract

Every error response shares one shape:

```json
{ "error": { "code": "...", "message": "...", "speak": "..." } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | malformed/missing field, non-object body, body > 1 MiB |
| 401 | `unauthorized` | missing or invalid bearer token |
| 404 | `not_found` | unknown route, unknown card, card not reviewable |
| 405 | `method_not_allowed` | valid path, wrong HTTP method |
| 409 | `confirm_required` | mutating call without `confirm: true` |
| 409 | `already_landed` | merge target already merged |
| 500 | `internal_error` | unhandled exception (traceback logged server-side only) |
| 502 | `merge_failed` / `apply_failed` / `stop_failed` | a mutation did not land |

Errors never leak the token or a raw traceback. A 500 logs the traceback to
`~/.hscc/api.log` and returns a neutral pointer to it.

---

## Request/response notes

- **Body cap:** 1 MiB; a larger declared body is rejected with `400`.
- **Query strings** are supported on GET routes (e.g. `/v1/cards?status=running`,
  `/v1/fleet/stats?days=30`).
- **URL path params** (`{card_id}`) override query-string keys of the same name.
- Reads that hit a degraded backing layer return **200 with an honest
  `speak`** (e.g. `"cluster status unavailable"`) — never a fabricated value,
  never a crash.

---

## Security notes

- **Tailscale is the transport.** There is **no TLS termination inside the
  API itself** — the HTTP server speaks plain HTTP. Encrypted transport is
  provided by Tailscale (a WireGuard mesh, end-to-end encrypted). Do not run
  the API exposed on a plain, non-VPN network.
- **Token required on every call, including reads** — there is no anonymous
  surface.
- **Never expose the port to a public interface.** The API can start/stop GPU
  work and dispatch cards; `0.0.0.0` / `::` binds are hard-refused by design,
  and the tailnet bind is a deliberate, explicit opt-in. Keep the bind on
  loopback or your tailnet only.
- The token is mode-0600, never logged, never printed by any command, and
  this document never contains a real token value.
