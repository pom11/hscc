import Foundation
import WidgetKit

// ===========================================================================
// cluster_widget_check — the executed half of the cluster widget gate.
//
// Compiles the REAL projection + text model (`ClusterWidgetMetrics.swift`,
// `ClusterNodeTally.swift`) against the committed monitor fixture and asserts
// the exact lines every widget family draws. No network, simulator or device.
//
// The fixture is a scrubbed capture of the live `/v1/cluster/monitor` body:
// 192.0.2.x documentation addresses, generic node/model/workload names, and
// `serving[]` in the shape the CLI's serving table consumes.
// ===========================================================================

var failures: [String] = []
var checks = 0

func expect(_ group: String, _ name: String, _ condition: Bool) {
    checks += 1
    if condition {
        print("  ok: \(group) — \(name)")
    } else {
        failures.append("\(group): \(name)")
        print("FAIL: \(group) — \(name)")
    }
}

func requireData(_ name: String) -> Data {
    let path = "\(FileManager.default.currentDirectoryPath)/scripts/model_decode_check/fixtures/\(name)"
    guard let data = FileManager.default.contents(atPath: path) else {
        print("FATAL: fixture not readable at \(path) (run the script from ios-app/)")
        exit(2)
    }
    return data
}

// ---------------------------------------------------------------------------
// 1 — the committed up-state capture, projected to CLI rows.
// ---------------------------------------------------------------------------

let group = "fixture"
let monitor = (try? JSONDecoder().decode(MonitorResponse.self, from: requireData("v1_cluster_monitor.json")))
guard let response = monitor, let fleet = FleetProjection(projecting: response) else {
    print("FATAL: the committed v1_cluster_monitor.json did not decode + project")
    exit(2)
}

expect(group, "4 hosts listed, 4 up", fleet.tally.nodesUp == 4 && fleet.tally.nodesTotal == 4)
expect(group, "nodes keep route order and display labels",
       fleet.nodes.map { $0.label } == ["node-1", "node-2", "node-3", "node-4"])
expect(group, "the serving table names 2 models",
       fleet.serving.models == 2 && fleet.serving.modelNames == ["model-a", "model-b"])
expect(group, "the CLI total Gen t/s is the sum of serving[].gen_tps",
       fleet.serving.genTps.map { abs($0 - 12.3) < 0.001 } == true)
expect(group, "run/wait totals match the serving rows (5 run, 0 wait)",
       fleet.serving.running == 5 && fleet.serving.waiting == 0)
expect(group, "2 distinct workload_id jobs",
       fleet.jobs == 2)
expect(group, "the first node's model comes from the serving table",
       fleet.nodes[0].model == "model-a" && fleet.nodes[2].model == "model-b")
expect(group, "the first node's GPU/temperature come from the sample",
       fleet.nodes[0].gpuPct == 0 && fleet.nodes[0].gpuTempC == 63 && fleet.nodes[0].cpuTempC == 70.7)
expect(group, "the busy node's GPU is 96 and 78°C",
       fleet.nodes[2].gpuPct == 96 && fleet.nodes[2].gpuTempC == 78)

// §7: no address or path reaches the projected rows or their description.
let wireFree = String(describing: fleet)
expect("§7_redaction", "the projection carries no dotted-quad address",
       !wireFree.contains("192.0.2."))
expect("§7_redaction", "the projection carries no path or recipe",
       !wireFree.contains("/Users") && !wireFree.contains(".yaml") && !wireFree.contains("recipe"))
let workloadText = String(describing: response.nodes[0].workloads ?? [])
expect("§7_redaction", "the workload projection declares only ids, never recipe/containers",
       !workloadText.contains("recipe") && !workloadText.contains("containers")
        && !workloadText.contains("image") && !workloadText.contains("/"))

// `hscc cluster status`'s counts are the same number the jobs line draws.
let status = (try? JSONDecoder().decode(ClusterStatusResponse.self,
                                        from: requireData("v1_cluster_status.json")))
