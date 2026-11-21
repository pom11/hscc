kind: Added
task: t_480cee2d

* **`hscc cluster status` says why a node is idle: `paused (reason, since <t>)`.**
  `hscc_daemon/cluster_render.py` now reads the node-pause overlay when it draws
  the cluster summary panel, and adds a `node pauses` block — one line per parked
  node, `192.0.2.10  paused (training run, since 2026-11-21T18:04:07+00:00)`.
  Until now the parked machine simply sat in `idle hosts` with nothing attached to
  it: `cmd_cluster_status` parses sparkrun's own status output, and sparkrun has no
  idea a human parked anything, so a deliberate act rendered as an unexplained gap
  in the fleet. This is the visibility half of the overlay on the CLI side — the
  repair loops already skip parked nodes (watchdog auto-heal, keepalive, `cluster
  up`, autoscale, the heal breaker, autodown) and the API edge already projects
  `status`/`paused_reason`/`paused_since` (t_824b617f); the screen an operator
  actually types was the last one that could not say so.
* **The block is additive, and a parked node absent from the frame still gets its
  line.** With no pause recorded the panel is byte-identical to what it printed
  before, so nothing on this screen moves. A node the frame never listed appears
  anyway — sparkrun does not report a machine whose workloads are stopped, and the
  ABSENT row is exactly the one an operator must not conclude is healthy. Span
  siblings are parked with the keyed host (the `covering` rule from #560), a
  host's own record wins over a span record naming it, and an empty `reason` or a
  missing `since` degrades to `paused (since …)` / `paused (…)` / bare `paused`
  rather than inventing either.
* **The same word in `hscc cluster monitor`'s serving grid.** A parked backend
  whose metrics endpoint stopped answering read `down` in red — while the API's
  own monitor payload had been saying `status: "paused"` for that very row since
  #569, so the CLI and the widgets called one machine broken and the other
  deliberate. `serving_table`/`render` now take the overlay and the cell reads
  `paused (reason, since …)` in `warn`, the row's error text intact, plus a
  `node pauses` block under the table (the grid's Model column caps at 26 chars;
  the block carries the string on any terminal that can hold it). `pauses=None`
  still means "no pause knowledge" rather than "go read `~/.hscc`", so
  `cluster_monitor_cli_parity.py`'s committed golden stays hermetic to whoever
  regenerates it; the two live surfaces (`--live` and the snapshot) pass the
  overlay, and a test pins that each of them actually does.
* **The seam: `pause_overlay(directory=None)`, plus `pause_display` /
  `pause_for_node` / `pause_lines`.** One reader, one formatter, one identity
  rule (trim + case-fold, nothing clever), reused by every fleet view; the
  renderer holds a projection and no parser of its own (pinned: it never opens
  the overlay file, and calls `node_pauses.list_pauses` exactly once). `directory=`
  is the overlay's documented explicit-root seam, threaded not env-read, so the
  reader and the CLI that wrote the pause can agree on a root the way two processes
  do. An unreadable overlay fails open to "no pauses" and the screen still renders;
  the error panel path never reads it at all. 51 tests, all against TEST-NET-1
  (192.0.2.0/24) hosts and a tmp-dir overlay — no live overlay, no sparkrun, no ssh.
