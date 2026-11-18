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
| `GET /v1/mobile/summary` | Board-status widget, board-progress Live Activity | `routes_mobile.handle_mobile_summary` | `flightdeck.core.kanban.list_boards` / `list_cards(include_archived=True)` — the same board library the project/kanban commands read (not `/v1/cards`) |
| `GET /v1/kanban/running` | Cluster, running-cards, board-progress | `routes_kanban.handle_kanban_running` | `hscc_daemon.kill_switch.list_running_tasks` |
| `GET /v1/kanban/blocked` | Cluster, running-cards, kanban monitor | `routes_kanban.handle_kanban_blocked` | `hscc_daemon.kanban_blocked.list_blocked_tasks` (`hscc kanban blocked` family) |
| `GET /v1/kanban/stale` | Cluster, kanban monitor, running-cards | `routes_kanban.handle_kanban_stale` | `hscc_daemon.autodown.list_stale_tasks` (`hscc kanban stale`) |
| `GET /v1/cluster/monitor` | Cluster + monitor widgets, monitor Live Activity | `routes_cluster.handle_cluster_monitor` | `hscc-cluster` engine `cmd_monitor` (`hscc cluster monitor`) |
| `GET /v1/cluster/status` | Cluster widget | `routes_cluster.handle_cluster_status` | `hscc-cluster` engine `cmd_cluster_status` (`hscc cluster status`) |
| `GET /v1/daemon/host` | Monitor widget, monitor Live Activity | `routes_ops.handle_daemon_host` | `hscc_daemon` host metrics (`hscc status` daemon-host block) |
| `GET /v1/autodown/status` | Cluster widget, cluster-health Live Activity | `routes_autodown.handle_autodown_status` | `hscc_daemon.autodown` + `lifecycle` status classifiers (`hscc autodown status`) |

## Project rows (CARD A3)

`GET /v1/projects` keeps the existing identity fields (`name`, `repo`, `board`,
`topic`) and adds the board truth the Projects widget draws:

| Field | Meaning | Owner |
|---|---|---|
| `health` | `ok` / `partial` / `absent` | `project_lifecycle.project_health` — identical to `hscc project list` |
| `counts` | status histogram + `total`, including `done` / `archived` | one archived-inclusive `kanban.list_cards()` read, folded by `routes_mobile.count_cards` (the `/v1/mobile/summary` projection) |
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
  builds one SQLite board, serves it through the route's flightdeck seam and
  through `hscc_daemon.kanban_live.collect` (the function behind
  `hscc kanban monitor --json`), and asserts identical status histograms.
