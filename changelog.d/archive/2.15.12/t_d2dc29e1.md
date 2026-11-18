kind: Added
task: t_d2dc29e1

- **`ios-app/Sources/HSCCWidgets/CardsRunningWidget.swift` — the running-cards
  widget: what is moving, who is moving it, and for how long.** The phone answer
  to `hscc cards monitor --live`, whose panel header is
  `board · card · worker` + run time (`hscc_daemon/cards_live.py`); the widget
  keeps those three fields and the one age the payload can actually support.
  Rows come from `GET /v1/kanban/running`, not `/v1/cards` — §8.3's rule and
  §5.4's measurement (8 rows cost 81,855 B from `/v1/cards`, 98.6 % discarded,
  against ~2 KB for the same rows already projected server-side). Paired with
  `GET /v1/kanban/blocked` as §5.4's "failure rows", so an empty running list
  can be told apart from a jammed fleet: **two requests per refresh, exactly
  §2's ceiling**, run in parallel. The failure read failing nulls one number
  (`blocked —`), it never fails the surface the rows came from.
  Rendered across `systemSmall` (2 rows), `systemMedium` (4, with board),
  `systemLarge` (8 = the stored cap), and the three accessory families
  (§5.4's per-family row/field budget). Age is minutes since `started_at`
  (ISO-8601 UTC from `kill_switch._started_iso`, parsed with and without
  fractional seconds), computed once per refresh; the route carries no heartbeat
  field, so a row never renders the heartbeat age `--live` also prints, and an
  unparseable `started_at` draws `—` rather than `0m`. §6 rule 1 end to end:
  `runningCount`/`blockedCount` are optional, `nil` is "this refresh was not
  told" and draws `—`, `0` is "the server counted none". **A blind read cannot
  render an idle board**: a 2xx with no `tasks` array is
  `.decoding`, not an empty list, and with no projection `runningText` returns
  nil so no zero exists to draw — every family then shows
  `exclamationmark.triangle.fill` in `Theme.Semantic.bad` with
  `WidgetDataFailure.shortLabel`/`.operatorLine`, and the stale state shows the
  rows *plus* their age *plus* why this refresh did not answer, in `warn`. The
  setup invite is checked before any failure or stale state in every family
  body (audit-3, as `widget_view_dispatch_check.sh` enforces for the cluster
  widget). Storage is the projection only — five scalars per row, no `body`, no
  path, no address, no token — keyed fleet-wide, through `SharedStore.suite`.
  Registered in `HSCCWidgetsBundle` and listed in `project.yml`; the shared
  per-surface cadence is not honoured because `WidgetSurfaceKind` has no case
  for this surface (a `SharedStore.swift` + config-screen change, another card's
  file set), so it uses a conservative 15 minutes. Nothing was run on a device.
