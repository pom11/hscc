kind: Fixed

- **Remove/pause/disable during a run now stops before the push and the
  merge.** The tick re-reads the claim under the file lock after the rebase,
  after the gates, and again after the post-push probe; a cleared claim means
  the operator removed the PR and it is dropped without a force-push or merge,
  while pause/disable keep the entry at the head of the queue for later.
- **The queue re-reads the PR after its force-push and merges only the exact
  green head it pushed.** `headRefOid`, `statusCheckRollup` and `mergeable` are
  checked on the new head, the merge is pinned with
  `gh pr merge --match-head-commit <pushed sha>`, and an empty rollup is no
  longer assumed green: it is accepted only when the base branch's
  required-status-checks endpoint says the repo requires none (a 404), not when
  the answer is unknown.
- **Auto-enqueue only considers cards in `running`, `ready` or `review`.** A
  `blocked`/`scheduled`/`todo` card is never merged just because its PR looks
  green, and a PR with `reviewDecision: CHANGES_REQUESTED` is skipped even on
  a ready card.

---
kind: Verified

- `hscc_daemon/tests/test_merge_queue.py`: ten new cases cover the three
  fixed defects — remove mid-gate, pause/disable mid-gate (queue entry kept),
  post-push pending checks, a changed head SHA, empty rollup with and without
  required checks, the `--match-head-commit` argument, card-status filtering,
  and `CHANGES_REQUESTED`; each fails on the pre-fix file.
- `hscc_daemon/tests/test_merge_queue.py`,
  `hscc-api/tests/test_routes_merge_queue.py` and
  `hscc_daemon/tests/test_kanban_live.py`: 91 passed. `hscc merge-queue
  add/list/status/remove` under a temp `HSCC_HOME`, and
  `python3 scripts/address_guard.py --tracked` (clean).
