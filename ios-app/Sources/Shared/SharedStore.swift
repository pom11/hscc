import Foundation
import Security

// ---------------------------------------------------------------------------
// The App Group container + the typed stores every HSCC target shares.
//
// PIVOT (2026-11-17): `ios-app/` is now a widget + Live Activity client. The
// app keeps only connection settings and a small widget/Live Activity config
// screen; the widget and the Live Activity extensions read everything about
// that configuration through THIS file. `SharedModels.swift` beside it is for
// the Codable wire payloads only: anything that is STORED belongs here,
// anything that is DECODED belongs there.
//
// Compiled into ALL four targets (HSCC, HSCCWidgets, HSCCLiveActivity,
// HSCCLiveActivityMonitor) through each target's `sources:` list in
// project.yml. It imports Foundation + Security only — no
// SwiftUI, no app-only code — so an extension can compile it on its own.
//
// THE CONTRACT (binding on every surface that touches the group):
//
//   1. ONE IDENTIFIER. `AppGroup.suiteName` is the only App Group literal in
//      Swift source; `SharedStore.appGroupID` delegates to it, and all five
//      .entitlements files name the same group. `scripts/shared_store_check.sh`
//      guards the discipline around it.
//
//   2. NO SECRETS IN THE STORE. Nothing credential-bearing is ever written to
//      the suite. The bearer token lives in the app's own secure storage (the
//      keychain, written by `KeychainStore`) and is only ever REFERENCED from
//      here — by `AppGroup.keychainService` + `AppGroup.keychainAccount`
//      (per-cluster items are `api-token.<uuid>`). `KeychainShared.readToken()`
//      and `APIConfig.load()` resolve that reference per request and never copy
//      the token into the group. A key added to `AppGroup` must hold a fact,
//      never a credential.
//
//   3. EXTENSIONS NEVER FALL BACK. The app may fall back to `.standard` so it
//      keeps working when the container is missing, but it must then surface
//      that (`SettingsStore.appGroupUnavailable`). An extension reads the
//      shared suite with NO fallback and accepts `nil`: a widget that cannot
//      open the group says "not configured", it never silently reads the app's
//      private defaults.
//
//   4. PERSIST THE PROJECTION, NEVER THE RESPONSE. The typed structs below are
//      the storage shape. No raw server body, no absolute paths, no node
//      addresses — ios-app/docs/WIDGET_DATA_CONTRACT.md section 7 is the
//      binding exclusion list.
//
//   5. WRITES ARE THE APP'S, READS ARE EVERYONE'S. Only the app's config screen
//      calls the save paths; the widget and both Live Activities only read and
//      render what is there.
// ---------------------------------------------------------------------------

/// The App Group container and the shared read/write discipline.
enum SharedStore {
    /// The App Group identifier every target shares. Delegates to
    /// `AppGroup.suiteName` so the literal exists exactly once in Swift source.
    static let appGroupID = AppGroup.suiteName

    /// The shared suite, or nil when the App Group is unavailable.
    ///
    /// Deliberately NOT `?? .standard`: a caller that cannot open the group has
    /// to see `nil` and behave honestly. The app's own `SettingsStore` is the
    /// one place allowed to fall back, and it publishes `appGroupUnavailable`
    /// when it does — `shared_store_check.sh` enforces both halves.
    static var suite: UserDefaults? { UserDefaults(suiteName: appGroupID) }

    /// True when the App Group container is actually provisioned here.
    ///
    /// `UserDefaults(suiteName:)` hands back an object even for a group that was
    /// never provisioned, so this — not a nil check — is the honest signal.
    /// Device-only in practice: macOS auto-creates group containers, which is
    /// why no compile-time gate can assert it and the app shows a banner
    /// instead (see `shared_store_check.sh` for the full history).
    static var containerAvailable: Bool {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroupID) != nil
    }
}

// ---------------------------------------------------------------------------
// Connection settings (App Group UserDefaults + shared keychain reference)
// ---------------------------------------------------------------------------

/// A saved cluster the operator can connect to: a named host/port/token set.
///
/// The app holds a LIST of these (SettingsStore) so an operator can keep
/// several clusters around — a test cluster, a colleague's cluster, their own
/// production one — and switch between them without destroying settings.
///
/// The bearer token is NOT stored here (never in UserDefaults/source): it lives
/// in the Keychain, one item per cluster, keyed by `id` (see KeychainStore).
/// This struct only carries the non-secret connection facts + connection
/// history used by the settings UI and the cluster picker.
struct SavedCluster: Identifiable, Codable, Equatable {
    var id: UUID
    /// Human-friendly name the operator picks ("Production", "Test DGX", …).
    var name: String
    /// Hostname or IP (tailnet name, e.g. `dgx-tailscale`). Never a default.
    var host: String
    /// The HSCC API port (8778 usually; default 8788).
    var port: Int
    /// When the cluster last answered a ping successfully. nil = never.
    var lastConnected: Date?
    /// Result of the last connection test. nil = never tested; true/false =
    /// the ping outcome (drives the health dot). A successful *use* of the
    /// cluster also sets this to true.
    var lastTestSuccess: Bool?

