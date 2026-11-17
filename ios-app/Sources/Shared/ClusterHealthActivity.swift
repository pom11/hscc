import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// Live Activity — cluster health.
//
// `ClusterHealthActivityAttributes` is the ActivityKit attributes type for the
// cluster-health Live Activity: the fleet's serving state and its nodes
// up/down — the Dynamic Island / Lock Screen form of the two lines the cluster
// widget leads with ("Serving", "3/4 hosts up").
//
// It is NOT a second data path and NOT a second opinion. Both signals come
// from the same two routes the cluster widget already reads (dataset 5 of
// `docs/WIDGET_DATA_CONTRACT.md` §3: `GET /v1/autodown/status` for the serving
// state, `GET /v1/cluster/monitor` for the per-node sample), the state word is
// a `ClusterState.rawValue` and nothing else, and a signal the payload did not
// send renders `—`, never `0` (§6 rule 1). Per §7 the only node identity this
// type can carry is the `.NNN` display tail (`MonitorNode.displayLabel`),
// never an address.
//
// Why it is a separate activity from the monitor one
// (`MonitorActivityAttributes`): that answers "how hard is the fleet working"
// — CPU/RAM/GPU per node plus the daemon host. This answers "is the fleet up
// at all" — a state, a tally, and which nodes did not answer. Different
// questions, different content states, and both are allowed on screen at once.
//
// Live Activities are PUSH-ONLY: this type is compiled into BOTH the app (which
// starts/updates/ends it and runs the launch orphan sweep) and the
// `HSCCLiveActivityMonitor` extension (which renders it), so both sides agree
// on the exact activity type and its content state. This extension never
// fetches and this type is never persisted — ActivityKit carries it, the App
// Group does not (§7).
// ---------------------------------------------------------------------------

struct ClusterHealthActivityAttributes: ActivityAttributes {
    /// The state the Dynamic Island / Lock Screen renders: what the app last
    /// learned about the fleet, plus when it last learned it.
    public struct ContentState: Codable, Hashable {
        /// The five signals every headline, tally and colour on this surface is
        /// derived from. A type of its own so the launch sweep and the gate can
        /// take the signals without ActivityKit — see `ClusterHealthOrphanRule`.
        public var signals: ClusterHealthSignals
        /// When the app last pushed. Drives `staleDate`, the age line, and the
        /// orphan decision below.
        public var updatedAt: Date
    }

    /// The most down-node labels a surface draws. The expanded Island's bottom
    /// region is two lines and the Lock Screen row is one, so the view clips as
    /// well as the driver: a pushed ContentState is untrusted input to a
    /// fixed-height region.
    static let downNodeLimit = 2

    /// A token identifying this activity so it can be updated/ended.
    public var healthID: UUID = UUID()
}

// GATE-SLICE-BEGIN: cluster-health-reading-rules
// ---------------------------------------------------------------------------
// The signals, and every number, word and colour this activity can print.
//
// Foundation only — no ActivityKit, no Theme, no SwiftUI — because
// `live_activity_rehydration_check.sh` SLICES this block out of this file and
// compiles it with its macOS harness. That is the whole arrangement: the
// decision table executes the code the app's launch sweep calls and the code
// the extension's views draw, instead of executing a transcription of it that
// could drift while staying green.
//
// Every rule below takes the signals and returns what to draw. The views
// forward here and decide nothing locally, so one pushed state can never render
// as two different answers in two regions — and a read that answered nothing
// can never render as a healthy fleet.
// ---------------------------------------------------------------------------

/// What the app last learned about the fleet. Field-for-field the reduced set
/// the surfaces draw: nothing here is a wire payload, and nothing here can hold
/// an address (§7).
struct ClusterHealthSignals: Codable, Hashable {
    /// The fleet's serving state — a `ClusterState.rawValue`
    /// (`serving` / `waking` / `down` / `unreachable` / `unknown`) and nothing
    /// else. A string, not the enum, because this type is sliced and compiled
    /// without SwiftUI; `stateLabel` is the copy table, and the gate asserts it
    /// still agrees with `ClusterState.label` for all five raw values.
    var servingState: String
    /// Nodes the monitor route listed and sampled cleanly. nil = that route did
    /// not answer, which draws `—`; `0` = it answered and every node failed.
    var nodesUp: Int?
    /// Nodes the route listed but could not sample (`error` non-null). nil = not
    /// told. `0` is a fact, not a gap.
    var nodesDown: Int?
    /// Nodes the route listed at all (`json.hosts.count`). nil = not told. The
    /// tally is printed against this, not against a number we assume.
    var nodesTotal: Int?
    /// The blind-read label (`WidgetDataFailure.shortLabel`, ≤ 18 chars by
    /// contract), or nil when the last push came from a live read.
    var failureLabel: String?
    /// Labels of the nodes that did not answer (`.NNN` tails / hostnames,
    /// never addresses), in the order the route listed them. Empty means either
    /// "nothing is down" (`nodesDown == 0`) or "not told" (`nodesDown == nil`) —
    /// the two render differently, so `nodesDown` stays beside this.
    var downNodes: [String]

