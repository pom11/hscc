import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// The dense systemSmall tile (t_9e110d88), shared by the cluster and monitor
// widgets — both surface the same `/v1/cluster/monitor` rows, so both draw the
// same grid rather than growing a second layout that can disagree.
//
// Every number on this surface arrives already formatted by
// `ClusterSmallGrid`/`ClusterMonitorViewModel` (which formats the way
// `hscc cluster monitor` does), so this file composes LAYOUT AND COLOUR and
// nothing else. That split is what makes the tile's honesty testable: the
// decode harness executes the model and asserts the cell strings, and this file
// cannot invent a rounding, a unit or a zero without going red there.
//
// STYLE RULES THIS FILE MUST KEEP (widget_style_check.sh enforces the file-wide
// half; the two toggles are wired here rather than at the call site so a future
// family cannot forget them):
//   * no literal colour constructors and no hard-coded system fonts — fonts and
//     colours come from the stored `WidgetStyle` the operator edits in Settings
//     (that gate also refuses this file's own comments naming those patterns, so
//     the rule is stated here without spelling them);
//   * threshold colours come from `ClusterMetricLevel`, which the view-model
//     produced, so a temperature and a wattage are painted by declared bands and
//     never by a value this file compared itself;
//   * the header obeys `showHeader` and the icon obeys `showIcons`.
//
// The Lock Screen accessories are NOT drawn here: they are system-tinted and
// have one line, so they keep their own bodies in the widget's view file.
// ---------------------------------------------------------------------------

extension ClusterMetricLevel {
    /// The operator's palette, addressed by the view-model's verdict. `.dim` and
    /// `.unknown` share the secondary ink on purpose: `.dim` is the CLI's word
    /// for "measured, and idle" and `.unknown` is "not told", and the two are
    /// already distinguishable in the grid by what they print (`0` versus `-`).
    /// Painting an absent metric like an idle one would be the lie; painting them
    /// the same colour while printing different text is not.
    func color(_ style: WidgetStyle) -> Color {
        switch self {
        case .ok: return style.okColor
        case .warn: return style.warnColor
        case .critical: return style.badColor
        case .dim, .unknown: return style.secondaryTextColor
        }
    }
}

/// Up to four hosts x four CLI columns, plus the serving totals: the densest
/// legible thing the app draws on a Home Screen small tile.
struct ClusterSmallGridView: View {
    @Environment(\.widgetStyle) private var style

    let tile: ClusterSmallGrid.Tile
    /// The surface's identity line. The two widgets already differ here (a
    /// different glyph and word), so the grid takes them instead of guessing
    /// which widget it is rendering.
    let headerIcon: String
    let headerWord: String
    /// The host tally the entry's existing projection produced ("3/4"), and the
    /// colour its existing state rule produced. This tile draws them; it does
    /// not decide them.
    let tally: String?
    let tallyColor: Color
    /// The age / degrade sentence the entry's text model already owns.
    let footer: String?

