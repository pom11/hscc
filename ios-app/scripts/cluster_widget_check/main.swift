import Foundation
import WidgetKit   // WidgetFamily — for the board-row budget assertions below

// ===========================================================================
// cluster_widget_check — the executed half of the cluster widget gate.
//
// What it proves, with the REAL sources compiled in (ClusterWidget.swift's
// projection + heat bands, ClusterWidgetViews.swift's text rules, SharedModels'
// wire types, WidgetDataClient's failure labels) and no iOS runtime, no
// simulator and no network:
//
//   1. the /v1/cluster/monitor projection against the COMMITTED fixture — nodes
//      up/down, distinct-cluster_id model counting, and MAX-not-mean metrics;
//   2. that the three new sample keys are actually DECODED (a stored property
//      with a declaration default is not, and that failure is a warning, not an
//      error — this is the assertion that would have caught it);
//   3. §7's redaction on the workload projection, executed off the real body;
//   4. degraded honesty: a 38 B down-state answer and an empty host list yield
//      NO data, never a zero; an errored host counts down but stays counted;
//   5. every text rule a family renders, and every heat band, including the
//      thresholds MonitorMeter uses so a number and the monitor's bar agree;
//   6. the accessory constraint the widget-data client's handoff set: a
//      failure's shortLabel fits one Lock Screen line (<= 18 characters);
//   7. the request-budget decision: which families pay for the board row.
//
// Views cannot run headless: `ClusterMetricText` and `ClusterMetricHeat` are the
// renderer-free halves of the views, and `scripts/cluster_widget_check.sh`
// greps the real dispatch/gating order so this mirror cannot drift silently.
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
// 1 + 2 + 3 — the committed up-state fixture, decoded and projected.
// ---------------------------------------------------------------------------

let group = "fixture"
let monitor = (try? JSONDecoder().decode(MonitorResponse.self, from: requireData("v1_cluster_monitor.json")))
guard let response = monitor, let fleet = ClusterFleet(projecting: response) else {
    print("FATAL: the committed v1_cluster_monitor.json did not decode + project")
    exit(2)
}

expect(group, "4 hosts listed", fleet.nodesTotal == 4)
expect(group, "4 hosts up (no error on any)", fleet.nodesUp == 4)
expect(group, "models served = 2 (4 hosts, 2 distinct cluster_id — a TP pair shares one)",
       fleet.modelsServed == 2)
expect(group, "summing per-host workload counts would have lied (4 != 2)",
       response.nodes.reduce(0) { $0 + ($1.workloads?.count ?? 0) } == 4)
expect(group, "GPU util = 96 (busiest node)", fleet.gpuUtilPct == 96)
expect(group, "GPU util is MAX, not mean (mean would read 48 and hide a saturated node)",
       fleet.gpuUtilPct != 48)
expect(group, "GPU temp = 78 (hottest)", fleet.gpuTempC == 78)
expect(group, "CPU temp = 87.1 (hottest)", fleet.cpuTempC == 87.1)
expect(group, "load = 4.02 (highest 1-minute)", fleet.load1m == 4.02)
expect(group, "hasMetrics true", fleet.hasMetrics)

// The `= nil` trap: a stored property with an initial value is NOT decoded, and
// the compiler only WARNS. If cpu_temp_c / gpu_temp_c / cpu_load_1m had been
// declared with defaults, every value below would be nil and the widget would
// render no metric line while `build_check.sh` stayed green.
expect("decode", "gpu_temp_c decoded on all 4 hosts",
       response.nodes.compactMap { $0.sample?.gpuTempC }.count == 4)
expect("decode", "cpu_temp_c decoded on all 4 hosts",
       response.nodes.compactMap { $0.sample?.cpuTempC }.count == 4)
expect("decode", "cpu_load_1m decoded on all 4 hosts",
       response.nodes.compactMap { $0.sample?.cpuLoad1m }.count == 4)
expect("decode", "workloads decoded on all 4 hosts",
       response.nodes.allSatisfy { ($0.workloads?.count ?? 0) == 1 })
expect("decode", "the fixture's node host names survive on the wire type (never stored)",
       response.nodes.allSatisfy { $0.host.contains(".") && $0.host.count > 4 })

// §7 executed: the workload projection is ONE field, so the path / name / image
// keys in the same object were never decoded and cannot be rendered or stored.
let workloadText = String(describing: response.nodes[0].workloads ?? [])
expect("§7_redaction", "no recipe key in the decoded workload", !workloadText.contains("recipe"))
expect("§7_redaction", "no absolute path in the decoded workload", !workloadText.contains("/"))
expect("§7_redaction", "no recipe file name (.yaml) in the decoded workload", !workloadText.contains(".yaml"))
expect("§7_redaction", "no container/image key in the decoded workload",
       !workloadText.contains("containers") && !workloadText.contains("image"))
