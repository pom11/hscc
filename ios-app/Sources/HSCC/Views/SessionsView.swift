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
        .navigationTitle("Sessions")
        .refreshable { if let client { await load(client) } }
        .task {
            if let client, list.value == nil, !list.isLoading {
                await load(client)
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "text.bubble")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to inspect sessions.")
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
            Text("The Hermes profile whose sessions you want to inspect.")
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

    // MARK: - List

    private func listSection(_ client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Sessions", systemImage: "text.bubble")
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
                    sessListBody(client, state)
                }
            case .loaded(let state):
                sessListBody(client, state)
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

    /// The rendered session list for a live or stale-last-known response.
    @ViewBuilder
    private func sessListBody(_ client: HSCCClient, _ state: SessionsListResponse) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(.secondary)
            let items = state.sessions ?? []
            if items.isEmpty {
                emptyLabel("No sessions on this profile.")
            } else {
                ForEach(items) { session in
                    sessionRow(client, session)
                    if session.id != items.last?.id {
                        Divider()
                    }
                }
            }
        }
    }

    private func sessionRow(_ client: HSCCClient, _ session: SessionItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(session.displayTitle)
                    .font(.headline)
                    .lineLimit(1)
                Spacer()
                if session.isBloated {
                    bloatBadge
                }
            }
            HStack(spacing: 12) {
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
        Text("bloated")
            .font(.caption2.bold())
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Theme.Semantic.bad.opacity(0.18)))
            .foregroundColor(Theme.Semantic.bad)
    }

    private func statText(_ value: String, _ label: String) -> some View {
        HStack(spacing: 3) {
            Text(value)
                .font(.caption.monospacedDigit())
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
