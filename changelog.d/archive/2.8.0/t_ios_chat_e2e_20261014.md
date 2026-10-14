kind: Fixed
task: t_ios_chat_e2e_20261014

- **iOS chat now receives the terminal REPL's replies.** The store-tail poller
  no longer waits for the `hermes serve` mount: it starts first, and a failed
  mount leaves the driver running tail-only instead of dead. With serve down
  and the project's REPL in tmux, a tail-only driver is mounted.
- **Orphaned `hermes serve` no longer 403s every mount.** On start, a serve
  already holding the port is adopted if it accepts the API's token; otherwise,
  if it is a `hermes serve` process, it is replaced. Foreign listeners are left
  alone. Failed mounts are retried at most once per 60s per project.
- **Correct transcript:** rows ordered by state.db id, compaction summaries
  shown as system rows (never as user messages), no duplicate user message for
  an app send, tool calls emit start and finish, persisted reasoning is shown.
- **iOS app:** the live banner reaches Connected (hello is matched by `type`),
  and the resume cursor rebases when the API restarts and renumbers events.
- **`scripts/api_smoke.py`:** end-to-end check that drives the real WebSocket
  protocol and fails unless the reply comes back in order.
