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
    @State private var selectedTab: Tab = .projects
    @State private var pingState: PingState = .idle
    @StateObject private var approvals = ApprovalPoller()

    enum Tab: Hashable {
        case projects, cluster, settings
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            // Primary tab — the dozen projects the operator actually cares about.
            ProjectsView(client: makeClient())
                .safeAreaInset(edge: .top) { connectionBanner }
                .tabItem { Label("Projects", systemImage: "folder") }
                .tag(Tab.projects)

            // Fleet hub — the node topology strip + every fleet-level surface.
            // The tab badge shows the pending-approval count at a glance
            // (approvals inbox, t_9a5cfc3b) — the operator's "is something
            // needing me right now?" signal.
            ClusterView(client: makeClient(), approvalCount: approvals.pendingCount)
                .safeAreaInset(edge: .top) { connectionBanner }
                .tabItem { Label("Cluster", systemImage: "bolt") }
                .badge(approvals.pendingCount.flatMap { $0 > 0 ? String($0) : nil })
                .tag(Tab.cluster)

            // App connection ONLY. The nested duplicate entry point is removed.
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(Tab.settings)
        }
        .onAppear {
            // Probe once at launch so the banner reflects reality immediately.
            refreshConnection()
            approvals.setClient(makeClient())
        }
        .onChange(of: settings.connectionIdentity) {
            refreshConnection()
            approvals.setClient(makeClient())
        }
        .onChange(of: settings.appGroupUnavailable) {
            refreshConnection()
        }
    }

    /// A compact banner summarizing connection state (informational only — the
    /// old Settings NavigationLink here was the duplicate entry point and has
    /// been removed; Settings is its own tab now).
    private var connectionBanner: some View {
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
        .padding(.top, 8)
    }

    private var bannerText: String {
        // A broken App Group is more important than a green ping: the app
        // would report "Connected" while the widget/intents read nothing.
        if settings.appGroupUnavailable {
            return settings.isConfigured
                ? "App Group unavailable — check the install."
                : "Set host, port, and token in Settings to connect."
        }
        switch pingState {
        case .checking:
            return "Checking connection…"
        case .success:
            return "Connected to \(settings.host)."
        case .failure(let message):
            return message
        case .idle:
            if !settings.isConfigured {
                return "Set host, port, and token in Settings to connect."
            }
            return "Configured — testing connection…"
        }
    }

    private var bannerIcon: String {
        if settings.appGroupUnavailable {
            return "exclamationmark.triangle.fill"
        }
        switch pingState {
        case .success: return "checkmark.circle.fill"
        case .failure: return "exclamationmark.triangle.fill"
        case .checking, .idle: return "circle.dotted"
        }
    }

    private var bannerColor: Color {
        if settings.appGroupUnavailable {
            return Theme.Semantic.bad
        }
        switch pingState {
        case .success: return Theme.Semantic.ok
        case .failure: return Theme.Semantic.bad
        case .checking, .idle: return Theme.Semantic.neutral
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

    private func refreshConnection() {
        guard settings.isConfigured else {
            pingState = .idle
            return
        }
        pingState = .checking
        Task {
            guard let token = settings.token,
                  let port = Int(settings.port) else {
                pingState = .idle
                return
            }
            let client = HSCCClient(host: settings.host, port: port, token: token)
            do {
                _ = try await client.ping()
                pingState = .success
            } catch {
                pingState = .failure((error as? HSCCError)?.localizedDescription
                                     ?? "Connection failed.")
            }
        }
    }
}

/// Connection-probe state shown in the banner.
enum PingState {
    case idle
    case checking
    case success
    case failure(String)
}
