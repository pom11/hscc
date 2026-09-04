import SwiftUI

/// Root view: three tabs, project-centric IA.
///
/// The new information architecture is project-first:
///   1. **Projects** (primary) — the dozen projects from /v1/projects. Tapping
///      a project opens a detail screen with segmented sections
///      (Overview · Chat · Board · Settings). Content repeats across tabs have
///      been folded away: kanban/board content lives under the project it
///      belongs to; fleet-level content lives in one Cluster tab.
///   2. **Cluster** — everything fleet-level in ONE place: the node topology
///      strip, /v1/verify, nodes, fleet stats/throughput/streams, autodown
///      control, and templates. No fleet-related content lives outside this tab.
///   3. **Settings** — app connection only (host, port, token, test
///      connection). The old duplicate nested Settings entry point is gone.
///
/// The design system (Theme.swift) supplies the palette via dynamic semantic
/// colors that adapt to light/dark automatically.
struct ContentView: View {
    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var replyWatcher: StreamReplyWatcher
    @EnvironmentObject private var router: DeepLinkRouter
    @State private var selectedTab: Tab = .projects
    @StateObject private var approvals = ApprovalPoller()
    /// Queued messages dropped because the user switched clusters mid-queue
    /// (t_42ba90d2) — surfaced here so nothing is silently lost.
    @State private var droppedBanner: [OfflineSendQueue.QueuedMessage]?

    enum Tab: Hashable {
        case projects, cluster, settings
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            // Primary tab — the dozen projects the operator actually cares about.
            // `router.projectsPath` is bound as this stack's navigation path so
            // a deep link can push directly into a project / card (t_136762f3).
            ProjectsView(client: makeClient(), path: $router.projectsPath)
                .tabItem { Label("Projects", systemImage: "folder") }
                .tag(Tab.projects)

            // Fleet hub — the node topology strip + every fleet-level surface.
            // The tab badge shows the pending-approval count at a glance
            // (approvals inbox, t_9a5cfc3b) — the operator's "is something
            // needing me right now?" signal.
            ClusterView(client: makeClient(), approvalCount: approvals.pendingCount)
                .tabItem { Label("Cluster", systemImage: "bolt") }
                .badge(approvals.pendingCount.flatMap { $0 > 0 ? String($0) : nil })
                .tag(Tab.cluster)

            // App connection ONLY. The nested duplicate entry point is removed.
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(Tab.settings)
        }
        .overlay(alignment: .bottom) {
            // Cluster-switch drop banner (t_42ba90d2): queued messages were
            // cleared because the user pointed the app at a different cluster.
            // Surface them (never silently dropped); Dismiss clears the banner
            // (the messages must be re-sent by hand on the new cluster).
            if let dropped = droppedBanner, !dropped.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
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
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.Semantic.warn.opacity(0.15))
                .overlay(alignment: .leading) {
                    Rectangle().fill(Theme.Semantic.warn).frame(width: 4)
                }
                .clipShape(RoundedRectangle(cornerRadius: 10))
                .padding()
            }
        }
        .onReceive(OfflineSendQueue.shared.$drainedDueToClusterSwitch) { dropped in
            droppedBanner = dropped
        }
        .onAppear {
            approvals.setClient(makeClient())
            replyWatcher.setClient(makeClient())
            NotificationCoordinator.shared.setClient(makeClient())
            router.setClient(makeClient())
            seedOfflineQueue()
            // Cold-start deep link: if a notification tap / Handoff set a
            // requested tab before this view appeared, onChange(of:) won't fire
            // for that initial value — apply it here (one-shot, then cleared).
            if let requested = router.requestedTab {
                selectedTab = requested
                router.requestedTab = nil
            }
            // Re-hydration: end any Live Activity left over from a prior process
            // kill (deinit doesn't run when the process is killed, so ActivityKit
            // would otherwise keep a stale wake/session bubble alive forever).
            // Both sweeps are idempotent and only end what the staleness heuristic
            // flags; a genuinely in-flight wake is left untouched.
            LiveActivityManager.sweepLeftoverWakes()
            SessionActivityDriver.sweepLeftoverSessions()
        }
        .onChange(of: settings.connectionIdentity) {
            // Cluster switched: reset the shared monitor so the banner reflects
            // the NEW cluster's first real request, not an outcome from the old
            // one. The banner itself is rendered by ConnectionBanner inside each
            // tab's content, below the nav bar (t_4889e978 — never overlapping).
            ConnectionMonitor.shared.reset()
            approvals.setClient(makeClient())
            replyWatcher.setClient(makeClient())
            NotificationCoordinator.shared.setClient(makeClient())
            router.setClient(makeClient())
            // A different cluster is a different session: queued messages destined
            // for the OLD cluster must never flush into the new one. Drain (clear)
            // the queue but SURFACE what was dropped so nothing is silently lost;
            // a banner shows the count and the messages can be re-sent by hand.
            seedOfflineQueue()
            OfflineSendQueue.shared.drainDueToClusterSwitch()
        }
        .onChange(of: settings.appGroupUnavailable) {
            // ConnectionBanner observes SettingsStore directly and redraws.
        }
        // Deep links (t_136762f3). Every entry point funnels through the router:
        //   · onOpenURL  — `hscc://` URLs (typed, tapped in Messages, tapped in
        //                  a Safari/Shortcuts URL, or launched via x-callback).
        //   · onChange(requestedTab) — the router asks for a specific tab
        //     (the Projects one) and we apply it to the TabView.
        //   · alert      — a malformed / stale link surfaces an honest message
        //                  instead of crashing or landing on a blank screen.
        .onOpenURL { url in
            router.handle(url)
        }
        .onChange(of: router.requestedTab) { _, requested in
            guard let requested else { return }
            selectedTab = requested
            router.requestedTab = nil
        }
        .alert(
            "Couldn't open that link",
            isPresented: Binding(
                get: { router.lastError != nil },
                set: { if !$0 { router.lastError = nil } }
            )
        ) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(router.lastError ?? "That link couldn't be opened.")
        }
    }

    /// Build the client from current settings, or nil when not configured/useful.
    /// Views receive this client so networking stays in the client layer.
    private func makeClient() -> HSCCClient? {
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else {
            return nil
        }
        return HSCCClient(host: settings.host, port: port, token: token)
    }

    /// Wire the offline queue's one real delivery path (t_42ba90d2). The queue
    /// is app-scoped and persisted; it must be able to flush a queued message
    /// regardless of which view is open, so the handler is seeded at the app
    /// root rather than by any single chat view.
    ///
    /// For an orchestrator-chat message this is exactly the fresh-delivery path:
    /// POST to create the job, then persist the job_id to the SAME key the chat
    /// view reads (`ChatStore.persistJobID`), so when the operator next opens
    /// that project's chat, `resumeInFlightJob()` polls and collects the answer.
    /// Delivering = a job was created. A transport failure returns `.unreachable`
    /// (keep queued); a server rejected/failed returns `.rejected` (the queue
    /// can't fix a 400/502, so it removes the message rather than hammer the
    /// server in a loop).
    private func seedOfflineQueue() {
        let client = makeClient()
        // NOTE: no `self`/no `weak` — ContentView is a View struct, not a class,
        // so `weak self` is illegal, and `deliverFromQueue` doesn't read any view
        // state (it only uses `client`). Capturing `client` alone keeps the
        // handler free of any view lifetime.
        OfflineSendQueue.shared.sendHandler = { msg in
            guard let client else { return .unreachable }
            do {
                let started = try await client.orchestratorChatStart(project: msg.project, prompt: msg.text)
                // Persist the job so the chat view resumes and collects the reply.
                ChatStore.persistJobID(started.jobID, for: msg.project)
                return .delivered
            } catch {
                if let e = error as? HSCCError, case .transport = e {
                    return .unreachable  // still can't reach — keep queued, retry later
                }
                return .rejected(
                    (error as? HSCCError)?.localizedDescription
                        ?? "The cluster reached but rejected the queued message."
                )
            }
        }
    }
}

