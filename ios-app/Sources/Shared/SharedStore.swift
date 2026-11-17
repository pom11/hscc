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

/// The four surfaces the operator controls. The raw values are the registry
/// keys stored in the App Group — stable identifiers, not display names.
enum WidgetSurfaceKind: String, CaseIterable, Codable {
    /// The existing Home Screen cluster widget (state / topology / models /
    /// board work).
    case widgetCluster = "widget.cluster"
    /// The new monitor widget (sibling card t_6d545375).
    case widgetMonitor = "widget.monitor"
    /// The existing fleet-wake Live Activity.
    case liveActivityWake = "liveActivity.wake"
    /// The new monitor Live Activity (sibling card t_03d318cd).
    case liveActivityMonitor = "liveActivity.monitor"
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
}

/// The full registry: one entry per surface kind. Stored as one JSON blob.
struct AppGroupWidgetSettings: Codable, Equatable {
    var surfaces: [WidgetSurfaceKind: WidgetSurfaceSettings]

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
                                     showSections: s.showSections.intersection(Set(kind.availableSections)))
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
        case .widgetCluster, .widgetMonitor: return false
        case .liveActivityWake, .liveActivityMonitor: return true
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
    var availableSections: Set<String> {
        switch self {
        case .widgetCluster:    return ["state", "topology", "models", "board"]
        case .widgetMonitor:    return ["state", "nodes", "host"]
        case .liveActivityWake: return ["state", "topology", "timing"]
        case .liveActivityMonitor: return ["state", "nodes", "host"]
        }
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
