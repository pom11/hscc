import Foundation

/// A tiny, bounded last-known-state cache for read endpoints (offline feature).
///
/// Stores the raw JSON `Data` of the last successful read per endpoint path,
/// plus when it was captured, in `UserDefaults` so it survives app relaunch —
/// the phone stays useful when the cluster (or Tailscale) is unreachable.
///
/// It is a convenience, not a database: capped at `maxEntries` keys with the
/// oldest evicted by recency, and only ever holds the LAST known value per
/// endpoint. Views never present this as live; they render it clearly marked
/// stale with its age (see `LoadState.stale` / `StaleBanner`).
///
/// The cache is keyed by ENDPOINT PATH ONLY — not by cluster. That is fine
/// while the app talks to one cluster, but on a cluster SWITCH it must be
/// cleared: last-known data from the old cluster would otherwise be served as
/// "stale" under the new cluster's name (via `Offline.load` → `.stale`), which
/// is exactly the "one cluster's data under another cluster's name" hazard the
/// settings take care to avoid. `StateCache.clear()` is called by
/// `SettingsStore` whenever the ACTIVE cluster changes.
enum StateCache {
    private static let storageKey = "hscc.stateCache.v1"
    private static let maxEntries = 40

    /// One cached read: the raw body + when it was captured.
    struct Entry: Codable {
        var data: Data
        var timestamp: Date
    }

    /// Turn an endpoint path into the stable storage key (keeps the mapping
    /// explicit and testable — the same path is used by fetch and cache read).
    static func key(for path: String) -> String { "read.\(path)" }

    // MARK: - Persistence

    private static func readAll() -> [String: Entry] {
        guard let data = UserDefaults.standard.data(forKey: storageKey),
              let dict = try? JSONDecoder().decode([String: Entry].self, from: data) else {
            return [:]
        }
        return dict
    }

    private static func writeAll(_ dict: [String: Entry]) {
        guard let data = try? JSONEncoder().encode(dict) else { return }
        UserDefaults.standard.set(data, forKey: storageKey)
    }

    // MARK: - Store / read

    /// Persist `data` as the last-known response for `path` (called by the
    /// client's `get` on every successful read). Bounded: evicts the oldest
    /// entries beyond `maxEntries`.
    static func store(_ data: Data, for path: String) {
        var dict = readAll()
        dict[key(for: path)] = Entry(data: data, timestamp: Date())
        if dict.count > maxEntries {
            let overflow = dict.count - maxEntries
            let oldest = dict.sorted { $0.value.timestamp < $1.value.timestamp }
                .prefix(overflow).map(\.key)
            for k in oldest { dict.removeValue(forKey: k) }
        }
        writeAll(dict)
    }

    /// The age in seconds of the last-known response for `path`, or nil if
    /// never fetched.
    static func age(for path: String) -> TimeInterval? {
        guard let ts = readAll()[key(for: path)]?.timestamp else { return nil }
        return Date().timeIntervalSince(ts)
    }

    /// The last-known decoded value for `path`, or nil if never fetched.
    static func value<T: Decodable>(_ type: T.Type, for path: String) -> T? {
        guard let entry = readAll()[key(for: path)] else { return nil }
        return try? JSONDecoder().decode(T.self, from: entry.data)
    }

    /// Whether a last-known value exists for `path`.
    static func hasValue(for path: String) -> Bool {
        readAll()[key(for: path)] != nil
    }

    // MARK: - Reset

    /// Wipe EVERY cached entry. Called when the ACTIVE cluster changes so
    /// last-known data from one cluster is never served under another cluster's
    /// name — showing one cluster's data under another's name is worse than
    /// showing nothing (a fresh cluster starts with a clean, honest slate).
    static func clear() {
        UserDefaults.standard.removeObject(forKey: storageKey)
    }
}


/// Canonical read paths the app caches last-known state under (offline
/// feature). The client's `get` caches under the exact path string, and views
/// pass the same constant to `Offline.load` so the read-back lines up. Keeping
/// them in one place prevents a fetch path and its cache key drifting apart.
enum EndpointPath {
    static let projects = "/v1/projects"
    static let clusterStatus = "/v1/cluster/status"
    static let verify = "/v1/verify"
    static let autodownStatus = "/v1/autodown/status"
    static let cards = "/v1/cards"
    static let templateList = "/v1/template/list"
    static let templateStatus = "/v1/template/status"
    static let kanbanBlocked = "/v1/kanban/blocked"
    static let activityFeed = "/v1/activity/feed"
    static let sessions = "/v1/sessions"
}
