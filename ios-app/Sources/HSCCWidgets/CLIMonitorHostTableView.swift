import SwiftUI

// ===========================================================================
// `hscc cluster monitor`'s host table, drawn.
//
// The "looks like the CLI" surface (t_2d1c12dc): a header row of the CLI's own
// column names, then one row per host carrying the CLI's own cells. The tile
// does not restate, abbreviate or re-round anything — see
// `CLIMonitorTableReading.swift` for why the rules live there and this file is
// kept too thin to hold one.
//
// THE ONE DECISION THIS FILE OWNS: which slot of the operator's palette a
/// `ClusterMetricLevel` paints. Everything else — the columns, their order,
// their alignment, the cell text, the row cap — is read from the view-model or
// from the reading layer, and a second colour rule here would be a second
// thing to keep in agreement with the CLI.
//
// WHY `.ok` IS NOT GREEN. The CLI prints every cell of `host_table` in the
// default style; its only threshold anywhere is `_load_style` on the serving
// `Run` column. So an ok temperature gets the operator's primary ink, exactly
// like the terminal, and only `warn`/`critical` borrow the palette. Painting
// every calm number green would be the loudest way to stop looking like the
// CLI, and it would make the one number that matters look like the eight that
// do not. `unknown` (a metric the node could not read) goes secondary, which is
// the same treatment the `-` gets in the terminal's `dim` cells.
//
// Everything visible is style-driven: `WidgetStyle.font` for size/weight/design
// and the operator's text scale, `WidgetStyle.spacing` for the column gap, and
// the style's own colours for text, hairline and thresholds, so an operator
// with a custom palette, a rounded design, compact density or a 1.4× text scale
// gets a table in THEIR style rather than a screenshot of someone else's.
// ===========================================================================

/// The host table: `CLIMonitorTableReading`'s columns, `CLIMonitorHostRow`'s
/// cells, painted with the operator's palette.
struct CLIMonitorHostTableView: View {
    @Environment(\.widgetStyle) private var style

    /// Every host the route listed. The view caps the rows itself so the cap
    /// and the `+n` cannot be computed twice and disagree.
    let rows: [CLIMonitorHostRow]
    /// Rows the surface has room for. `systemMedium`'s budget is the reading
    /// layer's; a taller surface passes its own.
    var rowLimit: Int = CLIMonitorTableReading.mediumRowLimit

    private var visible: [CLIMonitorHostRow] {
        CLIMonitorTableReading.visibleRows(from: rows, limit: rowLimit)
    }

    var body: some View {
        let columns = CLIMonitorTableReading.drawnColumns
        VStack(alignment: .leading,
               spacing: CLIMonitorTableReading.rowSpacing) {
            Grid(alignment: .trailing,
                 horizontalSpacing: style.spacing(CLIMonitorTableReading.columnSpacing),
                 verticalSpacing: CLIMonitorTableReading.rowSpacing) {
                GridRow {
                    ForEach(columns) { column in
                        Text(column.title)
                            .font(style.font(size: CLIMonitorTableReading.headerPointSize,
                                             weight: .semibold))
                            .foregroundColor(style.secondaryTextColor)
                            .lineLimit(1)
                            .minimumScaleFactor(0.5)
                            .gridColumnAlignment(column.leadingAligned ? .leading : .trailing)
                            .accessibilityHidden(true)
                    }
                }
                GridRow {
                    Rectangle()
                        .fill(style.hairlineColor)
                        .frame(height: 1)
                        .gridCellColumns(max(1, columns.count))
                        .accessibilityHidden(true)
                }
                ForEach(visible) { row in
                    GridRow {
                        ForEach(CLIMonitorTableReading.cells(for: row)) { cell in
                            Text(cell.text)
                                .font(style.font(size: CLIMonitorTableReading.cellPointSize,
                                                 weight: .medium)
                                        .monospacedDigit())
                                .foregroundColor(ink(for: cell.level))
                                .lineLimit(1)
                                .minimumScaleFactor(0.5)
                                .gridColumnAlignment(cell.leadingAligned ? .leading : .trailing)
                        }
                    }
                }
            }
            if let note = truncatedNote {
                Text(note)
                    .font(style.font(size: CLIMonitorTableReading.headerPointSize))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilitySummary)
    }

    /// `+n` when the tile had to leave hosts out. A table that stops at four
    /// rows without saying so reads as "the fleet is four hosts", which is the
    /// false zero §6 rule 1 refuses; the count itself is the route's, and the
    /// headline tally above it is the CLI's.
    private var truncatedNote: String? {
        let hidden = CLIMonitorTableReading.hiddenRowCount(from: rows, limit: rowLimit)
        guard hidden > 0 else { return nil }
        return "+\(hidden) more"
    }

    /// One sentence for the whole table: every drawn row's own label. Built
    /// from the cells, so VoiceOver and the eyes read the same strings.
    private var accessibilitySummary: String {
        visible.map { CLIMonitorTableReading.accessibilityLabel(for: $0) }
            .joined(separator: "; ")
    }

    /// The one mapping this file owns. `nil` — a column nobody thresholds — is
    /// primary ink, never the "unreadable" grey.
    private func ink(for level: ClusterMetricLevel?) -> Color {
        switch level ?? .ok {
        case .ok, .dim: return style.primaryTextColor
        case .warn: return style.warnColor
        case .critical: return style.badColor
        case .unknown: return style.secondaryTextColor
        }
    }
}
