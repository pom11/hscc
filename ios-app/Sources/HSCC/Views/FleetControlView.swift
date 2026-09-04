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
/// Every mutation goes through `MutationButton` (confirm dialog + `confirm:
/// true` + honest failure). Nothing here fires from a single tap.
struct FleetControlView: View {
    let client: HSCCClient?

    @State private var status = LoadState<TemplateStatusResponse>.idle

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        appliedSection
                        clusterActionsSection(client: client)
                    }
                    .padding()
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Fleet Control")
            .refreshable { if client != nil { await loadStatus() } }
            .task {
                if client != nil, status.value == nil, !status.isLoading {
                    await loadStatus()
                }
            }
        }
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

    @ViewBuilder
    private var appliedSection: some View {
        HSSectionCard(title: "Applied Template", systemImage: "rectangle.stack.badge.checkmark") {
            switch status {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadStatus() } })
            case .stale(let state, let ageMessage):
                VStack(alignment: .leading, spacing: 8) {
                    StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                        Task { await loadStatus() }
                    }
                    statusBody(state)
                }
            case .loaded(let state):
                statusBody(state)
            default:
                EmptyView()
            }
        }
    }

    /// The rendered body for a successfully-loaded (or stale last-known) status.
    @ViewBuilder
    private func statusBody(_ state: TemplateStatusResponse) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if let applied = state.applied {
                if let name = applied.template, !name.isEmpty {
                    LabeledContent("Template") { Text(name) }
                }
                if let at = applied.applied_at, !at.isEmpty {
                    LabeledContent("Applied at") { Text(at) }
                }
                if let node = applied.orchestrator_node, !node.isEmpty {
                    LabeledContent("Orchestrator") { Text(node) }
                }
                if let fams = applied.families, !fams.isEmpty {
                    LabeledContent("Families") { Text(fams.joined(separator: ", ")) }
                }
                if let units = applied.units {
                    LabeledContent("Units") { Text(displayJSON(units)) }
                }
            } else {
                emptyLabel("No template applied.")
            }
            if let note = state.note, !note.isEmpty {
                Label(note, systemImage: "info.circle")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    // MARK: - Cluster up/down (confirm-gated, destructive down)

    @ViewBuilder
    private func clusterActionsSection(client: HSCCClient) -> some View {
        HSSectionCard(title: "Cluster", systemImage: "power") {
            VStack(alignment: .leading, spacing: 12) {
                Text("Bring the serving fleet up, or stop ALL workloads fleet-wide.")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)

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
            }
        }
    }

    // MARK: - Shared building blocks


    private func errorLabel(_ message: String, retry: (() -> Void)? = nil) -> some View { HSErrorLabel(message: message, retry: retry) }
    private func emptyLabel(_ text: String) -> some View { HSEmptyLabel(message: text) }

    private func displayJSON(_ value: JSONValue) -> String {
        switch value {
        case .int(let n): return "\(n)"
        case .double(let d): return String(format: "%.2f", d)
        case .string(let s): return s
        case .bool(let b): return b ? "true" : "false"
        case .null: return "—"
        case .object, .array: return "<complex>"
        }
    }
}
