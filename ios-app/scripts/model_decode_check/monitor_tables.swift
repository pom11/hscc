import Foundation

// ===========================================================================
// monitor_tables.swift — the large / extra-large layout check (t_9e64d9d9).
//
// Compiled by model_decode_check.sh alongside main.swift and the REAL model +
// view-model + layout sources. Declarations only: Swift forbids top-level
// statements outside main.swift, which holds the calls.
//
// WHAT THIS COVERS AND WHAT `monitor_parity.swift` COVERS
//
// Parity owns the CELLS: every host and serving string is compared there
// against goldens generated from `hscc_daemon/cluster_live.py`. This file owns
// what the CLI does not have: the SELECTION (which columns a widget width
// draws), the CLIP (how many rows, and whether a clipped table says so), and
// the six XL detail fields (VRAM, GPU clock, net, uptime, fan, load) that
// `host_table()` has no column for. Those six are THIS app's formatting rule,
// so they are pinned by exact inputs and by fixture values rather than by a
// CLI golden — and the first block below asserts the rules with literals chosen
// so no decimal rounding is involved in the claim.
//
// THE LOAD-BEARING ASSERTION is `columnsMatchCLI`: for every column the plan
// names, its header and its cell are the CLI's header and cell at the same
// index of the same row. A column that started reading a different field, or a
// header that quietly became a different word, fails here instead of shipping a
// table that looks like the CLI's and is not.
// ===========================================================================

/// The exact-input rules for the bytes/s formatter: the SI steps, the dash for
/// "not told", and the boundary between B/s and KB/s.
func monitorRateRules(_ c: DecodeCheck) {
    let label = "detail rate rule (SI steps, dash for unreadable)"
    let cases: [(Double?, String)] = [
        (nil, FleetFormat.dash),
        (0, "0 B/s"),
        (999, "999 B/s"),
        (1_000, "1.0 KB/s"),
        (1_418_500, "1.4 MB/s"),
        (2_400_000_000, "2.4 GB/s"),
        (-1, FleetFormat.dash),
    ]
    for (input, want) in cases {
        let got = ClusterMonitorHostDetail.rate(input)
        c.expect("monitor-tables", "\(label): \(String(describing: input)) -> \(want)",
                 got == want, "got \(got)")
    }
}

