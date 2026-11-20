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
// It is the ONE surviving Live Activity (the 2026-11-20 picks): the old
// monitor, wake, running-cards and board-progress activities were removed, and
// their separate questions now live on the Home Screen widgets.
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

    /// The most host lines the LOCK SCREEN draws — the same four the projection
    /// clips a push to, so a Lock Screen with room for them shows every host the
    /// app ever sent. `+n` counts the rest (`hostsMoreText`), against the count
    /// the route itself named.
    static let hostLineLimit = ClusterHealthProjection.hostLineLimit

    /// The most host lines the EXPANDED Island's bottom region draws. Declared on
    /// `ClusterHealthProjection` (the slice the gate compiles) and re-exported
    /// here, so the view's budget and the gate's assertion cannot drift apart.
    static let islandHostLineLimit = ClusterHealthProjection.islandHostLineLimit

    /// The one Live Activity is operator-started and may run for eight hours;
    /// the driver and the launch sweep both use this budget.
    static let maxRuntime: TimeInterval = ClusterHealthOrphanRule.maxRuntime

    /// When the operator started this watch. Optional so an activity left by
    /// an older build still decodes; the driver adopts it and starts the
    /// eight-hour clock from its own launch in that case.
    public var startedAt: Date? = nil

    /// A token identifying this activity so it can be updated/ended.
    public var healthID: UUID = UUID()

    init(startedAt: Date? = nil) {
        self.startedAt = startedAt
    }
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
    /// The first model the serving table names (`serving[].model`, shortened
    /// the way the CLI does). Nil when the route sent no serving section.
    var modelLabel: String? = nil
    /// The CLI total "Gen t/s" — the sum of `serving[].gen_tps`. Nil when no
    /// row had a rate yet (the CLI prints "-" on a first sample).
    var genTps: Double? = nil

    // MARK: The load signals (t_b009f32d)
    //
    // Everything below comes from the SAME `/v1/cluster/monitor` read the tally
    // above is counted from — this activity does not open a third route — and
    // every cell string is `hscc cluster monitor`'s own, formatted by
    // `ClusterMonitorViewModel` and never re-derived here. `nil` on all of them
    // means the monitor read did not answer (or the operator switched the tally
    // off), which draws `—` and never `0` (§6 rule 1); a `0` is an answer.

    /// One line per host: the CLI's `GPU%` / `GPU Temp` / `GPU Power` cells plus
    /// the two verdicts that paint them. nil = not told; a non-nil list is never
    /// empty (an empty host list is the down-fleet silence §4.4, which projects
    /// to nil here exactly as it does in `ClusterNodeTally.project`).
    ///
    /// Declared `var … ? = nil`, never `let … = nil`: an immutable property with
    /// an initial value is silently dropped from the synthesized `CodingKeys`
    /// (Swift warns "immutable property will not be decoded because it is
    /// declared with an initial value which cannot be overwritten"), so the
    /// extension would draw `—` for data the app pushed. Measured, not assumed.
    var hostLines: [ClusterHealthHostLine]? = nil
    /// Total GPU draw: the sum of the hosts that reported a wattage. nil when no
    /// host reported one — a fleet whose nodes never answered draws `—`, not
    /// "0 W", which would be a fleet at idle.
    var totalPowerW: Double? = nil
    /// The worst GPU-power verdict among the hosts that reported one, against
    /// each part's own `gpu_power_limit_w`. nil when nothing was reported;
    /// `.unknown` when wattages came in but no limit did (the view-model's rule:
    /// no limit read is no verdict, never `.ok`).
    var powerLevel: ClusterMetricLevel? = nil
    /// The busiest GPU the fleet reported (`max gpu_util_pct`), for the Island's
    /// compact and minimal forms. nil = no host told.
    var peakGpuPct: Double? = nil
    /// The hottest GPU the fleet reported (`max gpu_temp_c`). nil = no host told.
    var peakGpuTempC: Double? = nil
    /// The verdict for `peakGpuTempC`, taken from the row that HOLDS the peak —
    /// so the compact's colour is the view-model's threshold applied to the
    /// value it is drawing, not a second threshold declared somewhere else.
    var peakGpuTempLevel: ClusterMetricLevel? = nil
    /// The serving table's `total (n)` Run column. nil = no serving section.
    var servingRunning: Int? = nil
    /// The serving table's `total (n)` Wait column. nil = no serving section.
    var servingWaiting: Int? = nil
    /// The daemon machine's own state from `/v1/daemon/host`. nil = not told.
    var daemonRunning: Bool? = nil
    /// Whether autodown is armed, from `/v1/autodown/status`. nil = not told;
    /// `false` is the operator's "autodown off", never a gap.
    var autodownEnabled: Bool? = nil

    /// A state with no signal in it at all: nothing answered, so there is
    /// nothing to show. Used by the orphan rule and by the empty-state line.
    ///
    /// The load signals count too (t_b009f32d): a push that carried host lines
    /// or serving totals has content for the Lock Screen to draw even when the
    /// state word is missing, and the launch sweep must not end a bubble that
    /// has something true on it.
    var isEmpty: Bool {
        if servingState != "unknown" { return false }
        if nodesUp != nil || nodesDown != nil || nodesTotal != nil { return false }
        if modelLabel != nil || genTps != nil { return false }
        if let failureLabel, !failureLabel.isEmpty { return false }
        if hostLines?.isEmpty == false { return false }
        if totalPowerW != nil || powerLevel != nil { return false }
        if peakGpuPct != nil || peakGpuTempC != nil || peakGpuTempLevel != nil { return false }
        if servingRunning != nil || servingWaiting != nil { return false }
        return true
    }
}

