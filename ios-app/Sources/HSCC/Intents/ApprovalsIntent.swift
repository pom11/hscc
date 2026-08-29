import AppIntents

/// Approvals inbox — spoken pending-approval count, hands-free (t_9a5cfc3b).
///
/// Calls the existing `/v1/kanban/blocked` read, classifies the blocked cards
/// with `BlockedCard.isPendingApproval` (the same one-line classification the
/// on-screen Approvals inbox uses — a single source of truth, never two
/// divergent definitions), and speaks the resulting count as the dialog
/// result, e.g. "3 pending approvals." / "No pending approvals."
///
/// Honesty rules (matching ReviewQueueIntent): if the app isn't configured or
/// the request fails, Siri gets a clear spoken message — a crash or silent
/// no-op is never ok.
struct ApprovalsIntent: AppIntent {
    static let title: LocalizedStringResource = "Pending approvals"
    static let description = IntentDescription(
        "Reads the number of pending approvals awaiting a human decision aloud.")
    static let openAppWhenRun: Bool = false

    static var parameterSummary: some ParameterSummary {
        Summary("Get pending approvals")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let client = IntentClient.make() else {
            return .result(dialog: IntentDialog(stringLiteral: IntentSettingsMessage.notConfigured))
        }
        do {
            let blocked = try await client.kanbanBlocked()
            let pending = (blocked.tasks ?? []).filter(\.isPendingApproval).count
            let spoken: String
            if pending == 0 {
                spoken = "No pending approvals."
            } else if pending == 1 {
                spoken = "1 pending approval awaits your decision."
            } else {
                spoken = "\(pending) pending approvals await your decision."
            }
            // Speak a count derived from the same classification the on-screen
            // inbox uses — never a fabricated number.
            return .result(dialog: IntentDialog(stringLiteral: spoken))
        } catch {
            let message = (error as? HSCCError)?.localizedDescription
                ?? "Couldn't get pending approvals."
            return .result(dialog: IntentDialog(stringLiteral: message))
        }
    }
}
