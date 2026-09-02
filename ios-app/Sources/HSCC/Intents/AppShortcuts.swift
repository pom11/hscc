import AppIntents

/// B5 + per-project — surfaces the voice intents to Siri / Shortcuts with
/// natural invocation phrases so they're reachable hands-free from the car
/// ("Hey Siri, ...").
///
/// Every shortcut here maps to one of the AppIntents above:
///   * "cluster status"  → `ClusterStatusIntent`
///   * "review queue"    → `ReviewQueueIntent`
///   * "pending approvals" → `ApprovalsIntent` (read-only count)
///   * "dispatch a card" → `DispatchCannedCardIntent` (picks a known card)
///   * "ask <project> <question>" → `AskOrchestratorIntent` (job-based, speaks
///     the reply after a normal wait)
///   * "how is <project> doing" → `ProjectStatusIntent` (read-only summary)
///
/// The dispatch + ask-orchestrator shortcuts carry explicit confirmation
/// before they run — a voice tap can never silently start real work. The
/// read-only ones (cluster status, review queue, approvals, project status)
/// need none.
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
            intent: ApprovalsIntent(),
            phrases: [
                "Get \(.applicationName) approvals",
                "\(.applicationName) pending approvals",
                "Are there pending \(.applicationName) approvals",
            ],
            shortTitle: "Pending approvals",
            systemImageName: "checkmark.seal"
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
        AppShortcut(
            intent: AskOrchestratorIntent(),
            // ONE parameter per phrase — App Intents rejects two
            // ("Multiple parameters detected in phrase"). The project is the
            // phrase parameter because it selects WHICH orchestrator answers;
            // Siri then asks for the question via the prompt parameter's
            // requestValueDialog, so nothing is lost.
            phrases: [
                "Ask \(.applicationName) \(\.$project)",
                "Ask \(.applicationName) about \(\.$project)",
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
