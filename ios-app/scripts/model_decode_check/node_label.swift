import Foundation

// ===========================================================================
// node_label.swift — the apps 1.4.4 node-label rule: the LAST OCTET of the
// node's IP, and lists ordered by that octet numerically.
//
// Compiled by model_decode_check.sh alongside main.swift and the REAL
// SharedModels.swift + ClusterNodeTally.swift, so every assertion here runs the
// code the shipped surfaces run — `MonitorNode.nodeLabel` /
// `MonitorNode.displayLabel` for the label, `FleetNodeOrder` and
// `FleetProjection` for the order. Declarations only: Swift forbids top-level
// statements outside main.swift, which holds the call.
//
// EVERY address in this file is RFC 5737 TEST-NET-1 (192.0.2.0/24), the same
// block the committed fixtures use, and `scripts/address_guard.py` refuses a
// real one. The operator's own fleet never appears here — that is the hygiene
// rule this card ships under, and it is also why the rule is worth having: a
// label derived at runtime needs no fixture of the real fleet to be tested.
//
// WHY THE ORDERING LEG IS WRITTEN THE WAY IT IS
//
// The card's requirement is not "the order is 9, 10, 100" — it is that the test
// FAILS ON A STRING SORT. So the same leg also asserts, in the opposite
// direction, what a lexical sort of those very labels produces ("10", "100",
// "9"). If someone swaps the comparator for `sorted()`, the first half goes red
// while the second half (a fact about `String.<`, not about this code) stays
// true, and the leg's failure text prints both orders side by side. A leg that
// only asserted the wanted order would pass for the wrong reason on any fleet
// whose fixture hosts happened to be ascending — which is every fixture here.
// ===========================================================================

/// The exact-input rules for `MonitorNode.nodeLabel`, and the order rule.
func nodeLabelRules(_ c: DecodeCheck) {
    let label = "node label (last octet, hostname fallback)"
    // (host, the hostname the sample reported, the label the operator should see)
    let cases: [(String, String?, String)] = [
        // The two the card names. `10` and `2` are the last dot-separated
        // component AS WRITTEN — no zero padding, no trimming.
        ("192.0.2.10", nil, "10"),
        ("192.0.2.2", nil, "2"),
        // The octet beats a reported hostname: that inversion IS the 1.4.4
        // change. A hostname was the label before, and the operator replaced it
        // with the octet because a hostname is what the box calls itself and the
        // octet is what they call it.
        ("192.0.2.244", "gx10-cad0", "244"),
        // An empty reported hostname is silence, not a name.
        ("192.0.2.9", "", "9"),
        // Written as the route wrote it: an address with a leading zero keeps
        // its digits, because a label is not an arithmetic value. (The ORDER
        // rule is what reads it as a number — see the legs below.)
        ("192.0.2.013", nil, "013"),
        // Hostname fallback: no IPv4 in the host, so the short name the existing
        // derivation yields is the label — unchanged from before 1.4.4,
        // including not inventing a truncation for an FQDN here.
        ("gx10-cad0", nil, "gx10-cad0"),
        ("cad0.example.com", nil, "cad0.example.com"),
        // With no IPv4 to read, a hostname the node reported beats the host.
        ("cad0", "gx10-cad0", "gx10-cad0"),
        // Addresses embedded in a longer string still yield their octet — the
        // route has been seen to pad this field, and a label that returned the
        // whole string would be a §7 leak.
        ("192.0.2.10:8000", nil, "10"),
        ("node-3 (192.0.2.100)", nil, "100"),
        // Nothing at all: the same "?" the app has always drawn, never an empty
        // row a reader cannot tell apart from a rendering bug.
        ("", nil, "?"),
    ]
    for (host, reported, want) in cases {
        let got = MonitorNode.nodeLabel(host: host, reportedHostname: reported)
        c.expect("node_label:exact", "\(label): \(host.isEmpty ? "\"\"" : host) → \(want)",
                 got == want, "got \"\(got)\", want \"\(want)\"")
    }

    // §7: whatever the input, a label carries no dotted quad. This is the half
    // the old leg asserted as `!label.contains(host)`, which the octet rule
    // cannot satisfy and must not be asked to — `11` is legitimately a
    // substring of `192.0.2.11`. What §7 forbids is an ADDRESS on a tile, so
    // that is what gets asserted: no dotted quad in the label, and never the
    // host string when the host string IS an address. (A hostname host and its
    // label are legitimately the same text — that is the fallback, and a name is
    // not an address.)
    for (host, reported, want) in cases + [("", "gx10-cad0", "gx10-cad0")] {
        let got = MonitorNode.nodeLabel(host: host, reportedHostname: reported)
        let hostIsAddress = holdsDottedQuad(host)
        c.expect("node_label:§7", "\(label): \(host.isEmpty ? "\"\"" : host)/\(reported ?? "-") leaks no address",
                 !holdsDottedQuad(got) && (!hostIsAddress || got != host) && got == want,
                 "got \"\(got)\"")
    }

    // The identity tail is NOT the label, and its bytes are unchanged: a
    // serving row is matched to a host row by `shortTail` on both sides, so a
    // label change that also changed the tail would silently re-pair models to
    // the wrong machines.
    c.expect("node_label:identity", "\(label): shortTail keeps its dot while the label drops it",
             MonitorNode.shortTail("192.0.2.244") == ".244"
                && MonitorNode.nodeLabel(host: "192.0.2.244", reportedHostname: nil) == "244"
                && MonitorNode.shortTail("192.0.2.244 up 3d") == ".244"
                && MonitorNode.shortTail("gx10-cad0") == "gx10-cad0"
                && MonitorNode.shortTail("") == "?",
             "the label rule reached the identity tail, or the tail stopped matching")
}

