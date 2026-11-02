kind: Added
task: kanban-pause

- **`hscc kanban pause | resume | paused | enforce` — hold whole boards by
  project priority.** Hermes has no per-board pause, so it is built from its
  own per-card verbs: `pause <board>...` reclaims every running card and blocks
  it and every ready card with a PAUSED reason (todo cards stay parent-gated),
  holds `kanban.auto_decompose` off while any board is paused (triage cards
  cannot be blocked), and INTERRUPTS + steers the board's `<board>-orch`
  through `hermes-inject` (cancel the running turn; don't unblock / create /
  dispatch until resumed). `resume <board>...` unblocks exactly the cards the
  pause held, restores auto-decompose when the last board resumes, and tells
  the orchestrator it is live again. `enforce` holds cards that became
  ready/running on a paused board since. State in
  `~/.hscc/state/kanban_paused.json`.
