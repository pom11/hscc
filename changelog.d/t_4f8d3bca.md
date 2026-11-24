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
