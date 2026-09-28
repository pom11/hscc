import SwiftUI

/// Settings screen: enter + persist host, port, and token.
///
/// Persistence:
///   * host  — UserDefaults (via `SettingsStore`)
///   * port  — UserDefaults (via `SettingsStore`)
///   * token — Keychain ONLY (via `KeychainStore` / `SettingsStore.saveToken`).
///     Never UserDefaults, never a plist, never hardcoded in source.
///
/// Includes a "Test connection" button that calls GET /v1/ping against the
/// current settings and shows a clear success/failure result, and a "Scan QR"
/// button that fills host/port/token from the setup code printed by
/// `hscc api status`/`hscc api start`.
struct SettingsView: View {
    @EnvironmentObject private var settings: SettingsStore
    /// The shared Widgets & Live Activities settings store (t_dea36c48):
    /// read/write in the section below, read-only by the widget/Live Activity
    /// extensions via the App Group.
    @EnvironmentObject private var widgetSettings: WidgetSettingsStore

    // Local editing state seeded from the store.
    @State private var hostField: String = ""
    @State private var portField: String = ""
    @State private var tokenField: String = ""
    @State private var showingToken = false
    /// True while the operator is defining a brand-new cluster (Add Cluster was
    /// tapped and the form fields were cleared). Save then CREATES a new
    /// cluster instead of editing the active one. Reset to false on load.
    @State private var addingCluster = false

    // Test-connection state.
    @State private var testResult: String?
    @State private var testIsSuccess: Bool?
    @State private var isTesting = false

    // Notification toggles — backed by the shared App-Group suite so the
    // foreground NotificationCoordinator reads the identical store the operator
    // edits here (one source of truth, reachable across processes/widgets).
    // The default-`true` on a fresh install matches `NotifyPreferences.isEnabled`
    // (absent key ⇒ on), so what the toggle shows always equals what fires.
    @AppStorage("hscc.notify.enabled.needsReview",
                store: UserDefaults(suiteName: AppGroup.suiteName))
    private var notifyNeedsReview = true
    @AppStorage("hscc.notify.enabled.cardFailedBlocked",
                store: UserDefaults(suiteName: AppGroup.suiteName))
    private var notifyCardFailedBlocked = true
    @AppStorage("hscc.notify.enabled.fleetUnreachable",
                store: UserDefaults(suiteName: AppGroup.suiteName))
    private var notifyFleetUnreachable = true

