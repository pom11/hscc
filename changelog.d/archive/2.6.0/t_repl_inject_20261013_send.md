kind: Changed
task: t_repl_inject_20261013

- **`hscc project message send` delivers for real.** `--apply` types the
  message into the project's live orchestrator REPL pane through the SAME
  `repl_inject` module the iOS/WS write path uses (bracketed paste, `/queue`
  when busy) — both doors, one pane. Without `--apply` the command stays a
  dry-run render, matching `message dispatch`. The old "Telegram removed →
  no delivery" notice is gone: the delivery target is the operator's REPL,
  and refusals are honest (named pane/profile, never a silent success).
