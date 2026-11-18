import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// Live Activity — one board's kanban progress.
//
// `BoardProgressActivityAttributes` is the ActivityKit attributes type for the
// per-board progress bubble: the Dynamic Island / Lock Screen form of the Board
// Status widget's five numbers — running · ready · todo · blocked · done 24h.
//
// It is NOT a second data path. The numbers come from the same projection the
// Board Status widget reads (`BoardStatusProjection` over
// `GET /v1/mobile/summary`, with the sanctioned `/v1/kanban/{stale,blocked}`
// interim when that route is missing), and the last-known value is the SAME
// App Group snapshot (`BoardStatusSnapshotStore`) the widget draws. This
// activity is started by the operator for ONE board; the board slug is an
// attribute and the counts are the pushed ContentState.
//
// Live Activities are PUSH-ONLY: this type is compiled into BOTH the app (which
// starts/updates/ends it and runs the poll loop) and the
// `HSCCLiveActivityMonitor` extension (which renders it), so both sides agree
// on the exact activity type and its content state. The extension never
// fetches, and this type is never persisted — ActivityKit carries it, the App
// Group does not (§7).
// ---------------------------------------------------------------------------

struct BoardProgressActivityAttributes: ActivityAttributes {
    /// The state the Dynamic Island / Lock Screen renders: the five board
    /// counts as the app last pushed them, plus when it last pushed.
    public struct ContentState: Codable, Hashable {
        /// The five numbers the surface draws — exactly the CLI board table's
        /// Running/Ready/Todo/Blocked/Done 24h cells for this board. Every one
        /// is optional on purpose: nil means "this refresh did not carry that
        /// count" and draws `—`, while `0` is the server's own "none".
        public var done: Int?
        public var running: Int?
        public var blocked: Int?
        public var ready: Int?
        public var todo: Int?
        /// The blind-read label (`WidgetDataFailure.shortLabel`, ≤ 18 chars by
        /// contract), or nil when the last push came from a live read.
        public var failureLabel: String?
        /// The operator-facing reason, when there is one. Separated from the
        /// label so the Lock Screen can say "No connection — is Tailscale
        /// connected?" while the Island stays one short word.
        public var failureDetail: String?
        /// When the app last pushed. Drives the age line and `staleDate`.
        public var updatedAt: Date

        /// Whether anything at all is drawable. False means the surface shows
        /// the failure line, never five reassuring dashes dressed as a zero.
        var hasNumbers: Bool {
            done != nil || running != nil || blocked != nil || ready != nil || todo != nil
        }
    }

    /// The board this activity watches. Empty means every board together, the
    /// same "All boards" choice the widget's configuration offers.
    public var board: String = ""
}

/// One count in the surface's row: the key is stable for `ForEach`, the label
/// is the short copy that fits the Lock Screen, and the value stays optional so
/// "not told" can never become `0`.
struct BoardProgressCount: Hashable, Identifiable {
    let key: String
    let label: String
    let value: Int?

    var id: String { key }
    var text: String { BoardProgressReading.numberText(value) }
}

/// The colour a reading earns. Deliberately an enum, not a `Color`: a `Color`
/// needs SwiftUI and would lock this rule out of Foundation-only harnesses. The
/// extension maps a tone to `Theme.Semantic` in one place.
enum BoardProgressTone: String {
    case ok
    case warn
    case bad
    case neutral
}

/// Every number, word and tone this activity prints.
///
/// Views forward here and decide nothing locally, so the compact, expanded and
/// Lock Screen regions of one pushed state cannot disagree — and a refresh that
/// did not answer can never render as a quiet board.
enum BoardProgressReading {
    /// The five CLI columns, in reading order: what is moving, what is queued,
    /// what is stuck, what is finished today.
    static func counts(done: Int?, running: Int?, blocked: Int?,
                       ready: Int?, todo: Int?) -> [BoardProgressCount] {
        [
            BoardProgressCount(key: "running", label: "run", value: running),
            BoardProgressCount(key: "ready", label: "ready", value: ready),
            BoardProgressCount(key: "todo", label: "todo", value: todo),
            BoardProgressCount(key: "blocked", label: "blocked", value: blocked),
            BoardProgressCount(key: "done", label: "done 24h", value: done),
        ]
    }

