import SwiftUI
import WidgetKit

/// The Settings window (`Settings` scene). It edits the SAME saved-cluster store
/// the iOS app and every widget read — multiple addresses with automatic
/// failover, per-cluster Keychain tokens — plus the macOS-only menu bar choices
/// and launch-at-login. The Appearance tab is the shared iOS editor, and every
/// change repaints the desktop widgets.
struct MacSettingsView: View {
    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var preferences: MacPreferences
    @EnvironmentObject private var widgetSettings: WidgetSettingsStore
    @EnvironmentObject private var dashboard: DashboardMonitor

    // The cluster currently in the editor. nil + adding == a new cluster.
    @State private var editingID: UUID?
    @State private var adding = false

    @State private var name = ""
    @State private var host = ""
    @State private var port = "8788"
    @State private var token = ""
    @State private var fallbackHosts: [String] = []

    @State private var statusLine: String?
    @State private var statusOK: Bool?
    @State private var isTesting = false
    @State private var didLoad = false

    var body: some View {
        TabView {
            menuBarTab
                .tabItem { Label("Menu Bar", systemImage: "menubar.rectangle") }
            clustersTab
                .tabItem { Label("Clusters", systemImage: "server.rack") }
            // The SAME Appearance editor the iOS app ships (t_444519ae), reused
            // by path. Its macOS shell supplies a sidebar + detail pane because
            // a Settings window has no navigation stack; the controls, presets
            // and live preview are the shared ones.
            WidgetAppearanceView()
                .environmentObject(widgetSettings)
                .tabItem { Label("Appearance", systemImage: "paintpalette") }
        }
        .frame(width: 780, height: 700)
        .preferredColorScheme(.dark)
        .onAppear {
            guard !didLoad else { return }
            didLoad = true
            loadActive()
        }
        .onChange(of: settings.activeClusterID) { _, _ in
            if !adding { loadActive() }
            reloadSurfaces()
        }
        .onChange(of: settings.clusters) { _, _ in
            // A saved host/token change does not move activeClusterID, so the
            // cluster list is the second trigger. Both edges repaint the
            // widgets and the menu bar stat from the one shared store.
            reloadSurfaces()
        }
        .onChange(of: widgetSettings.settings) { _, _ in
            // Appearance edits are persisted by WidgetSettingsStore (which also
            // reloads timelines); this refresh makes the open dropdown reflect
            // any accompanying cadence/active change immediately.
            reloadSurfaces()
        }
    }

    // MARK: - Tabs

    /// The macOS-only preferences. Everything here is about the menu bar
    /// surface, not the shared widget registry.
    private var menuBarTab: some View {
        Form {
            Section {
                Picker("Menu bar stat", selection: $preferences.menuBarStat) {
                    ForEach(MenuBarStat.allCases) { stat in
                        Text(stat.label).tag(stat)
                    }
                }
                Picker("Refresh every", selection: $preferences.refreshSeconds) {
                    ForEach(MacPreferences.cadences, id: \.self) { seconds in
                        Text(cadenceLabel(seconds)).tag(seconds)
                    }
                }
                Toggle("Launch at login", isOn: Binding(
                    get: { preferences.launchAtLogin },
                    set: { preferences.setLaunchAtLogin($0) }
                ))
                if let error = preferences.launchAtLoginError {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.bad)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } header: {
                Text("Menu Bar")
            } footer: {
                Text("The stat rides next to the H in the menu bar. Refresh uses the shared read-only client; the system may coalesce it while the Mac sleeps.")
            }
        }
        .formStyle(.grouped)
    }