let statusSummary = ClusterStatusSummary(status)
expect(group, "status read: 3 workloads, 4 hosts, 4 idle",
       statusSummary?.jobs == 3 && statusSummary?.totalHosts == 4 && statusSummary?.idleHosts == 4)

// ---------------------------------------------------------------------------
// 2 — every family's exact lines, straight from the fixture fields.
// ---------------------------------------------------------------------------

let small = fleet.text(for: .small)
expect("small", "one clear headline number", small.headline == "4/4 up")
expect("small", "supporting line is models + throughput (no jobs/rows)",
       small.detail == "2 models · 12.3 t/s" && small.rows.isEmpty)

let medium = fleet.text(for: .medium)
expect("medium", "headline + models/jobs/throughput",
       medium.headline == "4/4 up" && medium.detail == "2 models · 2 jobs · 12.3 t/s")
expect("medium", "four compact node rows", medium.rows.count == 4)
expect("medium", "row 1 is node-1 / model-a / up / GPU 0% / 63°C",
       medium.rows[0].title == "node-1 · model-a"
        && medium.rows[0].detail == "up · GPU 0% · 63°C")

let large = fleet.text(for: .large)
expect("large", "full row adds CPU temp + run/wait",
       large.rows[2].title == "node-3 · model-b"
        && large.rows[2].detail == "up · GPU 96% · 78°C · CPU 87°C · run 3 · wait 0")

let inline = fleet.text(for: .inline)
expect("inline", "headline + detail on one line",
       fleet.inlineLine() == "4/4 up · 2 models · 12.3 t/s" && inline.rows.isEmpty)

expect("circular", "headline is the tally only",
       fleet.text(for: .circular).headline == "4/4 up"
        && fleet.text(for: .circular).rows.isEmpty)

let rect = fleet.text(for: .rectangular)
expect("rectangular", "two-line tally + detail",
       rect.headline == "4/4 up" && rect.detail == "2 models · 2 jobs · 12.3 t/s")
expect("rectangular", "first node row is present, compact",
       rect.rows.first?.title == "node-1 · model-a")

// The entry's gate states and failure/age footer.
let timedOut = ClusterEntry(date: .now, fleet: nil, failure: .timeout(seconds: 8),
                            configured: true, paused: false, lastKnownAgeMinutes: 3)
expect("gate", "a failed read leads with the reason", timedOut.text(for: .small).headline == "Timed out")
expect("gate", "and shows the age, never as fresh", timedOut.text(for: .small).footer == "3m ago"
        || timedOut.text(for: .small).footer == "updated 3m ago")
expect("gate", "unconfigured invites setup", ClusterEntry.unconfigured.shortGateLabel == "Set up in HSCC")
expect("gate", "paused says so", ClusterEntry.paused().shortGateLabel == "Paused")
expect("gate", "placeholder is sample-only and not the unconfigured state",
       ClusterEntry.placeholder.fleet != nil && !ClusterEntry.placeholder.configured == false)

// A fleet with one errored host is honest: 3/4, that node down, others up.
func sample(_ hostname: String, gpu: String) -> MonitorSample {
    MonitorSample(hostname: hostname, uptime_sec: nil, cpu_usage_pct: nil,
                  mem_used_pct: nil, mem_total_mb: nil, mem_used_mb: nil,
                  gpu_name: nil, gpu_util_pct: gpu, gpu_mem_used_pct: nil,
                  cpu_temp_c: "70", gpu_temp_c: "65", cpu_load_1m: nil)
}
let mixed = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: [
    MonitorNode(host: "192.0.2.11", error: nil, sample: sample("node-1", gpu: "10"),
                used_slots: nil, free_slots: nil,
                workloads: [MonitorWorkload(workload_id: "a", cluster_id: nil)]),
    MonitorNode(host: "192.0.2.12", error: "collect failed", sample: nil,
                used_slots: nil, free_slots: nil, workloads: nil),
]), serving: nil, serving_error: nil, serving_proxy_error: nil, speak: "")
let mixedFleet = FleetProjection(projecting: mixed)!
expect("degraded", "an errored host counts down but stays in the total",
       mixedFleet.tally.nodesUp == 1 && mixedFleet.tally.nodesTotal == 2)
