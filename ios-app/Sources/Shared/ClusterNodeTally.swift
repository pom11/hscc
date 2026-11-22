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
    /// Hosts the route listed but could not sample (`error` non-nil), MINUS the
    /// ones the operator parked. A parked machine is a choice, not a fault, and
    /// the overlay keeps its `error` — so without this subtraction a pause
    /// counts as a down node and every warn tone downstream fires on it.
    let nodesDown: Int?
    /// Hosts the route listed at all, errored ones included — the denominator,
    /// taken from the payload rather than guessed.
    let nodesTotal: Int?
    /// Nodes the pause overlay holds, as the payload states them. This is the
    /// union of the rows the route tagged and the top-level `paused_nodes` set,
    /// because a parked host can be absent from the frame (sparkrun does not
    /// list a machine whose workloads are stopped) and an absent row is a row
    /// there is nothing to tag. `0` = the route named no parked node; nil = the
    /// read said nothing about nodes at all.
    let nodesPaused: Int?
    /// Display labels of the nodes that did not answer, in `FleetNodeOrder`'s
    /// order — by last octet, numerically — via `MonitorNode.displayLabel`,
    /// never an address (§7). Parked nodes are NOT in here: they are in
    /// `pausedLabels`.
    let downLabels: [String]
    /// Display labels of the parked nodes, same `FleetNodeOrder` order and same
    /// §7 rule as `downLabels` — a last octet, or the reported short name when
    /// the payload carries no IPv4 for that machine. The pause record's own host
    /// string is never drawn: it is an address the moment the operator paused by
    /// IP.
    let pausedLabels: [String]

    /// "The read told us nothing about nodes." Every field nil, no labels.
    static let noAnswer = ClusterNodeTally(nodesUp: nil, nodesDown: nil,
                                           nodesTotal: nil, nodesPaused: nil,
                                           downLabels: [], pausedLabels: [])

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
        // file cannot disagree with itself about what "up" means. The pause
        // check is asked FIRST on purpose — `MonitorNode.isPaused` is the row's
        // own tag and a parked row keeps its `error`, so a `down` filter that
        // ran `isUp` alone would count a parked machine as broken.
        let paused = hosts.filter { $0.isPaused }
        let pausedKeys = Set(paused.compactMap { PauseContract.nodeKey($0.host) })
        // The top-level set adds the parked nodes the frame never listed. Keyed
        // by the route's own identity rule (`PauseContract.nodeKey`, the mirror
        // of the server's `_node_key`), never by a guess about addresses.
        let extra = (response?.paused_nodes ?? [])
            .filter { $0.isPaused }
            .filter { ($0.key.map { !pausedKeys.contains($0) }) ?? true }
        let down = hosts.filter { !$0.isUp && !$0.isPaused }
        // Both label lists come out in `FleetNodeOrder`'s order — by last octet,
        // numerically — not in the order sparkrun happened to put them in the
        // frame. These are the lines a tile names machines by ("down: 9, 10"),
        // so they read in the same order every other node list in the app does.
        let downLabels = FleetNodeOrder.sortedByOctet(down,
                                                     label: { $0.displayLabel })
            .map { $0.displayLabel }
        let pausedLabels = FleetNodeOrder.sortedByOctet(
            paused.map { $0.displayLabel }
                + extra.compactMap { FleetPausedNode(entry: $0, in: hosts)?.label },
            label: { $0 })
        return ClusterNodeTally(nodesUp: hosts.filter { $0.isUp }.count,
                                nodesDown: down.count,
                                nodesTotal: hosts.count,
                                nodesPaused: paused.count + extra.count,
                                downLabels: downLabels,
                                pausedLabels: pausedLabels)
    }
}

