import Foundation

// MARK: - Unit -> node attribution (spec §5 "Nodes & serving", t_65d791da)
//
// The merged "Nodes & serving" screen groups serving units BY NODE. That needs a
// unit -> node edge, and the API does not ship one where you would look for it:
// `GET /v1/cluster/status` reports each unit as `{name, tp, pp, container_id}`
// and nothing about which machine it runs on (SharedModels.swift `ClusterWorkload`).
//
// The only payload in the API that puts a job's containers and their hosts in
// the same place is `GET /v1/cluster/hosts` -> `live_status.output`, which is the
// raw `sparkrun status` text the cluster engine passes through VERBATIM
// (`hscc-cluster/hscc.py` `cmd_hosts` returns `run_cmd([SPARKRUN, "status"])`
// untouched, and `hscc-api/routes_cluster.py` `handle_cluster_hosts` forwards
// `live_status` as-is). Its shape:
//
//   Job: <recipe.yaml>  (tp=2, pp=1)  [<container_id>]  (2 container(s))
//     node_0     10.0.0.1     Up 12 hours    <image>
//     node_1     10.0.0.2     Up 12 hours    <image>
//     logs: sparkrun logs <container_id>
//     stop: sparkrun stop <container_id>
//
// So this file is a text parse of a text blob. That is precisely why it is
// isolated from the view, why it has a headless harness
// (`scripts/serving_placement_check.sh`) running against the committed live
// capture, and why a unit whose job block the parse cannot find is reported as
// UNATTRIBUTED instead of being guessed onto a node: an operator is about to
// press STOP beside these labels, so a wrong node name is worse than none.
//
// The job-id extraction mirrors the engine's own rule byte-for-byte (first
// whitespace-delimited token on the `Job:` line that starts with `[` and ends
// with `]`, `hscc-cluster/hscc.py` cmd_cluster_status). Deliberately the SAME
// rule, so client and server can never disagree about which token is a
// container id — if sparkrun ever changes that format both sides go blind
// together and the screen degrades honestly, instead of the client inventing a
// placement the server never reported.
//
// Deliberately NOT used for node grouping: `ClusterStatusResponse.idle_hosts`.
// The engine's status parser files EVERY indented, dotted line that is not
// `solo`/`logs:`/`stop:` under `idle_hosts` — including the container lines that
// belong to a running job. In the committed capture
// (`scripts/model_decode_check/fixtures/v1_cluster_status.json`) all four
// `idle_hosts` entries are lines that ALSO appear as containers of running
// jobs. Treating that field as "these nodes have nothing on them" would name a
// busy machine idle beside a Stop button.

// MARK: - Models

/// One node with the serving units that run on it.
struct ServingNodeGroup: Identifiable {
    /// The node's IP as the cluster reported it (`10.0.0.1`).
    let ip: String
    /// The host's role from `/v1/cluster/hosts` (`gateway` / `worker`), nil when
    /// the live report named a node the host list does not carry.
    let role: String?
    /// The units with at least one container on this node. A TP pair contributes
    /// the SAME unit to both of its nodes — that is the truth, not a duplicate.
    let units: [ClusterWorkload]

    var id: String { ip }
}

/// A node the cluster registered that carries no serving unit right now.
struct ServingIdleNode: Identifiable {
    let ip: String
    let role: String?

    var id: String { ip }
}

// MARK: - Parser

enum ServingPlacement {
    /// The grouping the screen renders.
    struct Result {
        /// One entry per node that hosts at least one unit, ordered by IP.
        let groups: [ServingNodeGroup]
        /// Registered nodes carrying no unit, ordered by IP.
        let idleNodes: [ServingIdleNode]
        /// Units the live report did not place on any node. Never guessed.
        let unattributed: [ClusterWorkload]
        /// True when the live report yielded at least one job -> node mapping.
        /// False means the blob was absent, empty, or not in the shape above —
        /// the view then says plainly that node placement is unavailable rather
        /// than leaving the reader to wonder whether the fleet is simply empty.
        let didPlaceAnything: Bool
    }

    /// A registered host row from `/v1/cluster/hosts`.
    struct HostEntry {
        let ip: String
        let role: String?
        let name: String?
    }

    /// Hosts that never serve a unit. The NAS is storage, not a serving node:
    /// the topology strip has always shown only the DGX nodes, and listing the
    /// NAS under "nodes with no serving unit" would imply it could have one.
    private static let nonServingRoles: Set<String> = ["nas"]

    // MARK: Public

