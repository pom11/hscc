import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for the one
/// surviving Live Activity, "Cluster watch" (A2).
///
/// It reuses the Cluster Health activity's plumbing: the attributes, the
/// projection and the launch sweep in `Sources/Shared/ClusterHealthActivity.swift`
/// are unchanged in shape, and the app-side feeder is
/// `ClusterHealthActivityDriver`. This file only redraws the four surfaces the
/// operator picked on 2026-11-20:
///
///   * Lock Screen — `CLUSTER · <model>` + as-of, four node columns
///     (`72°` over `11 · 34%`, the short name being the node's last octet),
///     then the tok/s · daemon · autodown footer.
///   * compact     — a state dot + `4/4`, and `72° · 231 t/s`.
///   * minimal     — the hottest temperature (or the tally when none was told).
///   * expanded    — the Lock Screen layout.
///
/// Live Activities are PUSH-ONLY. Every word and number comes from the pushed
/// `ClusterHealthSignals` through `ClusterHealthReading`, so a degraded read
/// still renders its failure line rather than a fresh-looking zero.
struct ClusterHealthLiveActivity: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityClusterHealth)
        return ActivityConfiguration(for: ClusterHealthActivityAttributes.self) { context in
            LockScreenClusterWatchView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            let signals = context.state.signals
            let tone = ClusterHealthReading.tone(signals)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.bottom) {
                    ClusterWatchContent(signals: signals,
                                        updatedAt: context.state.updatedAt,
                                        isStale: context.isStale)
                        .widgetStyle(style)
                }
            } compactLeading: {
                HStack(spacing: 3) {
                    Circle()
                        .fill(ClusterHealthToneColour.color(tone, style: style))
                        .frame(width: 6, height: 6)
                    Text(ClusterHealthReading.watchTally(signals))
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(ClusterHealthToneColour.color(tone, style: style))
                        .lineLimit(1)
                }
                .widgetStyle(style)
            } compactTrailing: {
                Text(ClusterHealthReading.watchTrailing(signals))
                    .font(style.font(size: 10, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
            } minimal: {
                Text(ClusterHealthReading.watchMinimal(signals))
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(ClusterWatchTempColour.color(
                        ClusterHealthReading.watchTempLevel(signals.peakGpuTempC),
                        style: style))
                    .lineLimit(1)
            }
        }
    }
}

// MARK: - Shared rendering pieces

/// The Lock Screen row and the expanded Island share ONE body, so the two can
/// never disagree about a temperature, a model name or the daemon state.
private struct ClusterWatchContent: View {
    @Environment(\.widgetStyle) private var style
    let signals: ClusterHealthSignals
    let updatedAt: Date
    let isStale: Bool

    var body: some View {
        let columns = ClusterHealthReading.nodeColumns(signals.hostLines)
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "square.stack.3d.up")
                    .widgetIcon(style)
                    .foregroundColor(ClusterHealthToneColour.color(
                        ClusterHealthReading.tone(signals), style: style))
                Text(ClusterHealthReading.watchHeader(model: signals.modelLabel))
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
                Spacer(minLength: 4)
                Text(ClusterHealthReading.asOfText(updatedAt))
                    .font(style.font(size: 10, weight: .semibold))
                    .foregroundColor(style.secondaryTextColor)
                    .monospacedDigit()
            }
            .widgetHeader(style)

            if columns.isEmpty {
                Text(signals.failureLabel ?? "No node reading")
                    .font(style.font(size: 13, weight: .semibold))
                    .foregroundColor(signals.failureLabel == nil
                                     ? style.secondaryTextColor
                                     : style.badColor)
                    .lineLimit(2)
            } else {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(columns) { column in
                        VStack(spacing: 2) {
                            Text(column.tempText)
                                .font(style.font(size: 20, weight: .bold))
                                .foregroundColor(ClusterWatchTempColour.color(column.tempLevel,
                                                                             style: style))
                                .monospacedDigit()
                                .lineLimit(1)
                            Text(column.detailText)
                                .font(style.font(size: 10, weight: .semibold))
                                .foregroundColor(column.isDown
                                                 ? style.badColor
                                                 : style.secondaryTextColor)
                                .lineLimit(1)
                        }
                        .frame(maxWidth: .infinity)
                    }
                }
            }

            Text(ClusterHealthReading.watchFooter(signals))
                .font(style.font(size: 11, weight: .semibold))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(1)

            if isStale {
                Text("stale · \(ClusterHealthReading.asOfText(updatedAt))")
                    .font(style.font(size: 10, weight: .semibold))
                    .foregroundColor(style.warnColor)
            }
        }
        .padding(.vertical, 2)
    }
}

/// The Lock Screen (and banner) presentation of the Cluster watch.
struct LockScreenClusterWatchView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<ClusterHealthActivityAttributes>

    var body: some View {
        ClusterWatchContent(signals: context.state.signals,
                            updatedAt: context.state.updatedAt,
                            isStale: context.isStale)
            .padding()
    }
}

/// The one place a `ClusterHealthTone` becomes a `Color`. The tone itself is
/// decided in the Foundation-only slice (a `Color` there could not be compiled
/// by the macOS gate); mapping it once here keeps every region agreeing.
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

/// Temperature ink for the A2 four-column row: below 70 is ok (green),
/// 70–74 warn (orange), 75+ bad (red), and an unread temperature stays grey
/// rather than claiming a cool GPU.
enum ClusterWatchTempColour {
    static func color(_ level: ClusterMetricLevel, style: WidgetStyle) -> Color {
        switch level {
        case .ok, .dim: return style.okColor
        case .warn: return style.warnColor
        case .critical: return style.badColor
        case .unknown: return style.secondaryTextColor
        }
    }
}
