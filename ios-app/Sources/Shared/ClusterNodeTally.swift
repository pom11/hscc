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
    let model: String?
    let isUp: Bool
    let gpuPct: Double?
    let cpuTempC: Double?
    let gpuTempC: Double?
    let running: Int?
    let waiting: Int?
    let error: String?

    var id: String { label }

    init(host: MonitorNode, serving: [ServingRow]?) {
        self.label = host.displayLabel
        self.isUp = host.isUp
        self.gpuPct = host.sample?.gpuUtilPct
        self.cpuTempC = host.sample?.cpuTempC
        self.gpuTempC = host.sample?.gpuTempC
        self.error = host.error
        let ownTail = MonitorNode.shortTail(host.host)
        let row = serving?.first {
            !$0.isDown && $0.node.map { MonitorNode.shortTail($0) == ownTail } == true
        }
        self.model = row?.shortModel
        self.running = row?.running
        self.waiting = row?.waiting
    }
}

/// The monitor payload projected to exactly what the CLI prints, shared by the
/// cluster widget, the monitor widget and both Live Activities.
struct FleetProjection: Equatable {
    let tally: ClusterNodeTally
    let nodes: [FleetNodeLine]
    let serving: ServingSummary
    /// `hscc cluster status`'s workload count: distinct `workload_id`s across
    /// the host table. Nil when the route sent workloads without ids.
    let jobs: Int?

    init?(projecting response: MonitorResponse?) {
        guard let response, let hosts = response.json?.hosts, !hosts.isEmpty else {
            return nil
        }
        self.tally = ClusterNodeTally.project(response)
        self.serving = ServingSummary(rows: response.serving)
        self.nodes = hosts.map { FleetNodeLine(host: $0, serving: response.serving) }
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
}

extension FleetProjection {
    /// Every family renders this. `compact` is the medium/rectangular row:
    /// model + the two temperatures the CLI shows; large adds run/wait.
    func text(for surface: FleetSurface,
              failure: String? = nil,
              ageMinutes: Int? = nil) -> FleetText {
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
            var bits: [String] = [node.isUp ? "up" : "down"]
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