/// Connection status banner — driven by the shared `ConnectionMonitor`, which
/// `HSCCClient` updates on EVERY completed real request (not a one-shot launch
/// probe). It reflects the MOST RECENT API OUTCOME, so a screenful of
/// freshly-loaded data can never sit under a stale "\"Can't reach the cluster\""
/// alarm (t_4889e978).
///
/// Placement: rendered as the FIRST element inside each tab's NavigationStack
/// content, BELOW the nav bar. It must never be attached with
/// `.safeAreaInset(edge: .top)` on the stack root, which draws over the nav bar
/// and toolbar items.
struct ConnectionBanner: View {
    @EnvironmentObject private var settings: SettingsStore
    @ObservedObject private var monitor = ConnectionMonitor.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: bannerIcon)
                    .foregroundColor(bannerColor)
                Text(bannerText)
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if settings.appGroupUnavailable {
                Label(
                    "Sharing broken: the widget & Siri can't see this. Reinstall the app to fix the App Group.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption2)
                .foregroundColor(Theme.Semantic.bad)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(bannerColor.opacity(0.12))
    }

    private var bannerText: String {
        // A broken App Group is more important than reachability: the app
        // would report "Connected" while the widget/intents read nothing.
        if settings.appGroupUnavailable {
            return settings.isConfigured
                ? "App Group unavailable — check the install."
                : "Set host, port, and token in Settings to connect."
        }
        if !settings.isConfigured {
            return "Set host, port, and token in Settings to connect."
        }
        // The MOST RECENT REAL API OUTCOME. `.unknown` = not yet known (neutral);
        // `.reachable` = a real request reached the API (clears the alarm);
        // `.unreachable` = a transport failure (the only state that alarms red).
        switch monitor.status {
        case .unknown:
            return "Configured — waiting for the first request…"
        case .reachable:
            return "Connected to \(settings.host)."
        case .unreachable(let message):
            return message
        }
    }

    private var bannerIcon: String {
        if settings.appGroupUnavailable {
            return "exclamationmark.triangle.fill"
        }
        switch monitor.status {
        case .reachable: return "checkmark.circle.fill"
        case .unreachable: return "exclamationmark.triangle.fill"
        case .unknown: return "circle.dotted"
        }
    }

    private var bannerColor: Color {
        if settings.appGroupUnavailable {
            return Theme.Semantic.bad
        }
        switch monitor.status {
        case .reachable: return Theme.Semantic.ok
        case .unreachable: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.neutral
        }
    }
}
