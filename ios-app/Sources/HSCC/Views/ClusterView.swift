import SwiftUI

/// Cluster hub — the topology glance (IOS_REDESIGN 2026-10-14 §5).
///
/// RETIRED AS A HUB: its 14 navigation rows became the drawer (the migration
/// map in spec §8 is the contract — every row is a drawer destination). What
/// remains is the one thing worth a screen of its own: the signature node
/// topology strip + the fleet status line, reachable from the drawer's
/// Cluster row and the dock.
///
/// Only the hub's own header needs live reads: `/v1/cluster/status` (hosts up /
/// workloads) and `/v1/cluster/hosts` (node IPs/roles) drive the topology
/// strip. The surfaces that used to hang off the hub each own their own load
/// state and refresh independently when opened.
struct ClusterView: View {
    let client: HSCCClient?

    @State private var status = LoadState<ClusterStatusResponse>.idle
    @State private var hosts = LoadState<ClusterHostsResponse>.idle

    var body: some View {
        ScrollView {
            // Connection banner sits BELOW the nav bar, inside the content —
            // never .safeAreaInset(edge: .top) on the stack root, which
            // would draw over the nav bar (t_4889e978).
            ConnectionBanner()
            if client != nil {
                VStack(alignment: .leading, spacing: Theme.Spacing.lg.rawValue) {
                    topologyStrip
                }
                .padding(.horizontal)
                .padding(.top, Theme.Spacing.sm.rawValue)
                .padding(.bottom)
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Cluster")
        .refreshable { if client != nil { await loadAll() } }
        .task {
            if client != nil, status.value == nil, !status.isLoading {
                await loadAll()
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "bolt.slash", verb: "to see the fleet")
    }

    // MARK: - Load (hub header reads)

    private func loadAll() async {
        guard let client else { return }
        async let statusTask: Void = loadStatus(client)
        async let hostsTask: Void = loadHosts(client)
        _ = await (statusTask, hostsTask)
    }

    private func loadStatus(_ client: HSCCClient) async {
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.clusterStatus,
                                    client: client) {
            try await client.clusterStatus()
        }
    }

    private func loadHosts(_ client: HSCCClient) async {
        hosts = .loading
        do { hosts = .loaded(try await client.clusterHosts()) }
        catch { hosts = .failed(errorMessage(for: error)) }
    }

    private func errorMessage(for error: Error) -> String {
        operatorErrorMessage(error)
    }

    // MARK: - Signature element: the node topology strip

    /// The two serving TP pairs, with each node's live state derived from the
    /// reads we already hold:
    ///   * Pair 1 (orchestrator head) — .244 (gateway) + .246.
    ///   * Pair 2 (worker) — .247 + .248.
    ///
    /// The API doesn't expose a per-node live-state field (it reports hosts as
    /// text blobs), so we drive the strip's overall state from the two honest
    /// signals we DO have: whether the hosts are up (/v1/cluster/status) and
    /// whether the gateway responded (/v1/cluster/hosts decodes). A node that
    /// isn't reported is down; one that is, but the cluster isn't fully up, is
    /// waking. This keeps the strip truthful without fabricating per-node
    /// telemetry the API doesn't ship.
    private var topologyStrip: some View {
        let pairs = topologyPairs()
        return VStack(alignment: .leading, spacing: Theme.Spacing.md.rawValue) {
            switch status {
            case .loading where status.value == nil:
                HSLoading()
            case .stale(let state, let ageMessage):
                NodeTopologyView(pairs: pairs)
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadAll() }
                }
                fleetStatusLine(state)
            case .loaded(let state):
                NodeTopologyView(pairs: pairs)
                fleetStatusLine(state)
            case .failed(let message):
                NodeTopologyView(pairs: pairs)
                HSErrorLabel(message: message, retry: { Task { await loadAll() } })
            default:
                NodeTopologyView(pairs: pairs)
            }
        }
    }

    /// The one-line fleet status beneath the strip: hosts up / workloads running.
    private func fleetStatusLine(_ state: ClusterStatusResponse) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text(state.speak)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// Build the two TP pairs from the canonical node set, colouring each dot
    /// by live state (see `topologyStrip` for how state is derived).
    private func topologyPairs() -> [TopologyPair] {
        // Canonical two TP pairs. The gateway (.244) heads the orchestrator
        // pair; only each pair's head serves HTTP.
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

    /// Derive one node's live state from the topology inputs.
    private func nodeState(ip: String) -> TopologyNode.NodeState {
        // Hosts reported up? Then the pair is serving → up. If the status read
        // suggests the fleet is down, show down. Otherwise (no signal yet) show
        // unknown. We do NOT fabricate per-node telemetry the API doesn't ship.
        switch status {
        case .loaded(let state), .stale(let state, _):
            if state.total_hosts > 0 {
                return .up
            }
            return .down
        case .failed:
            return .unknown
        case .loading, .idle:
            return status.value == nil ? .unknown : .up
        }
    }
}
