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
hscc api start --tailscale --bind 192.0.2.10
                               # LAN + tailnet at once (first --bind = primary)
hscc api stop
hscc api status                # running/stopped + every bound host:port
```

`start` forks the server into the background (own PID/log at
`~/.hscc/api.pid` / `~/.hscc/api.log`). It serves the same port, token and
handlers on every requested address. `--bind <ip>` is repeatable and combines
with `--tailscale`; each explicit address must be an interface address on this
host. If any non-tailnet listener fails to bind, the whole start fails with no
half-up state. If the tailnet address is requested and Tailscale is down, the
other listeners stay up and `status` reports the tailnet one as unavailable.
`status` lists every bound host:port from the runtime snapshot
(`~/.hscc/api-state.json`, `host` = primary, `hosts` = all) and the QR payload
carries the same `hosts` array so clients can fail over.

### Config

Precedence (lowest → highest): defaults → `~/.hscc/api.json` → flags.

```jsonc
// ~/.hscc/api.json
{
  "bind": "loopback",   // "loopback" | "tailscale" | "<explicit ip>" | [ ... ]
  "port": 8787
}
```

- `bind: "loopback"` (default) → `127.0.0.1`.
- `bind: "tailscale"` → the host's tailnet IPv4; a hard error if none is found
  and it is the only address (never widens the bind).
- An explicit IP string is used only when it is an interface address on this
  host, **unless** it is `0.0.0.0` / `::` — those are **always refused**.
- A list binds several addresses (first = primary).
- `port`: default **8787**, shared by every listener.

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
| GET | `/v1/health` | **Fleet** health — the last cached verify snapshot: cheap, and safe to poll. Returns `{ok, checks: [...], speak, age_seconds, generated_at, stale, source, paused_nodes}`. `ok` is true only when every check in that snapshot passed; `checks` is an array of per-check `{name, ok, ...}`; `generated_at` is the epoch second the verdict was produced and `age_seconds` its age now; `stale` is true when that verdict is older than the 120 s cache TTL; `source` is `"cache"` or `"inline"`. `paused_nodes` lists the nodes parked by `hscc node pause` as `{node, status: "paused", paused_reason, paused_since}` — a parked machine is a choice, not a fault, and this route names it without softening the verdict. | read |
| GET | `/v1/verify` | **Run the fleet check now** — inline: every check of the fleet smoke test, real GPU chat round-trips included. Returns `{ok, checks: [...], speak}`. Never serves the cache, and its verdict also refreshes the snapshot `/v1/health` answers with. | read |

> **`/v1/ping` vs `/v1/health` vs `/v1/verify`:** `/v1/ping` answers neither
> fleet question — it only confirms the API process is up, which is why the
> API's own liveness lives there: to avoid colliding with the fleet health
> check at `/v1/health`. `/v1/health` is **the last cached verify snapshot**
> (cheap, pollable — served by the health cache, and it never runs a verify on
> the request thread). `/v1/verify` is **run the fleet check now**.
>
> **Poll `/v1/health`; call `/v1/verify` only when you specifically want a
> fresh verdict.** A `/v1/health` poll never runs a verify except when no
> snapshot exists anywhere (the first poll of a fresh process, with no snapshot
> file to read): then exactly one verify runs inline and every concurrent poll
> waits for that same verdict instead of starting its own. A snapshot younger
> than the 120 s TTL is answered as-is; an older one is answered immediately
> with `stale: true` while one background refresh runs, so a poll never waits
> on a verify. `/v1/verify` is the expensive route — the whole fleet smoke
> test, real GPU chat round-trips included — so keep it off any timer.
>
> When there is no verdict and none could be produced, `/v1/health` answers
> `{"speak": "health check unavailable"}` with no `ok` or `checks`: that means
> "no health information", never "the fleet is fine".

### Cluster (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/cluster/status` | Running workloads + idle hosts. Returns `{workloads, idle_hosts, total_hosts, speak}`. | read |
| GET | `/v1/cluster/hosts` | Registered hosts + saved clusters + live status. Returns `{hosts, saved_clusters, live_status, speak}`. | read |
| GET | `/v1/cluster/monitor` | Fleet monitor snapshot (aggregate metrics). Nodes the pause overlay holds are projected as parked rather than broken: their host and serving rows carry `{status: "paused", paused_reason, paused_since}`, and the parked set is also stated top-level as `paused_nodes`. | read |
| GET | `/v1/cluster/jobs` | Spark job list. | read |
| GET | `/v1/cluster/info` | Cluster configuration summary. | read |

