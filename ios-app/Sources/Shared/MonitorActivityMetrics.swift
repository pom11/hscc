import Foundation

// ---------------------------------------------------------------------------
// The Monitor Live Activity's surface payload, and every decision about which
// metric each surface shows.
//
// `MonitorActivityAttributes.ContentState` is the bytes the APP pushes and the
// extension draws. Before this file the state carried per-node CPU/RAM/GPU
// numbers and a state word, and each surface decided locally what to render —
// which is how one surface could show a summary while another showed nothing
// useful, and how a threshold could mean one thing in the Lock Screen view and
// another in the island. Every such decision lives here now.
//
// WHY THIS FILE IS FOUNDATION-ONLY (and why the payload types are in it rather
// than beside `MonitorActivityAttributes`): `MonitorActivity.swift` imports
// ActivityKit, so no macOS harness can compile it whole — only `GATE-SLICE`
// blocks can be cut out of it. A rule placed there is a rule `model_decode_check`
// cannot execute. Plain Foundation types in their own file are the shape that
// gets RUN against the committed fixtures (the `ClusterNodeTally.swift` /
// `CardsRunningProjection.swift` precedent), so `MonitorSurfaceReading` below is
// the code the shipped surfaces actually call AND the code the gate executes.
//
// WHAT THIS FILE DOES NOT DO.
//
//   1. It does not format a metric. Every host cell comes from
//      `CLIMonitorHostRow` (the `hscc cluster monitor` view-model, whose cells
//      are held against goldens generated from `cluster_live.py`) and every
//      glance string from `FleetFormat` (the app's existing glance vocabulary).
//      A unit, a rounding or a `°C`-vs-` C` rule written here would be a second
//      owner of that contract.
//   2. It does not set a threshold. Colours come back as `ClusterMetricLevel`,
//      whose bands are declared in the view-model, plus the CLI's own
//      `_load_style` for a queue. The views map level → `WidgetStyle` colour.
//   3. It does not fetch or read a store. The driver is the only refresh owner;
//      the extension renders these bytes and never asks a route anything.
// ---------------------------------------------------------------------------

// MARK: - Row caps

/// How many rows a push may carry and how many a surface may draw.
///
/// Two numbers per surface on purpose. ActivityKit caps the serialized
/// attributes + content state (~4 KB), so the DRIVER trims what it pushes
/// (`maxPushed*`) and records how many rows it dropped — silently losing hosts
/// off a big fleet is exactly the failure this card exists to fix, so the
/// truncation is pushed as data (`hostRowsDropped`) and named on the surface.
/// The views then clip AGAINST THEIR OWN REGION (`*RowLimit`): a pushed
/// ContentState is untrusted input to a fixed-height area, which is why
/// `ClusterHealthDetail` clips too (`ClusterHealthLiveActivity.swift:127-130`).
enum MonitorSurfaceLimits {
    /// Host rows the driver pushes (the Lock Screen + expanded clip lower).
    static let maxPushedHostRows = 6
    /// `total (n)` rows the driver pushes, one per model.
    static let maxPushedServingTotalRows = 4
    /// Host rows the Lock Screen draws.
    static let lockScreenHostRows = 4
    /// Host rows the expanded island's bottom region draws, beside its header
    /// row and the serving line — the region is roughly three lines tall.
    static let islandHostRows = 2
    /// `total (n)` lines the Lock Screen draws under its aggregate Run/Wait.
    static let lockScreenServingTotals = 2
    /// `total (n)` lines the expanded island draws.
    static let islandServingTotals = 1
}

// MARK: - The pushed payload

