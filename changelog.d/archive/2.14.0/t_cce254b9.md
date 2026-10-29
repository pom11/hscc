kind: Added
task: t_cce254b9

- **`hscc kanban monitor [--live]`.** The kanban twin of `hscc cluster monitor
  --live`: running cards on every board (worker, run time, heartbeat age —
  >4h runs and >10 min silent heartbeats are flagged), ready cards waiting
  for a slot, per-board counts by status plus cards done in the last 24h, and
  the last hour's outcomes (completed / blocked / gave_up / crashed / …). The
  header shows the global `kanban.max_in_progress` slots in use and says when
  a full cap is stranding other boards' ready cards. `--board <slug>`,
  `--interval <s>`, `--json` snapshot. Read-only; reuses the autodown board
  enumeration that `hscc kanban stale` uses.
---
kind: Fixed

- **`hscc kanban stale --json` printed the human table.** The `hscc kanban`
  dispatcher (`kanban_blocked.cmd_kanban`) strips `--json` before delegating
  `stale`/`monitor` to `kanban_cli` and never handed it back; it is now
  re-appended on delegation.
---
kind: Verified

- `hscc_daemon/tests/test_kanban_live.py`: 9 cases (collector, board filter +
  unreadable board, render escaping + full-cap warning, short-pane section
  dropping, age format, arg parsing, `--json` snapshot, `--help` executes
  nothing, `--json` through the `hscc kanban` dispatcher); kanban CLI suites
  green.
