import Foundation

// ===========================================================================
// monitor_surfaces.swift — the Monitor Live Activity's surface checks (t_1685f884).
//
// Compiled by model_decode_check.sh alongside main.swift, monitor_parity.swift
// and the REAL sources, including `Sources/Shared/MonitorActivityMetrics.swift`
// — the file that owns "which metric goes on which surface". So these checks run
// the same functions the shipped Lock Screen, compact, expanded and minimal
// surfaces call; nothing here re-states a rule, and nothing here can be green
// because a check transcribed what the view supposedly does.
//
// WHY THIS EXISTS NEXT TO monitor_parity.swift
//
// `monitor_parity` proves `ClusterMonitorViewModel` formats a cell the way
// `cluster_live.py` does. That is one link in the chain. The next link is this
// activity: the driver pushes the view-model's cells into an
// `ActivityKit`-stored state and four surfaces pick a subset of them to draw.
// A surface that picks the wrong column, invents a fleet average no route
// sends, colours an absent metric green, or quietly truncates a table is not
// caught by cell parity. Every one of those mistakes is a rule in
// `MonitorSurfaceReading`, and every rule below is asserted by CALLING it.
//
// The goldens come from `cluster_monitor_cli_parity.py` (see monitor_parity.swift
// for what they are). Where a check here compares a pushed cell against a golden
// cell it is comparing against the CLI, not against a copy of the view-model.
// ===========================================================================

