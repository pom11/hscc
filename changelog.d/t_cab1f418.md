kind: Fixed
task: t_cab1f418

- **A healthy, busy merge queue read as a stalled one, because the surfaces
  showed neither liveness nor reasons.** Measured 2026-11-27:
  `state/merge_queue.json` held a live gating claim (PR #847, elapsed 90.4 s)
  while `hscc merge-queue list hscc --json` answered `in_flight: None` with
  two waiting PRs exposed as bare `{pr, attempts}` rows — two orchestrators
  read that pair as a 4-hour livelock while the engine was alive and
  REFUSING those waiting PRs every tick (`mergeable=CONFLICTING` each,
  checks green, 25–26 commits behind main). Three reporting gaps, three
  fixes, no queue decision touched:
  1. **Liveness is a surface now.** `view()`, `status --json` and
     `list` carry an `engine` block: the age of the LEDGER write
     (`generated_at`, advances every tick that returns) and of the
     health STAMP (heartbeats from inside a gate leg), and
     `seconds_since_last_write` — the fresher of the two. A ledger
     alone freezes for a whole leg and would call a live gating engine
     dead; the pair cannot. "No claim, 400 s" is a healthy idle;
     "no claim, 4 h" visibly is not.
  2. **A waiting entry names WHY it waits.** The wait-list drain and the
     initial park now stamp `waiting_reason`/`waiting_detail` onto the
     entry (rendered by `view` rows and `list`; carried whole by
     `status --json` / `list --json`):
     `dirty-head` (mergeable=CONFLICTING) is distinct from
     `checks-failing`, `checks-pending`, `changes-requested`,
     `draft`, `merge-unknown` — the refusals that looked like one
     state today are distinct fields, asserted, not log volume.
  3. **`list` renders a held claim.** It used to print "merge queue is
     empty" while a gate ran — the claim was in the data but not on the
     human surface.
- Classification reuses `_pr_ready`/`_checks_verdict` verbatim —
  `required_checks=None` ("could not determine") passes through
  un-normalized, so an undetermined answer is never read as
  green.
