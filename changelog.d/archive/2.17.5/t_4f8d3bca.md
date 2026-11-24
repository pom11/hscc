kind: Fixed
task: t_4f8d3bca

- **A red PR in the merge-queue wait list stayed red forever.** The wait
  list only ever pulled a PR back into the FIFO once its checks went green;
  nothing re-ran the checks. PRs sat 9-18 h red because *main* was broken
  when their CI ran, or because one flaky leg happened to fail on their run —
  in both cases the PR was fine and the verdict was stale. The per-tick
  wait-list pass now updates a wait-listed PR whose checks are `red` or
  `cancelled` onto the current base with `gh pr update-branch`, which
  re-triggers CI on a base that may since have been fixed. The entry records
  the base sha it was refreshed onto (`refreshed_main_sha`, `refreshed_at`),
  so this happens at most once per base commit per PR and cannot loop; a
  conflicted update is never retried and is flagged `update_conflict` for the
  hand-back that follows. PRs outside the wait list are untouched.
- **The refresh died on every tick in production, and took the tick with it.**
  The daemon reaches the wait list through the drain with NO runners
  (`check_merge_queue` injects none), so a `None` arrived at the `gh` call and
  the `TypeError: 'NoneType' object is not callable` escaped into `tick()` —
  the queue reported `last_error` and processed nothing, every tick. Both
  runners are now defaulted at the top of the drain, as in `process_entry`
  and `_resume_ci_pending`, so every gh/git call made from there inherits a
  runner. A `gh` that could not run at all is no longer recorded as an attempt
  on the base either: it keeps `last_update_error` and leaves
  `refreshed_main_sha` unset, so the refresh resumes the moment `gh` can run
  instead of writing the base off for good.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_merge_queue.py`: 134 -> **147** cases. The new ones
  pin the bound rather than the happy path — one `gh pr update-branch` per
  main commit (not per tick, not zero when main moves), no retry after a
  conflict even onto a newer base, no retry after a `gh` error, and zero
  `gh` calls at all when the wait list is empty. One case injects no git
  runner, so the new base probe runs through `_default_runner`, the seam
  that once raised on every production call while the injected runners
  passed. The three production-shape cases inject NO runner at all — one
  pins the `pr update-branch <n>` argv the drain's refresh was handed, one
  the same argv for a direct refresh call (which defaults its own runners
  rather than trusting the drain), and one captures every runner the drain
  hands downward across BOTH of its red paths and fails on a `None` — and a
  fourth pins that a local gh fault retries the same base instead of
  burning it.
