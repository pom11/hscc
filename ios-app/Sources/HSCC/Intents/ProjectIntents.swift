import AppIntents
import AVFoundation

// ---------------------------------------------------------------------------
// Per-project voice intents (the original in-car use case).
//
// Two shortcuts, both project-aware so Siri resolves the project by name:
//   1. `AskOrchestratorIntent`  — "ask hscc about the review queue" routes to
//      that project's orchestrator and, later, SPEAKS the reply.
//   2. `ProjectStatusIntent`    — "how is ecofire-app doing" speaks a short
//      per-project summary.
//
// `HSCCProject` is the AppEnum (Siri resolves the spoken project name to a
// case). It follows `CannedCard`'s exact pattern: a String raw-value AppEnum
// with `caseDisplayRepresentations` for voice-discoverable labels. The raw
// values MUST match the project names in GET /v1/projects (`name`), because
// they're passed straight through to the API route.
//
// Reuses the existing client (`IntentClient` → `HSCCClient`), the existing
// job-based orchestrator chat endpoints (`orchestratorChatStart` /
// `orchestratorChatPoll`), and the existing per-project read
// (`projectDetail`). No second intent style — same patterns as B5.
// ---------------------------------------------------------------------------

// MARK: - HSCCProject AppEnum

/// The 12 projects registered with HSCC (GET /v1/projects), as a voice-
/// discoverable AppEnum so Siri can resolve a spoken project name to a case.
///
/// The raw values MUST stay in sync with the API's project `name` values —
/// they are sent to `/v1/projects/{name}` and `/v1/orchestrator/chat`
/// verbatim. (The app's registry `Project` struct in Models.swift is the
/// live-list type; this enum is the fixed voice vocabulary for Siri.)
enum HSCCProject: String, AppEnum, CaseIterable {
    case hscc
    case ecofireBC = "ecofire-bc"
    case ecofireApp = "ecofire-app"
    case sphoin
    case soconn
    case flosana
    case powerbi
    case efsdriver
    case grid
    case radio
    case pickolo
    case pom

    /// The project's `name` as sent to the API (the raw string value).
    var apiName: String { rawValue }

    /// Human-friendly display name for Siri resolution and copy.
    var displayName: String {
        switch self {
        case .hscc:       return "hscc"
        case .ecofireBC:  return "ecofire BC"
        case .ecofireApp: return "ecofire app"
        case .sphoin:     return "sphoin"
        case .soconn:     return "soconn"
        case .flosana:    return "flosana"
        case .powerbi:    return "powerbi"
        case .efsdriver:  return "efsdriver"
        case .grid:       return "grid"
        case .radio:      return "radio"
        case .pickolo:    return "pickolo"
        case .pom:        return "pom"
        }
    }

    // MARK: - AppEnum conformance (voice-discoverable labels)

    static var typeDisplayRepresentation: TypeDisplayRepresentation {
        "Project"
    }

    static var caseDisplayRepresentations: [HSCCProject: DisplayRepresentation] {
        [
            .hscc:       "hscc",
            .ecofireBC:  "ecofire BC",
            .ecofireApp: "ecofire app",
            .sphoin:     "sphoin",
            .soconn:     "soconn",
            .flosana:    "flosana",
            .powerbi:    "powerbi",
            .efsdriver:  "efsdriver",
            .grid:       "grid",
            .radio:      "radio",
            .pickolo:    "pickolo",
            .pom:        "pom",
        ]
    }

    var displayRepresentation: DisplayRepresentation {
        Self.caseDisplayRepresentations[self] ?? DisplayRepresentation(stringLiteral: rawValue)
    }
}

// MARK: - Shared helpers