/// Detail rules that need values the live capture does not carry (the GB10 has
/// no VRAM or fan sensor), asserted on a hand-built sample so the claim is
/// about the rule, not about the capture.
func monitorDetailRules(_ c: DecodeCheck) {
    let label = "detail field rules (VRAM/clock/fan/load)"
    /// The arguments are passed in `MonitorSample.init`'s declared order — Swift
    /// requires that even when the skipped parameters are defaulted, and this
    /// init's order is NOT the property order (the pre-t_61ed3f5e keys keep
    /// theirs and the new ones append).
    func sample(uptime: String? = nil, vramPct: String? = nil, load1: String? = nil,
                load5: String? = nil, load15: String? = nil, vramUsed: String? = nil,
                vramTotal: String? = nil, clock: String? = nil, fan: String? = nil) -> MonitorSample {
        MonitorSample(uptime_sec: uptime, gpu_mem_used_pct: vramPct, cpu_load_1m: load1,
                      cpu_load_5m: load5, cpu_load_15m: load15,
                      gpu_mem_used_mb: vramUsed, gpu_mem_total_mb: vramTotal,
                      gpu_clock_mhz: clock, gpu_fan_pct: fan)
    }
    func detail(_ host: String, _ sample: MonitorSample) -> ClusterMonitorHostDetail {
        ClusterMonitorHostDetail(host: MonitorNode(host: host, error: nil, sample: sample,
                                                   used_slots: nil, free_slots: nil,
                                                   workloads: nil))
    }
    let full = detail("192.0.2.31", sample(uptime: "7200", vramPct: "50", load1: "1.0",
                                           load5: "2.0", load15: "3.0", vramUsed: "8192",
                                           vramTotal: "16384", clock: "1965", fan: "60"))
    c.expect("monitor-tables", "\(label): 8192/16384 MB -> 8.0/16.0G",
             full.vram == "8.0/16.0G", "got \(full.vram)")
    c.expect("monitor-tables", "\(label): clock 1965 -> 1965 MHz",
             full.clock == "1965 MHz", "got \(full.clock)")
    c.expect("monitor-tables", "\(label): fan 60 -> 60%", full.fan == "60%", "got \(full.fan)")
    c.expect("monitor-tables", "\(label): load triple keeps its three slots",
             full.load == "1.0 2.0 3.0", "got \(full.load)")
    c.expect("monitor-tables", "\(label): 7200 s -> 2h 0m",
             full.uptime == "2h 0m", "got \(full.uptime)")
    c.expect("monitor-tables", "\(label): VRAM at 50% is .ok, not painted",
             full.vramLevel == .ok, "got \(full.vramLevel.rawValue)")

    // One VRAM side missing is a dash, not a fraction of a guess: `8192/-` is
    // not a utilisation anyone can read, and `0/-` would be a false zero.
    let half = detail("192.0.2.32", sample(vramUsed: "8192"))
    c.expect("monitor-tables", "\(label): VRAM with no total is a dash",
             half.vram == FleetFormat.dash && half.vramLevel == .unknown,
             "got \(half.vram) / \(half.vramLevel.rawValue)")

    // A missing load member keeps its slot: the two that answered must not
    // slide left into the 15-minute position.
    let partial = detail("192.0.2.33", sample(load1: "1.0", load15: "3.0"))
    c.expect("monitor-tables", "\(label): an unreadable load member keeps its slot",
             partial.load == "1.0 \(FleetFormat.dash) 3.0", "got \(partial.load)")

    // Nothing reported at all is one dash for load, and no invented number for
    // any other field.
    let none = detail("192.0.2.34", sample())
    c.expect("monitor-tables", "\(label): nothing reported is a dash in every field",
             none.load == FleetFormat.dash && none.fan == FleetFormat.dash
                && none.vram == FleetFormat.dash && none.clock == FleetFormat.dash
                && none.uptime == FleetFormat.dash
                && none.net == "↓\(FleetFormat.dash) ↑\(FleetFormat.dash)",
             "load=\(none.load) fan=\(none.fan) net=\(none.net) up=\(none.uptime)")

    // The six fields, in the order the block prints them, with a threshold on
    // VRAM only — an un-thresholded field must never be painted a status hue.
    let names = full.fields.map(\.label)
    c.expect("monitor-tables", "\(label): six fields in VRAM/clock/net/up/fan/load order",
             names == ["VRAM", "GPU clock", "Net", "Up", "Fan", "Load"], names.joined(separator: ","))
    let thresholded = full.fields.filter { $0.level != nil }.map(\.label)
    c.expect("monitor-tables", "\(label): only VRAM has a threshold",
             thresholded == ["VRAM"], thresholded.joined(separator: ","))
}

/// The plan each surface gets, including the surfaces that get no table.
func monitorPlans(_ c: DecodeCheck) {
    let label = "table plan"
    let large = ClusterMonitorTableLayout.plan(for: .large)
    let extra = ClusterMonitorTableLayout.plan(for: .extraLarge)
    c.expect("monitor-tables", "\(label): large has a plan", large != nil, "nil")
    c.expect("monitor-tables", "\(label): extraLarge has a plan", extra != nil, "nil")
    c.expect("monitor-tables", "\(label): every other surface draws no table",
             [FleetSurface.small, .medium, .inline, .circular, .rectangular]
                .allSatisfy { ClusterMonitorTableLayout.plan(for: $0) == nil })

    if let large {
        c.expect("monitor-tables", "\(label): large draws all nine host columns",
                 large.hostColumns == ClusterMonitorHostColumn.allCases,
                 large.hostColumns.map(\.rawValue).joined(separator: ","))
        c.expect("monitor-tables", "\(label): large draws Node Port Model Run Wait KV% In Out",
                 large.servingColumns == [.node, .port, .model, .run, .wait, .kvPct,
                                          .tokensIn, .tokensOut],
                 large.servingColumns.map(\.rawValue).joined(separator: ","))
        c.expect("monitor-tables", "\(label): large abbreviates the header words",
                 large.compactHeaders, "full headers at 320 pt")
        c.expect("monitor-tables", "\(label): large does not draw the XL detail block",
                 !large.showsHostDetail)
    }
    if let extra {
        c.expect("monitor-tables", "\(label): extraLarge keeps the CLI's header words",
                 !extra.compactHeaders, "abbreviated headers at 660 pt")
        c.expect("monitor-tables", "\(label): extraLarge draws the per-host detail block",
                 extra.showsHostDetail)
        c.expect("monitor-tables", "\(label): extraLarge fits more serving rows than large",
                 extra.servingRowLimit > (ClusterMonitorTableLayout.plan(for: .large)?.servingRowLimit ?? 0),
                 "extra \(extra.servingRowLimit) vs large \(ClusterMonitorTableLayout.plan(for: .large)?.servingRowLimit ?? 0)")
        c.expect("monitor-tables", "\(label): extraLarge never drops a serving column large shows",
                 Set(ClusterMonitorTableLayout.plan(for: .large)?.servingColumns ?? [])
                    .isSubset(of: Set(extra.servingColumns)))
    }
    // The clip rule, both sides of it: a clipped table says how much it hid,
    // and an un-clipped one invents nothing.
    c.expect("monitor-tables", "\(label): a clipped block reports the hidden rows",
             ClusterMonitorTableLayout.overflowLine(shown: 8, total: 10) == "+2 more",
             "got \(ClusterMonitorTableLayout.overflowLine(shown: 8, total: 10) ?? "nil")")
    c.expect("monitor-tables", "\(label): an un-clipped block adds no line",
             ClusterMonitorTableLayout.overflowLine(shown: 4, total: 4) == nil
                && ClusterMonitorTableLayout.overflowLine(shown: 4, total: 2) == nil)
}