/// One host of `hscc cluster monitor`'s host table, as the app pushed it.
///
/// The nine cell strings are `CLIMonitorHostRow.cells` — what the CLI's
/// `Table.add_row` receives, `-` wherever the CLI prints `-`. A surface that
/// needed the raw numbers to paint a bar has them here as levels instead: the
/// view-model already decided how hot is hot, and a surface re-deriving a
/// threshold is a second opinion nobody asked for.
struct MonitorHostMetric: Codable, Hashable {
    /// The label to draw (`displayLabel` — the sample hostname or the `.NNN`
    /// tail, §7: never a whole address).
    var label: String
    var jobs: String
    var cpuPct: String
    var ramPct: String
    var swapPct: String
    var gpuPct: String
    var cpuTemp: String
    var gpuTemp: String
    var gpuPower: String
    /// This host's serving run/wait counts, from the serving table's row for
    /// the same machine. Both nil when the serving section was never told —
    /// which is NOT the same as zero (rule 1).
    var running: Int?
    var waiting: Int?
    /// The view-model's verdicts, for the surface to paint. `.unknown` means
    /// "not told", `.dim` means "measured and idle" — the two must not share a
    /// colour.
    var cpuTempLevel: ClusterMetricLevel?
    var gpuTempLevel: ClusterMetricLevel?
    var gpuUtilLevel: ClusterMetricLevel?
    var gpuPowerLevel: ClusterMetricLevel?
    /// The CLI's `_load_style` verdict on THIS host's own queue, so the row's
    /// run/wait tail is coloured by the rule rather than by a view guessing
    /// what two numbers mean. nil when the serving table never answered for
    /// this host — which is not "idle".
    var servingLevel: ClusterMetricLevel?
    /// `host_table`'s error branch: the reason is in `cpuPct` and every numeric
    /// cell is `-`. A surface must say "not sampled" rather than draw a row of
    /// dashes a healthy host could also produce.
    var isFailure: Bool
}

/// One `total (n)` row of the serving table — the CLI's per-model totals.
struct MonitorServingTotalMetric: Codable, Hashable, Identifiable {
    /// `total (n)`, the CLI's own label (`n` = live backend rows, so a `down`
    /// backend is not counted).
    var label: String
    /// Short model name (`org/model` → `model`, the CLI's `rsplit`). Unique
    /// among total rows — the CLI keys its accumulator by it — so it is the id.
    var model: String
    var run: String
    var wait: String
    var tokensIn: String
    var tokensOut: String
    var genTps: String
    /// The CLI's `_load_style` verdict on THIS model's queue.
    var loadLevel: ClusterMetricLevel

    var id: String { model }
}

/// Which metric the island's compact and minimal surfaces lead with, and what
/// it means. Decided once app-side so the two forms of one push cannot
/// disagree.
struct MonitorPrimaryMetric: Codable, Hashable {
    /// What the value is. Raw-valued and defaulted nil-safe so an older
    /// payload that never had this field decodes and renders as "nothing".
    enum Kind: String, Codable {
        /// The hottest GPU temperature in the fleet.
        case gpuTemp
        /// The hottest CPU temperature, when no GPU reported one.
        case cpuTemp
        /// The busiest host's GPU utilization, when no temperature was read.
        case gpuUtil
        /// The serving table's live run count.
        case serving
        /// The serving table's live wait count — a queue is worth its own kind
        /// so a surface can colour it by the CLI's load rule without guessing
        /// what an abbreviation meant.
        case waiting
        /// The host tally, when the monitor answered but no metric did.
        case tally
    }

    var kind: Kind
    /// The value with its unit, from `FleetFormat` (`87°C`, `62%`, `3 running`).
    var value: String
    /// Whose metric it is — the host's display label for a per-host reading,
    /// nil for a fleet aggregate. A per-host number shown without this reads
    /// like a fleet number.
    var label: String?
    /// The view-model's verdict on the value, for colour.
    var level: ClusterMetricLevel?
}

