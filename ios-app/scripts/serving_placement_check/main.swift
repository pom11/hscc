import Foundation

// ===========================================================================
// serving_placement_check/main.swift — exercise the REAL unit->node attribution
// used by "Nodes & serving" (Sources/HSCC/ServingPlacement.swift).
//
// Why this harness exists at all: the merged screen groups serving units under
// node headers, and the only API payload that pairs a unit with the machine it
// runs on is `live_status.output` inside /v1/cluster/hosts — a TEXT blob the
// cluster engine passes through verbatim from `sparkrun status`. A parse of a
// text blob is exactly the kind of code that looks fine in review and then puts
// a Stop button beside the wrong machine. So the parse is compiled VERBATIM
// (never redeclared here) and asserted against the committed live capture plus
// the malformed shapes that would each produce a WRONG node name if the parse
// were sloppy.
//
// Compiled by scripts/serving_placement_check.sh together with the real model
// sources (Models.swift, SharedModels.swift, APIError.swift) — the same
// "compile the real thing, never mirror it" rule as model_decode_check.sh.
// ===========================================================================

var failures = 0
var checks = 0

func expect(_ condition: Bool, _ what: String) {
    checks += 1
    if condition {
        print("OK   \(what)")
    } else {
        failures += 1
        print("FAIL \(what)")
    }
}

func workload(_ name: String, _ cid: String?) -> ClusterWorkload {
    ClusterWorkload(name: name, tp: "2", pp: "1", container_id: cid)
}

func host(_ ip: String, _ role: String?) -> JSONValue {
    var obj: [String: JSONValue] = ["ip": .string(ip)]
    if let role { obj["role"] = .string(role) }
    return .object(obj)
}

let fixtureDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/scripts/model_decode_check/fixtures"

// ---------------------------------------------------------------------------
// 1. THE LIVE CAPTURE — decode the committed /v1/cluster/hosts fixture with the
// REAL model, then place the committed /v1/cluster/status units with it. This
// is the case that actually runs on the phone: a fixture typo or a model drift
// fails here before the view ever sees the data.
// ---------------------------------------------------------------------------
func loadFixture(_ name: String) throws -> Data {
    try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/" + name))
}

do {
    let hostsResponse = try JSONDecoder().decode(ClusterHostsResponse.self,
                                                 from: try loadFixture("cluster_hosts.json"))
    let statusResponse = try JSONDecoder().decode(ClusterStatusResponse.self,
                                                  from: try loadFixture("v1_cluster_status.json"))
    let out = hostsResponse.live_status?["output"]?.string
    let placement = ServingPlacement.place(workloads: statusResponse.workloads,
                                           hosts: hostsResponse.hosts,
                                           liveStatusOutput: out)

    expect(placement.didPlaceAnything, "live capture: the parse placed at least one unit")
    expect(placement.unattributed.isEmpty,
           "live capture: every running unit got a node (none left unattributed)")
    // The capture holds 3 jobs: two TP pairs across .1+.2 and .3+.4, one solo
    // container on .3. So .1 and .2 host one unit each, .3 hosts two, .4 one.
    let groups = Dictionary(uniqueKeysWithValues: placement.groups.map { ($0.ip, $0.units.count) })
    expect(groups["10.0.0.1"] == 1, "live capture: 10.0.0.1 hosts exactly 1 unit")
    expect(groups["10.0.0.2"] == 1, "live capture: 10.0.0.2 hosts exactly 1 unit")
    expect(groups["10.0.0.3"] == 2, "live capture: 10.0.0.3 hosts BOTH the TP pair and the solo unit")
    expect(groups["10.0.0.4"] == 1, "live capture: 10.0.0.4 hosts exactly 1 unit")
    expect(groups["10.0.0.5"] == nil, "live capture: the NAS is never a serving node")
    expect(placement.idleNodes.isEmpty,
           "live capture: no registered node is reported idle while it hosts a unit")
    // TP truth: the pair's unit must appear under BOTH of its nodes — a naive
    // "first node only" grouping would hide half the fleet.
    let pairID = "9a0467d69586ef5f_4e690f2e56f8"
    let onOne = placement.groups.first { $0.ip == "10.0.0.1" }?.units.contains { $0.container_id == pairID } ?? false
    let onTwo = placement.groups.first { $0.ip == "10.0.0.2" }?.units.contains { $0.container_id == pairID } ?? false
    expect(onOne && onTwo, "live capture: a TP pair's unit is listed under BOTH nodes")
    // Role comes from the host rows, not from the text blob.
    expect(placement.groups.first { $0.ip == "10.0.0.1" }?.role == "gateway",
           "live capture: the node's role label comes from the host rows")
} catch {
    failures += 1
    print("FAIL live capture fixtures could not be decoded: \(error)")
}