/// The column/CLI coupling, run over both real captures. This is the check that
/// makes "the table is the CLI's table" a measured fact rather than a claim in
/// a comment: every column's header is the CLI's word at that index and every
/// column's cell is the CLI's string at that index.
func monitorColumnsMatchCLI(_ c: DecodeCheck, fixture: String) {
    let label = "table columns are the CLI's cells"
    guard let response = try? JSONDecoder().decode(MonitorResponse.self,
                                                   from: try c.load(fixture)),
          let tables = ClusterMonitorTables.project(response),
          let plan = ClusterMonitorTableLayout.plan(for: .large) else {
        c.expect(fixture, label, false, "the fixture did not project tables")
        return
    }
    var hostProblem = ""
    var labelProblem = ""
    for row in tables.hostRows {
        // Column 0 is the ONE deliberate difference from the CLI's row, and it
        // is the parent view-model's rule rather than this file's: the CLI's
        // cell is the wire host, while a surface must print `displayLabel`
        // because drawing a node's address on the Home Screen is the §7
        // violation. Asserted BOTH ways below, so the exclusion is a pinned
        // rule and not a hole in the loop.
        if ClusterMonitorHostColumn.host.cell(in: row) != row.displayLabel {
            labelProblem = "\(row.host): host column != displayLabel"
        }
        if row.cells[0] != row.host {
            labelProblem = "\(row.host): the CLI's own host cell is not the wire host"
        }
        for (index, column) in ClusterMonitorHostColumn.allCases.enumerated() where index > 0 {
            if column.cell(in: row) != row.cells[index] {
                hostProblem = "\(row.host) column \(index) (\(column.rawValue)): "
                    + "\(column.cell(in: row)) != \(row.cells[index])"
            }
        }
    }
    c.expect(fixture, "\(label) — the eight numeric host columns × \(tables.hostRows.count) rows",
             hostProblem.isEmpty, hostProblem)
    c.expect(fixture, "\(label) — column 0 is displayLabel while the CLI's cell stays the wire host (§7)",
             labelProblem.isEmpty, labelProblem)

    var headerProblem = ""
    for (index, column) in ClusterMonitorServingColumn.allCases.enumerated() {
        if column.header != CLIMonitorServingRow.wideColumns[index] {
            headerProblem = "column \(index): \(column.header) != "
                + CLIMonitorServingRow.wideColumns[index]
        }
        if column.indexInWideRow != index {
            headerProblem = "column \(column.rawValue) index \(column.indexInWideRow) != \(index)"
        }
    }
    c.expect(fixture, "\(label) — eleven serving headers in the CLI's order",
             headerProblem.isEmpty, headerProblem)

    var cellProblem = ""
    for row in tables.servingRows {
        for column in plan.servingColumns {
            if row.cells(wide: true)[column.indexInWideRow] != column.cell(in: row) {
                cellProblem = "\(row.id) \(column.rawValue): "
                    + "\(row.cells(wide: true)[column.indexInWideRow]) != \(column.cell(in: row))"
            }
        }
        // The selected eight are the CLI's NARROW row minus its last cell
        // (`Gen t/s`), which is the drop this card documents in its PR body.
        let drawn = plan.servingColumns.map { $0.cell(in: row) }
        let narrowWithoutGen = Array(row.cells(wide: false).dropLast())
        if drawn != narrowWithoutGen {
            cellProblem = "\(row.id): drawn \(drawn) != narrow-minus-Gen \(narrowWithoutGen)"
        }
    }
    c.expect(fixture, "\(label) — drawn serving columns equal narrow row minus Gen t/s",
             cellProblem.isEmpty, cellProblem)
}

