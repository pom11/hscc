import Foundation
import ActivityKit

// ===========================================================================
// HSCC Monitor Live Activity — headless data-path check (task t_03d318cd).
//
// Compiled against the REAL committed models + REAL MonitorContent derivation
// (injected below) and the LIVE /v1/cluster/monitor + /v1/daemon/host JSON
// (passed as argv[1] dir), then runs the REAL MonitorContent.make to print the
// derived ContentState the extension would render. Only Foundation/ActivityKit
// — no app code beyond the injected models.
// ===========================================================================

MODELS_MARKER

// ---- Harness body ----
// Plain structs (no main-actor); ContentState/ActivityAttributes are Codable.
let dir = CommandLine.arguments[1]

func loadJSON<T: Decodable>(_ name: String, as type: T.Type) -> T? {
    let path = (dir as NSString).appendingPathComponent(name)
    guard let data = FileManager.default.contents(atPath: path) else {
        print("missing file: \(name)"); return nil
    }
    return try? JSONDecoder().decode(T.self, from: data)
}

func printIf(_ label: String, _ v: Any?) -> String {
    guard let v else { return "—" }
    return "\(label)\(v)"
}

// Decode the real wire shapes.
let monitor = loadJSON("cluster_monitor.json", as: ClusterMonitorResponse.self)
let daemonHost = loadJSON("daemon_host.json", as: DaemonHostResponse.self)

if monitor == nil {
    print("FAIL: /v1/cluster/monitor did NOT decode against ClusterMonitorResponse")
    exit(1)
}
if daemonHost == nil {
    print("FAIL: /v1/daemon/host did NOT decode against DaemonHostResponse")
    exit(1)
}

// Run the REAL derivation with all sections on (the monitor surface default).
let sections: Set<String> = ["state", "nodes", "host"]

// Decoded as the SHARED `MonitorResponse` — what the driver decodes this route
// as (t_1685f884), what `MonitorContent.make` now takes, and what
// `ClusterMonitorViewModel` projects the CLI's tables from. The app's parallel
// `ClusterMonitorResponse` above is still decoded, because nothing has retired
// it yet; it is no longer what the activity is built from.
let shared = loadJSON("cluster_monitor.json", as: MonitorResponse.self)
guard let shared else {
    print("FAIL: /v1/cluster/monitor did NOT decode against the shared MonitorResponse")
    exit(1)
}

let content = MonitorContent.make(monitor: shared,
                                  daemonHost: daemonHost,
                                  state: "serving",
                                  updatedAt: Date(),
                                  sections: sections)

print("== Derived MonitorActivityAttributes.ContentState ==")
print("state        : \(content.state)")
print("updatedAt    : \(content.updatedAt)")
print("nodes.count  : \(content.nodes.count)")
print("nodesUp      : \(printIf("nodesUp=", content.nodesUp))")
print("nodesTotal   : \(printIf("nodesTotal=", content.nodesTotal))")
print("models       : \(printIf("models=", content.models))")
print("genTps       : \(printIf("genTps=", content.genTps))")
for n in content.nodes {
    var parts = ["\(n.label) -> CPU \(fmt(n.cpuPct))%"]
    parts.append("RAM \(fmt(n.memPct))%")
    if let g = n.gpuName { parts.append("GPU \(g) \(fmt(n.gpuUtilPct))%") }
    if let model = n.model { parts.append("model \(model)") }
    if let gpuTemp = n.gpuTempC { parts.append("GPU \(Int(gpuTemp.rounded()))°C") }
    if let cpuTemp = n.cpuTempC { parts.append("CPU \(Int(cpuTemp.rounded()))°C") }
    print("  " + parts.joined(separator: ", "))
}
if let h = content.host {
    print("host:")
    print("  \(h.hostname) (\(h.os) \(h.arch))")
    print("  CPU \(fmt(h.cpuPct))%  RAM \(fmt(h.memPct))%  Disk \(fmt(h.diskPct))%  daemonRunning=\(h.daemonRunning)")
} else {
    print("host: <nil> — host section would be hidden")
}

// Sanity assertions against realistic expectations.
var ok = true
if content.nodes.isEmpty { print("WARN: no nodes derived (empty monitor hosts? section on)"); }
if content.host == nil { print("WARN: host nil though section on (daemon host decode ok but make excluded?)") }

// The pushed tally and serving totals must equal the fields the live wire
// carried — not a second opinion computed by the extension.
let expectedUp = monitor!.hosts.filter { $0.error == nil }.count
let expectedServing = ServingSummary(rows: monitor!.serving)
if content.nodesUp != expectedUp || content.nodesTotal != monitor!.hosts.count {
    print("FAIL: tally \(String(describing: content.nodesUp))/\(String(describing: content.nodesTotal)) != wire \(expectedUp)/\(monitor!.hosts.count)")
    ok = false
}
if content.models != expectedServing.models || content.genTps != expectedServing.genTps {
    print("FAIL: serving totals != wire serving[]")
    ok = false
}
let wireModels = Set((monitor!.serving ?? []).compactMap { $0.shortModel })
let pushedModels = Set(content.nodes.compactMap { $0.model })
if !pushedModels.isSubset(of: wireModels) {
    print("FAIL: a pushed node model is not in serving[]")
    ok = false
}

