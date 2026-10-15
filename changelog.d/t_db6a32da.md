kind: Added
task: t_db6a32da

- **`/v1/cluster/status` workloads now carry a `nodes` list.** Each entry of
  `workloads` gains one optional key, `"nodes": ["10.0.0.1", "10.0.0.2"]`,
  the host lines of that job's block in `sparkrun status` — the unit→node
  edge the iOS "Nodes & serving" screen needs. `cmd_cluster_status()`
  (`hscc-cluster/hscc.py`) already walked those lines and threw them away; it
  now keeps the IP for the `Job:` block in play. A TP=2 job legitimately
  reports BOTH nodes; a solo job reports its one. Purely additive — the
  existing four keys are untouched and byte-identical, no new subprocess
  call (the same `sparkrun status` output is already in hand), and `handle_cluster_status`
  forwards the workloads verbatim so the key rides through unchanged. Old
  clients keep decoding exactly as before; new clients must treat `nodes` as
  optional because a pre-field engine omits it.
