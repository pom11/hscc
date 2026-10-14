import SwiftUI

/// Nodes & serving — the node topology strip AS A HEADER, then the serving
/// units grouped under the node that runs them, each row carrying its own
/// Stop / Restart (spec IOS_REDESIGN_2026-10-14 §5 "Nodes & serving", §8
/// migration map: "ServingControlView → drawer · 'Nodes & serving' (absorbs
/// ServingControl + the NodeTopology strip)").
///
/// This screen is the merge of two surfaces that used to sit one floor apart:
///   * the unit list + per-unit mutations (was `ServingControlView`, whose
///     restyle landed in 644286f6 — one List, Stop/Restart colour split and
///     per-unit `accessibilityLabel`s landed in dca7f25c), and
///   * the topology strip (was `ClusterView`, the topology-only "Cluster"
///     drawer row).
/// Reading them in one place is the point: the strip answers "which machines
/// are up", and the very next scroll answers "and THIS is what is running on
/// each of them, with the button that takes it out of service".
///
/// RETIRES `ClusterView` as a screen. That is the §3.1 question the card asked
/// for a decision on, and the answer is: retire it, do not keep a drawer row for
/// one strip that now lives here. §3.1's drawer has no Cluster row; §5 already
/// states "Cluster hub: retired as a screen — its 14 rows become the drawer";
/// §8 maps "ClusterView hub (tab) → retired"; and §3.2's dock verb "Cluster" is
/// described as "the physical layer", which is precisely what this screen is
/// now. `AppDestination.clusterHub` survives as a mapped legacy alias (the
/// `.selfHeal` precedent), so the dock, `ContentView.Tab.cluster`, and any stale
/// deep link still land on a real screen — and no phantom drawer row appears.
///
/// WHAT'S ACTUALLY NEW HERE, and the one thing to review carefully: grouping
/// units BY NODE needs a unit→node edge that `/v1/cluster/status` does not
/// carry. It comes from parsing `live_status.output` in `/v1/cluster/hosts` —
/// the `sparkrun status` TEXT the engine forwards verbatim — in
/// `ServingPlacement.swift`, asserted by `scripts/serving_placement_check.sh`
/// (41 assertions, live-capture-driven, with a negative test). Where that parse
/// cannot place a unit, the unit lands in a "Node not reported" section rather
/// than being guessed onto a machine: an operator presses STOP beside these
/// labels, so an honest "not reported" beats a confident wrong node.
///
/// PER-SECTION LOAD STATE (spec §5, the pattern proven in Fleet at 45645201):
/// two reads, two `LoadState`s — `status` (the units) and `hosts` (the node
/// grouping). Each renders its own inline loading/failed row inside the List, so
/// a dead `/v1/cluster/hosts` cannot blank the units that `/v1/cluster/status`
/// answered, and vice versa. Full-pane spinners are never used here.
///
/// Row accessibility is deliberately different from the read-only screens: these
/// rows are NOT combined into one VoiceOver element, because each carries two
/// confirm-gated buttons that must stay individually reachable (§9 names the
/// buttons separately for exactly that reason, and both name their unit).

struct NodesServingView: View {
    let client: HSCCClient?

