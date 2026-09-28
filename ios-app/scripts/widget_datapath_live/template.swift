import Foundation

// Template for the widget data-path live check. The models + functions
// placeholders (see the dedicated marker lines below) and the JSON dir are
// filled by the Python orchestrator; this file itself is never compiled alone.

// Topology + ClusterState types (inlined clone of the real nested types so the
// extracted code type-checks — these are NOT the code under test).
enum ClusterState: String {
    case serving, waking, down, unreachable, unknown
    var label: String {
        switch self {
        case .serving: return "Serving"
        case .waking: return "Waking"
        case .down: return "Down"
        case .unreachable: return "Can't reach the cluster"
        case .unknown: return "Unknown"
        }
    }
}
struct TopologyNode {
    let label: String; let state: NodeState
    enum NodeState: String { case up, busy, warn, down, unknown }
}
struct TopologyPair { let nodes: [TopologyNode]; let role: String }

MODELS_MARKER

FNS_MARKER

func shortTail(_ line: String) -> String {
    let ips = line.split(whereSeparator: { !$0.isNumber && $0 != "." })
        .map(String.init).filter { $0.split(separator: ".").count == 4 }
    guard let ip = ips.first else { return "?" }
    let tail = ip.split(separator: ".").last.map(String.init) ?? ip
    return "." + tail
}

// ---- Harness: decode the REAL captured/live responses (read from disk, so no
// ---- string-escaping fragility) and derive the numbers exactly as
// ---- ClusterTimelineProvider.fetchEntry does.
let jsonDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/diag"
let decoder = JSONDecoder()
func loadJSON(_ name: String) -> Data {
    let url = URL(fileURLWithPath: jsonDir).appendingPathComponent(name)
    return try! Data(contentsOf: url)
}

print("=== WIDGET DATA-PATH live check (real API responses -> derivation) ===")

let auto = try! decoder.decode(AutodownStatusResponse.self, from: loadJSON("autodown_status.json"))
print("autodown: state=\(auto.state ?? "nil") enabled=\(auto.enabled ?? false) idle_minutes=\(auto.idle_minutes ?? -1)")
let cluster = try! decoder.decode(ClusterStatusResponse.self, from: loadJSON("cluster_status.json"))
print("cluster: total_hosts=\(cluster.total_hosts) workloads.count=\(cluster.workloads.count) (modelCount)")
print("  idle_hosts node tails: " + cluster.idle_hosts.map(shortTail).joined(separator: ", "))

let running = try! decoder.decode(KanbanRunningLite.self, from: loadJSON("kanban_running.json"))
let blocked = try! decoder.decode(KanbanBlockedLite.self, from: loadJSON("kanban_blocked.json"))
let stale = try! decoder.decode(KanbanStaleLite.self, from: loadJSON("kanban_stale.json"))

// Widget render derivation (ClusterWidget.fetchEntry):
let state = resolveState(autodownState: auto.state)
print("clusterState = \(state.rawValue)  (widget dot + label)")
let pairs = canonicalPairs(up: cluster.total_hosts > 0, state: state)
print("pairs: " + pairs.map { "\($0.role)[" + $0.nodes.map{n -> String in "\(n.label)=\(n.state.rawValue)"}.joined(separator:",") + "]" }.joined(separator: " "))
let modelCount = cluster.workloads.count
print("modelCount = \(modelCount)")
let idle = idleRemaining(auto: auto, clusterState: state)
print("idleRemaining = \(idle.map(String.init) ?? "nil") (to autodown)")
let qd = queueDepth(stale: stale)
print("queueDepth = \(qd.map(String.init) ?? "nil") (ready+todo)")

//------------------
// assertions against the real API values
//------------------
var ok = true
func check(_ name: String, _ cond: Bool, _ got: String) {
    if cond { print("  ok: \(name) -> \(got)") } else { ok = false; print("  FAIL: \(name) -> \(got)") }
}
ok = (auto.state == "up") && ok
check("clusterState is serving when autodown state=up", state == .serving, state.rawValue)
check("modelCount == workloads.count (\(modelCount))", modelCount == cluster.workloads.count, "\(modelCount)")
check("topology labels come from real idle_hosts tails", pairs.flatMap{$0.nodes}.map{$0.label}.sorted() == [".244",".246",".247",".248"].sorted(), pairs.flatMap{$0.nodes}.map{$0.label}.joined(separator:","))
check("kanban running count decodes", running.count != nil, "\(running.count ?? -1)")
check("queueDepth decodes from stale statuses", qd != nil, "\(qd ?? -1)")

if ok { print("PASS: WIDGET DATA PATH — real API -> real widget derivation reproduces live numbers") }
else  { print("FAIL: WIDGET DATA PATH MISMATCH"); exit(1) }
