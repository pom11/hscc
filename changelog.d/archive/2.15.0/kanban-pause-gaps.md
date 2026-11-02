kind: Fixed
task: kanban-pause-gaps

- **`hscc kanban pause` closes two leaks found on its first live day.**
  `enforce` skipped cards it had already recorded, so a card an in-flight
  orchestrator turn unblocked after the pause ran unnoticed; it now re-holds any
  ready/running card on a paused board (recorded once). And `pause` now also
  pauses the orchestrator's ACTIVE cron jobs (board watchers run an agent on
  their own schedule) and `resume` resumes exactly those.