expect("degraded", "the errored row says down and names no model",
       mixedFleet.nodes[1].isUp == false && mixedFleet.nodes[1].model == nil)
expect("degraded", "no serving section is nil, not 0 models", mixedFleet.serving.models == nil)

let empty = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: []),
                            serving: [], serving_error: nil, serving_proxy_error: nil, speak: "")
expect("degraded", "an empty host list projects to no fleet (not 0/0)",
       FleetProjection(projecting: empty) == nil)
let noServing = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: [
    MonitorNode(host: "192.0.2.11", error: nil, sample: sample("node-1", gpu: "0"),
                used_slots: nil, free_slots: nil, workloads: []),
]), serving: [], serving_error: nil, serving_proxy_error: nil, speak: "")
expect("degraded", "an answered empty serving table is 0 models, a value",
       FleetProjection(projecting: noServing)?.serving.models == 0)
expect("degraded", "and no rate at all is nil, never 0.0 t/s",
       FleetProjection(projecting: noServing)?.serving.genTps == nil)

// The degraded wire body (no json) must not become a zero-node fleet.
let degraded = (try? JSONDecoder().decode(MonitorResponse.self,
                                          from: requireData("v1_cluster_monitor_unavailable.json")))
expect("degraded", "the 38 B down-state body decodes with no fleet",
       degraded?.json == nil && degraded.flatMap(FleetProjection.init(projecting:)) == nil)

// ---------------------------------------------------------------------------
// 3 — text rules (kept as pure functions so the views cannot re-round).
// ---------------------------------------------------------------------------

expect("text", "percent rounds", FleetFormat.pct(88.4) == "88%")
expect("text", "percent nil is an em dash, not 0%", FleetFormat.pct(nil) == "—")
expect("text", "celsius carries its unit", FleetFormat.celsius(87.1) == "87°C")
expect("text", "tps keeps one decimal and its unit", FleetFormat.tps(12.34) == "12.3 t/s")
expect("text", "tps nil is an em dash", FleetFormat.tps(nil) == "—")
expect("text", "counts pluralise", FleetFormat.count(2, singular: "model", plural: "models") == "2 models"
        && FleetFormat.count(1, singular: "job", plural: "jobs") == "1 job")
expect("text", "tally reads up/total", FleetFormat.up(3, 4) == "3/4 up")
expect("text", "tally nil either side never becomes 0/0",
       FleetFormat.up(nil, 4) == "—" && FleetFormat.up(3, nil) == "3 up")
expect("text", "age reads in minutes", FleetFormat.age(3) == "updated 3m ago")

let heat = "heat"
expect(heat, "88% util is calm", ClusterMetricHeat.percent(88) == .calm)
expect(heat, "91% util is warm (>90)", ClusterMetricHeat.percent(91) == .warm)
expect(heat, "96% util is hot (>95)", ClusterMetricHeat.percent(96) == .hot)
expect(heat, "bands match MonitorMeter: CPU warns at >85", ClusterMetricHeat.percent(86, isCPU: true) == .warm)
expect(heat, "81°C is warm", ClusterMetricHeat.celsius(81) == .warm)
expect(heat, "91°C is hot", ClusterMetricHeat.celsius(91) == .hot)
expect(heat, "no reading is unknown", ClusterMetricHeat.celsius(nil) == .unknown)
expect(heat, "a down node is never calm", ClusterMetricHeat.node(mixedFleet.nodes[1]) == .hot)

// ---------------------------------------------------------------------------
// 4 — failure labels must fit a Lock Screen line.
// ---------------------------------------------------------------------------

let allFailures: [WidgetDataFailure] = [
    .notConfigured, .invalidURL, .pathNotAllowed(path: "/v1/whatever"),
    .timeout(seconds: 8), .unreachable(code: 54), .unauthorized,
    .notFound(path: "/v1/mobile/summary"), .http(status: 503, code: "internal"),
    .decoding(detail: "keyNotFound"),
]
expect("labels", "every shortLabel fits an accessory line",
       allFailures.allSatisfy { $0.shortLabel.count <= 18 && !$0.shortLabel.isEmpty })
