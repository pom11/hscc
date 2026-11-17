import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// Live Activity — running cards.
//
// `CardsRunningActivityAttributes` is the ActivityKit attributes type for the
// running-cards Live Activity: the Dynamic Island / Lock Screen form of
// `hscc cards monitor --live`'s panel header — board · card · worker · run
// time — for the whole fleet at once.
//
// It is the same information the Home Screen widget (t_d2dc29e1,
// `CardsRunningWidget.swift`) draws, narrowed for the narrowest surface the
// platform has. It is NOT a second data path: the rows come from the same two
// routes the widget reads (`GET /v1/kanban/running` + `GET /v1/kanban/blocked`,
// WIDGET_DATA_CONTRACT §5.4 — never `/v1/cards`, which has no field projection),
// and the same three-state rule applies: a blind read never renders as an idle
// board, and a status the payload did not send renders `—`, never `0`.
//
// Live Activities are PUSH-ONLY: this type is compiled into BOTH the app (which
// starts/updates/ends it, and runs the launch orphan sweep) and the
// `HSCCLiveActivityMonitor` extension (which renders it), so both sides agree
// on the exact activity type and its content state.
//
// What ContentState carries is the REDUCED set the surfaces draw — no card
// body, no workspace path, no branch, no address. ActivityKit serializes this
// state into the extension's process, and `RunningCardLite` deliberately does
// not decode card prose (§7); the row type below keeps that property.
// ---------------------------------------------------------------------------

struct CardsRunningActivityAttributes: ActivityAttributes {
    /// The state the Dynamic Island / Lock Screen renders: the fleet's running
    /// cards as the app last pushed them.
    public struct ContentState: Codable, Hashable {
        /// The rows, longest-running first, at most `rowLimit` of them.
        ///
        /// Empty means either "the fleet is running nothing" (a real zero,
        /// because `failureLabel` is nil) or "the last read answered nothing"
        /// (`failureLabel` set). The two render differently — that distinction
        /// is the whole point of keeping `failureLabel` beside the rows.
        public var rows: [RunningCardMetric]
        /// The `/v1/kanban/running` route's own `count`. nil = this refresh was
        /// not told, which draws `—`; `0` = the server counted none.
        public var runningCount: Int?
        /// From `/v1/kanban/blocked`. nil = that read did not answer, which
        /// draws `—` rather than a zero (a board cannot look unblocked because
        /// one route was down).
        public var blockedCount: Int?
        /// True when `rows` are the whole running set (rows ≥ `runningCount`),
        /// so a family may say "that's all of them"; false means a subset is on
        /// screen and the footer has to say so.
        public var rowsAreComplete: Bool
        /// The blind-read label (`WidgetDataFailure.shortLabel`, ≤ 18 chars by
        /// contract), or nil when the last push came from a live read. Rendered
        /// in `Theme.Semantic.warn`/`.bad` — a stale or blind surface is never
        /// quiet.
        public var failureLabel: String?
        /// When the app last pushed. Drives the pushed `staleDate`, the age
        /// line, and the orphan decision below.
        public var updatedAt: Date
    }

    /// The most rows the expanded Island and the Lock Screen row can hold. The
    /// view clips to this as well, because a pushed ContentState is untrusted
    /// input to a fixed-height region.
    static let rowLimit = 3

    /// A token identifying this activity so it can be updated/ended.
    public var cardsID: UUID = UUID()
}

/// One running card, in the compact form the activity renders.
///
/// Mirrors `CardsRunningRow` (the widget's stored projection) field for field —
/// the same five scalars plus the card id — so the Island and the Home Screen
/// cannot disagree about what a row means. It is a separate type because
/// `CardsRunningRow` lives in the widget target and the App Group must keep no
/// payload type (§7); this one is ActivityKit's, never persisted.
struct RunningCardMetric: Codable, Hashable, Identifiable {
    /// Board slug, or nil when the row did not carry one.
    public var board: String?
    /// The card id (`t_…`) — machine-produced, so it renders monospaced.
    public var cardID: String
    /// The card title. Human-written, so it renders in the system font.
    public var title: String?
    /// The worker profile running it (`assignee` on the wire).
    public var worker: String?
    public var status: String?
    /// Minutes since `started_at`, computed once when the app built the state.
    /// nil = the row had no usable `started_at`, which draws `—`.
    public var ageMinutes: Int?

    /// Board-qualified: the same card id on two boards is two rows, and a
    /// `ForEach` id must not collide.
    var id: String { "\(board ?? "?")/\(cardID)" }

