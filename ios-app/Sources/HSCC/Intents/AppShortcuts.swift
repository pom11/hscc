import AppIntents

/// B5 + per-project — surfaces the voice intents to Siri / Shortcuts with
/// natural invocation phrases so they're reachable hands-free from the car
/// ("Hey Siri, ...").
///
/// Every shortcut here maps to one of the AppIntents above (t_0f3f70e6: the
/// review-queue, approvals, ask-orchestrator and project-status shortcuts
/// retired with the console screens / intents they voiced):
///   * "cluster status"  → `ClusterStatusIntent`
///   * "dispatch a card" → `DispatchCannedCardIntent` (picks a known card)
///
/// The dispatch shortcut carries explicit confirmation before it runs — a
/// voice tap can never silently start real work. The read-only cluster-status
/// one needs none.
struct AppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: ClusterStatusIntent(),
            phrases: [
                "Get \(.applicationName) cluster status",
                "\(.applicationName) cluster status",
            ],
            shortTitle: "Cluster status",
            systemImageName: "bolt"
        )
        AppShortcut(
            intent: DispatchCannedCardIntent(),
            // The `card` parameter MUST be referenced in a phrase: App Intents
            // requires every non-optional, non-defaulted parameter to appear in
            // at least one phrase, and this intent's `card` has no
            // requestValueDialog for Siri to prompt for. Before this fix the
            // phrase named no card, so Siri had no way to resolve WHICH card to
            // dispatch — an invisible intent-processor failure (the same class
            // as the two-parameter rejection fixed in 9d7903a). Each phrase
            // keeps exactly ONE parameter (`$card`) so the single-parameter-per-
            // phrase rule still holds; `.applicationName` is a built-in, not a
            // parameter.
            phrases: [
                "Dispatch the \(.applicationName) \(\.$card) card",
                "\(.applicationName) dispatch the \(\.$card) card",
            ],
            shortTitle: "Dispatch a card",
            systemImageName: "paperplane"
        )
    }
}