// ---------------------------------------------------------------------------
// 2. EMPTY / ABSENT BLOB — the screen must be able to tell "nothing is placed"
// from "there is nothing to place".
// ---------------------------------------------------------------------------
let one = [workload("qwen3-30b", "abc123")]
for (label, blob) in [("nil output", nil), ("empty output", ""), ("whitespace output", "   \n  ")] {
    let r = ServingPlacement.place(workloads: one, hosts: [host("10.0.0.1", "worker")],
                                   liveStatusOutput: blob)
    expect(!r.didPlaceAnything, "\(label): reported as 'nothing placed', not as 'empty fleet'")
    expect(r.unattributed.count == 1, "\(label): the unit falls to 'not reported', never a guessed node")
    expect(r.groups.isEmpty, "\(label): no node group invented")
    expect(r.idleNodes.count == 1, "\(label): the registered node still shows as carrying nothing")
}

// ---------------------------------------------------------------------------
// 3. HOSTILE BLOBS — each of these would put a unit on a WRONG node if the
// parse matched loosely.
// ---------------------------------------------------------------------------
struct Hostile { let label: String; let blob: String }
let hostile: [Hostile] = [
    // An IPv6-ish / version string / image tag containing dots: only a real
    // dotted quad may count as a node address.
    Hostile(label: "image tag with dots is not a node",
            blob: "Job: /x/r.yaml  (tp=2, pp=1)  [cid1]  (1 container(s))\n  solo       scitrera/dgx-spark-vllm-jasl-ds4:20260609-hscc   Up 12 hours\nTotal: 1 container(s) across 1 host(s)"),
    // Out-of-range octets are not addresses.
    Hostile(label: "999.1.1.1 is not a node",
            blob: "Job: /x/r.yaml  (tp=1, pp=1)  [cid2]  (1 container(s))\n  solo       999.1.1.1   Up 1 hour\nTotal: 1 container(s) across 1 host(s)"),
    // A truncated quad must not match.
    Hostile(label: "10.0.0 is not a node",
            blob: "Job: /x/r.yaml  (tp=1, pp=1)  [cid3]  (1 container(s))\n  solo       10.0.0   Up 1 hour\nTotal: 1 container(s) across 1 host(s)"),
    // A Job line without a bracketed id must not steal the previous job's nodes.
    Hostile(label: "job line without a container id starts no block",
            blob: "Job: /x/r.yaml  (tp=1, pp=1)  [cidA]  (1 container(s))\n  node_0     10.0.0.9   Up 1 hour\nJob: /x/other.yaml  (tp=1, pp=1)  (1 container(s))\n  node_0     10.0.0.8   Up 1 hour\nTotal: 2 container(s) across 2 host(s)"),
    // Empty bracket token.
    Hostile(label: "empty [] is not a container id",
            blob: "Job: /x/r.yaml  []  (1 container(s))\n  solo       10.0.0.7   Up 1 hour\nTotal: 1 container(s) across 1 host(s)"),
    // Garbage / non-status text.
    Hostile(label: "unrelated text places nothing",
            blob: "sparkrun: command not found\nError: connection refused"),
    // The `logs:`/`stop:` trailer lines name the container, not a node.
    Hostile(label: "logs:/stop: trailer lines add no node",
            blob: "Job: /x/r.yaml  (tp=1, pp=1)  [cidB]  (1 container(s))\n  solo       10.0.0.6   Up 1 hour\n  logs: sparkrun logs 10.0.0.6\n  stop: sparkrun stop 10.0.0.6\nTotal: 1 container(s) across 1 host(s)"),
]