    /// A state with no signal in it at all: nothing answered, so there is
    /// nothing to show. Used by the orphan rule and by the empty-state line.
    var isEmpty: Bool {
        if servingState != "unknown" { return false }
        if nodesUp != nil || nodesDown != nil || nodesTotal != nil { return false }
        if let failureLabel, !failureLabel.isEmpty { return false }
        return true
    }
}

/// The colour a state earns. Deliberately an enum, not a `Color`: a `Color`
/// needs SwiftUI and would lock this rule out of the gate. The extension maps
/// a tone to `Theme.Semantic` in one place, so the Lock Screen row and the
/// Island's compact regions cannot disagree about what green means.
enum ClusterHealthTone: String {
    case ok
    case warn
    case bad
    case neutral
}

enum ClusterHealthReading {
    /// The state word. Sentence-case copy for a `ClusterState.rawValue`; an
    /// unrecognised word is `Unknown`, because a state the app does not model
    /// must never be shown as if it were one it does.
    static func stateLabel(_ servingState: String) -> String {
        switch servingState {
        case "serving": return "Serving"
        case "waking": return "Waking"
        case "down": return "Down"
        case "unreachable": return "Can't reach"
        default: return "Unknown"
        }
    }

    /// The up tally: `3/4 up`, or `3 up` when the route named no total, or
    /// `— up` when it did not answer at all. A nil never becomes a number.
    static func tallyText(up: Int?, total: Int?) -> String {
        guard let up else { return "— up" }
        guard let total else { return "\(up) up" }
        return "\(up)/\(total) up"
    }

    /// The down tally. `0 down` is a fact the route stated; `— down` is the gap
    /// where a route did not answer. They are different sentences on purpose.
    static func downText(down: Int?) -> String {
        guard let down else { return "— down" }
        return down == 1 ? "1 down" : "\(down) down"
    }

    /// The down nodes worth naming, clipped to `limit` and with blanks dropped.
    /// Naming which node is down is the difference between a health surface and
    /// a state chip; naming more than a region can hold is how a fixed-height
    /// surface gets truncated mid-label instead of showing a `+n more`.
    static func downNodeList(_ labels: [String], limit: Int) -> [String] {
        guard limit > 0 else { return [] }
        return labels.filter { !$0.isEmpty }.prefix(limit).map { $0 }
    }

    /// `+2 more` when names were dropped, nil when the list is the whole set
    /// (or there is nothing to append to it).
    static func moreText(shown: Int, down: Int?) -> String? {
        guard let down, down > shown, shown >= 0 else { return nil }
        return "+\(down - shown) more"
    }

    /// The headline. The branch is "was a number told", in this order:
    ///   * a number was told → the state word plus that tally. Even under a
    ///     failure label: last-known numbers stay on screen and the `stale ·`
    ///     line carries the failure, which is what keeps a stale surface useful
    ///     instead of blank.
    ///   * no number, but a failure label → the label. A read that answered
    ///     nothing leads with what it knows, not with a state word.
    ///   * no number, no label, and a state the app models → that state alone
    ///     (the node route may have failed while autodown answered).
    ///   * nothing at all → an em dash. Never "Down" (that word is the server's
    ///     answer, not our guess) and never "0/0 up".
    static func headline(_ signals: ClusterHealthSignals) -> String {
        let head = stateLabel(signals.servingState)
        if let up = signals.nodesUp {
            return "\(head) · \(tallyText(up: up, total: signals.nodesTotal))"
        }
        if let failureLabel = signals.failureLabel, !failureLabel.isEmpty { return failureLabel }
        if signals.isEmpty { return "—" }
        return head
    }

    /// The Dynamic Island compact-trailing string: `3/4`, or `3` when the route
    /// named no total, or `!` for a read that produced nothing, or `—` when
    /// nothing was told. Clipped, because the compact region is about four
    /// characters wide.
    ///
    /// A told `up` ALWAYS prints something: a surface that shows `—` while the
    /// app knows three nodes are answering is lying about knowing.
    static func compactTrailing(_ signals: ClusterHealthSignals) -> String {
        guard let up = signals.nodesUp else {
            return (signals.failureLabel?.isEmpty == false) ? "!" : "—"
        }
        if let total = signals.nodesTotal {
            if up > 9 || total > 9 { return "9+" }
            return "\(up)/\(total)"
        }
        return up > 9 ? "9+" : "\(up)"
    }

