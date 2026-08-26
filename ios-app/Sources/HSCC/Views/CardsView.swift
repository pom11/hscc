import SwiftUI

/// One card's detail (GET /v1/cards/{id}). READ-ONLY.
///
/// Reached from a project's Board section (`ProjectBoardView`). This is the
/// per-card read surface under the project-centric IA; dispatch/merge actions
/// are wired in by the follow-up "project depth" card.
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
