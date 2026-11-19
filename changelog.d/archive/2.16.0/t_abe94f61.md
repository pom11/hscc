kind: Fixed
task: t_abe94f61

- **The bootstrap board stage can no longer write a kanban root outside the
  home it was told to install into.** `main_board.ensure` now resolves the root
  Hermes' own kanban layer would use — `HERMES_KANBAN_HOME` first, otherwise
  `HERMES_HOME` with Hermes' rules — and refuses unless it is equal to or under
  the target root, naming both roots and the pin that caused the mismatch. That
  includes a "throwaway" `HERMES_HOME` placed inside the native `~/.hermes`,
  which Hermes resolves back to the live tree. `hscc main start` applies the
  same guard before it launches the main orchestrator's REPL, so the other
  path that shares the Hermes-binary lookup cannot start an orchestrator
  pointed at a foreign board. A refused run runs no `hermes kanban boards
  create|switch` at all.
- **The board stage no longer drives the Hermes CLI into a home Hermes has
  never initialised.** Hermes writes `<root>/config.yaml` when it first runs,
  and the board stage ran six stages before the plugin/toolset wiring stage, so
  a first install could reach wiring with a home Hermes had already touched —
  making a first install indistinguishable from a re-install (t_6f4895aa).
  `main_board.ensure` now defers with `hermes-not-started` while the target home
  has no `config.yaml`; the bootstrap warning names the fix: start Hermes once,
  then re-run bootstrap. On a home where Hermes has already run, the board is
  still created and made current as before.
