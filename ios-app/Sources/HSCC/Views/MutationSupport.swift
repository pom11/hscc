import SwiftUI

/// B4's confirm-gated mutation presenter.
///
/// The whole point of this phase: a tap must NEVER fire a mutating request
/// directly — every mutation (dispatch / merge / template-apply / stop)
/// touches real infrastructure or real git state, so the user has to take a
/// deliberate SECOND step that names exactly what will happen. This helper
/// wraps that contract so every mutating surface behaves identically and there
/// is exactly ONE code path that can trigger a confirm-gated request, and that
/// path always goes through the user's explicit confirm.
///
/// Flow:
///   1. The button's tap ONLY sets `showConfirm = true` — it never sends a
///      request. This is the gate: a mutation cannot fire from a single tap.
///   2. The `.confirmationDialog` names precisely what will happen (`prompt`),
///      with a confirm button (destructive for Stop).
///   3. Only after the user confirms does `perform()` run the caller's
///      `run` closure — which calls an `HSCCClient` mutating method that always
///      sends `"confirm": true`.
///   4. In-flight: the button is disabled and shows a spinner, so a double-tap
///      can't double-fire a mutation.
///   5. The result is surfaced honestly via an alert: success shows the real
///      message; an error shows the real message in a "Failed" alert. A non-2xx
///      makes the client throw, which lands in `.failure` — an error can never
///      render as a success (no green checkmark for a failed merge/apply/stop).
///
/// Request logic stays in `HSCCClient` (B5 reuses it); this view only decides
/// WHEN to call and HOW to present the outcome.
struct MutationButton: View {
    /// The button's short label, e.g. "Merge & Close", "Dispatch", "Stop".
    let title: String
    /// SF Symbol shown beside the label when idle.
    let systemImage: String
    /// Renders the confirm button as destructive (red). Used for Stop.
    var destructive: Bool = false
    /// The confirmation wording naming EXACTLY what will happen, e.g.
    /// "Merge card t_abc into main and close it?" or "Stop container 1b6e77?".
    let prompt: String
    /// The mutating call. It must send `"confirm": true` (HSCCClient methods
    /// always do). Returns the success message to show.
    let run: () async throws -> String

    @State private var showConfirm = false
    @State private var isRunning = false
    @State private var outcome: MutationOutcome?

    var body: some View {
        Button {
            // STEP 1 — only arm the confirmation. No request is sent here.
            showConfirm = true
        } label: {
            HStack(spacing: 6) {
                if isRunning {
                    ProgressView()
                } else {
                    Image(systemName: systemImage)
                }
                Text(title)
            }
        }
        .disabled(isRunning)   // in-flight guard: no double-fire
        .confirmationDialog(prompt, isPresented: $showConfirm, titleVisibility: .visible) {
            // STEP 2 — the deliberate second step.
            Button(destructive ? "Stop" : "Confirm", role: destructive ? .destructive : nil) {
                // STEP 3 — only now does the mutating request actually fire.
                Task { await perform() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(prompt)
        }
        .alert(item: $outcome) { outcome in
            switch outcome {
            case .success(let message):
                return Alert(title: Text("Done"),
                             message: Text(message),
                             dismissButton: .default(Text("OK")))
            case .failure(let message):
                return Alert(title: Text("Failed"),
                             message: Text(message),
                             dismissButton: .default(Text("OK")))
            }
        }
    }

    @MainActor
    private func perform() async {
        isRunning = true
        defer { isRunning = false }
        do {
            let message = try await run()
            outcome = .success(message)
        } catch {
            // A non-2xx (409 confirm refused, 502 failed merge/apply/stop) makes
            // the client throw, so it lands here — surfaced as a FAILURE, never
            // a success. Especially: a failed merge is NOT presented as merged.
            let message = operatorErrorMessage(error)
            outcome = .failure(message)
        }
    }
}

/// The result of a confirm-gated mutation, driving the outcome alert.
enum MutationOutcome: Identifiable {
    case success(String)
    case failure(String)

    var id: String { UUID().uuidString }
}
