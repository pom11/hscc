import SwiftUI

/// Board hygiene (C6) — blocked cards (with confirm-gated recover) and stale
/// cards, across every board.
///
/// Two panes behind a segmented picker (a Cluster-hub nested screen):
///   * Blocked — GET /v1/kanban/blocked, listing why each card is blocked
///     with a confirm-gated "Recover" one-card-at-a-time action.
///   * Stale   — GET /v1/kanban/stale, listing non-terminal cards (older_than=0
///     so the operator sees every stale card, matching `hscc kanban stale`).
///
/// Recover is a MUTATION and never bulk — exactly one card, and only after the
/// confirm dialog names the card. It passes through MutationButton so it sends
/// `confirm: true` and surfaces the real message (never a blank success).
struct BoardHygieneView: View {
    let client: HSCCClient?

    enum Pane: String, CaseIterable, Identifiable {
        case blocked, stale
        var id: String { rawValue }
        var label: String {
            switch self {
            case .blocked: return "Blocked"
            case .stale: return "Stale"
            }
        }
    }

    @State private var selected: Pane = .blocked
    @State private var blocked = LoadState<KanbanBlockedResponse>.idle
    @State private var stale = LoadState<KanbanStaleResponse>.idle

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Pane", selection: $selected) {
                    ForEach(Pane.allCases) { pane in
                        Text(pane.label).tag(pane)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal)
                .padding(.vertical, 8)

                if let client {
                    switch selected {
                    case .blocked: blockedPane(client)
                    case .stale: stalePane(client)
                    }
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Hygiene")
        }
    }

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "broom", verb: "to manage boards")
    }

    // MARK: - Blocked pane

    @ViewBuilder
    private func blockedPane(_ client: HSCCClient) -> some View {
        Group {
            switch blocked {
            case .idle:
                HSLoading("Loading…").task { await loadBlocked(client) }
            case .loading:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load blocked cards", message: message) {
                    Task { await loadBlocked(client) }
                }
            case .stale(let response, let ageMessage):
                blockedList(response, client: client, staleMessage: ageMessage)
            case .loaded(let response):
                blockedList(response, client: client, staleMessage: nil)
            }
        }
    }

    @ViewBuilder
    private func blockedList(_ response: KanbanBlockedResponse, client: HSCCClient, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await loadBlocked(client) }
                    }
                }
            }
            Section {
                Label(response.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }
            let tasks = response.tasks ?? []
            if tasks.isEmpty {
                Section {
                    Text("No blocked cards on any board.")
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            ForEach(tasks) { card in
                blockedRow(card, client: client)
            }
            if let errors = response.errors, !errors.isEmpty {
                Section("Errors") {
                    ForEach(errors, id: \.self) { error in
                        Text(error).font(.caption).foregroundColor(Theme.Semantic.bad)
                    }
                }
            }
        }
        .refreshable { await loadBlocked(client) }
    }

    @ViewBuilder
    private func blockedRow(_ card: BlockedCard, client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(card.displayTitle)
                .font(.body)
            HStack(spacing: 6) {
                if let board = card.board, !board.isEmpty {
                    Text(board).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                if let assignee = card.assignee, !assignee.isEmpty {
                    Text(assignee).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                if let age = card.age_days {
                    Text("\(age)d").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            if let kind = card.block_kind, !kind.isEmpty {
                Label(kind, systemImage: "hand.raised.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.warn)
            }
            if let why = card.why, !why.isEmpty {
                Text(why).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            // Recover — confirm-gated, one card at a time. Names the card so
            // the operator knows exactly what will be re-run.
            MutationButton(
                title: "Recover",
                systemImage: "arrow.counterclockwise",
                prompt: "Recover \(card.id) (\"\(card.displayTitle)\") to ready so it re-runs? Only recover a card you're sure is safe to re-run.",
                run: {
                    let result = try await client.recoverBlockedCard(card.id)
                    await loadBlocked(client)
                    return result.message ?? "Recovered \(card.id)."
                }
            )
            .font(.caption)
            .buttonStyle(.borderless)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Stale pane

    @ViewBuilder
    private func stalePane(_ client: HSCCClient) -> some View {
        Group {
            switch stale {
            case .idle:
                HSLoading("Loading…").task { await loadStale(client) }
            case .loading:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load stale cards", message: message) {
                    Task { await loadStale(client) }
                }
            case .stale(let response, let ageMessage):
                staleList(response, client: client, staleMessage: ageMessage)
            case .loaded(let response):
                staleList(response, client: client, staleMessage: nil)
            }
        }
    }

    @ViewBuilder
    private func staleList(_ response: KanbanStaleResponse, client: HSCCClient, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await loadStale(client) }
                    }
                }
            }
            Section {
                Label(response.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }
            let tasks = response.tasks ?? []
            if tasks.isEmpty {
                Section {
                    Text("No stale cards.")
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            ForEach(tasks) { card in
                VStack(alignment: .leading, spacing: 3) {
                    Text(card.displayTitle)
                        .font(.body)
                    HStack(spacing: 6) {
                        if let board = card.board, !board.isEmpty {
                            Text(board).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        if let status = card.status, !status.isEmpty {
                            Text(status).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        if let assignee = card.assignee, !assignee.isEmpty {
                            Text(assignee).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        if let age = card.age_days {
                            Text("\(age)d old").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let errors = response.errors, !errors.isEmpty {
                Section("Errors") {
                    ForEach(errors, id: \.self) { error in
                        Text(error).font(.caption).foregroundColor(Theme.Semantic.bad)
                    }
                }
            }
        }
        .refreshable { await loadStale(client) }
    }

    private func loadBlocked(_ client: HSCCClient) async {
        blocked = await Offline.load(blocked,
                                     cacheKey: "/v1/kanban/blocked",
                                     client: client) {
            try await client.kanbanBlocked()
        }
    }

    private func loadStale(_ client: HSCCClient) async {
        stale = await Offline.load(stale,
                                   cacheKey: "/v1/kanban/stale",
                                   client: client) {
            try await client.kanbanStale(olderThan: 0)
        }
    }
}
