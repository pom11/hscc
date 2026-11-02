kind: Added
task: cards-monitor

- **`hscc cards monitor [--live]` — watch every running card's worker at
  once.** One bordered panel per running kanban card follows that worker's
  transcript (the dispatcher's per-task log) in a single full-screen view; the
  grid re-flows as cards start and finish (a finished card lingers ~10 s with
  its outcome, then drops out) and cards waiting for a slot are listed along
  the bottom. Panel headers show board · card · worker, then run time,
  heartbeat age and worker-session tokens as space allows. Keys: tab/arrows
  focus, enter/z zoom, esc unzoom, `+`/`-` bigger/smaller panels (`0` auto),
  `t` full transcript / tool calls only, `p`
  pause, `q` quit. `--board`, `--interval`, `--tools`; without `--live` it
  prints one snapshot. Read-only — reuses the `hscc kanban monitor` collector.