/// Everything this file derives from one monitor read, in one bundle, so the
/// driver assigns fields instead of re-deciding rules.
struct MonitorSurfacePayload: Equatable {
    /// The host table, capped at `MonitorSurfaceLimits.maxPushedHostRows`.
    /// nil = the read never answered (the route failed), which is NOT the same
    /// as an empty array — that is the degraded `0 host(s)` case the CLI still
    /// prints a header for.
    var hostRows: [MonitorHostMetric]?
    /// Hosts the cap left out — pushed so a surface can say `+N more`.
    var hostRowsDropped: Int?
    /// The per-model `total (n)` rows. nil = the route never sent a serving
    /// section; `[]` = it sent one with nothing live in it.
    var servingTotals: [MonitorServingTotalMetric]?
    var servingTotalsDropped: Int?
    /// The serving table's aggregate Run/Wait. nil = the section was never
    /// told (`ServingSummary.noAnswer`), which is not zero.
    var servingRunning: Int?
    var servingWaiting: Int?
    /// Hosts the route listed / that answered (the CLI's `N host(s)`).
    var nodesUp: Int?
    var nodesTotal: Int?
    /// The island's headline metric, and the secondary beside it.
    var primary: MonitorPrimaryMetric?
    var secondary: MonitorPrimaryMetric?
    /// The queue verdict, so a colour can follow the CLI's own rule. nil when
    /// the serving table never answered — "not told" is not "idle".
    var queueLevel: ClusterMetricLevel?
}

// MARK: - The rules

/// Every "what does this surface show" rule for the Monitor Live Activity.
/// Static funcs over plain values — no SwiftUI, no `Color`, no ActivityKit —
/// so `model_decode_check` compiles and runs exactly what ships.
enum MonitorSurfaceReading {

    /// Reduce one monitor read to everything the surfaces draw.
    ///
    /// `nodes` is nil only when the read itself failed; an EMPTY array is the
    /// degraded route (`{"speak": …}`, no `json`), which the CLI prints as
    /// `0 host(s)` rather than nothing — so the payload says "0 total", not
    /// "not told".
    static func payload(nodes: [MonitorNode]?,
                        serving: [ServingRow]?) -> MonitorSurfacePayload {
        var out = MonitorSurfacePayload()

        // Host rows: the CLI's host_table, one entry per host the route listed,
        // in the route's order. Capped; the cap is reported, not hidden.
        if let nodes {
            let rows = nodes.map { CLIMonitorHostRow(node: $0) }
            let shown = rows.prefix(MonitorSurfaceLimits.maxPushedHostRows)
            out.hostRows = shown.map { row in
                let match = servingRow(forHost: row.host, in: serving)
                return MonitorHostMetric(
                    label: row.displayLabel,
                    jobs: row.jobs,
                    cpuPct: row.cpuPct,
                    ramPct: row.ramPct,
                    swapPct: row.swapPct,
                    gpuPct: row.gpuPct,
                    cpuTemp: row.cpuTemp,
                    gpuTemp: row.gpuTemp,
                    gpuPower: row.gpuPower,
                    running: match?.running,
                    waiting: match?.waiting,
                    cpuTempLevel: row.cpuTempLevel,
                    gpuTempLevel: row.gpuTempLevel,
                    gpuUtilLevel: row.gpuUtilLevel,
                    gpuPowerLevel: row.gpuPowerLevel,
                    servingLevel: match.map {
                        CLIMonitorFormat.loadLevel(running: $0.running ?? 0, waiting: $0.waiting ?? 0)
                    },
                    isFailure: row.error != nil)
            }
            out.hostRowsDropped = max(0, rows.count - shown.count)
            out.nodesTotal = nodes.count
            out.nodesUp = nodes.filter { $0.isUp }.count
        }

        // Serving: the same `ServingSummary` the widget tallies from (one rule
        // for "what did the serving table say"), and the same
        // `CLIMonitorServingTotal` the CLI's total rows are built from. A nil
        // section stays nil everywhere — silence is not "0 models".
        let summary = ServingSummary(rows: serving)
        out.servingRunning = summary.running
        out.servingWaiting = summary.waiting
        if let serving {
            let totals = CLIMonitorServingTotal.summing(rows: serving)
            let shownTotals = totals.prefix(MonitorSurfaceLimits.maxPushedServingTotalRows)
            out.servingTotals = shownTotals.map { total in
                let row = CLIMonitorServingRow(total: total)
                return MonitorServingTotalMetric(label: row.node,
                                                 model: total.model,
                                                 run: row.run,
                                                 wait: row.wait,
                                                 tokensIn: row.tokensIn,
                                                 tokensOut: row.tokensOut,
                                                 genTps: row.genTps,
                                                 loadLevel: CLIMonitorFormat.loadLevel(
                                                    running: total.running, waiting: total.waiting))
            }
            out.servingTotalsDropped = max(0, totals.count - shownTotals.count)
        }
        if let running = summary.running {
            out.queueLevel = CLIMonitorFormat.loadLevel(running: running,
                                                        waiting: summary.waiting ?? 0)
        }

        out.primary = primaryMetric(nodes: nodes,
                                    servingRunning: summary.running,
                                    servingWaiting: summary.waiting,
                                    nodesUp: out.nodesUp,
                                    nodesTotal: out.nodesTotal)
        out.secondary = secondaryMetric(servingRunning: summary.running,
                                        servingWaiting: summary.waiting,
                                        nodesUp: out.nodesUp,
                                        nodesTotal: out.nodesTotal)
        // A glance that repeats one number twice reads as two facts. The
        // trailing slot is dropped rather than filled with a duplicate of the
        // leading one — the compact region then shows one metric, which is the
        // honest reading of a payload that only told us one thing.
        if out.primary?.kind != nil, out.primary?.kind == out.secondary?.kind {
            out.secondary = nil
        }
        return out
    }

