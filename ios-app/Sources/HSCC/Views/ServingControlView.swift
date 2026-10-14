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
///
/// LIST RECIPE (spec §5, Phase 2 C3): one plain `List`, two `Section`s (Serving
/// units · the fleet-wide note). The `HSSectionCard` box and the per-unit
/// `surfaceRaised` card inside it are gone — the List row is the card now, so a
/// unit is a row with its Stop/Restart trailing, the same shape as every other
/// data screen. The unit rows deliberately do NOT combine their children into
/// one VoiceOver element: unlike a read-only row, each of these carries two
/// buttons that must stay individually reachable (§9 names the buttons
/// separately for exactly that reason).
///
/// Restyle only — §5's merge of this screen with the NodeTopology strip into
/// "Nodes & serving" is deliberately NOT done here (filed as its own card; it
/// changes what the destination is).
struct ServingControlView: View {
    let client: HSCCClient?

    @State private var status = LoadState<ClusterStatusResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    unitsSection(client: client)
                }
                .listStyle(.plain)
                .refreshable { await loadStatus() }
                .task {
                    if status.value == nil, !status.isLoading {
                        await loadStatus()
                    }
                }
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Serving Control")
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

    // MARK: - Header copy

    /// The screen's honest "how to use this safely" prose. It was a `Text` above
    /// the cards plus a titled card below them; the recipe puts both in the one
    /// section footer, where they describe the rows they sit under.
    private var loadingHeaderText: String {
        let countLine: String
        if case .loaded(let state) = status {
            let n = state.workloads.count
            countLine = "\(n) serving unit\(n == 1 ? "" : "s") running. Pick exactly the one you need to recover — each row names what it serves so you cannot stop or restart the wrong unit."
        } else {
            countLine = "Each serving unit is listed with what it serves. Stop or restart exactly the one you mean — never a blind whole-fleet action from a single tap."
        }
        return countLine + " Fleet-wide actions live in Fleet Control: stopping a unit takes it out of service immediately, and to start a unit that is down — or recover after a hard stop — bring the fleet up or down from there (same confirm gate)."
    }

    // MARK: - Units

    @ViewBuilder
    private func unitsSection(client: HSCCClient) -> some View {
        Section {
            switch status {
            case .idle, .loading:
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    ProgressView()
                    Text("Loading serving units…")
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            case .failed(let message):
                HSErrorLabel(message: message, retry: { Task { await loadStatus() } })
            case .stale(let state, let ageMessage):
                StaleBanner(age: ageMessage,
                            reason: "Can't reach the cluster right now.") {
                    Task { await loadStatus() }
                }
                unitsBody(client: client, state: state)
            case .loaded(let state):
                unitsBody(client: client, state: state)
            }
        } header: {
            Text("Serving units")
        } footer: {
            Text(loadingHeaderText)
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// The unit list for a loaded (or stale last-known) status snapshot.
    @ViewBuilder
    private func unitsBody(client: HSCCClient, state: ClusterStatusResponse) -> some View {
        if state.workloads.isEmpty {
            HSEmpty("No serving units",
                    message: "A unit that is down does not appear here. `cluster up` (in Fleet Control) starts the whole serving fleet — orchestrator and workers — so a stopped unit only comes back through a fleet-up.",
                    systemImage: "cpu")
        } else {
            ForEach(state.workloads) { unit in
                unitRow(client: client, unit: unit)
            }
        }
    }

    /// One serving unit: what it serves, its parallelism, and its container id —
    /// then Stop / Restart, both confirm-gated. It is one List row now (the old
    /// per-unit `surfaceRaised` card inside a card is gone).
    @ViewBuilder
    private func unitRow(client: HSCCClient, unit: ClusterWorkload) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            // What this unit serves — the name is the model it hosts.
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

            // Stop / Restart — only meaningful when the unit has a live
            // container to stop. A unit without a container id is not running;
            // there is nothing to stop or restart (it comes back with a fleet
            // up in Fleet Control).
            if let cid = unit.container_id, cid != "?" {
                HStack(spacing: Theme.Spacing.md.rawValue) {
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
                    // Spec §5: the confirm names the unit; the BUTTON must too.
                    // A VoiceOver user swiping the unit list heard only "Stop"
                    // and "Restart" repeated for every row with no idea which
                    // unit either one pointed at.
                    .accessibilityLabel("Stop \(unit.name)")

                    MutationButton(
                        title: "Restart",
                        systemImage: "arrow.clockwise.circle",
                        // Restart is RECOVERY, not destruction. It was marked
                        // destructive and tinted `bad` alongside Stop, so the
                        // two highest-contrast colours on the screen meant the
                        // same thing — an operator scanning for the safe option
                        // could not tell them apart by colour. The confirm
                        // prompt still names exactly what happens (it stops
                        // this unit and re-asserts the whole serving fleet),
                        // so the warning is carried by words, not by red.
                        destructive: false,
                        prompt: "Restart unit \"\(unit.name)\" (container \(cid))? It is stopped, then the serving fleet is brought back up. The fleet-up re-asserts EVERY unit in serving.json — there is no per-unit start, so the rest of the fleet is re-started too.",
                        run: {
                            _ = try await client.stopCluster(containerID: cid)
                            let up = try await client.clusterUp()
                            await loadStatus()
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
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
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
}