/// A tiny helper that speaks a string aloud from a background task using the
/// system speech synthesizer.
///
/// An AppIntent's `perform()` returns promptly so Siri can hand control back;
/// the long orchestrator wait (17 s idle, 165 s+ under load) must NOT hold
/// Siri. So after returning the acknowledgment dialog, we hand the finished
/// answer to `AVSpeechSynthesizer` in a detached task and speak it when the
/// job completes — the wait reads as normal, never as an error.
enum SpeechSpeaker {
    /// Speak `rawText` aloud, first stripping common markdown markers.
    ///
    /// The orchestrator's reply is plain-language prose but can carry markdown
    /// (`**bold**`, backticks, bullet lists, em-dashes). A speech synthesizer
    /// reads those litterally as "asterisk asterisk", which is exactly the
    /// "reading a JSON list" failure the in-car copy rules forbid. We strip the
    /// markdown syntax characters (never the words) so Siri reads clean prose.
    /// Nothing here fabricates content — it only removes formatting.
    static func speak(_ rawText: String) {
        let synthesizer = AVSpeechSynthesizer()
        let utterance = AVSpeechUtterance(string: strippingMarkdown(rawText))
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
        // Retain the synthesizer until it finishes.
        active = synthesizer
    }

    /// Remove the markdown markers that a speech synthesizer would read aloud
    /// literally (`**`, `*`, `` ` ``, `#`, `__`, `_` emphasis). Keeps all
    /// content words, drops formatting only. Bullet chars (`- `, `* ` at line
    /// start) become plain lines so the sentence still hangs together.
    private static func strippingMarkdown(_ text: String) -> String {
        var s = text
        // Bold / italic / inline code / emphasis markers.
        s = s.replacingOccurrences(of: "**", with: "")
        s = s.replacingOccurrences(of: "__", with: "")
        s = s.replacingOccurrences(of: "`", with: "")
        s = s.replacingOccurrences(of: "###", with: "")
        s = s.replacingOccurrences(of: "##", with: "")
        s = s.replacingOccurrences(of: "#", with: "")
        // Bullet/list markers at line start.
        var lines = s.components(separatedBy: "\n")
        lines = lines.map { $0.trimmingCharacters(in: .whitespaces) }
        lines = lines.filter { !$0.isEmpty }
        s = lines.joined(separator: ". ")
        return s
    }

    /// Keep the synthesizer alive across the async boundary (it is released
    /// when the run completes).
    private static var active: AVSpeechSynthesizer?
}

// MARK: - AskOrchestratorIntent

