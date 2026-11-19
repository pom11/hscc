import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// ClusterMonitorTableViews.swift — the serving table, the extra-large detail
// block, and the arrangement of those two with the host table, for BOTH
// cluster/monitor widgets.
//
// These two widgets show the same fleet at the same sizes, so they draw the
// same blocks: two bodies each, one shared implementation. Everything numeric
// here is a string the view-model already produced (`CLIMonitorServingRow`,
// `ClusterMonitorHostDetail`) and the columns and rows come from
// `ClusterMonitorTableLayout` — a view here selects and paints, it never
// formats. That is what keeps a widget cell and a terminal cell from rounding
// differently, and it is why no number is composed in this file.
//
// The HOST table is not here either: `CLIMonitorHostTableView` draws it at
// every size, and this file hands it its row budget.
//
// Every colour and font comes from `WidgetStyle`, so the operator's palette,
// design, weight, text scale and density apply to the new elements exactly as
// they do to the tiles that already shipped.
// ---------------------------------------------------------------------------

extension ClusterMetricLevel {
    /// THE app's one `ClusterMetricLevel` -> operator-palette mapping, and the
    /// only one: `CLIMonitorHostTableView.ink(for:)` delegates here rather than
    /// keeping its own switch, so the host table and the serving table cannot
    /// drift apart on what `warn` looks like.
    ///
    /// Deliberately NOT "green means fine": at these sizes a table where every
    /// ordinary reading is green is noise, and it makes the alarming cells stop
    /// being alarming. `ok` therefore paints in the operator's text colour and
    /// only a crossed threshold carries hue — the same rule the CLI prints by,
    /// which styles serving load and never a temperature (`_load_style` is its
    /// only style function). `dim`/`unknown` recede: a dash or an idle reading
    /// must not read as a value.
    ///
    /// Named `cliCellColor`, not `color`, because the SECOND rule on this type
    /// is a different surface with a different job: `ClusterSmallGridView.swift`
    /// owns `color(_:)` for the glance grid, where a calm cell IS the headline
    /// and the palette's ok colour is the point. Two names, two documented
    /// rules; one unnamed rule would have quietly repainted the small tile (or
    /// turned the CLI table green) depending on which file the compiler picked.
    func cliCellColor(_ style: WidgetStyle) -> Color {
        switch self {
        case .ok: return style.primaryTextColor
        case .warn: return style.warnColor
        case .critical: return style.badColor
        case .dim, .unknown: return style.secondaryTextColor
        }
    }
}

// The host table is NOT drawn in this file. `CLIMonitorHostTableView` (main,
// t_2d1c12dc) is the only thing that draws `host_table()`'s nine columns, on
// every surface, from `CLIMonitorTableReading.drawnColumns`; large and
// extra-large pass it a taller `rowLimit` instead of growing their own table
// with their own abbreviated header words. This file owns the serving table and
// the extra-large detail block — the two blocks main's reading layer has no
// equivalent for — and the arrangement of the three.

// MARK: - Serving table

/// The serving table: Node, Port, Model, Run, Wait, KV%, In, Out — plus the
/// per-model `total (n)` rows, which carry a hairline and the operator's accent
/// instead of a bold-only cue, so a subtotal is distinguishable in a custom
/// palette too. The `Gen t/s` column is the one the widget's width cannot pay
/// for, and it is exactly the column `cluster_live` drops first in its narrow
/// render (pinned by the decode gate).
struct MonitorServingTableView: View {
    @Environment(\.widgetStyle) private var style

    let rows: [CLIMonitorServingRow]
    let columns: [ClusterMonitorServingColumn]
    let compactHeaders: Bool

