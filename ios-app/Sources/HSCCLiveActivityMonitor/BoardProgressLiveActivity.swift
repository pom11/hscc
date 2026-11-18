import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for one board's
/// kanban-progress Live Activity.
///
/// Hosted by the `HSCCLiveActivityMonitor` app-extension target beside the
/// monitor, running-cards and cluster-health activities (see
/// `HSCCLiveActivityMonitorBundle`): one extension hosts several Live Activity
/// types, each with its own `ActivityConfiguration`.
///
/// Live Activities are PUSH-ONLY — this extension never fetches. It renders the
/// four board counts the app last pushed into
/// `BoardProgressActivityAttributes.ContentState` (done · running · blocked ·
/// ready, from the same projection the Board Status widget draws) and ages
/// honestly via the pushed `updatedAt`.
///
/// Dynamic Island:
///   * compact leading  — the board glyph, coloured by the reading.
///   * compact trailing — the blocked count, else running (`!` for a blind
///     read with no counts).
///   * expanded         — board name + headline, age, then the four counts.
///   * minimal          — the glyph alone (the island is minimized).
///
/// Lock Screen — board name, headline, age, the four counts, and the one
/// sentence that replaces them when nothing was told.
///
/// Every word, number and tone comes from `BoardProgressReading` (in
/// `Sources/Shared/BoardProgressActivity.swift`), never from a local decision,
/// so a blind read renders its failure label and never four quiet zeros.
struct BoardProgressLiveActivity: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityBoardProgress)
        return ActivityConfiguration(for: BoardProgressActivityAttributes.self) { context in
            LockScreenBoardProgressView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            let state = context.state
            let board = context.attributes.board
            let tone = BoardProgressReading.tone(running: state.running,
                                                 blocked: state.blocked,
                                                 hasNumbers: state.hasNumbers,
                                                 failureLabel: state.failureLabel)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    BoardProgressGlyph(tone: tone).widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(BoardProgressReading.boardLabel(board))
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                            .lineLimit(1)
                        Text(BoardProgressReading.headline(running: state.running,
                                                           blocked: state.blocked,
                                                           ready: state.ready,
                                                           done: state.done,
                                                           failureLabel: state.failureLabel))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                        if let failureLabel = state.failureLabel, !failureLabel.isEmpty,
                           state.hasNumbers {
                            // Stale is not quiet: the counts below are
                            // last-known, and this says why they are not new.
                            // With no counts the headline already IS the
                            // failure, so it is not repeated here.
                            Text("stale · \(failureLabel)")
                                .font(style.font(.caption2))
                                .foregroundColor(style.warnColor)
                                .lineLimit(1)
                        }
                    }
                    .widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(BoardProgressReading.pushedAgeText(updatedAt: state.updatedAt,
                                                            now: Date()))
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    BoardProgressCountsRow(state: state,
                                           labelFont: style.font(size: 10, weight: .semibold)).widgetStyle(style)
                }
            } compactLeading: {
                BoardProgressGlyph(tone: tone).widgetStyle(style)
            } compactTrailing: {
                Text(BoardProgressReading.compactTrailing(running: state.running,
                                                          blocked: state.blocked,
                                                          failureLabel: state.failureLabel))
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(BoardProgressToneColour.color(tone, style: style))
            } minimal: {
                BoardProgressGlyph(tone: tone).widgetStyle(style)
            }
        }
    }
}

// MARK: - Shared rendering pieces

/// The activity's glyph: the board grid, coloured by what the last read was.
/// Colour carries the state here so the compact regions stay text free.
struct BoardProgressGlyph: View {
    @Environment(\.widgetStyle) private var style
    let tone: BoardProgressTone

    var body: some View {
        Image(systemName: "square.grid.2x2").widgetIcon(style)
            .foregroundColor(BoardProgressToneColour.color(tone, style: style))
    }
}

