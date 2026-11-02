kind: Added
task: t_2b2a78ae

- **orch-waker W2 — the wake now reaches the REPL (`hscc_daemon/orch_waker.py`).**
  W1's pure core decided *when* to nudge but had no delivery; W2 wires it end
  to end behind `run_tick(now=...)`, the entry the daemon loop (W3) calls
  every ~30 s. Per tick the waker reads the paused-board file
  (`~/.hscc/state/kanban_paused.json` — pending for a paused board is
  dropped, its last-id watermark still advances), resolves each board's
  orchestrator through the repo's single naming rule
  (`kanban_pause.orchestrator_for` → `<board>-orch`) gated by the stopgap's
  existence check (`~/.hermes/profiles/<board>-orch` on disk — no hardcoded
  board lists), and delivers with one nudge per board per tick through the
  **in-process** `repl_inject` parity module (`hscc-api/repl_inject.py::inject`,
  `mode="auto"`): the parity module's mode logic resolves to `/queue` while
  the orch is busy and a plain send when idle, so a nudge never interrupts a
  running turn — the same entry point and posture `orch_message` uses. No
  shell-out to `~/bin/hermes-inject`, no raw tmux from the daemon. Every
  delivery attempt appends exactly one JSON line to
  `~/.hscc/state/orch_waker.jsonl` (`ts`, `board`, `profile`, `ok`, `outcome`
  = `sent` or `failed: <error tail>`, `msg`) — append-only, never rewritten.
  Failure paths all survive: a failed inject is audited and its batch stays
  pending for the next tick; an unreadable paused file means nothing paused;
  `run_tick` never raises, because the daemon calls it forever.
  `hscc_daemon/tests/test_orch_waker.py`: 24 -> **37** offline tests (queue
  path with a busy-orch stub, real-`repl_inject` delivery with its tmux/ps
  seams stubbed asserting the `/queue` prefix, paused skip with no audit
  line, missing profile, failed-audit + retry, two ticks append two lines).
