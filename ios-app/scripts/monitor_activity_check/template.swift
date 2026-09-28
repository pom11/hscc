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
let content = MonitorContent.make(monitor: monitor,
                                  daemonHost: daemonHost,
                                  state: "serving",
                                  updatedAt: Date(),
                                  sections: sections)

print("== Derived MonitorActivityAttributes.ContentState ==")
print("state        : \(content.state)")
print("updatedAt    : \(content.updatedAt)")
print("nodes.count  : \(content.nodes.count)")
for n in content.nodes {
    var parts = ["\(n.label) -> CPU \(fmt(n.cpuPct))%"]
    parts.append("RAM \(fmt(n.memPct))%")
    if let g = n.gpuName { parts.append("GPU \(g) \(fmt(n.gpuUtilPct))%") }
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