/// One host's line on this surface.
///
/// The strings are `host_table()`'s cells, taken from `CLIMonitorHostRow` — the
/// CLI's rounding and its ` C` / ` W` units, `°` nowhere — and the levels are
/// the view-model's own threshold verdicts (`ClusterMetricLevel` names whose
/// rules they are). Nothing here re-formats a number, which is the whole point
/// of the type: the Lock Screen and the expanded Island draw the same cells the
/// CLI and the `systemMedium` tile draw, so they cannot print `87°C` where the
/// CLI prints `87 C`.
///
/// `Codable` because ActivityKit carries it between the app process and the
/// extension; `Hashable` because `ContentState` is `Hashable`.
struct ClusterHealthHostLine: Codable, Hashable {
    /// `CLIMonitorHostRow.displayLabel` — a `.NNN` tail or the reported
    /// hostname, never the address the CLI's own Host column carries (§7).
    let label: String
    /// The `GPU%` cell. No verdict is attached to it on purpose: `host_table`
    /// prints GPU% in the default style and the view-model gives that column no
    /// level, so a surface that colours it is no longer showing the CLI's rule.
    let gpuPct: String
    /// The `GPU Temp` cell, with the CLI's ` C` unit.
    let gpuTemp: String
    /// The `GPU Power` cell, with the CLI's ` W` unit.
    let gpuPower: String
    /// The temp verdict: `.unknown` when the host reported no temperature.
    let gpuTempLevel: ClusterMetricLevel
    /// The power verdict against this host's own limit: `.unknown` when either
    /// the wattage or the limit was not read.
    let gpuPowerLevel: ClusterMetricLevel
    /// The host did not answer at all, so its cells are the CLI's dashes and
    /// the line is a fact about the fleet's health, not a load reading.
    let isDown: Bool
    /// The numeric values behind the cells, for the A2 layouts. Optional and
    /// defaulted so a payload an older build pushed still decodes.
    var gpuTempC: Double? = nil
    var gpuUtilPct: Double? = nil
}

/// One node column on the A2 Lock Screen row: the short name the spec draws
/// (`S1`…), the temperature and the utilisation. Built from the SAME
/// `ClusterHealthHostLine` rows the driver pushes, so the column can never
/// show a number the CLI did not.
struct ClusterHealthNodeColumn: Equatable, Identifiable {
    let shortName: String
    let tempC: Double?
    let utilPct: Double?
    let tempLevel: ClusterMetricLevel
    let isDown: Bool

