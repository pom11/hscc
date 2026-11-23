kind: Fixed

- **The merge queue fetches `origin` before every entry, not only when its
  worktree is first created.** A PR merged between two entries advanced the
  base on GitHub while the queue worktree kept the old remote-tracking ref, so
  the next PR was rebased and gated against a stale base.
- **The queue uses each PR's `baseRefName` and the repo's real default
  branch.** `origin/main` was hard-coded, so `master` projects
  (`ecofire-app`, `kronos`) could never create a worktree; a PR targeting
  `release` was silently rebased onto `main`. The default is now detected from
  `origin/HEAD` (falling back to an existing `origin/main`/`origin/master`),
  `merge_queue.base_branch` can pin another base, and a PR on any other base is
  refused once with a clear reason.
- **Cross-repository (fork) PRs are refused, not retried.** `isCrossRepository`
  is now read; the queue only pushes branches in the project's own `origin`,
  so a fork PR gets a terminal `refused` outcome and the reason on the PR
  instead of three failed fetches and a permanent retry loop. Auto-enqueue
  skips fork and wrong-base PRs for the same reason.
- **Projects that share one repo path now share one FIFO and one per-repo
  lock.** Two registry names on the same checkout (`ecofire-bc`,
  `ecofire-pontaje`) each ran a PR per tick from separate worktrees; entries
  are now ordered together and only one PR per repo is claimed per tick, under
  a lock keyed by the canonical repo path.

---
kind: Verified

- `hscc_daemon/tests/test_merge_queue.py`: six new tests (fresh fetch on an
  existing worktree after a second clone advances the base, `master` default,
  wrong-base refusal plus auto-enqueue skip, configured `base_branch`, fork
  refusal plus auto-enqueue skip, and shared-repo FIFO/lock). Each fails on the
  pre-fix file.
- `hscc-api/tests/test_routes_merge_queue.py`, `hscc merge-queue status/list`
  under a temp `HSCC_HOME`, and `python3 scripts/address_guard.py --tracked`
  (clean).
