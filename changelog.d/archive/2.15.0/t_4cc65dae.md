kind: Added
task: t_4cc65dae

- **orch-waker W1 — the pure waker core (`hscc_daemon/orch_waker.py`).**
  Card events (done / worker-raised blocked / GAVE UP / CRASHED / timed out /
  spawn failed / review requested / block loop) never woke an orchestrator
  REPL: kanban notify+wake only reaches gateway chat platforms, there is no
  wait tool, and the orch SOUL bans polling, so blocked/done cards sat until
  someone typed. This is the event-scan/decision core of the built-in fix
  (W1 of 4 — W2 wires repl_inject delivery, W3 the daemon loop, W4 the docs).
  `tick(now, boards, paused, has_orch, inject, state_path, ...)` scans every
  board's `task_events` read-only (sqlite `mode=ro`, 2s timeout, unreadable
  boards warned once per run and skipped), tail-starts each board on first
  sight (no backlog flood), skips orch/operator-raised blocks (run_id NULL),
  batches per board behind the stopgap's debounce (90s) and per-board min-gap
  (300s), and formats the nudge byte-identically to the operator's live
  `~/bin/orch-waker` stopgap — same labels, same dedup + 6-line cap +
  "+N more", same `{"last","pending","sent"}` state schema at
  `~/.hscc/state/orch_waker.json` (tmp+os.replace atomic), so retiring the
  stopgap takes over its state file seamlessly. The module is pure: clock,
  delivery, orch resolution and the paused set are all injected; the state
  file is its only side effect. Board enumeration covers every
  `~/.hermes/kanban/boards/<slug>/kanban.db` **plus** the legacy
  `~/.hermes/kanban.db` default board (which the stopgap missed) with its
  own last-id key. `hscc_daemon/tests/test_orch_waker.py`: 24 offline tests
  (temp sqlite boards, tmp state, injected clock) including the stopgap
  takeover round-trip and the unreadable-board-not-fatal path.
