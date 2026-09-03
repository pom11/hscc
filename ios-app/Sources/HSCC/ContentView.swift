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
    @State private var selectedTab: Tab = .projects
    @StateObject private var approvals = ApprovalPoller()

    enum Tab: Hashable {
        case projects, cluster, settings
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            // Primary tab — the dozen projects the operator actually cares about.
            ProjectsView(client: makeClient())
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
        .onAppear {
            approvals.setClient(makeClient())
            replyWatcher.setClient(makeClient())
            NotificationCoordinator.shared.setClient(makeClient())
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
        }
        .onChange(of: settings.appGroupUnavailable) {
            // ConnectionBanner observes SettingsStore directly and redraws.
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
