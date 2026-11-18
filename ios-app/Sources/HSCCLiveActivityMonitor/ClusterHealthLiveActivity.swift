import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for the
/// cluster-health Live Activity.
///
/// Hosted by the `HSCCLiveActivityMonitor` app-extension target beside the
/// cluster-monitor and running-cards activities (see
/// `HSCCLiveActivityMonitorBundle`): one extension hosts several Live Activity
/// types, each with its own `ActivityConfiguration`, which is why the
/// extension's `@main` is a `WidgetBundle` rather than a single `Widget`.
///
/// Live Activities are PUSH-ONLY — this extension never fetches. It renders
/// whatever the app last pushed into `ClusterHealthActivityAttributes.ContentState`
/// (the serving state from `/v1/autodown/status` and the node tally from
/// `/v1/cluster/monitor`, projected to the reduced `ClusterHealthSignals`) and
/// ages honestly via the pushed `updatedAt`.
///
/// Dynamic Island:
///   * compact leading  — the fleet glyph, coloured by the reading.
///   * compact trailing — the tally (`!` for a blind read, `—` for no signal).
///   * expanded         — headline + failure line, then the down nodes.
///   * minimal          — the glyph alone (the island is minimized).
///
/// Lock Screen — the row: headline, age, the down-node labels or the one
/// sentence that replaces them.
///
/// Every word, number and tone comes from `ClusterHealthReading` (in
/// `Sources/Shared/ClusterHealthActivity.swift`), never from a local decision,
/// so the gate that executes those rules covers what this file draws: a blind
/// read renders its failure label, never `0/0 up` and never a mint "Serving".
struct ClusterHealthLiveActivity: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityClusterHealth)
        return ActivityConfiguration(for: ClusterHealthActivityAttributes.self) { context in
            LockScreenClusterHealthView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            // The reading is decided once, then handed to every region, so the
            // compact and expanded forms of one state can never disagree.
            let signals = context.state.signals
            let tone = ClusterHealthReading.tone(signals)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    ClusterHealthGlyph(tone: tone).widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ClusterHealthReading.headline(signals))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                        if let failureLabel = signals.failureLabel, !failureLabel.isEmpty {
                            // Stale is not quiet: the tally below may be
                            // last-known, and this says why it is not new.
                            Text("stale · \(failureLabel)")
                                .font(style.font(.caption2))
                                .foregroundColor(style.warnColor)
                                .lineLimit(1)
                        }
                        if let feed = ClusterHealthReading.feedText(model: signals.modelLabel,
                                                                    genTps: signals.genTps) {
                            Text(feed)
                                .font(style.font(.caption2))
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                    }
                    .widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(ClusterHealthReading.pushedAgeText(updatedAt: context.state.updatedAt,
                                                            now: Date()))
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    ClusterHealthDetail(signals: signals,
                                        limit: ClusterHealthActivityAttributes.downNodeLimit,
                                        labelFont: style.font(size: 10, weight: .semibold)).widgetStyle(style)
                }
            } compactLeading: {
                ClusterHealthGlyph(tone: tone).widgetStyle(style)
            } compactTrailing: {
                Text(ClusterHealthReading.compactTrailing(signals))
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(ClusterHealthToneColour.color(tone, style: style))
            } minimal: {
                ClusterHealthGlyph(tone: tone).widgetStyle(style)
            }
        }
    }
}

// MARK: - Shared rendering pieces

/// The activity's glyph: the fleet symbol, coloured by what the last read was.
/// Colour carries the state here so the compact regions stay text free.
struct ClusterHealthGlyph: View {
    @Environment(\.widgetStyle) private var style
    let tone: ClusterHealthTone

    var body: some View {
        Image(systemName: "square.stack.3d.up").widgetIcon(style)
            .foregroundColor(ClusterHealthToneColour.color(tone, style: style))
    }
}

/// The one place a `ClusterHealthTone` becomes a `Color`. The tone itself is
/// decided in the Foundation-only slice above (a `Color` there could not be
/// compiled by the macOS gate); mapping it once here is what keeps the Island's
/// compact regions and the Lock Screen header from inventing their own green.
enum ClusterHealthToneColour {
    static func color(_ tone: ClusterHealthTone, style: WidgetStyle) -> Color {
        switch tone {
        case .ok: return style.okColor
        case .warn: return style.warnColor
        case .bad: return style.badColor
        case .neutral: return style.secondaryTextColor
        }
    }
}

/// The detail region: the down nodes by name, or the one sentence that stands
/// in for them. Names come from the pushed state and are clipped to `limit`,
/// because a pushed ContentState is untrusted input to a fixed-height region.
struct ClusterHealthDetail: View {
    @Environment(\.widgetStyle) private var style
    let signals: ClusterHealthSignals
    let limit: Int
    let labelFont: Font

    var body: some View {
        let names = ClusterHealthReading.downNodeList(signals.downNodes, limit: limit)
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(ClusterHealthReading.tallyText(up: signals.nodesUp, total: signals.nodesTotal))
                    .font(labelFont)
                    .foregroundColor(ClusterHealthToneColour.color(ClusterHealthReading.tone(signals), style: style))
                Text(ClusterHealthReading.downText(down: signals.nodesDown))
                    .font(labelFont)
                    .foregroundColor(style.secondaryTextColor)
                Spacer(minLength: 4)
            }
            if !names.isEmpty {
                // Which node is down is the point of a health surface — a tally
                // alone cannot be acted on. `.NNN` tails only (§7); the pushed
                // state cannot carry an address in the first place.
                HStack(spacing: 6) {
                    ForEach(names, id: \.self) { name in
                        HStack(spacing: 3) {
                            Circle()
                                .fill(style.badColor)
                                .frame(width: 6, height: 6)
                            Text(name)
                                .font(labelFont)
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                    }
                    if let more = ClusterHealthReading.moreText(shown: names.count,
                                                                down: signals.nodesDown) {
                        Text(more)
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    }
                    Spacer(minLength: 4)
                }
            }
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the cluster-health activity.
struct LockScreenClusterHealthView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<ClusterHealthActivityAttributes>

    var body: some View {
        let signals = context.state.signals
        VStack(alignment: .leading, spacing: 8) {
            // Header: glyph + headline + age.
            HStack(spacing: 8) {
                ClusterHealthGlyph(tone: ClusterHealthReading.tone(signals))
                Text(ClusterHealthReading.headline(signals))
                    .font(style.font(.headline))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                Spacer()
                Text(ClusterHealthReading.pushedAgeText(updatedAt: context.state.updatedAt,
                                                        now: Date()))
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            .widgetHeader(style)

            ClusterHealthDetail(signals: signals,
                                limit: ClusterHealthActivityAttributes.downNodeLimit,
                                labelFont: style.font(size: 11, weight: .semibold))

            if let feed = ClusterHealthReading.feedText(model: signals.modelLabel,
                                                        genTps: signals.genTps) {
                Text(feed)
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }

            if let downLine = emptyLine(signals) {
                Text(downLine)
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
        .padding()
    }

    /// A blind read with nothing to fall back on says so; a read that answered
    /// and found every node healthy says the fleet is up. Those two must never
    /// share a line — that collapse is exactly how a dead cluster reads as fine.
    private func emptyLine(_ signals: ClusterHealthSignals) -> String? {
        if signals.isEmpty {
            if let failureLabel = signals.failureLabel, !failureLabel.isEmpty {
                return "\(failureLabel) — no cluster reading to show"
            }
            return "No cluster reading yet"
        }
        if signals.nodesDown == 0 && signals.downNodes.isEmpty {
            return "All nodes answering"
        }
        return nil
    }
}
