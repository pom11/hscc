import Foundation

// ===========================================================================
// ComposerText — pure, harness-testable text-shaping rules for the chat
// composers.
//
// The VOICE path feeds SYSTEM DICTATION into the composer: the operator taps
// the mic key on the on-screen keyboard (the system dictation affordance —
// preferred over a custom speech pipeline: less code, better accessibility,
// no new permission story beyond the mic, which the keyboard manages itself)
// and the recognized text lands in the TextField's draft.
//
// These pure functions own the text-shaping rules that BOTH typed and dictated
// input pass through, so they are provable in a headless macOS CLI even though
// the actual microphone capture / IPC is device-only and cannot run on this
// host (see scripts/voice_draft_check.sh). Keeping the rules here (not buried
// in a View body) is what makes them testable at all.
// ===========================================================================

enum ComposerText {
    /// The sendable form of a draft: surrounding whitespace trimmed.
    /// Both a typed and a dictated draft go through this before send.
    static func sendable(_ draft: String) -> String {
        draft.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// True when the draft holds nothing sendable (blank or whitespace-only).
    /// Guards the Send button so a dictate+accidental-space can never fire an
    /// empty message.
    static func isEmpty(_ draft: String) -> Bool {
        sendable(draft).isEmpty
    }

    /// Merge dictated/pasted text into the current draft so it reads naturally:
    /// no leftover leading/trailing whitespace from the recognition result and
    /// no double space between a trailing word and the inserted text.
    ///
    /// This is the pure rule behind the composer's Voice button: it focuses the
    /// field (summoning the system keyboard + its dictation key) and, when the
    /// recognized text arrives through the binding, the draft is normalised with
    /// this helper so a multi-sentence dictation across taps stays clean.
    static func inserting(_ incoming: String, into draft: String) -> String {
        let fragment = sendable(incoming)
        guard !fragment.isEmpty else { return draft }
        let base = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !base.isEmpty else { return fragment }
        return base + " " + fragment
    }
}
