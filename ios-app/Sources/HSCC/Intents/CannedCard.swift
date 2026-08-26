import AppIntents

/// B5 — a small FIXED set of pre-defined cards the driver can pick by voice.
///
/// These are deliberately canned: the user selects one of these known cards by
/// name (Siri resolves the enum case from the spoken phrase), and the card's
/// board/title/body are taken from THIS list. The intent NEVER accepts
/// free-form dictated text for a card's title or body — voice dictation of
/// arbitrary content while driving is explicitly out of scope for B5.
///
/// Each case is a safe, reminder-style card that dispatches no risky work —
/// they are the kind of thing you'd create on the road and act on later.
enum CannedCard: String, AppEnum, CaseIterable {
    case dailyStandup
    case fleetReview
    case preDeployCheck

    /// The kanban board each card is dispatched onto. Kept in one place so a
    /// changed board name is a one-line edit, not a hunt.
    private static let board = "hscc"

    /// The card's title — becomes the kanban card title.
    var title: String {
        switch self {
        case .dailyStandup:   return "Daily standup check-in"
        case .fleetReview:    return "Review fleet status"
        case .preDeployCheck: return "Pre-deploy sanity check"
        }
    }

    /// A short, concrete body so the card is actionable when a human reads it.
    var body: String {
        switch self {
        case .dailyStandup:
            return "Filed from the car: run today's standup digest and address anything needing attention."
        case .fleetReview:
            return "Filed from the car: check fleet health, throughput, and daemon streams."
        case .preDeployCheck:
            return "Filed from the car: run pre-deploy checks before the next rollout."
        }
    }

    /// The board + title the client's confirm-gated dispatch uses.
    var boardName: String { Self.board }

    // MARK: - AppEnum conformance (voice-discoverable labels)

    static var typeDisplayRepresentation: TypeDisplayRepresentation {
        "Card"
    }

    static var caseDisplayRepresentations: [CannedCard: DisplayRepresentation] {
        [
            .dailyStandup:   "Daily standup",
            .fleetReview:    "Fleet review",
            .preDeployCheck: "Pre-deploy check",
        ]
    }

    var displayRepresentation: DisplayRepresentation {
        Self.caseDisplayRepresentations[self] ?? DisplayRepresentation(stringLiteral: rawValue)
    }
}

/// B5 — voice-dispatch ONE pre-defined card (never free-form dictation).
///
/// The user says e.g. "dispatch the daily standup card"; Siri resolves the
/// `CannedCard` enum parameter from that phrase. The intent then reuses B4's
/// confirm-gated `HSCCClient.dispatchCard` (which ALWAYS sends `confirm: true`),
/// so the same single mutating code path is used.
///
/// Honesty: if the API returns a non-2xx the client throws and this intent
/// speaks an honest failure — it NEVER claims a card was dispatched when it
/// wasn't. Confirmation is the API/cli-level `confirm: true` (Siri already
/// confirms the action with the user before running an App Intent).
struct DispatchCannedCardIntent: AppIntent {
    static let title: LocalizedStringResource = "Dispatch a card"
    static let description = IntentDescription(
        "Dispatch one of your saved cards (daily standup, fleet review, pre-deploy check).")

    @Parameter(title: "Card")
    var card: CannedCard

    static var parameterSummary: some ParameterSummary {
        Summary("Dispatch \(\.$card)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let client = IntentClient.make() else {
            return .result(dialog: IntentDialog(IntentSettingsMessage.notConfigured))
        }
        // EXPLICIT confirmation before any mutation. Every other mutating
        // surface in the app is fronted by MutationButton's confirmationDialog;
        // this is the voice equivalent. Do NOT rely on Siri implicitly
        // confirming an AppIntent — it does not reliably do so, which would
        // let a stray invocation dispatch a real card with no user assent.
        try await requestConfirmation(
            result: .result(dialog: IntentDialog("Dispatch \(card.title) onto \(card.boardName)?"))
        )
        do {
            // ALWAYS confirm: true — B4's client sends it on every mutation.
            let result = try await client.dispatchCard(
                board: card.boardName,
                title: card.title,
                body: card.body
            )
            // Speak from the REAL response only (the API's message or the id it
            // actually returned) — never a fabricated success.
            let spoken = result.message
                ?? "Dispatched card \(result.id ?? "") onto \(card.boardName)."
            return .result(dialog: IntentDialog(spoken))
        } catch {
            // Non-2xx makes the client throw — say it failed, never claim success.
            let message = (error as? HSCCError)?.localizedDescription
                ?? "The card could not be dispatched."
            return .result(dialog: IntentDialog("Dispatch failed. \(message)"))
        }
    }
}
