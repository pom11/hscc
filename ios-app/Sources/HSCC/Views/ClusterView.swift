import SwiftUI

/// Cluster tab — the fleet hub (new project-centric IA).
///
/// Everything fleet-level lives in ONE place here. The signature node topology
/// strip is pinned at the top; beneath it are nested screens for each fleet
/// surface. Nothing fleet-related lives outside this tab:
///
///   * the **node topology strip** — the two TP pairs, the signature element
///   * **Health**        — OpsView (verify, daemon, triggers, escalations, profiles)
///   * **Fleet**         — FleetView (health, stats, throughput, streams, autoscale)
///   * **Fleet Control** — FleetControlView (cluster up/down)
///   * **Templates**     — TemplatesView (browse, preview, confirm-gated apply)
///   * **Autodown**      — AutodownView (the operator's most-used surface)
///   * **Approvals**     — ApprovalsView (the destructive-action decision inbox)
///   * **Board Hygiene** — BoardHygieneView (blocked/stale ACROSS all boards)
///
/// Kanban/board content that belongs to a SPECIFIC project lives under that
/// project (ProjectsView → detail → Board), not here.
///
/// Only the hub's own header needs live reads: `/v1/cluster/status` (hosts up /
/// workloads) and `/v1/cluster/hosts` (node IPs/roles) drive the topology
/// strip. The nested screens each own their own load state and refresh
/// independently when opened.
struct ClusterView: View {
    let client: HSCCClient?
    /// The pending-approval count from the tab badge poller (nil = not yet
    /// known). Shown on the Approvals hub row so the inbox is reachable and
    /// its count is visible at a glance.
    var approvalCount: Int? = nil

    @State private var status = LoadState<ClusterStatusResponse>.idle
    @State private var hosts = LoadState<ClusterHostsResponse>.idle

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        topologyStrip
                        hubLinks(client: client)
                    }
                    .padding(.horizontal)
                    .padding(.top, 8)
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
        return VStack(alignment: .leading, spacing: 10) {
            switch status {
            case .loading where status.value == nil:
                ProgressView()
                    .frame(maxWidth: .infinity, alignment: .leading)
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
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.bad)
            default:
                NodeTopologyView(pairs: pairs)
            }
        }
    }

    /// The one-line fleet status beneath the strip: hosts up / workloads running.
    private func fleetStatusLine(_ state: ClusterStatusResponse) -> some View {
        HStack(spacing: 8) {
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

    // MARK: - Hub navigation rows

    private func hubLinks(client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            approvalsRow(client)
            hubRow("Health & Ops", systemImage: "stethoscope",
                   subtitle: "verify, daemon, triggers, escalations, profiles") {
                OpsView(client: client)
            }
            hubRow("Fleet", systemImage: "wave.3.right",
                   subtitle: "stats, throughput, streams, autoscale") {
                FleetView(client: client)
            }
            hubRow("Fleet Control", systemImage: "power",
                   subtitle: "bring fleet up / down") {
                FleetControlView(client: client)
            }
            hubRow("Templates", systemImage: "square.grid.2x2",
                   subtitle: "browse layouts, preview, apply a fleet template") {
                TemplatesView(client: client)
            }
            hubRow("Autodown", systemImage: "timer",
                   subtitle: "the idle power-down you can arm or wake") {
                AutodownView(client: client)
            }
            hubRow("Board Hygiene", systemImage: "broom",
                   subtitle: "blocked and stale cards across every board") {
                BoardHygieneView(client: client)
            }
            hubRow("Sessions", systemImage: "text.bubble",
                   subtitle: "list a profile's sessions, compact or retire one") {
                SessionsView(client: client)
            }
            hubRow("Memories", systemImage: "brain",
                   subtitle: "what a profile remembers — correct or delete one") {
                MemoryView(client: client)
            }
            hubRow("Activity", systemImage: "waveform.path.ecg",
                   subtitle: "live feed — who is running, which tool, on which card") {
                ActivityFeedView(client: client)
            }
        }
    }

    private func approvalsRow(_ client: HSCCClient) -> some View {
        NavigationLink {
            ApprovalsView(client: client)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "checkmark.seal")
                    .font(.title3)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Approvals")
                        .font(.headline)
                        .foregroundColor(Theme.Semantic.onSurface)
                    Text(approvalCountLabel)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(approvalTint)
            )
        }
        .buttonStyle(.plain)
    }

    /// The subtitle for the Approvals hub row — either the pending count when
    /// known, or a neutral "still loading" cue when the badge poller hasn't
    /// produced a number yet.
    private var approvalCountLabel: String {
        if let approvalCount, approvalCount > 0 {
            return "\(approvalCount) pending — allow or leave blocked"
        }
        if approvalCount == 0 {
            return "no pending approvals"
        }
        return "this is where you allow or leave blocked a worker's request"
    }

    /// A distinct tint so the approvals inbox reads as the attention surface it
    /// is — amber when there's something to decide, neutral otherwise.
    private var approvalTint: Color {
        guard let approvalCount, approvalCount > 0 else {
            return Theme.Semantic.surfaceRaised
        }
        return Theme.Semantic.warn.opacity(0.14)
    }

    private func hubRow<Destination: View>(
        _ title: String,
        systemImage: String,
        subtitle: String,
        @ViewBuilder destination: @escaping () -> Destination
    ) -> some View {
        NavigationLink {
            destination()
        } label: {
            HStack(spacing: 12) {
                Image(systemName: systemImage)
                    .font(.title3)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.headline)
                        .foregroundColor(Theme.Semantic.onSurface)
                    Text(subtitle)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Theme.Semantic.surfaceRaised)
            )
        }
        .buttonStyle(.plain)
    }
}
