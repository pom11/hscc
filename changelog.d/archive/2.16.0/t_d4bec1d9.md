kind: Added
task: t_d4bec1d9

- **A card handed back for resolve now tells its worker exactly how to land
  the fix.** The merge queue's conflict and red-gate handbacks carry the
  branch recorded on the PR (`wt/<card>` or the card's own branch) and the
  four steps: rebase onto `origin/main`, fix the conflict or failing gate,
  `git push --force-with-lease origin <own branch>`, then hand the card back.
  The merge queue keeps landing green PRs; the worker only ever touches its
  own branch.
- **Force-push is fenced to the run's own card branch.** A new committed
  `.githooks/pre-push` (detector: `scripts/worker_push_guard.py`, armed by the
  same `core.hooksPath` install as the address guard, so every worker worktree
  inherits it) refuses a non-fast-forward update to `main`, a tag, or any
  branch other than `HERMES_KANBAN_BRANCH` (falling back to
  `wt/$HERMES_KANBAN_TASK`). Non-worker processes — the operator, orchestrators
  and the merge-queue daemon in its own worktree — are untouched.

---
kind: Changed
task: t_d4bec1d9

- **Anti-churn rule 2 now names the merge queue as the landing owner.** In
  `hscc-roles/generator.py`'s single shared `_ANTI_CHURN_RULES` block, an
  orchestrator on a queue-enabled project reviews the diff and enqueues with
  `hscc merge-queue add <project> <pr>` — it never rebases or merges by hand.
  Projects without the queue keep the old rule verbatim: review and land the
  worker's green PR yourself, and rebase your own open PRs rather than carding
  them. Live profiles were not regenerated.

---
kind: Verified
task: t_d4bec1d9

- `hscc_daemon/tests/test_merge_queue.py`: the conflict and red-gate handbacks
  name `git rebase origin/main`, `git push --force-with-lease origin
  wt/t_card` and the "never force-push main or any other branch" rule.
- `scripts/tests/test_worker_push_guard.py`: own branch allowed (including
  `HERMES_KANBAN_BRANCH` override and the `wt/<task>` fallback); `main`,
  another card branch and a tag refused; fast-forward/create allowed;
  non-worker runs untouched; the real `.githooks/pre-push` driven through git's
  stdin contract for all three verdicts.
- `hscc-roles/tests/test_anti_churn_rules.py`: the new rule-2 wording, the
  retained manual fallback, and the single-source grep guard.
