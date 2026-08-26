import SwiftUI

/// Root view: shows connection state, links to Settings, and hosts placeholder
/// tabs that the B2 (cluster) / B3 (kanban) / B4 (actions) cards will fill in.
///
/// Phase B1 ships only skeleton + settings — the feature tabs below are
/// placeholders that will be replaced by real views in later cards.
struct ContentView: View {
    @EnvironmentObject private var settings: SettingsStore
    @State private var selectedTab: Tab = .cluster
    @State private var pingState: PingState = .idle

    enum Tab: Hashable {
        case cluster, kanban, autodown, control, chat, settings
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            // B2 — cluster + fleet views.
            ClusterView(client: makeClient())
                .safeAreaInset(edge: .top) { connectionBanner }
                .tabItem { Label("Cluster", systemImage: "bolt") }
                .tag(Tab.cluster)

            // B3 — kanban views (standup, cards, review, qa — all read-only).
            KanbanView()
                .safeAreaInset(edge: .top) { connectionBanner }
                .tabItem { Label("Kanban", systemImage: "list.bullet") }
                .tag(Tab.kanban)

            // C6 — autodown control (the operator's most-used surface).
            AutodownView(client: makeClient())
                .safeAreaInset(edge: .top) { connectionBanner }
                .tabItem { Label("Autodown", systemImage: "timer") }
                .tag(Tab.autodown)

            // C6 — the Control hub: Ops, Board Hygiene, Fleet Control, Projects.
            ControlView(client: makeClient())
                .safeAreaInset(edge: .top) { connectionBanner }
                .tabItem { Label("Control", systemImage: "slider.horizontal.3") }
                .tag(Tab.control)

            // C5 — orchestrator chat (confirm-gated mutation).
            NavigationStack {
                OrchestratorChatView()
            }
            .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
            .tag(Tab.chat)

            // Settings — implemented in B1.
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .tag(Tab.settings)
        }
        .onAppear {
            // Probe once at launch so the banner reflects reality immediately.
            refreshConnection()
        }
        .onChange(of: settings.isConfigured) {
            refreshConnection()
        }
    }

    /// A compact banner summarizing connection state. Links to Settings so the
    /// user can fix host/port/token in one tap.
    private var connectionBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: bannerIcon)
                .foregroundColor(bannerColor)
            Text(bannerText)
                .font(.footnote)
                .frame(maxWidth: .infinity, alignment: .leading)
            NavigationLink {
                SettingsView()
            } label: {
                Label("Settings", systemImage: "gearshape")
                    .font(.footnote)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(bannerColor.opacity(0.12))
        .padding(.top, 8)
    }

    private var bannerText: String {
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
        switch pingState {
        case .success: return "checkmark.circle.fill"
        case .failure: return "exclamationmark.triangle.fill"
        case .checking, .idle: return "circle.dotted"
        }
    }

    private var bannerColor: Color {
        switch pingState {
        case .success: return .green
        case .failure: return .red
        case .checking, .idle: return .secondary
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

/// C6 — the Control hub tab: hosts the Ops / Hygiene / Fleet / Projects
/// surfaces behind a segmented picker (mirroring KanbanView's host pattern).
///
/// Every pane is its own NavigationStack view with its own load state, so
/// switching panes never cross-contaminates. All mutations inside these panes
/// go through MutationButton (confirm-gated, `confirm: true`).
struct ControlView: View {
    let client: HSCCClient?

    enum Pane: String, CaseIterable, Identifiable {
        case ops, hygiene, fleet, projects
        var id: String { rawValue }
        var label: String {
            switch self {
            case .ops: return "Ops"
            case .hygiene: return "Hygiene"
            case .fleet: return "Fleet"
            case .projects: return "Projects"
            }
        }
    }

    @State private var selected: Pane = .ops

    var body: some View {
        VStack(spacing: 0) {
            Picker("Pane", selection: $selected) {
                ForEach(Pane.allCases) { pane in
                    Text(pane.label).tag(pane)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)
            .padding(.vertical, 8)

            switch selected {
            case .ops: OpsView(client: client)
            case .hygiene: BoardHygieneView(client: client)
            case .fleet: FleetControlView(client: client)
            case .projects: ProjectsView(client: client)
            }
        }
    }
}
