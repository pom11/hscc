import Foundation
import SwiftUI

// ---------------------------------------------------------------------------
// Shared cross-target glue for the HSCC app + its extensions.
//
// Compiled into ALL three bundles (app, HSCCWidgets, HSCCLiveActivity) so
// every surface reads the SAME connection settings and the SAME cluster state.
// The app Group is the seam that lets an extension see what the app stores.
//
// Keep this file self-contained (no imports beyond Foundation/SwiftUI) so any
// target can compile it without dragging in app-only code.
// ---------------------------------------------------------------------------

/// Protocol for any read response that carries the API's first-class `speak`
/// field (design §B). Declared here (not in `Models.swift`) so the extensions
/// can conform too without importing the whole app model file.
protocol Speakable {
    var speak: String { get }
}

/// One workload entry from GET /v1/cluster/status.
struct ClusterWorkload: Decodable, Identifiable {
    let name: String
    let tp: String?
    let pp: String?
    let container_id: String?

    var id: String { name }
}

/// GET /v1/cluster/status — workloads + idle/total hosts.
struct ClusterStatusResponse: Decodable, Speakable {
    let workloads: [ClusterWorkload]
    let idle_hosts: [String]
    let total_hosts: Int
    let speak: String
}

/// GET /v1/autodown/status — the autodown report (drives the widget state and
/// the Live Activity's start/end).
///
/// Verified live shape:
///   { enabled, state, idle_minutes, last_activity_iso, down_since,
///     wake_source, reason, watchdog_blocked, watchdog_intentional,
///     kanban_ok, kanban_reason, blocked_by, force_armed,
///     force_armed_overrides, active_cron_cpu_only, active_cron_model, speak }
/// Every field is optional except `speak` (which the API always synthesizes).
struct AutodownStatusResponse: Decodable, Speakable {
    let enabled: Bool?
    let state: String?
    let idle_minutes: Int?
    let last_activity_iso: String?
    let down_since: String?
    let wake_source: String?
    let reason: String?
    let watchdog_blocked: Bool?
    /// The watchdog block's `intentional` marker — a STRING ("autodown") when a
    /// teardown is in effect, absent otherwise. Not a Bool: the server passes
    /// `block.get("intentional")` through verbatim (routes_autodown.py:216).
    let watchdog_intentional: String?
    let kanban_ok: Bool?
    let kanban_reason: String?
    let blocked_by: String?
    let force_armed: Bool?
    let force_armed_overrides: [String]?
    let active_cron_cpu_only: [String]?
    let active_cron_model: [String]?
    let speak: String
}

/// The App Group that the app and all extensions share for preferences +
/// Keychain access, so the widget and Live Activity see the operator's
/// connection settings and last-known cluster state.
enum AppGroup {
    /// The shared UserDefaults suite / Keychain access group identifier.
    static let suiteName = "group.com.hscc.ios"
    /// Keychain item identity (service/account) shared by app + extensions.
    static let keychainService = "com.hscc.ios"
    static let keychainAccount = "api-token"
    // Preference keys stored in the shared suite.
    static let hostKey = "hscc.host"
    static let portKey = "hscc.port"
    // Last-known cluster snapshot keys (see SnapshotStore below).
    static let snapStateKey = "hscc.snap.state"
    static let snapModelCountKey = "hscc.snap.model_count"
    static let snapIdleMinutesKey = "hscc.snap.idle_minutes"
    static let snapNodes = "hscc.snap.nodes"
    static let snapTimestampKey = "hscc.snap.timestamp"
    static let snapErrorKey = "hscc.snap.error"
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
// Cluster state
// ---------------------------------------------------------------------------

/// The top-level cluster state the widget/Live Activity surface.
/// `unreachable` is a FIRST-CLASS state (never an error/local fallback): the
/// cluster can't be reached, and we say so alongside the last-known state.
enum ClusterState: String {
    case serving      // autodown state == up
    case waking       // autodown state == waking
    case down         // autodown state == down
    case unreachable  // the API can't be reached
    case unknown      // no signal at all yet

