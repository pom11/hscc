kind: Fixed
task: t_94ca7ff4

* **A merge-queue gate that ran out of its budget was reported as a test
  failure and silently dropped the PR.** `_run_gate` knew the difference —
  its `TimeoutExpired` branch already wrote `timeout after {N}s` — but
  flattened it into `(False, output)`, so `process_entry` recorded
  `result="red-gate"`, `_finish` cleared the claim without requeueing, and
  the handback told the author "gate failed" with resolve instructions for
  code that had never been shown to be broken. Both queue handbacks measured
  on main (PR #360, PR #460) ended in the literal line `timeout after 1800s`
  and neither was a test failure. `_run_gate` now returns a tri-state
  `(verdict, output)` with `verdict` in `{"pass","fail","timeout"}` (internal
  contract; its one caller plus injected test runners). On `timeout` the
  entry goes back to the wait list via `_requeue_waiting` — it is **not**
  dropped — and the outcome is recorded as `gate-timeout`, which is
  non-terminal. `red-gate` keeps its meaning: a genuine non-zero gate still
  drops the PR.
* **The handback for a timed-out gate no longer sends the author off to fix
  code.** It names the gate, states the budget it exceeded, says plainly that
  this is **not** a test failure, and that the queue will look again on a
  later tick. The resolve-and-force-push instructions are gone from this path
  (they stay on the conflict and `red-gate` paths, where they belong).
* **The read model can now say "ran out of budget".** `MERGE_QUEUE_STATES`
  gained `timeout` (the stage stamped on the in-flight claim and mirrored onto
  the health stream, so the stream does not still say `merging` for a PR the
  queue has already handed back) and `_OUTCOME_STATES` maps
  `gate-timeout -> waiting`. `hscc merge-queue status` and
  `GET /v1/merge-queue` therefore render a timed-out PR as `waiting` — never
  as `resolve`/`failed`, which is what `red-gate` still maps to.
---
kind: Verified
task: t_94ca7ff4

* `hscc_daemon/tests/test_merge_queue.py` — 7 new cases, all injected runners
  only (no real repo, no network): a fake `subprocess.run` raising
  `subprocess.TimeoutExpired` yields `result == "gate-timeout"` with the entry
  still in `waiting`, `in_flight` cleared and no `red-gate` recorded anywhere;
  the timeout handback says the budget was exceeded, says it is not a test
  failure, and carries **neither** resolve instruction; a genuine non-zero
  gate still yields `red-gate` and still drops the entry from both lists;
  `outcome_state("gate-timeout") == "waiting"` while `red-gate` still maps to
  `resolve`; `view()` renders the requeued PR as a `waiting` row whose
  `last_result` is `gate-timeout`; the health stream `hscc verify` reads is
  stamped `stage="timeout"` rather than left saying `merging`; and the
  tri-state unit case pins all three verdicts plus the `timeout after {N}s`
  tail.
* Proven to bite by an in-place mutation sweep (8 mutants, all anchors
  occurrence-count-1, provenance gate asserting the mutated file is the one
  imported, whole-file runs so a sibling catch is not misattributed): forget
  the requeue, record `last` before the requeue, flatten `timeout` back into
  `fail`, map `gate-timeout` to `resolve`, drop `timeout` from
  `MERGE_QUEUE_STATES`, never stamp the stage, remove "NOT a test failure"
  from the handback, and stop dropping on `fail` — 8/8 caught.
* Ordering trap covered by an assertion, not a comment: `_requeue_waiting`
  only re-appends while the PR still holds the claim and `_finish` clears
  `in_flight`, so `_requeue_waiting` must run first — the reverse order
  reproduces the original drop.
* Measured: `hscc_daemon/tests/test_merge_queue.py` 80 passed (73 pre-existing
  at `a4c567a4` + 7 new) under `~/.hermes/hermes-agent/venv/bin/python`.