/// The one place a `BoardProgressTone` becomes a `Color`. The tone itself is
/// decided in the Foundation-only rules above (a `Color` there could not be
/// compiled by a macOS gate); mapping it once here keeps every region of the
/// Island and the Lock Screen from inventing its own green.
enum BoardProgressToneColour {
    static func color(_ tone: BoardProgressTone, style: WidgetStyle) -> Color {
        switch tone {
        case .ok: return style.okColor
        case .warn: return style.warnColor
        case .bad: return style.badColor
        case .neutral: return style.secondaryTextColor
        }
    }

    /// The colour one count earns. Done and ready are good news; running is
    /// active; blocked is the one that needs eyes — the same emphasis the
    /// project overview gives its blocked hero.
    static func count(_ key: String, style: WidgetStyle) -> Color {
        switch key {
        case "done": return style.okColor
        case "running": return style.warnColor
        case "blocked": return style.badColor
        case "ready": return style.okColor
        default: return style.secondaryTextColor
        }
    }
}

/// The four counts as a compact row of value-over-label tiles.
struct BoardProgressCountsRow: View {
    @Environment(\.widgetStyle) private var style
    let state: BoardProgressActivityAttributes.ContentState
    let labelFont: Font

    var body: some View {
        let counts = BoardProgressReading.counts(done: state.done,
                                                 running: state.running,
                                                 blocked: state.blocked,
                                                 ready: state.ready)
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            ForEach(counts) { count in
                VStack(alignment: .leading, spacing: 1) {
                    Text(count.text)
                        .font(labelFont)
                        .foregroundColor(BoardProgressToneColour.count(count.key, style: style))
                    Text(count.label)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 4)
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the board-progress activity.
struct LockScreenBoardProgressView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<BoardProgressActivityAttributes>

    var body: some View {
        let state = context.state
        VStack(alignment: .leading, spacing: 8) {
            // Header: glyph + board + age.
            HStack(spacing: 8) {
                BoardProgressGlyph(tone: BoardProgressReading.tone(running: state.running,
                                                                   blocked: state.blocked,
                                                                   hasNumbers: state.hasNumbers,
                                                                   failureLabel: state.failureLabel))
                Text(BoardProgressReading.boardLabel(context.attributes.board))
                    .font(style.font(.headline))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                Spacer()
                Text(BoardProgressReading.pushedAgeText(updatedAt: state.updatedAt,
                                                        now: Date()))
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            .widgetHeader(style)

            if state.hasNumbers {
                Text(BoardProgressReading.headline(running: state.running,
                                                   blocked: state.blocked,
                                                   ready: state.ready,
                                                   done: state.done,
                                                   failureLabel: state.failureLabel))
                    .font(style.font(.subheadline))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)

                BoardProgressCountsRow(state: state,
                                       labelFont: style.font(size: 14, weight: .bold))

                if let failureLabel = state.failureLabel, !failureLabel.isEmpty {
                    // Stale is not quiet: the counts above are last-known, and
                    // this says why the newest one is not.
                    Text("stale · \(failureLabel)")
                        .font(style.font(.caption2))
                        .foregroundColor(style.warnColor)
                        .lineLimit(1)
                } else if let line = BoardProgressReading.emptyLine(hasNumbers: true,
                                                                    running: state.running,
                                                                    blocked: state.blocked,
                                                                    failureLabel: nil) {
                    Text(line)
                        .font(style.font(.caption))
                        .foregroundColor(style.secondaryTextColor)
                }
            } else if let line = BoardProgressReading.emptyLine(hasNumbers: false,
                                                                running: state.running,
                                                                blocked: state.blocked,
                                                                failureLabel: state.failureLabel) {
                // A blind read with nothing to fall back on: this sentence is
                // the whole content, and it is warn-coloured, not quiet grey.
                Text(line)
                    .font(style.font(.caption))
                    .foregroundColor(state.failureLabel?.isEmpty == false
                                     ? style.warnColor
                                     : style.secondaryTextColor)
            }
        }
        .padding()
    }
}
