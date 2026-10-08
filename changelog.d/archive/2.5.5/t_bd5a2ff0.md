kind: Fixed
task: t_bd5a2ff0

- **Worker-facing guidance now bans name-based suite kills.** The rule against
  `pkill -f "run_tests.sh"` / `pkill -f pytest` (name sweeps reap *other cards'*
  concurrent legs) is now enforced guidance in docs/HANDOFF.md step 5, the
  kanban-worker skill's Do-NOT list, and the `scripts/run_tests.sh` header —
  stop your own run per-pid via the process tool instead (t_bd5a2ff0).