    var body: some View {
        Grid(alignment: .trailing,
             horizontalSpacing: style.spacing(5),
             verticalSpacing: style.spacing(2)) {
            GridRow {
                ForEach(Array(columns.enumerated()), id: \.offset) { index, column in
                    Text(column.header)
                        .font(style.font(size: compactHeaders ? 8 : 9, weight: .semibold))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)
                        .gridColumnAlignment(index == 0 ? .leading
                                            : (column == .model ? .leading : .trailing))
                }
            }
            ForEach(rows) { row in
                GridRow {
                    ForEach(Array(columns.enumerated()), id: \.offset) { index, column in
                        MonitorTableCell(text: column.cell(in: row),
                                         color: column.level(in: row).map { $0.cliCellColor(style) }
                                             ?? style.primaryTextColor,
                                         leading: index == 0 || column == .model,
                                         emphasised: row.isTotalRow,
                                         size: compactHeaders ? 9 : 10)
                    }
                }
                if row.isTotalRow {
                    // A subtotal is a boundary in the table, not just bolder
                    // text — the hairline is what makes `total (2)` read as a
                    // total when the whole table is six rows tall.
                    GridRow {
                        Color.clear.frame(height: 0)
                            .gridCellColumns(columns.count)
                            .overlay(alignment: .top) {
                                Rectangle()
                                    .fill(style.hairlineColor)
                                    .frame(height: 0.5)
                            }
                    }
                }
            }
        }
    }
}

// MARK: - Extra-large detail block

/// The six fields the host table has no column for, one line per host: VRAM
/// used/total, GPU clock, net recv/send per second, uptime, fan %, load 1/5/15.
/// Extra-large only, because at that width they are legible and nowhere else.
///
/// None of these is a `cluster monitor` column — `cluster_live.py` prints no
/// load average, no VRAM, no clock, no net, no uptime and no fan anywhere — so
/// they are the route's fields, formatted by the app and labelled with what
/// they are. `cluster_widget_check.sh`'s ban on a load average therefore
/// covers the sizes that mirror the CLI's own output, not this block, and says
/// so where it is declared.
struct MonitorHostDetailView: View {
    @Environment(\.widgetStyle) private var style

