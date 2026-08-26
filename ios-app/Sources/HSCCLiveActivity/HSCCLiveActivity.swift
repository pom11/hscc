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
        ActivityConfiguration(for: WakingActivityAttributes.self) { context in
            // Lock Screen / banner view.
            LockScreenWakeView(context: context)
                .activityBackgroundTint(Theme.Palette.graphite)
                .activitySystemActionForegroundColor(Theme.Semantic.onSurface)
        } dynamicIsland: { context in
            DynamicIsland {
                // Expanded UI.
                DynamicIslandExpandedRegion(.leading) {
                    stateDot(context.state.state)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(headline(state: context.state.state))
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.onSurface)
                        if context.state.failed, let message = context.state.message {
                            Text(message)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.bad)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    TopologyComingOnlineView(upNodes: context.state.upNodes)
                        .padding(.top, 2)
                }
            } compactLeading: {
                Image(systemName: "bolt")
                    .foregroundColor(Theme.Semantic.warn)
            } compactTrailing: {
                // Elapsed time + a state dot.
                HStack(spacing: 4) {
                    stateDot(context.state.state)
                    TextTimerView(context: context)
                }
            } minimal: {
                stateDot(context.state.state)
            }
        }
    }

    private func stateDot(_ state: String) -> some View {
        Circle()
            .fill(color(for: state))
            .frame(width: 10, height: 10)
    }

    private func color(for state: String) -> Color {
        switch state {
        case "up": return Theme.Semantic.ok
        case "waking": return Theme.Semantic.warn
        case "down": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
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
    let context: ActivityViewContext<WakingActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                if context.state.failed {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(Theme.Semantic.bad)
                } else {
                    ProgressView()
                        .tint(Theme.Semantic.warn)
                }
                Text(headline(state: context.state.state))
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer()
                if !context.state.failed {
                    TextTimerView(context: context)
                }
            }
            if context.state.failed, let message = context.state.message {
                Text(message)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.bad)
            } else {
                Text("Waking \(context.state.upNodes.count) of 4 units")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
    let context: ActivityViewContext<WakingActivityAttributes>

    var body: some View {
        if context.state.failed {
            // Settled — render a static total elapsed, no live ticker.
            Text(elapsedString(context.state.startedAt))
                .font(.hsccMono(13, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
                .monospacedDigit()
        } else if let start = context.state.startedAt {
            Text(timerInterval: start...Date.distantFuture, countsDown: false)
                .font(.hsccMono(13, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
                .monospacedDigit()
        } else {
            Text("—")
                .font(.hsccMono(13, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
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
                        .fill(Theme.Palette.mist.opacity(0.5))
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
            .fill(up ? Theme.Semantic.ok : Theme.Semantic.warn.opacity(0.8))
            .frame(width: 9, height: 9)
    }
}