expect("§7_redaction", "no node address in the decoded workload", !workloadText.contains("192."))
expect("§7_redaction", "the entry itself carries no path and no address", {
    let entry = ClusterEntry(date: .now, state: .serving, pairs: [], modelCount: fleet.modelsServed,
                             idleMinutesRemaining: nil, lastKnownAgeMinutes: nil, configured: true,
                             runningCards: nil, queueDepth: nil, blockedCards: nil, paused: false,
                             nodesUp: fleet.nodesUp, nodesTotal: fleet.nodesTotal,
                             gpuUtilPct: fleet.gpuUtilPct, gpuTempC: fleet.gpuTempC,
                             cpuTempC: fleet.cpuTempC, load1m: fleet.load1m)
    let text = String(describing: entry)
    return !text.contains("/Users") && !text.contains(".yaml") && !text.contains("192.")
}())

// ---------------------------------------------------------------------------
// 4 — degraded answers stay "no data", never zero.
// ---------------------------------------------------------------------------

let down = "down-state"
let unavailable = (try? JSONDecoder().decode(MonitorResponse.self,
                                             from: requireData("v1_cluster_monitor_unavailable.json")))
expect(down, "the 38 B down-state body decodes (speak only, no json)", unavailable?.json == nil)
expect(down, "it projects to NO data, not to a fleet of zero nodes",
       unavailable.flatMap(ClusterFleet.init(projecting:)) == nil)
let empty = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: []), speak: "none")
let emptyFleet = ClusterFleet(projecting: empty)
expect(down, "an empty hosts list nils nodesUp/nodesTotal/modelsServed",
       emptyFleet?.nodesUp == nil && emptyFleet?.nodesTotal == nil && emptyFleet?.modelsServed == nil)
expect(down, "and nils every metric (no dashes on a down fleet)", emptyFleet?.hasMetrics == false)

func sample(cpuTemp: String?, gpuTemp: String?, util: String?, load: String?) -> MonitorSample {
    MonitorSample(hostname: nil, uptime_sec: nil, cpu_usage_pct: nil, mem_used_pct: nil,
                  mem_total_mb: nil, mem_used_mb: nil, gpu_name: nil, gpu_util_pct: util,
                  gpu_mem_used_pct: nil, cpu_temp_c: cpuTemp, gpu_temp_c: gpuTemp,
                  cpu_load_1m: load)
}
func node(_ host: String, error: String?, workloads: [MonitorWorkload]?,
          util: String? = nil, gpuTemp: String? = nil, cpuTemp: String? = nil, load: String? = nil) -> MonitorNode {
    MonitorNode(host: host, error: error,
                sample: error == nil ? sample(cpuTemp: cpuTemp, gpuTemp: gpuTemp, util: util, load: load) : nil,
                used_slots: nil, free_slots: nil, workloads: workloads)
}
func wl(_ ids: [String]) -> [MonitorWorkload] { ids.map { MonitorWorkload(cluster_id: $0) } }

let mixed = ClusterFleet(projecting: MonitorResponse(
    json: MonitorPayload(timestamp: nil, hosts: [
        node("192.0.2.1", error: nil, workloads: wl(["a"]), util: "10", gpuTemp: "40", cpuTemp: "50", load: "0.5"),
        node("192.0.2.2", error: "ssh: connect timeout", workloads: nil),
    ]), speak: ""))
expect(down, "an errored host counts DOWN but stays in the total (2 listed, 1 up)",
       mixed?.nodesUp == 1 && mixed?.nodesTotal == 2)
expect(down, "a missing sample on a live host is a metric gap, not a down node",
       ClusterFleet(projecting: MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: [node("192.0.2.1", error: nil, workloads: [])]),
        speak: ""))?.nodesUp == 1)
expect(down, "metrics are nil when no host reported them (never 0%)",
       mixed?.gpuUtilPct == 10 && ClusterFleet(projecting: MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: [node("192.0.2.1", error: nil, workloads: [])]),
        speak: ""))?.gpuUtilPct == nil)
expect(down, "a live host list with no workloads is honestly 0 models (a value, not absence)",
       ClusterFleet(projecting: MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: [node("192.0.2.1", error: nil, workloads: [])]),
        speak: ""))?.modelsServed == 0)
