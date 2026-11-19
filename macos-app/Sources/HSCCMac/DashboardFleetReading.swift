import Foundation

// ===========================================================================
// The menu bar dropdown's cluster reading, and the fleet numbers its TITLE
// draws.
//
// `hscc cluster monitor` prints two tables (`hscc_daemon/cluster_live.py`:
// `host_table()` and `serving_table()`) and a few note lines. The menu bar app
// shows exactly those, so EVERY string in this file is either a cell of
// `ClusterMonitorViewModel` — the view-model `cluster_monitor_cli_parity.py`
// pins against the CLI's own functions on the same fixture bytes (t_61ed3f5e) —
// or one of the CLI's literal note lines, quoted verbatim below.
//
// THREE RULES THIS FILE ENFORCES BY CONSTRUCTION.
//
//   1. It formats nothing. No cell string is assembled here: a host cell is
//      read out of `CLIMonitorHostRow.cells` at the index its CLI column NAME
//      resolves to, and a serving cell out of `cells(wide:)` the same way
//      (the same lookup `ClusterSmallGrid.Column` already uses for the small
//      tile — reused rather than restated, so the dropdown and the tile cannot
//      disagree about which column a number is). Rename a CLI column in the
//      view-model and the lookup falls back to `-`; it cannot silently
//      mislabel a value, which is what a positional pick would do.
//   2. It shows no address. The CLI's Host and Node columns are the node's
//      address, and §7 forbids one in a surface: the row label is
//      `CLIMonitorHostRow.displayLabel` / `MonitorNode.shortTail`, the same two
//      rules the widgets already draw by. `idle_hosts` from
//      `/v1/cluster/status` is therefore drawn as a COUNT only — those strings
//      are raw `sparkrun status` lines (`node_0  192.0.2.1  Up 12 hours  <image>`)
//      and a dropdown is not where an address belongs.
//   3. "Not told" stays nil and draws `-`, never `0` (§6 rule 1). A serving
//      section the route did not send is not "no backends"; a degraded
//      `/v1/cluster/status` is not "0 hosts"; a host with no sample is a dash
//      row, not a missing host.
//
// FOUNDATION-ONLY ON PURPOSE, like the two files it reads: no `Color`, no
// SwiftUI. That is what lets a harness compile and RUN the bytes the shipped
// dropdown runs, the way `cluster_widget_check.sh` runs the tile's table.
// ===========================================================================

// MARK: - Host rows

/// One host row of the dropdown: the four CLI columns the `systemSmall` tile
/// already draws, plus that host's sparkrun slot usage.
struct DashboardHostRow: Equatable, Identifiable {
    /// Wire host value — identity only, never drawn (§7). `ClusterSmallGrid.Row`
    /// keys the same way for the same reason.
    let id: String
    /// What the row prints in place of the CLI's Host cell.
    let label: String
    /// GPU%, GPU Temp, CPU Temp, GPU Power, in `ClusterSmallGrid.Column` order.
    let cells: [ClusterSmallGrid.Cell]
    /// The CLI's error row: no sample, so every numeric cell is `-`.
    let isDown: Bool
    /// `used_slots` / `free_slots` — the slot pair `hscc cluster status`
    /// (sparkrun) counts. nil = the route did not send it for this host.
    let usedSlots: Int?
    let freeSlots: Int?

    init(node: MonitorNode, row: CLIMonitorHostRow) {
        id = row.host
        label = row.displayLabel
        cells = ClusterSmallGrid.Column.allCases.map { ClusterSmallGrid.Cell(column: $0, row: row) }
        isDown = row.error != nil
        usedSlots = node.used_slots
        freeSlots = node.free_slots
    }

    /// VoiceOver gets the numbers the dense row cannot fit: which host, every
    /// cell, and the slot pair.
    var accessibilityLabel: String {
        var parts = [label]
        parts.append(contentsOf: cells.map { "\($0.column.cliColumn) \($0.text)" })
        if let usedSlots, let freeSlots {
            parts.append("\(usedSlots) used, \(freeSlots) free slots")
        }
        return parts.joined(separator: ", ")
    }
}

// MARK: - Serving rows

/// One row of `serving_table()`: one (backend, model), or one of the
/// `total (n)` rows the CLI appends per model.
struct DashboardServingRow: Equatable, Identifiable {
    /// The five load/token columns the dropdown draws, in the CLI's order.
    /// Names are the CLI's own `serving_table` column titles, resolved against
    /// `CLIMonitorServingRow.narrowColumns` — the row shape the CLI builds for
    /// a terminal narrower than `WIDE_MIN`, which is what a ~330 pt dropdown is.
    enum Column: String, CaseIterable, Identifiable {
        case run, wait, kvPct, tokensIn, tokensOut

