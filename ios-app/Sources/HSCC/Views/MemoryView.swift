import SwiftUI

/// Memory viewer — the card's iOS surface (see t_e8ffd787).
///
/// Shows what a chosen Hermes profile actually remembers, and lets the operator
/// correct a wrong memory or delete one outright.
///
/// Backed by `GET /v1/memory?profile=<name>` plus the confirm-gated
/// `POST /v1/memory/{node_id}/delete` and `/v1/memory/{node_id}/edit` endpoints
/// (routes_memory.py). Memories are per-profile — exactly like sessions — so
/// the profile is chosen in the header, not guessed.
///
/// Design contract mirrored from the API:
///   * List is READ-ONLY (no `confirm`). It reloads on pull-to-refresh, on
///     profile change, and re-fetches after a mutation so the row reflects the
///     post-action state.
///   * `body` is the FULL entry text — the viewer shows everything the agent
///     remembers (this is the raw memory the operator may deem wrong), so it is
///     rendered unabridged, not truncated like a card title.
///   * Delete is destructive and goes through `MutationButton` (tap → confirm
///     dialog naming exactly what happens → only then does the request fire,
///     always sending `confirm: true`).
///   * Correct opens an editing sheet pre-filled with the current body; the
///     sheet holds its OWN MutationButton for the actual write, so the rewrite
///     is confirm-gated too. The value of `editingItem` just decides whether
///     the sheet is shown — nothing is mutated by opening it.
struct MemoryView: View {
    let client: HSCCClient?

    @State private var profile = "hscc-orch"
    @State private var list = LoadState<MemoryListResponse>.idle
    @State private var profileOptions = LoadState<ProfileListResponse>.idle
    @State private var editingItem: MemoryItem?
    @State private var profilePickerShown = false

