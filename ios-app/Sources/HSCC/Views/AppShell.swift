import SwiftUI

// MARK: - App shell (IOS_REDESIGN 2026-10-14 §3)
//
// One app-wide `NavigationStack` inside a `NavigationSplitView` drawer replaces
// the old three-tab TabView. On iPhone (compact width) NavigationSplitView
// renders the sidebar as a slide-over drawer with the system's own top-left
// toggle and edge-swipe reveal — requirement 2, without hand-rolling a
// gesture that fights the transcript's scroll.
//
// The home screen alone carries the dock (§3.2); every other screen reaches
// everything through the drawer. Deep links keep working: `DeepLinkRouter.
// projectsPath` stays the detail stack's path, and the legacy
// `ContentView.Tab` targets map onto drawer destinations.

/// Every first-class destination in the drawer. One enum, one stack — this
/// replaces the ten screens that used to wrap their own `NavigationStack`.
enum AppDestination: String, Hashable, Identifiable {
    case home, chats, projects, cards, approvals
    case fleet, clusterHub, fleetControl, nodesServing, opsHealth
    case autodown, cron, logs
    case memory, templates, sessions, activity
    case search, settings
    /// LEGACY alias, kept deliberately (spec §8 merge: SelfHealHistoryView is
    /// now the trailing section of AutodownView). No drawer row and no screen
    /// of its own — but any stale caller that still names `.selfHeal`
    /// (rawValue "selfHeal") lands on the MERGED destination instead of
    /// silently failing. New code must route `.autodown`.
    case selfHeal
    /// LEGACY alias, kept deliberately (spec §8 merge: BoardHygieneView is
    /// merged into Approvals — blocked + stale are sections of that one
    /// List). No drawer row and no screen of its own; any stale caller that
    /// still names `.boardHygiene` (rawValue "boardHygiene") lands on the
    /// MERGED destination (mapped in `AppShell.go`). New code routes
    /// `.approvals`.
    case boardHygiene

    var id: String { rawValue }

    var title: String {
        switch self {
        case .home: "Home"
        case .chats: "Chats"
        case .projects: "Projects"
        case .cards: "Cards"
        case .approvals: "Approvals"
        case .fleet: "Fleet"
        case .clusterHub: "Cluster"
        case .fleetControl: "Fleet control"
        case .nodesServing: "Nodes & serving"
        case .opsHealth: "Ops & health"
        case .autodown: "Autodown & self-heal"
        case .selfHeal: "Autodown & self-heal"   // legacy alias, §3.1 wording
        case .cron: "Cron"
        case .logs: "Logs"
        case .boardHygiene: "Approvals"   // legacy alias — merged destination
        case .memory: "Memory"
        case .templates: "Templates"
        case .sessions: "Sessions"
        case .activity: "Activity feed"
        case .search: "Search"
        case .settings: "Settings"
        }
    }

    var symbol: String {
        switch self {
        case .home: "square.grid.2x2"
        case .projects: "folder"
        case .approvals: "checkmark.circle"
        case .fleet: "chart.bar"
        case .clusterHub: "bolt"
        case .chats: "bubble.left.and.bubble.right"
        case .cards: "square.grid.2x2"
        case .fleetControl: "slider.horizontal.3"
        case .nodesServing: "serverrack"
        case .opsHealth: "heart.text.square"
        case .autodown: "power"
        case .selfHeal: "power"   // legacy alias — same destination, same glyph
        case .cron: "clock"
        case .logs: "doc.text.magnifyingglass"
        case .boardHygiene: "checkmark.circle"   // legacy alias — same destination, same glyph
        case .memory: "brain"
        case .templates: "square.stack.3d.up"
        case .sessions: "person.2"
        case .activity: "waveform.path.ecg"
        case .search: "magnifyingglass"
        case .settings: "gearshape"
        }
    }
}

/// The root shell. Owns drawer selection, the shared detail stack, the
/// approval badge poller, the offline-queue drop banner and every app-level
/// lifecycle hook the old TabView root carried.
struct AppShell: View {
    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var replyWatcher: StreamReplyWatcher
    @EnvironmentObject private var router: DeepLinkRouter
    @State private var selection: AppDestination? = .home
    @StateObject private var approvals = ApprovalPoller()
    /// Queued messages dropped because the user switched clusters mid-queue
    /// (t_42ba90d2) — surfaced here so nothing is silently lost.
    @State private var droppedBanner: [OfflineSendQueue.QueuedMessage]?