        var id: String { rawValue }

        var cliColumn: String {
            switch self {
            case .run: return "Run"
            case .wait: return "Wait"
            case .kvPct: return "KV%"
            case .tokensIn: return "In"
            case .tokensOut: return "Out"
            }
        }

        /// Where this column sits in the narrow serving row, per the CLI's own
        /// column list. Nil means the view-model renamed it, and the cell then
        /// prints `-` rather than whatever sits at the old index.
        var narrowIndex: Int? {
            CLIMonitorServingRow.narrowColumns.firstIndex(of: cliColumn)
        }

        func cellText(in row: CLIMonitorServingRow) -> String {
            guard let index = narrowIndex else { return CLIMonitorFormat.dash }
            let cells = row.cells(wide: false)
            return cells.indices.contains(index) ? cells[index] : CLIMonitorFormat.dash
        }
    }

    let id: String
    /// The Node cell, shortened to the `.NNN` tail (§7). A `total (n)` row's
    /// node cell is that row's own label, so it passes through unchanged.
    let nodeLabel: String
    let port: String
    /// The Model cell as the CLI prints it (`down` for a dead backend).
    let model: String
    let cells: [Column: String]
    let isTotalRow: Bool
    /// The CLI's `_load_style` verdict for this row, which is the only
    /// threshold the monitor CLI applies anywhere.
    let level: ClusterMetricLevel

    init(row: CLIMonitorServingRow) {
        id = row.id
        let node = row.node
        nodeLabel = row.isTotalRow || node.isEmpty
            ? node
            : MonitorNode.shortTail(node)
        port = row.port
        model = row.model
        var cells: [Column: String] = [:]
        for column in Column.allCases { cells[column] = column.cellText(in: row) }
        self.cells = cells
        isTotalRow = row.isTotalRow
        level = row.loadLevel
    }

    /// The CLI's `total (n)` row, through the same cell lookup as a backend row
    /// — one path, so a total can never be formatted differently from the rows
    /// it totals.
    init(total: CLIMonitorServingTotal) {
        self.init(row: CLIMonitorServingRow(total: total))
    }

    /// Cells in `Column.allCases` order, so a view draws the visual order
    /// without restating it.
    var orderedCells: [String] { Column.allCases.map { cells[$0] ?? CLIMonitorFormat.dash } }

    var accessibilityLabel: String {
        ([nodeLabel, port, model].filter { !$0.isEmpty } + orderedCells)
            .joined(separator: ", ")
    }
}

// MARK: - The dropdown's monitor section

/// `hscc cluster monitor`'s whole human view as the dropdown draws it. `nil`
/// from `tile(from:response:)` means there was nothing to draw — a degraded
/// `{speak}` body — and the caller shows why instead of a table of dashes.
enum DashboardFleetReading {
    /// Rows the dropdown has room for. The menu-bar window is a fixed 360 pt
    /// pane with no scroll view, so a fleet is capped rather than truncated off
    /// the bottom of the screen — and the cap is PRINTED (`hiddenHosts`,
    /// `hiddenServingRows`) so the pane never reads as the whole fleet.
    static let hostRowLimit = 6
    static let servingRowLimit = 6

    /// The pane's own words. The note lines that used to live here are GONE:
    /// they moved to `CLIMonitorNote`
    /// (`ios-app/Sources/Shared/ClusterMonitorViewModel.swift`) when the iOS
    /// tables block gained the note the dropdown had always drawn, and this pane
    /// now prints `view.note?.text` — the shared owner's sentence, so the phone
    /// and the desktop cannot re-word the same failure differently. `tokensNote`
    /// stays as an alias because the header below still names it.
    enum Copy {
        /// `render`'s title line: `hscc cluster monitor — N host(s) — …`.
        static func hosts(_ count: Int) -> String { "\(count) host(s)" }
        /// `render`'s dim note beside the serving header — the shared owner's.
        static var tokensNote: String { CLIMonitorNote.tokensNote }
    }