    /// The row's headline: its title, or its id when the card sent no title.
    /// An id is a real identifier; a placeholder word would be a guess.
    var headline: String {
        guard let title, !title.isEmpty else { return cardID }
        return title
    }

    /// The worker, or `—` ("nobody was named"). Never the board slug: that
    /// would let a row read as if a board were a worker.
    var workerText: String {
        guard let worker, !worker.isEmpty else { return "—" }
        return worker
    }

    /// `—` when unknown; `12m` under an hour; `2h07m` above it — the same rule
    /// the widget's rows use, so the two surfaces age a card identically.
    var ageText: String {
        guard let minutes = ageMinutes else { return "—" }
        if minutes < 60 { return "\(minutes)m" }
        return String(format: "%dh%02dm", minutes / 60, minutes % 60)
    }
}

// GATE-SLICE-BEGIN: cards-reading-rules
// ---------------------------------------------------------------------------
// The reading rules — every number and headline this activity can print.
//
// Same slicing arrangement as the orphan rule below: Foundation only, no
// ActivityKit, no Theme, so `live_activity_rehydration_check.sh` extracts this
// block from THIS file and executes it. The views forward here instead of
// deciding locally, which is what keeps the Lock Screen row, the expanded
// Island and the gate agreeing about one thing: a read that answered nothing
// can never render as an idle board.
// ---------------------------------------------------------------------------
enum CardsRunningReading {
    /// The number a surface may print as "running", or nil (which draws `—`).
    ///
    /// The server's own `count` wins. Counting the pushed rows is honest only
    /// when they are the whole set — `rowsAreComplete` — otherwise the rows are
    /// a subset and printing their number would claim a fleet is smaller than
    /// it is. nil is a rendering, not a failure: `—` is what "not told" looks
    /// like (WIDGET_DATA_CONTRACT §6 rule 1).
    static func displayCount(runningCount: Int?, rowCount: Int, rowsAreComplete: Bool) -> Int? {
        if let runningCount { return runningCount }
        return rowsAreComplete ? rowCount : nil
    }

    /// The headline. A blind read leads with its own label; only a read that
    /// answered may say anything about how much is running.
    static func headline(count: Int?, blockedCount: Int?, failureLabel: String?) -> String {
        if let count {
            let head = count == 1 ? "1 running" : "\(count) running"
            if let blockedCount { return head + " · \(blockedCount) blocked" }
            return head
        }
        if let failureLabel, !failureLabel.isEmpty { return failureLabel }
        return "—"
    }

    /// The Dynamic Island compact-trailing string: the count, `!` for a read
    /// that produced nothing, or `—` for a subset with no server count.
    /// Clipped, because the compact region is ~4 characters wide.
    static func compactTrailing(count: Int?, failureLabel: String?) -> String {
        guard let count else {
            return (failureLabel?.isEmpty == false) ? "!" : "—"
        }
        return count > 99 ? "99+" : "\(count)"
    }

    /// The blocked figure, or `— blocked` when that read did not answer. A
    /// board never looks unblocked because one route was down.
    static func blockedText(_ blockedCount: Int?) -> String {
        guard let blockedCount else { return "— blocked" }
        return blockedCount == 1 ? "1 blocked" : "\(blockedCount) blocked"
    }

    /// How long ago the app last pushed — the honest "this may be old" line.
    static func pushedAgeText(updatedAt: Date, now: Date) -> String {
        let total = max(0, Int(now.timeIntervalSince(updatedAt)))
        if total < 60 { return "now" }
        let minutes = total / 60
        if minutes < 60 { return "\(minutes)m ago" }
        return String(format: "%dh%02dm ago", minutes / 60, minutes % 60)
    }
}
// GATE-SLICE-END: cards-reading-rules

// GATE-SLICE-BEGIN: cards-orphan-decision
// ---------------------------------------------------------------------------
// The orphan decision — the ONE rule the launch sweep applies to a found
// running-cards activity.
//
// It lives here (Foundation only, no ActivityKit, no Settings) for two
// reasons: the sweep in the app target and the
// `live_activity_rehydration_check.sh` gate read the same text, and the gate
// SLICES this block out of this file and executes it (see that script) rather
// than transcribing a copy it could silently drift from.
// ---------------------------------------------------------------------------
enum CardsRunningOrphanRule {
    /// How long a running-cards activity may go without a push and still be
    /// treated as having a live feeder behind it.
    ///
    /// A Live Activity is only updated by the app process, so a backgrounded
    /// app is at the mercy of `BGAppRefreshTask`, which is best-effort and can
    /// legitimately lag for hours. Anything below that would end a bubble the
    /// operator is still entitled to look at. It is also bounded on purpose:
    /// the sweep runs at every launch, which is exactly when a real feeder
    /// would resume, so an activity still silent this long after the last push
    /// has no live feeder — it is the bubble a force-quit left behind.
    static let maxSilence: TimeInterval = 6 * 60 * 60

