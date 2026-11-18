import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for the
/// running-cards Live Activity.
///
/// Hosted by the `HSCCLiveActivityMonitor` app-extension target beside the
/// cluster-monitor activity (see `HSCCLiveActivityMonitorBundle`): one
/// extension can host several Live Activity types, each with its own
/// `ActivityConfiguration`, which is why the extension's `@main` is a
/// `WidgetBundle` rather than a single `Widget`.
///
/// Live Activities are PUSH-ONLY — this extension never fetches. It renders
/// whatever the app last pushed into `CardsRunningActivityAttributes.ContentState`
/// (the same two kanban routes `CardsRunningWidget` reads, projected to board ·
/// card · worker · run time) and ages honestly via the pushed `updatedAt`.
///
/// Dynamic Island:
///   * compact leading  — the cards glyph, coloured by the reading.
///   * compact trailing — the count (`!` for a read that produced nothing).
///   * expanded         — headline + blocked + age, then up to `rowLimit` rows.
///   * minimal          — the glyph alone (the island is minimized).
///
/// Lock Screen — the row: headline, blocked figure, pushed age, then the rows
/// (board · card · worker · age), longest-running first.
///
/// Every number and string comes from `CardsRunningReading` (in
/// `Shared/CardsRunningActivity.swift`), never from a local decision, so the
/// gate that executes those rules covers what this file draws: a blind read
/// renders its failure label, never `0 running`.
struct CardsRunningLiveActivity: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityCards)
        return ActivityConfiguration(for: CardsRunningActivityAttributes.self) { context in
            LockScreenCardsView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            // The reading is decided once, then handed to every region, so the
            // compact and expanded forms of one state can never disagree.
            let state = context.state
            let count = CardsRunningReading.displayCount(runningCount: state.runningCount,
                                                         rowCount: state.rows.count,
                                                         rowsAreComplete: state.rowsAreComplete)
            return DynamicIsland {
                // Expanded UI.
                DynamicIslandExpandedRegion(.leading) {
                    CardsGlyph(state: state).widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(CardsRunningReading.headline(count: count,
                                                          blockedCount: state.blockedCount,
                                                          failureLabel: state.failureLabel))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                        if let failureLabel = state.failureLabel, !failureLabel.isEmpty {
                            // Stale is not quiet: the rows below are real, and
                            // this says why the newest one is not.
                            Text("stale · \(failureLabel)")
                                .font(style.font(.caption2))
                                .foregroundColor(style.warnColor)
                                .lineLimit(1)
                        }
                    }
                    .widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(CardsRunningReading.pushedAgeText(updatedAt: state.updatedAt, now: Date()))
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    CardsRowList(state.rows, rowFont: style.font(size: 10, weight: .semibold))
                }
            } compactLeading: {
                CardsGlyph(state: state).widgetStyle(style)
            } compactTrailing: {
                Text(CardsRunningReading.compactTrailing(count: count,
                                                         failureLabel: state.failureLabel))
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(CardsRunningReadingColour.state(state, style: style))
            } minimal: {
                CardsGlyph(state: state).widgetStyle(style)
            }
        }
    }
}

// MARK: - Shared rendering pieces

/// The activity's glyph: the running-cards symbol, coloured by what the last
/// read was. Colour carries the state here so the compact regions stay text
/// free — a blind read is `bad`, a stale one `warn`, a live one `ok`, a real
/// zero `neutral`.
struct CardsGlyph: View {
    @Environment(\.widgetStyle) private var style
    let state: CardsRunningActivityAttributes.ContentState

    var body: some View {
        Image(systemName: "square.stack.3d.up").widgetIcon(style)
            .foregroundColor(CardsRunningReadingColour.state(state, style: style))
    }
}

/// The colour a state earns. Kept beside the glyph so the Island's compact
/// regions and the Lock Screen header cannot disagree about what green means.
enum CardsRunningReadingColour {
    static func state(_ state: CardsRunningActivityAttributes.ContentState, style: WidgetStyle) -> Color {
        let blind = (state.failureLabel?.isEmpty == false)
        if blind { return state.rows.isEmpty ? style.badColor : style.warnColor }
        return state.rows.isEmpty ? style.secondaryTextColor : style.okColor
    }
}

/// The row list, clipped to `rowLimit` — a pushed ContentState is untrusted
/// input to a fixed-height region, so the view caps as well as the driver.
struct CardsRowList: View {
    @Environment(\.widgetStyle) private var style
    let rows: [RunningCardMetric]
    let rowFont: Font

    init(_ rows: [RunningCardMetric], rowFont: Font) {
        self.rows = rows
        self.rowFont = rowFont
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(rows.prefix(CardsRunningActivityAttributes.rowLimit)) { row in
                CardsRunningRowView(row: row, labelFont: rowFont)
            }
        }
    }
}

/// One running card: board · title · worker · age.
///
/// The card id only appears where a title is missing (it is the fallback
/// headline, never decoration), the worker is `—` when nobody was named, and
/// the age is `—` when `started_at` was missing or unparseable — a row never
/// claims a card started this instant.
struct CardsRunningRowView: View {
    @Environment(\.widgetStyle) private var style
    let row: RunningCardMetric
    let labelFont: Font

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(style.cardStatusColor(row.status ?? ""))
                .frame(width: 6, height: 6)
            if let board = row.board, !board.isEmpty {
                Text(board)
                    .font(labelFont)
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }
            Text(row.headline)
                .font(style.font(.caption))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
            Spacer(minLength: 4)
            Text(row.workerText)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(1)
            Text(row.ageText)
                .font(labelFont)
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the running-cards activity.
struct LockScreenCardsView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<CardsRunningActivityAttributes>

    var body: some View {
        let state = context.state
        let count = CardsRunningReading.displayCount(runningCount: state.runningCount,
                                                     rowCount: state.rows.count,
                                                     rowsAreComplete: state.rowsAreComplete)
        VStack(alignment: .leading, spacing: 8) {
            // Header: glyph + headline + blocked + age.
            HStack(spacing: 8) {
                CardsGlyph(state: state)
                Text(CardsRunningReading.headline(count: count,
                                                  blockedCount: state.blockedCount,
                                                  failureLabel: state.failureLabel))
                    .font(style.font(.headline))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                Spacer()
                Text(CardsRunningReading.pushedAgeText(updatedAt: state.updatedAt, now: Date()))
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            .widgetHeader(style)

            // The rows, or the one sentence that replaces them.
            if state.rows.isEmpty {
                Text(emptyLine(state))
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            } else {
                CardsRowList(state.rows, rowFont: style.font(size: 11, weight: .semibold))
            }
        }
        .padding()
    }

    /// A blind read with nothing to fall back on says so; a live read that
    /// found nothing says the fleet is idle. Those two must never share a line.
    private func emptyLine(_ state: CardsRunningActivityAttributes.ContentState) -> String {
        if let failureLabel = state.failureLabel, !failureLabel.isEmpty {
            return "\(failureLabel) — no card list to show"
        }
        return "Nothing running"
    }
}
