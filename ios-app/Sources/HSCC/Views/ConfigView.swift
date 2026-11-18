import SwiftUI

// MARK: - HSCC root (t_9664a1c8, iOS pivot slice S1)
//
// The app is a widget + Live Activity client. It has TWO screens: this one —
// connection settings (daemon endpoint + auth, unchanged semantics) and the
// Widgets & Live Activities configuration below it — and nothing else. The
// operator console is gone: `AppShell`, `Sidebar`, `HomeView` and
// `ContentView` are deleted with the drawer, the dock and the detail stack,
// and every data surface the console rendered now lives in a widget or a Live
// Activity (docs/CONSOLE_RETIREMENT.md §2.3).
//
// Everything here is a byte-for-byte move out of the files it replaces:
//   * the connection + widget Sections, the cluster list, the QR flow, the
//     notification toggles and `WidgetSurfaceEditor` from `SettingsView.swift`;
//   * `ConnectionBanner` from `ContentView.swift`;
//   * `makeClient()` and the lifecycle seeding from `AppShell.swift`.
// Renaming the screen to `ConfigView` is the only content change.
//
// t_3dd38411 (the widget/Live Activity configuration half of the pivot) is the
// ONE addition on top of that move: the Scope section inside
// `WidgetSurfaceEditor` and the resolved-scope line under each surface row. It
// adds no second screen, no second status view (`ConnectionBanner` above is
// still the only connection readout), no second store and no second fetch
// path — it reads through `WidgetSettingsStore.scope(for:)`, writes through
// `WidgetSettingsStore.setScope(_:for:)`, and populates its pickers with the
// three `EntityQuery` types the widget configuration sheet already uses.
//
// What the shell carried that this root deliberately does NOT:
//   * the offline-send-queue `sendHandler` seeding and the cluster-switch drop
//     banner — queueing an orchestrator message is a MUTATION, and the pivot
//     keeps no actions (CONSOLE_RETIREMENT §1.2 rule 2). `OfflineSendQueue`
//     fails safe with no handler seeded (`guard let sendHandler else { return }`
//     leaves a message queued), so nothing is lost, only un-sendable.
//   * `navigationDestination(for: ProjectRoute.self)`: a deep link can no
//     longer land on a project, card or board, because those screens are being
//     deleted by the cards that own them. `onOpenURL` still drives the router,
//     so a malformed link still reports honestly through `lastError`; the
//     router's dead destination cases are retired with S8.
//   * the drawer (`Sidebar`), the dock (`DockItem`) and `AppDestination`.

// MARK: - Connection banner (moved verbatim from ContentView.swift)

/// Connection status banner — driven by the shared `ConnectionMonitor`, which
/// `HSCCClient` updates on EVERY completed real request (not a one-shot launch
/// probe). It reflects the MOST RECENT API OUTCOME, so a screenful of
/// freshly-loaded data can never sit under a stale "\"Can't reach the cluster\""
/// alarm (t_4889e978).
///
/// Placement: rendered as the FIRST element inside a screen's content, BELOW
/// the nav bar. It must never be attached with `.safeAreaInset(edge: .top)` on
/// the stack root, which draws over the nav bar and toolbar items.
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
        .background(Theme.Semantic.tintOverlay(bannerColor))
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
            return "Connected to \(monitor.activeHost ?? settings.host)."
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

// MARK: - The two-screen root

struct ConfigView: View {
    @EnvironmentObject private var settings: SettingsStore
    /// The shared Widgets & Live Activities settings store (t_dea36c48):
    /// read/write in the section below, read-only by the widget/Live Activity
    /// extensions via the App Group.
    @EnvironmentObject private var widgetSettings: WidgetSettingsStore
    /// Deep-link router: the only entry point left that can navigate, and the
    /// only thing that can still report a failed link (t_136762f3).
    @EnvironmentObject private var router: DeepLinkRouter

