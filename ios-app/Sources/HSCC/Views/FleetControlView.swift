import SwiftUI

/// Fleet control (C6) — cluster up/down + template list/status/apply.
///
/// The operating surface for the serving fleet:
///   * Status / template — GET /v1/template/status (currently applied).
///   * Cluster Up — POST /v1/cluster/up (confirm-gated; starts every unit).
///   * Cluster Down — POST /v1/cluster/down (confirm-gated, destructive; the
///     confirmation NAMES what happens: it stops ALL workloads fleet-wide).
///   * Templates — GET /v1/template/list with a per-template confirm-gated
///     Apply (POST /v1/template/apply — also destructive: it re-deploys the
///     fleet).
///
/// Every mutation goes through `MutationButton` (confirm dialog + `confirm:
/// true` + honest failure). Nothing here fires from a single tap.
struct FleetControlView: View {
    let client: HSCCClient?

    @State private var status = LoadState<TemplateStatusResponse>.idle
    @State private var list = LoadState<TemplateListResponse>.idle
    @State private var selectedTemplate = ""
    @State private var forceRecreate = false

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        appliedSection
                        clusterActionsSection(client: client)
                        templateSection(client: client)
                    }
                    .padding()
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Fleet Control")
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
        VStack(spacing: 12) {
            Image(systemName: "power")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to control the fleet.")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    // MARK: - Load

    private func loadAll() async {
        guard let client else { return }
        async let s: Void = loadStatus(client)
        async let l: Void = loadList(client)
        _ = await (s, l)
    }

    private func loadStatus(_ client: HSCCClient) async {
        status = .loading
        do { status = .loaded(try await client.templateStatus()) }
        catch { status = .failed(errorMessage(for: error)) }
    }

    private func loadList(_ client: HSCCClient) async {
        list = .loading
        do {
            let response = try await client.templateList()
            list = .loaded(response)
            // Keep the picker in sync with the available templates.
            if response.templates.first(where: { $0.name == selectedTemplate }) == nil {
                selectedTemplate = response.templates.first?.name ?? ""
            }
        } catch {
            list = .failed(errorMessage(for: error))
        }
    }

    private func errorMessage(for error: Error) -> String {
        (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
    }

    // MARK: - Applied template

    @ViewBuilder
    private var appliedSection: some View {
        sectionCard(title: "Applied Template", systemImage: "rectangle.stack.badge.checkmark") {
            switch status {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 8) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(.secondary)
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
                            .foregroundColor(.secondary)
                    }
                }
            default:
                EmptyView()
            }
        }
    }

    // MARK: - Cluster up/down (confirm-gated, destructive down)

    @ViewBuilder
    private func clusterActionsSection(client: HSCCClient) -> some View {
        sectionCard(title: "Cluster", systemImage: "power") {
            VStack(alignment: .leading, spacing: 12) {
                Text("Bring the serving fleet up, or stop ALL workloads fleet-wide.")
                    .font(.subheadline)
                    .foregroundColor(.secondary)

                // Cluster Up — starts every serving unit. Confirm names it.
                MutationButton(
                    title: "Bring Fleet Up",
                    systemImage: "play.circle",
                    prompt: "Bring the fleet up? This starts every serving unit in the cluster (orchestrator + workers).",
                    run: {
                        let result = try await client.clusterUp()
                        await loadStatus(client)
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
                        await loadStatus(client)
                        return result.message ?? "Fleet down issued."
                    }
                )
            }
        }
    }

    // MARK: - Template list + apply (confirm-gated, destructive)

    @ViewBuilder
    private func templateSection(client: HSCCClient) -> some View {
        sectionCard(title: "Apply Template", systemImage: "rectangle.stack.badge.plus") {
            switch list {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 10) {
                    if state.templates.isEmpty {
                        emptyLabel("No templates available.")
                    } else {
                        Picker("Template", selection: $selectedTemplate) {
                            ForEach(state.templates) { t in
                                Text(t.name).tag(t.name)
                            }
                        }
                        .pickerStyle(.menu)
                        if let t = state.templates.first(where: { $0.name == selectedTemplate }) {
                            if let desc = t.description, !desc.isEmpty {
                                Text(desc)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            if let fams = t.families, !fams.isEmpty {
                                Text("Families: \(fams.joined(separator: ", "))")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }

                        Toggle("Force recreate", isOn: $forceRecreate)
                            .font(.subheadline)

                        // Apply — confirm-gated, names the template and that it
                        // re-deploys the fleet.
                        MutationButton(
                            title: "Apply Template",
                            systemImage: "play.rectangle.on.rectangle",
                            prompt: "Apply template \"\(selectedTemplate)\" and (re)deploy the fleet? This tears down and recreates serving units per the template.",
                            run: {
                                let name = selectedTemplate
                                let result = try await client.applyTemplate(
                                    name: name,
                                    forceRecreate: forceRecreate
                                )
                                await loadStatus(client)
                                return result.message ?? "Applied template \(name)."
                            }
                        )
                        .disabled(selectedTemplate.isEmpty)
                    }
                }
            default:
                EmptyView()
            }
        }
    }

    // MARK: - Shared building blocks

    private func sectionCard<Content: View>(
        title: String,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    private func errorLabel(_ message: String) -> some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.bad)
    }

    private func emptyLabel(_ text: String) -> some View {
        Label(text, systemImage: "tray")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
    }

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