    /// The GPU column is a fixed track so the mini bar under the number has a
    /// width to be a fraction of. Scaled, because the digits beside it obey the
    /// operator's text scale too.
    private var gpuTrackWidth: CGFloat { style.scaled(26) }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(3)) {
            header
            grid
            totals
            Spacer(minLength: 0)
            if let footer {
                Text(footer)
                    .font(style.font(size: 7))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
        }
        // One element, one sentence, composed from the cells that were drawn —
        // a VoiceOver listener gets "node-3: 96, 78 C, 87.1 C, 71.06 W" rather
        // than twenty fragments, and the sentence cannot drift from the grid.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityLine)
    }

    private var header: some View {
        HStack(spacing: 4) {
            Image(systemName: headerIcon).widgetIcon(style)
                .font(style.font(size: 8))
                .foregroundColor(style.secondaryTextColor)
            Text(headerWord)
                .font(style.font(size: 8, weight: .semibold))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Spacer(minLength: 2)
            if let tally {
                Text(tally)
                    .font(style.font(size: 13, weight: .bold))
                    .foregroundColor(tallyColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
        }
        .widgetHeader(style)
    }

    /// One CLI column header row and up to `ClusterSmallGrid.maxHosts` host
    /// rows. The header labels come from the view-model's own column list, so
    /// this tile has exactly one vocabulary and a renamed column goes red in the
    /// decode gate rather than mislabelling a value here.
    private var grid: some View {
        Grid(alignment: .trailing,
             horizontalSpacing: style.spacing(5),
             verticalSpacing: style.spacing(1)) {
            GridRow {
                Text(ClusterSmallGrid.hostColumnLabel)
                    .gridColumnAlignment(.leading)
                    .font(style.font(size: 6.5, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
                ForEach(ClusterSmallGrid.Column.allCases) { column in
                    Text(column.cliColumn)
                        .gridColumnAlignment(.trailing)
                        .font(style.font(size: 6.5, weight: .semibold))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                        .minimumScaleFactor(0.45)
                }
            }
            ForEach(tile.rows) { row in
                GridRow {
                    Text(row.label)
                        .font(style.font(size: 8, weight: .semibold))
                        // A host that never sampled is down, and the row says so
                        // in the same colour the tile already uses for that.
                        .foregroundColor(row.isDown ? style.badColor : style.primaryTextColor)
                        .lineLimit(1)
                        .minimumScaleFactor(0.55)
                    ForEach(row.cells) { cell in
                        cellView(cell)
                    }
                }
            }
        }
    }

    /// One cell: the CLI's text, painted by the view-model's verdict. The GPU
    /// column carries a bar under the digits, because a percentage is a shape
    /// before it is a number and the tile has the width for both.
    private func cellView(_ cell: ClusterSmallGrid.Cell) -> some View {
        Text(cell.text)
            .font(style.font(size: 9, weight: .semibold))
            .foregroundColor(cell.level.color(style))
            .lineLimit(1)
            .minimumScaleFactor(0.45)
            .frame(width: cell.column == .gpuUtil ? gpuTrackWidth : nil, alignment: .trailing)
            .overlay(alignment: .bottom) {
                if let fraction = cell.fraction {
                    Capsule()
                        .fill(cell.level.color(style))
                        .frame(width: gpuTrackWidth * CGFloat(fraction),
                               height: style.scaled(2))
                }
            }
    }

    /// The serving totals the CLI's `total (n)` rows add up to. The CLI styles a
    /// whole serving row by its load, so both numbers take the row's verdict
    /// rather than each inventing its own.
    private var totals: some View {
        HStack(spacing: style.spacing(6)) {
            totalsItem(ClusterSmallGrid.runColumnLabel,
                       ClusterSmallGrid.countText(tile.totals.running))
            totalsItem(ClusterSmallGrid.waitColumnLabel,
                       ClusterSmallGrid.countText(tile.totals.waiting))
            // The number of per-model `total (n)` rows the two numbers above were
            // summed across — the tile's own sentence about its own arithmetic.
            if let models = tile.totals.models, models > 0 {
                Text("\(models) models")
                    .font(style.font(size: 7))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
            Spacer(minLength: 0)
            // Four rows is what fits, not what the fleet is: the tile counts
            // what it dropped instead of reading as the whole cluster.
            if tile.hiddenHosts > 0 {
                Text("+\(tile.hiddenHosts)")
                    .font(style.font(size: 8, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    }

    private func totalsItem(_ label: String, _ value: String) -> some View {
        HStack(spacing: 2) {
            Text(label)
                .font(style.font(size: 7, weight: .medium))
                .foregroundColor(style.secondaryTextColor)
            Text(value)
                .font(style.font(size: 11, weight: .bold))
                .foregroundColor(tile.totals.level.color(style))
                .lineLimit(1)
                .minimumScaleFactor(0.6)
        }
    }

    private var accessibilityLine: String {
        [headerWord, tally, tile.accessibilitySummary, footer]
            .compactMap { $0 }
            .joined(separator: ". ")
    }
}