// ---------------------------------------------------------------------------
// The fleet-level judgement: how many nodes a surface may COUNT, once the
// machines the operator parked are out of the arithmetic.
//
// `ClusterC1` wrote this rule first (t_983f177e) and its headline depends on
// it. CarPlay and the macOS menu bar both print an `N/M up` line of their own
// and were still feeding it the route's raw `nodesUp`/`nodesTotal`, which count
// a parked machine as a machine that failed. That is the `down` bug one level
// up: a fleet of nothing but parked nodes reads `Down` — an alarm about a
// choice the operator made on purpose — and the fleet-wide tally says the fleet
// is one quarter healthy when three quarters of it is deliberately out.
//
// It lives HERE rather than in `ClusterWidgetMetrics.swift` for a mechanical
// reason, not a tidy one: neither `carplay_check.sh` nor either macOS target
// compiles that file, so a rule written there is invisible to the two surfaces
// that need it (the same reason `ClusterNodeTally` itself is a separate
// Foundation-only file). This file is compiled into all four iOS targets and
// both macOS targets, which is what lets ONE rule be executed by three gates.
//
// It counts the projected ROWS rather than the tally's arithmetic, for the
// reason `ClusterC1` already documented: a parked row keeps its `error`, so the
// row is the only place that knows both the tag and the answer for one machine.
// ---------------------------------------------------------------------------

struct FleetVerdict: Equatable {
    /// Frame-listed nodes that answered AND were not parked. nil when the read
    /// named no frame at all — which is why no caller may write `answered ?? 0`.
    let answered: Int?
    /// Frame-listed nodes the operator did NOT park: the denominator a verdict
    /// may use. nil on the same rule as `answered`.
    let judged: Int?
    /// Frame-listed nodes the operator parked, carried so a surface can say how
    /// much of the frame is out of service without recounting it.
    let paused: Int?

    init(answered: Int?, judged: Int?, paused: Int?) {
        self.answered = answered
        self.judged = judged
        self.paused = paused
    }

    /// "The read told us nothing about the frame."
    static let noAnswer = FleetVerdict(answered: nil, judged: nil, paused: nil)

    /// Project the rows a surface already draws. An EMPTY frame is no answer for
    /// the same reason `ClusterNodeTally.project` treats it as one: an empty
    /// denominator is not a fact an operator can act on (§6 rule 1).
    init(rows: [FleetNodeLine]?) {
        guard let rows, !rows.isEmpty else { self = .noAnswer; return }
        self.paused = rows.filter { $0.isPaused }.count
        self.answered = rows.filter { $0.isUp && !$0.isPaused }.count
        self.judged = rows.filter { !$0.isPaused }.count
    }

    /// Every node the route listed is parked: nothing is failing and nothing is
    /// serving. Ask this BEFORE `answered == 0` — a parked frame fails both
    /// readings, and if the order is inverted the alarm one wins by accident.
    var allParked: Bool { judged == 0 }

    /// The fleet-level word for `allParked`. Title Case like the words it sits
    /// beside (`Down`, `All up`, `No data`), and the ONLY place that
    /// fleet-level word is written — the per-node copy is `FleetFormat.paused`,
    /// and the two must not drift.
    static let pausedWord = "Paused"

    /// The `answered/judged` tally string, `—` when the read named no frame.
    /// The C1 widget's own `tally` and the macOS menu bar's `Cluster nodes up`
    /// title are BOTH this line — three surfaces, one number, and the menu bar
    /// has no room for a second opinion.
    var tallyText: String {
        guard let answered, let judged else { return "—" }
        return "\(answered)/\(judged)"
    }
}

// ---------------------------------------------------------------------------
// The order: every node list a surface draws is ordered by LAST OCTET,
// numerically.
//
// The label rule above makes a node's identity its last octet, so the operator
// reads the fleet in the same order they read an address: 2, 9, 10, 100. A
// string sort would file `10` before `9` and `100` before `9` too — the same
// fleet would appear in a different order on two tiles of the same app
// depending on whether a host got a single-digit address, and the whole point of
// the label rule is that a number means the same machine everywhere.
//
// It lives HERE rather than in a view because it is one rule with six callers
// (the widget's node rows, the C1 column, the parked list, the two label lists
// in the tally, the desktop dropdown's host rows and CarPlay's pushed list), and
// every one of them already reads the rows this file projects. A surface that
// re-sorted would be a second order rule, which is what this file exists to
// prevent.
// ---------------------------------------------------------------------------