expect("models", "two hosts under one cluster_id = 1 model",
       ClusterFleet(projecting: MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: [
            node("192.0.2.1", error: nil, workloads: wl(["a"])),
            node("192.0.2.2", error: nil, workloads: wl(["a"])),
        ]), speak: ""))?.modelsServed == 1)
expect("models", "a nil cluster_id is not a model",
       ClusterFleet(projecting: MonitorResponse(
        json: MonitorPayload(timestamp: nil, hosts: [
            MonitorNode(host: "192.0.2.1", error: nil, sample: nil, used_slots: nil, free_slots: nil,
                        workloads: [MonitorWorkload(cluster_id: nil)]),
        ]), speak: ""))?.modelsServed == 0)

// ---------------------------------------------------------------------------
// 5 — the text rules and the heat bands every family renders.
// ---------------------------------------------------------------------------

let text = "text"
expect(text, "percent rounds and suffixes", ClusterMetricText.percent(88.4) == "88%")
expect(text, "percent nil is an em dash, never 0%", ClusterMetricText.percent(nil) == ClusterMetricText.dash)
expect(text, "celsius carries its unit (colour is never the only signal)",
       ClusterMetricText.celsius(87.1) == "87°C")
expect(text, "load keeps one decimal", ClusterMetricText.load(4.02) == "4.0")
expect(text, "nodes reads up/total", ClusterMetricText.nodes(up: 3, total: 4) == "3/4")
expect(text, "nodes nil either side is a dash, not 0/0",
       ClusterMetricText.nodes(up: nil, total: 4) == ClusterMetricText.dash
        && ClusterMetricText.nodes(up: 3, total: nil) == ClusterMetricText.dash)
expect(text, "models pluralises", ClusterMetricText.models(2) == "2 models"
        && ClusterMetricText.models(1) == "1 model")
expect(text, "models 0 is a value", ClusterMetricText.models(0) == "0 models")
expect(text, "models nil is a dash", ClusterMetricText.models(nil) == ClusterMetricText.dash)

let parts = ClusterMetricText.metricParts(gpuUtilPct: 96, gpuTempC: 78, cpuTempC: 87.1, load1m: 4.02)
expect(text, "the strip reads GPU, GPU temp, CPU temp, load — in that scan order",
       parts == ["GPU 96%", "GPU 78°C", "CPU 87°C", "load 4.0"])
expect(text, "a missing metric drops its WHOLE token (no row of dashes)",
       ClusterMetricText.metricParts(gpuUtilPct: nil, gpuTempC: 78, cpuTempC: nil, load1m: nil) == ["GPU 78°C"])
expect(text, "nothing reported is an empty strip, which the view omits",
       ClusterMetricText.metricParts(gpuUtilPct: nil, gpuTempC: nil, cpuTempC: nil, load1m: nil).isEmpty)
let tokens = ClusterMetricText.tokens(gpuUtilPct: 96, gpuTempC: 78, cpuTempC: 87.1, load1m: 4.02)
expect(text, "the strip's GPU token is in the hot band (96 > 95)", tokens.first?.heat == .hot)
expect(text, "the load token is never coloured (no core count served)", tokens.last?.heat == .unknown)
expect(text, "inline line composes nodes · models · GPU",
       ClusterMetricText.inlineLine(nodesUp: 3, nodesTotal: 4, models: 2, gpuUtilPct: 96,
                                    gateLabel: "No data") == "3/4 up · 2 models · GPU 96%")
expect(text, "inline line drops a nil part",
       ClusterMetricText.inlineLine(nodesUp: nil, nodesTotal: nil, models: 2, gpuUtilPct: nil,
                                    gateLabel: "No data") == "2 models")
expect(text, "inline line falls back to the gate label when nothing is live",
       ClusterMetricText.inlineLine(nodesUp: nil, nodesTotal: nil, models: nil, gpuUtilPct: nil,
                                    gateLabel: "Timed out") == "Timed out")

let heat = "heat"
expect(heat, "88% util is calm", ClusterMetricHeat.percent(88) == .calm)
expect(heat, "91% util is warm (>90)", ClusterMetricHeat.percent(91) == .warm)
expect(heat, "96% util is hot (>95)", ClusterMetricHeat.percent(96) == .hot)
expect(heat, "bands match MonitorMeter: CPU warns at >85, others at >90",
       ClusterMetricHeat.percent(86, isCPU: true) == .warm
        && ClusterMetricHeat.percent(88, isCPU: true) == .warm
        && ClusterMetricHeat.percent(85, isCPU: true) == .calm)
