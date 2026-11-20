kind: Fixed

- **Cluster widget refreshes share a 20 s server-side snapshot.** `/v1/cluster/monitor`
  and `/v1/cluster/status` cache their live sparkrun/SSH answer for
  `CLUSTER_SNAPSHOT_TTL_S` and coalesce concurrent misses into one in-flight read
  (single-flight). Every envelope carries `taken_at`, the cache-fill time, so a
  widget can show the age of the sample it is drawing instead of multiplying the
  subprocess/SSH work on every reload (W17).
- **A degraded `/v1/cluster/monitor` is a 503, not a 200 without `json`.** The
  body states `success: false`, `degraded: true`, the real `error` and a `speak`
  line; `MonitorResponse` decodes `success`/`error` and exposes `isDegraded`, and
  the Monitor Live Activity only calls a frame with host rows `serving`. A
  reachable daemon host alone is `degraded`, never a serving fleet (W5).
- **`/v1/projects` uses the mobile summary's rolling 24 h `done`.** `counts.done`
  and the explicit `counts.done_24h` now match `/v1/mobile/summary` for the same
  board, and `counts.total` uses that route's open + archived + done-24h
  definition. The all-time facts remain as `done_all_time` and `total_all_time`,
  named so they cannot be mistaken for the rolling window (W10).
- **Cluster history defaults to 1 h everywhere.** The route, `hscc cluster
  history`, `WidgetDataClient.clusterHistory`, `ClusterGraphWindow.recommended`
  and the macOS menu-bar graphs all choose `1h`; `15m` is still requestable where
  the recorder ring is resident, but it is no longer a default that renders
  `recorder_ring_absent` on the deployed API (W7).

---
kind: Verified

- `hscc-api/tests/test_routes_cluster.py`: snapshot TTL and single-flight, the
  `taken_at` envelope, and the 503 `success:false` monitor degradation.
- `hscc-api/tests/test_routes_project.py`: `done`/`done_24h`/`total` match
  `kanban_live.board_status_counts`, with `done_all_time`/`total_all_time` kept.
- `hscc-api/tests/test_contract_swift_routes.py`: the Swift history defaults are
  the durable `.oneHour`, not `.fifteenMinutes`.
- `ios-app/scripts/model_decode_check.sh` (356 checks) decodes the new degraded
  monitor fixture and executes `MonitorActivityState`, including "daemon host
  alone is degraded"; `ios-app/scripts/cluster_widget_check.sh` (134 checks).
- `pytest hscc-api/tests/test_routes_cluster.py test_routes_project.py
  test_contract_swift_routes.py test_cluster_history_parity.py
  test_cluster_history_cli_parity.py test_widget_route_parity.py
  test_widget_route_parity_cards.py` (210 passed).
- `hscc cluster history --json` (read-only, local recorder) answers
  `window=1h`, `tier=1`, `source=sqlite_store` when no flag names a window.
- `python3 scripts/address_guard.py --tracked` (clean).