// ---- t_1685f884: the cluster-metric half of the SAME ContentState -------------
//
// Until this card the activity carried per-node meters and a state word, and
// this gate printed exactly that. It now carries the host table, the serving
// totals and two glance metrics too — and a gate that prints half a payload
// certifies half a payload, so the new half is printed and checked HERE, off
// the same bytes and off the same `shared` decode `MonitorContent.make` used.
//
// Reduced by `MonitorSurfaceReading` — the rules the shipped surfaces call,
// compiled verbatim out of Sources/Shared/MonitorActivityMetrics.swift.
let surface = MonitorSurfaceReading.payload(nodes: shared.nodes, serving: shared.serving)

print("== Cluster metrics the surfaces draw ==")
print("hostRows     : \(surface.hostRows.map { "\($0.count) pushed" } ?? "nil (no table this push)"), "
      + "+\(surface.hostRowsDropped ?? 0) dropped, "
      + "\(surface.nodesUp.map(String.init) ?? "—")/\(surface.nodesTotal.map(String.init) ?? "—") hosts up")
for row in surface.hostRows ?? [] {
    let cells = MonitorSurfaceReading.detailCells(row).joined(separator: " | ")
    let tail = MonitorSurfaceReading.servingText(running: row.running, waiting: row.waiting)
    print("  \(row.label)  \(row.isFailure ? MonitorSurfaceReading.notSampledText : cells)"
          + "  \(tail ?? "—")")
}
for total in surface.servingTotals ?? [] {
    print("  \(MonitorSurfaceReading.servingTotalText(total, modelVisible: true))")
}
if let aggregate = MonitorSurfaceReading.servingSummaryText(running: surface.servingRunning,
                                                           waiting: surface.servingWaiting) {
    print("serving      : \(aggregate) (\(String(describing: surface.queueLevel)))")
}
if let p = surface.primary {
    print("glance       : primary \(p.value) [\(p.kind)] \(p.label ?? "") (\(String(describing: p.level)))")
} else {
    print("glance       : primary — nothing measurable was told")
}
if let s = surface.secondary {
    print("               secondary \(s.value) [\(s.kind)]")
} else {
    print("               secondary — empty rather than a repeat of the primary")
}

/// A dotted-quad IPv4 address, decided WITHOUT a regex: four non-empty numeric
/// dot-groups. `52.6 C` and `.241` are one and two groups respectively, so a
/// CLI cell or a shortened label cannot trip it.
func looksLikeAddress(_ text: String) -> Bool {
    let groups = text.split(separator: ".", omittingEmptySubsequences: false)
    return groups.count == 4 && groups.allSatisfy { !$0.isEmpty && $0.allSatisfy(\.isNumber) }
}

// §7: an activity's state is written to disk by ActivityKit and outlives the
// read, so no string it stores may be an address. The control underneath keeps
// this honest: if the wire stopped carrying addresses, the scan would be green
// for the wrong reason.
var addressHits: [String] = []
for row in surface.hostRows ?? [] {
    for text in [row.label] + MonitorSurfaceReading.detailCells(row) where looksLikeAddress(text) {
        addressHits.append("\(row.label): \(text)")
    }
}
if !addressHits.isEmpty {
    print("FAIL: §7 — an address reached the pushed state: \(addressHits.joined(separator: ", "))")
    ok = false
}
if (surface.hostRows?.isEmpty == false) && !shared.nodes.contains(where: { looksLikeAddress($0.host) }) {
    print("WARN: the wire carried no dotted-quad, so the §7 scan above proves nothing")
}

// The push cap is what the surfaces' `+N more` counts are built on, and the
// drop count has to account for every host the route listed.
if let rows = surface.hostRows, rows.count > MonitorSurfaceLimits.maxPushedHostRows {
    print("FAIL: pushed \(rows.count) host rows, over the cap of \(MonitorSurfaceLimits.maxPushedHostRows)")
    ok = false
}
if surface.nodesTotal != shared.nodes.count {
    print("FAIL: nodesTotal \(String(describing: surface.nodesTotal)) != hosts the route listed \(shared.nodes.count)")
    ok = false
}
if let dropped = surface.hostRowsDropped,
   let total = surface.nodesTotal,
   dropped != max(0, total - (surface.hostRows?.count ?? 0)) {
    print("FAIL: hostRowsDropped \(dropped) != listed \(total) − pushed \(surface.hostRows?.count ?? 0)")
    ok = false
}
// The aggregate is the serving section's own sum, not a second tally.
let surfaceSummary = ServingSummary(rows: shared.serving)
if surface.servingRunning != surfaceSummary.running || surface.servingWaiting != surfaceSummary.waiting {
    print("FAIL: aggregate Run/Wait \(String(describing: surface.servingRunning))/"
          + "\(String(describing: surface.servingWaiting)) drifted from serving[] "
          + "\(String(describing: surfaceSummary.running))/\(String(describing: surfaceSummary.waiting))")
    ok = false
}
// nil and zero are different facts: an answered read always has a table.
if surface.hostRows == nil && shared.json != nil {
    print("FAIL: the route answered but no host table was pushed")
    ok = false
}
if surface.primary == nil && (surface.hostRows?.isEmpty == false) {
    print("WARN: hosts were listed and nothing measurable came back — the glance draws —")
}

if ok {
    print("\nPASS: real /v1/cluster/monitor + /v1/daemon/host decode + derive to a populated ContentState")
} else {
    print("\nFAIL: did not derive a populated ContentState")
    exit(1)
}

func fmt(_ v: Double?) -> String {
    guard let v else { return "—" }
    return String(format: "%.0f", v)
}
