# Widget / Live Activity routes

Authority: `ios-app/docs/WIDGET_DATA_CONTRACT.md`. This page is the route map
for every read an iOS widget or Live Activity can make, and the server/CLI code
path that owns the answer.

`WidgetDataClient` is GET-only and closed: it can build a request only for a
path in `WidgetDataPath.all`. The contract test
(`hscc-api/tests/test_contract_swift_routes.py`) parses those paths **and** the
two direct `client.get` calls in `MonitorWidget.swift` from the Swift sources,
then requires each one to be registered in `api_server.ROUTES`. A widget route
with no server route now fails CI instead of reaching a phone as a 404.

| Route | Consumer | Server handler | Same code path the CLI uses |
|---|---|---|---|
| `GET /v1/projects` | Projects widget, board/project pickers | `routes_project.handle_projects` | `flightdeck.core.registry.load_registry` + `project_lifecycle.project_health` (`hscc project list`), the archived-inclusive card read (`/v1/mobile/summary`'s projection), and `kanban_live.attach_orchestrators` (`hscc kanban monitor`) |
| `GET /v1/cards` | Cards screen, board lists | `routes_project.handle_cards` | `kanban_live.collect(registered_only=True)` → `card_rows()` — exactly the running + ready cards `hscc cards monitor` draws |
| `GET /v1/mobile/summary` | Board-status widget, Kanban monitor widget, board-progress Live Activity | `routes_mobile.handle_mobile_summary` | `kanban_live.collect(registered_only=True)` → `board_status_counts()` (`hscc kanban monitor`'s board table) |
| `GET /v1/kanban/running` | Cluster, running-cards, board-progress | `routes_kanban.handle_kanban_running` | `kanban_live.collect(registered_only=True).running` (`hscc cards monitor` / `hscc kanban monitor`), with kill detail merged onto those rows |
| `GET /v1/kanban/blocked` | Cluster, running-cards, kanban monitor | `routes_kanban.handle_kanban_blocked` | `hscc_daemon.kanban_blocked.list_blocked_tasks(registered_only=True)` (`hscc kanban blocked`) |
| `GET /v1/kanban/stale` | Cluster, kanban monitor, running-cards | `routes_kanban.handle_kanban_stale` | `hscc_daemon.autodown.list_stale_tasks(registered_only=True)` (`hscc kanban stale`) |
| `GET /v1/cluster/monitor` | Cluster + monitor widgets, monitor Live Activity | `routes_cluster.handle_cluster_monitor` | `hscc-cluster` engine `cmd_monitor` (`hscc cluster monitor`) |
| `GET /v1/cluster/status` | Cluster widget | `routes_cluster.handle_cluster_status` | `hscc-cluster` engine `cmd_cluster_status` (`hscc cluster status`) |
| `GET /v1/daemon/host` | Monitor widget, monitor Live Activity | `routes_ops.handle_daemon_host` | `hscc_daemon` host metrics (`hscc status` daemon-host block) |
| `GET /v1/autodown/status` | Cluster widget, cluster-health Live Activity | `routes_autodown.handle_autodown_status` | `hscc_daemon.autodown` + `lifecycle` status classifiers (`hscc autodown status`) |


## GET /v1/kanban/running

Source: `hscc cards monitor` / `hscc kanban monitor` → the
`kanban_live.collect(registered_only=True).running` list, projected by
`kanban_live.card_rows()` (`CARD_VIEW_FIELDS`). A row is one running card; the
route's `count` is `len(tasks)`, so it is exactly the number of running cards the
CLI counts. Kill detail (`pid`, `host_local`, `killable`, `pin_conflict`) is
merged onto the same rows by `routes_kanban.handle_kanban_running`; widgets do
not declare it.

| field | type | meaning |
|---|---|---|
| `tasks` | array | running cards, longest `run_s` first |
| `tasks[].board` / `.id` / `.title` | string | the card's board slug, id and title |
| `tasks[].worker` | string | `collect()`'s `profile or assignee` — the CLI panel header's `worker` |
| `tasks[].assignee` | string/null | the card's assignment; the fallback for a server that predates `worker` |
| `tasks[].status` | string | `running` for this route |
| `tasks[].started_at` | string/null | ISO-8601 UTC start, also carried as `started_epoch` |
| `tasks[].run_s` | number/null | seconds since the card started — the CLI's `run` column |
| `tasks[].hb_s` | number/null | seconds since the worker's last heartbeat — the CLI's `heartbeat` column |
| `tasks[].age_s` | number/null | seconds since the card was created |
| `tasks[].priority` | integer | card priority |
| `count` | integer | `len(tasks)`; the running-card count |
| `errors` / `boards` | array | the collector's board-read errors and registered board names |
| `speak` | string | one-line summary |

The running-cards widget shows `title`, `board`, `worker` (falling back to
`assignee`) and `run_s` (falling back to `started_at`), with `count` as its
headline. Its secondary blocked figure is `/v1/kanban/blocked`'s `count` — the
`hscc kanban blocked` number, never a headline for this surface.


## Card and board truth (CARD A2)

The board scope is **registered projects only**: a board is served only when a
project in `~/.flightdeck/registry.yaml` (`hscc project list`) names it in its
`board` field. Orphan sqlite files are not project boards and never appear in a
widget payload. The autodown idle predicate keeps its full-board safety scope;
only read surfaces narrow.

**What “cards” means.** `/v1/cards` is not every row in every sqlite file. It is
exactly the set the cards monitor draws: cards with status `running` (the
panels) plus `ready` cards waiting for a slot (the bottom lines). Blocked,
archived, or settled cards are served by `/v1/kanban/blocked`,
`/v1/kanban/stale`, or the board counts — never smuggled into `/v1/cards` as if
active. Only `?board=` is accepted, because `hscc cards monitor` has `--board`
and nothing else; `status=` and `include_archived=` are rejected with `400`
rather than returning a different set than the CLI.

**What “done” means.** `/v1/mobile/summary` counts `done` as the collector’s
`done_24h` — the same “Done 24h” column `hscc kanban monitor` prints. It never
reports the all-time archive total as today’s work. `total` includes open
statuses, archived, and done-today, so the client’s open count
(`total - done - archived`) matches the board view. `other` is an integer count
(the Swift model declares `Int`); per-status detail is under `other_statuses`,
which no *widget* decodes. The server's `/v1/projects` enrichment folds that
detail map to keep `scheduled`/`review` cards in its counts instead of letting
the integer mask them.

**Board-table consumers (CARD B3).** The Kanban monitor widget draws one row per
registered board with `counts.running`, `counts.ready`, `counts.todo`,
`counts.blocked` and `counts.done` (= Done 24h), sorted like
`kanban_live.boards_table`. The Board Status widget draws the same five fields
for one board, and the Board Progress Live Activity carries them across the Lock
Screen and Dynamic Island. No client derives a status from a card list; the
numbers are the route's fields, which are the CLI's columns.


Rule (card t_8743f98c): **every route a widget or Live Activity reads is served
by the same code path as the CLI command it stands for.** A route imports and
calls the CLI/library function; it never re-implements the read. Each route has
a parity test in `hscc-api/tests/test_widget_route_parity.py` that runs the CLI
function and the route against the same fixture state and asserts equal
numbers/rows.

The CLI is the ground truth. If the CLI and one of these routes disagree, the
route is wrong.

Failure shape on the cluster routes: HTTP 200 with
`{"error": "<specific cause>", "speak": "<command> failed: <cause>"}` plus the
route's normal empty collections. `error` is the backing command's stderr/exit
status or the engine-load reason — never the word "unavailable", never the auth
token. This is deliberate: a generic "unavailable" hid the 2026-11-18 outage
(the API process had been launched from a Homebrew Cellar directory that an
upgrade deleted).

---

## GET /v1/cluster/status

Source: `hscc cluster status` → `hscc-cluster/hscc.py::cmd_cluster_status()`
called by `hscc_daemon/hscc.py::_handle_cluster` (same function the CLI uses).

| field | type | meaning |
|---|---|---|
| `workloads` | array | one running sparkrun workload per row |
| `workloads[].name` | string | workload recipe/name as sparkrun reports it |
| `workloads[].tp` / `.pp` | string | tensor / pipeline parallel degree (`"?"` if absent) |
| `workloads[].container_id` | string | sparkrun container id |
| `workloads[].nodes` | array[string] | host IPs in that job's block; a tp=2 job lists both |
| `idle_hosts` | array[string] | host lines in sparkrun's Idle section |
| `total_hosts` | integer | `Total: N host(s)` from sparkrun, else workloads + idle |
| `speak` | string | `"{total} hosts up. {n} workload(s) running, {i} idle."` |

`raw_output` is intentionally not sent (the CLI uses it only for human
rendering; widgets have no use for the ANSI table and it inflates the body).

---

## GET /v1/cluster/monitor

Source: `hscc cluster monitor` → `hscc-cluster/hscc.py::cmd_monitor()` for the
sparkrun frame, plus `hscc_daemon/cluster_live.py::ServingPoller.poll()` — the
same two calls `hscc_daemon/cluster_render.py::render_monitor()` makes for the
CLI's one-shot view.

| field | type | meaning |
|---|---|---|
| `success` | bool | the sparkrun monitor frame was received and parsed |
| `output` | string | raw first JSON line (CLI fallback text; clients may ignore) |
| `json` | object | the parsed sparkrun frame |
| `json.timestamp` | number | sample epoch |
| `json.hosts` | array | every host the monitor tried to sample |
| `json.hosts[].host` | string | node address |
| `json.hosts[].error` | string/null | non-null when this node could not be sampled |
| `json.hosts[].sample` | object/null | per-node metrics; every value is a STRING, often `""` when unreadable |
| `sample.gpu_name`, `gpu_util_pct`, `gpu_temp_c`, `gpu_power_w` | string | GPU identity/util/temp/power shown by the CLI |
| `sample.gpu_mem_used_mb`, `gpu_mem_total_mb`, `gpu_mem_used_pct` | string | unified-memory VRAM where reported |
| `sample.cpu_usage_pct`, `cpu_temp_c`, `mem_used_pct`, `mem_available_mb`, `swap_*` | string | CPU/RAM/swap columns the CLI shows |
| `sample.sparkrun_jobs`, `sparkrun_job_names` | string | job count/name(s) on that host |
| `json.hosts[].workloads` | array | sparkrun workloads on that host (contains `workload_id`, containers/status) |
| `json.hosts[].used_slots` / `free_slots` | integer | sparkrun slot accounting |
| `serving` | array | the CLI's serving table: one row per (backend, model) |
| `serving[].node` / `.port` | string | backend address |
| `serving[].model`, `.routes` | string / array | served model and aliases |
| `serving[].running` / `.waiting` | integer | vLLM requests running / queued |
| `serving[].kv` | number/null | KV-cache fraction (0–1) |
| `serving[].gen_tps` / `.prompt_tps` | number/null | token rates (null on the first sample) |
| `serving[].prompt_total` / `.gen_total` | number/null | cumulative engine counters |
| `serving_proxy_error` | string/null | why the proxy route map could not be refreshed |
| `serving_error` | string | present only if the serving poll itself failed |
| `speak` | string | `"Fleet snapshot: N hosts sampled."` |

`serving` is additive: older clients decode only `json` and are unaffected.

---

## GET /v1/fleet/throughput

Source: `hscc throughput` → `hscc_daemon/throughput.py::compute_throughput()`
(the same library function the CLI command calls).

| field | type | meaning |
|---|---|---|
| `by_node` | object | metrics URL → `{prompt_tokens, generation_tokens, running, waiting}` |
| `fleet.prompt_tokens` / `generation_tokens` | number | fleet totals since each engine started |
| `fleet.running` / `waiting` | number | fleet request counts |
| `fleet.nodes_ok` / `nodes_total` | integer | reachable / configured backend count |
| `speak` | string | `"{nodes_ok} of {nodes_total} nodes healthy."` |

---

## GET /v1/daemon/host

Source: the daemon host's own OS counters (`hscc-api/routes_ops.py::
_backing_daemon_host`, psutil with a stdlib fallback). There is no separate
`hscc` CLI verb for this machine-local read; it is the host the daemon/API runs
on, not a cluster node. The iOS Monitor widget/Live Activity decodes exactly
this shape.

| field | type | meaning |
|---|---|---|
| `hostname` | string | local machine hostname |
| `platform`, `arch` | string | `macOS`/`Linux`, CPU arch |
| `uptime_seconds` | number | host uptime |
| `cpu.count`, `cpu.percent`, `cpu.load_avg` | integer/float/array | CPU topology/usage/load |
| `memory.total_gb`, `used_gb`, `percent` | number | RAM |
| `disk.total_gb`, `used_gb`, `percent` | number | root filesystem |
| `processes` | integer | process count |
| `daemon_running`, `daemon_uptime_seconds` | bool/number | HSCC daemon liveness |
| `speak` | string | one-line host summary |

---

## GET /v1/autodown/status

Source: `hscc autodown status` → `hscc_daemon/autodown.py::status_snapshot()`,
called by both `hscc_daemon/autodown_cli.py::_cmd_status()` and this route. The
route adds only `speak`.

| field | type | meaning |
|---|---|---|
| `enabled` | bool | autodown armed |
| `state` | string | **effective** state: `up` / `waking` / `down` / `error` |
| `state_recorded` | string | raw `autodown.json` state |
| `state_source` | string | `"autodown.json"` or `"live probe"` |
| `fleet_up` | bool/null | orchestrator health probe result when taken |
| `state_note` | string | present when the recorded state was contradicted by reality |
| `idle_minutes` | integer | idle limit |
| `last_activity_iso` | string/null | last activity stamp |
| `down_since` | string/null | down timestamp; **null when the effective state is up** |
| `wake_source` | string/null | who triggered the last wake |
| `reason` | string/null | last transition reason |
| `watchdog_blocked`, `watchdog_intentional` | bool/string/null | watchdog block state |
| `kanban_ok`, `kanban_reason`, `blocked_by` | bool/string/null | kanban interlock and the specific blocking work |
| `prci_active`, `prci_ok`, `prci_reason` | bool/bool/string | PR/CI interlock |
| `force_armed`, `force_armed_overrides` | bool/array | armed despite model-requiring cron jobs |
| `active_cron_cpu_only`, `active_cron_model` | array[string] | active cron jobs, classified |
| `speak` | string | human sentence derived from the payload |

Reality rule: when `autodown.json` says `down` but the serving layer's
orchestrator answers healthy, the status read reports `state: "up"`,
`state_recorded: "down"`, `down_since: null` and a `state_note`. The read is
**read-only** — it does not rewrite the file. The daemon's own disabled-state
cycle performs the same reconcile (debounced) so the file heals too.

## GET /v1/metrics/history

Source: `hscc_daemon/metrics_history.py` — `MetricsStore.host_rows` /
`MetricsStore.engine_rows` for tiers 1 and 2, and `MetricsRecorder.tier0_points`
for tier 0. The route adds only `speak`, `fields` and the `tier0` block; it
renames nothing, because the field names are sparkrun's `MonitorSample` names and
a rename is how a chart silently reads `nil` for a real number. Card t_5ae8cfcf;
the recorder itself is t_81af8884.

`?tier=` is **required** (0, 1 or 2) and anything else is a 400: which resolution
a chart draws is not something a client may guess. `host` filters node rows,
`node` filters engine rows, `since`/`until` are inclusive epoch-second bucket
starts. The tier decides the row source, not the client's preference:

| tier | resolution | window | read from |
|---|---|---|---|
| 0 | 2 s | 15 min | the daemon process's memory ring |
| 1 | 1 min | 24 h | `~/.hscc/metrics/history.db` |
| 2 | 5 min | 7 d | the same file |

| field | type | meaning |
|---|---|---|
| `tier` | integer | the tier that was answered (echoes the request) |
| `tier_meta.resolution_s`, `tier_meta.window_s` | integer | 2/60/300 and 900/86400/604800 |
| `source` | string | `sqlite_store`, `sqlite_store_absent`, `recorder_ring`, `recorder_ring_absent` |
| `hosts[]` | array | one bucket per node; keys below, oldest `ts` first |
| `hosts[].ts` | integer | bucket start, epoch seconds — the store's own key |
| `hosts[].n` | integer | samples that went into the bucket (1 on tier 0) |
| `hosts[].error` | string/null | why this bucket has no numbers, when it has none |
| `hosts[].cpu_usage_pct`, `cpu_temp_c`, `gpu_util_pct`, `gpu_temp_c`, `gpu_power_w`, `mem_used_pct`, `swap_used_mb`, `swap_total_mb`, `swap_used_pct`, `gpu_mem_used_mb`, `gpu_clock_mhz`, `net_recv_bytes_s`, `net_send_bytes_s`, `sparkrun_jobs` | number/null | sparkrun's `MonitorSample` fields, verbatim |
| `hosts[].cpu_temp_c_max`, `gpu_temp_c_max`, `gpu_power_w_max` | number/null | the bucket's peak, stored **beside** the average (tiers 1 and 2 only) |
| `engines[]` | array | one bucket per backend; identity is `node` + `port` |
| `engines[].run`, `wait`, `kv`, `gen_tps`, `prompt_tps` | number/null | serving counters for that bucket |
| `counts.host_rows`, `counts.engine_rows` | integer | rows in this response |
| `fields.host`, `fields.host_peaks`, `fields.engine`, `fields.engine_key` | array[string] | the vocabulary this response uses, so no client hardcodes a column list |
| `tier0.points`, `tier0.capacity` | integer | the ring's depth: 0 right after a daemon restart is normal |
| `tier0.readable_here`, `tier0.source`, `tier0.reason` | bool/string | whether this process could read the ring at all |
| `stats.db_bytes`, `stats.bound_bytes`, `stats.tier1_rows`, `stats.tier2_rows`, `stats.exists` | integer/bool | the recorder's own `stats()`: the store's size against its stated 10 MiB bound |
| `error` | string | present only when the read itself failed; the rows are then empty |
| `speak` | string | one-line summary |

Three reality rules, all three load-bearing:

* **A `null` metric means "not reported", never 0.** A node that stopped
  answering must not render as an idle one; the `error` field says why the bucket
  is empty. A client that coalesces `null` to `0` forges an idle node.
* **A temperature or power chart draws the average AND the peak.** `gpu_temp_c`
  is the bucket's mean and `gpu_temp_c_max` is what it reached; the peak column
  exists precisely because a 1-minute mean is what hides a throttle spike.
  `fields.host_peaks` names the peak columns so a surface can check it drew them.
* **Tier 0 is not "nothing happened" when it is empty.** The 2 s ring lives in the
  daemon's memory and the API is a *separate process* (`hscc start-daemon` vs
  `api_server serve`), so the route never constructs a sampler — constructing one
  would start a second `sparkrun cluster monitor` and a second roll-up watermark
  writer. `tier0.points` may honestly read 450 while `hosts` is empty; the pair
  `tier0.readable_here` + `tier0.source`/`tier0.reason` is what a surface branches
  on, and `hasLiveTier0` on the Swift side is the same rule in one call. An empty
  tier-1 read whose `stats.exists` is false means the daemon has not rolled a
  bucket up **yet** — not that the fleet was idle for 24 hours.

## Project rows (CARD A3)

`GET /v1/projects` keeps the existing identity fields (`name`, `repo`, `board`,
`topic`) and adds the board truth the Projects widget draws:

| Field | Meaning | Owner |
|---|---|---|
| `health` | `ok` / `partial` / `absent` | `project_lifecycle.project_health` — identical to `hscc project list` |
| `counts` | status histogram + `total`, including `done` / `archived` | one archived-inclusive `kanban.list_cards()` read, folded by `routes_mobile.count_cards`; `/v1/mobile/summary` uses `kanban_live.board_status_counts` |
| `last_activity` | newest parseable card timestamp on the board (epoch seconds), `null` when none | the same card read |
| `orchestrator` | live turn row (`profile`, `session`, `state`, `turn_s`, tokens) or `null` when idle | `kanban_live.attach_orchestrators` — identical to `hscc kanban monitor` |

`repo` stays on the wire for the console. The widget does not decode it and the
App Group projection never stores it (contract §7).

## Deployment note (2026-11-18)

`/v1/mobile/summary` is present on `main` (`routes_mobile.py`, merged in
`d804d5fa`) with its own route tests and the Swift contract test. The live
2.15.11 server on the operator host predates that merge, so it answers
**404** for the route; the widget's honest fallback is the last-known snapshot,
not zeros. Deploying the current `main` API closes that gap. `/v1/projects` on
the live server already answers, but without the board-truth fields above —
older clients decode only `{name, board}`, newer ones render `—` for missing
counts.

## Parity tests

* `hscc-api/tests/test_routes_project.py::test_projects_list_matches_hscc_project_list_json`
  drives the HTTP route and `flightdeck.commands.project.cmd_list` over the same
  registry/board fixture and compares the rows.
* `hscc-api/tests/test_routes_mobile.py::test_counts_match_hscc_kanban_monitor_on_the_same_board`
* `hscc-api/tests/test_widget_route_parity.py` runs the cluster
  status/monitor and autodown status CLI functions against the live route on
  the same fixture state and asserts equal rows/numbers (card t_8743f98c).
  builds one SQLite board, serves it through the route's flightdeck seam and
  through `hscc_daemon.kanban_live.collect` (the function behind
  `hscc kanban monitor --json`), and asserts identical status histograms.

## Card/kanban parity tests (CARD A2)

`hscc-api/tests/test_widget_route_parity_cards.py` builds one fake fleet (one
registered project board + one orphan board) and runs both sides on it:
`/v1/cards` vs `cards_live.Monitor`, `/v1/kanban/running` vs
`hscc kanban monitor --json`, `/v1/kanban/blocked` vs
`hscc kanban blocked --json`, `/v1/kanban/stale?older_than=0` vs
`hscc kanban stale --json`, and `/v1/mobile/summary` vs
`kanban_live.collect()` + `board_status_counts()`. Each asserts equal
counts/rows and that the orphan board appears in neither answer.