for h in hostile {
    let map = ServingPlacement.nodesByContainer(fromLiveStatusOutput: h.blob)
    switch h.label {
    case "image tag with dots is not a node":
        expect(map["cid1"] == nil || map["cid1"]?.isEmpty != false,
               "\(h.label) (\(String(describing: map["cid1"])))")
    case "999.1.1.1 is not a node":
        expect(map["cid2"]?.isEmpty != false, "\(h.label)")
    case "10.0.0 is not a node":
        expect(map["cid3"]?.isEmpty != false, "\(h.label)")
    case "job line without a container id starts no block":
        expect(map["cidA"] == ["10.0.0.9"],
               "\(h.label) — cidA keeps only its own node, got \(String(describing: map["cidA"]))")
        expect(!map.values.contains(where: { $0.contains("10.0.0.8") }),
               "\(h.label) — the orphan node_0 line is attributed to nobody")
    case "empty [] is not a container id":
        expect(map.isEmpty, "\(h.label)")
    case "unrelated text places nothing":
        expect(map.isEmpty, "\(h.label)")
    case "logs:/stop: trailer lines add no node":
        expect(map["cidB"] == ["10.0.0.6"],
               "\(h.label) — got \(String(describing: map["cidB"]))")
    default:
        failures += 1
        print("FAIL hostile case \(h.label) not wired up")
    }
}

// ---------------------------------------------------------------------------
// 4. IDLE + ROLE + NAS handling through the public entry point.
// ---------------------------------------------------------------------------
do {
    let hosts = [host("10.0.0.1", "gateway"), host("10.0.0.2", "worker"), host("10.0.0.9", "nas")]
    let blob = """
    Job: /x/r.yaml  (tp=1, pp=1)  [c1]  (1 container(s))
      solo       10.0.0.1   Up 1 hour
    Total: 1 container(s) across 2 host(s)
    """
    let r = ServingPlacement.place(workloads: [workload("model-a", "c1"), workload("model-b", "cX")],
                                   hosts: hosts, liveStatusOutput: blob)
    expect(r.groups.count == 1 && r.groups.first?.ip == "10.0.0.1",
           "one placed unit yields exactly one node group")
    expect(r.groups.first?.units.count == 1, "the placed unit is under its node")
    expect(r.idleNodes.map(\.ip) == ["10.0.0.2"],
           "the registered worker with nothing on it shows as idle (got \(r.idleNodes.map(\.ip)))")
    expect(!r.idleNodes.contains(where: { $0.ip == "10.0.0.9" }),
           "the NAS is never listed, not even as idle")
    expect(r.unattributed.map(\.name) == ["model-b"],
           "the container the report never mentioned is 'not reported', not dropped")
    expect(r.didPlaceAnything, "a blob that placed one unit is not reported as unparseable")
}

// Units present but a dead/blank blob: honest "unattributed", never guessed.
do {
    let r = ServingPlacement.place(workloads: [workload("model-a", "c1")], hosts: [], liveStatusOutput: nil)
    expect(r.unattributed.count == 1 && r.groups.isEmpty && !r.didPlaceAnything,
           "no hosts + no blob => one unattributed unit, no groups, placement unavailable")
}

// No units at all: the fleet really is empty; the view must be able to say so.
do {
    let r = ServingPlacement.place(workloads: [],
                                   hosts: [host("10.0.0.1", "worker")],
                                   liveStatusOutput: "Total: 0 container(s) across 1 host(s)")
    expect(r.groups.isEmpty && r.idleNodes.count == 1 && r.unattributed.isEmpty,
           "empty fleet => no groups, the node listed idle, nothing unattributed")
}

// ---------------------------------------------------------------------------
// 5. Ordering + the strip's label grammar.
// ---------------------------------------------------------------------------
do {
    let blob = """
    Job: a  [j10]  (1 container(s))
      solo       10.0.0.10   Up 1 hour
    Job: b  [j2]  (1 container(s))
      solo       10.0.0.2    Up 1 hour
    Total: 2 container(s) across 2 host(s)
    """
    let r = ServingPlacement.place(workloads: [workload("a", "j10"), workload("b", "j2")],
                                   hosts: [], liveStatusOutput: blob)
    expect(r.groups.map(\.ip) == ["10.0.0.2", "10.0.0.10"],
           "node sections sort numerically, not lexically (got \(r.groups.map(\.ip)))")
}
expect(ServingPlacement.nodeTail("10.0.0.244") == ".244", "nodeTail matches the topology strip's `.244` form")
expect(ServingPlacement.nodeTail("not-an-ip") == "not-an-ip", "nodeTail passes a non-IP through unchanged")

// ---------------------------------------------------------------------------
print("\n\(checks - failures)/\(checks) serving-placement assertions passed")
if failures > 0 {
    print("FAIL: \(failures) serving-placement assertion(s) failed")
    exit(1)
}
print("serving placement OK — the REAL parse placed the REAL capture correctly")
