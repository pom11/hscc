import SwiftUI

/// Cards — the drawer's cross-board card list (IOS_REDESIGN 2026-10-14 §3.1).
///
/// The drawer's WORK group lists a Cards row, but until the shell cutover
/// there was no id-less route to it (cards were only reachable through a
/// project's board). This is that destination: every open card across every
/// board (GET /v1/cards, the same read a project board filters), grouped by
/// board, each row pushing `CardDetailView` through the one app-wide
/// `ProjectRoute.card` registration.
///
/// Board scoping stays where it belongs — a project's own Board screen still
/// filters to its board (ProjectBoardView). This screen is the fleet-wide view
/// for "what is moving anywhere".
struct CardsRootView: View {
    let client: HSCCClient?

    @State private var cards = LoadState<CardsResponse>.idle

    var body: some View {
        Group {
            // Below the nav bar, inside content (t_4889e978 placement rule).
            ConnectionBanner()
            if let client {
                content(client)
            } else {
                HSConnectGate(systemImage: "square.grid.2x2", verb: "to see cards")
            }
        }
        .navigationTitle("Cards")
        .task {
            if client != nil, cards.value == nil, !cards.isLoading {
                await load()
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await load() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .disabled(cards.isLoading)
                .accessibilityLabel("Refresh cards")
            }
        }
    }

    @ViewBuilder
    private func content(_ client: HSCCClient) -> some View {
        switch cards {
        case .loading, .idle:
            HSLoading("Loading…")
        case .failed(let message):
            HSError("Couldn't load cards", message: message) {
                Task { await load() }
            }
        case .stale(let response, let ageMessage):
            list(response, staleMessage: ageMessage)
        case .loaded(let response):
            list(response, staleMessage: nil)
        }
    }

    @ViewBuilder
    private func list(_ response: CardsResponse, staleMessage: String?) -> some View {
        let open = response.cards.filter { !["done", "merged", "closed"].contains($0.displayStatus) }
        // Group by board, stable order (nil-board cards last, named boards
        // alphabetised) so the list doesn't reshuffle between refreshes.
        let groups = Dictionary(grouping: open) { $0.board ?? "unassigned" }
            .sorted { boardOrder($0.key) == boardOrder($1.key)
                        ? $0.key < $1.key
                        : boardOrder($0.key) < boardOrder($1.key) }

        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            if open.isEmpty {
                Section {
                    HSEmpty("No open cards",
                            message: "Nothing is open on any board right now.",
                            systemImage: "square.grid.2x2")
                }
            }
            ForEach(groups, id: \.key) { board, items in
                Section("\(board) — \(items.count)") {
                    ForEach(items) { card in
                        NavigationLink(value: ProjectRoute.card(id: card.id)) {
                            cardRow(card)
                        }
                    }
                }
            }
        }
        .listStyle(.plain)
        .refreshable { await load() }
    }

    /// Named boards first (alphabetical), the `unassigned` bucket last.
    private func boardOrder(_ board: String) -> Int { board == "unassigned" ? 1 : 0 }

    /// The shared list recipe row: leading status dot + title over a machine
    /// meta line (status · assignee · id), VoiceOver-combined into one element
    /// (spec §5/§9).
    private func cardRow(_ card: Card) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
            HSStatusDot(statusColor(card.displayStatus))
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(card.displayTitle)
                    .foregroundColor(Theme.Semantic.onSurface)
                HSMetaLine([card.displayStatus, card.assignee, card.id])
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return Theme.Semantic.ok
        case "review", "blocked": return Theme.Semantic.warn
        case "done", "merged", "closed": return Theme.Semantic.ok
        case "failed", "failing": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }

    private func load() async {
        guard let client else { return }
        cards = await Offline.load(cards,
                                   cacheKey: EndpointPath.cards,
                                   client: client) {
            try await client.cards()
        }
    }
}