enum FleetNodeOrder {
    /// The numeric key of a label that IS an octet (`"10"` → 10), nil for a
    /// hostname fallback and for anything a numeric parse refuses (a digit run
    /// too long for `Int` is a hostname-shaped thing, not an octet).
    static func octetKey(_ label: String) -> Int? {
        guard !label.isEmpty, label.allSatisfy({ $0.isNumber }) else { return nil }
        return Int(label)
    }

    /// THE order for a list of nodes, given how to read each one's label.
    ///
    /// Octet-labelled rows first, lowest octet first; hostname-labelled rows
    /// after them, in the order the route listed them; equal keys keep their
    /// route order. The route position is carried into the comparison on
    /// purpose — Swift's `sorted` makes NO stability guarantee, so "stable by
    /// octet" has to be implemented rather than assumed, or the same payload
    /// can come out in a different order on two runs.
    static func sortedByOctet<T>(_ rows: [T], label: (T) -> String) -> [T] {
        rows.enumerated()
            .sorted { lhs, rhs in
                let left = octetKey(label(lhs.element))
                let right = octetKey(label(rhs.element))
                switch (left, right) {
                case let (a?, b?) where a != b: return a < b
                case (.some, .some): return lhs.offset < rhs.offset
                case (.some, .none): return true
                case (.none, .some): return false
                case (.none, .none): return lhs.offset < rhs.offset
                }
            }
            .map(\.element)
    }
}

// ---------------------------------------------------------------------------
// The serving table, projected once for every surface.
//
// `hscc cluster monitor` prints one row per (backend, model): model, run/wait,
// KV%, token totals and rates. The total line at the bottom is the sum this
// struct carries. `rows == nil` means the route did not send the section
// (older server); `rows == []` is the route telling us there are no backends —
// a fact, and therefore `0 models`, not `—`.
// ---------------------------------------------------------------------------

struct ServingSummary: Equatable {
    /// Distinct models the serving table names. Nil when the route sent no
    /// serving section; 0 when it sent an empty one.
    let models: Int?
    /// Short model names in table order, de-duplicated.
    let modelNames: [String]
    /// CLI "Run"/"Wait" totals. Nil when not told.
    let running: Int?
    let waiting: Int?
    /// CLI total "Gen t/s". The CLI sums the rows; a row whose first sample has
    /// no rate contributes nothing, and no rate at all is nil, never 0.
    let genTps: Double?
    /// Number of backend rows that answered (the CLI's total (n) label).
    let backends: Int

    static let noAnswer = ServingSummary(models: nil, modelNames: [], running: nil,
                                         waiting: nil, genTps: nil, backends: 0)

    init(rows: [ServingRow]?) {
        guard let rows else { self = .noAnswer; return }
        let live = rows.filter { !$0.isDown }
        self.backends = live.count
        self.modelNames = Array(Set(live.compactMap { $0.shortModel })).sorted()
        self.models = modelNames.count
        self.running = live.isEmpty ? 0 : live.compactMap { $0.running }.reduce(0, +)
        self.waiting = live.isEmpty ? 0 : live.compactMap { $0.waiting }.reduce(0, +)
        let rates = live.compactMap { $0.gen_tps }
        self.genTps = rates.isEmpty ? nil : rates.reduce(0, +)
    }

    private init(models: Int?, modelNames: [String], running: Int?,
                 waiting: Int?, genTps: Double?, backends: Int) {
        self.models = models
        self.modelNames = modelNames
        self.running = running
        self.waiting = waiting
        self.genTps = genTps
        self.backends = backends
    }
}