/// The live capture: 4 hosts, all sampled, one model, 6 requests running and an
/// empty queue. Read `fixtures/README.md` before changing expectations.
func monitorActivitySurfaces(_ c: DecodeCheck) {
    let fixture = "v1_cluster_monitor_live.json"
    let label = "monitor Live Activity surfaces"
    do {
        let response = try JSONDecoder().decode(MonitorResponse.self, from: try c.load(fixture))
        let cli = try JSONDecoder().decode(CLIGolden.self,
                                           from: try c.load("v1_cluster_monitor_live.cli.json"))
        let surface = MonitorSurfaceReading.payload(nodes: response.nodes, serving: response.serving)

        // --- The table the Lock Screen draws --------------------------------
        c.expect(fixture, "\(label): one host row per host the route listed, none dropped",
                 surface.hostRows?.count == cli.host_rows.count
                 && surface.hostRowsDropped == 0
                 && surface.nodesTotal == cli.host_rows.count
                 && surface.nodesUp == cli.host_rows.count,
                 "rows \(surface.hostRows?.count ?? -1) of \(cli.host_rows.count), dropped \(String(describing: surface.hostRowsDropped))")

        // The exact cells four surfaces draw, per host, against the CLI's own
        // cells for the same four columns. This is the check that makes a
        // lock-screen `62 C` mean `cluster_live.py`'s `62 C`.
        var cellMiss: [String] = []
        for (index, row) in (surface.hostRows ?? []).enumerated() where index < cli.host_rows.count {
            let want = Array(cli.host_rows[index][5...8])
            let got = MonitorSurfaceReading.detailCells(row)
            if got != want {
                cellMiss.append("row \(index): pushed \(got), CLI \(want)")
            }
        }
        c.expect(fixture, "\(label): every pushed host cell equals the CLI's GPU%/CPU Temp/GPU Temp/GPU Power cell",
                 cellMiss.isEmpty && (surface.hostRows?.count ?? 0) == cli.host_rows.count,
                 cellMiss.joined(separator: " ;; "))

        // Headers are selected from the view-model's own column list, so pin
        // that selection BY NAME against the golden header. If
        // `CLIMonitorHostRow.columns` is ever reordered, this names the column
        // that moved instead of letting a header label the wrong cell.
        c.expect(fixture, "\(label): the surface's column choice is the CLI's, by name and position",
                 MonitorSurfaceReading.detailHostColumns == ["GPU%", "CPU Temp", "GPU Temp", "GPU Power"]
                 && MonitorSurfaceReading.detailHostColumns == Array(cli.host_header[5...8])
                 && MonitorSurfaceReading.runColumnName == cli.serving_header_wide[4]
                 && MonitorSurfaceReading.waitColumnName == cli.serving_header_wide[5],
                 "chose \(MonitorSurfaceReading.detailHostColumns) from \(cli.host_header); run/wait \(MonitorSurfaceReading.runColumnName)/\(MonitorSurfaceReading.waitColumnName) vs \(cli.serving_header_wide[4])/\(cli.serving_header_wide[5])")

        // --- §7 on the bytes the activity stores ----------------------------
        // The control matters: the fixture's `host` values are dotted quads, so
        // a projection that used `host` as the label would fail here rather than
        // pass because the fixture had no addresses to find.
        let controlQuad = response.nodes.allSatisfy { holdsDottedQuad($0.host) }
        c.expect(fixture, "\(label): control — the capture's hosts really are dotted quads",
                 controlQuad && !response.nodes.isEmpty,
                 "hosts stopped being addresses, so the scan below proves nothing")
        var leaked: [String] = []
        for row in surface.hostRows ?? [] {
            for text in ([row.label, row.jobs, row.cpuPct, row.ramPct, row.swapPct]
                            + MonitorSurfaceReading.detailCells(row)).compactMap({ $0 }) {
                if holdsDottedQuad(text) { leaked.append("\(row.label): \(text)") }
            }
        }
        c.expect(fixture, "\(label): no dotted-quad reaches the pushed state (§7)",
                 leaked.isEmpty && surface.hostRows?.isEmpty == false,
                 leaked.joined(separator: " ;; "))
        c.expect(fixture, "\(label): a host is labelled by displayLabel, never by its address",
                 (surface.hostRows ?? []).count == response.nodes.count
                 && zip(surface.hostRows ?? [], response.nodes).allSatisfy { row, node in
                     row.label == node.displayLabel && row.label != node.host
                 },
                 "labels \(surface.hostRows?.map(\.label) ?? []) vs \(response.nodes.map(\.displayLabel))")

        // --- The serving tail, per host -------------------------------------
        // Host .13 runs 3 in the CLI's own row; the pushed row must carry the
        // same Run/Wait the golden printed, and the CLI's verdict on them.
        let thirteen = cli.serving_rows_wide.first { $0[0] == "192.0.2.13" } ?? []
        let rowThirteen = (surface.hostRows ?? []).first { $0.label == "node-3" }
        c.expect(fixture, "\(label): a host's row carries that host's serving Run/Wait, and the CLI's load verdict",
                 thirteen.count > 5
                 && rowThirteen?.running == Int(thirteen[4]) && rowThirteen?.waiting == Int(thirteen[5])
                 && MonitorSurfaceReading.servingText(running: rowThirteen?.running,
                                                      waiting: rowThirteen?.waiting) == "3 run · 0 wait"
                 && rowThirteen?.servingLevel == .ok,
                 "pushed \(String(describing: rowThirteen?.running))/\(String(describing: rowThirteen?.waiting)) vs CLI \(thirteen), level \(String(describing: rowThirteen?.servingLevel))")

        // --- The aggregate and the totals -----------------------------------
        // Both are summed from the golden's own `total (n)` cells, so the
        // Lock Screen's queue line is tied to the CLI's totals rather than to a
        // hand-written expectation that could rot alongside a code change.
        let totalRows = cli.serving_rows_wide.filter { $0[0].hasPrefix("total (") }
        let wantRun = totalRows.compactMap { Int($0[4]) }.reduce(0, +)
        let wantWait = totalRows.compactMap { Int($0[5]) }.reduce(0, +)
        c.expect(fixture, "\(label): the aggregate Run/Wait is the sum of the CLI's total rows",
                 surface.servingRunning == wantRun && surface.servingWaiting == wantWait
                 && MonitorSurfaceReading.servingSummaryText(running: surface.servingRunning,
                                                             waiting: surface.servingWaiting)
                    == "\(wantRun) run · \(wantWait) waiting"
                 && surface.queueLevel == CLIMonitorFormat.loadLevel(running: wantRun, waiting: wantWait),
                 "aggregate \(String(describing: surface.servingRunning))/\(String(describing: surface.servingWaiting)) vs CLI \(wantRun)/\(wantWait), level \(String(describing: surface.queueLevel))")

        let totals = surface.servingTotals ?? []
        // The seven cells of a `total (n)` row the activity stores, and the
        // golden columns they come from — named from the golden's own header so
        // a failure says WHICH cell moved. (Arrays rather than a 7-tuple: Swift
        // has no `==` past 6 elements, and a hand-rolled comparison is exactly
        // where a cell stops being checked.)
        let totalColumns = [cli.serving_header_wide[0], cli.serving_header_wide[2],
                            cli.serving_header_wide[4], cli.serving_header_wide[5],
                            cli.serving_header_wide[7], cli.serving_header_wide[8],
                            cli.serving_header_wide[9]]
        let totalColumnIndex = [0, 2, 4, 5, 7, 8, 9]
        var totalMiss: [String] = []
        for (index, goldenRow) in totalRows.enumerated() where index < totals.count {
            let got = totals[index]
            let pushed = [got.label, got.model, got.run, got.wait, got.tokensIn, got.tokensOut, got.genTps]
            for (cell, pair) in zip(pushed, totalColumns).enumerated() {
                let want = goldenRow[totalColumnIndex[cell]]
                if pair.0 != want {
                    totalMiss.append("total row \(index) \(pair.1): pushed \"\(pair.0)\", CLI \"\(want)\"")
                }
            }
        }
        c.expect(fixture, "\(label): every pushed `total (n)` row equals the CLI's total row, cell for cell",
                 totals.count == totalRows.count && totalMiss.isEmpty,
                 totalMiss.isEmpty ? "pushed \(totals.count), CLI \(totalRows.count)" : totalMiss.joined(separator: " ;; "))
        c.expect(fixture, "\(label): a totals line reads as the CLI's cells, joined for a glance",
                 MonitorSurfaceReading.servingTotalText(totals[0], modelVisible: true)
                    == "\(totalRows[0][0]) · \(totalRows[0][2]) · \(totalRows[0][4]) run · \(totalRows[0][5]) wait"
                 && MonitorSurfaceReading.servingTotalText(totals[0], modelVisible: false)
                    == "\(totalRows[0][0]) · \(totalRows[0][4]) run · \(totalRows[0][5]) wait",
                 MonitorSurfaceReading.servingTotalText(totals[0], modelVisible: true))

        // --- The glance metric ------------------------------------------------
        let hottest = response.nodes.compactMap { $0.sample?.gpuTempC }.max()
        c.expect(fixture, "\(label): compact/minimal lead with the hottest GPU temp, named by its host",
                 surface.primary?.kind == .gpuTemp
                 && surface.primary?.value == FleetFormat.celsius(hottest)
                 && surface.primary?.label == "node-3"
                 && surface.primary?.level == .ok,
                 "primary \(String(describing: surface.primary)) — hottest GPU temp in the capture is \(String(describing: hottest))")
        c.expect(fixture, "\(label): the trailing metric is the queue when nobody is waiting, so the run count",
                 surface.secondary?.kind == .serving && surface.secondary?.value == "\(wantRun) run"
                 && surface.secondary?.level == .ok,
                 "secondary \(String(describing: surface.secondary))")
        c.expect(fixture, "\(label): the two glance slots never repeat one number",
                 surface.primary?.kind != surface.secondary?.kind,
                 "both slots drew \(String(describing: surface.primary?.kind))")

        // --- Nothing was told: no invented zero ------------------------------
        let silent = MonitorSurfaceReading.payload(nodes: nil, serving: nil)
        c.expect(fixture, "\(label): a failed read pushes nil everywhere — no zero, no tally, no colour",
                 silent.hostRows == nil && silent.hostRowsDropped == nil
                 && silent.servingTotals == nil && silent.servingRunning == nil
                 && silent.servingWaiting == nil && silent.nodesUp == nil && silent.nodesTotal == nil
                 && silent.primary == nil && silent.secondary == nil && silent.queueLevel == nil,
                 "silent payload: \(silent)")

        // A degraded route DID answer and said zero. That is a reading, not an
        // absence: the CLI prints `0 host(s)`, the serving section came back with
        // no rows, so the run count is a real 0 (`§6: a missing key is a bug,
        // zero is a value`) and its colour is the CLI's `dim`, not green. The
        // queue is idle because it is empty, and the tally says no host answered.
        let empty = MonitorSurfaceReading.payload(nodes: [], serving: [])
        c.expect(fixture, "\(label): an empty-but-answered read is 0, not nil, and says `0/0 up`",
                 empty.hostRows == [] && empty.hostRowsDropped == 0
                 && empty.servingTotals == [] && empty.servingRunning == 0
                 && empty.servingWaiting == 0 && empty.queueLevel == .dim
                 && empty.nodesTotal == 0 && empty.nodesUp == 0
                 && empty.primary?.kind == .serving && empty.primary?.value == "0 run"
                 && empty.primary?.level == .dim
                 && empty.secondary?.kind == .tally && empty.secondary?.value == "0/0 up",
                 "empty payload: \(empty)")

        // --- Region caps -----------------------------------------------------
        // Eight rows is what a bigger fleet would push past the driver's cap;
        // the region limits then clip, and every clip announces itself.
        let stub = MonitorHostMetric(label: ".9", jobs: "1", cpuPct: "1.0", ramPct: "1.0",
                                     swapPct: "0%", gpuPct: "50", cpuTemp: "50 C", gpuTemp: "50 C",
                                     gpuPower: "10.0 W", running: 0, waiting: 0,
                                     cpuTempLevel: nil, gpuTempLevel: nil, gpuUtilLevel: nil,
                                     gpuPowerLevel: nil, servingLevel: nil, isFailure: false)
        let eight = Array(repeating: stub, count: 8)
        let lock = MonitorSurfaceReading.lockScreenHostRows(eight)
        let island = MonitorSurfaceReading.islandHostRows(eight)
        c.expect(fixture, "\(label): each region clips to its own size and says how many it hid",
                 lock.shown.count == MonitorSurfaceLimits.lockScreenHostRows && lock.dropped == 4
                 && island.shown.count == MonitorSurfaceLimits.islandHostRows && island.dropped == 6
                 && MonitorSurfaceLimits.maxPushedHostRows == 6
                 && MonitorSurfaceReading.moreText(dropped: lock.dropped) == "+4 more"
                 && MonitorSurfaceReading.moreText(dropped: 0) == nil,
                 "lock \(lock.shown.count)/hide \(lock.dropped), island \(island.shown.count)/hide \(island.dropped)")
        let stubTotal = MonitorServingTotalMetric(label: "total (1)", model: "M", run: "1", wait: "0",
                                                  tokensIn: "-", tokensOut: "-", genTps: "-", loadLevel: .ok)
        let fourTotals = Array(repeating: stubTotal, count: 4)
        c.expect(fixture, "\(label): the totals list clips too, and the push cap is what the driver applies",
                 MonitorSurfaceReading.lockScreenServingTotals(fourTotals).shown.count == 2
                 && MonitorSurfaceReading.lockScreenServingTotals(fourTotals).dropped == 2
                 && MonitorSurfaceReading.islandServingTotals(fourTotals).shown.count == 1
                 && MonitorSurfaceLimits.maxPushedServingTotalRows == 4,
                 "totals clipped wrong")
    } catch {
        c.expect(fixture, label, false, "\(error)")
    }
}