    /// Group `workloads` under the nodes that host their containers.
    ///
    /// - Parameters:
    ///   - workloads: the units from `/v1/cluster/status`.
    ///   - hosts: the untyped host rows from `/v1/cluster/hosts` (for roles).
    ///   - liveStatusOutput: `live_status.output` from the same response.
    static func place(workloads: [ClusterWorkload],
                      hosts: [JSONValue],
                      liveStatusOutput: String?) -> Result {
        let placement = nodesByContainer(fromLiveStatusOutput: liveStatusOutput)
        let entries = hostEntries(from: hosts)
        var roleByIP: [String: String] = [:]
        for entry in entries {
            if let role = entry.role, roleByIP[entry.ip] == nil { roleByIP[entry.ip] = role }
        }

        // Every node the cluster registered (minus storage), plus every node the
        // live report mentioned, so a host that is up but idle still appears and
        // a node the host list lost is not silently dropped.
        var known = Set(entries.map(\.ip).filter { !nonServingRoles.contains(roleByIP[$0] ?? "") })
        for ips in placement.values { known.formUnion(ips) }

        var unitsByIP: [String: [ClusterWorkload]] = [:]
        var unattributed: [ClusterWorkload] = []
        for unit in workloads {
            let ips = unit.container_id.flatMap { placement[$0] } ?? []
            if ips.isEmpty {
                unattributed.append(unit)
            } else {
                for ip in ips where !nonServingRoles.contains(roleByIP[ip] ?? "") {
                    unitsByIP[ip, default: []].append(unit)
                }
            }
        }

        let ordered = known.sorted(by: lessThanIP)
        var groups: [ServingNodeGroup] = []
        var idle: [ServingIdleNode] = []
        for ip in ordered {
            let units = unitsByIP[ip] ?? []
            if units.isEmpty {
                idle.append(ServingIdleNode(ip: ip, role: roleByIP[ip]))
            } else {
                groups.append(ServingNodeGroup(ip: ip, role: roleByIP[ip], units: units))
            }
        }
        return Result(groups: groups,
                      idleNodes: idle,
                      unattributed: unattributed,
                      didPlaceAnything: !placement.isEmpty)
    }

    /// `container_id` -> the node IPs its job occupies, parsed from the live
    /// `sparkrun status` text. Empty when the blob is absent or unrecognised.
    static func nodesByContainer(fromLiveStatusOutput output: String?) -> [String: [String]] {
        guard let output, !output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return [:]
        }
        var result: [String: [String]] = [:]
        var currentID: String?
        var currentNodes: [String] = []

        func flush() {
            if let id = currentID, !currentNodes.isEmpty {
                var merged = result[id] ?? []
                for ip in currentNodes where !merged.contains(ip) { merged.append(ip) }
                result[id] = merged
            }
            currentNodes = []
        }

        for raw in output.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("Job:") {
                flush()
                currentID = containerID(fromJobLine: line)
                continue
            }
            // The engine's own terminator for the job blocks.
            if line.hasPrefix("Total:") {
                flush()
                currentID = nil
                continue
            }
            guard currentID != nil, let ip = ipv4Token(in: line) else { continue }
            if !currentNodes.contains(ip) { currentNodes.append(ip) }
        }
        flush()
        return result
    }

    /// Host rows as typed values. `hosts` is untyped `[JSONValue]` on purpose
    /// (every field may be null — the NAS row has no id/name/ssh_user), so this
    /// is the one place that shape becomes something the view can render.
    static func hostEntries(from hosts: [JSONValue]) -> [HostEntry] {
        hosts.compactMap { host in
            guard case .object(let obj) = host, let ip = obj["ip"]?.string, !ip.isEmpty else {
                return nil
            }
            return HostEntry(ip: ip, role: obj["role"]?.string, name: obj["name"]?.string)
        }
    }

    /// The short tail the topology strip uses (`.244` for `10.0.0.244`), so the
    /// section headers and the strip's dots name a node the same way.
    static func nodeTail(_ ip: String) -> String {
        guard ip.contains(".") else { return ip }
        let tail = ip.split(separator: ".").last.map(String.init) ?? ip
        return ".\(tail)"
    }

    // MARK: Parsing helpers

    /// First `[bracketed]` token on a `Job:` line — the engine's rule, verbatim.
    private static func containerID(fromJobLine line: String) -> String? {
        for token in line.split(separator: " ") where token.count >= 2 {
            if token.hasPrefix("[") && token.hasSuffix("]") {
                let inner = String(token.dropFirst().dropLast())
                if !inner.isEmpty { return inner }
            }
        }
        return nil
    }

    /// The first IPv4-looking token on a line — the node's address. Node lines
    /// look like `node_0  10.0.0.1  Up 12 hours  <image>` and `solo  10.0.0.3 …`;
    /// `logs:`/`stop:` lines carry a container id (no dotted quad) and are
    /// therefore skipped by construction.
    private static func ipv4Token(in line: String) -> String? {
        for token in line.split(separator: " ") where isIPv4(String(token)) {
            return String(token)
        }
        return nil
    }

    /// Dotted quad, each octet 0-255. Hand-rolled rather than a regex so the
    /// rule is auditable and there is no escape-hell in the source.
    private static func isIPv4(_ token: String) -> Bool {
        let parts = token.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return false }
        for part in parts {
            if part.isEmpty || part.count > 3 { return false }
            guard part.allSatisfy({ $0.isNumber }) else { return false }
            guard let value = Int(part), (0...255).contains(value) else { return false }
        }
        return true
    }

    /// Numeric IP order, so `10.0.0.2` sorts before `10.0.0.10`.
    private static func lessThanIP(_ lhs: String, _ rhs: String) -> Bool {
        let l = lhs.split(separator: ".").compactMap { Int($0) }
        let r = rhs.split(separator: ".").compactMap { Int($0) }
        if l.count == 4, r.count == 4 {
            for i in 0..<3 {
                if l[i] != r[i] { return l[i] < r[i] }
            }
            return l[3] < r[3]
        }
        return lhs < rhs
    }
}
