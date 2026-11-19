# Decode fixtures

Live HSCC API responses — initial capture 2026-08-27, extended since (the
Phase 3/4 additions below, incl. the t_ca133e7d captures of 2026-09-27) —
**sanitized** for a public repo.

These feed `scripts/model_decode_check.sh`, which compiles the REAL model
sources (`Sources/HSCC/Models.swift`, `Sources/Shared/SharedModels.swift`,
`Sources/HSCC/APIError.swift`) into a macOS CLI and decodes every fixture. Any
decode failure exits nonzero and names the fixture + field.

## Provenance & sanitisation

Each file is a verbatim capture of a real `/v1/...` read response, with values
rewritten to strip internal detail. Sanitisation is **value-only** — it never
changes JSON shape (same keys, same value types, same nesting), because shape
is the entire point of the check.

Mapping applied (value → replacement):
- private node IPs  →  `192.0.2.x` (one distinct TEST-NET address per real node)
- private tailnet IP → `100.64.0.1`
- operator home dir  →  `/home/devuser`
- ssh username       →  `node`
- node hostnames     →  `node-N` / `gateway-1`
- token-file path reference → `~/.hscc/sample-token-path`

## Note on grepping the fixtures

A naive `grep -r token fixtures` in the JSON data matches only the legitimate
schema key names `prompt_tokens` and `generation_tokens` in
`fleet_throughput.json` — those are part of the model contract, not credential
values. No actual authorization value, API key, or private key material is
present anywhere in the JSON data (verified: the only sensitive-token hits in
the whole fixtures tree are this README naming the patterns). The real
authorization header value never appears in any capture.

`cards.json` and `card_detail_t_049d6986.json` are shape-faithful rewrites of
the real captures: the Card model reads only `id/title/status/assignee/board`
(unknown fields are ignored by Decodable), so the bulky card `body` prose —
which contained a tailnet IP and a token-file path — was dropped and the real
key structure preserved. The decode contract is therefore exercised identically.
Every JSON fixture in this directory autovalidates as JSON (last re-verified
2026-11-17: every `*.json` here parsed cleanly under `python3 -m json.tool`).
The count of files is deliberately **not** stated here — see **Coverage** for
why the number lives in the script's own output instead of in this file. If
ever in doubt, re-encode a fresh capture and diff the key structure.

## Coverage

The committed fixtures cover every decodable response model in Models.swift +
SharedModels.swift that has a decode check wired into `main.swift`, plus every
response type surfaced by the live capture (`live_decode_check`).

**How many checks are green is whatever the run prints, not what this file
says.** `./scripts/model_decode_check.sh` ends with `ALL DECODE CHECKS PASSED —
N/N checks (over F committed fixtures) decoded against the real models`. Both
numbers are printed because they are not the same number, and the line used to
call the check count "fixtures", which read as a fixture total that never
existed: `N` is checks, `F` is distinct files opened, and neither is typed
anywhere. It exits nonzero on the first miss, so the script reports its own
counts and is never stale, while a figure
typed in here is a snapshot that starts ageing the moment a fixture is added or
deleted. That is not hypothetical — this paragraph carried `88/88 (62 JSON
bodies)` while the run printed `98/98`, and the set shrank again when the
operator console was retired. So the rule for the next card is: **a card that
adds, rewrites or deletes a fixture changes no number in this file** — it runs
the script and reads its last line, which is the authority by construction.

What does not rot, and so is kept: the number of **checks** is higher than the
number of **files**, because several fixtures are decoded against more than one
model, the widget blocks assert derived values on top of the decode, and two
fixtures (`v1_mobile_summary_missing_done.json`,
`v1_monitor_snapshot_legacy.json`) are *negative* — they pass by **failing** to
decode. The live axis (`scripts/live_decode_check.sh`) is a separate harness
that needs `scripts/capture_live.sh` run against a live daemon, and its captures
are not committed, so every live-route figure below is a point-in-time
measurement of one run, not a green a checkout can reproduce.

`cron_list.json` is the Phase 3 scheduled-job roster (`GET /v1/cron/list`,
added 2026-09-27). It is a faithful 11-job capture of the REAL live response
(2 enabled/active, 9 paused), sanitized only at the value level — no secrets,
no IPs (the roster never contains host addresses). See `routes_cron.py`
`handle_cron_list` / `hscc_daemon.autodown.list_all_cron_jobs` for the shape.
The `main.swift` block additionally asserts the active/paused classification
via `CronJob.isActive` and that every nullable field tolerates null.