/// One node row: the identity + model + metrics the host table and serving
/// table together give us. Every field is a route field, not a derived state.
struct FleetNodeLine: Equatable, Identifiable {
    let label: String
    /// The hostname the node reported about itself (`sample.hostname`), when it
    /// reported one. This is NOT the label — since apps 1.4.4 the label is the
    /// last octet (`MonitorNode.nodeLabel`) — it is the name kept for the one
    /// place a name has always been the point: the accessibility label a surface
    /// speaks. Nil when the route carried none, and a caller must then fall back
    /// to `label` rather than to an address (§7).
    let reportedName: String?
    let model: String?
    let isUp: Bool
    let gpuPct: Double?
    let cpuTempC: Double?
    let gpuTempC: Double?
    let running: Int?
    let waiting: Int?
    let error: String?
    /// The node's own GPU wattage, straight from its monitor sample.
    let gpuPowerW: Double?
    /// The node's serving throughput, from the serving row that owns it.
    let genTps: Double?

    var id: String { label }

    /// The serving model names the orchestrator backend (`orchestrator-model`),
    /// so a node can be marked from the data rather than a hard-coded address
    /// or position. No match means no mark, never an invented role.
    var isOrchestrator: Bool {
        model?.lowercased().contains("orchestrator") == true
    }

    /// The node the pause overlay holds. Asked from the row's own `status` tag,
    /// so a parked machine never reaches the `down` reading below.
    let isPaused: Bool
    /// Why it was parked, trimmed of the route's `""` placeholder. nil = not told.
    let pausedReason: String?
    /// The ISO-8601 instant the pause began, as the route sent it. nil = not
    /// told, which renders a bare `paused` — never a `—`.
    let pausedSince: String?

    init(host: MonitorNode, serving: [ServingRow]?) {
        self.label = host.displayLabel
        let reported = host.sample?.hostname?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.reportedName = (reported?.isEmpty == true) ? nil : reported
        self.isUp = host.isUp
        self.isPaused = host.isPaused
        let reason = host.paused_reason?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        self.pausedReason = (reason?.isEmpty == true) ? nil : reason
        self.pausedSince = host.paused_since
        self.gpuPct = host.sample?.gpuUtilPct
        self.cpuTempC = host.sample?.cpuTempC
        self.gpuTempC = host.sample?.gpuTempC
        self.error = host.error
        self.gpuPowerW = host.sample?.gpuPowerW
        let ownTail = MonitorNode.shortTail(host.host)
        let row = serving?.first {
            !$0.isDown && $0.node.map { MonitorNode.shortTail($0) == ownTail } == true
        }
        self.model = row?.shortModel
        self.running = row?.running
        self.waiting = row?.waiting
        self.genTps = row?.gen_tps
    }
}

/// The monitor payload projected to exactly what the CLI prints, shared by the
/// cluster widget, the monitor widget and both Live Activities.
struct FleetProjection: Equatable {
    let tally: ClusterNodeTally
    let nodes: [FleetNodeLine]
    let serving: ServingSummary
    /// Every node the pause overlay holds, whether or not the monitor frame
    /// reached it. `[]` is the route saying nothing is parked; a parked host
    /// sparkrun never listed appears HERE and nowhere in `nodes`, which is why
    /// a surface that wants the full parked set reads this list and a surface
    /// that draws one line per sampled host draws `nodes`.
    let paused: [FleetPausedNode]
    /// Distinct `workload_id`s across the host table — counted from
    /// `/v1/cluster/monitor`, the route this projection is built from. It is NOT
    /// `hscc cluster status`'s workload count, which an earlier version of this
    /// line claimed: that is a different number with a different owner,
    /// `ClusterStatusSummary.jobs = response.workloads.count`
    /// (`Sources/HSCCWidgets/ClusterWidgetMetrics.swift:69-77`), read off the
    /// status route and drawn in exactly one place — the `fleet == nil` fallback
    /// line of `text(for:)`. The two routes can disagree about the same refresh,
    /// so a surface that shows both has to name which verb it answered.
    /// Nil when the route sent workloads without ids.
    let jobs: Int?