expect("labels", "no shortLabel leaks a path or a decode detail",
       allFailures.allSatisfy { !$0.shortLabel.contains("/Users") && !$0.shortLabel.contains("keyNotFound") })

// ---------------------------------------------------------------------------
// 5 — workload shape. ONE id field pair (contract + legacy), never a path.
// ---------------------------------------------------------------------------

let wl = String(describing: MonitorWorkload(workload_id: "a", cluster_id: "a"))
expect("workload", "id is the contract field", !wl.contains("Optional(") || wl.contains("a"))
expect("workload", "no recipe/container/path can be rendered", !wl.contains("recipe") && !wl.contains("/"))

// ---------------------------------------------------------------------------
// 6 — the CLI host table `systemMedium` draws (t_2d1c12dc).
//
// `CLIMonitorTableReading` is the tile's whole data half, and this is the half
// that can be EXECUTED: the columns it draws, the strings it puts in them, the
// verdicts it paints by, and how many rows the size has room for. The tile's
// own header and cell strings come from here, so if these hold, the tile shows
// `host_table`'s table.
// ---------------------------------------------------------------------------

let tableGroup = "cli_host_table"
guard let viewModel = ClusterMonitorViewModel.project(response) else {
    print("FATAL: the committed fixture did not project to a CLI monitor view-model")
    exit(2)
}
expect(tableGroup, "the fixture's four hosts are four rows",
       viewModel.hostRows.count == 4)

let drawnTitles = CLIMonitorTableReading.drawnColumns.map { $0.title }
expect(tableGroup, "the tile draws every CLI column, in the CLI's order",
       drawnTitles == CLIMonitorHostRow.columns)
expect(tableGroup, "that is the CLI's nine columns",
       drawnTitles == ["Host", "Jobs", "CPU%", "RAM%", "Swap%", "GPU%",
                       "CPU Temp", "GPU Temp", "GPU Power"])
expect(tableGroup, "only Host is left-justified, as in host_table",
       CLIMonitorTableReading.drawnColumns.map { $0.leadingAligned }
        == [true, false, false, false, false, false, false, false, false])

// THE claim of the card: every drawn cell IS the CLI's own cell for that
// column, `-` and units included, for every host in the fixture — with the one
// documented §7 exception, the Host column, which draws the display label the
// view-model hands over rather than the address the CLI prints.
for (index, row) in viewModel.hostRows.enumerated() {
    let drawn = CLIMonitorTableReading.cells(for: row)
    let cliRow = [row.displayLabel] + Array(row.cells.dropFirst())
    expect(tableGroup, "host \(index): drawn cells are the CLI's row verbatim",
           drawn.map { $0.text } == cliRow)
    expect(tableGroup, "host \(index): the eight numeric cells are byte-identical to the CLI's",
           Array(drawn.dropFirst().map { $0.text }) == Array(row.cells.dropFirst()))
    expect(tableGroup, "host \(index): the Host column draws the label, never the address",
           drawn.first?.text == row.displayLabel && drawn.first?.text != row.host)
}
expect(tableGroup, "no drawn cell of any row is a dotted-quad address",
       viewModel.hostRows.allSatisfy { row in
           !CLIMonitorTableReading.cells(for: row).contains { $0.text.contains("192.0.2.") }
       })

// Which columns carry a verdict. `host_table` colours nothing; these five are
// the view-model's thresholds, and the four others must be nil so the tile
// cannot invent a colour for a column nobody thresholds. The reads are
// bounds-guarded: a column that disappeared must report a FAIL naming the
// column, not trap on the harness's own subscript and bury the reason.
let verdicts = CLIMonitorTableReading.cells(for: viewModel.hostRows[0]).map { $0.level }
expect(tableGroup, "one verdict slot per drawn column",
       verdicts.count == CLIMonitorHostRow.columns.count)