    /// The saved clusters + connection editor. Same `SettingsStore` and
    /// `KeychainStore` the iOS app and every widget read.
    private var clustersTab: some View {
        Form {
            Section {
                ForEach(settings.clusters) { cluster in
                    clusterRow(cluster)
                }
                Button {
                    beginAdding()
                } label: {
                    Label("Add Cluster", systemImage: "plus.circle")
                }
            } header: {
                Text("Clusters")
            } footer: {
                Text("Exactly one cluster is active. Each keeps its own token in the Keychain; switching clears cached state from the previous cluster.")
            }

            Section {
                TextField("Name", text: $name)
                TextField("Host (hostname or IP)", text: $host)
                TextField("Port", text: $port)
                SecureField("Bearer token", text: $token)
            } header: {
                Text(connectionHeader)
            } footer: {
                Text("Plain HTTP over Tailscale is fine — Tailscale is the encrypted transport. The token is stored only in the Keychain.")
            }

            Section {
                if fallbackHosts.isEmpty {
                    Text("No extra addresses — only the host above is tried.")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                ForEach(fallbackHosts.indices, id: \.self) { index in
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        TextField("Additional address", text: $fallbackHosts[index])
                        Button {
                            moveFallback(at: index, by: -1)
                        } label: {
                            Image(systemName: "arrow.up")
                        }
                        .buttonStyle(.borderless)
                        .disabled(index == 0)
                        .accessibilityLabel("Move address up")

                        Button {
                            moveFallback(at: index, by: 1)
                        } label: {
                            Image(systemName: "arrow.down")
                        }
                        .buttonStyle(.borderless)
                        .disabled(index == fallbackHosts.count - 1)
                        .accessibilityLabel("Move address down")

                        Button(role: .destructive) {
                            fallbackHosts.remove(at: index)
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("Remove address")
                    }
                }
                Button {
                    fallbackHosts.append("")
                } label: {
                    Label("Add Address", systemImage: "plus.circle")
                }
            } header: {
                Text("Additional Addresses")
            } footer: {
                Text("Tried top to bottom after the primary host when a transport attempt fails. Every address uses the same port and token; the last one that answered is tried first next time.")
            }

            Section {
                HStack(spacing: Theme.Spacing.md.rawValue) {
                    Button {
                        save()
                    } label: {
                        Text(adding ? "Save New Cluster" : "Save")
                            .frame(minWidth: 120)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.Semantic.accent)
                    .disabled(!canSave)

                    Button {
                        Task { await testConnection() }
                    } label: {
                        if isTesting {
                            HStack(spacing: Theme.Spacing.xs.rawValue) {
                                ProgressView().controlSize(.small)
                                Text("Testing…")
                            }
                        } else {
                            Text("Test Connection")
                        }
                    }
                    .disabled(!canTest || isTesting)

                    Button(role: .destructive) {
                        deleteEditingCluster()
                    } label: {
                        Label("Delete", systemImage: "trash")
                    }
                    .disabled(editingID == nil || adding)
                }

                if let statusLine {
                    Label(statusLine, systemImage: statusOK == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .font(.caption)
                        .foregroundColor(statusOK == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let failure = settings.tokenSaveFailure {
                    Label(failure, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.bad)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .formStyle(.grouped)
    }

    /// Repaint every desktop widget and refresh the always-mounted menu bar
    /// dropdown after a settings write. `WidgetSettingsStore` already reloads
    /// timelines on an appearance edit; the cluster list and active cluster
    /// live in `SettingsStore`, which knows nothing about WidgetKit.
    private func reloadSurfaces() {
        WidgetCenter.shared.reloadAllTimelines()
        Task { await dashboard.refresh() }
    }

    // MARK: - Rows

    private func clusterRow(_ cluster: SavedCluster) -> some View {
        let isActive = cluster.id == settings.activeClusterID
        return HStack(spacing: Theme.Spacing.sm.rawValue) {
            Button {
                adding = false
                editingID = cluster.id
                settings.selectCluster(cluster.id)
                load(cluster)
            } label: {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Circle()
                        .fill(healthColor(cluster.health))
                        .frame(width: 8, height: 8)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(cluster.name)
                            .foregroundColor(Theme.Semantic.onSurface)
                        Text("\(cluster.host):\(cluster.port)")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    if isActive {
                        Text("active")
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.accent)
                    }
                }
            }
            .buttonStyle(.plain)
            Spacer()
            Button(role: .destructive) {
                settings.deleteCluster(cluster.id)
                if editingID == cluster.id { loadActive() }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .accessibilityLabel("Delete \(cluster.name)")
        }
    }

    // MARK: - Editor state

    private var connectionHeader: String {
        if adding { return "Connection — New Cluster" }
        if let editingID, let cluster = settings.clusters.first(where: { $0.id == editingID }) {
            return "Connection — \(cluster.name)"
        }
        return "Connection"
    }

    private var canSave: Bool {
        !host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && Int(port) != nil
    }

    private var canTest: Bool {
        canSave && !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var cleanedFallbacks: [String] {
        fallbackHosts
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private func beginAdding() {
        adding = true
        editingID = nil
        name = ""
        host = ""
        port = "8788"
        token = ""
        fallbackHosts = []
        statusLine = nil
        statusOK = nil
    }

    private func loadActive() {
        guard let cluster = settings.activeCluster else {
            beginAdding()
            adding = false
            return
        }
        editingID = cluster.id
        adding = false
        load(cluster)
    }

    private func load(_ cluster: SavedCluster) {
        name = cluster.name
        host = cluster.host
        port = String(cluster.port)
        fallbackHosts = cluster.fallbackHosts
        token = KeychainStore.readToken(forCluster: cluster.id) ?? ""
        statusLine = nil
        statusOK = nil
    }

    private func moveFallback(at index: Int, by offset: Int) {
        let target = index + offset
        guard fallbackHosts.indices.contains(index), fallbackHosts.indices.contains(target) else { return }
        fallbackHosts.swapAt(index, target)
    }

    // MARK: - Actions

    private func save() {
        guard canSave else { return }
        let existing = editingID.flatMap { id in settings.clusters.first(where: { $0.id == id }) }
        let trimmedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let cluster = SavedCluster(
            id: existing?.id ?? UUID(),
            name: trimmedName.isEmpty ? trimmedHost : trimmedName,
            host: trimmedHost,
            fallbackHosts: cleanedFallbacks,
            port: Int(port) ?? 8788,
            lastConnected: existing?.lastConnected,
            lastTestSuccess: existing?.lastTestSuccess
        )
        settings.saveCluster(cluster, token: token)
        editingID = cluster.id
        adding = false
        statusLine = "Saved."
        statusOK = true
    }

    private func deleteEditingCluster() {
        guard let editingID, !adding else { return }
        settings.deleteCluster(editingID)
        loadActive()
    }

    private func testConnection() async {
        guard canTest else { return }
        isTesting = true
        defer { isTesting = false }
        let config = APIConfig(
            host: host.trimmingCharacters(in: .whitespacesAndNewlines),
            port: Int(port) ?? 8788,
            token: token,
            fallbackHosts: cleanedFallbacks
        )
        let client = WidgetDataClient(config: config, timeout: 6)
        switch await client.clusterStatus() {
        case .success(let status):
            statusLine = "Connected — \(status.total_hosts) hosts, \(status.workloads.count) workloads."
            statusOK = true
        case .failure(let failure):
            statusLine = failure.operatorLine
            statusOK = false
        }
    }

    // MARK: - Text

    private func cadenceLabel(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds) seconds" }
        return seconds == 60 ? "1 minute" : "\(seconds / 60) minutes"
    }

    private func healthColor(_ health: ClusterHealth) -> Color {
        switch health {
        case .ok: return Theme.Semantic.ok
        case .failed: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.neutral
        }
    }
}