    /// The serving row that belongs to a host, matched by the SAME short-tail
    /// rule the node label uses, so the two tables can never disagree about
    /// which machine a row describes. One owner of that match: this func
    /// (`MonitorContent.make` calls it too).
    static func servingRow(forHost host: String, in rows: [ServingRow]?) -> ServingRow? {
        guard let rows else { return nil }
        let tail = MonitorNode.shortTail(host)
        for candidate in rows where !candidate.isDown {
            guard let address = candidate.node else { continue }
            if MonitorNode.shortTail(address) == tail { return candidate }
        }
        return nil
    }

    /// The one honest metric for compact and minimal, in a fixed order, so the
    /// island always leads with the most specific thing the fleet reported:
    ///
    ///   1. hottest GPU temp, 2. hottest CPU temp, 3. busiest GPU util —
    ///   always labelled with the host it came from, because a per-host number
    ///   without its host reads like a fleet number; 4. the serving run count;
    ///   5. the host tally; 6. nil, and the surface draws `—`.
    ///
    /// There is deliberately NO fleet-wide GPU% here: `/v1/cluster/monitor`
    /// sends a utilization per host and no aggregate, so a mean would be a
    /// number no route sends. "Total GPU%" is therefore the busiest single
    /// host's, with that host named beside it.
    static func primaryMetric(nodes: [MonitorNode]?,
                              servingRunning: Int?,
                              servingWaiting: Int?,
                              nodesUp: Int?,
                              nodesTotal: Int?) -> MonitorPrimaryMetric? {
        let rows = (nodes ?? []).map { CLIMonitorHostRow(node: $0) }

        if let row = hottestRow(rows: rows, key: { $0.gpuTempC }) {
            return MonitorPrimaryMetric(kind: .gpuTemp,
                                        value: FleetFormat.celsius(row.gpuTempC),
                                        label: row.displayLabel,
                                        level: row.gpuTempLevel)
        }
        if let row = hottestRow(rows: rows, key: { $0.cpuTempC }) {
            return MonitorPrimaryMetric(kind: .cpuTemp,
                                        value: FleetFormat.celsius(row.cpuTempC),
                                        label: row.displayLabel,
                                        level: row.cpuTempLevel)
        }
        if let row = hottestRow(rows: rows, key: { $0.gpuUtilPct }) {
            return MonitorPrimaryMetric(kind: .gpuUtil,
                                        value: FleetFormat.pct(row.gpuUtilPct),
                                        label: row.displayLabel,
                                        level: row.gpuUtilLevel)
        }
        if let servingRunning {
            return MonitorPrimaryMetric(kind: .serving,
                                        value: "\(servingRunning) run",
                                        label: nil,
                                        level: CLIMonitorFormat.loadLevel(running: servingRunning,
                                                                          waiting: servingWaiting ?? 0))
        }
        return tallyMetric(nodesUp: nodesUp, nodesTotal: nodesTotal)
    }

