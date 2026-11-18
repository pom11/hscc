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
        ActivityConfiguration(for: BoardProgressActivityAttributes.self) { context in
            LockScreenBoardProgressView(context: context)
                .activityBackgroundTint(Theme.Palette.graphite)
                .activitySystemActionForegroundColor(Theme.Semantic.onSurface)
        } dynamicIsland: { context in
            let state = context.state
            let board = context.attributes.board
            let tone = BoardProgressReading.tone(running: state.running,
                                                 blocked: state.blocked,
                                                 hasNumbers: state.hasNumbers,
                                                 failureLabel: state.failureLabel)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    BoardProgressGlyph(tone: tone)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(BoardProgressReading.boardLabel(board))
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                        Text(BoardProgressReading.headline(running: state.running,
                                                           blocked: state.blocked,
                                                           ready: state.ready,
                                                           done: state.done,
                                                           failureLabel: state.failureLabel))
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.onSurface)
                            .lineLimit(1)
                        if let failureLabel = state.failureLabel, !failureLabel.isEmpty,
                           state.hasNumbers {
                            // Stale is not quiet: the counts below are
                            // last-known, and this says why they are not new.
                            // With no counts the headline already IS the
                            // failure, so it is not repeated here.
                            Text("stale · \(failureLabel)")
                                .font(.caption2)
                                .foregroundColor(Theme.Semantic.warn)
                                .lineLimit(1)
                        }
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(BoardProgressReading.pushedAgeText(updatedAt: state.updatedAt,
                                                            now: Date()))
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    BoardProgressCountsRow(state: state,
                                           labelFont: .hsccMono(10, weight: .semibold))
                }
            } compactLeading: {
                BoardProgressGlyph(tone: tone)
            } compactTrailing: {
                Text(BoardProgressReading.compactTrailing(running: state.running,
                                                          blocked: state.blocked,
                                                          failureLabel: state.failureLabel))
                    .font(.hsccMono(11, weight: .semibold))
                    .foregroundColor(BoardProgressToneColour.color(tone))
            } minimal: {
                BoardProgressGlyph(tone: tone)
            }
        }
    }
}

// MARK: - Shared rendering pieces

/// The activity's glyph: the board grid, coloured by what the last read was.
/// Colour carries the state here so the compact regions stay text free.
struct BoardProgressGlyph: View {
    let tone: BoardProgressTone

    var body: some View {
        Image(systemName: "square.grid.2x2")
            .foregroundColor(BoardProgressToneColour.color(tone))
    }
}

/// The one place a `BoardProgressTone` becomes a `Color`. The tone itself is
/// decided in the Foundation-only rules above (a `Color` there could not be
/// compiled by a macOS gate); mapping it once here keeps every region of the
/// Island and the Lock Screen from inventing its own green.
enum BoardProgressToneColour {
    static func color(_ tone: BoardProgressTone) -> Color {
        switch tone {
        case .ok: return Theme.Semantic.ok
        case .warn: return Theme.Semantic.warn
        case .bad: return Theme.Semantic.bad
        case .neutral: return Theme.Semantic.neutral
        }
    }

    /// The colour one count earns. Done and ready are good news; running is
    /// active; blocked is the one that needs eyes — the same emphasis the
    /// project overview gives its blocked hero.
    static func count(_ key: String) -> Color {
        switch key {
        case "done": return Theme.Semantic.ok
        case "running": return Theme.Semantic.warn
        case "blocked": return Theme.Semantic.bad
        case "ready": return Theme.Semantic.ok
        default: return Theme.Semantic.neutral
        }
    }
}

/// The four counts as a compact row of value-over-label tiles.
struct BoardProgressCountsRow: View {
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
                        .foregroundColor(BoardProgressToneColour.count(count.key))
                    Text(count.label)
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Spacer()
                Text(BoardProgressReading.pushedAgeText(updatedAt: state.updatedAt,
                                                        now: Date()))
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }

            if state.hasNumbers {
                Text(BoardProgressReading.headline(running: state.running,
                                                   blocked: state.blocked,
                                                   ready: state.ready,
                                                   done: state.done,
                                                   failureLabel: state.failureLabel))
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .lineLimit(1)

                BoardProgressCountsRow(state: state,
                                       labelFont: .hsccMono(14, weight: .bold))

                if let failureLabel = state.failureLabel, !failureLabel.isEmpty {
                    // Stale is not quiet: the counts above are last-known, and
                    // this says why the newest one is not.
                    Text("stale · \(failureLabel)")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.warn)
                        .lineLimit(1)
                } else if let line = BoardProgressReading.emptyLine(hasNumbers: true,
                                                                    running: state.running,
                                                                    blocked: state.blocked,
                                                                    failureLabel: nil) {
                    Text(line)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            } else if let line = BoardProgressReading.emptyLine(hasNumbers: false,
                                                                running: state.running,
                                                                blocked: state.blocked,
                                                                failureLabel: state.failureLabel) {
                // A blind read with nothing to fall back on: this sentence is
                // the whole content, and it is warn-coloured, not quiet grey.
                Text(line)
                    .font(.caption)
                    .foregroundColor(state.failureLabel?.isEmpty == false
                                     ? Theme.Semantic.warn
                                     : Theme.Semantic.onSurfaceMuted)
            }
        }
        .padding()
    }
}
