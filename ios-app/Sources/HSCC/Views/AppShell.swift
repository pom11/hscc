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
    case fleet, nodesServing, opsHealth
    case autodown, cron, logs
    case memory, templates, sessions, activity
    case search, settings
    /// LEGACY alias, kept deliberately (spec §8 "FleetView · FleetControlView |
    /// drawer · Fleet (+ Control section)", folded into t_2a29a7ab because the
    /// ruling that put model-serving in Fleet also put fleet-wide up/down in
    /// Fleet). `FleetControlView` was retired as a screen: its applied-template
    /// Section and its confirm-gated up/down buttons live in FleetView's List,
    /// and the drawer has no separate row. No view of its own — but any stale
    /// caller that still names `.fleetControl` (rawValue "fleetControl") lands
    /// on Fleet instead of silently failing. New code routes `.fleet`.
    /// (Same pattern as `.selfHeal`, `.boardHygiene`, `.clusterHub`.)
    case fleetControl
    /// LEGACY alias, kept deliberately. `ClusterView` (the topology-only
    /// "Cluster" drawer row) was retired by t_65d791da — its strip and fleet
    /// status line are now the header Section of NodesServingView, and spec §3.1
    /// has no Cluster row while §5/§8 both say the hub is "retired as a screen".
    /// No drawer row and no screen of its own — but the dock's "Cluster" verb,
    /// `ContentView.Tab.cluster` and any stale caller that still names
    /// `.clusterHub` (rawValue "clusterHub") land on the merged screen instead of
    /// silently failing. New code must route `.nodesServing`. (Same pattern as
    /// `.selfHeal` below.)
    case clusterHub
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
        // The retired screen's alias carries the MERGED destination's title
        // ("Fleet" — where its sections now live), exactly as `.clusterHub`
        // carries "Nodes & serving": a stale caller must never land on a nav
        // bar naming a screen that no longer exists.
        case .fleetControl: "Fleet"
        case .clusterHub: "Nodes & serving"
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
        case .nodesServing: "server.rack"
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
    @State private var selection: AppDestination? = AppShell.initialSelection()
    @StateObject private var approvals = ApprovalPoller()

    /// Debug-only capture seam (t_0d5632bc, DebugHarness.swift): a capture
    /// launch opens one named drawer destination directly — that is the
    /// "drive navigation without taps" path, since this device refuses the
    /// only headless dismissal of the first-run alert (proof in
    /// PHASE2_PROGRESS_t_25aed701 "Screenshots"). Unknown values and Release
    /// builds get the normal `.home` default; this NEVER guesses a screen.
    /// (Fixture seeding for the same launch runs in onAppear below.)
    /// Debug-only drawer-open seam (t_5ed5d281): `HSCC_DEBUG_OPEN_DRAWER=1`
    /// attempts the two documented ways to present the sidebar column at
    /// launch (selection nil = documented "sidebar first when no selection";
    /// delayed preferredCompactColumn write). iPhone 16 / iOS 26.3 XCUITest
    /// evidence (DrawerNavigationUITests, 2026-10-15): the compact column
    /// ignores BOTH — selection .home stays on screen and the column write
    /// never lands. The shipping path of this flag is fixed in commit B.
    static func initialSelection() -> AppDestination? {
        #if DEBUG
        if let screen = DebugHarness.requestedScreen() { return screen }
        if DebugHarness.openDrawerAtLaunch { return nil }
        #endif
        return .home
    }

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
            selection = normalized(AppDestination(tab: requested))
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
        case .fleet: FleetView(client: client)
        // §8 merge (t_65d791da): the retired Cluster hub has no view of its own
        // any more; its destination lands on the SAME merged screen as
        // `.nodesServing`, which now carries the topology strip as its header.
        case .clusterHub: NodesServingView(client: client)
        // §8 fold-in (t_2a29a7ab): the retired Fleet Control screen has no view
        // of its own any more; its destination lands on the SAME screen as
        // `.fleet`, whose List now carries the "Applied template" + "Control"
        // sections. Same recipe as `.clusterHub` → NodesServingView.
        case .fleetControl: FleetView(client: client)
        case .nodesServing: NodesServingView(client: client)
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

    /// Map a legacy alias onto the drawer row that actually exists, so a
    /// destination arriving through the ROUTER (`requestedTab`) normalises the
    /// same way one arriving through `go` does — otherwise the selection would sit
    /// on `.clusterHub`, which has no row to highlight, and the drawer would open
    /// with nothing marked.
    private func normalized(_ destination: AppDestination?) -> AppDestination? {
        switch destination {
        case .selfHeal: return .autodown
        case .boardHygiene: return .approvals
        case .clusterHub: return .nodesServing
        case .fleetControl: return .fleet
        default: return destination
        }
    }

    /// Switch the drawer selection. Clearing the shared detail path is the
    /// point: the drawer swaps the ROOT of the stack, and a stale pushed
    /// screen must never sit on top of the newly chosen root (the tab bar
    /// used to own this for free; with one stack it must be explicit).
    /// Deep links are unaffected — they set `requestedTab`/path from the
    /// router and the router writes the path AFTER any selection change
    /// lands (its resolve goes through an await first).
    ///
    /// The §8 merges are applied here (`normalized`), mapped not deleted: a
    /// retired screen's destination resolves to the merged screen it now lives
    /// inside, so the selection identity — and the sidebar row's highlight — is
    /// the real row, never a phantom second one.
    ///   .selfHeal    -> Autodown & self-heal  (t_1a9bd7f8)
    ///   .boardHygiene-> Approvals             (t_31d037ac)
    ///   .clusterHub  -> Nodes & serving       (t_65d791da)
    ///   .fleetControl-> Fleet                 (t_2a29a7ab)
    private func go(_ destination: AppDestination?) {
        selection = normalized(destination)
        router.projectsPath = []
    }

    /// Legacy deep-link / intent tabs map onto drawer destinations.
    private func applyPendingTabRequest() {
        if let requested = router.requestedTab {
            selection = normalized(AppDestination(tab: requested))
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
        #if DEBUG
        // Capture harness (t_0d5632bc): a headless `simctl launch` cannot write
        // the Keychain (OSStatus -34018 errSecMissingEntitlement), so the seeded
        // fixture cluster reads back as "no token" and every optional-client
        // screen would honestly show its connect gate — proving the shell but
        // not the list recipe. Fixture mode therefore builds the client
        // directly, bypassing the Keychain entirely; its URLSession is the
        // fixture protocol (HSCCClient.defaultSession), which forwards NOTHING
        // to the network. Release builds never take this path.
        if DebugHarness.fixturesEnabled {
            return HSCCClient(host: "fixtures.invalid", port: 8788,
                              token: "debug-fixture-token")
        }
        #endif
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

// `FleetViewPlaceholder` (the deliberately-unusable client handed to FleetView
// when the app was unconfigured) was deleted by t_2a29a7ab: FleetView now takes
// an optional client like every other screen, because it now hosts the
// fleet-wide up/down mutations — an unusable-but-present client would render
// those buttons against `unconfigured.invalid`, where a connect gate is the
// honest surface.

#Preview("Shell — home") {
    let center = ProjectUnreadCenter()
    return AppShell()
        .environmentObject(SettingsStore())
        .environmentObject(center)
        .environmentObject(StreamReplyWatcher(unread: center))
        .environmentObject(DeepLinkRouter.shared)
}