The mutation/detail fixtures added 2026-08-30 (`dispatch_card`, `merge_card`,
`template_apply`, `stop_cluster`, `recover_card`, `session_retire`,
`memory_list`, `memory_delete`, `autodown_enable/disable/wake/cancel`,
`cluster_up`, `cluster_down`, `orchestrator_chat`,
`orchestrator_chat_status`, `review_detail`) were not captured from the live
host — their shapes were derived from the actual hscc-api handlers so each
fixture mirrors what the real endpoint returns on a 2xx success. **Seven of
these names are no longer files in this directory** — `template_apply`,
`memory_list`, `memory_delete` and the four `autodown_*` mutations went with the
operator console screens that read them (t_0f3f70e6); they are listed because
the provenance statement (handler-derived, not captured) is what tells the next
reader whether a shape is a capture or an inference, and the surviving fixtures
here still mix both kinds. See the handler at the cited source for each:
- `dispatch_card.json` — routes_actions.py `handle_create_card`
- `merge_card.json` — routes_actions.py `handle_merge_card` (clean close branch)
- `template_apply.json` — routes_actions.py `handle_template_apply` + cluster_template.py `apply_template`
- `stop_cluster.json` — routes_actions.py `handle_cluster_stop`
- `recover_card.json` — routes_kanban.py `handle_kanban_recover`
- `session_retire.json` — routes_sessions.py `handle_sessions_retire`
- `memory_list.json` — routes_memory.py `handle_memory_list`
- `memory_delete.json` — routes_memory.py `handle_memory_delete`
- `autodown_enable/disable/wake/cancel.json` — routes_autodown.py handlers
- `cluster_up.json` / `cluster_down.json` — routes_ops.py + hscc-cluster/hscc.py `cmd_cluster_up`/`cmd_cluster_down`
- `orchestrator_chat.json` — routes_orchestrator.py `handle_orchestrator_chat`
- `orchestrator_chat_status.json` — routes_orchestrator.py `handle_orchestrator_chat_job`

Only `ReadResponse` remains without a fixture — it is a self-contained generic
bucket (`{ speak, payload? }`) by design and needs no fixture.

`v1_sessions.json` is the sessions-manager list (`GET /v1/sessions?profile=...`),
captured against routes_sessions.py: one healthy session and one bloated one
(positive compaction-failure evidence), value-sanitized, shape-faithful.

## Phase 4 additions (t_ca133e7d, 2026-09-27)

Three previously-uncovered wire families were captured LIVE from the running
API and committed as shape-faithful sanitizations, closing the last decode gaps:

- `commands.json` — `GET /v1/commands` → `CommandsResponse` (`SlashCommand`).
  Verbatim live capture of the server-driven slash-command catalog; a subset of
  the commands retained, `speak` kept as served.
- `profiles_list.json` — `GET /v1/profiles/list` → `ProfileListResponse`
  (`ProfileSummary`). Real full-roster shape (10 keys incl. the tolerated-unknown
  `description_auto` and always-null `distribution_name`/`distribution_version`),
  trimmed to 4 representative profiles; values generalized, names kept clean
  (they are public role slugs), `count`/`speak` updated to the trimmed size.
- `logs_daemon.json` — `GET /v1/logs?source=daemon&limit=N` → `LogsResponse`
  (bare `[LogEntry]`, each row `{timestamp, level, source, line}`). Live daemon
  tail trimmed to 6 representative lines covering both INFO and ERROR levels;
  no IPs/paths/tokens present in the source, none introduced.

`capture_live.sh`, `live_decode_check/main.swift` and
`compare_fixtures_live.py` were extended so every live capture now includes
these three routes and both decode-axis harnesses (committed fixtures +
live-population) guard them. What that card measured at the time — a
point-in-time tally of its own run, superseded by every fixture change since,
and deliberately not restated as a current green: the committed set decoded
`62/62`, which it reconciled as 56 fixtures before Phase 4 + 3 new `c.check`
rows + 3 new contract assertions, with `36/36` live routes decoding populated.
**Today's number is the one `model_decode_check.sh` prints**, not this one.

## Running the check

From `ios-app/`:

    ./scripts/model_decode_check.sh       # exit 0 == every fixture decodes

