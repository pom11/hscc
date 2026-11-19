kind: Added
task: t_ac7fbe2d

- **A local merge queue now lands green PRs one at a time per project.**
  `hscc_daemon/merge_queue.py` keeps a durable FIFO in
  `~/.hscc/merge-queue-state.json` (atomic replace + fsync, recovered to the
  queue head after a daemon restart) and runs each PR in its own worktree under
  `~/.hscc/merge-queue/<project>/`: fetch, real `git rebase origin/main`,
  project gates, `git push --force-with-lease`, then
  `gh pr merge --squash --delete-branch`. A conflict or red gate aborts
  cleanly, comments the conflicted files / gate tail on the PR, hands the card
  back to its worker as a resolve step through the `hermes kanban` CLI, and
  drops it from the queue.
- **Opt-in per project through the flightdeck registry.** A new optional
  `merge_queue: {enabled, gates, timeout_s}` key is parsed and round-tripped by
  `flightdeck.core.registry`; `hscc`'s own defaults are the address guard, the
  pytest legs, and the relevant `build_check.sh` when `ios-app/` or
  `macos-app/` changed. Auto-enqueue picks up cards whose PR is open, not
  draft, mergeable and green.
- **`hscc merge-queue status|list|add|remove|pause|resume|enable|disable
  <project> [--json]`** steers it, and the daemon's `merge_queue` stream ticks
  once a minute. Design: `docs/design/2026-11-19-merge-queue-design.md`.

---
kind: Verified
task: t_ac7fbe2d

- `hscc_daemon/tests/test_merge_queue.py`: throwaway bare origin + clone cover
  a clean rebase/push/squash merge, FIFO one-PR-per-tick, conflict -> resolve
  handback with an aborted rebase, red gate tail handback, restart mid-run
  recovery from the persisted in-flight entry, pause leaving the queue
  untouched, the checks-green predicate, and hscc's conditional app build
  gates.
- `hscc-project/tests/test_registry.py`: absent / round-tripped `merge_queue`
  plus malformed shapes raising `RegistryError`.
- Smoke-tested the real CLI against a throwaway `HOME`: `hscc.py merge-queue
  status`, `add`, `list`, `pause`, `resume`, `remove`, `enable`, `disable`
  and `--help`, including `--json`.