    var body: some View {
        ScrollView {
            if let client {
                VStack(alignment: .leading, spacing: 16) {
                    profileField(client)
                    listSection(client)
                }
                .padding()
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Memories")
        .refreshable { if let client { await loadAll(client) } }
        .task {
            if let client, list.value == nil, !list.isLoading {
                await loadAll(client)
            }
        }
        .sheet(item: $editingItem) { item in
            if let client {
                MemoryEditSheet(client: client, profile: trimmedProfile, item: item) {
                    await reloadAfterMutation(client)
                }
            }
        }
        .sheet(isPresented: $profilePickerShown) {
            if let client {
                ProfilePickerSheet(options: profileOptions,
                                   selection: $profile) {
                    // A profile was chosen from the picker. Clear any stale
                    // list (the new profile's memories differ) and load fresh —
                    // no separate "Load" tap.
                    list = .idle
                    await load(client)
                }
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "brain")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to inspect memories.")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    // MARK: - Profile picker

    private func profileField(_ client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Profile", systemImage: "person.crop.circle")
                .font(.headline)
            // A tappable control (not a text field) that shows which profile is
            // selected and opens the searchable picker. No Load tap — selecting
            // from the picker loads the profile's memories immediately.
            Button {
                profilePickerShown = true
            } label: {
                HStack {
                    Text(trimmedProfile.isEmpty ? "Choose a profile…" : trimmedProfile)
                        .font(.body)
                        .foregroundColor(trimmedProfile.isEmpty ? .secondary : .primary)
                    Spacer()
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .padding(10)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .fill(Theme.Semantic.surface)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                )
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Profile: \(trimmedProfile.isEmpty ? "none" : trimmedProfile)")
            .accessibilityHint("Opens a searchable list of profiles")
            switch profileOptions {
            case .failed(let message):
                // We couldn't fetch the roster — the profile field degrades to a
                // retry-able error instead of silently showing an empty picker.
                HStack(spacing: 8) {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.bad)
                        .lineLimit(2)
                    Spacer()
                    Button("Retry") {
                        Task { await loadProfiles(client) }
                    }
                    .font(.caption)
                }
            default:
                EmptyView()
            }
            Text("The Hermes profile whose memories you want to inspect and correct. Picker shows every profile the cluster serves.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    // MARK: - Load

    private func loadAll(_ client: HSCCClient) async {
        async let m: Void = load(client)
        async let p: Void = loadProfiles(client)
        _ = await (m, p)
    }

    private func loadProfiles(_ client: HSCCClient) async {
        profileOptions = await Offline.load(profileOptions,
                                            cacheKey: "/v1/profiles/list",
                                            client: client) {
            try await client.profileList()
        }
    }

    private func load(_ client: HSCCClient) async {
        let trimmed = trimmedProfile
        guard !trimmed.isEmpty else { return }
        // Only show the explicit spinner on a first load. On refresh we keep
        // holding the last-known value so a transient failure degrades to
        // `.stale` (last-known, clearly labelled) instead of blanking the screen.
        if list.value == nil {
            list = .loading
        }
        list = await Offline.load(list,
                                  cacheKey: "/v1/memory",
                                  client: client) {
            try await client.memories(profile: trimmed)
        }
    }

    private var trimmedProfile: String {
        profile.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - List

    private func listSection(_ client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Memories", systemImage: "brain")
                .font(.headline)
            switch list {
            case .loading:
                ProgressView()
                    .frame(maxWidth: .infinity, alignment: .leading)
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let ageMessage):
                VStack(alignment: .leading, spacing: 8) {
                    StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load(client) }
                    }
                    memoryBody(client, state)
                }
            case .loaded(let state):
                memoryBody(client, state)
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

    private func memoryBody(_ client: HSCCClient, _ state: MemoryListResponse) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(.secondary)
            let items = state.memories ?? []
            if items.isEmpty {
                emptyLabel("This profile holds no memories.")
            } else {
                ForEach(items) { item in
                    memoryRow(client, item)
                    if item.id != items.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private func memoryRow(_ client: HSCCClient, _ item: MemoryItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(item.title ?? "(untitled)")
                    .font(.headline)
                Spacer()
                sourceBadge(item.sourceLabel)
            }
            // The FULL body — this is the actual memory the operator may deem
            // wrong, so it is never truncated here.
            Text(item.body ?? "")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurface)
                .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 12) {
                // Opening the editor does NOTHING destructive — it only arms
                // `editingItem`, which presents the sheet. The actual rewrite is
                // confirm-gated inside the sheet.
                Button {
                    editingItem = item
                } label: {
                    Label("Correct", systemImage: "pencil")
                }
                MutationButton(
                    title: "Delete",
                    systemImage: "trash",
                    destructive: true,
                    prompt: "Delete the memory \"\(titleDisplay(item))\"? "
                        + "This removes it permanently from \(item.sourceLabel)."
                ) {
                    let r = try await client.deleteMemory(nodeID: item.nodeID,
                                                          profile: trimmedProfile)
                    await reloadAfterMutation(client)
                    return r.speak
                }
                Spacer()
            }
        }
    }

    private func sourceBadge(_ text: String) -> some View {
        Text(text)
            .font(.caption2.bold())
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.accentColor.opacity(0.18)))
            .foregroundColor(Color.accentColor)
    }

    private func titleDisplay(_ item: MemoryItem) -> String {
        item.title ?? "(untitled)"
    }

    private func reloadAfterMutation(_ client: HSCCClient) async {
        await load(client)
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
}

/// The searchable profile picker sheet.
///
/// Shows every profile the cluster serves in a searchable list (not a 40-row
/// wheel) so the operator can find a profile by typing part of its slug. The
/// currently-selected profile is marked with a checkmark. Tapping a row picks
/// it, dismisses the sheet, and triggers the load of that profile's memories —
/// there is no separate "Load" tap.
private struct ProfilePickerSheet: View {
    let options: LoadState<ProfileListResponse>
    @Binding var selection: String
    let onPick: () async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var profiles: [ProfileSummary] {
        guard case .loaded(let state) = options else { return [] }
        return state.profiles ?? []
    }

    private var filtered: [ProfileSummary] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return profiles }
        return profiles.filter {
            $0.name.localizedCaseInsensitiveContains(trimmed)
                || ($0.description ?? "").localizedCaseInsensitiveContains(trimmed)
        }
    }

    var body: some View {
        NavigationStack {
            Group {
                switch options {
                case .loading:
                    ProgressView("Loading profiles…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                case .failed(let message):
                    VStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.system(size: 36))
                            .foregroundColor(Theme.Semantic.bad)
                        Text("Couldn't load profiles")
                            .font(.headline)
                        Text(message)
                            .font(.subheadline)
                            .foregroundColor(.secondary)
                            .multilineTextAlignment(.center)
                        Text("Close and pull to refresh to retry.")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    .padding()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                default:
                    if filtered.isEmpty {
                        ContentUnavailableView.search(text: query)
                    } else {
                        List(filtered) { p in
                            row(p)
                        }
                    }
                }
            }
            .navigationTitle("Choose Profile")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "Search profiles…")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
        .presentationDetents([.large])
    }

    @ViewBuilder
    private func row(_ p: ProfileSummary) -> some View {
        let isSelected = (p.name == selection)
        Button {
            selection = p.name
            dismiss()
            Task { await onPick() }
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(p.name)
                        .font(.body)
                        .foregroundColor(.primary)
                    if let desc = p.description, !desc.isEmpty {
                        Text(desc)
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .lineLimit(1)
                    } else if let model = p.model, !model.isEmpty {
                        Text(model)
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .lineLimit(1)
                    }
                }
                Spacer()
                if isSelected {
                    Image(systemName: "checkmark")
                        .foregroundColor(Color.accentColor)
                        .fontWeight(.semibold)
                }
                if p.is_default == true {
                    Text("default")
                        .font(.caption2.bold())
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.secondary.opacity(0.15)))
                        .foregroundColor(.secondary)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(p.name)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }
}

/// The confirm-gated editor for one memory card.
///
/// Pre-fills the `TextEditor` with the current body so the operator corrects
/// the real text, then rewrites through a `MutationButton` so the write only
/// fires after an explicit confirm that names what's about to change. Save is
/// disabled while the text is unchanged (no-op) or blank (would wipe the
/// entry — that's Delete's job). On success the sheet dismisses and the list
/// reloads so the row reflects the corrected text.
struct MemoryEditSheet: View {
    let client: HSCCClient
    let profile: String
    let item: MemoryItem
    let onSaved: () async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft: String = ""

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 10) {
                Text("Editing a memory in \(item.sourceLabel)")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)

                TextEditor(text: $draft)
                    .font(.body)
                    .scrollContentBackground(.hidden)
                    .padding(8)
                    .frame(maxWidth: .infinity, minHeight: 200, maxHeight: .infinity)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(Theme.Semantic.surface)
                    )

                HStack {
                    Text("Saving is confirm-gated: it rewrites this memory in the profile.")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    Spacer()
                    MutationButton(
                        title: "Save",
                        systemImage: "checkmark",
                        prompt: "Rewrite this memory in \(profile) with the corrected text?"
                    ) {
                        let r = try await client.editMemory(nodeID: item.nodeID,
                                                            profile: profile,
                                                            content: draft)
                        await onSaved()
                        dismiss()
                        return r.speak
                    }
                    .disabled(draft.isEmpty || draft == (item.body ?? ""))
                }
            }
            .padding()
            .navigationTitle("Correct Memory")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .onAppear {
                if draft.isEmpty {
                    draft = item.body ?? ""
                }
            }
        }
        .presentationDetents([.large])
    }
}
