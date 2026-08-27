import AppIntents

/// B5 + per-project — surfaces the voice intents to Siri / Shortcuts with
/// natural invocation phrases so they're reachable hands-free from the car
/// ("Hey Siri, ...").
///
/// Every shortcut here maps to one of the AppIntents above:
///   * "cluster status"  → `ClusterStatusIntent`
///   * "review queue"    → `ReviewQueueIntent`
///   * "dispatch a card" → `DispatchCannedCardIntent` (picks a known card)
///   * "ask <project> <question>" → `AskOrchestratorIntent` (job-based, speaks
///     the reply after a normal wait)
///   * "how is <project> doing" → `ProjectStatusIntent` (read-only summary)
///
/// The dispatch + ask-orchestrator shortcuts carry explicit confirmation
/// before they run — a voice tap can never silently start real work. The
/// read-only ones (cluster status, review queue, project status) need none.
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
        AppShortcut(
            intent: AskOrchestratorIntent(),
            phrases: [
                "Ask \(.applicationName) \(\.$project) \(\.$prompt)",
                "Ask \(.applicationName) \(\.$project) about \(\.$prompt)",
            ],
            shortTitle: "Ask an orchestrator",
            systemImageName: "bubble.left.and.bubble.right"
        )
        AppShortcut(
            intent: ProjectStatusIntent(),
            phrases: [
                "How is \(.applicationName) project \(\.$project) doing",
                "Get \(.applicationName) \(\.$project) status",
                "\(.applicationName) \(\.$project) status",
            ],
            shortTitle: "Project status",
            systemImageName: "folder"
        )
    }
}
