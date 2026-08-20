import SwiftUI

/// Phase B3 — kanban card list (GET /v1/cards) with tap-through to a card
/// detail (GET /v1/cards/{id}).
///
/// READ-ONLY: tapping navigates to the detail; nothing here dispatches cards
/// (that's the confirm-gated POST in B4).
struct CardsView: View {
    @EnvironmentObject private var settings: SettingsStore

    @State private var cards: CardsResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            Group {
                if let loadError {
                    errorView(loadError)
                } else if let cards {
                    content(cards)
                } else {
                    ProgressView("Loading…")
                }
            }
            .navigationTitle("Cards")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(isLoading)
                }
            }
            .task { await load() }
        }
    }

    @ViewBuilder
    private func content(_ response: CardsResponse) -> some View {
        List {
            Section {
                Label(response.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }

            if response.cards.isEmpty {
                Section {
                    Text("No cards.")
                        .foregroundColor(.secondary)
                }
            }

            ForEach(response.cards) { card in
                NavigationLink {
                    CardDetailView(cardID: card.id)
                } label: {
                    cardRow(card)
                }
            }
        }
        .refreshable { await load() }
    }

    @ViewBuilder
    private func cardRow(_ card: Card) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle()
                .fill(statusColor(card.displayStatus))
                .frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 3) {
                Text(card.displayTitle)
                HStack(spacing: 6) {
                    if let board = card.board, !board.isEmpty {
                        Text(board).font(.caption).foregroundColor(.secondary)
                    }
                    if let assignee = card.assignee, !assignee.isEmpty {
                        Text(assignee).font(.caption).foregroundColor(.secondary)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func errorView(_ err: HSCCError) -> some View {
        ContentUnavailableView {
            Label("Couldn't load cards", systemImage: "exclamationmark.triangle")
        } description: {
            Text(err.localizedDescription)
        } actions: {
            Button("Try again") { Task { await load() } }
        }
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return .blue
        case "review", "blocked": return .orange
        case "done", "merged", "closed": return .green
        case "failed", "failing": return .red
        default: return .gray
        }
    }

    private func load() async {
        guard !isLoading else { return }
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else {
            loadError = .invalidURL
            return
        }
        isLoading = true
        defer { isLoading = false }
        let client = HSCCClient(host: settings.host, port: port, token: token)
        do {
            cards = try await client.cards()
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }
}

/// Phase B3 — one card's detail (GET /v1/cards/{id}). READ-ONLY.
struct CardDetailView: View {
    @EnvironmentObject private var settings: SettingsStore
    let cardID: String

    @State private var card: CardDetailResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    var body: some View {
        Group {
            if let loadError {
                ContentUnavailableView {
                    Label("Couldn't load card", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(loadError.localizedDescription)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }
            } else if let card {
                List {
                    Section {
                        Label(card.speak, systemImage: "text.bubble")
                            .font(.subheadline)
                    }
                    Section("Card") {
                        row("ID", value: card.id)
                        row("Title", value: card.title)
                        row("Status", value: card.status)
                    }
                }
            } else {
                ProgressView("Loading…")
            }
        }
        .navigationTitle(card?.displayTitle ?? "Card")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    @ViewBuilder
    private func row(_ label: String, value: String?) -> some View {
        if let value, !value.isEmpty {
            LabeledContent(label) { Text(value) }
        }
    }

    private func load() async {
        guard !isLoading else { return }
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else {
            loadError = .invalidURL
            return
        }
        isLoading = true
        defer { isLoading = false }
        let client = HSCCClient(host: settings.host, port: port, token: token)
        do {
            card = try await client.cardDetail(cardID)
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }
}