    init(id: UUID = UUID(), name: String, host: String, port: Int,
         lastConnected: Date? = nil, lastTestSuccess: Bool? = nil) {
        self.id = id
        self.name = name
        self.host = host
        self.port = port
        self.lastConnected = lastConnected
        self.lastTestSuccess = lastTestSuccess
    }

    /// The connection health derived from `lastTestSuccess` — the color shown
    /// as the health dot in the cluster list and picker.
    var health: ClusterHealth {
        guard let s = lastTestSuccess else { return .unknown }
        return s ? .ok : .failed
    }
}

/// The health of a saved cluster — drives the colored dot in the list/picker.
enum ClusterHealth {
    case unknown   /// never tested (gray)
    case ok        /// last test / use succeeded (green)
    case failed    /// last test failed (red)
}

/// The App Group that the app and all extensions share for preferences +
/// Keychain access, so the widget and Live Activity see the operator's
/// connection settings and last-known cluster state.
enum AppGroup {
    /// The shared UserDefaults suite / Keychain access group identifier.
    static let suiteName = "group.com.hscc.ios"
    /// Keychain item identity (service/account) shared by app + extensions.
    static let keychainService = "com.hscc.ios"
    /// The legacy "current" token account. This holds the ACTIVE cluster's
    /// token so the extensions/intents (which read this single item) always see
    /// the cluster the operator is connected to. Per-cluster tokens live under
    /// the derived account `api-token.<uuid>` (see KeychainStore).
    static let keychainAccount = "api-token"
    // Preference keys stored in the shared suite.
    static let hostKey = "hscc.host"
    static let portKey = "hscc.port"
    // Multi-cluster list + which cluster is currently active.
    static let clustersKey = "hscc.clusters"
    static let activeClusterIDKey = "hscc.activeClusterID"
    // Last-known cluster snapshot keys (see SnapshotStore below).
    static let snapStateKey = "hscc.snap.state"
    static let snapModelCountKey = "hscc.snap.model_count"
    static let snapIdleMinutesKey = "hscc.snap.idle_minutes"
    static let snapNodes = "hscc.snap.nodes"
    static let snapTimestampKey = "hscc.snap.timestamp"
    static let snapErrorKey = "hscc.snap.error"
    // Last-known board work counts (see ClusterWidget fetch): cards running,
    // card queue depth, and blocked cards (the failure indicator). Persisted so
    // an unreachable window can still show the last-known work with its age.
    static let snapRunningKey = "hscc.snap.running"
    static let snapQueueKey = "hscc.snap.queue"
    static let snapBlockedKey = "hscc.snap.blocked"
    // Widgets & Live Activities shared settings (t_dea36c48): one JSON blob
    // keyed by surface kind, read by every widget / Live Activity extension.
    static let widgetSettingsKey = "hscc.widgetSettings"
}

// ---------------------------------------------------------------------------
// Widgets & Live Activities shared settings (t_dea36c48)
// ---------------------------------------------------------------------------
//
// The single source of truth every widget + Live Activity reads: \"when active,
// refresh rate, what to show\". Stored as ONE JSON blob in the EXISTING App
// Group suite (same defaults the cluster settings use), so the app writes it
// in Settings and each extension reads the identical store — mirroring exactly
// how APIConfig + clusters already flow to the extensions.
//
// There is deliberately NO per-surface bespoke settings: extensions look up
// their one entry by `WidgetSurfaceKind`. A surface with NO stored entry
// defaults to on (backward compatible — the Cluster widget predates this and
// must keep rendering exactly as it does today until the operator edits it).