    init?(projecting response: MonitorResponse?) {
        guard let response, let hosts = response.json?.hosts, !hosts.isEmpty else {
            return nil
        }
        self.tally = ClusterNodeTally.project(response)
        self.serving = ServingSummary(rows: response.serving)
        // Octet order, not frame order. `nodes` IS the node list every surface
        // draws — the widget's rows, the C1 column, CarPlay's pushed list, the
        // desktop dropdown's hosts — so ordering it once here is what makes
        // "stable by octet" true everywhere at once, and no view has to sort
        // (a view that sorted would be a second order rule).
        self.nodes = FleetNodeOrder.sortedByOctet(
            hosts.map { FleetNodeLine(host: $0, serving: response.serving) },
            label: { $0.label })
        // The parked set, assembled as the payload states it — the rows the
        // route tagged (in frame order) then the top-level entries the frame
        // never listed — and then put in `FleetNodeOrder`'s octet order like
        // every other node list. Both halves are de-duplicated by the route's
        // own identity rule (`PauseContract.nodeKey`), because a span record
        // parks a whole unit and the same machine can arrive tagged AND named
        // top-level.
        var listed: [FleetPausedNode] = hosts.compactMap { host in
            host.isPaused ? FleetPausedNode(node: host) : nil
        }
        let seen = Set(hosts.filter { $0.isPaused }.map { PauseContract.nodeKey($0.host) })
        for entry in response.paused_nodes ?? [] where entry.isPaused {
            if let key = entry.key, seen.contains(key) { continue }
            if let match = FleetPausedNode(entry: entry, in: hosts),
               !listed.contains(where: { $0.key == match.key }) {
                listed.append(match)
            }
        }
        self.paused = FleetNodeOrder.sortedByOctet(listed, label: { $0.label })
        let workloads = hosts.flatMap { $0.workloads ?? [] }
        let ids = Set(workloads.compactMap { $0.id })
        if !ids.isEmpty {
            self.jobs = ids.count
        } else if workloads.isEmpty {
            // Hosts answered and carry no workloads: the CLI's "0 workloads
            // running" is a value, not silence.
            self.jobs = 0
        } else {
            self.jobs = nil
        }
    }
}

extension FleetProjection {
    /// The parked set of one payload, indexed by the route's own identity rule
    /// (`PauseContract.nodeKey`), for a surface that has to answer "is THIS
    /// machine parked?" for a machine it is drawing a row of — a serving row's
    /// `node`, say. Both cell view-models index through this one function rather
    /// than each writing its own lookup, so the widget, the desktop dropdown and
    /// the menu bar cannot disagree about which backend sits on a parked box.
    static func parkedIndex(of response: MonitorResponse) -> [String: FleetPausedNode] {
        var index: [String: FleetPausedNode] = [:]
        for entry in FleetProjection(projecting: response)?.paused ?? [] {
            index[entry.key] = entry
        }
        return index
    }
}

// ---------------------------------------------------------------------------
// The parked set, and the ONE copy rule for it.
//
// `paused (reason, since)` is written here and nowhere else. A second site
// composing the phrase in a view is a second spelling that can drift, and this
// repo's doctrine is that a display rule must be single-sited so a gate can
// execute it. `cluster_widget_check.sh` compiles this file with the real models
// and asserts every branch of `FleetFormat.paused` against a literal.
// ---------------------------------------------------------------------------

/// One node the pause overlay holds, labelled for display.
///
/// Built from either half of the payload: a tagged host row (which can carry a
/// reported hostname) or a top-level `paused_nodes` entry (which carries only
/// the pause record's host string). Both go through the app-wide §7 label rule
/// `MonitorNode.nodeLabel` — the last octet of the node's IP, or the short name
/// when its host carries no IPv4 — so neither can put an address on a tile: an
/// operator who paused a node BY IP gets `13`, exactly like every other surface.
struct FleetPausedNode: Equatable, Identifiable {
    /// The route's identity for this node (trim + case-fold), used only to
    /// de-duplicate the two halves of the payload. Never drawn.
    let key: String
    /// The §7 display label.
    let label: String
    /// Why the operator parked it, `""` normalised to nil — an empty reason is
    /// silence, not a reason, and it must not render as `paused (, 3h ago)`.
    let reason: String?
    /// The ISO-8601 instant the pause began, as sent. nil renders a bare
    /// `paused`, never a dash.
    let since: String?