    /// The serving units (`GET /v1/cluster/status`).
    @State private var status = LoadState<ClusterStatusResponse>.idle
    /// The node list + the live report the unit→node grouping is parsed from
    /// (`GET /v1/cluster/hosts`). Its own `LoadState`, per §5.
    @State private var hosts = LoadState<ClusterHostsResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    topologySection(client: client)
                    unitsRegion(client: client)
                }
                .listStyle(.plain)
                .refreshable { await loadAll() }
                .task {
                    if status.value == nil, !status.isLoading {
                        await loadAll()
                    }
                }
            } else {
                HSConnectGate(systemImage: "server.rack",
                              verb: "to see which node runs what and control the serving units")
            }
        }
        .navigationTitle("Nodes & serving")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - Load

    private func loadAll() async {
        guard let client else { return }
        async let statusTask: Void = loadStatus(client)
        async let hostsTask: Void = loadHosts(client)
        _ = await (statusTask, hostsTask)
    }

    /// The unit list. `Offline.load` so a transient failure shows last-known
    /// state (`.stale`) rather than blanking which units serve what.
    private func loadStatus(_ client: HSCCClient) async {
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.clusterStatus,
                                    client: client) {
            try await client.clusterStatus()
        }
    }

    /// The node grouping. Also `Offline.load`: a placement that came from a read
    /// that succeeded earlier this session is still worth showing (marked stale)
    /// — it is the difference between "the fleet has 4 nodes, here is where your
    /// units sit (as of 6m ago)" and a screen that has forgotten its own nodes.
    private func loadHosts(_ client: HSCCClient) async {
        hosts = await Offline.load(hosts,
                                   cacheKey: EndpointPath.clusterHosts,
                                   client: client) {
            try await client.clusterHosts()
        }
    }

    // MARK: - Header section: the topology strip

    /// The signature strip, kept as the component it is (§8: "NodeTopologyView |
    /// component (kept)"). It renders whatever `[TopologyPair]` it is handed and
    /// fetches nothing, so this screen feeds it from the read it already owns —
    /// exactly as `ClusterView` did; the pair construction moved here verbatim
    /// (see `topologyPairs`).
    @ViewBuilder
    private func topologySection(client: HSCCClient) -> some View {
        Section {
            switch status {
            case .idle, .loading:
                loadingRow("Loading the cluster…")
            case .failed(let message):
                NodeTopologyView(pairs: topologyPairs())
                HSErrorLabel(message: message, retry: { Task { await loadAll() } })
            case .stale(let state, let ageMessage):
                NodeTopologyView(pairs: topologyPairs())
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadAll() }
                }
                fleetStatusLine(state)
            case .loaded(let state):
                NodeTopologyView(pairs: topologyPairs())
                fleetStatusLine(state)
            }
        } header: {
            Text("Topology")
        } footer: {
            Text("Dots reflect the reads this screen holds: the API reports node state as text, so a node the cluster does not report is shown down rather than guessed up. A unit with `tp=2` occupies BOTH nodes of its pair, so it is listed under each of them below.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// The one-line fleet status under the strip — server-authored `speak`, never
    /// a client-computed count that could disagree with it.
    private func fleetStatusLine(_ state: ClusterStatusResponse) -> some View {
        Text(state.speak)
            .font(.caption)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
    }

    /// The two canonical TP pairs: the gateway heads the orchestrator pair and
    /// only each pair's head serves HTTP — the fact that makes a naive health
    /// check call a healthy fleet broken, which is why the strip exists.
    ///
    /// MOVED VERBATIM from `ClusterView`. It is still hardcoded rather than
    /// derived from the host rows this screen now holds, and that is a known gap
    /// (flagged on the card, not fixed here): the strip is shared in spirit with
    /// the widget and the Live Activity, which draw the same pairs, so deriving
    /// pairs from real host data is one change across three surfaces — not
    /// something to half-do inside one screen's restyle.
    private func topologyPairs() -> [TopologyPair] {
        let orchestrator = TopologyPair(
            nodes: [
                TopologyNode(label: ".244", state: nodeState(ip: ".244")),
                TopologyNode(label: ".246", state: nodeState(ip: ".246")),
            ],
            role: "orchestrator"
        )
        let worker = TopologyPair(
            nodes: [
                TopologyNode(label: ".247", state: nodeState(ip: ".247")),
                TopologyNode(label: ".248", state: nodeState(ip: ".248")),
            ],
            role: "worker"
        )
        return [orchestrator, worker]
    }

    /// Node state from the honest signals available: hosts reported up → up; a
    /// status read saying otherwise → down; no signal at all → unknown. No
    /// fabricated per-node telemetry (the API does not ship any).
    private func nodeState(ip: String) -> TopologyNode.NodeState {
        switch status {
        case .loaded(let state), .stale(let state, _):
            return state.total_hosts > 0 ? .up : .down
        case .failed:
            return .unknown
        case .loading, .idle:
            return status.value == nil ? .unknown : .up
        }
    }

    // MARK: - The units, grouped by node

    /// Everything below the header. Branches on the two independent load states
    /// so that either read can fail without hiding what the other answered.
    @ViewBuilder
    private func unitsRegion(client: HSCCClient) -> some View {
        switch status {
        case .idle, .loading:
            Section("Serving units") {
                loadingRow("Loading serving units…")
            }
        case .failed(let message):
            Section("Serving units") {
                HSErrorLabel(message: message, retry: { Task { await loadStatus(client) } })
            }
        case .loaded(let state), .stale(let state, _):
            unitsSections(client: client, state: state, staleAge: status.staleMessage)
        }
    }

    @ViewBuilder
    private func unitsSections(client: HSCCClient,
                               state: ClusterStatusResponse,
                               staleAge: String?) -> some View {
        // The units section(s) share one stale banner, shown once, at the top of
        // the units region — same reason as the read-only screens: one banner per
        // load, not one per section.
        if let staleAge {
            Section {
                StaleBanner(age: staleAge, reason: "Can't reach the cluster right now.") {
                    Task { await loadAll() }
                }
            }
        }

        if state.workloads.isEmpty {
            Section("Serving units") {
                HSEmpty("No serving units",
                        message: "A unit that is down does not appear here. `cluster up` (in Fleet Control) starts the whole serving fleet — orchestrator and workers — so a stopped unit only comes back through a fleet-up.",
                        systemImage: "cpu")
            }
        } else if let placement = placement() {
            // Grouped by node: one Section per node that hosts units, in IP order.
            ForEach(placement.groups) { group in
                nodeSection(client: client, group: group)
            }
            if !placement.unattributed.isEmpty {
                unattributedSection(client: client, units: placement.unattributed)
            }
            if !placement.idleNodes.isEmpty {
                idleNodesSection(placement.idleNodes)
            }
        } else {
            // No grouping: the units are still shown (they answered!), with the
            // reason the split-by-node view is not available.
            Section {
                groupingUnavailableRow(client: client)
                ForEach(state.workloads) { unit in
                    unitRow(client: client, unit: unit)
                }
            } header: {
                Text("Serving units")
            } footer: {
                Text(ungroupedFooter)
            }
        }
    }

    /// The node grouping, or nil when it must not be presented as fact: still
    /// loading with nothing held, or failed with nothing to fall back on, or a
    /// live report that placed nothing.
    private func placement() -> ServingPlacement.Result? {
        guard let response = hosts.value else { return nil }
        let result = ServingPlacement.place(workloads: status.value?.workloads ?? [],
                                            hosts: response.hosts,
                                            liveStatusOutput: response.live_status?["output"]?.string)
        return result.didPlaceAnything ? result : nil
    }

    /// One node: its units as rows. The header names the node the way the strip
    /// above names it (`.244`) plus its registered role, so the header and the
    /// dots can be read against each other.
    @ViewBuilder
    private func nodeSection(client: HSCCClient, group: ServingNodeGroup) -> some View {
        Section {
            ForEach(group.units) { unit in
                unitRow(client: client, unit: unit)
            }
        } header: {
            Text("Node \(ServingPlacement.nodeTail(group.ip))"
                 + (group.role.map { " · \($0)" } ?? "")
                 + " (\(group.units.count) unit\(group.units.count == 1 ? "" : "s"))")
        } footer: {
            Text("Containers on \(group.ip). A `tp=2` unit spans two nodes and is listed under each — stopping it takes both containers down.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// Units the live report did not place. Named as a gap, not quietly dropped
    /// and not assigned to a machine we cannot prove.
    @ViewBuilder
    private func unattributedSection(client: HSCCClient, units: [ClusterWorkload]) -> some View {
        Section {
            ForEach(units) { unit in
                unitRow(client: client, unit: unit)
            }
        } header: {
            Text("Node not reported")
        } footer: {
            Text("The cluster's live report (`/v1/cluster/hosts` → `live_status`) did not pair \(units.count) unit\(units.count == 1 ? "" : "s") with a node, so they are not filed under any machine. The Stop/Restart buttons here still act on the unit's own container id — they do not depend on the grouping.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// Registered nodes carrying nothing — the "which machine is free" half of
    /// the question this screen exists to answer. Read-only rows.
    @ViewBuilder
    private func idleNodesSection(_ nodes: [ServingIdleNode]) -> some View {
        Section("Nodes with no serving unit") {
            ForEach(nodes) { node in
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: "circle.dashed")
                        .foregroundColor(Theme.Semantic.neutral)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                        Text(ServingPlacement.nodeTail(node.ip))
                            .font(.hsccMono(.subheadline, weight: .semibold))
                            .foregroundColor(Theme.Semantic.onSurface)
                        Text(node.role ?? "node")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    Spacer()
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Node \(node.ip)\(node.role.map { ", \($0)" } ?? ""), no serving unit")
            }
        }
    }

    @ViewBuilder
    private func groupingUnavailableRow(client: HSCCClient) -> some View {
        switch hosts {
        case .idle, .loading:
            loadingRow("Loading which node runs what…")
        case .failed(let message):
            HSErrorLabel(message: message, retry: { Task { await loadHosts(client) } })
        case .loaded, .stale:
            // We HAVE the host rows, but the live report inside them placed
            // nothing (fleet down, or the report is not in the shape we parse).
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Image(systemName: "questionmark.circle")
                    .foregroundColor(Theme.Semantic.neutral)
                Text("Node grouping unavailable — the cluster's live report did not name a node for any unit.")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    private var ungroupedFooter: String {
        let countLine: String
        if let state = status.value {
            let n = state.workloads.count
            countLine = "\(n) serving unit\(n == 1 ? "" : "s") running. Pick exactly the one you need to recover — each row names what it serves so you cannot stop or restart the wrong unit."
        } else {
            countLine = "Each serving unit is listed with what it serves."
        }
        return countLine + " Fleet-wide actions live in Fleet Control: stopping a unit takes it out of service immediately, and to start a unit that is down — or recover after a hard stop — bring the fleet up or down from there (same confirm gate)."
    }

    // MARK: - One unit row

    /// One serving unit: what it serves, its parallelism, its container id — then
    /// Stop / Restart, both confirm-gated. One List row (the recipe's rule: the
    /// row is the card).
    ///
    /// Deliberately NOT `accessibilityElement(children: .combine)`: unlike a
    /// read-only row, this one carries two buttons that must stay individually
    /// reachable, and both name the unit they act on.
    @ViewBuilder
    private func unitRow(client: HSCCClient, unit: ClusterWorkload) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(unit.name)
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .textSelection(.enabled)
                HStack(spacing: Theme.Spacing.md.rawValue) {
                    Label(parallelismLabel(unit), systemImage: "square.grid.3x3")
                    if let cid = unit.container_id, cid != "?" {
                        Label(cid, systemImage: "shippingbox")
                    }
                }
                .font(.hsccMono(.caption))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }

            if let cid = unit.container_id, cid != "?" {
                HStack(spacing: Theme.Spacing.md.rawValue) {
                    MutationButton(
                        title: "Stop",
                        systemImage: "stop.circle",
                        destructive: true,
                        prompt: "Stop unit \"\(unit.name)\" (container \(cid))? This interrupts any in-flight request this unit is serving.",
                        run: {
                            let result = try await client.stopCluster(containerID: cid)
                            await loadAll()
                            return result.message ?? "Stopped \(unit.name)."
                        }
                    )
                    .buttonStyle(.bordered)
                    .tint(Theme.Semantic.bad)
                    // Spec §5: the confirm names the unit; the BUTTON must too —
                    // otherwise VoiceOver hears "Stop" once per row with no idea
                    // which unit it points at.
                    .accessibilityLabel("Stop \(unit.name)")

                    MutationButton(
                        title: "Restart",
                        systemImage: "arrow.clockwise.circle",
                        // Restart is RECOVERY, not destruction (dca7f25c): it was
                        // `bad` beside Stop, so the two highest-contrast colours
                        // on the screen meant the same thing. The warning is
                        // carried by the words of the prompt, not by red.
                        destructive: false,
                        prompt: "Restart unit \"\(unit.name)\" (container \(cid))? It is stopped, then the serving fleet is brought back up. The fleet-up re-asserts EVERY unit in serving.json — there is no per-unit start, so the rest of the fleet is re-started too.",
                        run: {
                            _ = try await client.stopCluster(containerID: cid)
                            let up = try await client.clusterUp()
                            await loadAll()
                            return up.message ?? "Restarted \(unit.name)."
                        }
                    )
                    .buttonStyle(.bordered)
                    .tint(Theme.Semantic.accent)
                    .accessibilityLabel("Restart \(unit.name)")

                    Spacer()
                }
            } else {
                Text("Not running — no live container to stop or restart. It returns when the fleet is brought up (Fleet Control).")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .padding(Theme.Spacing.md.rawValue)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// "tp=1 · pp=1" or "—" when the status parser couldn't extract them.
    private func parallelismLabel(_ unit: ClusterWorkload) -> String {
        let tp = unit.tp ?? "?"
        let pp = unit.pp ?? "?"
        if (tp == "?" || tp.isEmpty) && (pp == "?" || pp.isEmpty) {
            return "parallelism unknown"
        }
        return "tp=\(tp) · pp=\(pp)"
    }

    /// Inline loading line for a section that is still fetching — never the
    /// centred `HSLoading`, because the sibling sections may already have content.
    private func loadingRow(_ label: String) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            ProgressView()
            Text(label)
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }
}
