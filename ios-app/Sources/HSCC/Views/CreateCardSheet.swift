import SwiftUI

/// "New card" sheet — create a kanban card on a specific board.
///
/// Backed by the EXISTING `POST /v1/cards` route (confirm-gated). This is the
/// Board's first write surface: the operator can direct work from their phone
/// by dispatching a new card with a title, a description (the flightdeck card
/// `body`), and an assignee picked from the real profile roster
/// (`GET /v1/profiles/list`).
///
/// Follows B4's mutation contract exactly (see `MutationSupport.swift`):
///   1. The "Create card" tap only ARMS a confirmation dialog that names the
///      card being dispatched — it never fires a request.
///   2. Only after the operator confirms does `perform()` call
///      `client.dispatchCard`, which always sends `"confirm": true`.
///   3. In-flight: the button is disabled and shows a spinner (no double-fire).
///   4. The result is surfaced honestly: success briefs the operator with the
///      real created-card message and closes the sheet; a non-2xx throws and
///      lands in a "Failed" alert with the real message — an error never
///      renders as a success.
///   5. The board refreshes on success (`onCreated`), so the new card appears.
struct CreateCardSheet: View {
    let client: HSCCClient
    let board: String?

    @Environment(\.dismiss) private var dismiss

    @State private var title = ""
    @State private var bodyText = ""
    @State private var assignee: String = ""
    @State private var profiles = LoadState<ProfileListResponse>.idle

    @State private var showConfirm = false
    @State private var isRunning = false
    @State private var outcome: MutationOutcome?

    /// Called after a card is created so the presenting board can refresh.
    let onCreated: () -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("Card") {
                    TextField("Title", text: $title)
                    TextField("Description", text: $bodyText, axis: .vertical)
                        .lineLimit(4...8)
                }

                Section {
                    Button {
                        showAssigneePicker = true
                    } label: {
                        HStack {
                            Label("Assignee", systemImage: "person")
                            Spacer()
                            if assignee.isEmpty {
                                Text("None").foregroundColor(Theme.Semantic.onSurfaceMuted)
                            } else {
                                Text(assignee).foregroundColor(Theme.Semantic.onSurface)
                            }
                            Image(systemName: "chevron.right").font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                    .sheet(isPresented: $showAssigneePicker) {
                        ProfilePickerSheet(options: profiles, selection: $assignee, onPick: {})
                    }
                } footer: {
                    Text("The profile that will be assigned this card. Leave unset to create it unassigned.")
                }
            }
            .navigationTitle("New Card")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .disabled(isRunning)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button {
                        // STEP 1 — only arm the confirmation. No request fires.
                        showConfirm = true
                    } label: {
                        HStack(spacing: 6) {
                            if isRunning { ProgressView() }
                            Text("Create")
                        }
                    }
                    .disabled(!canSubmit || isRunning)
                }
            }
        }
        .presentationDetents([.large])
        .task { await loadProfiles() }
        .confirmationDialog(
            "Dispatch card to \(board ?? "this board")?",
            isPresented: $showConfirm,
            titleVisibility: .visible
        ) {
            Button("Create") { Task { await perform() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Create \"\(trimmedTitle)\"\(assigneeLabel) on \(board ?? "this board")?")
        }
        .alert(item: $outcome) { outcome in
            switch outcome {
            case .success(let message):
                return Alert(
                    title: Text("Card created"),
                    message: Text(message),
                    dismissButton: .default(Text("OK")) { dismiss() }
                )
            case .failure(let message):
                return Alert(title: Text("Failed"),
                             message: Text(message),
                             dismissButton: .default(Text("OK")))
            }
        }
    }

    @State private var showAssigneePicker = false

    private var trimmedTitle: String {
        title.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var trimmedBody: String {
        bodyText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var assigneeLabel: String {
        assignee.isEmpty ? "" : " assigned to \(assignee)"
    }

    private var canSubmit: Bool {
        !isRunning && !trimmedTitle.isEmpty
    }

    private func loadProfiles() async {
        guard profiles.value == nil, !profiles.isLoading else { return }
        profiles = await Offline.load(profiles,
                                      cacheKey: "/v1/profiles/list",
                                      client: client) {
            try await client.profileList()
        }
    }

    @MainActor
    private func perform() async {
        guard canSubmit else { return }
        isRunning = true
        defer { isRunning = false }
        do {
            let result = try await client.dispatchCard(
                board: board ?? "",
                title: trimmedTitle,
                assignee: assignee.isEmpty ? nil : assignee,
                body: trimmedBody.isEmpty ? nil : trimmedBody
            )
            onCreated()
            outcome = .success(result.message ?? "Created card \(result.id ?? "?")")
        } catch {
            outcome = .failure(operatorErrorMessage(error))
        }
    }
}

// MARK: - Relocated from MemoryView.swift (t_0f3f70e6, console retirement)
//
// The memory viewer screen is deleted with the console; this picker stays
// because the card-creation assignee field above is its last consumer.
//
/// The searchable profile picker sheet.
///
/// Shows every profile the cluster serves in a searchable list (not a 40-row
/// wheel) so the operator can find a profile by typing part of its slug. The
/// currently-selected profile is marked with a checkmark. Tapping a row picks
/// it, dismisses the sheet, and triggers the load of that profile's memories —
/// there is no separate "Load" tap.
///
/// Reused by the Memory picker and the CreateCardSheet assignee field, so it
/// is `internal` (not private) to the module.
struct ProfilePickerSheet: View {
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
                    HSLoading("Loading profiles…")
                case .failed(let message):
                    VStack(spacing: Theme.Spacing.md.rawValue) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .font(.system(size: 36))
                            .foregroundColor(Theme.Semantic.bad)
                        Text("Couldn't load profiles")
                            .font(.headline)
                        Text(message)
                            .font(.subheadline)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .multilineTextAlignment(.center)
                        Text("Close and pull to refresh to retry.")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
            HStack(spacing: Theme.Spacing.md.rawValue) {
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text(p.name)
                        .font(.body)
                        .foregroundColor(Theme.Semantic.onSurface)
                    if let desc = p.description, !desc.isEmpty {
                        Text(desc)
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                    } else if let model = p.model, !model.isEmpty {
                        Text(model)
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                    }
                }
                Spacer()
                if isSelected {
                    Image(systemName: "checkmark")
                        .foregroundColor(Theme.Semantic.accent)
                        .fontWeight(.semibold)
                }
                if p.is_default == true {
                    Text("default")
                        .font(.caption2.bold())
                        .padding(.horizontal, Theme.Spacing.sm.rawValue)
                        .padding(.vertical, Theme.Spacing.xxs.rawValue)
                        .background(Capsule().fill(Theme.Semantic.onSurface.opacity(0.15)))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(p.name)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }
}