    /// The ONE place a count becomes a string. nil = "not told" and draws `—`;
    /// `0` is a real, server-counted none.
    static func numberText(_ value: Int?) -> String {
        value.map { "\($0)" } ?? "—"
    }

    /// The board heading. An empty slug is the all-boards choice, never a blank
    /// line the operator has to interpret.
    static func boardLabel(_ board: String) -> String {
        board.isEmpty ? "All boards" : board
    }

    /// The headline: what is moving, then what needs eyes. A real `0 running`
    /// is printed (it is the server's answer); a nil count is simply left out,
    /// because `— running` as a headline is noise, not information.
    static func headline(running: Int?, blocked: Int?, ready: Int?,
                         done: Int?, failureLabel: String?) -> String {
        if let running {
            var line = running == 1 ? "1 running" : "\(running) running"
            if let blocked, blocked > 0 {
                line += " · \(blocked) blocked"
            } else if let ready {
                line += " · \(ready) ready"
            }
            return line
        }
        if let blocked {
            return blocked == 1 ? "1 blocked" : "\(blocked) blocked"
        }
        if let done {
            return done == 1 ? "1 done" : "\(done) done"
        }
        if let failureLabel, !failureLabel.isEmpty { return failureLabel }
        return "—"
    }

    /// The Dynamic Island compact-trailing string. Blocked outranks running:
    /// the compact region is the one glance that has to surface a jam. `!` is a
    /// read that produced nothing; `—` is a state that was never told.
    static func compactTrailing(running: Int?, blocked: Int?,
                                failureLabel: String?) -> String {
        if let blocked, blocked > 0 { return clipped(blocked) }
        if let running { return clipped(running) }
        return (failureLabel?.isEmpty == false) ? "!" : "—"
    }

    private static func clipped(_ value: Int) -> String {
        value > 99 ? "99+" : "\(value)"
    }

    /// The colour. A blind read is never quiet: bad when it carries no numbers,
    /// warn when it still shows last-known ones. Otherwise a counted blocked
    /// board is amber, and a moving board is green.
    static func tone(running: Int?, blocked: Int?, hasNumbers: Bool,
                     failureLabel: String?) -> BoardProgressTone {
        if failureLabel?.isEmpty == false {
            return hasNumbers ? .warn : .bad
        }
        if let blocked, blocked > 0 { return .warn }
        if let running, running > 0 { return .ok }
        return .neutral
    }

    /// The sentence under an empty row. A blind read with nothing to fall back
    /// on says so; a read that answered and found nothing moving says that.
    /// Those two must never share a line.
    static func emptyLine(hasNumbers: Bool, running: Int?, blocked: Int?,
                          failureLabel: String?) -> String? {
        guard !hasNumbers else {
            if running == 0 && blocked == 0 { return "Nothing running or blocked" }
            return nil
        }
        if let failureLabel, !failureLabel.isEmpty {
            return "\(failureLabel) — no board counts to show"
        }
        return "No board counts yet"
    }

    /// How long ago the app last pushed — the honest "this may be old" line.
    /// A clock ahead of this device reads `now`, never a negative age.
    static func pushedAgeText(updatedAt: Date, now: Date) -> String {
        let total = max(0, Int(now.timeIntervalSince(updatedAt)))
        if total < 60 { return "updated just now" }
        let minutes = total / 60
        if minutes < 60 { return "updated \(minutes)m ago" }
        return String(format: "updated %dh%02dm ago", minutes / 60, minutes % 60)
    }
}
