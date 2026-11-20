kind: Added
task: t_c7bbb5b0

- **Orchestrator waker: `skip_boards`.** A list in the waker's config file (`orch_waker.json`) naming boards the waker never nudges — for a finished project whose `<board>-orch` profile still exists but has no REPL pane, which otherwise retried and backed off forever. Treated exactly like a paused board (pending kept, no attempt, no audit line) without pausing its cards or holding `kanban.auto_decompose` off fleet-wide the way `hscc kanban pause` does. Non-string entries are dropped; a non-list value means none.
---
kind: Verified
task: t_c7bbb5b0

- `hscc_daemon/tests/test_orch_waker_dead_pane.py`: knob loaded, coerced and threaded into `run_tick`; a skipped board gets no delivery attempt and no audit line. `hscc_daemon/tests`: 4019 passed, 1 skipped; waker CLI and doctor suites pass.
