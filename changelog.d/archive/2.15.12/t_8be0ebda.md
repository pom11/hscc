kind: Changed
task: t_8be0ebda

- **Projects widget now reads the CLI's project truth, not a name/board stub.**
  `GET /v1/projects` adds each registered project's `health`, board status
  counts (`running`/`blocked`/`ready`, plus `done`/`archived`/`total`), newest
  board activity and live orchestrator turn. The rows come from the same
  `registry` + `project_health` calls as `hscc project list` and the same
  archived-inclusive kanban read + `attach_orchestrators` pass as
  `hscc kanban monitor`; the widget renders the counts and omits anything the
  server did not send instead of printing zero.
- **Widget route contract covers the extension sources.** The Swift-route gate
  now parses `WidgetDataClient.swift`, every widget and Live Activity source,
  and requires each `/v1/...` read to be registered on the server and named in
  `hscc-api/docs/WIDGET_ROUTES.md`. A widget route with no server route fails
  CI. `/v1/mobile/summary` is already on `main` (`d804d5fa`); the live 2.15.11
  host predates it and answers 404, which the widget renders as stale/last-known
  rather than zero.

---
kind: Verified
task: t_8be0ebda

- `hscc-api/tests/test_routes_project.py`: the HTTP route and
  `hscc project list --json` are driven over one registry/board fixture and
  compare equal rows. `hscc-api/tests/test_routes_mobile.py`: one SQLite board
  is served through the route and through `kanban_live.collect` (the
  `hscc kanban monitor --json` path) and the status histograms compare equal.
  `hscc-api/tests/test_contract_swift_routes.py` derives all nine extension
  reads from Swift and asserts registration plus the route-map doc.