    var id: String { key }

    /// From a row the route tagged.
    init?(node: MonitorNode) {
        guard node.isPaused else { return nil }
        self.key = PauseContract.nodeKey(node.host)
        self.label = node.displayLabel
        self.reason = FleetPausedNode.normalised(node.paused_reason)
        self.since = node.paused_since
    }

    /// From a top-level `paused_nodes` entry. `hosts` is the frame this payload
    /// carried: when it happens to list the same machine, its `displayLabel`
    /// wins, because a hostname the route reported is a name the machine gave
    /// itself. When the frame did not reach the machine — the whole reason the
    /// top-level list exists — the label is derived from the pause record's host
    /// string with THE same rule, never taken from it raw: an operator who
    /// paused a node BY IP gets `13`, exactly like every other surface.
    init?(entry: PausedNodeEntry, in hosts: [MonitorNode]) {
        guard entry.isPaused, let key = entry.key else { return nil }
        self.key = key
        if let row = hosts.first(where: { PauseContract.nodeKey($0.host) == key }) {
            self.label = row.displayLabel
        } else {
            self.label = MonitorNode.nodeLabel(host: entry.node ?? "", reportedHostname: nil)
        }
        self.reason = FleetPausedNode.normalised(entry.paused_reason)
        self.since = entry.paused_since
    }

    /// The route sends `""` for a pause with no reason.
    private static func normalised(_ reason: String?) -> String? {
        guard let trimmed = reason?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty else { return nil }
        return trimmed
    }

    /// `paused (training run, 3h ago)` — see `FleetFormat.paused`.
    func statusText(now: Date = Date()) -> String {
        FleetFormat.paused(reason: reason, since: since, now: now)
    }
}

// ---------------------------------------------------------------------------
// Text rules. The views render these lines verbatim; the checks assert them
// against the committed fixture. No view composes its own number.
// ---------------------------------------------------------------------------

enum FleetSurface: String, CaseIterable {
    case small, medium, large, inline, circular, rectangular, extraLarge
}

struct FleetRow: Equatable {
    let title: String
    let detail: String?
}

struct FleetText: Equatable {
    let headline: String
    let detail: String?
    let rows: [FleetRow]
    let footer: String?
}

enum FleetFormat {
    static let dash = "—"

    static func pct(_ value: Double?) -> String {
        guard let value else { return dash }
        return "\(Int(value.rounded()))%"
    }

    static func celsius(_ value: Double?) -> String {
        guard let value else { return dash }
        return "\(Int(value.rounded()))°C"
    }

    static func tps(_ value: Double?) -> String {
        guard let value else { return dash }
        return String(format: "%.1f t/s", value)
    }

    static func count(_ value: Int?, singular: String, plural: String) -> String {
        guard let value else { return dash }
        return "\(value) \(value == 1 ? singular : plural)"
    }

    static func up(_ up: Int?, _ total: Int?) -> String {
        guard let up else { return dash }
        guard let total else { return "\(up) up" }
        return "\(up)/\(total) up"
    }

    static func age(_ minutes: Int?) -> String? {
        guard let minutes else { return nil }
        return "updated \(minutes)m ago"
    }

    // MARK: - Pause copy
    //
    // ONE site for the phrase. The card that landed the overlay (t_824b617f)
    // fixed the three field names as a contract; this fixes how they are read
    // out. `paused (reason, since)` with either half missing degrades to the
    // shorter truth — `paused (reason)`, `paused (3h ago)`, then bare
    // `paused` — because the route sends `paused_reason: ""` and
    // `paused_since: null` for a pause record that carried neither, and a dash
    // in those slots would claim a value the payload never named (§6 rule 1).

