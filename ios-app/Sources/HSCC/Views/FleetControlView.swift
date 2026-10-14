import SwiftUI

/// Fleet control (C6) — cluster up/down + currently-applied template status.
///
/// The operating surface for the serving fleet:
///   * Status / template — GET /v1/template/status (currently applied).
///   * Cluster Up — POST /v1/cluster/up (confirm-gated; starts every unit).
///   * Cluster Down — POST /v1/cluster/down (confirm-gated, destructive; the
///     confirmation NAMES what happens: it stops ALL workloads fleet-wide).
///
/// The template LIBRARY (browse grouped templates, preview against the
/// topology, confirm-gated apply with post-apply reload polling) lives in its
/// own `TemplatesView` — this screen is cluster up/down + the applied-status
/// read only.
///
/// LIST RECIPE (spec §5, Phase 2 C3): two `Section`s of one plain `List`. The
/// applied-status render is no longer local — it is `AppliedTemplateStatus`,
/// shared with TemplatesView (the §5 "two renders of /v1/template/status"
/// collapse). The cluster actions stay confirm-gated `MutationButton`s.
///
/// Every mutation goes through `MutationButton` (confirm dialog + `confirm:
/// true` + honest failure). Nothing here fires from a single tap.
struct FleetControlView: View {
    let client: HSCCClient?

    @State private var status = LoadState<TemplateStatusResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    appliedSection
                    clusterActionsSection(client: client)
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
        .navigationTitle("Fleet Control")
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "power", verb: "to control the fleet")
    }

    // MARK: - Load

    private func loadStatus() async {
        guard let client else { return }
        // Offline-aware: on failure with last-known (cached) data we render
        // `.stale` (last-known + "showing state from X ago") instead of a hard
        // error — matches TemplatesView for the same /v1/template/status source.
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.templateStatus,
                                    client: client) {
            try await client.templateStatus()
        }
    }

    // MARK: - Applied template

    /// The shared render (see `AppliedTemplateStatus`). This screen keeps its
    /// own header, refresh gesture and load call — only the rows are shared, so
    /// Templates and Fleet Control can no longer disagree about what the same
    /// response means.
    @ViewBuilder
    private var appliedSection: some View {
        Section("Applied template") {
            AppliedTemplateStatus(status: status) {
                Task { await loadStatus() }
            }
        }
    }

    // MARK: - Cluster up/down (confirm-gated, destructive down)

    @ViewBuilder
    private func clusterActionsSection(client: HSCCClient) -> some View {
        Section {
            // Cluster Up — starts every serving unit. Confirm names it.
            MutationButton(
                title: "Bring Fleet Up",
                systemImage: "play.circle",
                prompt: "Bring the fleet up? This starts every serving unit in the cluster (orchestrator + workers).",
                run: {
                    let result = try await client.clusterUp()
                    await loadStatus()
                    return result.message ?? "Fleet up issued."
                }
            )
            .accessibilityLabel("Bring serving fleet up")

            // Cluster Down — destructive. Confirmation NAMES what happens:
            // it stops ALL workloads fleet-wide.
            MutationButton(
                title: "Stop All Workloads",
                systemImage: "stop.circle",
                destructive: true,
                prompt: "Stop ALL workloads fleet-wide? This shuts down every serving unit across the entire cluster and interrupts any in-flight work.",
                run: {
                    let result = try await client.clusterDown()
                    await loadStatus()
                    return result.message ?? "Fleet down issued."
                }
            )
            .accessibilityLabel("Stop all cluster workloads")
        } header: {
            Text("Cluster")
        } footer: {
            Text("Bring the serving fleet up, or stop ALL workloads fleet-wide.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }
}
