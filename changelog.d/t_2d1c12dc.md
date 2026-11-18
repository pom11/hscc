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
  `ios-app/Sources/HSCCWidgets/CLIFleetTableReading.swift` holds the columns,
  the row cap and the accessibility sentence on Foundation only, which is what
  lets `scripts/cluster_widget_check.sh` execute the table against the committed
  fixture; `CLIFleetHostTableView.swift` is the SwiftUI grid and owns exactly
  one decision — which slot of the operator's palette a threshold verdict
  paints. Temps and power are coloured through the view-model's
  `cpuTempLevel`/`gpuTempLevel`/`gpuPowerLevel` (and CPU/RAM through
  `cpuLevel`/`memLevel`); an ok cell stays in plain ink, because the CLI prints
  every cell in the default style and a green wall of numbers would stop
  looking like the CLI. The Host column draws `displayLabel`, never the wire
  address (§7), and the operator's WidgetStyle drives every font, colour,
  density and the header/icon switches, as on the other families.