/// The surfaces the operator controls. The raw values are the registry
/// keys stored in the App Group — stable identifiers, not display names.
///
/// Not transcribed on purpose anywhere: `widget_settings_check.sh` iterates
/// `allCases` rather than asserting a literal count, so adding a surface here
/// cannot redden a healthy gate (it once did — `defaults.surfaces.count == 4`).
enum WidgetSurfaceKind: String, CaseIterable, Codable {
    /// The existing Home Screen cluster widget (state / topology / models /
    /// board work).
    case widgetCluster = "widget.cluster"
    /// The new monitor widget (sibling card t_6d545375).
    case widgetMonitor = "widget.monitor"
    /// The board status widget — five counts for the ONE board the operator
    /// picked in the widget's own configuration sheet (t_6d8731ab shipped the
    /// widget; t_09cd8abf gave it this registry entry so Settings can pause it
    /// and set its cadence like every other surface).
    case widgetBoardStatus = "widget.boardStatus"
    /// The "Running cards" widget (t_fa7926fb): the cards being worked right
    /// now, with the worker running each and how long it has been going.
    ///
    /// Until this case existed the widget hard-coded a quarter-hour on its own
    /// provider and could not be paused at all — the operator's only lever was
    /// pulling the widget off the Home Screen. A raw value is permanent (it is
    /// the key written into the installed App Group blob the moment the
    /// operator touches the row), so it is chosen once, here:
    /// `widget.cardsRunning` names the surface the widget calls itself
    /// (`configurationDisplayName("Running Cards")`, kind
    /// `CardsRunningWidget`), not one dataset inside it.
    case widgetCardsRunning = "widget.cardsRunning"
    /// The "Kanban Monitor" widget (t_fa7926fb): the
    /// `hscc kanban monitor --live` panel — running / ready / blocked plus the
    /// merged row list.
    ///
    /// Same reason and same shape as above. Its header claimed it "uses the
    /// widget default cadence (5 min) and is never 'paused'", which was true
    /// only because no registry entry existed that could say otherwise.
    case widgetKanbanMonitor = "widget.kanbanMonitor"
    /// The "Projects" widget (t_5f246177): the cluster's projects and the board
    /// each one lives on.
    ///
    /// The last widget in `HSCCWidgets` outside this registry, and its provider
    /// said so out loud: "`WidgetSurfaceKind` has no `widgetProjects` case yet,
    /// so this surface is not in the shared cadence/Active registry", with a
    /// private `refreshMinutes = 5` and no way for the operator to pause it —
    /// the same dead end the three widgets above were in.
    ///
    /// The raw value is permanent the moment an installed device writes it, so
    /// it is named here once: after the surface the widget calls itself
    /// (`configurationDisplayName("Projects")`, kind `ProjectsWidget`), not after
    /// one dataset inside it. It is deliberately NOT a reuse of `widget.cluster`
    /// or `widget.monitor` — switching off the cluster widget must never blank
    /// the project list, which is the reason #341 refused to let the monitor
    /// panel borrow `.widgetMonitor`'s entry either.
    case widgetProjects = "widget.projects"
    /// The existing fleet-wake Live Activity.
    case liveActivityWake = "liveActivity.wake"
    /// The new monitor Live Activity (sibling card t_03d318cd).
    case liveActivityMonitor = "liveActivity.monitor"
    /// The cluster-health Live Activity (t_f2867fb3): "is the fleet up", the
    /// Dynamic Island form of the cluster widget's state + nodes-up line.
    ///
    /// This case is what makes the surface controllable: the activity's launch
    /// sweep is deliberately a PURE rule (`ClusterHealthOrphanRule`) with no
    /// settings read, so it stays executable by
    /// `live_activity_rehydration_check.sh`. The Active toggle and the cadence
    /// belong to the DRIVER (`ClusterHealthActivityDriver`), which is the only
    /// consumer of this entry — the sweep still consults nothing.
    case liveActivityClusterHealth = "liveActivity.clusterHealth"
    /// The running-cards Live Activity — the Island / Lock Screen form of
    /// `hscc cards monitor --live` (t_f9cc34e0 renders it, t_71deac83 feeds it).
    ///
    /// Same division as the case above: the launch sweep
    /// (`CardsRunningActivitySweep` + `CardsRunningOrphanRule`) stays a pure
    /// rule the rehydration gate can execute, and the Active toggle plus the
    /// cadence belong to `CardsRunningActivityDriver`, the only consumer of
    /// this entry. It gets its OWN entry rather than borrowing
    /// `.liveActivityMonitor`'s: the two surfaces are pushed by different
    /// drivers off different routes, and one shared toggle would let switching
    /// off the cluster monitor silently stop the cards bubble too.
    case liveActivityCards = "liveActivity.cards"
}

/// One surface's settings: enabled, refresh cadence, and which content
/// sections it renders. Plain Codable struct (AppleScript-agnostic).
struct WidgetSurfaceSettings: Codable, Equatable {
    /// Whether the surface is shown/enabled at all.
    var isActive: Bool
    /// Widget timeline cadence / Live Activity push cadence, in minutes.
    var refreshMinutes: Int
    /// Which content sections the surface renders (set membership, bounded).
    var showSections: Set<String>
    /// What THIS ONE surface is pointed at (t_3dd38411 — the config screen's
    /// per-widget board/project/node picker). Three states, and they are NOT
    /// interchangeable once a fleet-wide default exists:
    ///   * key absent / nil  — the operator never chose for this surface, so
    ///     it INHERITS `AppGroupWidgetSettings.scope` (what the widget's
    ///     configuration intent wrote for the whole fleet);
    ///   * `WidgetScope()`   — the operator explicitly chose All for THIS
    ///     surface: render everything, do not inherit. An empty scope encodes
    ///     as `{}`, which is why it is distinguishable from key absence;
    ///   * a named board / project / node — scoped.
    /// Optional on purpose twice over: a blob written before this field
    /// existed decodes to nil (an absent key is not a decode failure), so an
    /// old install inherits instead of breaking.
    var scope: WidgetScope? = nil
}

/// The full registry: one entry per surface kind. Stored as one JSON blob.
struct AppGroupWidgetSettings: Codable, Equatable {
    var surfaces: [WidgetSurfaceKind: WidgetSurfaceSettings]
    /// The board / project / node the widgets are pointed at — one fleet-wide
    /// selection, nil meaning "everything" (t_98fb8726). Optional on purpose,
    /// twice over: a blob written before this field existed decodes to nil
    /// (an absent key is not a decode failure), and nil is the honest default
    /// for a device where the operator never picked anything. Living in THIS
    /// blob — not a second key — is what keeps the config intent's write and
    /// the widgets' read one and the same code path.
    ///
    /// Since t_3dd38411 this is the FLEET DEFAULT rather than the whole
    /// answer: a single surface may override it with its own `scope` (see
    /// `WidgetSurfaceSettings.scope`), and `scope(for:)` is the one call that
    /// resolves the two. Nothing about the intent's write path changes.
    var scope: WidgetScope? = nil

