# Widget / Live Activity data contract — five datasets audited against the live API

Card: `t_a873a0a2` · Owner: ios-engineer · Status: **AUTHORITY** (this file, not a
card comment, is the input every widget/Live-Activity card reads)

The iOS pivot turns `ios-app/` into a widget + Live Activity client: connection
settings, one small config screen, and read-only surfaces. That changes what a
payload has to *be*: a widget extension refetches on a timeline cadence for a
surface that renders 1–8 lines, and a Live Activity content state has a hard
ceiling. This file measures what the five datasets cost today, what each view
family actually needs, and which datasets can be served from the routes that
already exist.

**Verdicts in one line each:** projects `reuse` · board status counts
`needs compact server endpoint` · kanban monitor `narrow client-side` · cards
monitor `narrow client-side` · cluster monitor `narrow client-side`. Exactly one
dataset authorises the compact-endpoint card (`t_8580f9a3`), and §6 is its spec.

---

## 1. How this was measured (provenance)

Every byte figure below is a **real response body from the running `hscc-api`**,
captured 2026-11-17 ~08:30 UTC, read-only `GET`s, bearer token from
`~/.hscc/api-token`, base address **derived at runtime** from `hscc api status`
and never written into this repo (per the repo rule; the live address appears
nowhere in this file, and `100.64.0.1` below is the documented placeholder used
in §7's examples).

```bash
BASE=$(hscc api status | sed -n 's/.*Listening: *\([0-9.]*:[0-9]*\).*/http:\/\/\1/p' | head -1)
TOK=$(cat ~/.hscc/api-token)
curl -sS -H "Authorization: Bearer $TOK" -H "Accept: application/json" "$BASE/v1/cards" | wc -c
```

Conditions worth knowing when these numbers are re-checked:

| condition | value during measurement |
|---|---|
| boards registered | 23 |
| projects registered | 18 |
| open cards (fleet-wide) | 40 — `todo` 17, `blocked` 15, `running` 6, `ready` 2 |
| cards incl. archived | 2,541 — `done` 1,755, `archived` 746, + the 40 open |
| fleet power state | **down** (`/v1/autodown/status` → `state: down`) |
| transport compression | none — `Content-Encoding: identity` on every route |

The fleet being **down** is the one condition that changes two rows: with the
fleet down, `hscc cluster monitor` fails and `/v1/cluster/monitor` and
`/v1/cluster/status` degrade to a single `speak` key (38 B and 39 B — see §5.4).
Those two rows are therefore measured from the committed up-state fixture
`ios-app/scripts/model_decode_check/fixtures/v1_cluster_monitor.json` (4 nodes,
31 sample fields per node), and labelled *fixture* wherever quoted. Everything
else is live. **Re-measure §5.4 once against a powered-up fleet before the
cluster-widget card trusts the size.**

Byte conventions: *wire bytes* = the exact length of the response body as
delivered (what the phone pays for). *minified* = `json.dumps(separators=…)` of
the parsed body, used only for per-field cost attribution (the API pretty-prints
nothing, so wire ≈ minified for the live routes; the fixture is pretty-printed,
so its 13,468 B on disk is 10,747 B minified).

## 2. The budget a widget payload has to fit

| constraint | value | why it binds here |
|---|---|---|
| Live Activity `ContentState` | **≤ 4 KB** (Apple: "The size of its State property can't exceed 4KB in size", `Activity/update(_:alertConfiguration:timestamp:)`) | the LA driver (`MonitorActivityDriver`, `SessionActivityDriver`) builds a `ContentState` from a fetch; anything bigger is rejected by the system, not by us |
| ActivityKit push update | same 4 KB `content-state`, plus a per-hour push budget | if an LA is ever driven by push, the payload *is* the contract |
| Widget timeline refresh | the app's own cadence knob (`WidgetSettingsStore`, minutes; existing widgets use 5 min) × every installed surface | 82 KB × 12 refreshes/h × 2 surfaces = ~2 MB/h on cellular |
| Extension runtime | a widget/LA extension is killed silently if it overruns its memory/wall-clock ceiling | a 8.6 MB decode (`?include_archived=1`, §4.2) is the failure mode, not an error you can catch |

Working budget this contract sets (my decision, binding on the widget cards):

* **decoded content state ≤ 4 KB** (hard, Apple's);
* **wire ≤ 16 KB per surface per refresh** (soft — one request, one board,
  no `body` field);
* **≤ 2 requests per surface per refresh**;
* **no raw server body may be persisted into the App Group container** — only
  the projected struct (§7).

## 3. The table (the input every widget card reads)

| # | dataset | endpoint(s) today | wire bytes today (measured) | verdict | what a widget card must do |
|---|---|---|---|---|---|
| 1 | **projects** | `GET /v1/projects` (`ProjectsView.swift:169`) | **1,854 B** / 18 projects | `reuse` | decode `ProjectsResponse`, keep `{name, board}` only (~715 B); never copy `repo` into shared storage |
| 2 | **board status counts** (todo/ready/running/blocked/done) | *no route returns them.* Client derives them from `GET /v1/cards` (`ProjectsView.swift:846,855,863`); `done` is unreachable | **81,855 B** for open-only counts, and `done` needs **8.6 MB** (`?include_archived=1`) | **`needs compact server endpoint`** | stop deriving from `/v1/cards`; wait for / consume `GET /v1/mobile/summary` (§6) |
| 3 | **kanban monitor** (running + blocked + stale across boards) | `GET /v1/kanban/running`, `/v1/kanban/blocked`, `/v1/kanban/stale?older_than=0` (`ClusterWidget.swift:140-143`) | **2,024 + 4,386 + 5,335 = 11,745 B** (3 requests) | `narrow client-side` | keep counts + ≤5 rows; drop `comments`/`why`/`boards[]`; cache the projected struct only |
| 4 | **cards monitor** (per-card rows: title, status, board, age) | `GET /v1/cards` (`CardsRootView.swift:127`, `ProjectsView.swift:846`) | **81,855 B** / 40 cards; `?board=hscc` → 42,588 B | `narrow client-side` | **switch route** to the kanban trio (rows already projected, §5.3) or scope `?board=`; never `include_archived` |
| 5 | **cluster monitor** (per-node CPU/RAM/GPU + daemon host) | `GET /v1/cluster/monitor` + `/v1/daemon/host` (`MonitorWidget.swift:159-160`) | **10,747 B** (fixture, up-state) + **439 B**; 38 B when the fleet is down | `narrow client-side` | discard `output` (54% of the body) after decode; project 6 fields/node = 225 B; never persist `host` (§7) |

Two structural facts behind rows 2 and 4, both read from the server, not
inferred:

* `/v1/cards` has **no field projection** — `handle_cards`
  (`hscc-api/routes_project.py:190-208`) returns `list_cards()` verbatim, so the
  65 KB of card `body` text (81% of the payload) rides along even for a request
  that wants four numbers. Its filters are `?board=` (server-side), `?status=`
  (filtered *after* the full list is built, so it saves wire but not server
  work) and `?include_archived=1`.
* **`done` cards are only reachable through `?include_archived=1`**, which costs
  8.6 MB fleet-wide / 2.7 MB for one board. `/v1/cards?status=done` returns
  `{"cards": [], "count": 0}` — 46 B of a lie (§4.2).

## 4. Field inventory, per dataset

### 4.1 projects — `GET /v1/projects` · 1,854 B wire / 1,706 B minified

```
projects[]  1,648 B   18 items × {board, name, repo, topic}
speak          25 B
count           2 B
```

`repo` is an **absolute local path on the operator's machine** and is 58% of the
per-project bytes; `topic` is an integer nothing renders. `{name, board}` for
all 18 projects is **715 B**. `GET /v1/projects/{name}` (836 B) additionally
carries `board_counts`, `git{}` and `session_health{}` — see §5.2, it is the
closest existing thing to a status-counts route.

### 4.2 cards — `GET /v1/cards` · 81,855 B wire / 40 open cards

| variant | wire bytes | cards | status histogram |
|---|---|---|---|
| `/v1/cards` | **81,855** | 40 | todo 17, blocked 15, running 6, ready 2 |
| `?board=hscc` | 42,588 | 20 | todo 17, running 3 |
| `?board=headless-display` | 17,152 | 10 | blocked 10 |
| `?status=running` | 15,369 | 6 | running 6 |
| `?status=blocked` | 23,253 | 15 | blocked 15 |
| `?status=done` | **46** | **0** | *(empty — see below)* |
| `?include_archived=1` | **8,625,393** | 2,541 | done 1,755, archived 746, +40 open |
| `?board=hscc&include_archived=1` | **2,742,643** | 884 | done 750, archived 114, +20 open |

Per card: mean **2,015 B**, min 452 B, max 6,413 B. Field attribution over the
40 cards: `body` **65,486 B (81% of the array)**, `title` 3,353 B,
`workspace_path` 632 B, everything else ~11 KB. Card keys:
`assignee, board, body, branch, completed_at, created_at, id,
last_heartbeat_at, priority, started_at, status, title, workspace_kind,
workspace_path`.

Projections of the same 40 cards: `{id, title≤60, status, board}` = **4,445 B**;
counts-only (`{"counts":{...},"count":40}`) = **68 B**. So the bytes a
status-counts widget needs are **0.08%** of what the route sends.

`?status=done` returning zero cards while `?include_archived=1` returns 1,755
`done` cards is why row 2 cannot be served today: `list_cards(board=…)`
excludes closed cards unless archived is requested, and the archived path is
three orders of magnitude too big for an extension.

### 4.3 kanban trio — `running` / `blocked` / `stale`

| route | wire | tasks | field cost (minified) |
|---|---|---|---|
| `/v1/kanban/running` | 2,024 B | 6 | `tasks` 1,565 B · `boards` 264 B (23 names) · `speak` 18 · `count` 1 |
| `/v1/kanban/blocked` | 4,386 B | 15 | `tasks` 4,018 B (`why` + `comments` + `age_days` per card) · `speak` 36 |
| `/v1/kanban/stale?older_than=0` | 5,335 B | 25 | `tasks` 4,655 B · `boards` 264 B · `count`/`older_than` 3 |

`running.tasks[]` keys `assignee, board, host_local, id, pid, started_at,
status, title`; `blocked.tasks[]` keys `age_days, assignee, block_kind, board,
comments, id, status, title, why`; `stale.tasks[]` keys `age_days, assignee,
board, id, status, title`.

Two things a widget card must know: **`?board=` is ignored** by
`/v1/kanban/running` (measured: `?board=hscc` → 2,024 B, byte-identical to the
unscoped call — the route has no board filter, unlike `/v1/cards`), and the
`boards` array is a bare list of 23 board names (264 B) duplicated in two of the
three responses. Projected to `{id, status, board}` per task plus meta: 679 B /
1,046 B / 1,679 B. Counts-only: 297 B / 36 B / 298 B.

Note `stale.tasks[]` — **every non-terminal card with `title`, `status`,
`board`, `age_days` and no `body`** — is already the compact card-row projection
row 4 wants (§5.3).

### 4.4 cluster monitor — `GET /v1/cluster/monitor` (+ `/v1/daemon/host`)

Up-state (fixture, 4 nodes), 10,747 B minified:

| key | bytes | note |
|---|---|---|
| `output` | **5,857 B (54%)** | the ANSI-rendered `hscc cluster monitor` table, as one string — useless to a view |
| `json` | 4,813 B | `{timestamp, hosts[]}`; per host ≈ **1,191 B** |
| `speak` | 34 B | |
| `success` | 4 B | |

`hosts[]` = `{host, error, sample, used_slots, free_slots, workloads}`;
`sample` has **31 string-valued fields** (`cpu_*`, `mem_*`, `swap_*`,
`gpu_*` ×13, `sparkrun_jobs`, `sparkrun_job_names`, `hostname`,
`uptime_sec`, `timestamp`, …). Every numeric arrives as a string, `""` when
unreadable — the existing `MonitorSample` accessors
(`Sources/Shared/SharedModels.swift:220-243`) already parse that defensively;
reuse them, don't re-derive.

Down-state (live, fleet powered off): `{"speak": "fleet monitor unavailable"}` —
**38 B**, and `/v1/cluster/status` → `{"speak": "cluster status unavailable"}` —
**39 B**. That is `handle_cluster_monitor`'s degrade path
(`hscc-api/routes_cluster.py:363-370`): no `json` key at all, so `MonitorResponse.json`
must stay optional and the widget must fall back to its last-known snapshot.

Projection to what the views render (`label, cpu%, mem%, gpu util%, used_slots,
free_slots` for 4 nodes) = **225 B**. Sidecar `GET /v1/daemon/host` = 439 B over
13 keys; a monitor surface uses ~5 of them (`hostname`, `uptime_seconds`,
`cpu`, `memory`, `disk`). `GET /v1/autodown/status` = 926 B, of which
`blocked_by` alone is 398 B of prose; a widget uses `state`, `enabled`,
`idle_minutes` (~40 B).

## 5. Per-dataset verdict, and what each view family needs

Family vocabulary: `accessoryCircular` renders **one scalar + one colour** (no
label text, ~3-4 characters); `accessoryRectangular` ~**2 lines × ≤20 chars**;
`accessoryInline` **one short line, ≤ ~25 chars**; `small` one hero metric +
2-3 secondary rows; `medium` a 2×2/2×3 grid of status tiles **or** 3-4 card
rows; `large` up to ~8 rows (title ≤ 60 chars + status + age) or 4 node rows ×
3 metrics. All of it is read-only — no surface here mutates anything.

### 5.1 projects — verdict `reuse`

| family | needs | source |
|---|---|---|
| accessoryInline | `"18 projects"` / `"hscc · 20 open"` | `{count}` + one project |
| accessoryCircular | project count (scalar) + colour = any project `compaction_at_risk` | would need `board_counts` → see §5.2, not this route |
| accessoryRectangular | name + open count (2 lines) | `{name, board}` + §5.2 counts |
| small | count + up to 3 project names | 715 B projection |
| medium | up to 6 project rows `{name, board}` | 715 B projection |
| large | up to 8 rows + board label | same payload |

1,854 B is already inside budget, so `reuse`: the widget decodes the existing
`ProjectsResponse` and keeps `{name, board}`. **Constraint:** `repo` is a local
absolute path and must not be copied into the App Group store or rendered
(§7). A project list without counts is a weak widget — the board-status card
(`t_6d8731ab`) should pair this payload with §6's counts block rather than
invent a third read.

**Amendment (CARD A3, 2026-11-18).** The operator's ground truth is the CLI
(`hscc project list` + `hscc kanban monitor`), so `GET /v1/projects` now adds
`health`, the board's status `counts` (including `done`/`archived`), the newest
board activity and the live orchestrator turn. Those fields are optional on the
wire: an older server still sends `{name, board}` and the widget renders `—`
rather than a fabricated zero. `repo` remains undecoded and unstored.

### 5.2 board status counts — verdict **`needs compact server endpoint`** (the one that authorises `t_8580f9a3`)

The widget is five numbers for one board (`t_6d8731ab`: todo/ready/running/
blocked/done, board selectable in configuration).

**Amendment (CARD B3, 2026-11-18).** `done` means `done_24h` — the CLI board
table's **Done 24h** column — and is labelled that way on every family. The
route's `projects[].counts` and `boards[].counts` come from the same
`kanban_live.board_status_counts`, so the Projects widget, Board Status tile and
Board Progress Live Activity cannot disagree about a board's running/ready/todo/
blocked/done-today numbers.

| family | needs | bytes that must travel |
|---|---|---|
| accessoryInline | `"3 run · 15 blocked"` (≤25 chars) | 5 ints |
| accessoryCircular | **one** scalar (blocked count, or open total) + colour (blocked>0 → red) | 1 int + 1 bool |
| accessoryRectangular | 2 lines: `"hscc"` + `"17 todo · 6 run"` | 5 ints + board name |
| small | 5 status tiles + board name | 5 ints + board name |
| medium | 5 tiles + board name + last-refresh age | 5 ints + 2 strings |
| large | 5 tiles + up to 5 card rows | 5 ints + rows (see §5.3) |

Today's derivation and why it fails:

* `ProjectsView.swift:846,855,863` downloads **`/v1/cards` (81,855 B) +
  `/v1/kanban/blocked` (4,386 B) + `/v1/kanban/stale` (5,335 B)** = **91,576 B**
  and filters client-side to produce a handful of counts. For a widget that
  refreshes every 5 minutes, ≈ 1.1 MB/h per surface.
* `done` is not in any of them. The only route that can count it,
  `?include_archived=1`, costs **8.6 MB** fleet-wide / **2.7 MB** for one board
  — not a widget payload by any reading, and 4× over even a generous extension
  ceiling. `?status=done` returns **0 cards** (§4.2), so a "reuse" or
  "narrow client-side" answer would ship a widget that silently renders `done`
  as 0 forever. That is the honesty failure the fleet's values do not allow.
* The nearest existing route is `GET /v1/projects/{name}` (836 B), whose
  `board_counts` **are** computed server-side (`_project_detail`,
  `hscc-api/routes_project.py:469-487` → `list_cards(board=…)` → status
  histogram + `total`). Live today: `{"running": 3, "todo": 17, "total": 20}`.
  Three things rule it out as the answer: it is keyed by **project name**, not
  board (18 names ≠ 23 boards, and one board can back several projects); it is
  **one request per project** (a fleet-wide tile needs N+1 reads); and because
  it calls `list_cards()` *without* archived, **it has no `done`/`archived` key
  either** — a missing key is indistinguishable from a real zero.

So: **`needs compact server endpoint`, and this dataset is the sole authority
for `t_8580f9a3`.** The spec it must satisfy is §6. Until that endpoint lands,
the board-status card may ship the *open-only* subset (todo/ready/running/
blocked) from `/v1/kanban/{running,blocked,stale}` counts (3 reads, 11.7 KB,
row 3) and must render `done` as `—`, never as `0`.

### 5.3 kanban monitor — verdict `narrow client-side`

The three kanban reads (`running`, `blocked`, `stale`) are already a
projection-first design: small envelopes, per-task fields only what a row
renders, `speak` for voice. Total **11,745 B over 3 requests** — one request
over the ≤2 budget, and ~1.1 KB of it is per-task text the small family will
never draw.

| family | needs | fields |
|---|---|---|
| accessoryInline | `"6 running · 15 blocked"` | `count` ×2 |
| accessoryCircular | running count + colour (blocked>0 → red) | `count`, `blocked>0` |
| accessoryRectangular | running/blocked, 2 lines | `count` ×2 |
| small | running/blocked/stale + 1 title | 3 counts + `tasks[0].title` (≤40) |
| medium | 3 counts + up to 4 rows `{title≤40, board}` | counts + `tasks[].{title,board}` |
| large | 3 counts + up to 7 rows `{title≤60, board, age_days}`; blocked rows may add `block_kind` | + `age_days`, `block_kind` |

**Amendment (CARD B3, 2026-11-18).** The Kanban Monitor widget now draws the
CLI's **board table**, not a card list: one `GET /v1/mobile/summary` read gives
`boards[].counts.{running,ready,todo,blocked,done}` with `done` = Done 24h,
which the three-route trio cannot answer. The trio remains the card-list source
for the Cards Running surface; its narrowing rules below still bind there.

Client-side narrowing (binding): request the three routes as-is (they are the
cheapest per-row source in the API), decode with the existing `Lite` models
(`Sources/Shared/SharedModels.swift:83-160` — deliberately minimal decoders),
then keep only counts + the top N rows, sorted by `age_days`; drop
`blocked.tasks[].comments` and `why` beyond the single blocked row a large
surface shows, and drop `boards[]` (264 B × 2) — the widget never enumerates
boards it isn't rendering. Projected result: **~1.2 KB**, well inside budget.
`?board=` does not work on `running` (measured), so do the board filter
client-side and do not add the query param expecting a saving.

### 5.4 cards monitor — verdict `narrow client-side` (by switching route, not by filtering)

Rows of `{title, status, board, age}` — the "what is moving" list.

| family | needs | rows |
|---|---|---|
| accessoryInline | `"15 blocked"` | 0 (counts only — use row 2/3) |
| accessoryCircular | one count | 0 |
| accessoryRectangular | 1 row: `title≤20` + status | 1 |
| small | 2-3 rows `{title≤32, status}` | 3 |
| medium | 4 rows `{title≤40, status, board}` | 4 |
| large | 7-8 rows `{title≤60, status, board, age_days}` | 8 |

`/v1/cards` should **not** be the widget's route: at 2,015 B per card, the
`large` surface pays 81,855 B to draw 8 rows — 98.6% of the payload is
discarded (the `body` field alone is 81%, and a widget never renders a card
body). `/v1/kanban/stale?older_than=0` carries the *same* rows a card list
shows (title/status/board/age_days, plus `assignee`), for 25 cards in **5,335 B**
— 15× cheaper per row, because the projection is already server-side. Pair with
`/v1/kanban/running` for live workers and `/v1/kanban/blocked` for the failure
rows.

Client-side narrowing (binding): use the kanban trio above; if a card list must
come from `/v1/cards`, then **always** scope it (`?board=<board>`, optionally
`?status=`), **never** pass `include_archived=1` from an extension, and discard
the decoded object immediately after projecting — the raw response must never
reach the App Group store. This row is `narrow client-side` rather than a
compact-endpoint case because a `large` surface fits inside 16 KB on both paths;
the compact endpoint is not justified by row-size arithmetic here, only by row 2.

### 5.5 cluster monitor — verdict `narrow client-side`

| family | needs | fields |
|---|---|---|
| accessoryInline | `"CPU 12% · GPU 0%"` | `sample.cpu_usage_pct`, `gpu_util_pct` |
| accessoryCircular | fleet scalar (avg CPU %, or nodes up / nodes total) + colour | `used_slots`/`free_slots`, `state` |
| accessoryRectangular | 2 lines: `".1 CPU 12%"`-style node label + `CPU/MEM` | `cpu_usage_pct`, `mem_used_pct` |
| small | state + 1 node × (cpu, mem, gpu) + idle-minutes-remaining | `sample`, `autodown.status.idle_minutes` |
| medium | up to 4 nodes × (label, cpu, mem, gpu util, slots) | same, 4 nodes |
| large | 4 nodes × 3 metrics + daemon host line (`hostname`, `uptime_seconds`, disk %) + fleet state | + `/v1/daemon/host`, `/v1/cluster/status` |

Up-state cost 10,747 B for 4 nodes is survivable, and 54% of it is the
`output` table string the view cannot use — so the narrowing is: **decode with
`MonitorResponse`/`MonitorSample` as they exist, use `nodes` (the `json.hosts`
passthrough), throw the object away.** The projected entry is 225 B for 4 nodes
+ 439 B daemon-host = **under 700 B**, comfortably inside budget, which is why
this row does **not** need a server change: the fat is a constant, not
proportional to board/card growth, and 10.7 KB is one small request.

Two hard caveats: (a) when the fleet is down the route returns **no `json`
key** (38 B) — `json` stays optional, the entry falls back to the last-known
snapshot with an age, exactly as `MonitorWidget` does today; (b) `hosts[].host`
is the node's **LAN address** and `sample.sparkrun_job_names` can carry
workload names — neither may be persisted or rendered (§7). Re-measure this row
against a powered-up fleet (see §1).

## 6. The compact endpoint — authorised, scoped to dataset 2

`Authorised: YES` — one dataset (`board status counts`) requires a server
change; the other four are served by routes that exist. `t_8580f9a3` may build
`GET /v1/mobile/summary`, and this section is its contract. The other four
datasets are **not** licence to fold anything else into that route; if a widget
card later wants a block below in the payload, it asks here first.

```
GET /v1/mobile/summary[?boards=<a,b,c>][&rows=<n>][&with=cluster,kanban,projects]
200 →
{
  "generated_at": "2026-11-17T08:30:00Z",
  "boards": [
    { "board": "hscc",
      "counts": { "todo": 17, "ready": 0, "running": 3, "blocked": 0,
                  "done": 750, "archived": 114 },
      "total": 884 }
  ],
  "speak": "hscc: 20 open, 3 running, 750 done."
}
```

Non-negotiable rules (each one exists because of a measurement above):

1. **All five statuses always present, as integers, never absent.** A missing
   key must not be renderable as `0` — that is today's bug in `board_counts`
   (§5.2). Zero is a real value; absence is a bug. `done` + `archived` must come
   from the archived-inclusive listing (`list_cards(board=…,
   include_archived=True)`), **counted server-side** — the whole point is that
   2,541 cards / 8.6 MB never cross the wire.
2. **Default = all boards, one request.** `?boards=` narrows it. A widget must
   be able to render every board tile from one call (§5.1, §5.2's N+1 problem).
3. **`rows` optional and capped** (default `0` = no rows). If implemented, rows
   are `{id, title≤80, status, board, age_days}` — the §4.3 projection, never
   `body`, never `workspace_path`.
4. **Redaction is structural, not a scrub**: no `repo`, no `workspace_path`, no
   `branch`, no LAN/tailnet address, no absolute path, no token material, no
   `speak` text copied from a config file. `boards[]` is a board *name*, which
   is already public in the app's own UI.
5. **Budget: the response must be < 4 KB** with all 23 boards (23 boards ×
   ~90 B ≈ 2.1 KB today — comfortably met) so a Live Activity can carry it
   (§2). A test must assert the ceiling with a synthetic 50-board/5,000-card
   registry, and must assert `?boards=` scoping caps the size too.
6. **Read-only + same auth** as every other route (`_authorize`); no new auth
   surface, no mutation, no caching of the response body by the client beyond
   the projected struct (§7).
7. **Degrade honestly**: if the board listing fails, return the `speak` line and
   omit `boards` rather than returning empty counts — an empty `counts` map
   would be read as "board is quiet".

Optional blocks (`with=`), each governed by the same redaction and 4 KB budget,
each *only* if a widget card asks for it: `kanban` =
`{running, blocked, stale, rows[]}` (saves the 3-request budget in §5.3),
`cluster` = §5.5's 225 B node projection with `label` in place of `host`,
`projects` = §4.1's `{name, board}`. None of them is authorised by this audit —
they are the shape they would take if asked for.

## 7. Payload hygiene (binding on every widget/LA card)

The App Group container (`group.com.hscc.ios`) is readable by every extension
and survives app termination, so anything persisted here outlives the request
that fetched it. Rules:

* **Persist the projection, never the response.** The typed entry structs in
  `Sources/Shared/` are the storage shape. No raw JSON body, no `Data` blob of
  a server response.
* **Fields that must never enter shared storage or a view**:
  `cards[].body` (free text; may quote any customer, code, or credential a card
  ever mentioned), `cards[].workspace_path`, `projects[].repo` (both are
  absolute paths on the operator's machine — measured at 632 B and 58% of the
  projects array), `cards[].branch`, `cluster.monitor hosts[].host` +
  `sample.sparkrun_job_names` (LAN addresses / workload identity),
  `autodown.blocked_by` (398 B of internal prose).
* Node identity for display = the last octet of the address, never the whole
  address — the existing `shortTail()` in the widget data-path check
  (`scripts/widget_datapath_live/template.swift`) is the precedent; a configured
  label beats it. In docs and tests, `100.64.0.1` is the placeholder.
* `speak` strings are operator-facing text the server already sanitises — safe
  to render, still not a place to persist.
* The token never enters the App Group store: `WidgetDataClient`
  (`Sources/Shared/WidgetDataClient.swift`, which replaced `ExtensionClient`)
  reads it from the shared keychain per client and the struct is `GET`-only by
  construction, with a closed allowlist of read paths — keep it that way.

## 8. Consequences the next cards should not re-litigate

1. `t_6d8731ab` (board status widget) builds against §6 for `done`; open-only
   counts may ship first from the kanban trio, with `done` rendered `—`.
2. `t_8580f9a3` (compact endpoint) is **authorised**, scoped to board status
   counts, spec = §6. It must not add `kanban`/`cluster`/`projects` blocks
   without a new ask here.
3. Any card that wants a card *list* uses the kanban trio, not `/v1/cards`
   (§5.4). If `/v1/cards` is used at all, `?board=` is mandatory and
   `include_archived` is forbidden in an extension.
4. No widget may derive board counts by downloading `/v1/cards` — that is
   91.6 KB for what §6 returns in under 1 KB. `ProjectsView`'s existing
   derivation is the app-console path and stays; it must not be copied into an
   extension.
5. The ≤4 KB / ≤16 KB / ≤2-requests budget in §2 is the acceptance number for
   every widget card.

## 9. Left / follow-ups (recorded, not done here)

* **Re-measure §5.4 and the two degraded rows against a powered-up fleet.** The
  fleet was autodown during this audit; `cluster_monitor` up-state is the
  committed 4-node fixture, not a live capture. Cheap to redo once the cluster
  is up; the widget card should not trust a fixture-sized number for a real
  node count.
* **`/v1/kanban/running?board=` is silently ignored** (measured byte-identical).
  Either implement the filter server-side (the route already walks per-board)
  or drop the param from the docs — a client that adds it expects a saving it
  never gets. Not fixed here: server change, outside a doc card.
* **`board_counts` has no `done`/`archived` key** even though the statuses
  exist (`hscc-api/routes_project.py:469-487` calls `list_cards(board=…)`
  without archived). If §6 lands, `/v1/projects/{name}` becomes the second
  place with a half-truthful count — worth an upstream card to make it honest,
  independent of the widget work.
* **No transport compression on `hscc-api`** (`Content-Encoding: identity`
  everywhere). `/v1/cards` at 82 KB is ~5-8 KB gzipped. That would blunt the
  row-4 argument but not row 2 (`done` at 8.6 MB is ~300 KB gzipped — still not
  a widget payload), so it changes no verdict here. Filed as an observation for
  whoever next touches the API's HTTP layer.
