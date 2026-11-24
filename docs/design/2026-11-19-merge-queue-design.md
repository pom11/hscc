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

* **Per repo: a FIFO of green PRs.** One PR at a time per repo. Registry
  projects that share one repo path (for example two product names on the same
  checkout) share that FIFO and a single per-repo processing lock.
* **Queue worktree:** `~/.hscc/merge-queue/<project>/` — a git worktree of the
  project repo, never the operator's checkout. The queue state ledger is
  `~/.hscc/merge-queue-state.json`, written atomically (temp + fsync +
  `os.replace`) and reloaded on daemon start, so a restart resumes instead of
  forgetting.
* **Steps per PR:** fetch `origin` on every entry (not only when the worktree
  is created), rebase the PR head onto `origin/<base>` (a real `git rebase`;
  no automatic conflict resolution), run the project's gates,
  `git push --force-with-lease` to the PR branch, then
  `gh pr merge --squash --delete-branch`. Record the pushed and merged SHAs.
  `<base>` is the repo's default branch, detected from `origin/HEAD` (so
  `master` projects work too), unless the registry declares
  `merge_queue.base_branch`.
* **Refusals, not retries:** a PR whose `baseRefName` is not the queue's
  `<base>` is refused once with a clear comment; `merge_queue.base_branch`
  opts a project into a different target. A cross-repository (fork) PR is
  refused the same way, because the queue only pushes branches in the
  project's own `origin`. Auto-enqueue skips both, so neither is re-added
  every tick.
* **Conflict or red gate:** abort the rebase cleanly, comment on the PR with
  the conflicted files or the failing gate's tail, hand the card back to its
  worker as a *resolve* step (comment + reassign/unblock via the `hermes
  kanban` CLI), and drop the PR from the queue.
* **Auto-enqueue:** a card whose PR is open, not draft, mergeable, on the
  queue's base, not a fork, and whose `gh` checks are green.
* **Manual:** `hscc merge-queue add <project> <pr>`.

## Ownership

Orchestrators plan and review. They never rebase or merge; the queue does.
A project is **enabled by default** when all three are true:

1. its registry `repo` is the root of a git work tree;
2. `origin` is a `github.com` remote;
3. it declares gates (`merge_queue.gates`) or it is `hscc`, whose built-in
   gates apply.

A project missing any one stays off, and `hscc merge-queue status` says which
condition failed (`no repo`, `no GitHub remote`, `no gates`). The default is a
property of the checkout, not a flag someone has to remember:

```yaml
merge_queue:
  # Optional explicit override of the default-on rule. Omit both `enabled`
  # and the CLI override to follow the rule above.
  # enabled: false
  gates: ["pytest -q"]
  timeout_s: 1800
  # Optional: expect PRs against this base instead of the repo default
  # (e.g. release). PRs to any other base are refused.
  # base_branch: release
  # Optional: how long a PR may sit in the wait list before the queue drops it
  # (seconds; default 86400 = 24 h). Dropped entries get a PR comment with the
  # `hscc merge-queue add` to re-add them.
  # waiting_max_age_s: 43200
```

`hscc`'s own defaults are the address guard plus the pytest legs, with
`build_check.sh` added when the PR touched `ios-app/` or `macos-app/`.

Precedence, highest first: the ledger's explicit CLI override
(`hscc merge-queue enable|disable`), the registry's `merge_queue.enabled`, then
the default-on rule. A `disable` is persisted in the ledger and therefore
survives a daemon restart, a registry rewrite and a project that later becomes
eligible.

## CLI

`hscc merge-queue status|list|add|remove|pause|resume|enable|disable
<project> [--json]`.

`status` prints the effective state and, when the default-on rule declined a
project, the reason. Pause keeps the queue. Disable keeps the queue but stops
processing. Enable and disable are explicit per-project overrides of the
default and the registry setting; the CLI override wins and persists.