    /// The word a parked node draws. Lower-case like `up` and `down`, the two
    /// words it replaces in the same slot.
    static let pausedWord = "paused"

    /// The status word for one node row: `paused (...)`, `up` or `down`.
    ///
    /// The pause check runs FIRST. A parked row keeps its `error` (the overlay
    /// has no business deleting a real diagnostic), so a reader that asked
    /// `isUp` first would print `down` beside a machine the operator parked on
    /// purpose — which is exactly the bug this seam exists to close.
    static func status(_ node: FleetNodeLine, now: Date = Date()) -> String {
        guard node.isPaused else { return node.isUp ? "up" : "down" }
        return paused(reason: node.pausedReason, since: node.pausedSince, now: now)
    }

    /// `paused`, `paused (reason)`, `paused (3h ago)` or `paused (reason, 3h ago)`.
    ///
    /// A blank reason is not a reason: the route sends `""` for a pause record
    /// written without one, and it must degrade to the shorter truth rather than
    /// to `paused (, 3h ago)`.
    static func paused(reason: String?, since: String?, now: Date = Date()) -> String {
        let trimmed = reason?.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = [trimmed.flatMap { $0.isEmpty ? nil : $0 },
                     sinceText(since, now: now)].compactMap { $0 }
        guard !parts.isEmpty else { return pausedWord }
        return "\(pausedWord) (\(parts.joined(separator: ", ")))"
    }

    /// How long ago the pause began, compact: `just now`, `12m ago`, `3h ago`,
    /// `2d ago`. Relative for the same reason the age line is — a timestamp no
    /// one can read at a glance off a 60 mm tile is not information — and
    /// measured against `now`, which the caller injects so a gate can pin it.
    ///
    /// The minutes are ROUNDED, not floored, and the hours and days are derived
    /// from those rounded minutes: a pause that began three hours ago minus one
    /// eighth of a second is three hours ago, and flooring each step
    /// independently would have it claim two. Every smaller unit is derived from
    /// the larger one for the same reason — the copy cannot disagree with itself.
    ///
    /// A `since` the parser cannot read comes back VERBATIM rather than nil: the
    /// route sent a fact, and dropping it to render a shorter line would be the
    /// surface deciding the operator's pause began at no particular time.
    static func sinceText(_ since: String?, now: Date = Date()) -> String? {
        guard let raw = since?.trimmingCharacters(in: .whitespacesAndNewlines),
              !raw.isEmpty else { return nil }
        guard let date = iso8601(raw) else { return raw }
        let totalMinutes = Int(max(0, now.timeIntervalSince(date) / 60).rounded())
        if totalMinutes < 1 { return "just now" }
        if totalMinutes < 60 { return "\(totalMinutes)m ago" }
        let hours = totalMinutes / 60
        if hours < 24 { return "\(hours)h ago" }
        return "\(hours / 24)d ago"
    }

    /// The shapes this route emits: Python's `datetime.isoformat()` writes
    /// second precision with an offset, and SIX fractional digits whenever the
    /// record has any sub-second precision at all; a `Z` suffix is the same
    /// instant written differently, and swapping it for an offset is the only
    /// normalisation done here. Anything else fails to parse and is drawn as it
    /// arrived.
    static func iso8601(_ raw: String) -> Date? {
        let text = raw.hasSuffix("Z")
            ? String(raw.dropLast()) + "+00:00"
            : raw
        if let date = fractionalISO.date(from: text) ?? zoneISO.date(from: text) {
            return date
        }
        // `ISO8601DateFormatter`'s fractional option reads EXACTLY three digits,
        // so a microsecond timestamp — the common case for a Python-written
        // record — fails it and would otherwise be drawn as raw text. Cutting to
        // milliseconds loses nothing this copy can show: the smallest unit it
        // prints is a minute.
        if let shortened = truncatedFractional(text),
           let date = fractionalISO.date(from: shortened) {
            return date
        }
        return nil
    }