    /// The key used to store/read this blob in the App Group suite.
    static let storageKey = AppGroup.widgetSettingsKey

    /// Read the registry from the shared App-Group suite, or nil when absent /
    /// corrupt. The app and every extension read the SAME blob via this.
    ///
    /// No `.standard` fallback on purpose (matching `APIConfig`): an extension
    /// that cannot open the shared suite must read nil and behave honestly
    /// (default on / default cadence) rather than fabricate a store the app
    /// never wrote.
    static func load() -> AppGroupWidgetSettings? {
        guard let data = UserDefaults(suiteName: AppGroup.suiteName)?.data(forKey: storageKey),
              let decoded = try? JSONDecoder().decode(AppGroupWidgetSettings.self, from: data) else {
            return nil
        }
        return decoded
    }

    /// The settings for one surface, defaulting to ON when the store is absent
    /// or the surface has no entry yet (backward compatible — a surface that
    /// predates this store must keep working unchanged). Widgets default to a
    /// 5-minute cadence; Live Activities (push-fed) default to 15 minutes.
    func setting(for kind: WidgetSurfaceKind) -> WidgetSurfaceSettings {
        guard let s = surfaces[kind] else {
            return WidgetSurfaceSettings(isActive: true,
                                         refreshMinutes: kind.isLiveActivity ? 15 : 5,
                                         showSections: Set(kind.availableSections))
        }
        // Bound the set to the surface's known sections so a stale/foreign
        // identifier can never leak into the UI (\"bounded set sizes\").
        // refreshMinutes 0 = Off (surface shown but no scheduled refresh) is
        // preserved; only negatives are clamped to 1 so a corrupt blob cannot
        // schedule an instant refresh loop.
        return WidgetSurfaceSettings(isActive: s.isActive,
                                     refreshMinutes: max(0, s.refreshMinutes),
                                     showSections: s.showSections.intersection(Set(kind.availableSections)),
                                     // The scope survives the bounding pass.
                                     // Dropping it here would silently erase
                                     // the operator's per-widget choice on
                                     // every read (widget_settings_check.sh
                                     // pins this line with an executed case).
                                     scope: s.scope)
    }

    /// The scope ONE surface renders (t_3dd38411). Its own choice when the
    /// config screen made one for this surface — including the explicit
    /// `WidgetScope()` that means "All, and do not inherit" — otherwise the
    /// fleet-wide selection the surface's configuration intent wrote, and nil
    /// when neither exists (render everything). Two levels, one blob, ONE read
    /// path: a widget timeline calls this and never learns which level the
    /// answer came from, which is the whole reason the per-widget choice lives
    /// inside this blob rather than in a second key.
    func scope(for kind: WidgetSurfaceKind) -> WidgetScope? {
        surfaces[kind]?.scope ?? scope
    }

    /// The full defaults (all on, 5/15-min cadence, all sections), used to
    /// seed a brand-new store before the operator edits anything.
    static func defaults() -> AppGroupWidgetSettings {
        AppGroupWidgetSettings(surfaces: Dictionary(
            uniqueKeysWithValues: WidgetSurfaceKind.allCases.map { ($0, WidgetSurfaceSettings(
                isActive: true,
                refreshMinutes: $0.isLiveActivity ? 15 : 5,
                showSections: Set($0.availableSections))) }
        ))
    }
}

extension WidgetSurfaceKind {
    /// True for the push-fed Live Activity surfaces (15-min default cadence).
    /// Widgets are self-fetching (5-min default).
    var isLiveActivity: Bool {
        switch self {
        case .widgetCluster, .widgetMonitor, .widgetBoardStatus,
             .widgetCardsRunning, .widgetKanbanMonitor, .widgetProjects:
            return false
        case .liveActivityWake, .liveActivityMonitor, .liveActivityClusterHealth,
             .liveActivityCards: return true
        }
    }

    /// Whether this surface can render a `WidgetScope` at all.
    ///
    /// Not every dataset can be scoped, and a scope picker that the network
    /// ignores is a control that lies: the operator picks a board, the row
    /// says "board radio", and the surface keeps showing the whole fleet.
    /// `/v1/kanban/running` and `/v1/kanban/blocked` were measured to IGNORE
    /// `?board=` (WIDGET_DATA_CONTRACT §4.3), so the running-cards surfaces are
    /// fleet-wide whatever the store holds — and the app-side driver for this
    /// surface therefore does not consult `scope(for:)` at all.
    ///
    /// Every other surface keeps `true` because that is what ships today: each
    /// was measured by its own card against a route that honours a scope. A new
    /// surface that turns out not to honour one opts out HERE, and the config
    /// screen then hides the pickers rather than writing a value nothing reads.
    var supportsScope: Bool {
        switch self {
        case .liveActivityCards: return false
        // Every other surface keeps true: main grew this switch since this
        // branch was cut (widgetCardsRunning, widgetKanbanMonitor,
        // widgetProjects all landed from main), and a new case has to be
        // judged here deliberately rather than by an exhaustive-switch build
        // error. Each of the three is fetched through a route its own card
        // measured against a scope, so they keep what ships today.
        case .widgetCluster, .widgetMonitor, .widgetBoardStatus,
             .widgetCardsRunning, .widgetKanbanMonitor, .widgetProjects,
             .liveActivityWake, .liveActivityMonitor, .liveActivityClusterHealth:
            return true
        }
    }

