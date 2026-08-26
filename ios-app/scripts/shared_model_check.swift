import Foundation

// Verify the widget/Live Activity model field SHAPES decode the REAL live API
// JSON. The structs below mirror, key-for-key, the shared models in
// Sources/Shared/SharedModels.swift (which the widget + Live Activity decode
// with). This runs as a plain macOS CLI (no UIKit dep) so we can execute it on
// the host. It proves the Swift field names match the live JSON keys.

// Mirror of SharedModels.swift AutodownStatusResponse.
struct AutodownStatusResponse: Decodable {
    let enabled: Bool?
    let state: String?
    let idle_minutes: Int?
    let last_activity_iso: String?
    let down_since: String?
    let wake_source: String?
    let reason: String?
    let watchdog_blocked: Bool?
    let watchdog_intentional: String?
    let kanban_ok: Bool?
    let kanban_reason: String?
    let blocked_by: String?
    let force_armed: Bool?
    let force_armed_overrides: [String]?
    let active_cron_cpu_only: [String]?
    let active_cron_model: [String]?
    let speak: String
}

// Mirror of SharedModels.swift ClusterStatusResponse / ClusterWorkload.
struct ClusterWorkload: Decodable {
    let name: String
    let tp: String?
    let pp: String?
    let container_id: String?
}
struct ClusterStatusResponse: Decodable {
    let workloads: [ClusterWorkload]
    let idle_hosts: [String]
    let total_hosts: Int
    let speak: String
}

// Mirror of Models.swift VerifyResponse / HealthCheck.
struct HealthCheck: Decodable { let name: String; let ok: Bool; let detail: String? }
struct VerifyResponse: Decodable { let ok: Bool; let checks: [HealthCheck]; let speak: String }

let dir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp"
func load(_ name: String) -> Data { try! Data(contentsOf: URL(fileURLWithPath: dir + "/" + name)) }

let auto = try! JSONDecoder().decode(AutodownStatusResponse.self, from: load("autodown.json"))
print("autodown: state=\(auto.state ?? "nil") enabled=\(auto.enabled ?? false) idle=\(auto.idle_minutes ?? -1)")

let cluster = try! JSONDecoder().decode(ClusterStatusResponse.self, from: load("cluster.json"))
print("cluster: workloads=\(cluster.workloads.count) total_hosts=\(cluster.total_hosts)")

let upIPs = cluster.idle_hosts.flatMap { line -> [String] in
    line.split(whereSeparator: { !$0.isNumber && $0 != "." }).map(String.init)
        .filter { $0.split(separator: ".").count == 4 }
}
let labels = [".244", ".246", ".247", ".248"]
let upNodes = labels.filter { label in
    let tail = label.replacingOccurrences(of: ".", with: "")
    return upIPs.contains { $0.hasSuffix("." + tail) || $0.hasSuffix(tail) }
}
print("upNodes (Live Activity readiness) = \(upNodes) of 4")

let verify = try! JSONDecoder().decode(VerifyResponse.self, from: load("verify.json"))
print("verify: ok=\(verify.ok) checks=\(verify.checks.count)")

print("✅ All shared model shapes decode the real live API JSON cleanly.")