    /// The host tally as a metric (`4/4 up`, or `— up` when the route never
    /// counted hosts). Shared by primary and secondary so the same reading is
    /// worded the same way in either slot.
    static func tallyMetric(nodesUp: Int?, nodesTotal: Int?) -> MonitorPrimaryMetric? {
        guard nodesUp != nil else { return nil }
        return MonitorPrimaryMetric(kind: .tally,
                                    value: FleetFormat.up(nodesUp, nodesTotal),
                                    label: nil,
                                    level: nil)
    }

    /// The host reporting the highest of one column, or nil when no host
    /// reported that column at all. The row (not just the number) comes back so
    /// the caller can read the VERDICT off it — `gpuTempLevel` and friends are
    /// the view-model's thresholds, and re-writing `>= 90` here would put a
    /// second copy of a threshold outside the file that owns them.
    static func hottestRow(rows: [CLIMonitorHostRow],
                           key: (CLIMonitorHostRow) -> Double?) -> CLIMonitorHostRow? {
        var best: CLIMonitorHostRow?
        var bestValue: Double?
        for row in rows {
            guard let value = key(row) else { continue }
            if bestValue == nil || value > bestValue! { best = row; bestValue = value }
        }
        return best
    }

    /// The compact trailing metric: a queue first (a waiting request is the
    /// thing worth seeing at a glance), then the live run count, then the host
    /// tally. nil when none of the three was told.
    ///
    /// It is a `MonitorPrimaryMetric` rather than a bare string for the same
    /// reason `primary` is: the surface needs to know WHICH metric it is
    /// drawing to colour it by the CLI's load rule, and sniffing a string for
    /// "wait" would be that decision made twice, in two places, in two
    /// languages.
    static func secondaryMetric(servingRunning: Int?,
                                servingWaiting: Int?,
                                nodesUp: Int?,
                                nodesTotal: Int?) -> MonitorPrimaryMetric? {
        if let waiting = servingWaiting, waiting > 0 {
            return MonitorPrimaryMetric(kind: .waiting,
                                        value: "\(waiting) wait",
                                        label: nil,
                                        level: CLIMonitorFormat.loadLevel(running: servingRunning ?? 0,
                                                                          waiting: waiting))
        }
        if let running = servingRunning, running > 0 {
            return MonitorPrimaryMetric(kind: .serving,
                                        value: "\(running) run",
                                        label: nil,
                                        level: CLIMonitorFormat.loadLevel(running: running,
                                                                          waiting: servingWaiting ?? 0))
        }
        if nodesUp != nil {
            return MonitorPrimaryMetric(kind: .tally,
                                        value: FleetFormat.up(nodesUp, nodesTotal),
                                        label: nil,
                                        level: nil)
        }
        return nil
    }

    /// The host table's serving tail — `3 run · 1 wait`, `-` when the serving
    /// section never answered for this host. Both counts are shown because
    /// `0 run` is a real reading (an idle engine) and must not collapse to `-`.
    static func servingText(running: Int?, waiting: Int?) -> String? {
        guard let running, let waiting else { return nil }
        return "\(running) run · \(waiting) wait"
    }

    /// The serving-totals line under a table: `total (2) · model · 3 run · 1 wait`.
    /// `nil` only when there is no total row to name.
    static func servingTotalText(_ total: MonitorServingTotalMetric,
                                 modelVisible: Bool) -> String {
        var parts = [total.label]
        if modelVisible && !total.model.isEmpty { parts.append(total.model) }
        parts.append("\(total.run) run · \(total.wait) wait")
        return parts.joined(separator: " · ")
    }

    /// The honest note about rows the push cap or the region limit left out:
    /// `+2 more`, or nil when nothing was hidden.
    static func moreText(dropped: Int) -> String? {
        guard dropped > 0 else { return nil }
        return "+\(dropped) more"
    }

    /// The Lock Screen's host rows: at most `lockScreenHostRows`, plus how many
    /// of the rows it was GIVEN the region did not draw. The rows the DRIVER's
    /// cap left out are a separate number (`MonitorSurfacePayload.hostRowsDropped`,
    /// pushed as `hostRowsDropped`), and a surface adds the two before it prints
    /// `+N more` — a count of one clip alone would understate how much of the
    /// fleet is missing, which is the same lie as drawing a short table silently.
    static func lockScreenHostRows(_ rows: [MonitorHostMetric]) -> (shown: [MonitorHostMetric], dropped: Int) {
        let shown = Array(rows.prefix(MonitorSurfaceLimits.lockScreenHostRows))
        return (shown, max(0, rows.count - shown.count))
    }

