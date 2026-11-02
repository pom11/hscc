kind: Fixed
task: apply-serving-skip

- **`hscc template apply` no longer hangs on units that are already up.** The
  `cluster up` fix (t_be4b94ca) never reached apply's own launch loop: a unit
  already running the same recipe still got `sparkrun run --ensure`, whose
  `ssh … bash -s` can fail to EOF, so adding a 4th node hung every existing
  unit up to the 15-min provision timeout. Apply now probes each span node's
  `/v1/models` (non-empty catalogue on EVERY node) and skips the call when the
  unit is already serving and not being recreated; drift is still reported,
  `--force-recreate` still relaunches, and any doubt falls back to launching.
- **`4node-flash-next-all` declares its kanban caps and balancer** —
  `caps: {max_in_progress: 12, max_in_progress_per_profile: 2}` (operator
  rule: ~5 running requests per node; 4 nodes minus orchestrator/subagent
  headroom) and `proxy.families.coding.routing: least-in-flight` — so apply,
  not hand edits, owns them.