/// Ask a project's orchestrator a question by voice and, later, speak the reply.
///
/// Example: "Hey Siri, ask hscc about the review queue" → routes to the `hscc`
/// orchestrator and speaks its answer.
///
/// DESIGN — job-based, the wait is normal, not an error:
/// A chat takes 17 s idle and 165 s+ under load; Siri will not hold that. So
/// this intent uses the JOB API (`POST /v1/orchestrator/chat` returns a
/// `job_id` immediately), acknowledges out loud ("Asking hscc — I'll have an
/// answer shortly"), returns so Siri hands control back, and a DETACHED task
/// polls `GET /v1/orchestrator/chat/{id}` until it reaches a terminal state.
/// When done, the reply is spoken via `AVSpeechSynthesizer`. The reply (the
/// API's `reply` / `speak` field) is spoken — never raw JSON read aloud.
///
/// SAFETY — this is a MUTATION: the orchestrator can decompose the prompt and
/// dispatch real work onto the project's board. So it keeps an explicit
/// `requestConfirmation` whose spoken message names the real consequence. A
/// voice command never silently starts real work.
///
/// PROMPT IS FREE-TEXT — distinct from the CannedCard rule. That rule forbids
/// free-form dictation of CARD BODIES for dispatch (fixed cards only). Asking
/// an orchestrator a question is inherently free-form and matches the app's
/// `OrchestratorChatView` composer. This intent never dispatches a card body;
/// it only relays a prompt to the orchestrator.
struct AskOrchestratorIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask a project's orchestrator"
    static let description = IntentDescription(
        "Ask a project's orchestrator a question and hear the answer spoken.")
    static let openAppWhenRun: Bool = false

    /// The project whose orchestrator to ask (voice-resolved from the name).
    @Parameter(title: "Project")
    var project: HSCCProject

    /// The question to ask. Siri fills this from the spoken phrase (the
    /// personal-assistant analog of typing in the app's chat composer).
    @Parameter(title: "Prompt")
    var prompt: String

    /// Fixed poll cadence for the background job poll (2 s, same as the app's
    /// chat view).
    private static let pollIntervalNanos: UInt64 = 2_000_000_000

    static var parameterSummary: some ParameterSummary {
        Summary("Ask \(\.$project) \(\.$prompt)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let client = IntentClient.make() else {
            return .result(dialog: IntentDialog(stringLiteral: IntentSettingsMessage.notConfigured))
        }
        // EXPLICIT confirmation before ANY connection — the orchestrator can
        // dispatch real work onto the project's board. Same discipline as
        // DispatchCannedCardIntent. The spoken message names the real
        // consequence, never a vague "go".
        try await requestConfirmation(
            actionName: .send,
            dialog: IntentDialog("Ask the \(project.displayName) orchestrator “\(prompt)”? It may dispatch real work onto the \(project.displayName) board.")
        )
        do {
            // POST returns a job_id immediately (202) — no dead wait.
            let started = try await client.orchestratorChatStart(
                project: project.apiName,
                prompt: prompt
            )
            // Acknowledge out loud NOW and hand control back; poll in a
            // detached task so Siri isn't held for the 17-165 s wait.
            Task.detached {
                await AskOrchestratorIntent.pollAndSpeak(client: client, jobID: started.jobID)
            }
            return .result(dialog: IntentDialog("Asking \(project.displayName) — I'll have an answer shortly."))
        } catch {
            // Non-2xx POST (400 unknown_project/bad_request, 409) throws — never
            // claim a job was started. Honest failure speech.
            let message = (error as? HSCCError)?.localizedDescription
                ?? "The orchestrator could not be asked."
            return .result(dialog: IntentDialog("Couldn't ask \(project.displayName). \(message)"))
        }
    }

    /// Poll the job until a terminal state, then speak the outcome. Runs on a
    /// detached task after `perform()` has already returned the ack.
    private static func pollAndSpeak(client: HSCCClient, jobID: String) async {
        while true {
            do {
                let status = try await client.orchestratorChatPoll(jobID: jobID)
                if status.isTerminal {
                    if status.status == "done" {
                        // Speak the reply verbatim (the server's plain-language
                        // answer) — the `reply` when present, else the `speak`.
                        let text = status.reply ?? status.speak
                            ?? "The orchestrator has answered."
                        SpeechSpeaker.speak(text)
                    } else {
                        // A terminal failure — speak the reason, never a silence.
                        let message = status.error?.speak
                            ?? status.error?.message
                            ?? "The orchestrator did not answer."
                        SpeechSpeaker.speak("That didn't work. \(message)")
                    }
                    return
                }
            } catch {
                // A transient poll failure (brief offline blip) must NOT kill
                // the job — the server keeps working. Fall through and keep
                // polling.
            }
            try? await Task.sleep(nanoseconds: Self.pollIntervalNanos)
        }
    }
}

// MARK: - ProjectStatusIntent

/// Ask how a project is doing by voice — a short spoken summary.
///
/// Example: "Hey Siri, how is ecofire-app doing" → speaks that project's
/// status from GET /v1/projects/{name}.
///
/// READ-ONLY: fetches the per-project detail and speaks the server's own
/// `speak` summary (e.g. "hscc: 2 running, 6 open cards on board hscc")
/// VERBATIM — the server already derived the plain-language one-liner, so the
/// intent never fabricates a number on-device. No confirmation: read-only
/// queries stay frictionless.
struct ProjectStatusIntent: AppIntent {
    static let title: LocalizedStringResource = "Project status"
    static let description = IntentDescription(
        "Reads a project's current status aloud — board counts and running work.")
    static let openAppWhenRun: Bool = false

    @Parameter(title: "Project")
    var project: HSCCProject

    static var parameterSummary: some ParameterSummary {
        Summary("How is \(\.$project) doing")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let client = IntentClient.make() else {
            return .result(dialog: IntentDialog(stringLiteral: IntentSettingsMessage.notConfigured))
        }
        do {
            let detail = try await client.projectDetail(project.apiName)
            // Speak the server-derived summary verbatim — never fabricate.
            return .result(dialog: IntentDialog(stringLiteral: detail.speak))
        } catch {
            let message = (error as? HSCCError)?.localizedDescription
                ?? "Couldn't get the \(project.displayName) status."
            return .result(dialog: IntentDialog(stringLiteral: message))
        }
    }
}
