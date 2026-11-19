import Foundation

// ===========================================================================
// The CLI-shaped host table, as data.
//
// `systemMedium` is the "looks like the CLI" size (t_2d1c12dc): one row per
// host and the nine columns `hscc cluster monitor`'s `host_table()` prints, in
// its order, with its units and its rounding. This file is where a surface
// asks for those cells; `CLIMonitorHostTableView.swift` is the only thing that
// draws them, and `ClusterMonitorViewModel.swift` is the only thing that
// formats them.
//
// WHAT THIS FILE OWNS (three things, and nothing else):
//
//   1. which columns the surface draws, in which alignment — the CLI's own
//      list, `CLIMonitorHostRow.columns`, filtered through the specs below;
//   2. how many rows the tile has room for (`mediumRowLimit`);
//   3. the VoiceOver sentence for a row, built from the same cells.
//
// WHAT IT DELIBERATELY DOES NOT OWN: a number, a unit, a rounding, an
// abbreviation or a colour. No cell string is assembled here — `text` is
// `CLIMonitorHostRow.cells[index]`, i.e. the exact string the CLI hands to
// `Table.add_row`, and a title is `CLIMonitorHostRow.columns[index]`. That is
// the whole point of the split: `scripts/cluster_widget_check.sh` compiles and
// RUNS this file on the macOS SDK against the committed fixture, so "the
// medium tile shows the CLI's table" is executed against the CLI's own goldens
// rather than being a claim in a PR body.
//
// FOUNDATION-ONLY ON PURPOSE. The moment this file names a `Color` it can no
// longer be compiled by a macOS harness (`ClusterMonitorViewModel.swift`'s
// header has the same rule for the same reason), and the table's rules would
// go back to being untestable outside a simulator.
// ===========================================================================

/// One cell of the drawn table: the CLI's column title, the CLI's text for
/// that column on one host, and the view-model's threshold verdict for it.
struct CLIMonitorHostCell: Equatable, Identifiable {
    /// The CLI's own column title (`host_table`'s `add_column` name). It is the
    /// identity too: a view keyed on anything else could silently reorder the
    /// columns, and a column title is the only thing a `host_table` reader
    /// identifies a number by.
    let id: String
    /// The cell exactly as `Table.add_row(...)` receives it, `-` included —
    /// except in the Host column, where it is `CLIMonitorHostRow.displayLabel`
    /// (see `CLIMonitorTableReading.cells(for:)`; a tile never prints an address).
    let text: String
    /// The verdict for THIS column, or nil when neither the CLI nor the
    /// view-model thresholds it. Nil is not `.unknown`: it means "this column
    /// has no verdict to paint", and the view draws it in primary ink.
    let level: ClusterMetricLevel?
    /// `host_table` justifies `Host` left and all eight numeric columns right.
    let leadingAligned: Bool

    var title: String { id }
}

/// The host table a CLI-shaped surface draws.
enum CLIMonitorTableReading {
    /// Host rows a `systemMedium` tile draws, header row excluded.
    ///
    /// Four is not a new decision: `FleetProjection.text(for: .medium)` has
    /// always capped the medium surface at four node rows, so this is the same
    /// vertical budget for the same size — only the content of a row changed.
    /// A surface with more room than that (`systemLarge`, `systemExtraLarge`,
    /// the macOS desktop) passes its own `limit`; the hosts beyond the cap are
    /// counted by `hiddenRowCount` rather than dropped in silence, and the
    /// tile says `+n`.
    static let mediumRowLimit = 4

    /// Point sizes for a nine-column table in a ~320pt tile. Every one of them
    /// goes through `WidgetStyle.font(size:)`, so the operator's own text
    /// scale is applied on top — these are the size of the ROLE, not of the
    /// text the operator sees.
    static let headerPointSize: CGFloat = 6.5
    static let cellPointSize: CGFloat = 8
    /// Column gap. The CLI separates columns with the table's own padding; a
    /// tile has no padding to spare, and every glyph is drawn
    /// `monospacedDigit()` so the columns line up without one.
    static let columnSpacing: CGFloat = 3
    static let rowSpacing: CGFloat = 1

    // MARK: Columns

    /// A column this surface knows how to paint: the CLI title it belongs to,
    /// the verdict to paint it with, and — for one column only — the text to
    /// draw instead of the CLI's cell.
    private struct ColumnSpec {
        let title: String
        let leadingAligned: Bool
        let level: (CLIMonitorHostRow) -> ClusterMetricLevel?
        /// `nil` means "draw `CLIMonitorHostRow.cells[index]`", which is right
        /// for the eight numeric columns. `Host` is the one column the CLI
        /// prints as the wire host, and a tile that draws a node's address is
        /// drawing the wrong thing (§7): the view-model says so and hands over
        /// `displayLabel` instead, so this column takes that.
        let override: ((CLIMonitorHostRow) -> String)?
    }