### Fleet (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/fleet/stats?days=N` | Fleet completions & tool activity over the last `N` days (default `7`). | read |
| GET | `/v1/fleet/throughput` | vLLM token throughput + per-node queue depth. Returns `{fleet: {nodes_ok, nodes_total, ...}, by_node, ...}`. | read |
| GET | `/v1/fleet/streams` | Daemon stream health — a map of stream name → status; `ok: true` per stream. Returns `{streams, speak}`. | read |
| GET | `/v1/autoscale` | Scaling advice from current queue depth (read-only — it only *advises*). | read |
| GET | `/v1/fleet/usage` | Cost/usage roll-up from the tracked usage store. Returns `{budget, cost_tracked, per_bot, per_project, speak, total}`. When the store cannot be read the answer degrades to `{speak}` — no zeroes are invented for a store that was never read. | read |

### Orchestrator (read)

The dashboard pair from audit §2.6 / decision D9: the two questions behind
`hscc main status` and `hscc orch waker status`, which had no HTTP surface at
all. Each route answers exactly ONE CLI verb's question by calling the same
function that verb renders from, and each adds nothing to the payload but
`speak`.

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/main/status` | The configured main orchestrator and its pane — the `hscc main status` answer: `{name, profile, board, state, running, busy, pane, target, panes, detail, speak}`. `state` is `busy` / `idle` / `not-running` / `ambiguous` (several panes, never guessed between) / `unknown` (a pane whose screen could not be read). The identity is the one resolver's answer, so a renamed main needs no client change. Read-only: it never starts, re-attaches or renames the main orchestrator, and it takes no `profile` — it can only be asked about the configured one. | read |
| GET | `/v1/orch/waker/status` | The daemon-resident orchestrator waker — the `hscc orch waker status` answer: `{enabled, config, stream: {name, interval_s, scan_every_s, last_tick, tick_age_s, ok, message}, boards, paneless_skip, audit, speak}`. `tick_age_s` is `null` when the waker has never ticked (never a fabricated `0s ago`), `paneless_skip` names the boards it stopped nudging and why. Read-only: `enable`/`disable` stay CLI verbs, so nothing over HTTP can change when the operator is woken. | read |

> **These two are not one question.** `/v1/main/status` reports the operator's
> pane (is the orchestrator process up, is it mid-turn); `/v1/orch/waker/status`
> reports the daemon-side waker that knocks on it (is it enabled, when did it
> last tick). A dashboard that wants "is the main orchestrator being woken"
> needs both, and a green pane with a stale waker tick is exactly the state this
> pair exists to show.

### Project / kanban (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/standup` | The daily fleet digest (`NEEDS YOU` / `RUNNING` / `FAILING` / …). | read |
| GET | `/v1/cards?board=&status=` | Cards across the given board (or all), optionally filtered by `status`. Returns `{cards, count, speak}`. | read |
| GET | `/v1/cards/{card_id}` | One card's full detail. `404 not_found` if unknown. | read |
| GET | `/v1/review/queue` | Cards genuinely awaiting review, newest first. Returns `{queue, count, speak}`. | read |
| GET | `/v1/review/{card_id}` | **Dry-run** review facts for one card (branch state, merge conflicts, the VERIFY line). Read-only — never merges, never closes. `404` if the card does not resolve to a reviewable branch. | read |
| GET | `/v1/qa/queue` | The pre-merge QA queue + the manual-QA store. Returns `{queue, manual_qa, speak}`. | read |
| GET | `/v1/projects` | Registry truth plus each project's board truth. Returns `{count, projects, speak}`. | read |
| GET | `/v1/projects/{name}` | One project's detail — registry facts plus the board and git read of it. Returns `{board, board_counts, git, name, repo, speak, topic}`; `board` is the project's board (`unknown` when the registry does not name one), `board_counts` is status → card count with a `total`, `git` is the repo's `{is_repo, branch, dirty, uncommitted, last_activity_seconds_ago, head, ahead, behind}` and degrades to `{is_repo: false}` when the repo cannot be read. `session_health` — the `<name>` chat session on the `<name>-orch` profile, `{profile, session, messages, input_tokens, compression_failure_error, compression_fallback_streak, compression_ineffective_count, context_window, threshold_tokens, compaction_at_risk, bloated, reason}` — is present **only when that session can be read**: absence means "could not report", never "healthy" (see `/v1/sessions`). `404 not_found` if no project carries that name; a registry that cannot be read answers `{speak}` alone. Shipped consumer: iOS `ProjectDetailResponse`. | read |
| GET | `/v1/kanban/blocked` | Blocked cards across **all** boards, with why. Returns `{boards, count, errors, speak, tasks}` — `errors` carries what could not be read instead of counting an unread board as quiet. | read |
| GET | `/v1/kanban/stale` | Non-terminal cards across all boards. Returns `{boards, count, errors, older_than, speak, tasks}` — `older_than` states the age window the answer used. | read |
| GET | `/v1/kanban/running` | `hscc kanban monitor` over HTTP (audit F8) — the same `collect()` pass the CLI renders. Returns `{boards, count, errors, pin_conflicts, speak, tasks}`. | read |
| GET | `/v1/merge-queue` | The local merge queue's read-only rows, token-gated like every other route. Returns `{count, generated_at, ok, projects, queued, rows, speak}`. | read |
| GET | `/v1/mobile/summary` | Per-board status counts for the iOS widget / Live Activity, from the same `collect()` pass `hscc kanban monitor` renders, so `done` means done in the last 24 h. `?boards=a,b` narrows to those boards (`404 not_found` if one is not a registered project board). When the board listing is unavailable the answer carries **no** per-board key at all — empty counts would render as "every board is quiet". | read |