    // Local editing state seeded from the store.
    @State private var hostField: String = ""
    @State private var portField: String = ""
    @State private var tokenField: String = ""
    /// Extra addresses for the ACTIVE/named cluster, in try order after Host.
    /// Same port and token; reordering here changes the failover order.
    @State private var fallbackFields: [String] = []
    @State private var showingToken = false
    /// True while the operator is defining a brand-new cluster (Add Cluster was
    /// tapped and the form fields were cleared). Save then CREATES a new
    /// cluster instead of editing the active one. Reset to false on load.
    @State private var addingCluster = false

    // Test-connection state. `testReport` holds one row per address (primary +
    // every fallback, in the operator's order); `testResult` is the overall
    // line. Both are produced by the one shared `ClusterConnectionTest`.
    @State private var testResult: String?
    @State private var testIsSuccess: Bool?
    @State private var testReport: ConnectionTestReport?
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
                    // First element of the screen's content, below the nav bar
                    // (never `.safeAreaInset(edge: .top)` — that draws over the bar).
                    ConnectionBanner()
                }
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
                    if fallbackFields.isEmpty {
                        Text("No extra addresses — only the Host above is tried.")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    ForEach(fallbackFields.indices, id: \.self) { index in
                        HStack(spacing: Theme.Spacing.sm.rawValue) {
                            TextField("e.g. host-b.example", text: $fallbackFields[index])
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                                .keyboardType(.URL)
                            // Explicit reorder controls: the list order IS the
                            // failover order, and a drag-only affordance is
                            // undiscoverable in a Form.
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
                            .disabled(index == fallbackFields.count - 1)
                            .accessibilityLabel("Move address down")
                            Button(role: .destructive) {
                                fallbackFields.remove(at: index)
                            } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("Remove address")
                        }
                    }
                    Button {
                        fallbackFields.append("")
                    } label: {
                        Label("Add Address", systemImage: "plus.circle")
                    }
                } header: {
                    Text("Additional Addresses")
                } footer: {
                    Text("Tried top to bottom after Host when a connection attempt fails. Every address uses the same port and token. The app remembers the last address that answered and tries it first next time.")
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
                    // EVERY address gets a row — not just the first one that
                    // answered. A dead fallback must be visible here, not on
                    // the day failover needs it.
                    if let report = testReport {
                        ForEach(report.results) { row in
                            HStack(spacing: Theme.Spacing.sm.rawValue) {
                                Image(systemName: row.isOK ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundColor(row.isOK ? Theme.Semantic.ok : Theme.Semantic.bad)
                                Text(row.host)
                                    .foregroundColor(Theme.Semantic.onSurface)
                                if report.willUse(row.host) {
                                    Text("will use")
                                        .font(.caption2)
                                        .foregroundColor(Theme.Semantic.accent)
                                }
                                Spacer(minLength: 0)
                                Text(row.detail)
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
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
                    Text("Shared settings every widget and Live Activity reads. Each surface can be turned on or off, set its refresh rate, choose which content to show, and be pointed at one board, project or node — a surface with no choice of its own follows the fleet-wide scope. The OS does not reliably honor widget refresh rates below about 5 minutes.")
                }

                Section {
                    NavigationLink {
                        WidgetAppearanceView()
                    } label: {
                        Label("Appearance", systemImage: "paintpalette")
                    }
                } header: {
                    Text("Appearance")
                } footer: {
                    Text("Customise the colour, fonts, size, density and visible chrome of every widget and Live Activity — one global style, with an optional override per surface.")
                }

                Section {
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
            // Save is a STICKY bottom bar (spec §5): it used to be a row at the
            // bottom of a long form, disconnected from the fields it commits —
            // an operator who edited Host had to scroll to the very end to find
            // the control, and the failure labels below it pushed it further
            // away. The two diagnostics stay as the form's last section.
            .safeAreaInset(edge: .bottom) { saveBar }
            .navigationTitle("Settings")
            .onAppear(perform: rootDidAppear)
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
            // Delete confirm (spec §5): the dialog names the exact cluster and
            // says its token goes too, so the operator can't delete the wrong
            // one by a swipe they didn't mean.
            .confirmationDialog(
                "Delete this cluster?",
                isPresented: Binding(
                    get: { pendingDelete != nil },
                    set: { if !$0 { pendingDelete = nil } }
                ),
                titleVisibility: .visible
            ) {
                Button("Delete \(pendingDelete?.name ?? "cluster")", role: .destructive) {
                    if let cluster = pendingDelete {
                        deleteCluster(cluster)
                    }
                    pendingDelete = nil
                }
                Button("Cancel", role: .cancel) { pendingDelete = nil }
            } message: {
                if let cluster = pendingDelete {
                    Text("Removes “\(cluster.name)” (\(cluster.host):\(cluster.port)) and its Keychain token from this device. The cluster itself is untouched; you can add it again later.")
                }
            }
        }
        // A cluster switch is a clean rebuild: every @State the screens held
        // (fields, scan flow, widget editor path) belongs to the previous
        // endpoint and must not survive onto the new one.
        .id(settings.activeClusterID)
        .onChange(of: settings.connectionIdentity) {
            // The most-recent-API-outcome pill goes neutral, not "connected",
            // and every client is rebuilt against the new endpoint.
            ConnectionMonitor.shared.reset()
            seedClients()
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
        // A link that cannot be resolved says so. With the console screens gone
        // there is no destination to land on, so an honest alert is the only
        // correct outcome — never a silent nothing.
        .alert("Couldn't open that link", isPresented: Binding(
            get: { router.lastError != nil },
            set: { if !$0 { router.lastError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(router.lastError ?? "That link couldn't be opened.")
        }
    }

    // MARK: - State helpers

    /// The sticky Save bar (spec §5 button-placement contract: the primary
    /// action sits in a fixed slot, not at the end of a scroll). Disabled
    /// until a field actually differs from the store, exactly as the old
    /// in-form row was.
    private var saveBar: some View {
        Button(action: save) {
            Text(addingCluster ? "Save New Cluster" : "Save")
                .font(.body.weight(.semibold))
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .tint(Theme.Semantic.accent)
        .disabled(!hasEdits)
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .background(Theme.Semantic.surface)
        .accessibilityHint(hasEdits
            ? "Saves host, port and token for this cluster"
            : "Nothing to save — no field has changed")
    }

    private var isReadyToTest: Bool {
        !hostField.trimmingCharacters(in: .whitespaces).isEmpty
            && !tokenField.trimmingCharacters(in: .whitespaces).isEmpty
            && Int(portField) != nil
    }

    private var hasEdits: Bool {
        hostField != settings.host
            || fallbackFields != settings.fallbackHosts
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
    /// checkmark on the active cluster, and an explicit Delete row.
    private func clusterRow(_ cluster: SavedCluster) -> some View {
        let isActive = cluster.id == settings.activeClusterID
        return Button {
            if !isActive {
                switchTo(cluster)
            }
        } label: {
            HStack(spacing: Theme.Spacing.md.rawValue) {
                // Health dot: unknown/ok/failed from the last connection test.
                // HSStatusDot is the shared component, and the state it paints
                // is also spelled out as text next to it (spec §9 — colour is
                // never the only carrier).
                HSStatusDot(healthColor(cluster.health))
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text(cluster.name)
                        .foregroundColor(Theme.Semantic.onSurface)
                        .lineLimit(1)
                    if !cluster.host.isEmpty {
                        Text("\(cluster.host):\(cluster.port) · \(healthLabel(cluster.health))")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                    }
                    if isActive {
                        // Which address the last request actually used, not
                        // merely which one is first in the saved list.
                        Text("In use: \(inUseHost(for: cluster)):\(cluster.port)")
                            .font(.caption2)
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
        // Explicit Delete row + confirm (spec §5): swipe-only deletion was
        // undiscoverable, and the swipe had no confirm naming what it removes.
        // The destructive confirm names the cluster.
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button(role: .destructive) {
                pendingDelete = cluster
            } label: {
                Label("Delete", systemImage: "trash")
            }
        }
        .contextMenu {
            Button(role: .destructive) {
                pendingDelete = cluster
            } label: {
                Label("Delete cluster", systemImage: "trash")
            }
        }
    }

    /// The cluster awaiting the delete confirm (spec §5: a destructive action
    /// gets a `confirmationDialog` that names the target).
    @State private var pendingDelete: SavedCluster?

    /// The address the App Group last saw answer for this active cluster, else
    /// the saved primary. Never a stale address from a DIFFERENT cluster.
    private func inUseHost(for cluster: SavedCluster) -> String {
        guard let remembered = HostFailover.lastWorkingHost(),
              cluster.hosts.contains(where: {
                  $0.caseInsensitiveCompare(remembered) == .orderedSame
              }) else { return cluster.host }
        return remembered
    }

    private func healthLabel(_ health: ClusterHealth) -> String {
        switch health {
        case .ok: return "last test ok"
        case .failed: return "last test failed"
        case .unknown: return "not tested yet"
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
        fallbackFields = []
        testResult = nil
        testIsSuccess = nil
        testReport = nil
    }

    /// Move one extra address one slot up/down. The list order is the failover
    /// order, so this is the operator's only reorder control.
    private func moveFallback(at index: Int, by offset: Int) {
        let target = index + offset
        guard fallbackFields.indices.contains(index),
              fallbackFields.indices.contains(target) else { return }
        fallbackFields.swapAt(index, target)
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
        fallbackFields = settings.fallbackHosts
        portField = settings.port
        tokenField = settings.token ?? ""
        // A report describes the cluster that was tested. Switching or deleting
        // clusters must not leave the previous cluster's rows on screen.
        testResult = nil
        testIsSuccess = nil
        testReport = nil
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
        // Trim blanks/duplicates and keep the operator's order. Host is always
        // the primary, so it is never repeated in the fallback list.
        cluster.fallbackHosts = HostFailover.orderedHosts(primary: host,
                                                          fallbacks: fallbackFields)
            .filter { $0.caseInsensitiveCompare(host) != .orderedSame }
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
        testReport = nil
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

        // Probe EVERY address (primary + fallbacks), not just the first one
        // that answers. The shared implementation returns one row per address
        // in the operator's order, an overall success line when at least one
        // address works, and remembers the fastest working address so widgets
        // and the next request start with it.
        let report = await ClusterConnectionTest.test(
            hosts: [settings.host] + settings.fallbackHosts,
            port: port, token: token)
        testReport = report
        testResult = report.overallLine
        testIsSuccess = report.overallSuccess
    }

    // MARK: - Widgets & Live Activities

    /// One row per surface: a labelled row with a SEPARATE Active toggle,
    /// beside a link into the surface's sub-editor (refresh rate + content
    /// selection). Spec §5 split the old Toggle-inside-NavigationLink: a
    /// control nested in a link is ambiguous for VoiceOver and misfires — one
    /// tap could toggle or navigate depending on where it landed. Now the
    /// toggle and the link are two distinct controls. Reuses the generic
    /// `WidgetSurfaceEditor` for every surface — no bespoke per-surface editors.
    @ViewBuilder
    private var widgetsAndLiveActivitiesRows: some View {
        ForEach(WidgetSurfaceKind.allCases, id: \.self) { kind in
            let surface = widgetSettings.setting(for: kind)
            HStack(spacing: Theme.Spacing.md.rawValue) {
                NavigationLink {
                    WidgetSurfaceEditor(kind: kind, store: widgetSettings)
                } label: {
                    HStack(spacing: Theme.Spacing.md.rawValue) {
                        Image(systemName: surfaceIcon(kind))
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .frame(width: 24)
                        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                            Text(surfaceName(kind))
                                .foregroundColor(Theme.Semantic.onSurface)
                            Text("\(surface.refreshMinutes) min refresh")
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            // What this surface will actually show (t_3dd38411).
                            // Read through the SAME resolution the widget
                            // extensions use — `scope(for:)`, own choice first,
                            // then the fleet default — so a row can never
                            // advertise a slice the store does not hold for it.
                            Text(scopeLine(kind))
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        Spacer(minLength: 0)
                    }
                }
                .accessibilityLabel("\(surfaceName(kind)) settings")
                .accessibilityHint("Opens refresh and content settings")

                // The Active toggle is its OWN labelled control, never nested
                // in the link above.
                Toggle("Active", isOn: Binding(
                    get: { surface.isActive },
                    set: { widgetSettings.setActive($0, for: kind) }
                ))
                .toggleStyle(.switch)
                .accessibilityLabel("\(surfaceName(kind)) active")
            }
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

    /// The scope sentence under one surface's row (t_3dd38411). Resolved
    /// through `WidgetSettingsStore.scope(for:)` — the widgets' own read path,
    /// own choice first then the fleet default — so a row can never advertise
    /// a slice the store does not hold for that surface. Marked
    /// "(fleet default)" when the surface has made no choice of its own, so
    /// the operator can tell an inherited value from one set here.
    private func scopeLine(_ kind: WidgetSurfaceKind) -> String {
        // A surface whose route cannot be scoped says so out loud. Printing
        // `scope(for:)` here would advertise a slice the surface ignores:
        // `/v1/kanban/running` was measured to ignore `?board=` (§4.3), so
        // "board radio" under that row would be a claim about a store value,
        // not about what the bubble shows. See `WidgetSurfaceKind.supportsScope`.
        guard kind.supportsScope else { return "Fleet-wide — this surface cannot be scoped" }
        let resolved = (widgetSettings.scope(for: kind) ?? WidgetScope()).summaryLine
        return widgetSettings.setting(for: kind).scope == nil
            ? "\(resolved) (fleet default)"
            : resolved
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

    // MARK: - Root lifecycle glue (moved from the deleted AppShell.swift)

    private func makeClient() -> HSCCClient? {
        // t_0f3f70e6 (iOS pivot): the fixture-mode client bypass is retired
        // with the capture harness — no launch path builds a fake-token client.
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else { return nil }
        return HSCCClient(host: settings.host,
                          fallbacks: settings.fallbackHosts,
                          port: port, token: token)
    }

    /// One onAppear for the whole app: seed the surviving subsystems with a
    /// client built from the CURRENT settings, then re-hydrate any wake Live
    /// Activity orphan the last process kill left behind (a deinit does not run
    /// when the process is killed, so this sweep is what ends a stale bubble).
    /// The monitor activity's matching `sweepLeftoverMonitors()` runs from
    /// `NotificationsAppDelegate` / `HSCCApp`, not here; the session activity's
    /// `sweepLeftoverSessions()` left with the activity itself (t_3b15b8c9).
    private func rootDidAppear() {
        loadFromStore()
        seedClients()
        LiveActivityManager.sweepLeftoverWakes()
    }

    /// The two subsystems that still reach the daemon both take the same
    /// client: the operator-alert notifier (`Notify/`, which polls on its own
    /// schedule) and the deep-link router. (The reply watcher took a third
    /// seeding until the chat family it served was retired — t_9d5a30c9.)
    private func seedClients() {
        let client = makeClient()
        NotificationCoordinator.shared.setClient(client)
        router.setClient(client)
    }

}


// ---------------------------------------------------------------------------
// Generic per-surface editor (t_dea36c48)
// ---------------------------------------------------------------------------
//
// ONE reusable editor for every surface — the "Widgets & Live Activities"
// sub-editor for a single surface: Active toggle, refresh-rate picker, and
// content-selection toggles. Every surface uses this same component; only
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

/// A row in a scope picker: the identifier that gets persisted, plus what kind
/// of thing it is (shown as the row suffix so the three pickers cannot be
/// confused for one list). A plain view-local value — deliberately NOT the
/// intent's `WidgetScopeOption`: the screen maps that AppEntity down to these
/// two strings, so no AppIntents type has to live in the view's state.
private struct ScopeChoice: Identifiable {
    let id: String
    let subtitle: String?
}

/// The shared editor reused by every surface. Reads/writes the store via
/// the `WidgetSettingsStore` (app-side ObservableObject backed by the App
/// Group), which the enclosing sub-editor owns.
struct WidgetSurfaceEditor: View {
    let kind: WidgetSurfaceKind
    let store: WidgetSettingsStore

    /// Which of the three pickers a binding belongs to (t_3dd38411). The three
    /// pickers are ONE choice — at most one field of a scope may be set — so a
    /// write from any of them replaces the whole choice instead of merging
    /// into it, which is what keeps a stored scope honest.
    private enum ScopeLevel { case board, project, node }

    /// The three picker lists, fetched while this editor is on screen
    /// (t_3dd38411). `nil` = never fetched; a list holding only the "All"
    /// sentinel = fetched and nothing came back, which the UI says out loud
    /// rather than dressing up as an empty cluster.
    @State private var boardOptions: [ScopeChoice]?
    @State private var projectOptions: [ScopeChoice]?
    @State private var nodeOptions: [ScopeChoice]?
    /// Seeded true: the fetch starts the moment the editor appears, so the
    /// "nothing came back" line cannot flash before the first result lands.
    @State private var loadingScopeOptions = true

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

            // MARK: Scope — what THIS widget points at (t_3dd38411)
            // Scope is offered only where the route can honour it (t_71deac83):
            // the pickers below write a `WidgetScope` this surface would
            // ignore, and the fetch would spend three reads on lists it
            // never uses.
            if kind.supportsScope {
                Section {
                    LabeledContent("Shows") {
                        Text((store.scope(for: kind) ?? WidgetScope()).summaryLine)
                            .multilineTextAlignment(.trailing)
                    }
                    Picker("Board", selection: scopeSelection(.board)) {
                        scopeRows(boardOptions ?? [], showSubtitle: false)
                    }
                    Picker("Project", selection: scopeSelection(.project)) {
                        scopeRows(projectOptions ?? [], showSubtitle: true)
                    }
                    Picker("Node", selection: scopeSelection(.node)) {
                        scopeRows(nodeOptions ?? [], showSubtitle: false)
                    }

                    if loadingScopeOptions {
                        HStack(spacing: Theme.Spacing.md.rawValue) {
                            ProgressView()
                            Text("Loading boards, projects and nodes…")
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    } else if scopeListsAreEmpty {
                        Text("No board, project or node came back from the cluster, so only All is offered here. Check the connection at the top of this screen, then reload.")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        Task { await loadScopeOptions() }
                    } label: {
                        Label("Reload scope lists", systemImage: "arrow.clockwise")
                    }

                    if store.setting(for: kind).scope != nil {
                        // The one way back to "follow the fleet default": writing
                        // nil drops the surface's own entry rather than storing an
                        // empty scope (which would mean "All, explicitly").
                        Button("Use the fleet default for this widget") {
                            store.setScope(nil, for: kind)
                        }
                    }
                } header: {
                    Text("Scope")
                } footer: {
                    VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                        Text("Point this widget at one slice of the fleet. The picker you touch decides the whole scope: choosing a board drops a project or node, and choosing All in any of them clears it for this widget.")
                        if store.setting(for: kind).scope == nil {
                            Text("This widget has no choice of its own, so it follows the fleet-wide scope — currently \((store.settings.scope ?? WidgetScope()).summaryLine). That one is set in the widget's own configuration sheet, or by Siri.")
                        } else {
                            Text("This widget's own choice wins over the fleet-wide scope, currently \((store.settings.scope ?? WidgetScope()).summaryLine).")
                        }
                    }
                }
                // Fetching the lists is an OPERATOR action, not a timeline refresh,
                // so it spends none of WIDGET_DATA_CONTRACT §2's requests-per-
                // refresh budget — that rule governs what a widget does on each
                // scheduled refresh, and this is not one. It runs when the surface
                // editor appears and on Reload, and nothing here re-fetches on a
                // timer.
                .task(id: kind) { await loadScopeOptions() }
            } else {
                // The honest stand-in: what it shows, and why there is no
                // picker here.
                Section {
                    Text("This surface always shows the whole fleet. Its route ignores a board, project or node, so there is nothing to pick here — choosing a scope and being shown everything anyway would be a control that lies.")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .fixedSize(horizontal: false, vertical: true)
                } header: {
                    Text("Scope")
                }
            }

            // A surface whose widget has exactly one dataset declares an EMPTY
            // `availableSections` (t_09cd8abf for the board status tile,
            // t_fa7926fb for the running-cards and kanban-monitor panels).
            // Showing an empty "Show Content" group with a "choose which
            // sections" footer underneath would advertise a choice that does
            // not exist, so the group appears only when there is something to
            // choose — which is also why the running-cards Live Activity
            // (t_71deac83, `availableSections: []`) gets no group here rather
            // than a group that does nothing.
            if !sectionRows(kind).isEmpty {
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
        }
        .navigationTitle(surfaceName(kind))
    }

    // MARK: - Scope pickers (t_3dd38411)

    /// True when every list came back holding nothing but the "All" sentinel —
    /// i.e. the cluster answered nothing. Reported as such, never as an empty
    /// fleet.
    private var scopeListsAreEmpty: Bool {
        (boardOptions?.count ?? 0) <= 1
            && (projectOptions?.count ?? 0) <= 1
            && (nodeOptions?.count ?? 0) <= 1
    }

    /// The rows of one picker. "All" is ALWAYS present and needs no network, so
    /// an unreachable cluster still yields a usable picker (the same rule the
    /// configuration sheet follows).
    @ViewBuilder
    private func scopeRows(_ options: [ScopeChoice], showSubtitle: Bool) -> some View {
        Text("All").tag(WidgetScope.wildcard)
        ForEach(options, id: \.id) { option in
            if showSubtitle, let subtitle = option.subtitle, !subtitle.isEmpty {
                Text("\(option.id) — \(subtitle)").tag(option.id)
            } else {
                Text(option.id).tag(option.id)
            }
        }
    }

    /// The binding for ONE of the three pickers. Reading is per-field, writing
    /// is whole-scope: the picker the operator touched decides everything,
    /// folded by the SAME static the configuration intent folds with
    /// (`WidgetScope.buildPickerSelection`), so the three pickers cannot store
    /// a scope that names a board and a node at once.
    private func scopeSelection(_ level: ScopeLevel) -> Binding<String> {
        Binding(
            get: {
                guard let stored = store.setting(for: kind).scope else {
                    return WidgetScope.wildcard
                }
                switch level {
                case .board:   return stored.board ?? WidgetScope.wildcard
                case .project: return stored.project ?? WidgetScope.wildcard
                case .node:    return stored.node ?? WidgetScope.wildcard
                }
            },
            set: { picked in
                let folded = WidgetScope.buildPickerSelection(
                    board: level == .board ? picked : nil,
                    project: level == .project ? picked : nil,
                    node: level == .node ? picked : nil)
                // nil back means the operator chose All. For THIS widget that
                // is a decision, not an absence, so it stores the EMPTY scope
                // — an absent key would quietly re-inherit the fleet default,
                // which is the opposite of what was just tapped. Cases 15 and
                // 17 of widget_settings_check.sh pin that distinction on disk.
                store.setScope(folded ?? WidgetScope(), for: kind)
            }
        )
    }

    /// Pull the three lists through the queries the configuration sheet
    /// already uses (t_98fb8726) — one definition of what a board, a project
    /// and a node are, two entry points, and no second fetch path to drift.
    /// Each query is read-only (`WidgetDataClient`, GET allowlist) and answers
    /// with the sentinel alone on any failure, so a failure here is an empty
    /// picker, never a fabricated row.
    private func loadScopeOptions() async {
        loadingScopeOptions = true
        defer { loadingScopeOptions = false }
        async let boards = WidgetScopeBoardQuery().suggestedEntities()
        async let projects = WidgetScopeProjectQuery().suggestedEntities()
        async let nodes = WidgetScopeNodeQuery().suggestedEntities()
        let fetchedBoards = (try? await boards) ?? []
        let fetchedProjects = (try? await projects) ?? []
        let fetchedNodes = (try? await nodes) ?? []
        boardOptions = fetchedBoards.map { ScopeChoice(id: $0.id, subtitle: $0.subtitle) }
        projectOptions = fetchedProjects.map { ScopeChoice(id: $0.id, subtitle: $0.subtitle) }
        nodeOptions = fetchedNodes.map { ScopeChoice(id: $0.id, subtitle: $0.subtitle) }
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