    /// `T10:00:00.123456+00:00` to `T10:00:00.123+00:00`. nil when there is no
    /// over-long fractional run to shorten, so the retry above is a second
    /// attempt at the same instant and never a second guess at it.
    private static func truncatedFractional(_ text: String) -> String? {
        guard let dot = text.firstIndex(of: "."),
              text[text.startIndex..<dot].contains("T") else { return nil }
        var end = text.index(after: dot)
        while end < text.endIndex, text[end].isNumber { end = text.index(after: end) }
        guard text.distance(from: text.index(after: dot), to: end) > 3 else { return nil }
        let keep = text.index(text.index(after: dot), offsetBy: 3)
        return String(text.prefix(upTo: keep)) + String(text.suffix(from: end))
    }

    private static let fractionalISO: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let zoneISO: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()
}

extension FleetProjection {
    /// Every family renders this. `compact` is the medium/rectangular row:
    /// model + the two temperatures the CLI shows; large adds run/wait.
    func text(for surface: FleetSurface,
              failure: String? = nil,
              ageMinutes: Int? = nil,
              now: Date = Date()) -> FleetText {
        let headline: String
        if tally.nodesUp != nil {
            headline = FleetFormat.up(tally.nodesUp, tally.nodesTotal)
        } else if let failure, !failure.isEmpty {
            headline = failure
        } else {
            headline = FleetFormat.dash
        }

        var parts: [String] = []
        if let models = serving.models {
            parts.append(FleetFormat.count(models, singular: "model", plural: "models"))
        }
        if surface != .small, surface != .inline, let jobs {
            parts.append(FleetFormat.count(jobs, singular: "job", plural: "jobs"))
        }
        if let gen = serving.genTps {
            parts.append(FleetFormat.tps(gen))
        }
        let detail = parts.isEmpty ? nil : parts.joined(separator: " · ")

        let limit: Int
        let full: Bool
        switch surface {
        case .small, .inline, .circular:
            limit = 0; full = false
        case .medium, .rectangular:
            limit = 4; full = false
        case .large, .extraLarge:
            limit = 8; full = true
        }
        let rows = nodes.prefix(limit).map { node -> FleetRow in
            let title = [node.label, node.model].compactMap { $0 }.joined(separator: " · ")
            // ONE call, one rule: `up` / `down` / `paused (reason, since)`. The
            // row that says `paused` is a machine the operator parked, so no
            // view here may paint it as a fault — the tile reads this word and
            // the heat verdict next to it, and both say "not a problem".
            var bits: [String] = [FleetFormat.status(node, now: now)]
            if let gpu = node.gpuPct { bits.append("GPU " + FleetFormat.pct(gpu)) }
            if let gpuTemp = node.gpuTempC { bits.append(FleetFormat.celsius(gpuTemp)) }
            if full {
                if let cpuTemp = node.cpuTempC { bits.append("CPU " + FleetFormat.celsius(cpuTemp)) }
                if let running = node.running { bits.append("run \(running)") }
                if let waiting = node.waiting { bits.append("wait \(waiting)") }
            }
            return FleetRow(title: title, detail: bits.joined(separator: " · "))
        }

        var footParts: [String] = []
        if let failure, !failure.isEmpty, tally.nodesUp != nil { footParts.append(failure) }
        if let age = FleetFormat.age(ageMinutes) { footParts.append(age) }
        return FleetText(headline: headline,
                         detail: detail,
                         rows: rows,
                         footer: footParts.isEmpty ? nil : footParts.joined(separator: " · "))
    }

    /// `accessoryInline` is one line by construction: headline + detail.
    func inlineLine(failure: String? = nil) -> String {
        let text = self.text(for: .inline, failure: failure)
        return [text.headline, text.detail].compactMap { $0 }.joined(separator: " · ")
    }
}