    var id: String { shortName }

    var tempText: String {
        guard let tempC else { return "—" }
        return String(format: "%.0f°", tempC)
    }

    var utilText: String {
        guard let utilPct else { return "—" }
        return String(format: "%.0f%%", utilPct)
    }

    var detailText: String { "\(shortName) · \(utilText)" }
}

/// The one metric the Dynamic Island's compact and minimal forms lead with,
/// plus what may sit beside it.
///
/// A struct of already-decided strings rather than a switch the view reorders:
/// the compact region is about four characters wide and the minimal region is
/// smaller still, so WHICH number wins is the rule, and it must be one rule the
/// gate executes rather than three `if`s in two view files.
struct ClusterHealthGlance: Equatable {
    /// Which rule produced `text`. It is also the colour rule: a `heat` glance
    /// carries its own verdict, everything else is painted with the fleet's
    /// tone, so a load number never goes red for a reason the rest of the
    /// surface disagrees about.
    enum Kind: String {
        /// The busiest GPU's utilisation — the fleet-level "how hard is it
        /// working" number, and the one this surface leads with when told.
        case load
        /// The hottest GPU's temperature — what the compact falls back to when
        /// no host reported a utilisation.
        case heat
        /// No load was told, so the node tally holds the slot (the shape this
        /// surface had before the load signals existed).
        case tally
        /// Nothing was told: `!` for a read that answered nothing, `—` for a
        /// push with no signal in it at all.
        case mark
    }

    /// What to draw. Always non-empty: every kind has a string, because a
    /// compact region that draws nothing reads as a dead surface.
    let text: String
    let kind: Kind
    /// The verdict for a `heat` glance; nil for every other kind, which the
    /// view paints with the tone instead.
    let level: ClusterMetricLevel?
    /// The node tally beside the primary, when the primary is a load or heat
    /// number and a tally was in fact told. nil means "no room for a secondary
    /// in this state" — the compact region must never repeat its own number.
    let secondary: String?
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

