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
///
/// LIST RECIPE (spec §5, Phase 2 C3): one plain `List`. The applied-template
/// status is the list's first section (was a `surfaceRaised` box floating
/// above a second box), and each template group is a `Section` whose header
/// is the group name — the recipe's `sgrp` caps, not a hand-rolled label.
/// Rows are plain list rows (no per-row card background, no manual chevrons —
/// List draws those), and states use HSLoading / HSErrorLabel / HSEmpty.
struct TemplatesView: View {
    let client: HSCCClient?

    @State private var status = LoadState<TemplateStatusResponse>.idle
    @State private var list = LoadState<TemplateListResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    appliedSection()
                    librarySection(client: client)
                }
                .listStyle(.plain)
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Templates")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { if client != nil { await loadAll() } }
        .task {
            if client != nil, status.value == nil, !status.isLoading {
                await loadAll()
            }
        }
        // Sheet → PUSH (spec §3.1): TemplateDetail rides the app-wide stack
        // (a ClusterTemplate is the navigation value); the L4 sheet-in-sheet
        // chain is gone. The destination lives here so the push can still
        // hand the detail its `refreshStatus` callback.
        .navigationDestination(for: ClusterTemplate.self) { template in
            if let client {
                TemplateDetailView(client: client,
                                   template: template,
                                   onApplied: { await refreshStatus() })
            } else {
                HSConnectGate(systemImage: "square.grid.2x2", verb: "to apply a template")
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "square.grid.2x2", verb: "to browse templates")
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
        operatorErrorMessage(error)
    }

    // MARK: - Applied template (status)

    /// The applied-template status as the list's first section. The old
    /// `surfaceRaised` box is gone; List supplies the grouping and the
    /// section header.
    @ViewBuilder
    private func appliedSection() -> some View {
        Section {
            switch status {
            case .idle, .loading:
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    ProgressView()
                    Text("Loading…")
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            case .failed(let message):
                errorLabel(message, retry: { Task { await refreshStatus() } })
            case .stale(let state, let ageMessage):
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await refreshStatus() }
                }
                appliedBody(state)
            case .loaded(let state):
                appliedBody(state)
            }
        } header: {
            Text("Applied template")
        }
    }

    @ViewBuilder
    private func appliedBody(_ state: TemplateStatusResponse) -> some View {
        if let applied = state.applied,
           let name = applied.template, !name.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text(name)
                    .font(.hsccMono(20, weight: .bold))
                    .foregroundColor(Theme.Semantic.onSurface)
                if !appliedSummary(applied).isEmpty {
                    Text(appliedSummary(applied))
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
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

    /// A clean caption for the applied card, built from the structured
    /// `applied` fields instead of the server's raw `speak`. The live server's
    /// `speak` for a status is a Python dict repr (contains the orchestrator
    /// node's internal address and debug quoting) — not prose. Rendering the
    /// structured fields gives the operator the applied time, families, and
    /// unit count in one readable line. Falls back to empty (caller hides it)
    /// if there is nothing structured to say.
    private func appliedSummary(_ applied: TemplateApplied) -> String {
        var parts: [String] = []
        if let at = applied.applied_at, !at.isEmpty {
            parts.append("applied \(relativeApplied(at))")
        }
        if let families = applied.families, !families.isEmpty {
            parts.append("families: \(families.joined(separator: ", "))")
        }
        if case .int(let n)? = applied.units {
            parts.append("\(n) unit\(n == 1 ? "" : "s")")
        }
        return parts.joined(separator: " · ")
    }

    /// "3d ago"-style label for the ISO applied timestamp. Falls back to the
    /// raw timestamp if it can't be parsed. Same helper as ActivityFeedView.
    private func relativeApplied(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return iso }
        return Offline.agePhrase(Date().timeIntervalSince(date))
    }

    // MARK: - The browsable library

    @ViewBuilder
    private func librarySection(client: HSCCClient) -> some View {
        switch list {
        case .idle, .loading:
            Section {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    ProgressView()
                    Text("Loading templates…")
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        case .failed(let message):
            Section {
                errorLabel(message, retry: { Task { await loadList(client) } })
            }
        case .stale(let state, let ageMessage):
            Section {
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadList(client) }
                }
            }
            libraryGroups(state.templates)
        case .loaded(let state):
            if state.templates.isEmpty {
                Section {
                    HSEmpty("No templates",
                            message: "Add template definitions to the cluster to see them here.",
                            systemImage: "square.stack.3d.up")
                }
            } else {
                libraryGroups(state.templates)
            }
        }
    }

    /// Group the templates by their `group` field and render a `Section` per
    /// group, in the conventional node-count order (1node → 2node → … → other).
    @ViewBuilder
    private func libraryGroups(_ templates: [ClusterTemplate]) -> some View {
        let appliedName = currentAppliedName
        let groups = Self.orderedGroups(templates)
        ForEach(groups, id: \.self) { group in
            let members = templates.filter { ($0.group ?? "") == group }
            if !members.isEmpty {
                Section(groupHeaderLabel(group)) {
                    ForEach(members) { template in
                        templateRow(template, applied: template.name == appliedName)
                    }
                }
            }
        }
    }

    /// One template row: families, description, and an unmistakable Applied
    /// marker when it's the one live right now. A plain List row inside a
    /// NavigationLink — List supplies the disclosure chevron, so the row no
    /// longer draws its own card background or chevron.
    private func templateRow(_ template: ClusterTemplate, applied: Bool) -> some View {
        NavigationLink(value: template) {
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
                    Text(template.name)
                        .font(.headline)
                        .foregroundColor(Theme.Semantic.onSurface)
                        .multilineTextAlignment(.leading)
                    Spacer(minLength: 0)
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
                        .font(.hsccMono(.caption2))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
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

    private func errorLabel(_ message: String, retry: (() -> Void)? = nil) -> some View { HSErrorLabel(message: message, retry: retry) }
}

/// The unmistakable marker on the currently-applied template row.
private struct AppliedBadge: View {
    // Shared chip (spec §6 inventory). It used to be a filled ok capsule
    // with surface ink; HSStatusChip is the canonical role-tinted chip now.
    var body: some View {
        HSStatusChip("Applied", systemImage: "checkmark.seal.fill",
                     color: Theme.Semantic.ok)
            .accessibilityLabel("Currently applied")
    }
}