    var body: some View {
        NavigationSplitView {
            Sidebar(selection: drawerBinding, approvalCount: approvals.pendingCount)
                .navigationTitle("HSCC")
        } detail: {
            NavigationStack(path: $router.projectsPath) {
                destinationView(selection ?? .home)
                    .navigationDestination(for: AppDestination.self) { destinationView($0) }
                    // THE app-wide deep-link/user destination (t_136762f3).
                    // Hoisted from ProjectsView during the Phase 2 cutover:
                    // with one stack there is exactly one registration site,
                    // so a hscc:// push and a user tap resolve identically
                    // no matter which drawer row was active.
                    .navigationDestination(for: ProjectRoute.self) { route in
                        projectRouteView(route)
                    }
            }
            .id(settings.activeClusterID)   // cluster switch = clean rebuild
        }
        .overlay(alignment: .bottom) { dropBanner }
        .onReceive(OfflineSendQueue.shared.$drainedDueToClusterSwitch) { dropped in
            droppedBanner = dropped
        }
        .onAppear {
            approvals.setClient(makeClient())
            replyWatcher.setClient(makeClient())
            NotificationCoordinator.shared.setClient(makeClient())
            router.setClient(makeClient())
            seedOfflineQueue()
            applyPendingTabRequest()
            // Cold-start Live Activity re-hydration (deinit doesn't run when the
            // process is killed — these sweeps end stale bubbles idempotently).
            LiveActivityManager.sweepLeftoverWakes()
            SessionActivityDriver.sweepLeftoverSessions()
        }
        .onChange(of: settings.connectionIdentity) {
            ConnectionMonitor.shared.reset()
            approvals.setClient(makeClient())
            replyWatcher.setClient(makeClient())
            NotificationCoordinator.shared.setClient(makeClient())
            router.setClient(makeClient())
            seedOfflineQueue()
            OfflineSendQueue.shared.drainDueToClusterSwitch()
        }
        .onOpenURL { url in router.handle(url) }
        .onContinueUserActivity("com.hscc.ios.open") { activity in
            if let url = activity.webpageURL {
                router.handle(url)
            } else if let urlString = activity.userInfo?["hscc_url"] as? String,
                      let url = URL(string: urlString) {
                router.handle(url)
            }
        }
        .onChange(of: router.requestedTab) { _, requested in
            guard let requested else { return }
            selection = AppDestination(tab: requested)
            router.requestedTab = nil
        }
        .alert("Couldn't open that link", isPresented: Binding(
            get: { router.lastError != nil },
            set: { if !$0 { router.lastError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(router.lastError ?? "That link couldn't be opened.")
        }
    }

    /// The single switch from a destination to its screen. Every screen that
    /// existed under the tab bar is here — nothing was dropped (§8 migration
    /// map). `client` stays optional so unconfigured states stay honest.
    @ViewBuilder
    private func destinationView(_ destination: AppDestination) -> some View {
        let client = makeClient()
        switch destination {
        case .home: HomeView(selection: drawerBinding, approvalCount: approvals.pendingCount)
        case .chats: ChatsRootView(client: client)
        case .projects: ProjectsView(client: client)
        case .cards: CardsRootView(client: client)
        case .approvals: ApprovalsView(client: client)
        case .fleet: FleetView(client: client ?? FleetViewPlaceholder.client)
        case .clusterHub: ClusterView(client: client)
        case .fleetControl: FleetControlView(client: client)
        case .nodesServing: ServingControlView(client: client)
        case .opsHealth: OpsView(client: client)
        case .autodown: AutodownView(client: client)
        // §8 merge: the retired screen has no view of its own any more; its
        // destination lands on the SAME merged screen as `.autodown`.
        case .selfHeal: AutodownView(client: client)
        case .cron: CronView(client: client)
        case .logs: LogsView(client: client)
        // §8 merge: same for BoardHygiene — blocked + stale are sections of
        // the Approvals List, so its destination lands on ApprovalsView.
        case .boardHygiene: ApprovalsView(client: client)
        case .memory: MemoryView(client: client)
        case .templates: TemplatesView(client: client)
        case .sessions: SessionsView(client: client)
        case .activity: ActivityFeedView(client: client)
        case .search: SearchView(client: client)
        case .settings: SettingsView()
        }
    }

    /// Render a `ProjectRoute` (deep-link pushes AND the in-app project taps
    /// land here — one registration site, one path). With no client the
    /// destinations show the connect gate: a deep link must never crash or
    /// produce a blank screen, however it arrives.
    @ViewBuilder
    private func projectRouteView(_ route: ProjectRoute) -> some View {
        if let client = makeClient() {
            switch route {
            case .projectDetail(let name, let board):
                ProjectDetailView(client: client, project: Project(name: name, repo: nil, board: board, topic: nil))
            case .chat(let name):
                StreamingChatView(project: name)
            case .board(let name, let board):
                ProjectBoardView(client: client, board: board)
                    .navigationTitle("\(name) · Board")
                    .navigationBarTitleDisplayMode(.inline)
            case .projectSettings(let name):
                ProjectSettingsView(client: client, name: name)
                    .navigationTitle("\(name) · Settings")
                    .navigationBarTitleDisplayMode(.inline)
            case .card(let id):
                CardDetailView(cardID: id)
            }
        } else {
            HSConnectGate(systemImage: "link", verb: "to open this link")
        }
    }

    /// Drawer selection with the stack-consistency rule (see `go`). Both the
    /// Sidebar and Home's hero/tiles/dock drive the root through this.
    private var drawerBinding: Binding<AppDestination?> {
        Binding(
            get: { selection },
            set: { go($0) }
        )
    }

    /// Switch the drawer selection. Clearing the shared detail path is the
    /// point: the drawer swaps the ROOT of the stack, and a stale pushed
    /// screen must never sit on top of the newly chosen root (the tab bar
    /// used to own this for free; with one stack it must be explicit).
    /// Deep links are unaffected — they set `requestedTab`/path from the
    /// router and the router writes the path AFTER any selection change
    /// lands (its resolve goes through an await first).
    private func go(_ destination: AppDestination?) {
        // §8 merges, mapped not deleted: the retired self-heal and board-
        // hygiene screens' destinations resolve to the merged screens, and
        // they do so HERE so the selection identity (and the sidebar row's
        // highlight) is `.autodown` / `.approvals`, never a phantom second row.
        switch destination {
        case .selfHeal: selection = .autodown
        case .boardHygiene: selection = .approvals
        default: selection = destination
        }
        router.projectsPath = []
    }

    /// Legacy deep-link / intent tabs map onto drawer destinations.
    private func applyPendingTabRequest() {
        if let requested = router.requestedTab {
            selection = AppDestination(tab: requested)
            router.requestedTab = nil
        }
    }

    @ViewBuilder
    private var dropBanner: some View {
        if let dropped = droppedBanner, !dropped.isEmpty {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                Label(
                    "\(dropped.count) queued message\(dropped.count == 1 ? "" : "s") not sent — cluster changed",
                    systemImage: "exclamationmark.triangle"
                )
                .font(.subheadline.weight(.semibold))
                Text("Switch back to the previous cluster to send them, or re-send by hand.")
                    .font(.caption)
                Button("Dismiss") {
                    OfflineSendQueue.shared.consumeDrained()
                    droppedBanner = nil
                }
                .font(.subheadline.weight(.semibold))
            }
            .padding(Theme.Spacing.lg.rawValue)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.Semantic.tintOverlay(Theme.Semantic.warn))
            .clipShape(RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous))
            .padding()
        }
    }