### Profiles (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/profiles` | **The `hscc profiles` answer** — running kanban card counts per profile. Returns `{counts, total_running, profiles, speak}`: `counts` maps profile name → running cards, `total_running` is their sum, `profiles` is the list of profile **name strings** that have running cards (never the full roster). `speak` reads e.g. "2 profiles running 3 tasks." The payload has ONE implementation — `cluster_engine.cmd_profile_status()`, the same single call the CLI verb renders — and this handler adds nothing but `speak`. Shipped consumer: iOS `ProfilesResponse`. | read |
| GET | `/v1/profiles/list` | The profile **roster** — every profile the API can see, running or not. Returns `{count, profiles, speak}`, where `profiles` is a list of per-profile **objects** with the safe summary keys `{name, is_default, gateway_running, model, provider, skill_count, description, description_auto, distribution_name, distribution_version}` and `speak` reads e.g. "5 profiles total." It counts nothing: no `counts`, no `total_running`. Shipped consumer: iOS `ProfileListResponse` (the create-card assignee picker). | read |
| GET | `/v1/profiles/{name}` | One profile: the same summary object as a roster entry, under `summary`, plus the config **whitelist** under `config` (`model.default`/`model.provider`, `toolsets`, `skills.preload`, the `compression` block — never `api_key`, `base_url` or anything from `.env`). `404 not_found` if the profile does not exist. | read |

> **These are two questions, not two answers to one** (audit F2, measured).
> `GET /v1/profiles` is the canonical — and only — door to the running-card
> counts that `hscc profiles` prints; `GET /v1/profiles/list` answers "which
> profiles exist" and is the only door to the config whitelist. The two payloads
> share the key name `profiles` with different meanings (a list of NAMES here, a
> list of OBJECTS there), which is what made them read as one answer. Neither is
> an alias of the other, neither is deprecated, and neither should be "unified"
> into the other: each has a shipped decoder that the other cannot satisfy. A
> profile-status read that fails answers `{"speak": "Profile status
> unavailable."}` with no `counts` and no zeroes — absence is never reported as
> "nothing running". Pinned by
> `hscc-api/tests/test_profiles_route_canonical.py` (the payload boundary) and
> `hscc-api/tests/test_profiles_routes_documented.py` (these rows).

Per-profile lifecycle writes (`POST /v1/profiles/create`,
`/v1/profiles/{name}/delete`, `/v1/profiles/{name}/rename`,
`/v1/profiles/{name}/describe`) and the description read
(`GET /v1/profiles/{name}/describe`) are registered in `hscc-api/routes_profiles.py`;
every one of them is confirm-gated and is documented there rather than here.

### Daemon & host (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/daemon/status` | `hscc status` over HTTP — the daemon plus every stream it owns. Returns `{daemon_running, pid, speak, state, streams}`. | read |
| GET | `/v1/daemon/host` | The daemon host machine's **own** metrics (this box, not the fleet). Returns `{arch, cpu, daemon_running, daemon_uptime_seconds, disk, hostname, memory, platform, processes, speak, uptime_seconds}`. | read |
| GET | `/v1/daemon/history?limit=N` | Timeline of automated daemon actions, read from the recorder's bounded window. Returns `{count, events, exhausted, window_bytes}`. | read |
| GET | `/v1/triggers` | The trigger rules and their recent firings. Returns `{last_run, recent_events, rules, speak}`. | read |
| GET | `/v1/escalate` | Pending escalations — read-only, it never acts; the door that acts is `POST /v1/escalate`. Returns `{count, escalations, speak}`. | read |