    /// The content sections this surface can render — the vocabulary the
    /// settings UI offers and the sibling cards' rendering consumes.
    ///
    /// Canonical section identifiers (stable, do not rename once shipped):
    ///   - "state"     — the headline state line (serving/waking/down…)
    ///   - "topology"  — the topology pairs/pods
    ///   - "models"    — model count / workloads
    ///   - "board"     — board work (running / queue / blocked cards)
    ///   - "nodes"     — per-node cluster monitor metrics (CPU/RAM/GPU)
    ///   - "host"      — daemon-host machine data (the machine HSCC runs on)
    ///   - "timing"    — wake timing / elapsed (Live Activities)
    ///
    /// A surface with ONE dataset and no optional part declares the empty set:
    /// the board status widget draws five counts for one board and nothing
    /// else, so every candidate section here would be a toggle that either
    /// empties the widget or does nothing — and a toggle that does nothing is
    /// a lie in the settings UI (`WidgetSurfaceEditor` hides its whole
    /// "Show Content" group for exactly this case).
    var availableSections: Set<String> {
        switch self {
        case .widgetCluster:    return ["state", "topology", "models", "board", "nodes"]
        case .widgetMonitor:    return ["state", "nodes", "host"]
        // One dataset, nothing optional — see the rule above. The board tile is
        // five counts for one board; the running-cards widget is the row list
        // plus the running/blocked counts its header and footer are built from;
        // the monitor panel is running/ready/blocked plus that same merged list.
        // A toggle for any part of those would either empty the surface or do
        // nothing, and every entry here renders a Toggle — so an invented id is
        // a dead control the operator can touch.
        case .widgetBoardStatus: return []
        case .widgetCardsRunning, .widgetKanbanMonitor: return []
        // The projects surface is the same ruling, and the card asked explicitly
        // whether its per-project rows make a section genuine. They do not:
        //   * it draws ONE dataset (`GET /v1/projects` → `{name, board}` rows)
        //     and BOTH keys carry the fact — this file's own header says the
        //     surface exists to show which projects exist "and which board each
        //     one lives on", so a "board" toggle that emptied the board column
        //     would remove the reason the surface exists, not trim a garnish;
        //   * per-project scoping is already a control the operator has and can
        //     see: `ProjectFilterIntent`'s Project picker in the widget's own
        //     configuration sheet. A second copy of that choice in this store
        //     would be a Toggle the sheet never reads.
        // So: nothing here is optional, and `WidgetSurfaceEditor` hides its whole
        // "Show Content" group for exactly this case (ConfigView's
        // `if !sectionRows(kind).isEmpty`) — an invented id would ship as a dead
        // control the operator can touch and nothing obeys.
        case .widgetProjects: return []
        case .liveActivityWake: return ["state", "topology", "timing"]
        case .liveActivityMonitor: return ["state", "nodes", "host"]
        // Only the tally is optional here. The serving state is this
        // activity's HEADLINE — every region of the Island and the Lock
        // Screen row draws from it — and there is no honest rendering of a
        // cluster-health bubble without one, so it is not offered as a
        // toggle. It is also what keeps a settings change from ever making
        // `ClusterHealthSignals.isEmpty` true: that is the state the launch
        // sweep ENDS a bubble for, and an operator switching sections off
        // must never be what deletes their surface.
        case .liveActivityClusterHealth: return ["nodes"]
        // Rows (board · card · worker · run time) and the two counts are ONE
        // projection from ONE pair of routes: the blocked read is what tells an
        // idle fleet apart from a jammed one (§5.4), so it is part of the
        // dataset, not a section an operator can switch off.
        case .liveActivityCards: return []
        }
    }
}

// ---------------------------------------------------------------------------
// Widget scope — what a widget is pointed AT (card t_98fb8726)
// ---------------------------------------------------------------------------
//
// ONE fleet-wide selection — a board, a project, or a node — stored as one
// optional field of the SAME `hscc.widgetSettings` blob every extension
// already reads, so the config intent and the widgets share one write path,
// one storage key and one load path; two stores for one concept is how the
// two sides drift.
//
// Honesty rules:
//   * at most one field is set — the three pickers are one choice, and a
//     scope naming a board AND a node simultaneously has no honest meaning;
//   * the picker's "All" option is the `wildcard` sentinel in the SHEET only;
//     `normalized` folds it to nil, so the store never holds the sentinel and
//     a consumer never learns a magic string exists;
//   * a node is named by `MonitorNode.displayLabel` (sample hostname, else the
//     last octet) — never the address (`WIDGET_DATA_CONTRACT.md` §7:
//     `/v1/cluster/monitor` host addresses must never reach the App Group).

/// The one scope a widget renders: nil fields = "everything".
struct WidgetScope: Codable, Equatable {
    /// The sheet's "All" option id. Folded to nil by `normalized` — it never
    /// reaches the store, so absence and "All" are one state in the blob.
    static let wildcard = "*"

