import SwiftUI

/// B4 — dispatch a kanban card (POST /v1/cards) behind an explicit confirm UI.
///
/// Presented as a sheet from `CardsView`. The user fills in the board / title /
/// (optional) assignee / body, then taps "Dispatch" — which does NOT send the
/// request. It only arms a `.confirmationDialog` naming exactly what will
/// happen ("Dispatch card 'title' onto board X?"). Only after the user confirms
/// does the request fire, with `"confirm": true` (always sent by
/// `HSCCClient.dispatchCard`).
///
/// Failures are surfaced honestly: a non-2xx (409 confirm refused, 502) makes
/// the client throw, which `MutationButton` presents as a "Failed" alert — a
/// failed dispatch is never shown as a success. In-flight the button disables
/// and shows a spinner so a double-tap can't double-fire.
struct DispatchCardView: View {
    @EnvironmentObject private var settings: SettingsStore
    @Environment(\.dismiss) private var dismiss

    @State private var board = ""
    @State private var title = ""
    @State private var assignee = ""
    @State private var bodyText = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("New card") {
                    TextField("Board", text: $board)
                        .textInputAutocapitalization(.never)
                        .disableAutocorrection(true)
                    TextField("Title", text: $title)
                    TextField("Assignee (optional)", text: $assignee)
                        .textInputAutocapitalization(.never)
                        .disableAutocorrection(true)
                    TextField("Body (optional)", text: $bodyText, axis: .vertical)
                        .lineLimit(3...6)
                }

                Section {
                    // The confirm-gated dispatch. A tap only arms the
                    // confirmation; the request fires only after the user
                    // confirms. The closure captures the current field values
                    // at confirm time. Request logic stays in HSCCClient.
                    MutationButton(
                        title: "Dispatch",
                        systemImage: "paperplane.fill",
                        prompt: prompt,
                        run: {
                            let client = try clientOrThrow()
                            let result = try await client.dispatchCard(
                                board: trimmed(board),
                                title: trimmed(title),
                                assignee: optionalText(assignee),
                                body: optionalText(bodyText)
                            )
                            return result.message
                                ?? "Dispatched card \(result.id ?? "") onto \(trimmed(board))."
                        }
                    )
                    .disabled(!canDispatch)
                } footer: {
                    Text("Dispatch creates a real kanban card. It fires only after you confirm — a tap never sends it directly.")
                }
            }
            .navigationTitle("Dispatch Card")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    /// The confirmation wording — names the exact action.
    private var prompt: String {
        "Dispatch card \"\(trimmed(title))\" onto board \"\(trimmed(board))\"?"
    }

    /// Dispatch is only possible once a board AND a title are filled in.
    private var canDispatch: Bool {
        !trimmed(board).isEmpty && !trimmed(title).isEmpty
    }

    private func trimmed(_ s: String) -> String {
        s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func optionalText(_ s: String) -> String? {
        let t = trimmed(s)
        return t.isEmpty ? nil : t
    }

    /// Build the configured client, or fail with a clear message when settings
    /// aren't set yet. This is an error path, not a silent fallback.
    private func clientOrThrow() throws -> HSCCClient {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else {
            throw HSCCError.invalidURL
        }
        return HSCCClient(host: settings.host, port: port, token: token)
    }
}