### Autodown (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/autodown/status` | Read-only report mirroring `hscc autodown status`: whether the fleet is down, why, and what armed or blocked the decision. Returns `{active_cron_cpu_only, active_cron_model, blocked_by, down_since, enabled, fleet_up, force_armed, force_armed_overrides, idle_minutes, kanban_ok, kanban_reason, last_activity_iso, prci_active, prci_ok, prci_reason, prci_signal, reason, speak, state, state_recorded, state_source, wake_source, watchdog_blocked, watchdog_intentional}`. | read |

### Agent surface (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/activity/feed` | The live agent activity feed (the flight recorder). Returns `{count, entries, profiles, running_count, speak}`. | read |
| GET | `/v1/commands` | The slash commands available to the chat client, as registered by `hscc-commands`. Returns `{commands, speak}`. | read |
| GET | `/v1/cron/list` | Read-only roster of every scheduled job; each entry maps 1:1 from the on-disk `jobs.json` contract. Returns `{jobs, speak}` — and when the store cannot be read it degrades honestly to `{speak}` rather than fabricating a list. | read |
| GET | `/v1/alerts?since=&limit=` | The operator alert feed behind `hscc notify`, paged by exclusive ascending id. Returns `{alerts, count, has_more, latest_id, limit, next_since, oldest_id, since, speak}`. | read |

### Logs (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/logs?source=daemon&limit=N` | Bounded, **server-redacted** tail of one primary log: `source` selects it — `daemon` → `~/.hscc/daemon.log`, `api` → `~/.hscc/api.log`, `worker` → the Hermes agent log. The response is a **bare JSON array, not an object**: each entry is `{timestamp, level, source, line}`, and `timestamp`/`level` are `null` when the raw line carries no recognised header (the whole line then lands in `line`). `source` is required — absent or unknown → `400 bad_request` naming the three valid values; a non-integer `limit` → `400`; `limit` defaults to 50 and is clamped to `[1, 200]`. An absent or unreadable log answers `200` with an **empty array**: an empty tail is honest, an invented one never is. The handler reverse-seeks from EOF and reads only O(`limit`) bytes, so a 280 MB log costs what a small one costs. Every line is redacted before serving — Tailnet hosts → `100.64.0.1`, RFC1918 → `10.0.0.x`, other IPv4 → `[REDACTED_IP]`, plus bearer tokens, `token=`/`api_key=`-style secrets, session ids and long opaque runs — the client-side redactor is the second line, not the first. Pinned by `hscc-api/tests/test_routes_logs.py`. Shipped consumer: iOS `LogsResponse = [LogEntry]` (`Models.swift`), whose four fields are all optional so a partial line never blanks a row. | read |

### Templates & profiles (read)

