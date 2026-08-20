import AppIntents

/// B5 — spoken review queue, hands-free (the in-car path).
///
/// Calls the existing read for `/v1/review/queue` and speaks the API's own
/// `speak` one-liner (e.g. "3 cards await review." / "Nothing awaiting
/// review.") as the dialog result. The server derives this count and sentence,
/// so the intent never fabricates a number on-device.
///
/// Honesty rules (B5): if the app isn't configured or the request fails, Siri
/// gets a clear spoken message — a crash or silent no-op is never ok.
struct ReviewQueueIntent: AppIntent {
    static let title: LocalizedStringResource = "Review queue"
    static let description = IntentDescription(
        "Reads the number of cards awaiting review aloud.")
    static let openAppWhenRun: Bool = false

    static var parameterSummary: some ParameterSummary {
        Summary("Get the review queue")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let client = IntentClient.make() else {
            return .result(dialog: IntentDialog(IntentSettingsMessage.notConfigured))
        }
        do {
            let queue = try await client.reviewQueue()
            // Speak the server-derived count sentence verbatim.
            return .result(dialog: IntentDialog(queue.speak))
        } catch {
            let message = (error as? HSCCError)?.localizedDescription
                ?? "Couldn't get the review queue."
            return .result(dialog: IntentDialog(message))
        }
    }
}