    /// The nine columns of `host_table()`, keyed by TITLE rather than by
    /// position: `host_table`'s own list is the authority for the order and for
    /// the header text, so a reorder there re-pairs these verdicts correctly,
    /// while a rename makes the column disappear — which
    /// `cluster_widget_check.sh` catches by count rather than letting a renamed
    /// column quietly keep a threshold it no longer belongs to.
    ///
    /// Levels are the view-model's own thresholds — the CLI colours nothing in
    /// this table, and `ClusterMetricLevel`'s doc comment says whose rules those
    /// are — so a verdict is READ from the view-model and never re-derived here.
    /// `nil` is a considered answer, not a gap: `host_table` prints Jobs, Swap
    /// and GPU% in the default style, and a tile that colours them is no longer
    /// the CLI's table.
    private static let columnSpecs: [ColumnSpec] = [
        ColumnSpec(title: "Host", leadingAligned: true,
                   level: { _ in nil }, override: { $0.displayLabel }),
        ColumnSpec(title: "Jobs", leadingAligned: false, level: { _ in nil }, override: nil),
        ColumnSpec(title: "CPU%", leadingAligned: false, level: { $0.cpuLevel }, override: nil),
        ColumnSpec(title: "RAM%", leadingAligned: false, level: { $0.memLevel }, override: nil),
        ColumnSpec(title: "Swap%", leadingAligned: false, level: { _ in nil }, override: nil),
        ColumnSpec(title: "GPU%", leadingAligned: false, level: { _ in nil }, override: nil),
        ColumnSpec(title: "CPU Temp", leadingAligned: false,
                   level: { $0.cpuTempLevel }, override: nil),
        ColumnSpec(title: "GPU Temp", leadingAligned: false,
                   level: { $0.gpuTempLevel }, override: nil),
        ColumnSpec(title: "GPU Power", leadingAligned: false,
                   level: { $0.gpuPowerLevel }, override: nil),
    ]

    /// The columns to draw, in `host_table`'s order: its own titles, each with
    /// the alignment and verdict this file pairs with that title. A title with
    /// no spec is skipped rather than drawn unaligned and unlabelled — and the
    /// gate asserts this array's count against
    /// `CLIMonitorHostRow.columns.count`, so a skip cannot stay quiet.
    static var drawnColumns: [CLIMonitorHostColumn] {
        CLIMonitorHostRow.columns.enumerated().compactMap { index, title in
            guard let spec = columnSpecs.first(where: { $0.title == title }) else { return nil }
            return CLIMonitorHostColumn(index: index,
                                        title: title,
                                        leadingAligned: spec.leadingAligned)
        }
    }

    /// One host's cells, in drawn order, each with the verdict for its column.
    ///
    /// Text is positional (`row.cells[i]` beside `columns[i]`) exactly as
    /// `host_table` builds a row — with the one §7 exception the specs declare:
    /// the Host column draws `displayLabel`, because the CLI's own value for it
    /// is the node's address and a tile is not allowed to print one.
    static func cells(for row: CLIMonitorHostRow) -> [CLIMonitorHostCell] {
        let cells = row.cells
        return drawnColumns.compactMap { column in
            guard cells.indices.contains(column.index) else { return nil }
            guard let spec = columnSpecs.first(where: { $0.title == column.title }) else { return nil }
            return CLIMonitorHostCell(id: column.title,
                                      text: spec.override?(row) ?? cells[column.index],
                                      level: spec.level(row),
                                      leadingAligned: column.leadingAligned)
        }
    }

    /// The rows this tile draws, capped at `limit`.
    static func visibleRows(from rows: [CLIMonitorHostRow],
                            limit: Int = mediumRowLimit) -> [CLIMonitorHostRow] {
        guard limit > 0 else { return [] }
        return Array(rows.prefix(limit))
    }

    /// Hosts the route listed that the tile has no room for. The tile prints
    /// this as `+n`, because a table that silently stops at four hosts reads as
    /// "the fleet is four hosts".
    static func hiddenRowCount(from rows: [CLIMonitorHostRow],
                               limit: Int = mediumRowLimit) -> Int {
        max(0, rows.count - max(0, limit))
    }

    /// The VoiceOver sentence for one host: the display label, then every
    /// column as `Title value`. Built from the same cells a sighted operator
    /// reads, so the two can never disagree about what the table says.
    static func accessibilityLabel(for row: CLIMonitorHostRow) -> String {
        let pairs = cells(for: row).map { "\($0.title) \($0.text)" }
        return ([row.displayLabel] + pairs).joined(separator: ", ")
    }
}

/// A column the surface draws, before any row's text is attached to it.
struct CLIMonitorHostColumn: Equatable, Identifiable {
    /// Position in `CLIMonitorHostRow.columns` / `CLIMonitorHostRow.cells`.
    let index: Int
    let title: String
    let leadingAligned: Bool

    var id: String { title }
}