    /// The colour. Read top to bottom, because the order is the rule:
    ///   1. A blind read is never quiet: bad when it carried no numbers at all,
    ///      warn when it is showing last-known numbers under a failure label.
    ///   2. The fleet's own state beats the node tally — `down` is red and
    ///      `waking` is amber whatever the nodes say, because those words are
    ///      the server's answer about the fleet.
    ///   3. A serving fleet with a node down is amber, not mint: "serving" and
    ///      "everything healthy" are different claims and the surface must not
    ///      collapse them.
    ///   4. A nil `nodesDown` is NOT a warn — a route that did not answer is
    ///      silence, not a fault. Only a counted `down > 0` warns.
    static func tone(_ signals: ClusterHealthSignals) -> ClusterHealthTone {
        if signals.failureLabel?.isEmpty == false {
            return (signals.nodesUp == nil && signals.nodesTotal == nil) ? .bad : .warn
        }
        switch signals.servingState {
        case "down": return .bad
        case "waking": return .warn
        case "serving":
            if let down = signals.nodesDown, down > 0 { return .warn }
            return .ok
        default: return .neutral
        }
    }

    /// How long ago the app last pushed — the honest "this may be old" line.
    /// A clock ahead of this device reads `now`, never a negative age.
    static func pushedAgeText(updatedAt: Date, now: Date) -> String {
        let total = max(0, Int(now.timeIntervalSince(updatedAt)))
        if total < 60 { return "now" }
        let minutes = total / 60
        if minutes < 60 { return "\(minutes)m ago" }
        return String(format: "%dh%02dm ago", minutes / 60, minutes % 60)
    }
}
// GATE-SLICE-END: cluster-health-reading-rules

// GATE-SLICE-BEGIN: cluster-health-orphan-decision
// ---------------------------------------------------------------------------
// The orphan decision — the ONE rule the launch sweep applies to a found
// cluster-health activity.
//
// Foundation only, for the same reason as the block above: the sweep in the app
// target and `live_activity_rehydration_check.sh` read this same text, and the
// gate slices it out of this file and executes it as a decision table. This is
// the difference between this activity and the monitor one, whose sweep asks
// the settings store whether the surface is still on: this rule is pure, so it
// can be executed, and it needs no `WidgetSurfaceKind` case (the shared
// Active/cadence keys are another card's file set — see the PR).
// ---------------------------------------------------------------------------
enum ClusterHealthOrphanRule {
    /// How long a cluster-health activity may go without a push and still be
    /// treated as having a live feeder behind it.
    ///
    /// Six hours, the same budget the running-cards activity uses and for the
    /// same reasons: a Live Activity is only updated by the app process, so a
    /// backgrounded app is at the mercy of `BGAppRefreshTask`, which is
    /// best-effort and can legitimately lag for hours; and the sweep runs at
    /// every launch, which is exactly when a real feeder resumes. The honesty
    /// for an old bubble is not its deletion — it is the age line the views draw
    /// from `updatedAt` and the `staleDate` the driver sets.
    static let maxSilence: TimeInterval = 6 * 60 * 60

    /// True when a found activity can only be an orphan and the sweep must end
    /// it.
    ///
    ///   * `signals.isEmpty` → END. Nothing was ever learned: no state, no
    ///     tally, no failure label. Such a bubble has no content for any region
    ///     to draw, and no real driver ever pushes one — it is what an
    ///     unfinished build or a stale push from another version leaves behind.
    ///   * `now - updatedAt > maxSilence` → END. No legitimate feeder has been
    ///     this quiet.
    ///   * otherwise → KEEP. Note what this does NOT end: `nodesUp == 0` with
    ///     `servingState == "down"` is a fleet that is genuinely down, which is
    ///     the single most useful thing this surface can be showing. A zero that
    ///     the server stated is an answer, not an empty bubble — unlike the
    ///     running-cards activity, where zero rows means there is no work to be
    ///     ambient about.
    static func isOrphan(_ signals: ClusterHealthSignals, updatedAt: Date, now: Date) -> Bool {
        if signals.isEmpty { return true }
        return now.timeIntervalSince(updatedAt) > maxSilence
    }
}
// GATE-SLICE-END: cluster-health-orphan-decision
