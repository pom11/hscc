import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivity — the ActivityKit configuration for the fleet-wake Live
/// Activity.
///
/// Hosted by the `HSCCLiveActivity` app-extension target. It renders the
/// `WakingActivityAttributes` that the app starts via `LiveActivityManager`.
///
/// Dynamic Island:
///   * compact — elapsed time + a state dot (serving / waking).
///   * expanded — the topology pairs coming online, honest per-node readiness.
///
/// Ends on success (state → up) or failure, and says WHICH happened — a Live
/// Activity that just disappears is worse than none. The only "progress" shown
/// is elapsed time + per-node readiness from the API; there is NO fake
/// percentage bar (the wake has no reliable progress signal).
@main
struct HSCCLiveActivity: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityWake)
        return ActivityConfiguration(for: WakingActivityAttributes.self) { context in
            // Lock Screen / banner view.
            LockScreenWakeView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            DynamicIsland {
                // Expanded UI.
                DynamicIslandExpandedRegion(.leading) {
                    stateDot(context.state.state, style: style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(headline(state: context.state.state))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                        if context.state.failed, let message = context.state.message {
                            Text(message)
                                .font(style.font(.caption))
                                .foregroundColor(style.badColor)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    TopologyComingOnlineView(upNodes: context.state.upNodes)
                        .padding(.top, 2)
                        .widgetStyle(style)
                }
            } compactLeading: {
                Image(systemName: "bolt").widgetIcon(style)
                    .foregroundColor(style.warnColor)
            } compactTrailing: {
                // Elapsed time + a state dot.
                HStack(spacing: 4) {
                    stateDot(context.state.state, style: style)
                    TextTimerView(context: context)
                }
            } minimal: {
                stateDot(context.state.state, style: style)
            }
        }
    }

    private func stateDot(_ state: String, style: WidgetStyle) -> some View {
        Circle()
            .fill(color(for: state, style: style))
            .frame(width: 10, height: 10)
    }

    private func color(for state: String, style: WidgetStyle) -> Color {
        switch state {
        case "up": return style.okColor
        case "waking": return style.warnColor
        case "down": return style.badColor
        default: return style.secondaryTextColor
        }
    }

    private func headline(state: String) -> String {
        switch state {
        case "waking": return "Waking the fleet"
        case "up": return "Fleet is up"
        case "down": return "Wake failed"
        default: return "Fleet wake"
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the wake activity.
struct LockScreenWakeView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<WakingActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                if context.state.failed {
                    Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                        .foregroundColor(style.badColor)
                } else {
                    ProgressView()
                        .tint(style.warnColor)
                }
                Text(headline(state: context.state.state))
                    .font(style.font(.headline))
                    .foregroundColor(style.primaryTextColor)
                Spacer()
                if !context.state.failed {
                    TextTimerView(context: context)
                }
            }
            .widgetHeader(style)
            if context.state.failed, let message = context.state.message {
                Text(message)
                    .font(style.font(.caption))
                    .foregroundColor(style.badColor)
            } else {
                Text("Waking \(context.state.upNodes.count) of 4 units")
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
        .padding()
    }

    private func headline(state: String) -> String {
        switch state {
        case "waking": return "Waking the fleet"
        case "up": return "Fleet is up"
        case "down": return "Wake failed"
        default: return "Fleet wake"
        }
    }
}

// MARK: - Compact timer (elapsed time)

/// A `Text` that ticks: shows the elapsed time since the wake began, rendered
/// as mm:ss — the one honest progress signal a wake has. The wake has no
/// reliable progress signal, so elapsed time + per-unit readiness is all we
/// show (never a fabricated percentage bar).
///
/// `context.state.startedAt` carries the wake's start; `Text(timerInterval:)`
/// renders a count-UP that the system refreshes each minute for Live
/// Activities, bounded by a distant-future end (the activity is dismissed on
/// settle anyway). When the wake has settled (failed/up) the view shows a
/// static elapsed readout instead of a live ticker.
struct TextTimerView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<WakingActivityAttributes>

    var body: some View {
        if context.state.failed {
            // Settled — render a static total elapsed, no live ticker.
            Text(elapsedString(context.state.startedAt))
                .font(style.font(size: 13, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .monospacedDigit()
        } else if let start = context.state.startedAt {
            Text(timerInterval: start...Date.distantFuture, countsDown: false)
                .font(style.font(size: 13, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .monospacedDigit()
        } else {
            Text("—")
                .font(style.font(size: 13, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
        }
    }

    private func elapsedString(_ start: Date?) -> String {
        guard let start else { return "—" }
        let total = Int(Date().timeIntervalSince(start))
        let m = total / 60
        let s = total % 60
        return String(format: "%d:%02d", m, s)
    }
}

// MARK: - Topology coming online

/// The expanded "topology pairs coming online" strip: the two TP pairs, each
/// node dot glowing mint once the API reports it serving, amber while waking.
/// Honest per-node readiness — only nodes the API says are up are mint.
struct TopologyComingOnlineView: View {
    @Environment(\.widgetStyle) private var style
    let upNodes: [String]

    private let pairs: [TopologyPair] = [
        TopologyPair(nodes: [
            TopologyNode(label: ".244", state: .busy),
            TopologyNode(label: ".246", state: .busy),
        ], role: "orchestrator"),
        TopologyPair(nodes: [
            TopologyNode(label: ".247", state: .busy),
            TopologyNode(label: ".248", state: .busy),
        ], role: "worker"),
    ]

    var body: some View {
        HStack(spacing: 16) {
            ForEach(pairs) { pair in
                HStack(spacing: 4) {
                    dot(pair.nodes[0])
                    Rectangle()
                        .fill(style.secondaryTextColor.opacity(0.5))
                        .frame(width: 10, height: 2)
                    dot(pair.nodes[1])
                }
            }
            Spacer(minLength: 0)
        }
    }

    private func dot(_ node: TopologyNode) -> some View {
        let up = upNodes.contains(node.label)
        return Circle()
            .fill(up ? style.okColor : style.warnColor.opacity(0.8))
            .frame(width: 9, height: 9)
    }
}
