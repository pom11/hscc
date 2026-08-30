import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivitySession — the ActivityKit configuration for the SESSION
/// Live Activity.
///
/// Hosted by the `HSCCLiveActivitySession` app-extension target (a SEPARATE
/// target from `HSCCLiveActivity`, which owns the fleet-wake activity — one
/// widget extension body can only contain ONE `ActivityConfiguration`, so each
/// Live Activity attribute type gets its own extension target).
///
/// This is the LIVE mirror of a project's streaming session on the Lock Screen
/// / Dynamic Island. The app starts/updates/ends it via `SessionActivityDriver`
/// (fed by the streaming chat pipeline: StreamingTranscript/StreamingChatStore
/// fold session events, and the driver derives a compact summary). The summary
/// is decoded against the SAME session_event wire contract the full chat uses —
/// an extension never fabricates progress; it renders whatever the app's latest
/// fold derived.
///
/// Dynamic Island:
///   * compact/minimal — a phase-colored dot (mint = streaming, amber = tool,
///     muted = settled, red = error, neutral = idle).
///   * expanded — project name + derived headline/detail + event count.
///
/// Ends when the operator leaves that project's live chat (the driver calls
/// `end`); a session mirror simply goes away when not watched.
@main
struct HSCCLiveActivitySession: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SessionActivityAttributes.self) { context in
            LockScreenSessionActivityView(context: context)
                .activityBackgroundTint(Theme.Palette.graphite)
                .activitySystemActionForegroundColor(Theme.Semantic.onSurface)
        } dynamicIsland: { context in
            DynamicIsland {
                // Expanded UI.
                DynamicIslandExpandedRegion(.leading) {
                    sessionGlyph(context.state.phase)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(context.state.headline)
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.onSurface)
                        if !context.state.detail.isEmpty {
                            Text(context.state.detail)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .lineLimit(2)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text("\(context.state.activityCount) events")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            } compactLeading: {
                Image(systemName: "bubble.left.and.bubble.right")
                    .foregroundColor(sessionColor(context.state.phase))
            } compactTrailing: {
                sessionGlyph(context.state.phase)
            } minimal: {
                sessionGlyph(context.state.phase)
            }
        }
    }

    /// The leading/minimal glyph: a filled dot whose color encodes the turn
    /// phase (mint = streaming, amber = tool, muted = settled, red = error,
    /// neutral = idle).
    private func sessionGlyph(_ phase: String) -> some View {
        Circle()
            .fill(sessionColor(phase))
            .frame(width: 10, height: 10)
    }

    private func sessionColor(_ phase: String) -> Color {
        switch phase {
        case "streaming": return Theme.Semantic.ok
        case "tool": return Theme.Semantic.warn
        case "done": return Theme.Semantic.onSurfaceMuted
        case "error": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the session activity — the
/// live mirror of a project's streaming session. Led by the project name and
/// the derived headline/detail; the glyph color encodes the turn phase.
struct LockScreenSessionActivityView: View {
    let context: ActivityViewContext<SessionActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Circle()
                    .fill(sessionColor(context.state.phase))
                    .frame(width: 10, height: 10)
                Text(context.state.project)
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                Spacer()
                Text("\(context.state.activityCount) events")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            HStack(spacing: 8) {
                Image(systemName: sessionIcon(context.state.phase))
                    .foregroundColor(sessionColor(context.state.phase))
                Text(context.state.headline)
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer()
            }
            if !context.state.detail.isEmpty {
                Text(context.state.detail)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(2)
            }
        }
        .padding()
    }

    private func sessionColor(_ phase: String) -> Color {
        switch phase {
        case "streaming": return Theme.Semantic.ok
        case "tool": return Theme.Semantic.warn
        case "done": return Theme.Semantic.onSurfaceMuted
        case "error": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }

    private func sessionIcon(_ phase: String) -> String {
        switch phase {
        case "streaming": return "ellipsis.bubble"
        case "tool": return "hammer"
        case "done": return "checkmark.bubble"
        case "error": return "exclamationmark.triangle.fill"
        default: return "bubble.left.and.bubble.right"
        }
    }
}
