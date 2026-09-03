import SwiftUI

/// Serving Control (per-unit) — start / stop / restart a serving unit, confirm-gated.
///
/// This is the "recover a wedged unit from the phone" surface. Fleet Control
/// (FleetControlView) handles the all-or-nothing fleet up/down; THIS screen
/// is the per-unit operating view: list every serving unit with what it serves
/// BEFORE acting, then stop or restart exactly the one you mean.
///
/// Serving units come from GET /v1/cluster/status — each `ClusterWorkload` is
/// a running vLLM workload with its served model `name`, tensor/pipeline
/// parallelism (`tp`/`pp`), and a `container_id` used to stop it.
///
/// What the server actually supports (verified against the live API + source):
///   * stop ONE unit   — POST /v1/cluster/stop  `{ container_id, confirm: true }`
///                       (truly per-unit: `sparkrun stop <container_id>`).
///   * bring fleet up  — POST /v1/cluster/up     `{ confirm: true }`
///                       (starts EVERY unit in serving.json — orchestrator +
///                       all workers. There is NO per-unit start endpoint.)
///
/// So "restart this unit" is honestly translated as: stop that unit, then bring
/// the serving fleet back up (which re-asserts every unit including the stopped
/// one). The confirm dialog names the unit AND states the up is fleet-wide — it
/// never pretends a single unit can be started alone.
///
/// Every mutation goes through `MutationButton` (confirm dialog → `confirm:
/// true` → in-flight spinner/disable → honest success/failure alert). A single
/// tap never fires a request, and the screen reloads after each mutation so the
/// operator sees the real post-action state.
struct ServingControlView: View {
    let client: HSCCClient?

    @State private var status = LoadState<ClusterStatusResponse>.idle

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        header(client: client)
                        unitsSection(client: client)
                        noteSection
                    }
                    .padding()
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Serving Control")
            .refreshable {
                if client != nil { await loadStatus() }
            }
            .task {
                if client != nil, status.value == nil, !status.isLoading {
                    await loadStatus()
                }
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "cpu", verb: "to control the serving units")
    }

    // MARK: - Load

    /// Load the serving-unit list. Uses `Offline.load` so a transient network
    /// failure shows last-known state (`.stale`) rather than blanking the
    /// screen — no single failed request should hide which units serve what.
    private func loadStatus() async {
        guard let client else { return }
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.clusterStatus,
                                    client: client) {
            try await client.clusterStatus()
        }
    }

    // MARK: - Header

    @ViewBuilder
    private func header(client: HSCCClient) -> some View {
        // The summary line reflects the honest current state.
        Text(loadingHeaderText)
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
    }

    private var loadingHeaderText: String {
        if case .loaded(let state) = status {
            let n = state.workloads.count
            return "\(n) serving unit\(n == 1 ? "" : "s") running. Pick exactly the one you need to recover — each card names what it serves so you cannot stop or restart the wrong unit."
        }
        return "Each serving unit is listed with what it serves. Stop or restart exactly the one you mean — never a blind whole-fleet action from a single tap."
    }

    // MARK: - Units

    @ViewBuilder
    private func unitsSection(client: HSCCClient) -> some View {
        switch status {
        case .loading, .idle:
            HSSectionCard(title: "Serving Units", systemImage: "cpu") {
                ProgressView()
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        case .failed(let message):
            HSSectionCard(title: "Serving Units", systemImage: "cpu") {
                HSErrorLabel(message: message)
            }
        case .stale(let state, let ageMessage):
            HSSectionCard(title: "Serving Units", systemImage: "cpu") {
                VStack(alignment: .leading, spacing: 12) {
                    StaleBanner(age: ageMessage,
                                reason: "Can't reach the cluster right now.") {
                        Task { await loadStatus() }
                    }
                    unitsBody(client: client, state: state)
                }
            }
        case .loaded(let state):
            HSSectionCard(title: "Serving Units", systemImage: "cpu") {
                unitsBody(client: client, state: state)
            }
        }
    }

    /// The unit list body for a loaded (or stale last-known) status snapshot.
    @ViewBuilder
    private func unitsBody(client: HSCCClient, state: ClusterStatusResponse) -> some View {
        if state.workloads.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                HSEmptyLabel(message: "No serving units running right now.")
                Text("A unit that is down does not appear here. `cluster up` (in Fleet Control) starts the whole serving fleet — orchestrator and workers — so a stopped unit only comes back through a fleet-up.")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        } else {
            VStack(spacing: 12) {
                ForEach(state.workloads) { unit in
                    unitCard(client: client, unit: unit)
                }
            }
        }
    }

    /// One serving unit: what it serves, its parallelism, and its container id —
    /// then Stop / Restart, both confirm-gated.
    @ViewBuilder
    private func unitCard(client: HSCCClient, unit: ClusterWorkload) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            // What this unit serves — the name is the model it hosts.
            VStack(alignment: .leading, spacing: 4) {
                Text(unit.name)
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .textSelection(.enabled)
                HStack(spacing: 12) {
                    Label(parallelismLabel(unit), systemImage: "square.grid.3x3")
                    if let cid = unit.container_id, cid != "?" {
                        Label(cid, systemImage: "shippingbox")
                    }
                }
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }

            // Stop / Restart — only meaningful when the unit has a live
            // container to stop. A unit without a container id is not running;
            // there is nothing to stop or restart (it comes back with a fleet
            // up in Fleet Control).
            if let cid = unit.container_id, cid != "?" {
                HStack(spacing: 12) {
                    MutationButton(
                        title: "Stop",
                        systemImage: "stop.circle",
                        destructive: true,
                        prompt: "Stop unit \"\(unit.name)\" (container \(cid))? This interrupts any in-flight request this unit is serving.",
                        run: {
                            let result = try await client.stopCluster(containerID: cid)
                            await loadStatus()
                            return result.message ?? "Stopped \(unit.name)."
                        }
                    )
                    .buttonStyle(.bordered)
                    .tint(Theme.Semantic.bad)

                    MutationButton(
                        title: "Restart",
                        systemImage: "arrow.clockwise.circle",
                        destructive: true,
                        prompt: "Restart unit \"\(unit.name)\" (container \(cid))? It is stopped, then the serving fleet is brought back up. The fleet-up re-asserts EVERY unit in serving.json — there is no per-unit start, so the rest of the fleet is re-started too.",
                        run: {
                            _ = try await client.stopCluster(containerID: cid)
                            let up = try await client.clusterUp()
                            await loadStatus()
                            return up.message ?? "Restarted \(unit.name)."
                        }
                    )
                    .buttonStyle(.bordered)
                    .tint(Theme.Semantic.bad)

                    Spacer()
                }
            } else {
                Text("Not running — no live container to stop or restart. It returns when the fleet is brought up (Fleet Control).")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
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

    // MARK: - Note

    private var noteSection: some View {
        HSSectionCard(title: "Fleet-wide actions live in Fleet Control", systemImage: "arrow.up.to.line") {
            Text("Stopping a unit takes it out of service immediately. To start a unit that is down — or recover after a hard stop — bring the fleet up or down from \"Fleet Control\" (same confirm gate).")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }
}