    // QR-scan flow state.
    @State private var showingScanner = false
    /// A valid scanned code awaiting the operator's confirm before it is
    /// applied. Host/port shown for confirmation; token never shown in full.
    @State private var scannedCode: SetupQRCode?
    /// A rejected scan (bad shape / wrong version) — surfaced as an error alert.
    @State private var scanError: String?
    @State private var showingConfirm = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    ForEach(settings.clusters) { cluster in
                        clusterRow(cluster)
                    }
                    Button {
                        beginAddingCluster()
                    } label: {
                        Label("Add Cluster", systemImage: "plus.circle")
                    }
                } header: {
                    Text("Clusters")
                } footer: {
                    Text("Keep several clusters and switch with one tap. Each cluster keeps its own token in the Keychain. Switching clears all cached state from the previous cluster.")
                }

                Section {
                    LabeledContent {
                        TextField("e.g. dgx-tailscale (hostname or IP)", text: $hostField)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .multilineTextAlignment(.trailing)
                            .keyboardType(.URL)
                    } label: {
                        Text("Host")
                    }

                    LabeledContent {
                        TextField("8788", text: $portField)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                    } label: {
                        Text("Port")
                    }

                    LabeledContent {
                        HStack {
                            if showingToken {
                                TextField("Bearer token", text: $tokenField)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                            } else {
                                SecureField("Bearer token", text: $tokenField)
                                    .textInputAutocapitalization(.never)
                                    .autocorrectionDisabled()
                            }
                            Button {
                                showingToken.toggle()
                            } label: {
                                Image(systemName: showingToken ? "eye.slash" : "eye")
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel(showingToken ? "Hide token" : "Show token")
                            .accessibilityHint("Controls whether the bearer token is visible or masked")
                        }
                    } label: {
                        Text("Token")
                    }
                } header: {
                    Text(connectionHeader)
                } footer: {
                    Text("Plain HTTP over Tailscale is fine — Tailscale is the encrypted transport. The token is stored only in the iOS Keychain.")
                }

                Section {
                    Button {
                        showingScanner = true
                    } label: {
                        Label("Scan QR", systemImage: "qrcode.viewfinder")
                    }
                } footer: {
                    Text("Scan the setup code from `hscc api status` to fill host, port, and token instead of typing them.")
                }

                Section {
                    Button {
                        Task { await testConnection() }
                    } label: {
                        if isTesting {
                            HStack {
                                ProgressView()
                                Text("Testing…")
                            }
                        } else {
                            Text("Test connection")
                        }
                    }
                    .disabled(!isReadyToTest || isTesting)

                    if let testResult {
                        Label {
                            Text(testResult)
                        } icon: {
                            Image(systemName: testIsSuccess == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                        }
                        .foregroundColor(testIsSuccess == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                    }
                }

                Section {
                    Toggle("New card needs review", isOn: $notifyNeedsReview)
                    Toggle("Card failed or blocked", isOn: $notifyCardFailedBlocked)
                    Toggle("Cluster unreachable", isOn: $notifyFleetUnreachable)

                    Button {
                        Task { await NotificationCoordinator.shared.testNotification() }
                    } label: {
                        Label("Send test notification", systemImage: "bell.badge")
                    }
                } header: {
                    Text("Notifications")
                } footer: {
                    // Be honest: toggles flipped on do nothing if iOS-level
                    // authorization was denied or never granted. Surface that
                    // right here instead of the operator wondering why no banner
                    // ever arrives.
                    if !NotificationCoordinator.shared.canDeliver {
                        Text("Notifications are off in iOS Settings — enable them for HSCC there to receive alerts.")
                    } else {
                        Text("Get a banner when the daemon reports something needing you: a card in the review queue, a failed or blocked card, or the cluster going unreachable.")
                    }
                }

                Section {
                    widgetsAndLiveActivitiesRows
                } header: {
                    Text("Widgets & Live Activities")
                } footer: {
                    Text("Shared settings every widget and Live Activity reads. Each surface can be turned on or off, set its refresh rate, and choose which content to show. The OS does not reliably honor widget refresh rates below about 5 minutes.")
                }

                Section {
                    Button("Save", action: save)
                        .disabled(!hasEdits)
                    if let tokenSaveFailure = settings.tokenSaveFailure {
                        Label(tokenSaveFailure, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundColor(Theme.Semantic.bad)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if settings.appGroupUnavailable {
                        Label("The shared App Group isn't reachable on this install, so saved settings won't reach the widget or Siri. Reinstall the app to fix it — the app itself still connects fine.", systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundColor(Theme.Semantic.bad)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .navigationTitle("Settings")
            .onAppear(perform: loadFromStore)
            // MARK: QR-scan flow — camera sheet → confirm → apply → test.
            .sheet(isPresented: $showingScanner) {
                QRScannerView(
                    onScan: handleScan,
                    onCancel: { showingScanner = false }
                )
            }
            // Confirm-gated apply: like every mutating surface, a single tap
            // never mutates. After a valid scan we show host+port (token
            // redacted) and the operator must explicitly tap "Apply".
            .confirmationDialog(
                "Apply this connection?",
                isPresented: $showingConfirm,
                titleVisibility: .visible
            ) {
                Button("Apply") { Task { await applyScanned() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                if let code = scannedCode {
                    Text("Connect to \(code.host):\(code.port) and set a new token (from the scanned code)?")
                }
            }
            .alert("Scan rejected", isPresented: Binding(
                get: { scanError != nil },
                set: { if !$0 { scanError = nil } }
            )) {
                Button("OK") { scanError = nil }
            } message: {
                Text(scanError ?? "")
            }
        }
    }

    // MARK: - State helpers

    private var isReadyToTest: Bool {
        !hostField.trimmingCharacters(in: .whitespaces).isEmpty
            && !tokenField.trimmingCharacters(in: .whitespaces).isEmpty
            && Int(portField) != nil
    }

    private var hasEdits: Bool {
        hostField != settings.host
            || portField != settings.port
            || tokenField != (settings.token ?? "")
    }

    /// The Connection section header — names which cluster the fields edit, so
    /// the operator always knows what they are about to change.
    private var connectionHeader: String {
        if addingCluster { return "Connection — New Cluster" }
        if let c = settings.activeCluster { return "Connection — \(c.name)" }
        return "Connection"
    }

    /// One row in the Clusters list. Shows name + host:port, a health dot, a
    /// checkmark on the active cluster, and swipe-to-delete.
    private func clusterRow(_ cluster: SavedCluster) -> some View {
        let isActive = cluster.id == settings.activeClusterID
        return Button {
            if !isActive {
                switchTo(cluster)
            }
        } label: {
            HStack(spacing: 12) {
                // Health dot: unknown/ok/failed from the last connection test.
                Circle()
                    .fill(healthColor(cluster.health))
                    .frame(width: 10, height: 10)
                VStack(alignment: .leading, spacing: 2) {
                    Text(cluster.name)
                        .foregroundColor(Theme.Semantic.onSurface)
                        .lineLimit(1)
                    if !cluster.host.isEmpty {
                        Text("\(cluster.host):\(cluster.port)")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                    }
                }
                Spacer()
                if isActive {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(Theme.Semantic.ok)
                        .accessibilityLabel("Active cluster")
                }
            }
        }
        // On a plain Button row inside a Form, foreground styles set by the
        // label would be overridden by the button tint; use .accessibilityAddTraits
        // for the active marker instead of relying on color alone.
        .buttonStyle(.plain)
        .accessibilityLabel("Cluster \(cluster.name)")
        .accessibilityHint(isActive
            ? "This cluster is active"
            : "Switch to this cluster")
        .accessibilityAddTraits(isActive ? .isSelected : [])
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button(role: .destructive) {
                deleteCluster(cluster)
            } label: {
                Label("Delete", systemImage: "trash")
            }
        }
    }

    private func healthColor(_ health: ClusterHealth) -> Color {
        switch health {
        case .ok: return Theme.Semantic.ok
        case .failed: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.neutral
        }
    }

    /// Make `cluster` the active one and reload the edit fields to its values.
    private func switchTo(_ cluster: SavedCluster) {
        settings.selectCluster(cluster.id)
        loadFromStore()
        // The store cleared cached state on the switch; ContentView re-keys its
        // content on activeClusterID so in-session @State is discarded too.
    }

    /// Start defining a brand-new cluster: blank the edit fields and flip into
    /// add mode so Save creates (not edits) a cluster with a fresh id.
    private func beginAddingCluster() {
        addingCluster = true
        hostField = ""
        portField = ""
        tokenField = ""
        testResult = nil
        testIsSuccess = nil
    }

    /// Delete a saved cluster (its token too), keeping the edit form coherent.
    /// Deleting the ACTIVE cluster promotes the first remaining one and
    /// switches — ContentView + the store handle the cache reset.
    private func deleteCluster(_ cluster: SavedCluster) {
        settings.deleteCluster(cluster.id)
        loadFromStore()
    }

    private func loadFromStore() {
        addingCluster = false
        hostField = settings.host
        portField = settings.port
        tokenField = settings.token ?? ""
    }

    // MARK: - Actions

    private func save() {
        // `host`/`port` are computed views onto the ACTIVE SavedCluster since
        // the multi-cluster model landed — they are read-only. Writing the
        // fields means updating that cluster, or CREATING a brand-new one when
        // the operator tapped Add Cluster.
        let host = hostField.trimmingCharacters(in: .whitespaces)
        let port = Int(portField.trimmingCharacters(in: .whitespaces)) ?? 0
        var cluster: SavedCluster
        if addingCluster {
            // Add Cluster: always a fresh id, named from the host so the row in
            // the list is recognizable even before the operator renames it.
            cluster = SavedCluster(id: UUID(),
                                   name: host.isEmpty ? "New Cluster" : host,
                                   host: host, port: port,
                                   lastConnected: nil, lastTestSuccess: nil)
        } else {
            cluster = settings.activeCluster
                ?? SavedCluster(id: UUID(), name: host.isEmpty ? "Cluster" : host,
                                host: host, port: port,
                                lastConnected: nil, lastTestSuccess: nil)
        }
        cluster.host = host
        cluster.port = port
        // Trim the token too: a trailing space/newline pasted from terminal
        // output would otherwise be stored verbatim, making the app "configured"
        // but every request 401 — a silent dead end with no in-app explanation.
        let token = tokenField.trimmingCharacters(in: .whitespaces)
        settings.saveCluster(cluster, token: token.isEmpty ? nil : token)
        addingCluster = false
        // The root view re-probes on isConfigured change.
    }

    private func testConnection() async {
        isTesting = true
        testResult = nil
        testIsSuccess = nil
        defer { isTesting = false }

        // Persist first so the test uses the same settings the app will use.
        save()

        guard let token = settings.token,
              let port = Int(settings.port),
              !settings.host.isEmpty else {
            // Name what is ACTUALLY missing. Saying "set a host, port and token"
            // while all three fields are filled sends the operator looking in
            // the wrong place — that happened when a Keychain write was failing
            // silently and the token read back as nil.
            var missing: [String] = []
            if settings.host.isEmpty { missing.append("host") }
            if Int(settings.port) == nil { missing.append("port") }
            if settings.token == nil {
                if let err = KeychainStore.lastError {
                    missing.append("token (Keychain write failed, OSStatus \(err))")
                } else {
                    missing.append("token")
                }
            }
            testResult = "Missing: \(missing.joined(separator: ", "))."
            testIsSuccess = false
            return
        }

        // Connect through the shared pairing step so a failed scan is told WHY
        // (unreachable host vs rejected token vs wrong app version) instead of a
        // generic transport message. `QRPairingOutcome` is the same surface the
        // onboarding flow uses, so both paths report identically.
        let outcome = await QRPairing.test(host: settings.host, port: port, token: token)
        if case .success = outcome {
            testResult = outcome.message   // "Connected to <service> v<version>."
            testIsSuccess = true
        } else {
            // Headline + actionable explanation for every non-paired outcome.
            testResult = "\(outcome.title): \(outcome.message)"
            testIsSuccess = false
        }
    }

    // MARK: - Widgets & Live Activities

    /// One row per surface: an inline Active toggle + a link to the surface's
    /// sub-editor (refresh rate + content selection). Reuses the generic
    /// `WidgetSurfaceEditor` for every surface — no bespoke per-surface editors.
    @ViewBuilder
    private var widgetsAndLiveActivitiesRows: some View {
        ForEach(WidgetSurfaceKind.allCases, id: \.self) { kind in
            let surface = widgetSettings.setting(for: kind)
            NavigationLink {
                WidgetSurfaceEditor(kind: kind, store: widgetSettings)
            } label: {
                HStack(spacing: 12) {
                    Image(systemName: surfaceIcon(kind))
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .frame(width: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(surfaceName(kind))
                            .foregroundColor(Theme.Semantic.onSurface)
                        Text("\(surface.refreshMinutes) min refresh")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    Spacer()
                    // Inline Active toggle — the operator's single most common
                    // action; the sub-editor has the same toggle for context.
                    Toggle("", isOn: Binding(
                        get: { surface.isActive },
                        set: { widgetSettings.setActive($0, for: kind) }
                    ))
                    .labelsHidden()
                    .toggleStyle(.switch)
                }
            }
            .accessibilityLabel("\(surfaceName(kind)) settings")
            .accessibilityHint("Toggle active or open refresh and content settings")
        }
    }

    /// SF Symbol for a surface's list row.
    private func surfaceIcon(_ kind: WidgetSurfaceKind) -> String {
        kind.displayIcon
    }

    /// Display name for a surface.
    private func surfaceName(_ kind: WidgetSurfaceKind) -> String {
        kind.displayName
    }

    // MARK: - QR scan

    /// Handle a raw scanned payload: decode + validate it, then either move to
    /// the confirm step (valid) or reject it (never partially apply a bad
    /// payload). Dismisses the camera sheet in both cases; a bad scan surfaces
    /// a clear error and the operator can re-open the scanner.
    ///
    /// Runs on the main actor because it mutates `@State`; the AVFoundation
    /// delegate delivers `onScan` on a background queue, so the hop back here
    /// is what keeps the state updates safe.
    @MainActor
    private func handleScan(_ text: String) {
        showingScanner = false
        do {
            let code = try SetupQRCode.decode(text)
            scannedCode = code
            showingConfirm = true
        } catch {
            scanError = (error as? SetupQRCodeError)?.localizedDescription
                ?? "That isn't a valid HSCC setup code."
        }
    }

    /// Apply a confirmed scan: write host/port to the store, the token to the
    /// Keychain (never UserDefaults, never logged, never shown in full on
    /// screen), then run the connection test so the operator immediately sees
    /// whether the pairing worked.
    @MainActor
    private func applyScanned() async {
        guard let code = scannedCode else { return }
        // Bring the local edit fields in line so `save()` keeps them consistent
        // with the applied values (otherwise a stale field would win later).
        hostField = code.host.trimmingCharacters(in: .whitespaces)
        portField = String(code.port)
        tokenField = code.token
        save()
        await testConnection()
    }
}

// ---------------------------------------------------------------------------
// Generic per-surface editor (t_dea36c48)
// ---------------------------------------------------------------------------
//
// ONE reusable editor for every surface — the "Widgets & Live Activities"
// sub-editor for a single surface: Active toggle, refresh-rate picker, and
// content-selection toggles. All four surfaces use this same component; only
// the surface-kind metadata (name/icon/sections) differs, and that lives in
// the display mapping below. No bespoke per-surface editors.

/// Presets offered for refresh cadence. `nil` = Off (surface not refreshed).
/// WidgetKit does not reliably honor rates below ~5 minutes, noted in the UI.
private let refreshPresets: [(label: String, minutes: Int?)] = [
    ("Off", nil),
    ("5 min", 5),
    ("15 min", 15),
    ("30 min", 30),
    ("60 min", 60),
]

/// The shared editor reused by all four surfaces. Reads/writes the store via
/// the `WidgetSettingsStore` (app-side ObservableObject backed by the App
/// Group), which the enclosing sub-editor owns.
struct WidgetSurfaceEditor: View {
    let kind: WidgetSurfaceKind
    let store: WidgetSettingsStore

    var body: some View {
        let surface = store.setting(for: kind)
        Form {
            Section {
                Toggle("Active", isOn: Binding(
                    get: { surface.isActive },
                    set: { store.setActive($0, for: kind) }
                ))
            } footer: {
                Text("Whether this surface is shown at all. Turn it off to hide it without removing it from the Home Screen / Lock Screen.")
            }

            Section {
                Picker("Refresh", selection: Binding(
                    get: { surface.refreshMinutes },
                    set: { store.setRefreshMinutes($0, for: kind) }
                )) {
                    ForEach(refreshPresets, id: \.label) { preset in
                        if let minutes = preset.minutes {
                            Text(preset.label).tag(minutes)
                        } else {
                            Text("Off").tag(0)
                        }
                    }
                }
            } header: {
                Text("Refresh Rate")
            } footer: {
                Text(kind.isLiveActivity
                    ? "How often the app pushes an update while the Live Activity is active. Live Activities cannot fetch on their own — this is the app's push cadence."
                    : "How often the widget refreshes its timeline. The OS does not reliably honor widget refresh rates below about 5 minutes.")
            }

            Section {
                ForEach(sectionRows(kind), id: \.self) { section in
                    Toggle(sectionLabel(section), isOn: Binding(
                        get: { surface.showSections.contains(section) },
                        set: { _ in store.toggleSection(section, for: kind) }
                    ))
                }
            } header: {
                Text("Show Content")
            } footer: {
                Text("Choose which sections this surface renders. Sections you turn off are hidden from its layout.")
            }
        }
        .navigationTitle(surfaceName(kind))
    }

    // MARK: - Display metadata

    /// The ordered sections this surface offers, in a stable display order.
    private func sectionRows(_ kind: WidgetSurfaceKind) -> [String] {
        // Stable order matching the canonical vocabulary; NOT set iteration
        // order (unordered).
        let ordered: [String] = ["state", "topology", "models", "board", "nodes", "host", "timing"]
        return ordered.filter { kind.availableSections.contains($0) }
    }

    private func surfaceName(_ kind: WidgetSurfaceKind) -> String {
        kind.displayName
    }

    private func sectionLabel(_ id: String) -> String {
        WidgetSurfaceKind.displaySectionLabel(id)
    }
}

// ---------------------------------------------------------------------------
// Shared display metadata for surfaces + sections
// ---------------------------------------------------------------------------
//
// UI-only presentation (name, SF Symbol icon, human section labels) for the
// settings screen. Kept separate from the data model in SharedModels.swift so
// the shared model carries NO UI concerns, while the app (which renders the
// editor) has one set of display facts both `SettingsView` rows and the generic
// `WidgetSurfaceEditor` reference — no duplicated switch statements.

extension WidgetSurfaceKind {
    /// Display name shown in the settings rows and editor titles.
    var displayName: String {
        switch self {
        case .widgetCluster: return "Cluster Widget"
        case .widgetMonitor: return "Monitor Widget"
        case .liveActivityWake: return "Wake Live Activity"
        case .liveActivityMonitor: return "Monitor Live Activity"
        }
    }

    /// SF Symbol for the settings row.
    var displayIcon: String {
        switch self {
        case .widgetCluster: return "square.grid.2x2"
        case .widgetMonitor: return "gauge"
        case .liveActivityWake: return "bolt"
        case .liveActivityMonitor: return "waveform.path.ecg"
        }
    }

    /// Human label for a canonical section identifier (\"nodes\" → \"Node metrics\").
    static func displaySectionLabel(_ id: String) -> String {
        switch id {
        case "state": return "State"
        case "topology": return "Topology"
        case "models": return "Models"
        case "board": return "Board work"
        case "nodes": return "Node metrics"
        case "host": return "Host machine"
        case "timing": return "Timing"
        default: return id
        }
    }
}