expect(heat, "63°C (the fixture's GPU) reads calm", ClusterMetricHeat.celsius(63) == .calm)
expect(heat, "81°C is warm", ClusterMetricHeat.celsius(81) == .warm)
expect(heat, "91°C is hot", ClusterMetricHeat.celsius(91) == .hot)
expect(heat, "no reading is unknown, never calm", ClusterMetricHeat.celsius(nil) == .unknown)
expect(heat, "load has no band on purpose", ClusterMetricHeat.load == .unknown)

// ---------------------------------------------------------------------------
// 6 — a failure label has to fit one Lock Screen line.
// ---------------------------------------------------------------------------

let labels = "labels"
let allFailures: [WidgetDataFailure] = [
    .notConfigured, .invalidURL, .pathNotAllowed(path: "/v1/whatever"),
    .timeout(seconds: 8), .unreachable(code: 54), .unauthorized,
    .notFound(path: "/v1/mobile/summary"), .http(status: 503, code: "internal"),
    .decoding(detail: "keyNotFound"),
]
for failure in allFailures {
    expect(labels, "shortLabel of \(failure) fits an accessory line",
           failure.shortLabel.count <= 18 && !failure.shortLabel.isEmpty)
}
expect(labels, "no shortLabel leaks a path, a status code or a decode detail",
       allFailures.allSatisfy {
           let s = $0.shortLabel
           return !s.contains("/Users") && !s.contains("keyNotFound") && !s.contains("Optional")
       })
expect(labels, "operatorLine exists for every failure",
       allFailures.allSatisfy { $0.operatorLine.count > 10 })

let entry = "entry"
expect(entry, "unconfigured invites setup",
       ClusterEntry.unconfigured.shortGateLabel == "Set up in HSCC"
        && !ClusterEntry.unconfigured.configured)
expect(entry, "paused says so", ClusterEntry.paused().shortGateLabel == "Paused")
expect(entry, "a failure labels a configured tile",
       ClusterEntry(date: .now, state: .unknown, pairs: [], modelCount: nil, idleMinutesRemaining: nil,
                    lastKnownAgeMinutes: nil, configured: true, runningCards: nil, queueDepth: nil,
                    blockedCards: nil, paused: false,
                    failure: .timeout(seconds: 8)).shortGateLabel == "Timed out")
expect(entry, "live is not stale; an age makes it stale",
       !ClusterEntry.unconfigured.isStale
        && !(ClusterEntry(date: .now, state: .serving, pairs: [], modelCount: 2,
                          idleMinutesRemaining: nil, lastKnownAgeMinutes: nil, configured: true,
                          runningCards: nil, queueDepth: nil, blockedCards: nil, paused: false).isStale)
        && ClusterEntry(date: .now, state: .serving, pairs: [], modelCount: 2,
                        idleMinutesRemaining: nil, lastKnownAgeMinutes: 12, configured: true,
                        runningCards: nil, queueDepth: nil, blockedCards: nil, paused: false).isStale)
expect(entry, "the pre-pivot factories still compile and default the new signals to nil",
       ClusterEntry.unconfigured.nodesUp == nil && ClusterEntry.unconfigured.gpuTempC == nil
        && ClusterEntry.paused().load1m == nil)
expect(entry, "the cluster half defaults to every section the surface knows",
       ClusterEntry.unconfigured.showSections == ClusterEntry.availableSections
        && ClusterEntry.availableSections.contains("nodes"))

// ---------------------------------------------------------------------------
// 7 — which families pay for the board row (§2's budget).
// ---------------------------------------------------------------------------

let budget = "budget"
// The four accessory families are `@available(macOS, unavailable)`, so they
// cannot be named here — which is exactly why `showsBoardWork` is written as the
// positive list of system families, and why the .sh proves the accessory half
// structurally (their views never read the board row's fields).
expect(budget, "systemSmall keeps the board row", WidgetFamily.systemSmall.showsBoardWork)
expect(budget, "systemMedium keeps the board row", WidgetFamily.systemMedium.showsBoardWork)
expect(budget, "systemLarge keeps the board row", WidgetFamily.systemLarge.showsBoardWork)
expect(budget, "systemExtraLarge keeps it too (a new size losing a row is the worse error)",
       WidgetFamily.systemExtraLarge.showsBoardWork)

// ---------------------------------------------------------------------------

print("")
if failures.isEmpty {
    print("CLUSTER WIDGET CHECK PASSED — \(checks) assertions: monitor projection, model counting, "
        + "degraded honesty, §7 workload redaction, text + heat rules, accessory labels, board-row budget")
    exit(0)
}
print("CLUSTER WIDGET CHECK FAILED — \(failures.count)/\(checks) assertions: \(failures.joined(separator: "; "))")
exit(1)