    /// Kanban board slug — scopes the card/kanban datasets.
    var board: String?
    /// Project name (`GET /v1/projects`) — a project lives on exactly one
    /// board, so the widget scopes the project's board itself.
    var project: String?
    /// Node label (`MonitorNode.displayLabel`), never an address (§7).
    var node: String?

    init(board: String? = nil, project: String? = nil, node: String? = nil) {
        self.board = board
        self.project = project
        self.node = node
    }

    /// Nothing chosen (or "All" chosen): every widget renders fleet-wide.
    var isAll: Bool { board == nil && project == nil && node == nil }

    /// The sentence the config intent speaks and the config screen shows —
    /// built only from the stored values, so it can never name a scope the
    /// store does not hold.
    var summaryLine: String {
        if let board { return "board \(board)" }
        if let project { return "project \(project)" }
        if let node { return "node \(node)" }
        return "all boards, projects and nodes"
    }
}

extension WidgetScope {
    /// nil for nil/blank/the sentinel — the one normalization every write goes
    /// through, so the store holds real identifiers or nothing.
    static func normalized(_ raw: String?) -> String? {
        guard let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines),
              !trimmed.isEmpty, trimmed != wildcard else { return nil }
        return trimmed
    }

    /// The scope a configuration sheet's raw picker strings select: EXACTLY ONE
    /// real thing, board → project → node precedence, the sentinel and blanks
    /// folded away, and nil when the operator chose "All" in every picker.
    /// "Exactly one picker is set" is the sheet's rule; the fold enforces it
    /// anyway, because a stored scope naming a board AND a node has no honest
    /// meaning — the first real name wins and the rest are dropped, not
    /// silently kept. This is the intent's whole decision, as data —
    /// `widget_settings_check.sh` extracts this block and executes it verbatim
    /// against a real named suite.
    static func buildPickerSelection(board: String?, project: String?, node: String?) -> WidgetScope? {
        if let board = normalized(board) { return WidgetScope(board: board) }
        if let project = normalized(project) { return WidgetScope(project: project) }
        if let node = normalized(node) { return WidgetScope(node: node) }
        return nil
    }

    /// Persist the selection into the registry the extensions read: read-modify-
    /// write of the ONE `hscc.widgetSettings` blob, surfaces untouched. `nil`
    /// stores nil ("All") — never the sentinel. Returns false when the shared
    /// suite cannot be opened; writing through `UserDefaults.standard` instead
    /// would persist to a store no extension can read.
    @discardableResult
    static func apply(_ scope: WidgetScope?) -> Bool {
        guard let suite = UserDefaults(suiteName: AppGroup.suiteName) else { return false }
        var settings = AppGroupWidgetSettings.load() ?? AppGroupWidgetSettings.defaults()
        settings.scope = scope
        guard let data = try? JSONEncoder().encode(settings) else { return false }
        suite.set(data, forKey: AppGroupWidgetSettings.storageKey)
        return true
    }
}

/// A lightweight read of the token from the SHARED Keychain access group.
///
/// The app writes the token. Extensions must read the SAME item through the
/// App Group's access group, not a per-bundle item. `accessGroup` is applied
/// via the shared `kSecAttrAccessGroup`; the value uses `$(AppIdentifierPrefix)`
/// which resolves at build time from entitlements.
enum KeychainShared {
    private static func accessGroup() -> String? {
        // The shared app group access group — resolved at build time from the
        // entitlements (`$(AppIdentifierPrefix)group.com.hscc.ios`).
        KeychainConstants.keychainAccessGroup
    }

    static func readToken() -> String? {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: AppGroup.keychainService,
            kSecAttrAccount as String: AppGroup.keychainAccount,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        // Only scope by access group when one is configured — on a plain
        // simulator type-check none is material at runtime.
        if let ag = accessGroup(), !ag.isEmpty {
            query[kSecAttrAccessGroup as String] = ag
        }
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }
}

/// Reads the configured endpoint (host/port/token) from the App-Group shared
/// store so the app, widget, and Live Activity all see the same connection.
///
/// Host/port come from the App-Group `UserDefaults` suite (which the app's
/// `SettingsStore` writes); the token comes from the shared Keychain access
/// group (which the app's `KeychainStore` writes). Returns nil when not fully
/// configured.
struct APIConfig {
    let host: String
    let port: Int
    let token: String

    static func load() -> APIConfig? {
        let suite = UserDefaults(suiteName: AppGroup.suiteName)
        guard let host = suite?.string(forKey: AppGroup.hostKey),
              !host.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        let port = Int(suite?.string(forKey: AppGroup.portKey) ?? "8788") ?? 8788
        guard let token = KeychainShared.readToken(), !token.isEmpty else { return nil }
        return APIConfig(host: host, port: port, token: token)
    }
}