    struct Tile: Equatable {
        let rows: [DashboardHostRow]
        let hiddenHosts: Int
        /// `host_table`'s count — the CLI title line's number, hosts that were
        /// dropped from the view included.
        let hostCount: Int
        /// Backend rows (capped at `servingRowLimit`) followed by EVERY
        /// per-model `total (n)` row: the CLI appends the totals to the same
        /// table, and this pane never lets a long backend list push them out.
        let serving: [DashboardServingRow]
        /// Backend rows the pane could not fit. Never counts a total row.
        let hiddenServingRows: Int
        /// The per-model totals line, shared with the small tile.
        let totals: ClusterSmallGrid.ServingTotals
        /// The CLI's note line for this state, or nil when it prints none.
        let note: String?
        /// Whether the route sent a serving section at all. nil-drawing is
        /// `ServingTotals.notTold`; false is the CLI's `no backends` line.
        let hasServingSection: Bool

        var accessibilityLabel: String {
            var parts = [Copy.hosts(hostCount)]
            parts.append(contentsOf: rows.map(\.accessibilityLabel))
            if hiddenHosts > 0 { parts.append("\(hiddenHosts) more not shown") }
            parts.append(contentsOf: serving.map(\.accessibilityLabel))
            if let note { parts.append(note) }
            return parts.joined(separator: ". ")
        }
    }

    /// Project the view-model plus the raw response (the slot pair lives on
    /// `MonitorNode`, which the view-model's row deliberately does not carry).
    ///
    /// `hostRows` are `response.json?.hosts` mapped in order by the view-model,
    /// so `zip` pairs a row with its own node; a payload whose two halves differ
    /// in length pairs the prefix, which is the honest truncation.
    static func tile(from view: ClusterMonitorViewModel?,
                     response: MonitorResponse?) -> Tile? {
        guard let view, !view.hostRows.isEmpty || !view.servingRows.isEmpty else { return nil }
        let nodes = response?.json?.hosts ?? []
        let paired = zip(nodes, view.hostRows).map { DashboardHostRow(node: $0, row: $1) }
        let shownHosts = Array(paired.prefix(hostRowLimit))

        // Backend rows are capped; the per-model `total (n)` rows NEVER are.
        // They are the two lines the operator actually reads off the serving
        // table, there is one per model rather than per backend, and a pane that
        // pushed them out behind a long backend list would answer the question
        // "how many engines" while hiding "how much work".
        let backends = view.backendRows.map(DashboardServingRow.init(row:))
        let totals = view.totalRows.map(DashboardServingRow.init(total:))
        let shownBackends = Array(backends.prefix(servingRowLimit))
        let servingRows = shownBackends + totals

        // One owner for the sentence AND its precedence: the shared
        // `CLIMonitorNote` the iOS tables block now draws too. The CLI prints
        // two lines here when the map is stale and the table empty; a pane with
        // one line of room prints the first, as the dropdown always has.
        let note = view.note

        return Tile(rows: shownHosts,
                    hiddenHosts: max(0, paired.count - hostRowLimit),
                    hostCount: view.hostCount,
                    serving: servingRows,
                    hiddenServingRows: backends.count - shownBackends.count,
                    totals: ClusterSmallGrid.ServingTotals.summing(
                        view.totalRows, hasServingSection: view.hasServingSection),
                    note: note?.text,
                    hasServingSection: view.hasServingSection)
    }
}

// MARK: - The cluster-status section

/// One row of `render_cluster_status`'s workload table.
///
/// The CLI's table has four columns (workload, tp, pp, container); this row
/// carries the first three. The container id is deliberately absent: it is a
/// sparkrun container identifier, and the app's standing rule for those is the
/// one `SharedModels.swift` states for `sparkrun_job_names` — a container name
/// does not reach app storage, let alone a menu bar.
struct DashboardWorkloadRow: Equatable, Identifiable {
    /// Name + position, because `sparkrun status` legitimately lists the SAME
    /// recipe more than once (three replicas of one yaml in the committed
    /// fixture), and an `Identifiable` keyed on the name alone would hand
    /// SwiftUI three rows with one id.
    let id: String
    let name: String
    let tp: String
    let pp: String
}

/// One host's sparkrun slot pair. `used_slots`/`free_slots` ride on
/// `/v1/cluster/monitor`'s host objects — the status route has no per-node
/// slots — so this row is drawn under the status heading with its source named
/// in the section's own comment rather than invented from the tally.
struct DashboardNodeSlot: Equatable, Identifiable {
    let id: String
    let label: String
    let used: Int
    let free: Int
}

/// `hscc cluster status`'s human view: `render_cluster_status`'s workload table
/// plus its `cluster summary` panel (`total hosts`, `idle hosts`), and the
/// slot pair the operator reads next to it.
enum ClusterStatusReading {
    /// Workload rows the pane has room for; the rest are counted, not dropped
    /// silently.
    static let workloadRowLimit = 4
    static let slotRowLimit = 6

