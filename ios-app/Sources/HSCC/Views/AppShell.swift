import SwiftUI

// MARK: - App shell (IOS_REDESIGN 2026-10-14 §3)
//
// One app-wide `NavigationStack` replaces the old three-tab TabView. The
// drawer (t_5ed5d281) is a slide-over panel the shell owns, NOT the compact
// column of a NavigationSplitView: on iPhone compact (iOS 26.3) the system
// sidebar column is a dead end — no dismiss affordance of any kind once it
// takes the screen, row taps never leave it, and the documented column
// controls are inert (verified RED-first on the iPhone 16 simulator in
// UITests/DrawerNavigationUITests.swift). Explicit `drawerOpen`/`drawerClose`
// buttons replace the system toggle and the (non-existent) swipe-back.
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
    static func initialSelection() -> AppDestination? {
        #if DEBUG
        if let screen = DebugHarness.requestedScreen() { return screen }
        #endif
        return .home
    }

    /// Whether the drawer is on screen (t_5ed5d281). The drawer is NOT the
    /// NavigationSplitView sidebar column any more: on iPhone compact
    /// (iOS 26.3) that column is a dead end — once the system presents it
    /// (toggle/edge-swipe) it exposes ZERO dismiss affordances, row taps do
    /// not switch columns, and both column controls the field brief names
    /// (`preferredCompactColumn`, `columnVisibility`) are inert from the app
    /// side in BOTH directions (RED evidence: DrawerNavigationUITests,
    /// iPhone 16 sim, this card). The shell therefore owns the presentation
    /// outright: a slide-over panel over the one app-wide stack. Any row
    /// activation closes it (`go`), the panel's own close button closes it,
    /// and the `drawerOpen` toolbar button — visible on every screen's nav
    /// bar — opens it. `HSCC_DEBUG_OPEN_DRAWER=1` starts inside the drawer
    /// so the XCUITest walk begins in the operator's exact stuck state;
    /// Release builds only ever see `false`.
    @State private var drawerOpen = AppShell.initialDrawerOpen()

    static func initialDrawerOpen() -> Bool {
        #if DEBUG
        if DebugHarness.openDrawerAtLaunch { return true }
        #endif
        return false
    }

    #if DEBUG
    /// NEGATIVE-TEST seam for the nav-graph walker (t_810ab152): with
    /// `HSCC_DEBUG_NEGATE_BACK=1` the shell's drawer affordances — the
    /// `drawerOpen` toolbar button and the `drawerClose` button — are not
    /// rendered. A walker that cannot go RED when the way back disappears is
    /// not a gate; `nav_graph_check.sh` runs the walk once under this flag and
    /// REQUIRES it to fail naming the screen. Debug-only like every other
    /// harness seam (the Release build reads the literal `false` below and
    /// the affordances are unconditionally present).
    private var negateBackForTest: Bool {
        ProcessInfo.processInfo.environment["HSCC_DEBUG_NEGATE_BACK"] == "1"
    }
    #else
    private var negateBackForTest: Bool { false }
    #endif

    /// The app-owned drawer entry (t_5ed5d281), split out only so the
    /// negative-test seam above can omit it in one place.
    private var drawerOpenButton: some View {
        Button {
            openDrawer()
        } label: {
            Image(systemName: "sidebar.left")
        }
        .accessibilityLabel("Menu")
        .accessibilityIdentifier("drawerOpen")
    }

    private func openDrawer() {
        withAnimation(.easeInOut(duration: 0.2)) { drawerOpen = true }
    }

    private func closeDrawer() {
        withAnimation(.easeInOut(duration: 0.2)) { drawerOpen = false }
    }

    /// Queued messages dropped because the user switched clusters mid-queue
    /// (t_42ba90d2) — surfaced here so nothing is silently lost.
    @State private var droppedBanner: [OfflineSendQueue.QueuedMessage]?

    var body: some View {
        ZStack(alignment: .leading) {
            #if DEBUG
            // Live served-count probe for the walker's pull-to-refresh
            // receipt (DebugHarness.ptrProbeFeed). Off the visible canvas
            // (the offset pushes it below the safe area, off-screen) but IN
            // the AX tree — the XCUITest runner reads it with a label query
            // and never taps it. Renders on fixture launches that opt in;
            // nothing else ever sees it.
            if let feed = DebugHarness.ptrProbeFeed, DebugHarness.fixturesEnabled {
                PtrProbeView(feed: feed)
                    .accessibilityIdentifier("ptrProbe")
                    .offset(y: 1200)
                    .allowsHitTesting(false)
            }
            #endif
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
                    .toolbar {
                        // THE drawer entry (t_5ed5d281): an explicit, app-owned
                        // affordance on every screen — the system's compact
                        // split-view toggle was the only opener before, and it
                        // vanishes the moment the drawer takes the screen.
                        // Trailing on purpose: HomeView already owns the
                        // leading spot (its search button); the drawer entry
                        // sits opposite it, where no screen's own leading
                        // item can crowd it out.
                        ToolbarItem(placement: .topBarTrailing) {
                            if !negateBackForTest {
                                drawerOpenButton
                            }
                        }
                    }
            }
            .id(settings.activeClusterID)   // cluster switch = clean rebuild
            // The drawer is a modal surface: while it is up, the screen
            // behind it is not part of the accessibility tree (same rule a
            // sheet follows — and it keeps row labels like "Home" unique to
            // the drawer for tests and VoiceOver alike).
            .accessibilityHidden(drawerOpen)

            if drawerOpen {
                drawerOverlay
            }
        }
        .overlay(alignment: .bottom) { dropBanner }
        .onReceive(OfflineSendQueue.shared.$drainedDueToClusterSwitch) { dropped in
            droppedBanner = dropped
        }
        .onAppear {
            // t_810ab152: quiet-poller fixture launches (the walker's PTR
            // pass) skip the two BADGE pollers — ApprovalPoller's 60 s
            // kanban/blocked and StreamReplyWatcher's 30 s projects/session
            // polls bump the same served counters the walker reads as
            // pull-to-refresh receipts, and a timer increment is not proof
            // the gesture fired. Purely informational badges; zero navigation
            // change; default-off so capture_screens.sh is untouched.
            #if DEBUG
            if !DebugHarness.quietPollers {
                approvals.setClient(makeClient())
                replyWatcher.setClient(makeClient())
                NotificationCoordinator.shared.setClient(makeClient())
            }
            #else
            approvals.setClient(makeClient())
            replyWatcher.setClient(makeClient())
            NotificationCoordinator.shared.setClient(makeClient())
            #endif
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
            // A deep link/tab request must actually LAND on its destination —
            // if the drawer happens to be open it must not stay over it
            // (t_5ed5d281: the drawer is now an app-owned surface, the
            // system column hid this case behind the dead end).
            closeDrawer()
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

    /// The drawer itself (t_5ed5d281): a slide-over panel the shell presents
    /// and dismisses through its own buttons — the one presentation on iPhone
    /// compact that provably leaves (RED evidence in the header comment).
    /// The dim scrim closes it too (tap-outside, the gesture users expect).
    private var drawerOverlay: some View {
        ZStack(alignment: .leading) {
            Button {
                closeDrawer()
            } label: {
                Rectangle().fill(.ultraThinMaterial)
                    .ignoresSafeArea()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Close menu")
            .accessibilityIdentifier("drawerScrim")

            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("HSCC")
                        .font(.largeTitle.bold())
                        .foregroundColor(Theme.Semantic.onSurface)
                    Spacer()
                    // The negative-test seam (t_810ab152) omits the close too:
                    // under HSCC_DEBUG_NEGATE_BACK=1 the drawer is a real trap,
                    // which is what the walker's negative run must catch.
                    if !negateBackForTest {
                        Button {
                            closeDrawer()
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.title2)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .frame(minWidth: 44, minHeight: 44)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Close menu")
                        .accessibilityIdentifier("drawerClose")
                    }
                }
                .padding(.horizontal, Theme.Spacing.lg.rawValue)
                .padding(.top, Theme.Spacing.md.rawValue)
                .padding(.bottom, Theme.Spacing.xs.rawValue)

                Sidebar(selection: drawerBinding, approvalCount: approvals.pendingCount)
            }
            .frame(maxWidth: 320)
            .frame(maxHeight: .infinity)
            .background(Theme.Semantic.surfaceRaised)
            .transition(.move(edge: .leading))
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
        // THE dead-end fix (t_5ed5d281): any row activation leaves the drawer
        // — ALWAYS, even when the selection value is unchanged (Home re-tap
        // while Home is current, the operator's stuck case).
        closeDrawer()
    }

    /// Legacy deep-link / intent tabs map onto drawer destinations.
    private func applyPendingTabRequest() {
        if let requested = router.requestedTab {
            selection = normalized(AppDestination(tab: requested))
            closeDrawer()   // same rule as the requestedTab onChange (t_5ed5d281)
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

#if DEBUG
/// The AX probe AppShell renders for the walker (see AppShell.body): polls
/// the fixture protocol's served counter for `feed` twice a second and
/// publishes "ptr:<feed>:<count>" as its AX label. DEBUG-compiled only.
struct PtrProbeView: View {
    let feed: String
    @State private var count = 0

    var body: some View {
        Text("ptr:\(feed):\(count)")
            .font(.system(size: 1))
            .foregroundColor(.clear)
            .task {
                while !Task.isCancelled {
                    count = FixtureURLProtocol.servedCount(forPath: feed)
                    try? await Task.sleep(nanoseconds: 500_000_000)
                }
            }
    }
}
#endif

#Preview("Shell — home") {
    let center = ProjectUnreadCenter()
    return AppShell()
        .environmentObject(SettingsStore())
        .environmentObject(center)
        .environmentObject(StreamReplyWatcher(unread: center))
        .environmentObject(DeepLinkRouter.shared)
}