/// The whole projection against the live capture and the edge capture: what the
/// two sizes would actually draw, and what a dash means there.
func monitorTablesFromFixtures(_ c: DecodeCheck) {
    let label = "large/XL tables from the capture"

    // No response, and the fleet-down body that carries no `json` key, both
    // project to nil — the same rule FleetProjection uses, so the two surfaces
    // cannot disagree about a powered-off fleet (§6 rule 1).
    c.expect("monitor-tables", "\(label): a failed read projects no table",
             ClusterMonitorTables.project(nil) == nil)
    if let degraded = try? JSONDecoder().decode(MonitorResponse.self,
                                                from: try c.load("v1_cluster_monitor_unavailable.json")) {
        c.expect("monitor-tables", "\(label): the fleet-down body projects no table",
                 ClusterMonitorTables.project(degraded) == nil,
                 "it drew a table out of a body with no host list")
        let fleet = FleetProjection(projecting: degraded)
        c.expect("monitor-tables", "\(label): and the widget's existing projection agrees",
                 (fleet == nil) == (ClusterMonitorTables.project(degraded) == nil))
    }

    // ---- live: four GB10 hosts whose VRAM, fan and power-limit readings are
    // EMPTY STRINGS on the wire. Dashes there are the honest rendering, and the
    // assertion below is what stops a future "helpful" `?? "0"` from turning
    // them into zeroes.
    guard let live = try? JSONDecoder().decode(MonitorResponse.self,
                                               from: try c.load("v1_cluster_monitor_live.json")),
          let tables = ClusterMonitorTables.project(live) else {
        c.expect("monitor-tables", "\(label): live projects", false, "decode/project failed")
        return
    }
    c.expect("monitor-tables", "\(label): live — 4 host rows, 4 detail lines, 4+1 serving rows",
             tables.hostRows.count == 4 && tables.details.count == 4
                && tables.backendRows.count == 4 && tables.totalRows.count == 1
                && tables.servingRows.count == 5,
             "hosts \(tables.hostRows.count) details \(tables.details.count) "
                + "backends \(tables.backendRows.count) totals \(tables.totalRows.count)")
    c.expect("monitor-tables", "\(label): live — the serving block ends with the total row",
             tables.servingRows.last?.isTotalRow == true
                && tables.servingRows.last?.node == "total (4)"
                && tables.servingRows.last?.run == "6",
             "got \(tables.servingRows.last?.node ?? "nil") run="
                + "\(tables.servingRows.last?.run ?? "nil")")
    let liveDetail = tables.details[0]
    c.expect("monitor-tables", "\(label): live — an unreadable VRAM reading is a dash, never 0",
             liveDetail.vram == FleetFormat.dash && liveDetail.vramLevel == .unknown
                && liveDetail.fan == FleetFormat.dash,
             "vram=\(liveDetail.vram) level=\(liveDetail.vramLevel.rawValue) fan=\(liveDetail.fan)")
    c.expect("monitor-tables", "\(label): live — clock, uptime and net are the route's numbers",
             liveDetail.clock == "806 MHz" && liveDetail.uptime == "22d 2h"
                && liveDetail.net == "↓2.4 KB/s ↑14.2 KB/s",
             "clock=\(liveDetail.clock) up=\(liveDetail.uptime) net=\(liveDetail.net)")
    c.expect("monitor-tables", "\(label): live — the detail label is a hostname, not an address (§7)",
             tables.details.allSatisfy { !$0.label.hasPrefix("192.0.2.") }
                && liveDetail.label == "node-1", "got \(liveDetail.label)")
    let hostRow = tables.hostRows[0]
    c.expect("monitor-tables", "\(label): live — temp/power/util columns carry the threshold verdict",
             hostRow.cpuTempLevel == .ok && hostRow.gpuPowerLevel == .unknown
                && ClusterMonitorHostColumn.gpu.level(in: hostRow) == .warn,
             "cpuTemp=\(hostRow.cpuTempLevel.rawValue) power=\(hostRow.gpuPowerLevel.rawValue) "
                + "gpu=\(ClusterMonitorHostColumn.gpu.level(in: hostRow).map(\.rawValue) ?? "nil")")
    // The `GPU Power` column has no limit on this hardware, so it has no
    // verdict — and no verdict must not read as "cool".
    c.expect("monitor-tables", "\(label): live — no power limit read is .unknown, not .ok",
             hostRow.gpuPowerLimitW == nil && hostRow.gpuPowerLevel == .unknown,
             "limit=\(String(describing: hostRow.gpuPowerLimitW))")
    c.expect("monitor-tables", "\(label): live — a host row's header words are abbreviated on large",
             ClusterMonitorHostColumn.cpuTemp.header(compact: true) == "CPU°"
                && ClusterMonitorHostColumn.cpuTemp.header(compact: false) == "CPU Temp"
                && ClusterMonitorHostColumn.gpuPower.header(compact: true) == "W")
    c.expect("monitor-tables", "\(label): live — the un-thresholded columns are never painted",
             [ClusterMonitorHostColumn.host, .jobs, .swap]
                .allSatisfy { $0.level(in: hostRow) == nil })
    c.expect("monitor-tables", "\(label): live — serving rows exist, so the section is not silence",
             tables.hasServingSection)

    // ---- edge: a host with a real VRAM/fan reading, a host with an SSH error
    // and no sample at all, and a host whose whole GPU block is empty.
    guard let edge = try? JSONDecoder().decode(MonitorResponse.self,
                                               from: try c.load("v1_cluster_monitor_edge.json")),
          let edgeTables = ClusterMonitorTables.project(edge) else {
        c.expect("monitor-tables", "\(label): edge projects", false, "decode/project failed")
        return
    }
    let hot = edgeTables.details.first { $0.id == "192.0.2.21" }
    c.expect("monitor-tables", "\(label): edge — a host that reads VRAM prints used/total GB",
             hot?.vram == "56.0/64.0G" && hot?.fan == "45%" && hot?.clock == "2405 MHz"
                && hot?.net == "↓999 B/s ↑1.0 KB/s" && hot?.uptime == "1d 1h"
                && hot?.load == "0.5 1.5 2.5",
             "vram=\(hot?.vram ?? "nil") fan=\(hot?.fan ?? "nil") clock=\(hot?.clock ?? "nil") "
                + "net=\(hot?.net ?? "nil") up=\(hot?.uptime ?? "nil") load=\(hot?.load ?? "nil")")
    c.expect("monitor-tables", "\(label): edge — VRAM at 87.5% is .warn",
             hot?.vramLevel == .warn, "got \(hot?.vramLevel.rawValue ?? "nil")")
    let hotRow = edgeTables.hostRows.first { $0.host == "192.0.2.21" }
    c.expect("monitor-tables", "\(label): edge — a 87.25 C CPU is .warn and 71.5 C GPU is .ok",
             hotRow?.cpuTempLevel == .warn && hotRow?.gpuTempLevel == .ok,
             "cpu=\(hotRow?.cpuTempLevel.rawValue ?? "nil") gpu=\(hotRow?.gpuTempLevel.rawValue ?? "nil")")
    c.expect("monitor-tables", "\(label): edge — 37.885 W of a 120 W limit is .ok",
             hotRow?.gpuPowerLevel == .ok, "got \(hotRow?.gpuPowerLevel.rawValue ?? "nil")")
    let down = edgeTables.details.first { $0.id == "192.0.2.22" }
    let downFields = down.map { [$0.vram, $0.clock, $0.uptime, $0.fan, $0.load] } ?? []
    c.expect("monitor-tables", "\(label): edge — the host that never sampled has dashes everywhere, no zeroes",
             downFields.count == 5 && downFields.allSatisfy { $0 == FleetFormat.dash }
                && down?.net == "↓\(FleetFormat.dash) ↑\(FleetFormat.dash)",
             "got \(down.map { "\($0.vram)|\($0.clock)|\($0.uptime)|\($0.fan)|\($0.load)|\($0.net)" } ?? "nil")")
    c.expect("monitor-tables", "\(label): edge — an errored host keeps its row in the table",
             edgeTables.hostRows.contains { $0.host == "192.0.2.22" }
                && edgeTables.details.contains { $0.id == "192.0.2.22" })
    c.expect("monitor-tables", "\(label): edge — host order is the route's order, not a sorted copy",
             edgeTables.details.map(\.id) == edgeTables.hostRows.map(\.host))
    // A `down` backend is still a row (the CLI keeps it), and its Run cell
    // carries the reason — the colour rule must not invent a load verdict there.
    let downRow = edgeTables.backendRows.first { $0.isDown }
    c.expect("monitor-tables", "\(label): edge — the down backend row survives with the CLI's cells",
             downRow != nil && downRow?.model == "down"
                && ClusterMonitorServingColumn.run.level(in: downRow!) == .unknown,
             "run level=\(downRow.flatMap { ClusterMonitorServingColumn.run.level(in: $0) }?.rawValue ?? "nil")")

    monitorColumnsMatchCLI(c, fixture: "v1_cluster_monitor_live.json")
    monitorColumnsMatchCLI(c, fixture: "v1_cluster_monitor_edge.json")
}