    /// `render_cluster_status`'s labels, quoted verbatim (`total hosts`,
    /// `idle hosts`, `no running workloads`).
    enum Copy {
        static let totalHosts = "total hosts"
        static let idleHosts = "idle hosts"
        static let noWorkloads = "no running workloads"
    }

    struct Panel: Equatable {
        let workloads: [DashboardWorkloadRow]
        let hiddenWorkloads: Int
        /// `total hosts 4 · idle hosts 4` — the summary panel's two lines on
        /// one row. `render_cluster_status` prints both unconditionally, and
        /// `handle_cluster_status` answers them even when the fleet is empty,
        /// so they are only absent when the whole read failed.
        let hostsLine: String
        let slots: [DashboardNodeSlot]
        let hiddenSlots: Int

        var accessibilityLabel: String {
            var parts: [String] = [hostsLine]
            parts.append(contentsOf: workloads.map { "workload \($0.name), tp \($0.tp), pp \($0.pp)" })
            if hiddenWorkloads > 0 { parts.append("\(hiddenWorkloads) more workloads not shown") }
            parts.append(contentsOf: slots.map { "\($0.label), \($0.used) used, \($0.free) free slots" })
            return parts.joined(separator: ". ")
        }
    }

    /// Project both halves. `status == nil` (the read failed) or a route that
    /// answered with its own `error` gives nil — `total_hosts: 0` from a
    /// degraded route must never be drawn as an empty cluster, which is the
    /// reason `ClusterStatusResponse.error` exists.
    static func panel(status: ClusterStatusResponse?,
                      hosts: [MonitorNode]?) -> Panel? {
        guard let status, status.error == nil else { return nil }
        let rows = status.workloads.prefix(workloadRowLimit).enumerated().map { index, workload in
            DashboardWorkloadRow(id: "\(workload.name)#\(index)",
                                 name: workload.name,
                                 tp: workload.tp ?? "?",
                                 pp: workload.pp ?? "?")
        }
        // Slots: only hosts the monitor listed AND reported a slot pair for.
        var slots: [DashboardNodeSlot] = []
        for node in hosts ?? [] where node.used_slots != nil || node.free_slots != nil {
            slots.append(DashboardNodeSlot(id: node.host,
                                           label: node.displayLabel,
                                           used: node.used_slots ?? 0,
                                           free: node.free_slots ?? 0))
        }
        let shownSlots = Array(slots.prefix(slotRowLimit))
        let line = [
            "\(Copy.totalHosts) \(status.total_hosts)",
            "\(Copy.idleHosts) \(status.idle_hosts.count)"
        ].joined(separator: " · ")
        return Panel(workloads: Array(rows),
                     hiddenWorkloads: max(0, status.workloads.count - workloadRowLimit),
                     hostsLine: line,
                     slots: shownSlots,
                     hiddenSlots: max(0, slots.count - slotRowLimit))
    }
}

// MARK: - The dense menu-bar title

/// The fleet numbers the menu-bar TITLE draws: fleet GPU load, the hottest
/// temperature in the fleet, and the fleet's total reported draw.
///
/// Two of these are CLI cells and one is not:
///
///   * the temperature IS a CLI cell — the winning host's own `GPU Temp` or
///     `CPU Temp` string, so the title and the dropdown row agree by
///     construction;
///   * GPU load is a MEAN over the hosts that reported one. `host_table` has no
///     fleet-mean column, so there is no golden for it; the `%.0f` is borrowed
///     from `host_table`'s own GPU% formatting and the word `avg` says what it
///     is. A sum would be a number the CLI never prints and no operator can
///     interpret against a 0-100 per-host metric.
///   * watts are a SUM, likewise absent from the CLI, printed whole because a
///     fleet total in two decimals is noise in a menu bar. A per-host cell is
///     still `%.2f W`; only the sum is whole.
///
/// A metric no host reported is nil, and a nil drops its part from the title
/// rather than printing `0`, `-` or `0 W` — the same rule every other surface
/// in the app runs (§6 rule 1).
enum MenuBarFleet {
    enum Sensor: String {
        case gpu = "GPU"
        case cpu = "CPU"
    }

    struct Hottest: Equatable {
        /// The host the reading came from (`.NNN` tail or hostname, never an
        /// address) — carried so the VoiceOver sentence can name it.
        let label: String
        let sensor: Sensor
        /// The CLI's own cell for that host and sensor, `49 C`.
        let cell: String
        let tempC: Double
    }

