---
kind: Fixed
task: t_438aa6ce

- **The App Group no longer holds a node's LAN address.**
  `MonitorSnapshotStore.Snapshot.nodes` was `[MonitorNode]` — the decoded
  `/v1/cluster/monitor` host entry, whose `host` field *is* the node's LAN address
  as the API reports it — and the monitor widget's stale-while-error path wrote it
  into `group.com.hscc.ios` on every successful fetch. §7 names
  `cluster.monitor hosts[].host` (and `sample.sparkrun_job_names`) as fields that
  must never enter shared storage or a view; the container is readable by every
  extension and outlives the app process, so that write persisted a LAN address
  indefinitely and made it reachable from any extension.
- **The storage shape is now the projection.** `MonitorNodeEntry: Codable,
  Equatable` in `Sources/Shared/SharedStore.swift` (with the store, per the
  `SharedStore`/`SharedModels` split — the wire file is never persisted) carries
  exactly the §3 row-5 six fields: `label`, `cpuUsagePct`, `memUsedPct`,
  `gpuUtilPct`, `usedSlots`, `freeSlots`. `label` is `MonitorNode.displayLabel` —
  the single node-identity rule from t_595a5fee (sample hostname, else the address
  tail) — so a stored label can name a node without naming where it lives. No
  `host`, no `sparkrun_job_names`, no raw sample.
- **`MonitorSnapshotStore.project(_:)` is the only path from wire type to
  storage.** `save(nodes:)` takes `[MonitorNodeEntry]`, so a caller cannot persist
  the response by accident: it has to call `project` and watch the address drop.
  `MonitorWidget`'s fetch now writes `project(nodes)`; the widget's rendering is
  unchanged (this is a storage-shape change, not a UI change).
- **The unreachable pane reads a count, not the wire type.** Its only use for the
  stale nodes was `!nodes.isEmpty` — "did the last good fetch have nodes?" — so
  `MonitorEntry` now carries `lastKnownNodeCount: Int` instead of receiving
  `[MonitorNode]` back out of the container, and nothing drawn by a gate can hold
  an address. `daemonHost` stays in the snapshot (t_595a5fee's decision): its
  `hostname` is the daemon host's own name, which §5.5 names as renderable and §7
  does not list — and the decode check now proves that key by key rather than
  trusting it.
---
kind: Verified
task: t_438aa6ce

- `ios-app/scripts/model_decode_check.sh`: **ALL DECODE CHECKS PASSED — 97/97
  fixtures** (was 88/88). `Sources/Shared/SharedStore.swift` is in the compiled
  real-source list, so the new §7 block runs against the production store types —
  never a mirror. Nine new assertions: the fixture's own `hosts[].host` values are
  dotted quads (the control, so the scan cannot pass vacuously), `project()`
  yields one entry per node labelled by `displayLabel` and never by `host`, the
  six projected fields survive, `MonitorNodeEntry` round-trips through the real
  `Snapshot` encode/decode, **no key and no string value in the stored JSON holds
  a dotted-quad**, a stored node object declares no key outside the six projection
  fields (so `host` cannot be added back silently), and the stored daemon-host
  object holds exactly §5.5's eleven machine fields with a `hostname` that is a
  name, not an address. Test-the-test: pointing `project()`'s label at `node.host`
  turned the scan red and named the paths
  (`$.nodes[0].label=192.0.2.1 (gx10-cad0)`), then was reverted.
- `ios-app/scripts/build_check.sh`: HSCC **83** / HSCCWidgets **9** /
  HSCCLiveActivity **5** / HSCCLiveActivitySession **5** /
  HSCCLiveActivityMonitor **5** files, **0 error(s), 0 warning(s)** —
  `full compile clean, 0 warnings (compile only — never built or run on a device)`.
  No file added or removed under `Sources/`, so `project.yml` is untouched.
- `check_sources.sh`: `sources in sync: 91 Swift files, all listed in
  project.yml`. `check_theme.sh`: `CLEAN — no raw colour outside Theme.swift.`
  `actool_icon_check.sh`: `ACTOOL ICON CHECK PASSED — AppIcon compiles and is
  wired (still not verified on a device).`
- Neighbours that could notice a monitor/storage change, each run and passing:
  `shared_store_check.sh` (`SHARED STORE CHECK PASSED — a broken App Group cannot
  fail silently`), `snapshot_store_check.sh` (`✅ REAL SnapshotStore
  encode/decode round-trip OK`), `widget_settings_check.sh`,
  `widget_view_dispatch_check.sh`, `live_activity_rehydration_check.sh`, and
  `monitor_activity_check.sh` against the live API (`PASS: real
  /v1/cluster/monitor + /v1/daemon/host decode + derive to a populated
  ContentState` — with its own honest `WARN: no nodes derived`, the fleet being
  down at the time of the run).
- `scripts/snapshot_store_check.sh` and `scripts/monitor_activity_check.sh` were
  named as the harnesses whose subject moves. Checked rather than assumed:
  `grep -rn "MonitorSnapshotStore\|MonitorNode" scripts/` finds no reference to the
  monitor snapshot store in either — the first exercises the *topology*
  `SnapshotStore`'s encode/decode (extracted verbatim from the same file, still
  unchanged), the second the monitor **Live Activity**'s push path
  (`MonitorActivityAttributes.ContentState` + `NodeMetric`), which never reads the
  store. Both still pass; the §7 storage invariant therefore has its home in
  `model_decode_check`, where the bytes written to the container are the subject.