The script compiles the real model files — `Models.swift`, `SharedModels.swift`,
`APIError.swift`, and since t_438aa6ce also
`SharedStore.swift`, because §7 binds the **stored** shape and the monitor
snapshot's bytes are production types, not mirrors — with the harness into a
temporary macOS CLI and runs it, so it never falls out of sync with the models the
app actually ships. (`SessionEvent.swift` was in that list until t_9d5a30c9
retired the chat stream layer with the chat family; `v1_session_events.json` left
in the same commit — struct, fixture and `main.swift` block always move together.)

## Widget / Live Activity payload additions (t_595a5fee, 2026-11-17)

`SharedModels.swift` narrowed to the five datasets of
`docs/WIDGET_DATA_CONTRACT.md` §3, and `main.swift` now decodes those types too.
Eight fixtures, provenance stated per file because the repo's rule is that a
fixture is a capture unless labelled otherwise:

- **Live captures** (read-only `GET`s against the running daemon, base derived at
  runtime, value-sanitized only — shape untouched, same keys/types/nesting):
  - `v1_daemon_host.json` — `GET /v1/daemon/host` → `MonitorDaemonHost`. 445 B
    served. `hostname` and the `speak` line sanitized to `gateway-1` per the
    mapping above; the numbers (cpu/mem/disk/uptime) are as served. No route
    covered this payload before, and the widget + monitor Live Activity read it.
  - `v1_kanban_running.json` — `GET /v1/kanban/running` → `KanbanRunningLite`.
    Kept **with** `boards`, `errors`, `pid` and `host_local`, because the point of
    the check is that the wire carries them and the type does not (§5.3 + the
    read-only pivot). Nothing here is a credential or an address.
- **B2 cards-surface pair (t_635bae56, 2026-11-18)**:
  - `v1_kanban_running_cards.json` — a **scrubbed live capture** of
    `GET /v1/kanban/running` against the running API (read-only GET, token from
    `~/.hscc/api-token`, base derived from `hscc api status`; 2026-11-18).
    Shape, `count` and timestamps are the capture's; board names, card ids and
    titles are generalized, and the live server's `pid`/`host_local` keys are
    kept on the wire precisely so the Lite type's refusal to declare them stays
    proven. The old server sends `assignee` + `started_at` only, which is the
    fallback branch.
  - `v1_kanban_running_contract.json` — **authored from the handler**, not a
    live capture: the fixed route's `kanban_live.CARD_VIEW_FIELDS` shape
    (`worker`, `run_s`, `hb_s`, `age_s`, `priority`, `started_epoch`), which no
    deployed server answers yet. It proves the mapping prefers the CLI's
    `worker` column and `run_s` while still tolerating a null clock.
    `scripts/cards_running_widget_check.sh` drives the real projection and the
    sliced Live Activity rules from both, one section per family.
- **Authored from the handler** (the route is not deployed: `GET
  /v1/mobile/summary` returned 404 live on 2026-11-17, PR #310 being the server
  half). Shapes are read off `hscc-api/routes_mobile.py` — `_fold_counts`,
  `_assemble`, `_fit_budget` and `handle_mobile_summary` — not guessed:
  - `v1_mobile_summary.json` — the healthy payload: three tiles, all six statuses
    present, `total` equal to the sum of the statuses the way the server folds it.
  - `v1_mobile_summary_degraded.json` — §6 rule 7: `generated_at` + `speak` and
    **no `boards` key**, so a client can never confuse "no counts" with "quiet".
  - `v1_mobile_summary_board_error.json` — the `?boards=` path: one good tile and
    one `{"board": slug, "error": "counts unavailable"}` (no counts, no total).
  - `v1_mobile_summary_truncated.json` — the 4 KB budget trimmed the tail:
    `truncated`, `omitted_boards`, `unreadable_boards` all set and asserted.
  - `v1_mobile_summary_missing_done.json` — a **negative** fixture: a counts map
    with no `done`. `main.swift` asserts it *fails* to decode (§6 rule 1: a missing
    key is a bug, zero is a value — defaulting `done` to 0 is the bug this route
    exists to kill).
- **Shape-faithful rewrite** of a real capture, in the `cards.json` tradition:
  - `kanban_stale_queued.json` — keys exactly as `autodown.list_stale_tasks`
    serves them (§4.3). Needed because the committed `kanban_stale.json` is a
    two-row capture whose rows are both `running` at `age_days: 0`, so the tile's
    queue-depth derivation (`ready` + `todo`) and §5.3's oldest-first row order
    were exercised by nothing at all.

## Stored-shape additions (t_438aa6ce, 2026-11-17)

§7 binds what the App Group container may hold, so the subject here is a *stored*
blob rather than a route response.

