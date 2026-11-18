kind: Added
task: t_3fe8a7ec

- **`ios-app/scripts/merge_base_check.sh` — a branch whose merge-base with `main`
  has drifted too far behind now fails before review.** Four PRs in the
  board-status-widget line (#316, #317, #319, #335) died the same death and three
  of them reported it as something else: the base drifted behind `main` while
  `mergeable=MERGEABLE/CLEAN` kept answering green, because re-adding a file
  `main` deleted is not a conflict — #319 still carried 37 paths `main` had
  retired on purpose (the console screens, `Shared/ExtensionClient.swift`, the
  retired decode fixtures, the UITests). Measured while writing this: #316, #317
  and #319 all carried the SAME merge-base `52310791`, 16 commits behind
  `main` @ `3b5e475c` — three PRs rebasing onto a base they never moved. The gate
  measures `git rev-list --count <merge-base>..origin/main` — the commits `main`
  has that the branch's *base* lacks, which is what
  `git rebase --onto origin/main <merge-base>` has to absorb — against one named
  constant, `STALE_BASE_FAIL_AT`, and fails hard at it rather than warning: the
  author this gate is for already had a green badge and shipped anyway. The
  failure names the count and the base SHA and prints the rebase that drops only
  the inherited commits. It never fetches (a gate stays offline) and prints the
  base ref's SHA so a stale local fetch cannot pass as a real green, and it is
  honest about what it does *not* prove — drift is not harm, and zero drift is not
  a claim that the branch tree matches `main`. Proven red on a synthetic branch
  carrying this file on a base exactly `STALE_BASE_FAIL_AT` behind (rc 1, naming
  the count and the base SHA), green one commit nearer (rc 0), and green on a
  branch cut from current `main`.
