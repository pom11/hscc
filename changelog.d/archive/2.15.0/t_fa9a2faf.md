kind: Fixed
task: t_fa9a2faf

- **The built-in orchestrator waker now actually delivers nudges.** Its own
  scan-state file and the daemon's stream-state file for the `orch_waker`
  stream were the **same path** — `~/.hscc/state/orch_waker.json` — and the
  stream stamp that keeps `hscc verify` happy is a whole-document
  `state.write_state`, so every 30 s tick erased the scan state's
  `last`/`pending`/`sent` right after writing them. Every board therefore
  re-first-sighted on every tick and tail-started (the deliberate
  no-backlog-flood branch), batches never aged past `debounce_s` into a send,
  and the waker reported itself healthy forever: `ok: true`, a fresh tick
  stamp, a green doctor row, an empty audit JSONL, an empty `boards:` list in
  `hscc orch waker status`, and **zero nudges ever delivered**. The feature
  was inert while looking alive, and the only thing waking orchestrators was
  the `~/bin/orch-waker` stopgap.
  `waker_tick` now stamps through `stamp_entry`, which merges the stamp ON TOP
  of the scan state instead of replacing it, so one document serves both
  readers (the reverse direction already worked: `load_state` ignores unknown
  top-level keys and `save_state` writes back what it loaded, so the stamp's
  `last_tick`/`ok`/`message` survive a scan write untouched). A merge was
  chosen over renaming one of the two owners because the operator-facing
  promise is that the built-in continues from the stopgap's live
  `state/orch_waker.json` — a rename would have dropped the in-flight
  `pending` batches at hand-over, exactly the thing that promise exists to
  avoid. The merge carries only `{last, pending, sent}` forward, never a
  previous stamp's fields, so a `disabled`/`poll_gate` tick cannot keep
  advertising the last real scan's `nudged`/`decisions` counters, and it is
  skipped entirely when the two paths resolve to different files (a test or
  an operator pointing `state_path` elsewhere gets the old bare-stamp
  document, not a copy of scan state in a second file).
  The regression test (`hscc_daemon/tests/test_orch_waker_state_collision.py`)
  drives the pairing every earlier test bypassed — `check_orch_waker()` with
  **no** injected `write`/`read`, **no** injected `state_path`, the real
  `run_tick` chain and the real `state.write_state`, under a redirected
  `~/.hscc` — and asserts a nudge is actually delivered across ticks plus the
  per-board watermark and audit line that must accompany it. Of the seven
  existing tests that mention `waker_tick`, two replace the function itself
  and five stub `run_tick` — and all five of those inject `write=StreamSpy().write`
  instead of the real writer, one of them also passing a private `state_path`.
  So no test ever drove the deployed combination (real `state.write_state`
  writing the DEFAULT path), which is how this survived two cards and a green
  12-leg suite.