The cluster template engine's read verbs (`hscc-cluster`), served verbatim the
way `hscc template …` runs them — never a second implementation. All four are
read-only; the mutating `apply` is the confirm-gated POST in the Actions table.
`validate` and `preview` answer **different** questions and disagree on purpose
(see the two rows): one returns a verdict for a name nothing serves, the other a
`404`.

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/template/list` | The cluster templates on disk. Returns `{count, speak, templates}`. | read |
| GET | `/v1/template/status` | Which template is currently applied. Returns `{applied, note, speak}`. | read |
| GET | `/v1/template/preview/{name}` | Dry-run: what applying `name` would change — the plan is resolved against the **live** fleet (`_resolve(probe=True)`), so the changes name concrete nodes and ports. `404 not_found` when no template on disk has that name. | read |
| GET | `/v1/template/validate/{name}` | Preflight `name` — is it deployable? Returns the CLI's two-layer verdict `{ok, placement, speak, structural, template}`: `structural` is the **offline** layer (version recognised, unknown keys, nodes exist, routing targets resolve, recipes exist) and `placement` the **live resolver** layer (capacity / occupancy), each `{ok, errors, warnings}`; top-level `ok` is the AND of both. A name nothing serves answers `ok: false` with `structural.errors` reading `Template not found` — **not** `404`, because the verdict is itself the answer (contrast `preview` above). Optional `?structural-only=1` skips the live layer and marks `placement.skipped: true`. | read |
| GET | `/v1/profile/list` | Browse installed **agent profiles** (the `hscc-roles` surface — a different subject from `/v1/profiles` above). Returns `{count, profiles, speak}`. | read |
| GET | `/v1/profile/editor/{profile}` | The fields the profile editor reads (and, over `POST`, writes) plus the option lists its pickers render. Returns `{compression, description, model, preload_skills, profile, provider, skills_all, speak, toolsets, toolsets_all}`: `compression` is `{threshold, threshold_tokens}`, `toolsets_all` is the toolset catalog and `skills_all` the profile's installed skill names, and no key here carries `api_key` or `base_url`. `404 not_found` for an unknown profile; `400 bad_request` for a name outside `[A-Za-z0-9._-]` (the path-traversal guard on the path segment). A backing that fails mid-read answers `{profile, error, speak}` with `error: "unavailable"` rather than a blank editor — absence of the fields is never "the profile has none". Shipped consumer: iOS `ProfileEditorResponse`. | read |

### Sessions (read)

| Method | Path | Description | Mutates |
|---|---|---|---|
| GET | `/v1/sessions?profile=<name>` | A profile's sessions with the bloat signals an operator needs before a chat session wedges. Returns `{bloated_count, count, profile, sessions, speak}`: `sessions` are that profile's own listable, non-archived **root** sessions newest first (sub-agent runs are not listed), read from the profile's `state.db` opened **READ-ONLY**, and `bloated_count` is how many of them carry positive evidence that compaction is not firing. `profile` is required — absent/blank is `400 bad_request`. A profile whose `state.db` cannot be opened answers `{profile, sessions, speak}` with an empty `sessions` and **no** `count`: an unreachable profile is not "a profile with no sessions", and the absent keys are how a client tells the two apart. Shipped consumer: the iOS session list (`HSCCClient.swift`). | read |

> **This reference is curated, not exhaustive — but it is now gated.** Every route
> `hscc-api/api_server.py` registers (the `routes_*.py` modules append to
> `api_server.ROUTES`, and `WS_ROUTES` carries the WebSocket upgrade) must appear as a
> Path cell in this reference **or** in the reasoned register
> `hscc-api/tests/route_coverage_allowlist.py`; `hscc-api/tests/test_api_reference_route_coverage.py`
> reddens otherwise. That gate exists because audit F2 could stay "unresolved in the
> docs" while the code was already right: neither profile route was named here, and
> nothing caught it. Each register entry points at the file where its route *is* written
> down and carries the card that owes the row, so the gap has an owner instead of a
> nickname.

### Actions (mutating — confirm-gated)

Every mutating endpoint requires `"confirm": true` in the JSON body and returns
**409 `confirm_required`** otherwise: every handler behind this table enforces
the gate before it touches anything — `routes_actions.py` does it inside the
shared `_action_fields` preamble (`routes_actions.py:204-218`), the other
modules call their own `_require_confirm` directly. A confirm-less body is
refused with that 409 **before** any write happens, which is the only safe way
to probe one of these doors. Three blanket sentences an earlier version of this
paragraph carried are not true, and the handlers — not the paragraph — are the
evidence:

* mutating routes are **not** registered as **POST only**: `PATCH /v1/cards/{card_id}`
  is registered as `PATCH` (`hscc-api/routes_actions.py:521`).
* a `GET` to a mutating path returns `405 method_not_allowed` only when that
  path is registered under some other method; the dispatcher answers
  `404 not_found` for a path nothing serves at all
  (`hscc-api/api_server.py:928-933`), and `GET /v1/orchestrator/chat/{id}` is a
  real route, so a `GET` there answers `200`.
* mutating responses do **not** uniformly "carry a `message` and no `speak`":
  `POST /v1/escalate` returns `speak` with no `message`,
  `POST /v1/sessions/{session_id}/compact` and `/retire` carry **both**, and
  `POST /v1/orchestrator/chat` answers `202` with `speak` and no `message`.
  Read the row; do not infer the payload from this section.

| Method | Path | Required body | Description |
|---|---|---|---|
| POST | `/v1/cards` | `board`, `title`, `confirm` | Dispatch a new card. Optional `assignee`, `body`. Returns `{id, message}`. |
| POST | `/v1/review/{card_id}/merge` | `confirm` | Merge the card's branch into `main` and close the card. `404` if unresolvable; `409 already_landed` if already merged; `502 merge_failed` if the merge didn't land (card stays open). |
| POST | `/v1/template/apply` | `name`, `confirm` | Apply a cluster template. Optional `force_recreate`. `502 apply_failed` if not fully applied. |
| POST | `/v1/cluster/stop` | `container_id`, `confirm` | Stop a running workload by container id. `502 stop_failed` if it could not be stopped. |
| PATCH | `/v1/cards/{card_id}` | `assignee`, `confirm` | Re-assign one card — **the mutating route that is not a POST**. Only `assignee` is editable: `title`/`body` have no backing mutation in `kanban_db`, so a body without `assignee` is `400 bad_request` rather than a silent no-op. Unknown card → `404`; a backing failure → `502 edit_failed`. Success returns `{id, edited: true, assignee, message}`; a card the backing refused to write returns **`200`** with `{id, edited: false, message}` and **no** `assignee` — `edited` is the fact, the 200 is not. No `speak`. Shipped consumer: iOS card re-assign (`HSCCClient.swift`). |
| POST | `/v1/cluster/up` | `confirm` | Start every unit in `serving.json` (whole-fleet up). Returns the backing's up result plus `message: "cluster up issued"`, and `speak` **only when the body did not set `dry_run`** — `dry_run` suppresses `speak` and nothing else, so it is not a preview: the backing call runs either way. `502 cluster_up_failed` when the backing raises or answers an error. Shipped consumer: iOS fleet up (`HSCCClient.swift`). |
| POST | `/v1/cluster/down` | `confirm` | Stop **all** servings fleet-wide. Returns the backing's down result plus `message: "cluster down issued"` and `speak`. `502 cluster_down_failed` when the backing raises or answers an error. Shipped consumer: iOS fleet down (`HSCCClient.swift`). |
| POST | `/v1/escalate` | `confirm` | Perform the pending escalations **for real** — reassigns repeatedly-failing cards to the strong tier and notifies a human where one is needed. This is the door that acts; `GET /v1/escalate` only lists. Returns `{escalations, count, performed: true, speak}` — `speak`, and deliberately **no** `message`. `502 escalate_failed` when the backing raises or answers a non-list. Shipped consumer: iOS escalate (`HSCCClient.swift`). |
| POST | `/v1/kanban/blocked/{card_id}/recover` | `confirm` | Return one blocked card to `ready`. Optional `reason`, echoed back. Returns `{id, board, reason, message, speak}` — `board` is the board the card was found on. Not blocked on any board → `404 not_found`; a backing failure → `502 recover_failed`. Shipped consumer: iOS blocked-card recovery (`HSCCClient.swift`). |
| POST | `/v1/profile/editor/{profile}` | `confirm` + at least one of `model`, `provider`, `toolsets`, `preload_skills`, `description`, `compression` | Write the profile editor's fields (`routes_profile_editor.py`). Only the keys you send are written; the rest of `config.yaml`/`profile.yaml` is preserved verbatim. Returns the editor state after the write — the same fields `GET /v1/profile/editor/{profile}` answers — plus `updated` (the field names actually written) and `speak`. A body carrying none of the supported fields is `400 bad_request`, not a silent success. Shipped consumer: iOS profile editor save (`HSCCClient.swift`). |
| POST | `/v1/sessions/{session_id}/compact` | `confirm`, `profile` | Re-arm native compaction on one session **without losing it**: clears the compaction-failure latch (`compression_failure_error`, `compression_fallback_streak`, `compression_ineffective_count`) and re-ensures the profile's early threshold, so Hermes' own compressor retakes the floor on the next turn. Only the named session is touched. Returns `{session_id, title, message, speak}`. `profile` missing/blank → `400`; a profile with no session store → `404 profile_unreachable`. Shipped consumer: iOS session list compaction (`HSCCClient.swift`). |
| POST | `/v1/sessions/{session_id}/retire` | `confirm`, `profile` | Non-destructive retirement: retitles the session to `<title>-retired-<ts>` so it leaves the live listable set while its full history stays on disk. Returns `{session_id, previous_title, retired_title, message, speak}` — `retired_title` is what to look for if the operator wants the history back. Same `400`/`404` contract as `/compact`. Shipped consumer: iOS session list retirement (`HSCCClient.swift`). |
| POST | `/v1/triggers/run` | `confirm` | Re-evaluate every enabled trigger rule now. Enabled rules may fire `notify` / `emit_event` / `auto_restart` / `block_pipeline` immediately, which is why this needs a confirm. Returns the run's own result plus `message: "trigger engine run"` and `speak`; `502 triggers_run_failed` when the backing raises or answers a non-dict. Shipped consumer: iOS trigger run (`HSCCClient.swift`). |

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

### Orchestrator chat (job API)

| Method | Path | Description | Mutates |
|---|---|---|---|
| POST | `/v1/orchestrator/chat` | Start an orchestrator chat **job**: validate + confirm-gate + resolve the project synchronously, then invoke hermes on a background thread and answer **`202` immediately** with `{job_id, project, status: "queued", elapsed, profile, session, speak}` — never the reply. Three keys are conditional and absent in the ordinary case: `notice` (the resolved orchestrator profile is busy with running kanban work — or its busyness cannot be verified), `session_created` (`{profile, title, session}` — this project's chat session did not exist and was created on first use) and `session_rotation` (`{profile, title, retired_session, retired_title, session, reason}` — the session was retired and recreated because compaction had positive evidence of failing). `400 bad_request` (missing/empty `prompt`, malformed attachment), `400 unknown_project`, `409 confirm_required`. Shipped consumer: iOS `OrchestratorChatJobResponse` (`Models.swift`, `HSCCClient.swift`). | ⚠ confirm |
| GET | `/v1/orchestrator/chat/{id}` | Poll one chat job. Always `{job_id, project, status, elapsed}`; `+ notice` while the job is still live and a busy notice was set at submission (DROPPED the moment the job turns terminal — a finished job never tells you to poll); `status: "done"` adds `reply`, `profile`, `session`, `speak`; a job carrying an error adds `error: {code, message, speak}` with `code` one of `orchestrator_error` / `orchestrator_unavailable` / `orchestrator_timeout` / `orchestrator_stopped`. `elapsed` is a live counter while `queued`/`running` and is **frozen at the termination instant** for every terminal state, so it always agrees with `status` and `error`. Unknown/expired id → `404 not_found`. Read-only: the orchestrator was already messaged by the POST, so no `confirm`. Shipped consumer: iOS `OrchestratorChatJobStatus` (`Models.swift`, `HSCCClient.swift`). | read |

`POST /v1/orchestrator/chat` talks to a project's **orchestrator directly**,
bypassing Telegram. This is the conversational endpoint of the API — the one
where an operator can say "go build X" and the orchestrator decomposes it and
dispatches real work onto its board. Because it can dispatch real work it is a
**mutation**, and therefore requires `"confirm": true` (409 otherwise) exactly
like every other mutating endpoint.

**Body:**

| Field | Type | Required | Meaning |
|---|---|---|---|
| `project` | string \| null | no | Project name; absent/`null` → the ONE **main orchestrator** (named at bootstrap, default `general`). Unknown project → `400 unknown_project`. |
| `prompt` | string | yes | What to ask the orchestrator. Missing/empty → `400 bad_request`. |
| `confirm` | bool | yes | Must be `true`, else `409 confirm_required`. |
| `image_data` + `image_mime` | string + string | no | An optional screenshot/photo attached to the message, `image_data` base64 and `image_mime` its type. **The two must come together** — one without the other is `400 bad_request`, as is non-base64, an empty image, or one over the decoded-size cap. |

Project → orchestrator resolution follows the C1 convention
(`hscc-roles/orchestrators.py`): a named project resolves to profile
**`<project>-orch`** / named session **`<project>`**; absent/null resolves to
the ONE **main orchestrator** named at bootstrap (`hscc-roles/main_orch.py`,
default **`general`**) — its stored name is the profile, session and board.

**Transport:** the API invokes Hermes directly —

```bash
hermes -p <profile> chat -Q --continue <session> -q "<prompt>"
```

— with the prompt passed as an argv element (never shell-interpolated), a
configurable timeout (default **600 s**, see `chat_timeout` below), and quiet
mode so the reply is the only thing on stdout.
`chat --continue` resolves the session by title from the profile's `state.db`
and persists the exchange into it (the Telegram-topic analog). The
`localhost:4000` proxy is litellm — a stateless OpenAI-compatible inference
relay with no Hermes session store, so it is NOT used here.

**It is a JOB API (not streaming).** `hermes chat -Q` emits the reply ONLY
once, complete, when the underlying run finishes — there is no supported
incremental/streaming interface (no `--stream`/SSE/ndjson; quiet mode explicitly
nulls the stream callback, `cli.py:17863`). So the POST does **not** block for
the (30-90 s, sometimes much longer) reply. Instead:

* **`POST /v1/orchestrator/chat`** validates + confirm-gates + resolves the
  project synchronously, spawns a background thread that invokes hermes, and
  returns **`202 Accepted` immediately** with a `job_id` — the request/response
  path returns in milliseconds, not minutes.
* **`GET /v1/orchestrator/chat/{id}`** polls the job: `queued` / `running` /
  `done` (with the `reply`) or a terminal failure state, plus an honest
  `elapsed` seconds counter. The phone shows real progress; a backgrounded app
  can pick the finished answer up later by `job_id` (a job that outlives the
  connection that submitted it is no longer lost).

**Timeout semantics:** because a job holds no connection open (the POST returns
in ms, the GET polls), a short "reply latency" budget is meaningless — the
honest model is *report elapsed and let the operator decide*. The worker still
runs the `hermes chat` process under a **wedge-backstop** so a genuinely stuck
(or orphaned) process cannot burn GPU forever, but the default is a generous
**600 s** (~3.5× the ~165 s measured while three kanban workers were busy; the
old 180 s sat barely above loaded latency and normal busy-cluster chats timed
out). It is configurable as `chat_timeout` (seconds) in `~/.hscc/api.json`:

```json
{ "chat_timeout": 900 }
```

In every **terminal** state (`done` or a failure) `elapsed` is **frozen at the
moment the job terminated** (`finished_at − submitted_at`), so the reported
elapsed always agrees with the status and the error message — a job marked
`timeout` reports the seconds it actually ran, *never* the minutes the operator
spent polling it afterward.

**`POST` response `202`:**

```json
{
  "job_id": "chat-1",
  "project": "hscc",
  "status": "queued",
  "elapsed": 0.0,
  "profile": "hscc-orch",
  "session": "hscc",
  "speak": "Chat request accepted (job chat-1)."
}
```

**`GET /v1/orchestrator/chat/{id}` — running:**

```json
{ "job_id": "chat-abc…", "status": "running", "elapsed": 47.3, "profile": "hscc-orch", "session": "hscc" }
```

**`GET` — done:**

```json
{
  "job_id": "chat-abc…",
  "status": "done",
  "elapsed": 87.2,
  "profile": "hscc-orch",
  "session": "hscc",
  "reply": "<the orchestrator's reply text>",
  "speak": "hscc-orch says: <short summary>"
}
```

**`GET` — terminal failure** (`failed`), carrying the same error `code` a
blocking call would have returned as an HTTP status:

```json
{ "job_id": "chat-abc…", "status": "timeout", "elapsed": 180.1, "error": { "code": "orchestrator_timeout", "message": "…", "speak": "…" } }
```

The terminal `error.code` is one of `orchestrator_error` / `orchestrator_unavailable`
/ `orchestrator_timeout`; the `POST` itself only returns 400/409 (validation) or
202 (accepted). The `server` never crashes and never leaks a traceback or token
on a bad orchestrator call:

| Status | `code` | When |
|---|---|---|
| 400 | `unknown_project` | `project` is neither a registry project nor the main orchestrator's name |
| 502 | `orchestrator_error` | the hermes invocation failed (empty reply / bad exit) |
| 503 | `orchestrator_unavailable` | the orchestrator profile/session is not reachable (e.g. the named session hasn't been created yet) |
| 504 | `orchestrator_timeout` | the orchestrator did not reply within the configured timeout (default 600 s) — the reported `elapsed` equals the seconds it actually ran |

---

## Error contract

Every error response shares one shape:

```json
{ "error": { "code": "...", "message": "...", "speak": "..." } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | malformed/missing field, non-object body, body > 1 MiB |
| 400 | `unknown_project` | orchestrator chat `project` is neither a registry project nor the main orchestrator's name |
| 401 | `unauthorized` | missing or invalid bearer token |
| 404 | `not_found` | unknown route, unknown card, card not reviewable |
| 405 | `method_not_allowed` | valid path, wrong HTTP method |
| 409 | `confirm_required` | mutating call without `confirm: true` |
| 409 | `already_landed` | merge target already merged |
| 500 | `internal_error` | unhandled exception (traceback logged server-side only) |
| 502 | `merge_failed` / `apply_failed` / `stop_failed` / `orchestrator_error` | a mutation did not land / the orchestrator call failed |
| 503 | `orchestrator_unavailable` | orchestrator profile/session not reachable |
| 504 | `orchestrator_timeout` | orchestrator did not reply within the timeout |

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