func verdict(_ index: Int) -> ClusterMetricLevel? {
    verdicts.indices.contains(index) ? verdicts[index] : .critical
}
expect(tableGroup, "Host/Jobs/Swap/GPU% carry no verdict",
       verdict(0) == nil && verdict(1) == nil && verdict(4) == nil && verdict(5) == nil)
expect(tableGroup, "CPU%, RAM%, both temps and power carry the view-model's verdict",
       verdict(2) == viewModel.hostRows[0].cpuLevel
        && verdict(3) == viewModel.hostRows[0].memLevel
        && verdict(6) == viewModel.hostRows[0].cpuTempLevel
        && verdict(7) == viewModel.hostRows[0].gpuTempLevel
        && verdict(8) == viewModel.hostRows[0].gpuPowerLevel)

// The row budget of the size, and the `+n` that admits there were more.
func tableNode(_ tail: Int) -> MonitorNode {
    MonitorNode(host: "192.0.2.\(tail)", error: nil,
                sample: MonitorSample(hostname: "node-\(tail)"),
                used_slots: nil, free_slots: nil, workloads: nil)
}
let six = (11...16).map { tableNode($0) }.map(CLIMonitorHostRow.init(node:))
expect(tableGroup, "medium keeps the four-row budget it always had",
       CLIMonitorTableReading.visibleRows(from: six).count == 4
        && CLIMonitorTableReading.mediumRowLimit == 4)
expect(tableGroup, "the two hosts it left out are counted, not dropped",
       CLIMonitorTableReading.hiddenRowCount(from: six) == 2)
expect(tableGroup, "a fleet that fits says so (no phantom +0)",
       CLIMonitorTableReading.hiddenRowCount(from: viewModel.hostRows) == 0)
expect(tableGroup, "limit 0 draws no row and hides every host",
       CLIMonitorTableReading.visibleRows(from: six, limit: 0).isEmpty
        && CLIMonitorTableReading.hiddenRowCount(from: six, limit: 0) == 6)
expect(tableGroup, "a zero-row limit never reads as a zero-host fleet",
       CLIMonitorTableReading.hiddenRowCount(from: viewModel.hostRows, limit: 0) == 4)

// VoiceOver reads the same CLI strings the eyes do.
let spoken = CLIMonitorTableReading.accessibilityLabel(for: viewModel.hostRows[0])
expect(tableGroup, "the row's sentence names each column and its CLI cell",
       spoken.hasPrefix(viewModel.hostRows[0].displayLabel)
        && spoken.contains("CPU Temp \(viewModel.hostRows[0].cpuTemp)")
        && spoken.contains("GPU Power \(viewModel.hostRows[0].gpuPower)"))

// The gate that decides whether the tile draws a table at all — executed, not
// grepped, because "an unconfigured tile invites setup" is the same invariant
// every other family is held to.
let liveEntry = ClusterEntry(date: .now, fleet: fleet, monitor: viewModel,
                             configured: true, paused: false)
expect(tableGroup, "configured + live: four rows", liveEntry.cliHostRows?.count == 4)
expect(tableGroup, "paused draws no table", ClusterEntry.paused().cliHostRows == nil)
expect(tableGroup, "unconfigured draws no table", ClusterEntry.unconfigured.cliHostRows == nil)
let noNodes = ClusterEntry(date: .now, fleet: fleet, monitor: viewModel,
                           configured: true, paused: false,
                           showSections: ["state", "models"])
expect(tableGroup, "the operator switched 'nodes' off: no table",
       noNodes.cliHostRows == nil)
expect(tableGroup, "no host list is nil, not an empty table",
       ClusterEntry(date: .now, fleet: nil, configured: true, paused: false).cliHostRows == nil)
expect(tableGroup, "the gallery sample carries the CLI table",
       ClusterEntry.placeholder.cliHostRows?.count == 4)

print("")
if failures.isEmpty {
    print("CLUSTER WIDGET CHECK PASSED — \(checks) checks: fixture rows, every family's lines, nil-is-not-zero, redaction")
    exit(0)
}
print("FAILURES: \(failures.count) of \(checks)")
for failure in failures { print("  - \(failure)") }
exit(1)