    struct Stats: Equatable {
        let hostCount: Int
        /// Mean `gpu_util_pct` over the hosts that reported one.
        let avgGpuUtilPct: Double?
        /// How many hosts that mean came from, and out of how many.
        let gpuUtilHosts: Int
        let hostsTotal: Int
        /// Summed `gpu_power_w` over the hosts that reported it.
        let totalPowerW: Double?
        let powerHosts: Int
        let hottest: Hottest?

        /// The host reported nothing readable.
        static let noAnswer = Stats(hostCount: 0, avgGpuUtilPct: nil, gpuUtilHosts: 0,
                                    hostsTotal: 0, totalPowerW: nil, powerHosts: 0,
                                    hottest: nil)

        /// Reduce the CLI rows. The numbers come from `CLIMonitorHostRow`'s
        /// parsed doubles — "parsed from the same reads, never re-derived from
        /// the formatted strings", per the view-model — and the drawn
        /// temperature comes from its cell.
        static func project(_ view: ClusterMonitorViewModel?) -> Stats {
            guard let view, !view.hostRows.isEmpty else { return .noAnswer }
            let rows = view.hostRows

            let utils = rows.compactMap(\.gpuUtilPct)
            let avg = utils.isEmpty ? nil : utils.reduce(0, +) / Double(utils.count)

            let powers = rows.compactMap(\.gpuPowerW)
            let totalW = powers.isEmpty ? nil : powers.reduce(0, +)

            // Hottest of the two thermal columns. A tie goes to GPU: the GPU
            // die is the reading an operator acts on, and `GPU Temp` is the
            // column the CLI prints first of the pair.
            var best: Hottest?
            for row in rows {
                for column in [ClusterSmallGrid.Column.gpuTemp, .cpuTemp] {
                    guard let value = thermalValue(column, row) else { continue }
                    let sensor: Sensor = column == .gpuTemp ? .gpu : .cpu
                    if let current = best, current.tempC >= value { continue }
                    best = Hottest(label: row.displayLabel, sensor: sensor,
                                   cell: column.cellText(in: row), tempC: value)
                }
            }

            return Stats(hostCount: view.hostCount,
                         avgGpuUtilPct: avg,
                         gpuUtilHosts: utils.count,
                         hostsTotal: rows.count,
                         totalPowerW: totalW,
                         powerHosts: powers.count,
                         hottest: best)
        }

        private static func thermalValue(_ column: ClusterSmallGrid.Column,
                                         _ row: CLIMonitorHostRow) -> Double? {
            switch column {
            case .gpuTemp: return row.gpuTempC
            case .cpuTemp: return row.cpuTempC
            case .gpuUtil, .gpuPower: return nil
            }
        }

        // MARK: Title strings

        /// `195 W`, or nil when no host reported a wattage.
        var wattsText: String? {
            totalPowerW.map { String(format: "%.0f W", $0) }
        }

        /// `48% · 195 W`. nil when no host reported GPU load: the icon alone is
        /// honest, a `0%` is not.
        func gpuLoadTitle() -> String? {
            guard let avg = avgGpuUtilPct else { return nil }
            return joinParts([String(format: "%.0f%%", avg), wattsText])
        }

        /// `49 C · 195 W`, the CLI's cell for the hottest host and sensor.
        func hottestTitle() -> String? {
            guard let hottest else { return nil }
            return joinParts([hottest.cell, wattsText])
        }

        /// The full sentence the dense title had to drop: which hosts each
        /// fleet number came from.
        func gpuLoadAccessibilityText() -> String? {
            guard let avg = avgGpuUtilPct else { return nil }
            var parts = [String(format: "GPU avg %.0f%% over %d of %d hosts",
                                avg, gpuUtilHosts, hostsTotal)]
            if let wattsText {
                parts.append("\(wattsText) total over \(powerHosts) hosts")
            }
            return parts.joined(separator: ", ")
        }

        /// The host and sensor the dense title had to drop.
        func hottestAccessibilityText() -> String? {
            guard let hottest else { return nil }
            var parts = ["hottest \(hottest.sensor.rawValue) \(hottest.cell) on \(hottest.label)"]
            if let wattsText { parts.append("\(wattsText) total draw") }
            return parts.joined(separator: ", ")
        }

        private func joinParts(_ parts: [String?]) -> String? {
            let kept = parts.compactMap { $0 }
            return kept.isEmpty ? nil : kept.joined(separator: " · ")
        }
    }
}