    let details: [ClusterMonitorHostDetail]

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(3)) {
            MonitorTableCaption(title: "Host detail")
            // No cap here: the projector already clipped to the plan's rows,
            // and a view that clips too would be a second owner of the rule.
            ForEach(details) { detail in
                detailLine(detail)
                    .accessibilityLabel(detail.accessibilityLine)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One host: its label, then the six fields. Each field is its own pair of
    /// `Text`s rather than a concatenated run, because `Text + Text` is
    /// deprecated on this SDK — and a per-field view is also what lets a field
    /// that crossed a threshold be the only coloured thing on the line.
    private func detailLine(_ detail: ClusterMonitorHostDetail) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: style.spacing(6)) {
            Text(detail.label)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .fixedSize()
            ForEach(Array(detail.fields.enumerated()), id: \.offset) { _, field in
                fieldPair(field)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func fieldPair(_ field: ClusterMonitorField) -> some View {
        let valueColor: Color = field.level.map { $0.cliCellColor(style) } ?? style.primaryTextColor
        return HStack(alignment: .firstTextBaseline, spacing: 2) {
            Text(field.label)
                .font(style.font(size: 7, weight: .medium))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(1)
                .fixedSize()
            Text(field.value)
                .font(style.font(size: 9, weight: .regular))
                .monospacedDigit()
                .foregroundColor(valueColor)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .fixedSize()
        }
    }
}

// MARK: - Pieces

/// A table caption: the block's name, in the operator's secondary colour. The
/// tables have no other title, and "Run Wait KV%" without a caption is a
/// puzzle — the CLI has a column-header row for it, this is the compromise the
/// width allows.
struct MonitorTableCaption: View {
    @Environment(\.widgetStyle) private var style

    let title: String
    var emphasized = false

    var body: some View {
        Text(title)
            .font(style.font(size: 8, weight: .semibold))
            .foregroundColor(emphasized ? style.accentColor : style.secondaryTextColor)
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
    }
}

/// One cell. Monospaced digits so a column of numbers lines up — a table whose
/// digits jitter is not a table.
private struct MonitorTableCell: View {
    @Environment(\.widgetStyle) private var style

    let text: String
    let color: Color
    var leading = false
    var emphasised = false
    var size: CGFloat = 10

    var body: some View {
        Text(text)
            .font(style.font(size: size, weight: emphasised ? .semibold : .regular))
            .monospacedDigit()
            .foregroundColor(color)
            .lineLimit(1)
            .minimumScaleFactor(0.6)
            .frame(maxWidth: .infinity, alignment: leading ? .leading : .trailing)
    }
}

/// The `+N more` line under a clipped table: what the width gave up, in the
/// operator's secondary colour, so a clipped fleet never reads as a smaller
/// fleet.
struct MonitorTableOverflow: View {
    @Environment(\.widgetStyle) private var style

    let text: String

    var body: some View {
        Text(text)
            .font(style.font(size: 8, weight: .regular))
            .foregroundColor(style.secondaryTextColor)
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The blocks a large or extra-large monitor surface draws, or nothing when
/// there is no table to draw. Both widgets go through here so the column/row
/// selection and the arrangement live in exactly one place per size: stacked on
/// `systemLarge` (a phone tile is tall, not wide) and side by side on
/// `systemExtraLarge`, where the CLI's two stacked tables fit as two columns.
///
/// `hostRows` comes from the CALLER (`entry.cliHostRows ?? []`) rather than
/// being re-derived here, because `cliHostRows` is the one predicate that says
/// whether numbers may be on this surface at all. A block that reached
/// `entry.monitor` itself would be a second definition of that rule, and the
/// two would drift.
struct MonitorTablesBlock: View {
    @Environment(\.widgetStyle) private var style

    let tables: ClusterMonitorTables
    let hostRows: [CLIMonitorHostRow]
    let surface: FleetSurface

    var body: some View {
        if let plan = ClusterMonitorTableLayout.plan(for: surface) {
            if plan.surface == .extraLarge {
                HStack(alignment: .top, spacing: style.spacing(12)) {
                    hostBlock(plan)
                    Rectangle()
                        .fill(style.hairlineColor)
                        .frame(width: 0.5)
                    MonitorServingColumn(tables: tables, plan: plan)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .contain)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(6)) {
                    hostBlock(plan)
                    MonitorServingColumn(tables: tables, plan: plan)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityElement(children: .contain)
            }
        }
    }

    /// The host half: main's table, given this surface's row budget, plus —
    /// where the plan asks — the per-host detail block that only extra-large
    /// has room for. Not a `View` of its own: a second host-table component is
    /// exactly the thing this file used to get wrong.
    @ViewBuilder private func hostBlock(_ plan: ClusterMonitorTablePlan) -> some View {
        VStack(alignment: .leading, spacing: style.spacing(3)) {
            CLIMonitorHostTableView(rows: hostRows, rowLimit: plan.hostRowLimit)
            if plan.showsHostDetail {
                MonitorHostDetailView(
                    details: ClusterMonitorTableLayout.visibleDetails(tables.details, plan: plan))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The serving table with its caption, or nothing when the route sent no
/// serving section at all — silence, not an empty table of zeroes.
struct MonitorServingColumn: View {
    @Environment(\.widgetStyle) private var style

    let tables: ClusterMonitorTables
    let plan: ClusterMonitorTablePlan

    var body: some View {
        let rows = ClusterMonitorTableLayout.visibleServingRows(tables.servingRows, plan: plan)
        if tables.hasServingSection {
            VStack(alignment: .leading, spacing: style.spacing(3)) {
                MonitorTableCaption(title: plan.compactHeaders ? "Serving" : "Serving engines")
                MonitorServingTableView(rows: rows,
                                        columns: plan.servingColumns,
                                        compactHeaders: plan.compactHeaders)
                if let more = ClusterMonitorTableLayout.overflowLine(shown: rows.count,
                                                                    total: tables.servingRows.count) {
                    MonitorTableOverflow(text: more)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
