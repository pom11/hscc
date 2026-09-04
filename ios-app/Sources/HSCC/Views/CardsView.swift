import SwiftUI

/// One card's detail (GET /v1/cards/{id}) plus the card actions that have
/// routes in the API.
///
/// Reached from a project's Board section (`ProjectBoardView`). This is the
/// per-card surface under the project-centric IA. Read-only for the card's
/// body/fields; the one mutating action surfaced is UNBLOCK (the recover
/// route) for a blocked card. Comment / block / complete-standalone / edit
/// have no HTTP route in the API yet — they are NOT invented here, they are
/// recorded on t_89f693ac for an API follow-up.
struct CardDetailView: View {
    @EnvironmentObject private var settings: SettingsStore
    let cardID: String

    @State private var card: CardDetailResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    var body: some View {
        Group {
            if let loadError {
                HSError("Couldn't load card", message: loadError.localizedDescription) {
                    Task { await load() }
                }
            } else if let card {
                List {
                    Section {
                        Label(card.speak, systemImage: "text.bubble")
                            .font(.subheadline)
                    }
                    if let body = card.body, !body.isEmpty {
                        Section("Description") {
                            Text(body)
                                .font(.hsccMono(13))
                                .textSelection(.enabled)
                        }
                    }
                    Section("Card") {
                        row("ID", value: card.id)
                        row("Title", value: card.title)
                        row("Status", value: card.status)
                        row("Assignee", value: card.assignee)
                        row("Board", value: card.board)
                    }
                    // Read-only per-card diff (t_cb93feee): the files changed on
                    // the card's branch and their hunks, paged. OPENED for any
                    // card; the diff view shows a clear notice if the card does
                    // not resolve to a reviewable branch (the endpoint 404s).
                    Section {
                        NavigationLink {
                            DiffDetailView(cardID: cardID)
                        } label: {
                            Label("Files & diff", systemImage: "doc.plaintext")
                        }
                    }
                    // Board actions that have routes. Only surface what the API
                    // actually supports — a control that can't act is worse than
                    // none. Today that is UNBLOCK (POST /v1/kanban/blocked/{id}/recover)
                    // for a blocked card. Comment / block / complete-standalone /
                    // edit have no HTTP route yet (recorded on t_89f693ac).
                    if let status = card.status, status.lowercased() == "blocked" {
                        actionsSection(card)
                    }
                }
            } else {
                HSLoading("Loading…")
            }
        }
        .navigationTitle(card?.title ?? "Card")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    @ViewBuilder
    private func row(_ label: String, value: String?) -> some View {
        if let value, !value.isEmpty {
            LabeledContent(label) { Text(value).font(.hsccMono(15)) }
        }
    }

    /// The card-actions section for a BLOCKED card: a confirm-gated Unblock
    /// that uses the existing recover route.
    private func actionsSection(_ card: CardDetailResponse) -> some View {
        Section("Actions") {
            MutationButton(
                title: "Unblock",
                systemImage: "arrow.counterclockwise",
                prompt: "Recover \(card.id) (\"\(card.title ?? card.id)\") to ready so it re-runs? Only unblock a card you're sure is safe to re-run.",
                run: {
                    guard let client = makeClient() else {
                        throw HSCCError.invalidURL
                    }
                    let result = try await client.recoverBlockedCard(card.id)
                    await load()   // refresh detail status
                    return result.message ?? "Recovered \(card.id)."
                }
            )
            Text("Blocks can only be lifted here — blocking a card, commenting, or completing it outright needs a route that isn't in the API yet.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private func load() async {
        guard !isLoading else { return }
        guard let client = makeClient() else {
            loadError = .invalidURL
            return
        }
        isLoading = true
        defer { isLoading = false }
        do {
            card = try await client.cardDetail(cardID)
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }

    /// A configured client from the current settings, or nil when the operator
    /// hasn't set a usable host/port/token yet. Both the detail fetch and the
    /// card actions use this so they share one source of truth.
    private func makeClient() -> HSCCClient? {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return nil }
        return HSCCClient(host: settings.host, port: port, token: token)
    }
}