/// Constants for Keychain access sharing, kept here so both the app's
/// `KeychainStore` and the extensions' `KeychainShared` agree on the group.
enum KeychainConstants {
    /// EMPTY ON PURPOSE — do not put `$(AppIdentifierPrefix)...` here.
    ///
    /// `$(AppIdentifierPrefix)` is a BUILD SETTING placeholder. Xcode expands it
    /// in .entitlements and Info.plist files, but NEVER inside Swift source: the
    /// string stays literal at runtime, no such access group exists, and every
    /// SecItemAdd/SecItemUpdate fails with errSecMissingEntitlement (-34018).
    /// The token then reads back as nil and the app reports "Set a host, port,
    /// and token first" even though the fields are filled.
    ///
    /// When the access group is empty, `KeychainStore` omits
    /// `kSecAttrAccessGroup` and iOS uses the FIRST group in the
    /// `keychain-access-groups` entitlement — which is exactly
    /// `<team prefix>group.com.hscc.ios`, already shared by the app and both
    /// extensions. Same sharing, correctly resolved, no hardcoded prefix.
    static let keychainAccessGroup = ""
}

// ---------------------------------------------------------------------------
// Last-known snapshot persistence (App Group UserDefaults)
// ---------------------------------------------------------------------------

/// Persists the last-known good cluster snapshot in the App Group suite so a
/// later unreachable window can show yesterday's real state with its age — never
/// a blank or stale-looking-live widget. The app and both extensions write and
/// read this same store.
enum SnapshotStore {
    private static var suite: UserDefaults? { UserDefaults(suiteName: AppGroup.suiteName) }

    /// Serialize the nodes to a storable form (label:state per node, `;`-joined).
    ///
    /// IMPORTANT (fix t_15a88458): the node separator MUST differ from the
    /// label-state separator, or decode cannot recover the pairing. The prior
    /// code used `|` for BOTH, so `encode` produced `.244|up|.246|up|...` and
    /// `decode` split every `|` then re-split a token with no `|` inside —
    /// always recovering 0 nodes. Node labels are `.NNN` and states are words;
    /// neither contains `;` or `:`, so those are unambiguous:
    ///   label : state  (one node)   `;`  joins nodes.
    private static func encodeNodes(_ pairs: [TopologyPair]) -> String {
        pairs.flatMap { $0.nodes }.map { "\($0.label):\($0.state.rawValue)" }.joined(separator: ";")
    }

    /// Save the current successful snapshot.
    static func save(state: ClusterState, modelCount: Int?, idleMinutes: Int?, pairs: [TopologyPair],
                     running: Int?, queueDepth: Int?, blocked: Int?) {
        let d = suite
        d?.set(state.rawValue, forKey: AppGroup.snapStateKey)
        if let modelCount { d?.set(modelCount, forKey: AppGroup.snapModelCountKey) }
        if let idleMinutes { d?.set(idleMinutes, forKey: AppGroup.snapIdleMinutesKey) }
        d?.set(encodeNodes(pairs), forKey: AppGroup.snapNodes)
        d?.set(Date().timeIntervalSince1970, forKey: AppGroup.snapTimestampKey)
        d?.removeObject(forKey: AppGroup.snapErrorKey)
        // Work counts (best-effort; only written when present so a nil keeps
        // the prior last-known value rather than blanking it).
        if let running { d?.set(running, forKey: AppGroup.snapRunningKey) }
        if let queueDepth { d?.set(queueDepth, forKey: AppGroup.snapQueueKey) }
        if let blocked { d?.set(blocked, forKey: AppGroup.snapBlockedKey) }
    }

    /// Load the last-known snapshot, or nil if none was ever recorded.
    static func load() -> (state: ClusterState, modelCount: Int?, idleMinutes: Int?, nodes: [TopologyNode], timestamp: Date?)? {
        let d = suite
        guard let raw = d?.string(forKey: AppGroup.snapStateKey),
              let state = ClusterState(rawValue: raw) else { return nil }
        let modelCount = d?.object(forKey: AppGroup.snapModelCountKey) as? Int
        let idleMinutes = d?.object(forKey: AppGroup.snapIdleMinutesKey) as? Int
        let nodes = decodeNodes(d?.string(forKey: AppGroup.snapNodes))
        let timestamp: Date? = {
            guard let t = d?.object(forKey: AppGroup.snapTimestampKey) as? Double else { return nil }
            return Date(timeIntervalSince1970: t)
        }()
        return (state, modelCount, idleMinutes, nodes, timestamp)
    }

    /// Load just the last-known board work counts (running / queue depth /
    /// blocked). Each is nil when never recorded. Used by the unreachable
    /// widget view so a stale window still shows what the fleet WAS doing.
    static func workCounts() -> (running: Int?, queueDepth: Int?, blocked: Int?) {
        let d = UserDefaults(suiteName: AppGroup.suiteName)
        return (d?.object(forKey: AppGroup.snapRunningKey) as? Int,
                d?.object(forKey: AppGroup.snapQueueKey) as? Int,
                d?.object(forKey: AppGroup.snapBlockedKey) as? Int)
    }

    /// Keep the timestamp from a prior snapshot even when the current fetch
    /// fails, so we can report the last-known state's age.
    static func lastKnownTimestamp() -> Date? {
        guard let t = UserDefaults(suiteName: AppGroup.suiteName)?.object(forKey: AppGroup.snapTimestampKey) as? Double else { return nil }
        return Date(timeIntervalSince1970: t)
    }