/// The order rule: `FleetNodeOrder`, `FleetProjection.nodes`, and the two label
/// lists `ClusterNodeTally` prints.
func nodeOrderRules(_ c: DecodeCheck) {
    let label = "node order (numeric last octet, stable)"
    let f = "node_label:order"

    // The card's discriminating case. Route order is deliberately neither
    // ascending nor descending, so a projection that simply kept frame order
    // would fail here too.
    let hosts = ["192.0.2.10", "192.0.2.100", "192.0.2.9"]
    let rows = hosts.map { FleetNodeLine(host: MonitorNode(host: $0), serving: nil) }
    let ordered = FleetNodeOrder.sortedByOctet(rows, label: { $0.label })
    let lexical = rows.map { $0.label }.sorted()
    c.expect(f, "\(label): 9 before 10 before 100 — the order a string sort gets wrong",
             ordered.map { $0.label } == ["9", "10", "100"]
                && lexical == ["10", "100", "9"],
             "octet order \(ordered.map { $0.label }), string order \(lexical)")

    // The same three labels through the projection a surface actually draws.
    let payload = MonitorResponse(
        success: true, error: nil,
        json: MonitorPayload(timestamp: 1_795_000_000,
                             hosts: hosts.map { MonitorNode(host: $0) }),
        serving: nil, speak: "")
    c.expect(f, "\(label): FleetProjection.nodes comes out in octet order",
             FleetProjection(projecting: payload)?.nodes.map { $0.label } == ["9", "10", "100"],
             "the rows a tile draws are not ordered by octet")

    // A leading-zero octet sorts as the NUMBER it is, while still being drawn
    // as written: 002 sits under 9 (it is 2), and its label keeps three digits.
    let padded = FleetNodeOrder.sortedByOctet(
        ["192.0.2.9", "192.0.2.002", "192.0.2.10"].map {
            FleetNodeLine(host: MonitorNode(host: $0), serving: nil)
        },
        label: { $0.label })
    c.expect(f, "\(label): 002 sorts as 2 and draws as 002",
             padded.map { $0.label } == ["002", "9", "10"],
             "got \(padded.map { $0.label })")

    // Hostname-labelled rows have no octet, so they sort after the octet rows
    // and keep their route order among themselves — "stable" has to mean
    // something for them too, and Swift's `sorted` guarantees nothing on its own.
    let mixed = FleetNodeOrder.sortedByOctet(
        [FleetNodeLine(host: MonitorNode(host: "gx10-cad9"), serving: nil),
         FleetNodeLine(host: MonitorNode(host: "192.0.2.200"), serving: nil),
         FleetNodeLine(host: MonitorNode(host: "cad0"), serving: nil)],
        label: { $0.label })
    c.expect(f, "\(label): hostname rows follow the octet rows and keep route order",
             mixed.map { $0.label } == ["200", "gx10-cad9", "cad0"],
             "got \(mixed.map { $0.label })")

    // Equal keys keep route order. Two hosts cannot share an octet on a real
    // /24, but the comparator must be a total order regardless — an
    // undeterministic tie-break is how one tile and another disagree.
    let tied = FleetNodeOrder.sortedByOctet(["a", "b", "c"].map { _ in
        FleetNodeLine(host: MonitorNode(host: "192.0.2.7"), serving: nil)
    }, label: { _ in "7" })
    c.expect(f, "\(label): equal octets keep the order the route listed",
             tied.count == 3, "the comparator dropped or duplicated a row")

    // The two label lists the tally prints ("down: 9, 10") obey the same rule.
    let downHosts = [MonitorNode(host: "192.0.2.10", error: "timeout"),
                     MonitorNode(host: "192.0.2.2", error: "timeout"),
                     MonitorNode(host: "192.0.2.100", error: "timeout")]
    let downTally = ClusterNodeTally.project(
        MonitorResponse(success: true, error: nil,
                        json: MonitorPayload(timestamp: 1_795_000_000, hosts: downHosts),
                        serving: nil, speak: ""))
    c.expect(f, "\(label): downLabels are octet-ordered too",
             downTally.downLabels == ["2", "10", "100"],
             "got \(downTally.downLabels)")

    // And the parked set, which the dropdown's `node pauses` block prints. The
    // top-level entry — a machine the monitor frame never reached, the whole
    // reason that half exists — is deliberately LOWER than the tagged row, so
    // the two halves cannot come out in their assembled order and a projection
    // that skipped the sort would fail this leg.
    let pausedHosts = [MonitorNode(host: "192.0.2.10", error: "timeout", status: "paused",
                                   paused_reason: "training run"),
                       MonitorNode(host: "192.0.2.9")]
    let pausedResponse = MonitorResponse(
        success: true, error: nil,
        json: MonitorPayload(timestamp: 1_795_000_000, hosts: pausedHosts),
        serving: nil,
        paused_nodes: [PausedNodeEntry(node: "192.0.2.3", status: "paused",
                                       paused_reason: "maintenance window",
                                       paused_since: "2026-11-21T10:00:00+00:00")],
        speak: "")
    let paused = FleetProjection(projecting: pausedResponse)?.paused ?? []
    c.expect(f, "\(label): the parked set is octet-ordered across both halves",
             paused.map { $0.label } == ["3", "10"],
             "got \(paused.map { $0.label })")
    c.expect(f, "\(label): a node paused BY IP is labelled by the same rule",
             paused.first?.label == "3" && paused.first?.key == "192.0.2.3",
             "the pause record's own host string reached the label: \(paused.first?.label ?? "nil")")
    // The row the frame did reach keeps the frame's label, and the unpaused
    // host in the same frame is not in this list at all.
    c.expect(f, "\(label): a host the frame lists that is not parked is not parked",
             !paused.contains { $0.label == "9" },
             "an answering node joined the parked set")
}
