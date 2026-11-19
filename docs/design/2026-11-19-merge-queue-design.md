# Local merge queue (2026-11-19)

Status: accepted (MQ1). The daemon owns a **local** merge queue; no GitHub
org and no GitHub merge queue are involved. Plain `git` + `gh`.

## Goal

Any number of workers per orchestrator without rebase churn.

## Why the old flow failed

Workers branch from the same `main` and touch the same shared lines. The first
merge breaks every other PR. Workers cannot rebase/force-push, so every rebase
became a new card and the orchestrator became the bottleneck.

## Decision

A local merge queue runs in the `hscc` daemon on the gateway Mac.

* **Per project repo: a FIFO of green PRs.** One PR at a time per repo.
* **Queue worktree:** `~/.hscc/merge-queue/<project>/` — a git worktree of the
  project repo, never the operator's checkout. The queue state ledger is
  `~/.hscc/merge-queue-state.json`, written atomically (temp + fsync +
  `os.replace`) and reloaded on daemon start, so a restart resumes instead of
  forgetting.
* **Steps per PR:** fetch, rebase the PR head onto `origin/main` (a real
  `git rebase`; no automatic conflict resolution), run the project's gates,
  `git push --force-with-lease` to the PR branch, then
  `gh pr merge --squash --delete-branch`. Record the pushed and merged SHAs.
* **Conflict or red gate:** abort the rebase cleanly, comment on the PR with
  the conflicted files or the failing gate's tail, hand the card back to its
  worker as a *resolve* step (comment + reassign/unblock via the `hermes
  kanban` CLI), and drop the PR from the queue.
* **Auto-enqueue:** a card whose PR is open, not draft, mergeable, and whose
  `gh` checks are green.
* **Manual:** `hscc merge-queue add <project> <pr>`.

## Ownership

Orchestrators plan and review. They never rebase or merge; the queue does.
Opt-in is per project; registry entries declare their gate commands:

```yaml
merge_queue:
  enabled: false
  gates: ["pytest -q"]
  timeout_s: 1800
```

`hscc`'s own defaults are the address guard plus the pytest legs, with
`build_check.sh` added when the PR touched `ios-app/` or `macos-app/`.

## CLI

`hscc merge-queue status|list|add|remove|pause|resume|enable|disable
<project> [--json]`.

Pause keeps the queue. Disable keeps the queue but stops processing. Enable
and disable are explicit per-project overrides of the registry opt-in.