    /// True when a found activity can only be an orphan and the sweep must end
    /// it.
    ///
    ///   * `rowCount == 0` → END. With rows the driver's own rule is to end the
    ///     activity as soon as it learns nothing is running (an ambient surface
    ///     about work, with no work, is noise), and a blind read that carried no
    ///     fallback rows names no card either — so an empty bubble is never
    ///     "still in flight", whichever of the two produced it.
    ///   * `now - updatedAt > maxSilence` → END. No legitimate feeder has been
    ///     this quiet.
    ///   * otherwise → KEEP. There are rows and the app pushed recently, so the
    ///     poll loop is alive; ending it would blank a live surface.
    static func isOrphan(rowCount: Int, updatedAt: Date, now: Date) -> Bool {
        if rowCount == 0 { return true }
        return now.timeIntervalSince(updatedAt) > maxSilence
    }
}
// GATE-SLICE-END: cards-orphan-decision

// GATE-SLICE-BEGIN: cards-feed-decision
// ---------------------------------------------------------------------------
// The feed decision — the ONE rule the app's driver applies to each refresh.
//
// Same arrangement as the two blocks above, for the same reason: the driver
// itself is ActivityKit code (`Activity.request` / `update` / `end`) that no
// macOS harness can compile, so the DECISION it makes lives here, where
// `live_activity_rehydration_check.sh` slices it out and executes it as a
// table. What the driver adds on top of this is transport and timing, not
// judgement.
//
// It is deliberately the same shape as `CardsRunningOrphanRule.isOrphan` above
// — rows first, age second — because driver and launch sweep MUST agree. If
// they disagreed, the app would push a bubble the next launch immediately
// ends, which is the flicker an ambient surface cannot have.
// ---------------------------------------------------------------------------
enum CardsRunningFeedRule {
    /// What one refresh tells the driver to do with the activity.
    enum Action: Equatable {
        /// Start the activity if it is not up, otherwise `update` it with this state.
        case push
        /// `end` it. An empty bubble is never "still in flight" — the same
        /// conclusion `CardsRunningOrphanRule` reaches at launch.
        case end
        /// Nothing to do: there is no activity up and nothing worth starting.
        case idle
    }

    /// The decision for one refresh.
    ///
    ///   * `rowCount == 0` → END if one is up, else nothing. This is the rule
    ///     the card decides on: a Live Activity about work, with no work, is
    ///     noise — so the driver ENDS when it learns nothing is running rather
    ///     than leaving an empty bubble up. It applies to both kinds of empty:
    ///     the real zero (a live read that counted none) and a blind read with
    ///     no fallback rows, neither of which names a card to show.
    ///   * rows and a live read → PUSH. A subset (`rowsAreComplete` false)
    ///     still pushes: the headline counts the server's own `count`, not the
    ///     rows, so a partial list cannot under-report the fleet.
    ///   * rows carried by a stale read → PUSH only when one is already up. A
    ///     snapshot can HOLD a bubble honest (the label says why this refresh
    ///     did not answer); it must not START one, because a bubble that was
    ///     never live is a bubble whose age the operator cannot see.
    static func decide(activityIsUp: Bool, rowCount: Int, failureLabel: String?) -> Action {
        if rowCount == 0 { return activityIsUp ? .end : .idle }
        if failureLabel == nil { return .push }
        return activityIsUp ? .push : .idle
    }

    /// The `failureLabel` a push carries: nil only for a live read, and never
    /// empty for one that was not live.
    ///
    /// The second half is the invariant the whole surface rests on, seen from
    /// the push side: a refresh that did not answer ALWAYS says so. A nil
    /// label on a non-live push is what would let a blind refresh render as a
    /// quiet, current board — which is the one failure mode §5.4 refuses.
    /// `reason` is `WidgetDataFailure.shortLabel` (≤ 18 chars by contract);
    /// the fallback word is for the case where the caller had a failure and no
    /// label, and it must still be a sentence the operator can read.
    static func label(reason: String?, live: Bool) -> String? {
        guard !live else { return nil }
        guard let reason, !reason.isEmpty else { return "No answer" }
        return reason
    }
}
// GATE-SLICE-END: cards-feed-decision
