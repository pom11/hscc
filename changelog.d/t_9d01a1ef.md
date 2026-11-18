kind: Fixed
task: t_9d01a1ef

- **Widget card/kanban routes now answer from the CLI's own functions.**
  `GET /v1/cards`, `/v1/kanban/running`, `/v1/kanban/blocked`,
  `/v1/kanban/stale`, and `/v1/mobile/summary` call the same
  `kanban_live.collect()` / `kanban_blocked.list_blocked_tasks()` /
  `autodown.list_stale_tasks()` paths as `hscc cards monitor` and
  `hscc kanban monitor|blocked|stale`. `/v1/cards` returns exactly the cards
  the monitor draws (running, then ready) instead of old blocked/archived rows,
  and only accepts the CLI's `?board=` filter. Every surface is scoped to
  boards owned by registered projects (`hscc project list`), so orphan
  sqlite boards no longer appear as project boards. `done` is the CLI's
  done-in-the-last-24 h count, not an all-time archive total. Parity tests run
  both the CLI path and the HTTP route over one fixture fleet and assert equal
  rows/numbers; route documentation is in `hscc-api/docs/WIDGET_ROUTES.md`.