    private static func decodeNodes(_ raw: String?) -> [TopologyNode] {
        guard let raw, !raw.isEmpty else { return [] }
        return raw.split(separator: ";").map { seg -> TopologyNode? in
            let parts = seg.split(separator: ":", maxSplits: 1)
            guard parts.count == 2, let label = parts.first, let stateRaw = parts.last,
                  let state = TopologyNode.NodeState(rawValue: String(stateRaw)) else { return nil }
            return TopologyNode(label: String(label), state: state)
        }.compactMap { $0 }
    }
}

// ---------------------------------------------------------------------------
// Last-known MONITOR snapshot (t_6d545375) — storage shape per §7 (t_438aa6ce)
// ---------------------------------------------------------------------------

/// The ONLY persisted form of a monitor node: §7's "persist the projection,
/// never the response", typed as the §3 row-5 six-field node projection.
///
/// `MonitorNode` beside it (SharedModels.swift) carries `host` — the node's LAN
/// address as the API reports it — plus `sample.sparkrun_job_names`. §7 names
/// both as fields that must never enter shared storage or a view, and the App
/// Group container is readable by every extension and outlives the app process,
/// so writing the wire type there persists a LAN address indefinitely. This
/// struct is what the container holds instead: no `host`, no job names, no raw
/// sample. `scripts/model_decode_check/main.swift` encodes a real `Snapshot` and
/// asserts no key or value in the stored JSON holds a dotted-quad.
///
/// `label` is `MonitorNode.displayLabel` — the single node-identity rule (sample
/// hostname, else the last octet), so a stored label can name a node without
/// ever naming where it lives.
struct MonitorNodeEntry: Codable, Equatable {
    /// The node's display label (`displayLabel`), never its address.
    let label: String
    let cpuUsagePct: Double?
    let memUsedPct: Double?
    let gpuUtilPct: Double?
    let usedSlots: Int?
    let freeSlots: Int?
}

/// Persists the last-known-good Monitor widget payload in the App Group suite
/// so a later unreachable window can report yesterday's real node metrics +
/// daemon-host machine data with their age — never a blank widget (mirrors
/// `SnapshotStore`). The monitor's node samples and daemon-host data are richer
/// than the topology `SnapshotStore` carries, so they live in their own blob.
///
/// What the blob holds is the PROJECTION (`MonitorNodeEntry`), not the decoded
/// response: see the struct above for why the wire type may never be written
/// here. `project(_:)` is the only path from one to the other.
enum MonitorSnapshotStore {
    private static var suite: UserDefaults? { UserDefaults(suiteName: AppGroup.suiteName) }
    static let storageKey = "hscc.monitor.snapshot"

    struct Snapshot: Codable {
        /// When this snapshot was recorded (epoch seconds).
        let date: Double
        /// The per-node monitor projection, if one had been fetched. The
        /// projected element type is the §7 guarantee — a `Snapshot` holding an
        /// address does not type-check.
        let nodes: [MonitorNodeEntry]
        /// The daemon-host machine data, if it had been fetched. Carries no
        /// address: `hostname` is the daemon host's own name, which §5.5 names
        /// as renderable (and §7 does not list). Asserted address-free by
        /// `scripts/model_decode_check/main.swift`.
        let daemonHost: MonitorDaemonHost?
    }

    /// The ONLY path from the wire type to storage. `Snapshot.nodes` is
    /// `[MonitorNodeEntry]`, so a caller that wants to persist monitor nodes
    /// must come through here, and the address is dropped on the way in — no
    /// caller can persist the wire type by accident.
    static func project(_ nodes: [MonitorNode]) -> [MonitorNodeEntry] {
        nodes.map { node in
            MonitorNodeEntry(label: node.displayLabel,
                             cpuUsagePct: node.sample?.cpuUsagePct,
                             memUsedPct: node.sample?.memUsedPct,
                             gpuUtilPct: node.sample?.gpuUtilPct,
                             usedSlots: node.used_slots,
                             freeSlots: node.free_slots)
        }
    }

    /// Save the last-known-good monitor projection. Optional-tolerant: if
    /// neither source ever returned data, nothing meaningful is persisted.
    /// Takes the projection, never `[MonitorNode]` — pass `project(nodes)`.
    static func save(nodes: [MonitorNodeEntry], daemonHost: MonitorDaemonHost?) {
        guard let d = suite, !nodes.isEmpty || daemonHost != nil else { return }
        let snap = Snapshot(date: Date().timeIntervalSince1970, nodes: nodes, daemonHost: daemonHost)
        if let data = try? JSONEncoder().encode(snap) {
            d.set(data, forKey: storageKey)
        }
    }

    /// Load the last-known monitor snapshot, or nil if none was ever recorded.
    static func load() -> Snapshot? {
        guard let data = UserDefaults(suiteName: AppGroup.suiteName)?.data(forKey: storageKey),
              let decoded = try? JSONDecoder().decode(Snapshot.self, from: data) else { return nil }
        return decoded
    }

    /// Age of the last-known monitor snapshot in minutes, or nil.
    static func ageMinutes() -> Int? {
        guard let s = load() else { return nil }
        return Int(Date().timeIntervalSince1970 - s.date) / 60
    }
}
