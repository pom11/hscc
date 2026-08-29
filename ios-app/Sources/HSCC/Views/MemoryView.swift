import SwiftUI

/// Memory viewer — the card's iOS surface (see t_e8ffd787).
///
/// Shows what a chosen Hermes profile actually remembers, and lets the operator
/// correct a wrong memory or delete one outright.
///
/// Backed by `GET /v1/memory?profile=<name>` plus the confirm-gated
/// `POST /v1/memory/{node_id}/delete` and `/v1/memory/{node_id}/edit` endpoints
/// (routes_memory.py). Memories are per-profile — exactly like sessions — so
/// the profile is chosen in the field at the top, not guessed.
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
    @State private var editingItem: MemoryItem?

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
        .refreshable { if client != nil { await load(client) } }
        .task {
            if client != nil, list.value == nil, !list.isLoading {
                await load(client)
            }
        }
        .sheet(item: $editingItem) { item in
            if let client {
                MemoryEditSheet(client: client, profile: trimmedProfile, item: item) {
                    await reloadAfterMutation(client)
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
            TextField("hscc-orch", text: $profile)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .textFieldStyle(.roundedBorder)
                .onSubmit { Task { await load(client) } }
            Button {
                Task { await load(client) }
            } label: {
                Label("Load", systemImage: "arrow.clockwise")
            }
            .font(.subheadline)
            Text("The Hermes profile whose memories you want to inspect and correct.")
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

    private func load(_ client: HSCCClient) async {
        let trimmed = trimmedProfile
        guard !trimmed.isEmpty else { return }
        list = .loading
        do {
            list = .loaded(try await client.memories(profile: trimmed))
        } catch {
            list = .failed(errorMessage(for: error))
        }
    }

    private func errorMessage(for error: Error) -> String {
        (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
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
            case .loaded(let state):
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
                    prompt: "Delete the memory \\\"\\(titleDisplay(item))\\\"? "
                        + "This removes it permanently from \\(item.sourceLabel)."
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
                        prompt: "Rewrite this memory in \\(profile) with the corrected text?"
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