    /// Sentence-case, active copy for this state.
    var label: String {
        switch self {
        case .serving: return "Serving"
        case .waking: return "Waking"
        case .down: return "Down"
        case .unreachable: return "Can't reach the cluster"
        case .unknown: return "Unknown"
        }
    }

    var color: Color {
        switch self {
        case .serving: return Theme.Semantic.ok
        case .waking: return Theme.Semantic.warn
        case .down: return Theme.Semantic.bad
        case .unreachable: return Theme.Semantic.neutral
        case .unknown: return Theme.Semantic.neutral
        }
    }
}

// ---------------------------------------------------------------------------
// Topology models (moved here so the app widget + Live Activity can reuse the
// app's canonical topology without re-declaring it).
// ---------------------------------------------------------------------------

/// One serving pair in the topology strip — two nodes + a role label.
struct TopologyPair: Identifiable {
    /// A stable identity for the pair (the joining node labels).
    var id: String { "\(nodes[0].label)-\(nodes[1].label)" }
    let nodes: [TopologyNode]       // exactly 2
    let role: String                // e.g. "orchestrator" / "worker"
}

/// One node in the topology strip: its displayed label (the short ip tail) and
/// its live state.
struct TopologyNode: Identifiable {
    let label: String               // e.g. ".244"
    let state: NodeState

    var id: String { label }

    /// The live serving state of a node, driving its dot colour.
    enum NodeState: String {
        /// Serving / healthy.
        case up
        /// Busy / waking / in transition.
        case busy
        /// Warning / degraded.
        case warn
        /// Down / faulted.
        case down
        /// No live signal yet.
        case unknown

        var color: Color {
            switch self {
            case .up: return Theme.Semantic.ok
            case .busy: return Theme.Semantic.warn
            case .warn: return Theme.Semantic.warn
            case .down: return Theme.Semantic.bad
            case .unknown: return Theme.Semantic.neutral
            }
        }
    }
}

/// The canonical two TP pairs as a pure value — shared by the app's topology
/// strip, the widget, and the Live Activity so every surface draws the SAME
/// cluster.
struct TopologySnapshot {
    var pairs: [TopologyPair]
    var modelCount: Int?
    var idleMinutes: Int?
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

    /// Serialize the nodes to a storable form (label|state per node, pipes).
    private static func encodeNodes(_ pairs: [TopologyPair]) -> String {
        pairs.flatMap { $0.nodes }.map { "\($0.label)|\($0.state.rawValue)" }.joined(separator: "|")
    }

    /// Save the current successful snapshot.
    static func save(state: ClusterState, modelCount: Int?, idleMinutes: Int?, pairs: [TopologyPair]) {
        let d = suite
        d?.set(state.rawValue, forKey: AppGroup.snapStateKey)
        if let modelCount { d?.set(modelCount, forKey: AppGroup.snapModelCountKey) }
        if let idleMinutes { d?.set(idleMinutes, forKey: AppGroup.snapIdleMinutesKey) }
        d?.set(encodeNodes(pairs), forKey: AppGroup.snapNodes)
        d?.set(Date().timeIntervalSince1970, forKey: AppGroup.snapTimestampKey)
        d?.removeObject(forKey: AppGroup.snapErrorKey)
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

    /// Keep the timestamp from a prior snapshot even when the current fetch
    /// fails, so we can report the last-known state's age.
    static func lastKnownTimestamp() -> Date? {
        guard let t = UserDefaults(suiteName: AppGroup.suiteName)?.object(forKey: AppGroup.snapTimestampKey) as? Double else { return nil }
        return Date(timeIntervalSince1970: t)
    }

    private static func decodeNodes(_ raw: String?) -> [TopologyNode] {
        guard let raw, !raw.isEmpty else { return [] }
        return raw.split(separator: "|").map { seg -> TopologyNode? in
            let parts = seg.split(separator: "|", maxSplits: 1)
            guard parts.count == 2, let label = parts.first, let stateRaw = parts.last,
                  let state = TopologyNode.NodeState(rawValue: String(stateRaw)) else { return nil }
            return TopologyNode(label: String(label), state: state)
        }.compactMap { $0 }
    }
}
