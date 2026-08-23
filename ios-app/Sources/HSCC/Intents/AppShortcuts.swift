import AppIntents

/// B5 — surfaces the voice intents to Siri / Shortcuts with natural invocation
/// phrases so they're reachable hands-free from the car ("Hey Siri, ...").
///
/// Every shortcut here maps to one of the AppIntents above:
///   * "cluster status"  → `ClusterStatusIntent`
///   * "review queue"    → `ReviewQueueIntent`
///   * "dispatch a card" → `DispatchCannedCardIntent` (picks a known card)
///
/// These are non-destructive, natural phrases. The `DispatchCannedCardIntent`
/// still carries Siri's built-in confirmation before it runs — a voice tap can
/// never silently dispatch work.
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
            intent: ReviewQueueIntent(),
            phrases: [
                "Get \(.applicationName) review queue",
                "\(.applicationName) review queue",
            ],
            shortTitle: "Review queue",
            systemImageName: "list.bullet"
        )
        AppShortcut(
            intent: DispatchCannedCardIntent(),
            phrases: [
                "Dispatch a \(.applicationName) card",
                "\(.applicationName) dispatch a card",
            ],
            shortTitle: "Dispatch a card",
            systemImageName: "paperplane"
        )
    }
}