- `v1_monitor_snapshot_legacy.json` — a **negative** fixture: the exact shape
  `MonitorSnapshotStore` wrote before this card, when `Snapshot.nodes` was
  `[MonitorNode]` — a node object with `host` (RFC 5737 test-net `192.0.2.1`,
  sanitized per the mapping above), `error`, and the full `sample` including
  `sparkrun_job_names`. `main.swift` asserts it **fails** to decode into the real
  `MonitorSnapshotStore.Snapshot`: `MonitorNodeEntry` declares no such fields, so
  an upgraded build refuses the legacy blob outright (`load()` returns nil) instead
  of reading an address back out of the container. That rejection *is* the migration
  behaviour, so it is asserted rather than argued.

## Cluster-history additions (t_b6512c2e, 2026-11-19)

The fixtures for `GET /v1/cluster/history`, and none of them are captures
nor hand-typed shapes: `scripts/make_cluster_history_fixture.py` fills a store
through the recorder's own write path (`MetricsRecorder.tick`, which rolls tiers
1 and 2 with its own `rollup`), pins the route's clock seam, and calls
`routes_cluster.handle_cluster_history`. The JSON committed here is what the
handler returned. Sanitisation is one key — `store.path` is rewritten to
`/Users/example/.hscc/metrics/history.db`, because the generator's temp dir would
otherwise be machine-specific — and `ClusterHistoryStore` declares no `path`, so
the model can never carry a server filesystem location into the App Group. The
hosts are TEST-NET-1 `192.0.2.10`/`.11` by construction.
`scripts/cluster_history_metric_vocabulary.py` re-checks every key in these files
against `hscc_daemon/metrics_history.py`'s own column tuples.

- `v1_cluster_history_6h.json` — tier 1 (1-minute averages, the three `*_max`
  peak columns present). It carries the two paths the chart depends on:
  `192.0.2.11` reported no GPU in any tick, so its `gpu_temp_c`/`gpu_power_w` are
  `null` and stay `null` rather than becoming `0`, and the engine rows hold `kv`
  as vLLM's raw 0-1 fraction. One measured surprise worth keeping: the store's
  columns are sqlite `REAL`, so `run`/`wait` are on the wire as `2.0`/`1.0`. That
  is the recorder's schema and not a typo, and it means the model's `Int?` holds
  only because `JSONDecoder` converts an integral number losslessly — the gate
  now pins that conversion instead of trusting it.
- `v1_cluster_history_7d.json` — tier 2 (5-minute averages) from the same store,
  so the two tiers differ in resolution and in which columns carry data.
- `v1_cluster_history_6h_engine_only.json` — `?metrics=gen_tps,run`: the host
  series is *excluded* (`hosts_included: false`) rather than silently empty, and
  each projected row keeps only its identity plus the requested columns.
- `v1_cluster_history_15m.json` — tier 0 as the deployed topology answers it: the
  2 s ring lives in the daemon process, so `store.tier0_resident` is false, both
  series are empty, and `note`/`speak` say why. A chart must draw that as a gap
  with a reason, never as a flat line at zero. Branch on `source`
  (`recorder_ring_absent` here) for the decision and read `note` for the sentence.
- `v1_cluster_history_tier_axis.json` — `?tier=1&since=&until=` (t_b611b56e), the
  second request axis: an explicit span instead of a preset anchored to `now`.
  `axis` is `tier`, `window` is the derived label `"600s"` and deliberately not a
  preset name, and `since`/`until` are the pair the server served *after* clamping
  to the tier's retention — so a surface draws the span it was given rather than
  the one it asked for.

`store.tier0_resident` is a **tier-0 measurement only**, so read the reason for an
empty history answer from `note` (or `speak`), never from that flag. The route asks
the recorder seam — `tier0_resident = points is not None` — inside its tier-0 branch
and nowhere else (`hscc-api/routes_cluster.py`, since PR #417 / `d6a2d64e`), because
the 2 s ring is memory in the daemon process and a durable-window read cannot see it.
The key is on the wire at every tier, but at tiers 1-2 its value is `null`: a tier the
route never read cannot report residency. That is why the tier-1 and tier-2 fixtures
above say `null` and the tier-0 one says `false` — measured from one generator in one
process, not chosen per file. Before PR #417 the flag defaulted to `True`, so all
three durable fixtures claimed a residency nothing had checked; a fixture that says
`true` at tier 1 or 2 is a regression, not a value to restore.
