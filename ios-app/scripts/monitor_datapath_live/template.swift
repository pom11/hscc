import Foundation

// ---- committed models, extracted verbatim (run.py replaces markers) ----
MODELS_MARKER

// ---- assertions (mirrors the widget's honest derivation) ----
var failures: [String] = []
func check(_ name: String, _ cond: Bool) {
    if cond { print("  ok: " + name) } else { failures.append(name); print("FAIL: " + name) }
}

// Load the live JSON captured to disk by run.py (one arg = the tmp dir).
let dir = CommandLine.arguments[1]

func load<T: Decodable>(_ name: String, as type: T.Type) -> T? {
    guard let data = FileManager.default.contents(atPath: dir + "/" + name),
          let decoded = try? JSONDecoder().decode(T.self, from: data) else { return nil }
    return decoded
}

let monitor = load("monitor.json", as: MonitorResponse.self)
let host = load("daemon_host.json", as: MonitorDaemonHost.self)

print("== Monitor response decode ==")
if let monitor {
    print("  speak: " + monitor.speak)
    print("  nodes: " + String(monitor.nodes.count))
    for node in monitor.nodes {
        let err = node.error ?? "nil"
        print("    " + node.id + ": error=" + err + ", sample=" + (node.sample != nil ? "yes" : "no"))
        if let s = node.sample {
            let cpu = s.cpuUsagePct.map { String(format: "%.1f", $0) } ?? "nil"
            let mem = s.memUsedPct.map { String(format: "%.1f", $0) } ?? "nil"
            let gpu = s.gpuUtilPct.map { String(format: "%.1f", $0) } ?? "nil"
            let up = s.uptimeText ?? "nil"
            print("      cpu=" + cpu + "% mem=" + mem + "% gpu=" + gpu + "% up=" + up)
        }
        if let u = node.used_slots, let f = node.free_slots {
            print("      slots used=" + String(u) + " free=" + String(f))
        }
    }
    check("monitor decodes", monitor.json != nil)
} else {
    print("  MONITOR DID NOT DECODE (route degraded: cluster monitor unavailable)")
    check("monitor decodes", false)
}

print("")
print("== Daemon host decode ==")
if let host {
    let hostname = host.hostname ?? "-"
    let plat = host.platform ?? "-"
    let arch = host.arch ?? ""
    print("  " + hostname + " | " + plat + " " + arch)
    let cpu = host.cpuPercent.map { String(format: "%.1f", $0) } ?? "-"
    let count = host.cpuCount.map { String($0) } ?? "-"
    let mem = host.memUsedGB.map { String(format: "%.1f", $0) } ?? "-"
    let memT = host.memTotalGB.map { String(format: "%.1f", $0) } ?? "-"
    let disk = host.diskPercent.map { String(format: "%.1f", $0) } ?? "-"
    print("  cpu=" + cpu + "% (x" + count + ") mem=" + mem + "/" + memT + "GB disk=" + disk + "%")
    let procs = host.processes.map { String($0) } ?? "-"
    let run = host.daemon_running.map { String($0) } ?? "-"
    let up = host.uptimeText ?? "nil"
    print("  processes=" + procs + " daemon_running=" + run + " up=" + up)
    check("daemon host decodes", host.hostname != nil || host.cpu != nil)
} else {
    print("  DAEMON HOST DID NOT DECODE")
    check("daemon host decodes", false)
}

print("")
if failures.isEmpty {
    print("MONITOR DATA-PATH CHECK PASSED — live /v1/cluster/monitor + /v1/daemon/host decode into the committed models")
    exit(0)
} else {
    print("MONITOR DATA-PATH CHECK FAILED — " + String(failures.count) + " assertion(s)")
    exit(1)
}
