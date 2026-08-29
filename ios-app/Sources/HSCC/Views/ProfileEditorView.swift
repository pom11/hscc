import SwiftUI

/// Editable, per-project profile editor.
///
/// Backed by `GET/POST /v1/profile/editor/{profile}`. Targets the
/// orchestrator's per-project profile `<project>-orch` — the bot that routes
/// the kanban board for this project. The operator edits the fields the hub
/// routes against: model, toolsets, preloaded skills, description, compression.
///
/// Save is confirm-gated (`MutationButton`): a tap only arms the dialog; the
/// POST with `confirm: true` fires only after the operator confirms. On
/// success the sheet re-reads the profile so the fields reflect what's on disk.
struct ProfileEditorView: View {
    let client: HSCCClient
    /// The orchestrator profile for this project (`<project>-orch`).
    let profile: String

    @Environment(\.dismiss) private var dismiss

    @State private var detail = LoadState<ProfileEditorResponse>.idle
    @State private var model = ""
    @State private var provider = ""
    @State private var profileDescription = ""
    @State private var selectedToolsets: Set<String> = []
    @State private var selectedSkills: Set<String> = []
    @State private var thresholdTokens = 0
    @State private var edited = false
    @State private var materials: ProfileEditorMaterials?

    private var cacheKey: String { "/v1/profile/editor/\(profile)" }

    var body: some View {
        Group {
            switch detail {
            case .idle, .loading:
                HSLoading("Loading profile…")
            case .failed(let message):
                HSError("Couldn't load profile", message: message) {
                    Task { await load() }
                }
            case .stale(let state, let ageMessage):
                editor(state, staleMessage: ageMessage)
            case .loaded(let state):
                editor(state, staleMessage: nil)
            }
        }
        .navigationTitle(profile)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if detail.value == nil, !detail.isLoading { await load() }
        }
        .sheet(item: $materials) { materials in
            MultiSelectSheet(
                title: materials.title,
                options: materials.options,
                selected: materials.selected
            )
        }
    }

    @ViewBuilder
    private func editor(_ state: ProfileEditorResponse, staleMessage: String?) -> some View {
        Form {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }

            Section {
                Text("This is the bot profile for the project — what the orchestrator routes the kanban board against.")
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }

            Section("Model") {
                TextField("model", text: $model)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                if !provider.isEmpty {
                    TextField("provider", text: $provider)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
            }

            Section("Description") {
                TextField("What this project's bot is for", text: $profileDescription, axis: .vertical)
                    .lineLimit(2...4)
            }

            Section {
                Button {
                    materials = ProfileEditorMaterials(
                        title: "Select toolsets",
                        options: state.toolsets_all ?? [],
                        selected: $selectedToolsets
                    )
                } label: {
                    HStack {
                        Label("Toolsets", systemImage: "wrench.and.screwdriver")
                        Spacer()
                        Text("\(selectedToolsets.count)").foregroundColor(Theme.Semantic.onSurfaceMuted)
                        Image(systemName: "chevron.right").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
            } footer: {
                Text("Tool groups this bot can use.")
            }

            Section {
                Button {
                    materials = ProfileEditorMaterials(
                        title: "Select skills",
                        options: state.skills_all ?? [],
                        selected: $selectedSkills
                    )
                } label: {
                    HStack {
                        Label("Preloaded skills", systemImage: "sparkles")
                        Spacer()
                        Text("\(selectedSkills.count)").foregroundColor(Theme.Semantic.onSurfaceMuted)
                        Image(systemName: "chevron.right").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
            } footer: {
                Text("Skills the bot loads at startup.")
            }

            Section {
                Stepper(value: $thresholdTokens, in: 0...200000, step: 4000) {
                    LabeledContent("Compaction threshold") {
                        Text("\(thresholdTokens) tokens")
                            .font(.hsccMono(14))
                    }
                }
            } header: {
                Text("Compression")
            } footer: {
                Text("Above this token budget the session compresses. 0 uses Hermes' default.")
            }

            Section {
                MutationButton(
                    title: "Save profile",
                    systemImage: "square.and.arrow.down",
                    prompt: "Apply these changes to the \(profile) profile on the cluster?"
                ) {
                    try await save()
                }
                .disabled(!edited)
            }
        }
    }

    private func load() async {
        detail = await Offline.load(detail,
                                    cacheKey: cacheKey,
                                    client: client) {
            try await client.profileEditor(profile: profile)
        }
        if let state = detail.value {
            apply(state)
        }
    }

    /// Populate the editable fields from a freshly-read profile.
    private func apply(_ state: ProfileEditorResponse) {
        model = state.model ?? ""
        provider = state.provider ?? ""
        profileDescription = state.description ?? ""
        selectedToolsets = Set(state.toolsets ?? [])
        selectedSkills = Set(state.preload_skills ?? [])
        thresholdTokens = state.compression?.threshold_tokens ?? 0
    }

    /// POST the edited fields (confirm-gated upstream). Returns the success
    /// message the `MutationButton` alert shows.
    private func save() async throws -> String {
        let result = try await client.updateProfile(
            profile,
            model: model.isEmpty ? nil : model,
            provider: provider.isEmpty ? nil : provider,
            toolsets: Array(selectedToolsets).sorted(),
            preloadSkills: Array(selectedSkills).sorted(),
            description: profileDescription,
            compression: ["threshold_tokens": thresholdTokens]
        )
        // Re-read so the sheet reflects exactly what's on disk now.
        await load()
        return result.speak
    }
}

/// The catalog + current selection handed to a multi-select sheet.
struct ProfileEditorMaterials: Identifiable {
    let title: String
    let options: [String]
    let selected: Binding<Set<String>>

    var id: String { title }
}

/// A searchable, checkmark multi-select sheet for toolsets / skills.
struct MultiSelectSheet: View {
    let title: String
    let options: [String]
    @Binding var selected: Set<String>

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var filtered: [String] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return options }
        return options.filter { $0.lowercased().contains(q) }
    }

    var body: some View {
        NavigationStack {
            List {
                ForEach(filtered, id: \.self) { option in
                    Button {
                        if selected.contains(option) {
                            selected.remove(option)
                        } else {
                            selected.insert(option)
                        }
                    } label: {
                        HStack {
                            Text(option).font(.hsccMono(14))
                            Spacer()
                            if selected.contains(option) {
                                Image(systemName: "checkmark")
                                    .foregroundColor(Color.accentColor)
                            }
                        }
                    }
                    .foregroundColor(Theme.Semantic.onSurface)
                }
            }
            .searchable(text: $query, prompt: "Search")
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
