import Foundation

// ---------------------------------------------------------------------------
// The host tally of GET /v1/cluster/monitor — ONE projection, every surface.
//
// Two surfaces ask the same question of the same payload: "how many nodes did
// the monitor list, and how many of them answered?" The cluster widget asked it
// first (`ClusterFleet`, t_56b910c8); the cluster-health Live Activity asks it
// too (`ClusterHealthActivityDriver`, t_f2867fb3). Two hand-written loops over
// the same `hosts` array is how one of those surfaces ends up saying a fleet is
// healthy while the other says it is degraded, so the loop lives here and both
// call it.
//
// It is deliberately NOT inside `ClusterHealthActivity.swift`: that file imports
// ActivityKit, so it cannot be compiled by a macOS harness at all — only its
// `GATE-SLICE` blocks can be sliced out and executed. This file is
// Foundation-only and depends on nothing but `SharedModels.swift`, which is what
// lets BOTH `cluster_widget_check.sh` (the widget's gate) and
// `live_activity_rehydration_check.sh` (the activity's) execute the same code
// the shipped surfaces run.
//
// The two predicates are not re-decided here either: `MonitorNode.isUp`
// (`error == nil`) and `MonitorNode.displayLabel` (`.NNN` tail, never an
// address, §7) are the app-wide rules in `SharedModels.swift`, and this file
// only counts and filters by them.
// ---------------------------------------------------------------------------

/// What `/v1/cluster/monitor` says about the FLEET's node health, reduced to a
/// tally plus the labels of the nodes that did not answer.
struct ClusterNodeTally: Equatable {
    /// Hosts the route listed and that answered (`error` nil). nil = the read
    /// gave no host list at all, which is NOT the same as `0`.
    let nodesUp: Int?
    /// Hosts the route listed but could not sample (`error` non-nil). `0` is a
    /// fact the route stated; nil is silence.
    let nodesDown: Int?
    /// Hosts the route listed at all, errored ones included — the denominator,
    /// taken from the payload rather than guessed.
    let nodesTotal: Int?
    /// Display labels of the nodes that did not answer, in the order the route
    /// listed them (`.NNN` tails via `MonitorNode.displayLabel`, never an
    /// address — §7).
    let downLabels: [String]

    /// "The read told us nothing about nodes." Every field nil, no labels.
    static let noAnswer = ClusterNodeTally(nodesUp: nil, nodesDown: nil,
                                           nodesTotal: nil, downLabels: [])

    /// Project the wire payload.
    ///
    /// `nil` response (the read failed) and a response that carried NO host
    /// list both answer `noAnswer`, for the same reason and by the same rule the
    /// cluster widget already runs (`ClusterFleet.init(projecting:)`): when the
    /// fleet is powered off the route answers 38 B with no `json` key (§4.4),
    /// and "no data" must never be drawn as "four hosts, none of them up"
    /// (§6 rule 1). A host list that exists but is empty is the same silence —
    /// an empty denominator is not a fact an operator can act on, and it is the
    /// shape `ClusterFleet` has always treated as no-answer.
    static func project(_ response: MonitorResponse?) -> ClusterNodeTally {
        guard let hosts = response?.json?.hosts, !hosts.isEmpty else { return .noAnswer }
        // ONE predicate, read twice: `down` is the complement of `isUp`, so this
        // file cannot disagree with itself about what "up" means.
        let down = hosts.filter { !$0.isUp }
        return ClusterNodeTally(nodesUp: hosts.count - down.count,
                                nodesDown: down.count,
                                nodesTotal: hosts.count,
                                downLabels: down.map { $0.displayLabel })
    }
}
