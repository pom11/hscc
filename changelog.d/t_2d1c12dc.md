kind: Added
task: t_2d1c12dc

- **The `systemMedium` Cluster and Monitor tiles now draw
  `hscc cluster monitor`'s host table.** One row per host with the CLI's own
  nine columns in the CLI's own order — Host, Jobs, CPU%, RAM%, Swap%, GPU%,
  CPU Temp, GPU Temp, GPU Power — under the CLI's own header row. Every string
  in the table is `CLIMonitorHostRow.cells` and every title is
  `CLIMonitorHostRow.columns` from the shared view-model, so the units (`" C"`,
  `" W"`), the rounding (`{:.0f}`/`{:.1f}`/`{:.2f}` per column) and the `-` for
  an unreadable metric are the CLI's, not a widget's guess at them. The tile
  owns layout and colour only.
  `ios-app/Sources/HSCCWidgets/CLIMonitorTableReading.swift` holds the columns,
  the row cap and the accessibility sentence on Foundation only, which is what
  lets `scripts/cluster_widget_check.sh` execute the table against the committed
  fixture; `CLIMonitorHostTableView.swift` is the SwiftUI grid and owns exactly
  one decision — which slot of the operator's palette a threshold verdict
  paints. Temps and power are coloured through the view-model's
  `cpuTempLevel`/`gpuTempLevel`/`gpuPowerLevel` (and CPU/RAM through
  `cpuLevel`/`memLevel`); an ok cell stays in plain ink, because the CLI prints
  every cell in the default style and a green wall of numbers would stop
  looking like the CLI. The Host column draws `displayLabel`, never the wire
  address (§7), and the operator's WidgetStyle drives every font, colour,
  density and the header/icon switches, as on the other families. Both entries
  carry the view-model from the same read that produces their glance projection
  (still two requests), and reach the table only through `cliHostRows`, the one
  place unconfigured, paused and "nodes" switched off are refused.

---
kind: Verified
task: t_2d1c12dc

- **`ios-app/scripts/cluster_widget_check.sh` now executes the medium tile's
  table instead of describing it.** The gate compiles
  `CLIMonitorTableReading.swift` with the real view-model on the macOS SDK and
  asserts, per host of the committed fixture, that the drawn cells are
  byte-identical to `CLIMonitorHostRow.cells`, that the drawn titles are the
  CLI's own `columns`, which five columns carry a threshold verdict and which
  four carry none, that the four-row budget of the size and its `+n` agree with
  the route's host count, and that no drawn cell is a dotted-quad address. The
  gate states (`paused`, `unconfigured`, section off, no host list) are executed
  through `cliHostRows` rather than grepped. Structural proofs cover what an
  executed check cannot see: both CLI-shaped tiles draw the ONE table view, no
  column title is transcribed into a widget body, and no family reaches
  `entry.monitor` directly. Six mutations were run — a cell suffix, drawing the
  wire address in Host, a changed row cap, a dropped column, a bypassed
  `cliHostRows`, and the table removed from the body — and each reddened the
  gate with the file restored byte-identically afterwards.
