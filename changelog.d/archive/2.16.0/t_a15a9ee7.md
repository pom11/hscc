kind: Docs
task: t_a15a9ee7

- **`ios-app/docs/WIDGET_COVERAGE.md` now matches the widget tree on `main`:**
  nine iOS/macOS widgets (Projects, CardsRunning, KanbanMonitor, BoardStatus,
  Cluster, Monitor, Needs Me, Cluster Graph and Merge Queue), the same nine
  files compiled into `HSCCMacWidgets`, and the five Live Activities. The
  Cluster Graph and Merge Queue rows are present, the macOS XL list names
  Cluster Graph, and there are no notification widget or Live Activity rows.
  The field matrices now record what merged after the first measurement pass:
  the accessory families draw the CLI temperature cells through
  `CLILockScreenMetrics` (#415), and the Monitor Live Activity draws the CLI
  host and `total (n)` rows through `MonitorActivityMetrics` (#423). The
  hardcoded decode tally in the Needs me section is replaced by the gate's own
  `N/N` banner, like the other gates.