    private func makeClient() -> HSCCClient? {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return nil }
        return HSCCClient(host: settings.host, port: port, token: token)
    }

    /// Wire the offline queue's one real delivery path (t_42ba90d2) — identical
    /// to what the TabView root did, moved here unchanged.
    private func seedOfflineQueue() {
        let client = makeClient()
        // NOTE: no `self`/no `weak` — a View struct is not a class, and the
        // handler only needs `client`.
        OfflineSendQueue.shared.sendHandler = { msg in
            guard let client else { return .unreachable }
            do {
                let started = try await client.orchestratorChatStart(project: msg.project, prompt: msg.text)
                ChatStore.persistJobID(started.jobID, for: msg.project)
                return .delivered
            } catch {
                if let e = error as? HSCCError, case .transport = e {
                    return .unreachable
                }
                return .rejected(
                    (error as? HSCCError)?.localizedDescription
                        ?? "The cluster reached but rejected the queued message."
                )
            }
        }
    }
}

extension AppDestination {
    /// Map the legacy deep-link tab targets onto drawer destinations
    /// (`ContentView.Tab` stays the router's public vocabulary).
    init(tab: ContentView.Tab) {
        switch tab {
        case .projects: self = .projects
        case .cluster: self = .clusterHub
        case .settings: self = .settings
        }
    }
}

/// FleetView is the only screen whose client is non-optional (it predates the
/// optional-client convention). Keep that contract without faking a connection:
/// when the app is unconfigured we hand it a deliberately unusable client and
/// it renders its honest error states like every other screen.
enum FleetViewPlaceholder {
    static let client = HSCCClient(host: "unconfigured.invalid", port: 0, token: "")
}

#Preview("Shell — home") {
    let center = ProjectUnreadCenter()
    return AppShell()
        .environmentObject(SettingsStore())
        .environmentObject(center)
        .environmentObject(StreamReplyWatcher(unread: center))
        .environmentObject(DeepLinkRouter.shared)
}
