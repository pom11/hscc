import SwiftUI

/// The templates library — browse, preview, and confirm-gated apply.
///
/// Replaces the old single-dropdown section: the operator picks a fleet layout
/// the way they pick a configuration — by seeing what it does to the cluster.
/// Templates are grouped by their `group` (1node / 2node / … / other), each
/// shows its families, and the currently-applied one is marked distinctly.
///
/// Selecting a template opens `TemplateDetailView`, which shows its shape
/// (`TemplateTopologyView`), a read-only preview of exactly what applying it
/// would change (`GET /v1/template/preview/{name}`), and the confirm-gated
/// apply. Apply never fires from a single tap: the confirmation states the
/// real consequence in plain words — it stops and restarts serving units and
/// takes several minutes, during which the fleet cannot serve. After apply the
/// view shows the fleet reloading and polls `/v1/template/status` +
/// `/v1/verify` rather than claiming instant success.
struct TemplatesView: View {
    let client: HSCCClient?

    @State private var status = LoadState<TemplateStatusResponse>.idle
    @State private var list = LoadState<TemplateListResponse>.idle
    @State private var selected: ClusterTemplate?
    @State private var showDetail = false

    var body: some View {
        ScrollView {
            if let client {
                VStack(alignment: .leading, spacing: 16) {
                    appliedCard
                    librarySection(client: client)
                }
                .padding(.horizontal)
                .padding(.top, 8)
                .padding(.bottom)
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Templates")
        .refreshable { if client != nil { await loadAll() } }
        .task {
            if client != nil, status.value == nil, !status.isLoading {
                await loadAll()
            }
        }
        .sheet(isPresented: $showDetail) {
            if let selected, let client {
                TemplateDetailView(client: client,
                                   template: selected,
                                   onApplied: { await refreshStatus() })
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "square.grid.2x2")
                .font(.system(size: 44))
                .foregroundColor(Theme.Semantic.neutral)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to browse templates.")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.templateStatus,
                                    client: client) {
            try await client.templateStatus()
        }
    }

    private func loadList(_ client: HSCCClient) async {
        list = await Offline.load(list,
                                  cacheKey: EndpointPath.templateList,
                                  client: client) {
            try await client.templateList()
        }
    }

    private func refreshStatus() async {
        guard let client else { return }
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.templateStatus,
                                    client: client) {
            try await client.templateStatus()
        }
    }

    private func errorMessage(for error: Error) -> String {
        (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
    }

    // MARK: - Applied template (status)

    @ViewBuilder
    private var appliedCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Applied Template", systemImage: "rectangle.stack.badge.checkmark")
                .font(.headline)
            switch status {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let ageMessage):
                VStack(alignment: .leading, spacing: 10) {
                    StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                        Task { await refreshStatus() }
                    }
                    appliedBody(state)
                }
            case .loaded(let state):
                appliedBody(state)
            default:
                EmptyView()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    @ViewBuilder
    private func appliedBody(_ state: TemplateStatusResponse) -> some View {
        if let applied = state.applied,
           let name = applied.template, !name.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text(name)
                    .font(.hsccMono(20, weight: .bold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Text(state.speak)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        } else {
            Label("No template applied yet.",
                  systemImage: "tray")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if !state.speak.isEmpty {
                Text(state.speak)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    // MARK: - The browsable library

    @ViewBuilder
    private func librarySection(client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Library")
                .font(.title3.weight(.semibold))
            switch list {
            case .loading:
                ProgressView()
            case .failed(let message):
                VStack(alignment: .leading, spacing: 8) {
                    errorLabel(message)
                    Text("Pull to retry, or check that the cluster is reachable.")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            case .stale(let state, let ageMessage):
                VStack(alignment: .leading, spacing: 12) {
                    StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                        Task { await loadList(client) }
                    }
                    if state.templates.isEmpty {
                        emptyLabel("No templates are available right now.")
                    } else {
                        groupedLibrary(state.templates)
                    }
                }
            case .loaded(let state):
                if state.templates.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        emptyLabel("No templates are available right now.")
                        Text("Add template definitions to the cluster to see them here.")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                } else {
                    groupedLibrary(state.templates)
                }
            default:
                EmptyView()
            }
        }
    }

    /// Group the templates by their `group` field and render a section per
    /// group, in the conventional node-count order (1node → 2node → … → other).
    private func groupedLibrary(_ templates: [ClusterTemplate]) -> some View {
        let appliedName = currentAppliedName
        let groups = Self.orderedGroups(templates)
        return VStack(alignment: .leading, spacing: 20) {
            ForEach(groups, id: \.self) { group in
                let members = templates.filter { ($0.group ?? "") == group }
                if !members.isEmpty {
                    groupSection(group, members: members, appliedName: appliedName)
                }
            }
        }
    }

    /// Render one group's templates as a section with a header + rows.
    private func groupSection(_ group: String,
                              members: [ClusterTemplate],
                              appliedName: String?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(groupHeaderLabel(group))
                .font(.subheadline.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .textCase(.uppercase)
            ForEach(members) { template in
                templateRow(template, applied: template.name == appliedName)
            }
        }
    }

    /// One template row: families, description, and an unmistakable Applied
    /// marker when it's the one live right now.
    private func templateRow(_ template: ClusterTemplate, applied: Bool) -> some View {
        Button {
            selected = template
            showDetail = true
        } label: {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 8) {
                        Text(template.name)
                            .font(.body.weight(.semibold))
                            .foregroundColor(Theme.Semantic.onSurface)
                            .multilineTextAlignment(.leading)
                        if applied {
                            AppliedBadge()
                        }
                    }
                    if let desc = template.description, !desc.isEmpty {
                        Text(desc)
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .multilineTextAlignment(.leading)
                    }
                    if let fams = template.families, !fams.isEmpty {
                        Text("Families: \(fams.joined(separator: ", "))")
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .padding(.top, 4)
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Theme.Semantic.surfaceRaised)
            )
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows what applying \(template.name) would change.")
    }

    /// The name of the currently-applied template, if any.
    private var currentAppliedName: String? {
        guard case .loaded(let state) = status,
              let name = state.applied?.template, !name.isEmpty else {
            return nil
        }
        return name
    }

    /// Order the distinct groups by node count, then "other"/empty last.
    static func orderedGroups(_ templates: [ClusterTemplate]) -> [String] {
        let groups = Set(templates.map { $0.group ?? "" })
        return groups.sorted { a, b in
            let ra = rank(a), rb = rank(b)
            if ra != rb { return ra < rb }
            return a < b
        }
    }

    /// Group sort rank: "Nnode" → its node count; anything else → Int.max.
    private static func rank(_ group: String) -> Int {
        if group == "" { return Int.max }
        let digits = group.filter(\.isNumber)
        if digits.isEmpty { return Int.max }
        return Int(digits) ?? Int.max
    }

    /// The human section header for a group value.
    private func groupHeaderLabel(_ group: String) -> String {
        group.isEmpty ? "Other" : group
    }

    // MARK: - Building blocks

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
}

/// The unmistakable marker on the currently-applied template row.
private struct AppliedBadge: View {
    var body: some View {
        Text("APPLIED")
            .font(.caption2.weight(.bold))
            .foregroundColor(Theme.Semantic.surface)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(
                Capsule().fill(Theme.Semantic.ok)
            )
            .accessibilityLabel("Currently applied")
    }
}
