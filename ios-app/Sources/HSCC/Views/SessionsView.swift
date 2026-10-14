import SwiftUI

/// Sessions manager — the card's iOS surface (list a profile's sessions with
/// message count, token totals and compaction headroom; retire or compact a
/// bloated one).
///
/// Backed by `GET /v1/sessions?profile=<name>` plus the confirm-gated
/// `POST /v1/sessions/{id}/retire` and `/v1/sessions/{id}/compact` endpoints
/// (routes_sessions.py). The profile whose state.db to inspect is chosen by
/// the operator in the field at the top — sessions are per-profile, and a
/// single honest edit box beats guessing at which profile the operator means.
///
/// LIST RECIPE (spec §5, Phase 2 C3): plain List, one row per session —
/// leading status dot, title over the mono machine line, trailing
/// `HSStatusChip`. The old hand-rolled `surfaceRaised` boxes (the app's
/// second row dialect) are gone; the profile field rides as the list's
/// first section.
///
/// Design contract mirrored from the API:
///   * List is READ-ONLY (no `confirm`). It reloads on pull-to-refresh and on
///     profile change, and re-fetches after a mutation so the row reflects the
///     post-action state.
///   * A session is never called "bloated" just for being large — `bloated`
///     comes from the SAME positive-evidence verdict the guard uses, so the
///     app and the guard never disagree. Only bloated rows offer actions.
///   * Every mutation goes through `MutationButton` (tap → confirm dialog that
///     names exactly what happens → only then does the request fire, always
///     sending `confirm: true`). Retire is destructive (red); Compact is not.
struct SessionsView: View {
    let client: HSCCClient?

    @State private var profile = "hscc-orch"
    @State private var list = LoadState<SessionsListResponse>.idle

    var body: some View {
        Group {
            if let client {
                switch list {
                case .idle, .loading:
                    HSLoading("Loading…")
                case .failed(let message):
                    HSError("Couldn't load sessions", message: message) {
                        Task { await load(client) }
                    }
                case .stale(let state, let ageMessage):
                    sessionsList(state, staleMessage: ageMessage, client: client)
                case .loaded(let state):
                    sessionsList(state, staleMessage: nil, client: client)
                }
            } else {
                HSConnectGate(systemImage: "text.bubble", verb: "to inspect sessions")
            }
        }
        .navigationTitle("Sessions")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { if let client { await load(client) } }
        .task {
            if let client, list.value == nil, !list.isLoading {
                await load(client)
            }
        }
    }

    // MARK: - Load

    private func load(_ client: HSCCClient) async {
        let trimmed = profile.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        // Only show the explicit spinner on a first load (no value to fall back
        // to). On a refresh/reload we keep holding the last-known value so a
        // transient failure can degrade to `.stale` instead of blanking the
        // screen — the offline-robustness contract (see `Offline.load`).
        if list.value == nil {
            list = .loading
        }
        list = await Offline.load(list,
                                  cacheKey: EndpointPath.sessions,
                                  client: client) {
            try await client.sessions(profile: trimmed)
        }
    }

    // MARK: - List (the shared recipe)

    private func sessionsList(_ state: SessionsListResponse,
                              staleMessage: String?,
                              client: HSCCClient) -> some View {
        let items = state.sessions ?? []
        return List {
            profileSection(client)
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage,
                                reason: "Can't reach the cluster right now.") {
                        Task { await load(client) }
                    }
                }
            }
            if items.isEmpty {
                Section {
                    HSEmpty("Nothing here",
                            message: "No sessions on this profile.",
                            systemImage: "tray")
                }
            } else {
                if !state.speak.isEmpty {
                    Section {
                        Text(state.speak)
                            .font(.footnote.italic())
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                Section("Sessions") {
                    ForEach(items) { session in
                        sessionRow(client, session)
                    }
                }
            }
        }
        .listStyle(.plain)
    }

    /// The profile picker as the list's first section (recipe: no boxed
    /// field floating above the list).
    private func profileSection(_ client: HSCCClient) -> some View {
        Section {
            TextField("hscc-orch", text: $profile)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .onSubmit { Task { await load(client) } }
            Button {
                Task { await load(client) }
            } label: {
                Label("Load", systemImage: "arrow.clockwise")
            }
            .font(.subheadline)
            Text("The Hermes profile whose sessions you want to inspect.")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        } header: {
            Text("Profile")
        }
    }

    private func sessionRow(_ client: HSCCClient, _ session: SessionItem) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
                HSStatusDot(session.isBloated ? Theme.Semantic.bad : Theme.Semantic.ok)
                Text(session.displayTitle)
                    .font(.headline)
                    .lineLimit(1)
                Spacer()
                if session.isBloated {
                    bloatBadge
                }
            }
            // Machine stats line — the recipe's mono caption row.
            HStack(spacing: Theme.Spacing.md.rawValue) {
                statText("\(session.message_count ?? 0)", "msgs")
                statText(session.tokenSummary, "tokens")
                if let headroom = session.compaction_headroom {
                    statText(formatCount(headroom), "headroom")
                }
                Spacer()
            }
            if let reason = session.reason, session.isBloated {
                Text(reason)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.bad)
            }
            if session.isBloated {
                HStack(spacing: 10) {
                    MutationButton(
                        title: "Compact",
                        systemImage: "arrow.clockwise",
                        prompt: "Re-arm native compaction on \"\(session.displayTitle)\"? "
                            + "It keeps the session and shrinks it on its next turn."
                    ) {
                        let r = try await client.compactSession(id: session.id, profile: trimmedProfile)
                        await reloadAfterMutation(client)
                        return r.speak
                    }
                    MutationButton(
                        title: "Retire",
                        systemImage: "tray.and.arrow.down",
                        destructive: true,
                        prompt: "Retire \"\(session.displayTitle)\"? Its history stays on disk "
                            + "but it leaves the live list."
                    ) {
                        let r = try await client.retireSession(id: session.id, profile: trimmedProfile)
                        await reloadAfterMutation(client)
                        return r.speak
                    }
                    Spacer()
                }
            } else {
                // A healthy session still gets a Compact affordance so the
                // operator can recover BEFORE it bloats — but never Retire,
                // which is for a truly bloated last-resort recovery.
                MutationButton(
                    title: "Compact",
                    systemImage: "arrow.clockwise",
                    prompt: "Proactively re-arm native compaction on \"\(session.displayTitle)\"? "
                        + "It keeps the session and re-ensures its compaction headroom."
                ) {
                    let r = try await client.compactSession(id: session.id, profile: trimmedProfile)
                    await reloadAfterMutation(client)
                    return r.speak
                }
            }
        }
    }

    private var trimmedProfile: String {
        profile.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var bloatBadge: some View {
        // Shared chip (spec §6: one of the six bespoke badges it replaces).
        HSStatusChip("bloated", color: Theme.Semantic.bad)
    }

    private func statText(_ value: String, _ label: String) -> some View {
        HStack(spacing: 3) {
            Text(value)
                .font(.hsccMono(.caption))
                .foregroundColor(Theme.Semantic.onSurface)
            Text(label)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private func formatCount(_ n: Int) -> String {
        if n >= 1000 { return String(format: "%.1fk", Double(n) / 1000) }
        return "\(n)"
    }

    private func reloadAfterMutation(_ client: HSCCClient) async {
        await load(client)
    }
}
