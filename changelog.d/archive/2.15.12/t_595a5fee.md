---
kind: Changed
task: t_595a5fee

- **`ios-app/Sources/Shared/SharedModels.swift` is now the widget / Live Activity
  payload vocabulary** — the five datasets of `docs/WIDGET_DATA_CONTRACT.md` §3,
  ordered by dataset, each type anchored to the route and the section that sized
  it. The narrowing is structural: a value that must never reach a surface or the
  App Group container is declared nowhere, so it is never decoded. Gone from the
  kanban envelopes: `boards[]` (264 B of board names, duplicated in two bodies),
  `errors[]`, `older_than`, and running's `pid`/`host_local` — those two existed
  so an operator could see which process a kill would stop, and every surface in
  the pivot is read-only. `blocked`'s `why`/`comments` stay undeclared, so card
  prose cannot reach an extension. `cluster/monitor`'s `output` table string
  (54% of the body) is still not declared. Console-only fields the app *does*
  still render (`ClusterWorkload`'s `tp`/`pp`/`container_id`,
  `AutodownStatusResponse`'s `blocked_by`/`watchdog_*`/`force_armed*` family)
  remain, each carrying a `RETIRE` marker naming the retirement slice and the
  check scripts that take them, so the next card can see they are debt with a
  due date rather than payload the widgets need.
- **Two payloads that the five datasets needed and the file did not have.**
  `ProjectsLite`/`ProjectLite` — `{name, board}` only, so `repo` (an absolute
  path on the operator's machine, 58% of the projects array) and `topic` are
  never decoded. `MobileSummaryResponse`/`MobileSummaryBoard`/
  `BoardStatusCounts` — the §6 board-status counts, where **all six statuses are
  required `Int`s**: a missing key is a bug and zero is a value, so a server that
  omits `done` makes the decode throw and the surface keeps its last-known
  snapshot, instead of rendering `done: 0` forever (the bug the client-side
  derivation shipped). Degrade (no `boards` key), per-board `error` and 4 KB
  truncation each decode and assert their own path; `openTotal` follows the
  server's own `total − done − archived`.
- **One node-identity rule instead of two.** `MonitorNode.displayLabel` (sample
  hostname, else the address tail) and `MonitorNode.shortTail` now live on the
  model in `Sources/Shared/`, matching `shortTail` in
  `scripts/widget_datapath_live/template.swift`; `MonitorViews.swift` dropped its
  private copy of the same rule. §7's redaction depends on there being exactly one
  place that decides how a node is named.
- **The row rules the contract binds are now on the envelopes**: `rows(limit:)`
  caps what a surface draws and orders blocked/stale rows oldest-first (§5.3), so
  no widget can invent a different sort while claiming to follow the contract.
- **Two mirror harnesses deleted**: `scripts/shared_model_check.swift` and
  `scripts/widget_kanban_decode_check.swift`. Each re-declared the shared structs
  key-for-key, neither was wired to any `.sh`, and a mirror that quietly drifts
  from the real model is precisely the false green `model_decode_check` exists to
  prevent. Their ground is covered against the real types now.
---
kind: Verified
task: t_595a5fee

- `ios-app/scripts/model_decode_check.sh`: **ALL DECODE CHECKS PASSED — 88/88
  fixtures** (was 62/62), compiled against the real `Models.swift` +
  `SharedModels.swift` — never a mirror. New coverage, per dataset: projects
  (row cap + the `repo`/`topic` structural-redaction proof), the four
  mobile-summary shapes and the **negative** counts-without-`done` fixture
  (asserted to *fail* decode), the kanban trio (`boards`/`errors`/`pid`/
  `host_local` on the wire and not in the type, blocked rows stuck-longest-first,
  queue depth = ready+todo), and cluster monitor + daemon host (node metrics
  parse, `displayLabel` can never render the node's address).
- New fixtures: `v1_daemon_host.json` and `v1_kanban_running.json` are **live
  captures** (read-only `GET`s, value-sanitized — the daemon hostname became
  `gateway-1`); the five `v1_mobile_summary*.json` are **authored from
  `hscc-api/routes_mobile.py`** because that route is not deployed (PR #310 open,
  and a live `GET /v1/mobile/summary` returned 404 when checked); and
  `kanban_stale_queued.json` closes a gap the committed fixtures could not — the
  captured `kanban_stale.json` holds two `running` rows, so queue depth and age
  ordering were exercised by nothing. Provenance is recorded per file in
  `fixtures/README.md`.
- `ios-app/scripts/build_check.sh`: HSCC **86** / HSCCWidgets **9** /
  HSCCLiveActivity **5** / HSCCLiveActivitySession **5** /
  HSCCLiveActivityMonitor **5** files, **0 error(s), 0 warning(s)** — full
  compile+link of every target against the iOS 26 SDK. Never built or run on a
  device.
- `check_sources.sh`: 94 Swift files, all listed in `project.yml` (no file added
  or removed under `Sources/`; the two deletions were harnesses in `scripts/`).
  `check_theme.sh`: CLEAN. `actool_icon_check.sh`: PASSED. `ws_decode_check.sh`:
  both live frame captures decode through the shipped parsers.
