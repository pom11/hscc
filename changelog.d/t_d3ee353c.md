kind: Changed
task: t_d3ee353c

- **Cluster widget + ClusterHealth Live Activity + Monitor widget/Live Activity
  now render the CLI rows instead of derived aggregates.** The cluster tile
  reads `GET /v1/cluster/monitor` (host table + serving table) and
  `GET /v1/cluster/status` (workload/host counts), and draws: the answered/listed
  host tally, each node's serving-table model and up/down state, the CLI total
  `Gen t/s` (sum of `serving[].gen_tps`), and the sample's GPU%/CPU/GPU
  temperatures. The hard-coded `.244/.246/.247/.248` topology glyph, the load
  average the host table does not print, and the kanban board row are removed.
  The monitor tile reads the same monitor route plus `/v1/daemon/host`, and adds
  the daemon-health line from `hscc status`. Both Live Activities carry the
  tally, model and throughput in their pushed content state; the Lock Screen and
  Dynamic Island compact forms show the tally as the headline number.
- **Every family is one text model.** `FleetProjection` / `FleetText` in
  `Sources/Shared/ClusterNodeTally.swift` project the contract fields once; the
  six iOS families and four macOS families render the same lines, so a family
  cannot re-round or invent a number. `ios-app/scripts/cluster_widget_check.sh`
  executes the projection and asserts each family's exact lines against the
  committed scrubbed live capture.
- **Fixture scrubbed to the current wire shape.** `v1_cluster_monitor.json`
  now carries `serving[]` and `workloads[].workload_id` (no private recipe path,
  hostname or job id), and the cluster widget gate decodes it plus the degraded
  `{speak}` body. `ios-app/docs/WIDGET_COVERAGE.md` records the new sources and
  gate counts.