/// One row of the glance-rule table: what was told, and what the two slots must
/// come back with. `nil` fields are part of the expectation (a slot that must
/// stay empty is as much a rule as a slot that must hold a number).
private struct GlanceCase {
    let name: String
    let nodes: [MonitorNode]
    let serving: [ServingRow]?
    let wantPrimary: MonitorPrimaryMetric?
    let wantSecondary: MonitorPrimaryMetric?
}

/// The glance ladder, on payloads this file builds.
///
/// WHY SYNTHETIC, and what that bought: both committed captures put the hottest
/// CPU temperature and the hottest GPU temperature on the SAME host (measured:
/// `.13` holds both in the live capture, `node-11` in the edge one), so the
/// ORDER of `primaryMetric` — "GPU heat first" — is invisible in them. The first
/// version of this file "proved" that order off the edge fixture and passed a
/// mutation that swapped the two branches, because picking the hottest-CPU row
/// and then printing its GPU column happened to be the same answer. A rule a
/// mutation cannot redden is not being checked, so the order is pinned here on
/// hosts built so the two maxima sit on different machines.
func monitorActivityGlanceLadder(_ c: DecodeCheck) {
    let fixture = "monitor glance ladder (built, not captured)"
    /// A host built to carry EXACTLY the columns a case is about. Reusing a
    /// node that also carries a temperature was this file's own first bug: the
    /// "no heat at all" case inherited a GPU temp from a borrowed node and the
    /// rule correctly led with heat, which read like a broken rule.
    func made(_ host: String, _ hostname: String, util: String? = nil,
              cpuTemp: String? = nil, gpuTemp: String? = nil) -> MonitorNode {
        MonitorNode(host: host, error: nil,
                    sample: MonitorSample(hostname: hostname, gpu_util_pct: util,
                                          cpu_temp_c: cpuTemp, gpu_temp_c: gpuTemp),
                    used_slots: nil, free_slots: nil, workloads: nil)
    }
    let hotCPU = made("192.0.2.50", "hot-cpu", util: "10", cpuTemp: "90", gpuTemp: "40")
    let hotGPU = made("192.0.2.51", "hot-gpu", util: "88", cpuTemp: "10", gpuTemp: "55")
    let quiet = made("192.0.2.52", "quiet")
    let queue = (try? JSONDecoder().decode(
        [ServingRow].self,
        from: Data(#"[{"node":"192.0.2.50","port":"8000","model":"org/Model-A","running":3,"waiting":2}]"#.utf8))) ?? []
    let busy = (try? JSONDecoder().decode(
        [ServingRow].self,
        from: Data(#"[{"node":"192.0.2.50","port":"8000","model":"org/Model-A","running":3,"waiting":0}]"#.utf8))) ?? []

    let cases: [GlanceCase] = [
        GlanceCase(name: "GPU heat outranks a hotter CPU on another host",
                   nodes: [hotCPU, hotGPU], serving: nil,
                   wantPrimary: MonitorPrimaryMetric(kind: .gpuTemp, value: "55°C", label: "hot-gpu", level: .ok),
                   wantSecondary: MonitorPrimaryMetric(kind: .tally, value: "2/2 up", label: nil, level: nil)),
        GlanceCase(name: "no GPU heat at all, so CPU heat leads",
                   nodes: [made("192.0.2.50", "hot-cpu", cpuTemp: "61.9"), quiet], serving: nil,
                   wantPrimary: MonitorPrimaryMetric(kind: .cpuTemp, value: "62°C", label: "hot-cpu", level: .ok),
                   wantSecondary: MonitorPrimaryMetric(kind: .tally, value: "2/2 up", label: nil, level: nil)),
        GlanceCase(name: "no heat at all, so the busiest host's GPU utilization leads",
                   nodes: [made("192.0.2.50", "idle-one", util: "10"),
                           made("192.0.2.51", "busy-one", util: "88")], serving: nil,
                   wantPrimary: MonitorPrimaryMetric(kind: .gpuUtil, value: "88%", label: "busy-one", level: .warn),
                   wantSecondary: MonitorPrimaryMetric(kind: .tally, value: "2/2 up", label: nil, level: nil)),
        GlanceCase(name: "a queue is all there is: the run count leads, the queue trails",
                   nodes: [quiet], serving: queue,
                   wantPrimary: MonitorPrimaryMetric(kind: .serving, value: "3 run", label: nil, level: .warn),
                   wantSecondary: MonitorPrimaryMetric(kind: .waiting, value: "2 wait", label: nil, level: .warn)),
        GlanceCase(name: "an idle queue is the run count, in the CLI's ok, and it is not repeated",
                   nodes: [quiet], serving: busy,
                   wantPrimary: MonitorPrimaryMetric(kind: .serving, value: "3 run", label: nil, level: .ok),
                   wantSecondary: nil),
        GlanceCase(name: "hosts and nothing else: the tally leads and the duplicate slot is dropped",
                   nodes: [quiet], serving: nil,
                   wantPrimary: MonitorPrimaryMetric(kind: .tally, value: "1/1 up", label: nil, level: nil),
                   wantSecondary: nil),
        GlanceCase(name: "a tie goes to the host the route listed first, and the label names it",
                   nodes: [made("192.0.2.50", "hot-cpu", gpuTemp: "40"),
                           made("192.0.2.53", "also-hot", gpuTemp: "40")], serving: nil,
                   wantPrimary: MonitorPrimaryMetric(kind: .gpuTemp, value: "40°C", label: "hot-cpu", level: .ok),
                   wantSecondary: MonitorPrimaryMetric(kind: .tally, value: "2/2 up", label: nil, level: nil)),
    ]

    for one in cases {
        let payload = MonitorSurfaceReading.payload(nodes: one.nodes, serving: one.serving)
        c.expect(fixture, "\(one.name)",
                 payload.primary == one.wantPrimary && payload.secondary == one.wantSecondary,
                 "primary \(String(describing: payload.primary)) (want \(String(describing: one.wantPrimary))), "
                 + "secondary \(String(describing: payload.secondary)) (want \(String(describing: one.wantSecondary)))")
    }

    // The control for the case above: the two maxima really are on different
    // hosts. If a future edit makes them the same host, the ordering case stops
    // proving anything and this names that, instead of the suite going quiet.
    c.expect(fixture, "control — the ordering case's hottest CPU and hottest GPU are on different hosts",
             MonitorSurfaceReading.hottestRow(rows: [hotCPU, hotGPU].map { CLIMonitorHostRow(node: $0) },
                                              key: { $0.cpuTempC })?.displayLabel == "hot-cpu"
             && MonitorSurfaceReading.hottestRow(rows: [hotCPU, hotGPU].map { CLIMonitorHostRow(node: $0) },
                                                 key: { $0.gpuTempC })?.displayLabel == "hot-gpu",
             "both maxima are on one host, so the ordering case above is vacuous")

    // The same argument one level down, on the row-picking itself. Swapping the
    // two `if let row = hottestRow(...)` branches in `primaryMetric` — i.e.
    // "look for the hottest CPU first, then print whatever GPU temperature that
    // host happens to have" — is a real edit someone could make, and NEITHER
    // committed capture reddens it: in both of them the hottest CPU and the
    // hottest GPU are the same host, so the wrong branch still prints the right
    // cell. This constructed pair is what separates the two branches: the
    // hottest CPU is on `cpu-hot`, the hottest GPU on `gpu-hot`.
    let twoHosts = [
        MonitorNode(host: "192.0.2.60", error: nil,
                    sample: MonitorSample(hostname: "cpu-hot", cpu_temp_c: "70", gpu_temp_c: "40"),
                    used_slots: nil, free_slots: nil, workloads: nil),
        MonitorNode(host: "192.0.2.61", error: nil,
                    sample: MonitorSample(hostname: "gpu-hot", cpu_temp_c: "55", gpu_temp_c: "91"),
                    used_slots: nil, free_slots: nil, workloads: nil),
    ]
    let ordered = MonitorSurfaceReading.primaryMetric(nodes: twoHosts, servingRunning: nil,
                                                      servingWaiting: nil, nodesUp: 2, nodesTotal: 2)
    c.expect(fixture, "control — the glance rule follows GPU heat across hosts, not the hottest CPU's row",
             ordered?.kind == .gpuTemp && ordered?.value == "91°C" && ordered?.label == "gpu-hot"
             && ordered?.level == .critical,
             "primary \(String(describing: ordered)) — expected the hottest GPU (91 C on gpu-hot), "
             + "not the hottest CPU host's gpu cell")
}

/// The edge capture: 7 hosts (two unreachable, one with no sample at all, one
/// on the tailnet, one whose GPU columns are `-`), one down backend row, three
/// model totals with a non-empty queue. Everything the honest-degrade rules
/// exist for is in this one fixture.
func monitorActivityEdgeSurfaces(_ c: DecodeCheck) {
    let fixture = "v1_cluster_monitor_edge.json"
    let label = "monitor Live Activity surfaces (edge)"
    do {
        let response = try JSONDecoder().decode(MonitorResponse.self, from: try c.load(fixture))
        let cli = try JSONDecoder().decode(CLIGolden.self,
                                           from: try c.load("v1_cluster_monitor_edge.cli.json"))
        let surface = MonitorSurfaceReading.payload(nodes: response.nodes, serving: response.serving)

        // The push cap is not hypothetical here: this capture is bigger than the
        // activity's budget, so the truncation the surfaces have to announce is
        // exercised by a real fixture rather than a fabricated list.
        // `nodesUp` is the APP's rule, not the CLI's: `MonitorNode.isUp` is
        // `error == nil`, so the host whose error is an empty string and the one
        // whose `nvidia-smi` failed both count as not-up even though the CLI
        // prints a normal row for them (the divergence is documented on
        // `CLIMonitorHostRow.error`). 3 of 7.
        c.expect(fixture, "\(label): a fleet larger than the push cap clips AND reports the clip",
                 response.nodes.count == 7 && surface.hostRows?.count == 6
                 && surface.hostRowsDropped == 1 && surface.nodesTotal == 7
                 && surface.nodesUp == 3,
                 "7 hosts: pushed \(surface.hostRows?.count ?? -1), dropped \(String(describing: surface.hostRowsDropped)), up \(String(describing: surface.nodesUp))")

        let failed = (surface.hostRows ?? []).filter(\.isFailure)
        c.expect(fixture, "\(label): only a host with no sample and a non-empty error is a failed row",
                 failed.map(\.label) == [".22", ".23"]
                 && failed.allSatisfy { MonitorSurfaceReading.detailCells($0) == ["-", "-", "-", "-"] },
                 "flagged \(failed.map(\.label)) cells \(failed.map { MonitorSurfaceReading.detailCells($0) })")

        // The phrase is this app's, and the route's reason stays where the CLI
        // put it (the CPU % column, which no surface of this activity draws). So
        // a host that never sampled says so without the app quoting a string it
        // does not control into state ActivityKit keeps.
        let cliReason = cli.host_rows[1][2]
        c.expect(fixture, "\(label): a failed host says `not sampled`, and the route's reason is not drawn",
                 MonitorSurfaceReading.notSampledText == "not sampled"
                 && cliReason.contains("ssh")
                 && MonitorSurfaceReading.detailCells(failed[0]).joined()
                    .range(of: "ssh", options: .caseInsensitive) == nil,
                 "placeholder \(MonitorSurfaceReading.notSampledText), CLI reason \(cliReason), cells \(MonitorSurfaceReading.detailCells(failed[0]))")

        // The down backend must not be attributed to its host: `serving_table`
        // prints `down` + the reason for a backend that did not answer, and
        // counting it would show that host a queue it does not have.
        let downHost = (surface.hostRows ?? []).first { $0.label == ".23" }
        c.expect(fixture, "\(label): a `down` backend is not counted as that host's queue",
                 downHost?.running == nil && downHost?.waiting == nil
                 && MonitorSurfaceReading.servingText(running: downHost?.running,
                                                      waiting: downHost?.waiting) == nil,
                 ".23 got \(String(describing: downHost?.running))/\(String(describing: downHost?.waiting))")

        // A queue is the loudest thing a glance can carry, so a non-empty one
        // wins the trailing slot and carries the CLI's `warn` verdict.
        c.expect(fixture, "\(label): a waiting queue takes the trailing slot and the CLI's warn colour",
                 surface.secondary?.kind == .waiting && surface.secondary?.value == "4 wait"
                 && surface.secondary?.level == .warn
                 && surface.queueLevel == .warn
                 && surface.servingRunning == 3 && surface.servingWaiting == 4,
                 "secondary \(String(describing: surface.secondary)), aggregate \(String(describing: surface.servingRunning))/\(String(describing: surface.servingWaiting))")

        // On THIS capture the hottest CPU and the hottest GPU are the same host
        // (`node-11`, 87.25 C against 71.5 C), so this check pins which host and
        // cell the glance picked from real bytes — it cannot see the ORDER of the
        // rule, and `monitorActivityGlanceLadder` is the one that does.
        c.expect(fixture, "\(label): the glance metric names the hottest-GPU host and its cell",
                 surface.primary?.kind == .gpuTemp && surface.primary?.value == "72°C"
                 && surface.primary?.label == "node-11" && surface.primary?.level == .ok,
                 "primary \(String(describing: surface.primary))")

        // Every row keeps the §7 label rule even when the sample never arrived
        // (`.22`, `.23` fall back to the last octet) — and the capture's tailnet
        // host is the control: `100.64.0.1` is on the wire, and `node-16` is what
        // the activity is allowed to store.
        c.expect(fixture, "\(label): every label is a name or a last octet — the tailnet host included (§7)",
                 (surface.hostRows ?? []).allSatisfy { !holdsDottedQuad($0.label) }
                 && response.nodes.contains { $0.host == "100.64.0.1" && $0.displayLabel == "node-16" }
                 && (surface.hostRows ?? []).contains { $0.label == "node-16" }
                 && failed.map(\.label) == [".22", ".23"],
                 "labels \(surface.hostRows?.map(\.label) ?? [])")

        let totals = surface.servingTotals ?? []
        c.expect(fixture, "\(label): every model's total row survives to the Lock Screen, in the CLI's order",
                 totals.map(\.label) == ["total (2)", "total (1)", "total (1)"]
                 && totals.map(\.model) == ["Model-A", "Model-B", "Qwen3-30B-A3B[GPTQ]"]
                 && surface.servingTotalsDropped == 0
                 && totals[2].loadLevel == .warn && totals[0].loadLevel == .warn
                 && totals[1].loadLevel == .dim,
                 "totals \(totals.map { "\($0.label)/\($0.model)/\($0.run)/\($0.wait)" }) levels \(totals.map(\.loadLevel))")
    } catch {
        c.expect(fixture, label, false, "\(error)")
    }
}