    /// The expanded island's host rows.
    static func islandHostRows(_ rows: [MonitorHostMetric]) -> (shown: [MonitorHostMetric], dropped: Int) {
        let shown = Array(rows.prefix(MonitorSurfaceLimits.islandHostRows))
        return (shown, max(0, rows.count - shown.count))
    }

    /// The `total (n)` lines the Lock Screen draws.
    static func lockScreenServingTotals(_ totals: [MonitorServingTotalMetric]) -> (shown: [MonitorServingTotalMetric], dropped: Int) {
        let shown = Array(totals.prefix(MonitorSurfaceLimits.lockScreenServingTotals))
        return (shown, max(0, totals.count - shown.count))
    }

    /// The `total (n)` lines the expanded island draws.
    static func islandServingTotals(_ totals: [MonitorServingTotalMetric]) -> (shown: [MonitorServingTotalMetric], dropped: Int) {
        let shown = Array(totals.prefix(MonitorSurfaceLimits.islandServingTotals))
        return (shown, max(0, totals.count - shown.count))
    }

    /// The queue line for the Lock Screen / expanded footer: the aggregate the
    /// serving table reported, spelled out (the compact form abbreviates it).
    static func servingSummaryText(running: Int?, waiting: Int?) -> String? {
        guard let running, let waiting else { return nil }
        return "\(running) run · \(waiting) waiting"
    }

    // MARK: - Host-table columns
    //
    // A lock-screen row cannot carry nine columns, so the surfaces show a
    // SUBSET — and the subset, its headers and its values are decided HERE, not
    // in the view, for two reasons: the header text comes from the view-model's
    // own `CLIMonitorHostRow.columns` (a surface retyping "GPU Temp" is a string
    // that can drift away from the column it labels), and `model_decode_check`
    // can only assert what it can execute.

    /// The four host columns the Lock Screen and the expanded island draw: the
    /// utilization the operator asked about, both temperatures, and the power
    /// draw. Selected from the CLI's own column names by position;
    /// `monitor_surface_check` asserts these ARE the four names, so a reordered
    /// `CLIMonitorHostRow.columns` fails by name instead of silently labelling
    /// a wattage "GPU %".
    static let detailHostColumns: [String] = [
        CLIMonitorHostRow.columns[5],   // "GPU%"
        CLIMonitorHostRow.columns[6],   // "CPU Temp"
        CLIMonitorHostRow.columns[7],   // "GPU Temp"
        CLIMonitorHostRow.columns[8],   // "GPU Power"
    ]

    /// The serving table's Run/Wait column names, same rule.
    static var runColumnName: String { CLIMonitorServingRow.wideColumns[4] }
    static var waitColumnName: String { CLIMonitorServingRow.wideColumns[5] }

    /// One host's detail cells, in `detailHostColumns` order — the CLI's own
    /// strings, so a lock-screen `87 C` is `cluster_live.py`'s `87 C`.
    static func detailCells(_ row: MonitorHostMetric) -> [String] {
        [row.gpuPct, row.cpuTemp, row.gpuTemp, row.gpuPower]
    }

    /// The verdict behind each detail cell, same order, for the surface to
    /// paint. `nil` = no verdict, which renders neutral, never green.
    static func detailLevels(_ row: MonitorHostMetric) -> [ClusterMetricLevel?] {
        [row.gpuUtilLevel, row.cpuTempLevel, row.gpuTempLevel, row.gpuPowerLevel]
    }

    /// What a host whose sample never arrived says instead of four dashes. A
    /// fixed phrase, deliberately NOT the route's own error text: the CLI
    /// prints that reason in the CPU % column, and copying it into a Live
    /// Activity's stored state would push a string this app does not control
    /// into storage that outlives the read (§7).
    static let notSampledText = "not sampled"
}
