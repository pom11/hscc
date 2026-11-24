kind: Changed
task: t_b56a5e1e

* monitors: the local merge queue is ONE compact `mq` line instead of a table
  per project, and it is global rather than hscc-shaped.
  `hscc kanban monitor` and `hscc cards monitor` now share one formatter
  (`hscc_daemon/kanban_live.py::merge_queue_line`):
  `mq  hscc #726 gating 12m · 9q · 7w · last #733 merged 5m`, one
  ` | `-separated segment per ENABLED project that has something queued,
  waiting or in flight (`idle` in place of the PR when nothing is claimed),
  and hidden entirely when no enabled project has anything at all. Over the
  terminal width the `· last …` cells drop first, then whole projects from the
  end (`+N proj`). No per-PR rows and no error text on the line — the detail
  stays in `hscc merge-queue list <proj>`, and `hscc kanban monitor --json`
  still prints the whole read model unchanged.
---
kind: Fixed
task: t_b56a5e1e

* merge-queue: a ledger whose per-project value is not an object (a hand-edited
  or half-written `merge-queue-state.json`) made `merge_queue._project_state`
  raise `AttributeError`, so `hscc kanban monitor`, `hscc merge-queue list` and
  `GET /v1/merge-queue` failed on a queue they should have read as empty. Such
  a value is now treated as an empty project state.