    /// The serving line the CLI prints beside the host table: model and the
    /// total generation rate. Both are `serving[]` fields; a row that has no
    /// rate yet contributes nothing, so this is nil rather than "0.0 t/s".
    static func feedText(model: String?, genTps: Double?) -> String? {
        var parts: [String] = []
        if let model, !model.isEmpty { parts.append(model) }
        if let genTps { parts.append(String(format: "%.1f t/s", genTps)) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    // MARK: The load surfaces (t_b009f32d)

    /// The host lines a region draws, clipped to `limit` with blanks dropped —
    /// the same shape `downNodeList` enforces, because a pushed ContentState is
    /// untrusted input to a fixed-height region and the view clips as well as
    /// the projection.
    static func hostLineList(_ lines: [ClusterHealthHostLine]?, limit: Int) -> [ClusterHealthHostLine] {
        guard limit > 0, let lines else { return [] }
        return lines.filter { !$0.label.isEmpty }.prefix(limit).map { $0 }
    }

    /// `+2 more` when the region had no room for the rest of the fleet. Delegates
    /// to `moreText` rather than repeating its arithmetic: the clip rule is one
    /// rule, whether what was clipped was down-node names or host rows, and this
    /// call site only has to say which count it means (`listed` = hosts the route
    /// named). A table that stops at four hosts without this reads as "the fleet
    /// is four hosts".
    static func hostsMoreText(shown: Int, listed: Int?) -> String? {
        moreText(shown: shown, down: listed)
    }

    /// The total-draw string for the Lock Screen and the expanded Island: the
    /// sum of the hosts that reported a wattage, in the CLI's own power format
    /// (two decimals + ` W`), so the total and the per-host cells beside it are
    /// the same quantity in the same unit.
    ///
    /// nil in, nil out — `CLIMonitorFormat.cell` renders a nil as `-`, which is
    /// the CLI's table glyph and not this surface's: every other "not told" on
    /// this activity is an em dash, and a `-` beside `— down` would be two words
    /// for one condition.
    static func totalPowerText(_ totalW: Double?) -> String? {
        guard let totalW else { return nil }
        return CLIMonitorFormat.cell(totalW, places: 2, suffix: " W")
    }

    /// The serving totals line: `6 run · 0 wait`. A `0` is the CLI's stated
    /// answer for a settled queue; a nil means the serving section was not sent,
    /// and that part is simply absent — it never becomes `—` beside a real
    /// number, and never becomes `0` beside nothing.
    static func servingLoadText(running: Int?, waiting: Int?) -> String? {
        var parts: [String] = []
        if let running { parts.append("\(running) run") }
        if let waiting { parts.append("\(waiting) wait") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// What the Island's compact and minimal forms lead with. The order IS the
    /// rule, and it is the order the card asked for:
    ///
    ///   1. the busiest GPU's utilisation — "how hard is the fleet working",
    ///      which is the one number a four-character region can act on;
    ///   2. else the hottest GPU's temperature — a GPU that never reported a
    ///      utilisation still said how hot it is, and that is the same question
    ///      answered with the data that exists;
    ///   3. else the node tally, which is what this surface led with before the
    ///      load signals existed and stays the fallback rather than an em dash;
    ///   4. else `!` / `—`, the two gap marks `compactTrailing` already owns.
    ///
    /// `load` is a MAXIMUM, not a sum or a mean: a summed utilisation would
    /// exceed 100% and read as a bug, and a mean hides a pegged GPU behind three
    /// idle ones — which is the one thing a glance surface must not do.
    ///
    /// The secondary is the node tally, and only when a tally was actually told:
    /// a compact region that printed the metric twice (as a number, then as `—`)
    /// is worse than one that printed it once.
    static func glance(_ signals: ClusterHealthSignals) -> ClusterHealthGlance {
        let tally = compactTrailing(signals)
        let secondary = signals.nodesUp != nil ? tally : nil
        if let peak = signals.peakGpuPct {
            return ClusterHealthGlance(text: CLIMonitorFormat.cell(peak, places: 0, suffix: "%"),
                                       kind: .load, level: nil, secondary: secondary)
        }
        if let peak = signals.peakGpuTempC {
            return ClusterHealthGlance(text: CLIMonitorFormat.cell(peak, places: 0, suffix: " C"),
                                       kind: .heat, level: signals.peakGpuTempLevel,
                                       secondary: secondary)
        }
        return ClusterHealthGlance(text: tally,
                                   kind: signals.nodesUp != nil ? .tally : .mark,
                                   level: nil, secondary: nil)
    }

    /// The worst of several verdicts, for a fleet-level line that has to pick one
    /// colour. Ranked rather than "any warn wins", so an added case in
    /// `ClusterMetricLevel` breaks the build here instead of being silently
    /// unranked: `.unknown` is the absence of a verdict and loses to everything,
    /// which is why a fleet that reported watts but no limits is grey and not
    /// green.
    static func worstLevel(_ levels: [ClusterMetricLevel]) -> ClusterMetricLevel? {
        levels.max(by: { rank($0) < rank($1) })
    }

    private static func rank(_ level: ClusterMetricLevel) -> Int {
        switch level {
        case .critical: return 4
        case .warn: return 3
        case .ok: return 2
        case .dim: return 1
        case .unknown: return 0
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

    // MARK: — Data clock (stale snapshots keep their real time)

    /// The real time a stale snapshot belongs to: `now` less the age the
    /// snapshot store reported. Never `now` — that is the defect this helper
    /// exists to make impossible to re-introduce.
    static func snapshotDate(now: Date = Date(), ageMinutes: Int) -> Date {
        now.addingTimeInterval(-Double(max(0, ageMinutes)) * 60)
    }

    /// The oldest of the reads a push draws, so one stale route cannot be
    /// presented as newer than it is. nil when no read produced a date.
    static func dataDate(_ dates: [Date?]) -> Date? {
        dates.compactMap { $0 }.min()
    }

    // MARK: — A2 Cluster watch (2026-11-20)

    /// The data clock, `HH:mm`, in the device's own time zone. The watch is a
    /// live surface, so its as-of is a clock time rather than a relative age.
    static func asOfText(_ date: Date) -> String {
        asOfFormatter.string(from: date)
    }

    private static let asOfFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        return formatter
    }()

    /// `CLUSTER · <model>` (or just `CLUSTER` when no serving row named one).
    static func watchHeader(model: String?) -> String {
        guard let model, !model.isEmpty else { return "CLUSTER" }
        return "CLUSTER · \(model)"
    }

    /// The compact-leading tally: `4/4`, clipped to the four-character region.
    static func watchTally(_ signals: ClusterHealthSignals) -> String {
        compactTrailing(signals)
    }

    /// The compact-trailing pair the operator picked: `72° · 231 t/s`.
    static func watchTrailing(_ signals: ClusterHealthSignals) -> String {
        let temp = signals.peakGpuTempC.map { String(format: "%.0f°", $0) } ?? "—"
        let tok = signals.genTps.map { String(format: "%.0f t/s", $0) } ?? "— t/s"
        return "\(temp) · \(tok)"
    }

    /// The minimal region: the hottest temperature, else the tally when no
    /// temperature was told. Never empty, so the region cannot read as dead.
    static func watchMinimal(_ signals: ClusterHealthSignals) -> String {
        if let temp = signals.peakGpuTempC { return String(format: "%.0f°", temp) }
        return compactTrailing(signals)
    }

    /// The footer: `N tok/s · daemon OK · autodown off`. Every part is drawn
    /// from what the routes actually said; an absent part is `—`, not a zero.
    static func watchFooter(_ signals: ClusterHealthSignals) -> String {
        let tok = signals.genTps.map { String(format: "%.0f tok/s", $0) } ?? "— tok/s"
        let daemon: String
        switch signals.daemonRunning {
        case .some(true): daemon = "daemon OK"
        case .some(false): daemon = "daemon down"
        case .none: daemon = "daemon —"
        }
        let autodown: String
        switch signals.autodownEnabled {
        case .some(true): autodown = "autodown on"
        case .some(false): autodown = "autodown off"
        case .none: autodown = "autodown —"
        }
        return "\(tok) · \(daemon) · \(autodown)"
    }

    /// The four node columns (or fewer when the route listed fewer), in the
    /// route's own order. The short name is positional because the A2 layout is
    /// a four-column row; the identity below it stays the CLI's own util cell.
    static func nodeColumns(_ lines: [ClusterHealthHostLine]?,
                            limit: Int = 4) -> [ClusterHealthNodeColumn] {
        guard limit > 0, let lines else { return [] }
        return lines.prefix(limit).enumerated().map { index, line in
            ClusterHealthNodeColumn(shortName: "S\(index + 1)",
                                    tempC: line.gpuTempC,
                                    utilPct: line.gpuUtilPct,
                                    tempLevel: watchTempLevel(line.gpuTempC),
                                    isDown: line.isDown)
        }
    }

    /// The A2 temperature bands from the operator's picks: below 70 ok,
    /// 70–74 warn, 75 and above bad. Deliberately not the host table's
    /// CPU/GPU bands — this surface draws the operator's four-level glance.
    static func watchTempLevel(_ celsius: Double?) -> ClusterMetricLevel {
        guard let celsius else { return .unknown }
        if celsius >= 75 { return .critical }
        if celsius >= 70 { return .warn }
        return .ok
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
    /// Eight hours: the one Live Activity is operator-started and the card's
    /// contract ends it after eight hours. A Live Activity is only updated by
    /// the app process, so a backgrounded app is at the mercy of
    /// `BGAppRefreshTask`, which is best-effort and can legitimately lag for
    /// hours; and the sweep runs at every launch, which is exactly when a real
    /// feeder resumes. The honesty for an old bubble is not its deletion — it
    /// is the age line the views draw from `updatedAt` and the `staleDate` the
    /// driver sets.
    static let maxSilence: TimeInterval = 8 * 60 * 60
    static let maxRuntime: TimeInterval = maxSilence

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

// GATE-SLICE-BEGIN: cluster-health-projection-rules
// ---------------------------------------------------------------------------
// The projection — how the two sanctioned reads become `ClusterHealthSignals`.
//
// The driver (`ClusterHealthActivityDriver`, app target) owns WHEN to read and
// WHETHER to push; it owns no rule about WHAT the reads mean. Everything the
// poll-and-push loop could get wrong about a degraded fleet lives here instead,
// in the same Foundation-only shape the other two blocks have, so
// `live_activity_rehydration_check.sh` slices it out and executes it as a
// decision table: a fleet that is down, a route that degraded to `{speak}`, a
// read that timed out, a monitor whose host list has two errors in it, an
// operator who turned the tally off.
//
// Two reads feed this, both already on the §3 allowlist and both reached through
// `WidgetData.read(using:lastKnown:_:)` so stale-while-error is the shared rule
// rather than a second one: `GET /v1/autodown/status` for the serving state and
// `GET /v1/cluster/monitor` for the tally.
//
// The failure parameters are already-mapped `shortLabel` strings, not
// `WidgetDataFailure`. That is deliberate, twice over: this file is compiled
// into the Live Activity extension, which renders and must never carry the HTTP
// client — and a sliced block whose inputs are `String?` needs nothing but
// `SharedModels.swift` to compile, which the harness already provides.
//
// ONE more file since t_b009f32d: the host rows come from
// `ClusterMonitorViewModel`, so the harness compiles THAT too. It is
// Foundation-only for exactly that reason, and naming it is the price of not
// transcribing the CLI's formatting into this file — the alternative is a
// second copy of the ` C` / ` W` / `-` rules, which is the drift this repo has
// deleted mirrors twice to avoid.
// ---------------------------------------------------------------------------

enum ClusterHealthProjection {
    /// How many host lines one push carries. Four is the Lock Screen's budget and
    /// it is not a new number: `CLIMonitorTableReading.mediumRowLimit` caps the
    /// `systemMedium` tile at four host rows, and `FleetProjection.text(for:)` has
    /// always capped the medium surface at four — the same vertical budget for
    /// the same kind of table. The rest of the fleet is counted by
    /// `hostsMoreText` against `nodesTotal`, never dropped in silence, and the
    /// fleet-level numbers below are always computed over EVERY host, so a clip
    /// can never change what the compact region claims.
    static let hostLineLimit = 4

    /// The most host lines the EXPANDED Island's bottom region draws, and the
    /// reason it is declared here rather than on the attributes type: this file
    /// is the slice the gate can compile (no ActivityKit), and a budget the view
    /// keeps to itself is a budget the gate can only transcribe. The attributes
    /// type re-exports this one, exactly as it re-exports `hostLineLimit` above.
    ///
    /// Smaller than the Lock Screen's on purpose — the expanded Island is a few
    /// lines tall above the notch's own content, and a fifth host is worth less
    /// there than the queue and the down-node names beside it. The clip stays
    /// honest because `hostsMoreText` counts what it hid.
    static let islandHostLineLimit = 3

    /// The serving state word, from the autodown read's `state`.
    ///
    /// A nil `response` means that read produced NOTHING at all — neither a
    /// live answer nor a last-known one — which is the only honest route to
    /// `unreachable`. A response that carried no recognisable `state` is
    /// `unknown` instead: the server answered, and guessing a state the server
    /// did not state is how a surface starts lying about a fleet.
    ///
    /// Stale counts as answered: `WidgetData.read` hands back the last-known
    /// payload with the failure as its `reason`, so a fleet that was serving
    /// when we last reached it still reads `serving`, and the failure label the
    /// driver passes beside it is what tells the operator it is not live news.
    static func servingState(from autodown: AutodownStatusResponse?) -> String {
        guard let autodown else { return "unreachable" }
        switch autodown.state {
        case "up": return "serving"
        case "waking": return "waking"
        case "down": return "down"
        default: return "unknown"
        }
    }

    /// The one label a blind read earns. The state read wins when both broke:
    /// "is the fleet up" is the headline this surface exists to answer, so the
    /// failure that took the headline away is the story, not the failure that
    /// took the denominator.
    ///
    /// A nil monitor label when the operator turned the tally off is not a
    /// courtesy — it is the reason the driver does not read the monitor at all
    /// in that case. A failure of a read whose data this surface is not drawing
    /// must not become the sentence the surface leads with.
    static func failureLabel(autodownLabel: String?,
                             monitorLabel: String?) -> String? {
        autodownLabel ?? monitorLabel
    }

    /// Assemble the signals one push carries.
    ///
    ///   - `showNodeTally`: the "nodes" section. Off means the tally is NOT
    ///     told — every count goes nil and `downNodes` empties — and the
    ///     monitor's failure label goes with them. It is never `0`: a section
    ///     the operator switched off is not a fleet with no nodes answering,
    ///     and the reading rules above already draw a nil as `—`.
    ///   - The tally itself comes from `ClusterNodeTally.project`, the same
    ///     projection the cluster widget runs, so the two surfaces cannot
    ///     disagree about how many nodes are up.
    ///   - The LOAD signals (t_b009f32d) ride the same read and the same
    ///     section switch: they are host data, so a switched-off tally nils them
    ///     exactly as it nils the counts. The serving totals are the one
    ///     exception and are deliberately NOT gated by it — the serving state is
    ///     this surface's headline and already has no section toggle (that is
    ///     why `modelLabel` and `genTps` were never gated either), and they come
    ///     from `ServingSummary`, the same projection the CLI's `total (n)` row
    ///     is built from.
    static func signals(autodown: AutodownStatusResponse?,
                        autodownLabel: String?,
                        monitor: MonitorResponse?,
                        monitorLabel: String?,
                        showNodeTally: Bool,
                        daemonRunning: Bool? = nil) -> ClusterHealthSignals {
        let tally = showNodeTally ? ClusterNodeTally.project(monitor) : .noAnswer
        let serving = ServingSummary(rows: monitor?.serving)
        // ONE projection for every host number on this surface: the same
        // `ClusterMonitorViewModel` the widget tiles draw, so a cell here is the
        // CLI's cell and not a re-derivation of it.
        let rows = showNodeTally ? (ClusterMonitorViewModel.project(monitor)?.hostRows ?? []) : []
        // Read ONCE: the number and its colour come from the same row, so they
        // cannot be taken from two different hosts by two separate calls.
        let hottest = peakGpuTemp(from: rows)
        return ClusterHealthSignals(
            servingState: servingState(from: autodown),
            nodesUp: tally.nodesUp,
            nodesDown: tally.nodesDown,
            nodesTotal: tally.nodesTotal,
            failureLabel: failureLabel(autodownLabel: autodownLabel,
                                       monitorLabel: showNodeTally ? monitorLabel : nil),
            downNodes: tally.downLabels,
            modelLabel: serving.modelNames.first,
            genTps: serving.genTps,
            hostLines: hostLines(from: rows, limit: hostLineLimit),
            totalPowerW: totalPowerW(from: rows),
            powerLevel: powerLevel(from: rows),
            peakGpuPct: peakGpuPct(from: rows),
            peakGpuTempC: hottest?.tempC,
            peakGpuTempLevel: hottest?.level,
            servingRunning: serving.running,
            servingWaiting: serving.waiting,
            // A last-known autodown payload still describes the same armed
            // state, so its own `enabled` rides beside its state word.
            daemonRunning: daemonRunning,
            autodownEnabled: autodown?.enabled)
    }

    // MARK: The load projection (t_b009f32d)

    /// The host lines a push carries, in the route's own order, clipped to
    /// `limit`.
    ///
    /// `nil` (not `[]`) when there are no host rows at all: `ClusterNodeTally`
    /// already ruled that a host list which is missing or empty is the
    /// powered-off fleet's silence (§4.4), and a surface that drew a zero-row
    /// table for that would be claiming a fleet of no hosts.
    ///
    /// `isDown` reads the view-model's `error`, which is set exactly when
    /// `host_table` took its error branch — i.e. when every cell of this row is
    /// a `-`. A host that reported an error AND a sample is a normal row on the
    /// CLI's authority, and is drawn as one here.
    static func hostLines(from rows: [CLIMonitorHostRow], limit: Int) -> [ClusterHealthHostLine]? {
        guard !rows.isEmpty, limit > 0 else { return nil }
        return rows.prefix(limit).map { row in
            ClusterHealthHostLine(label: row.displayLabel,
                                  gpuPct: row.gpuPct,
                                  gpuTemp: row.gpuTemp,
                                  gpuPower: row.gpuPower,
                                  gpuTempLevel: row.gpuTempLevel,
                                  gpuPowerLevel: row.gpuPowerLevel,
                                  isDown: row.error != nil,
                                  gpuTempC: row.gpuTempC,
                                  gpuUtilPct: row.gpuUtilPct)
        }
    }

    /// The fleet's total GPU draw: the sum over the hosts that reported one.
    ///
    /// nil when NO host reported a wattage, which draws `—`. A host whose
    /// sample says 0 W contributes a real zero (an idle node is an answer), and
    /// a host that never sampled contributes nothing at all — so `nil` means
    /// "nobody told us", never "the fleet draws nothing".
    static func totalPowerW(from rows: [CLIMonitorHostRow]) -> Double? {
        let watts = rows.compactMap { $0.gpuPowerW }
        return watts.isEmpty ? nil : watts.reduce(0, +)
    }

    /// The colour of that total: the worst verdict among the hosts that
    /// reported a wattage, each measured against its own `gpu_power_limit_w`.
    /// `.unknown` when watts arrived but no limit did — the view-model gives a
    /// power reading with no limit no verdict, and this surface must not invent
    /// one by painting it green.
    static func powerLevel(from rows: [CLIMonitorHostRow]) -> ClusterMetricLevel? {
        let reported = rows.filter { $0.gpuPowerW != nil }
        guard !reported.isEmpty else { return nil }
        return ClusterHealthReading.worstLevel(reported.map { $0.gpuPowerLevel })
    }

    /// The busiest GPU in the fleet, over EVERY host — including the ones the
    /// clip left out, which is why this is stored rather than read off
    /// `hostLines`: a compact region must not change its answer because the
    /// Lock Screen ran out of vertical room.
    static func peakGpuPct(from rows: [CLIMonitorHostRow]) -> Double? {
        rows.compactMap { $0.gpuUtilPct }.max()
    }

    /// The hottest GPU and the verdict that belongs to it, as a pair on purpose:
    /// the verdict is taken from the row HOLDING the peak, so the compact
    /// region's colour is `CLIMonitorHostRow.gpuTempLevel` applied to the value
    /// it is drawing rather than a second threshold table. A pair, so a caller
    /// cannot take the number from the hottest host and the colour from another.
    static func peakGpuTemp(from rows: [CLIMonitorHostRow]) -> (tempC: Double, level: ClusterMetricLevel)? {
        let reporting = rows.compactMap { row -> (Double, ClusterMetricLevel)? in
            guard let temp = row.gpuTempC else { return nil }
            return (temp, row.gpuTempLevel)
        }
        guard let hottest = reporting.max(by: { $0.0 < $1.0 }) else { return nil }
        return (hottest.0, hottest.1)
    }
}
// GATE-SLICE-END: cluster-health-projection-rules
