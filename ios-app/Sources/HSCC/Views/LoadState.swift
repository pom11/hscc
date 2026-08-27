import Foundation

/// A generic async-load container for a single resource fetch.
///
/// Standard lifecycle: `idle → loading → loaded(Value) | stale(Value, msg) |
/// failed(message)`.
///
/// This is the app's offline-aware load surface (offline last-known state
/// feature). The three on-screen states a view must distinguish honestly are:
///
///   * **live**          — `.loaded` — we just fetched, or are showing a value
///                         held from a successful fetch this session.
///   * **stale**         — `.stale(Value, msg)` — we could NOT reach the cluster
///                         but we have last-known data to show. The payload is
///                         the last-known value and the message explains how old
///                         it is (e.g. "showing state from 6m ago").
///   * **never fetched** — `.idle` / `.loading` with no value, or `.failed`
///                         when there is truly nothing to fall back on.
///
/// The `failed`/`stale` message is ALWAYS a human-readable string derived from
/// `HSCCError.localizedDescription` (or a stable placeholder) — never a raw
/// error dump.
enum LoadState<Value> {
    case idle
    case loading
    case loaded(Value)
    /// Last-known data shown because we couldn't reach the cluster. Payload is
    /// the last-known value; the message says how old it is (and the real
    /// reason we couldn't reach the cluster is reported separately by the view).
    case stale(Value, String)
    case failed(String)

    /// The currently-held value, if any (nil when we never loaded successfully,
    /// whether live, stale, or failed-with-no-fallback).
    var value: Value? {
        switch self {
        case .loaded(let v): return v
        case .stale(let v, _): return v
        default: return nil
        }
    }

    /// The staleness message when we're showing last-known data, if any.
    var staleMessage: String? {
        if case .stale(_, let m) = self { return m }
        return nil
    }

    /// The human-readable failure message, if any (a live fetch that failed
    /// with nothing to fall back on).
    var errorMessage: String? {
        if case .failed(let m) = self { return m }
        return nil
    }

    /// Whether we should show a spinner (no value to fall back on).
    var isBusy: Bool {
        switch self {
        case .idle, .loading: return true
        case .loaded, .stale, .failed: return false
        }
    }

    /// Whether we are currently mid-fetch (true for both the first load and a
    /// refresh).
    var isLoading: Bool {
        if case .loading = self { return true }
        return false
    }
}

/// Helpers for building offline-aware load results and stale-age copy.
enum Offline {

    /// The human "how old is this last-known data" phrase for a cache age in
    /// seconds, e.g. `25s`, `6m`, `3h`, `2d`. Pass the raw interval from the
    /// cache; this floors at 0 and rounds.
    static func agePhrase(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds.rounded()))
        if s < 60 { return "\\(s)s" }
        let m = s / 60
        if m < 60 { return "\\(m)m" }
        let h = m / 60
        if h < 24 { return "\\(h)h" }
        return "\\(h / 24)d"
    }

    /// A reusable offline-aware fetch-and-cache helper (offline last-known
    /// state). This is the single place a view runs a read that should degrade
    /// gracefully when the cluster is unreachable. It wires a fetch closure
    /// through the client's `StateCache` so every named read surface behaves
    /// the same way:
    ///
    ///   * **on success** — returns `.loaded(fresh)`; the client already
    ///     persisted the response to its cache, so it's the new last-known state.
    ///   * **on failure with a cached value** — returns `.stale(cached, ageMsg)`
    ///     so the view renders last-known data clearly marked with its age, plus
    ///     the reason it couldn't reach the cluster.
    ///   * **on failure, holding a value from earlier this session** — returns
    ///     `.stale(currentValue, ageMsg)`; last known is last known, even if it
    ///     only arrived moments ago.
    ///   * **on failure with nothing at all** — returns `.failed(message)` (the
    ///     honest never-fetched case: we have no data and say so).
    ///
    /// `cacheKey` is the endpoint path the client caches under (e.g. "/v1/projects")
    /// so the helper can read back the persisted last-known value and its age.
    ///
    /// Returns the new state for `current` after attempting `fetch`.
    ///
    /// - Parameters:
    ///   - current: the view's current state (used to decide whether to show a
    ///     spinner and to fall back to an in-session value if the persisted
    ///     cache is empty).
    ///   - cacheKey: the endpoint path the client's `StateCache` keyed this
    ///     read under (must match the path the fetch hit so the cache read
    ///     lines up).
    ///   - client: the configured client (nil → returns `current` unchanged;
    ///     callers already gate on configuration upstream).
    ///   - fetch: the read closure (e.g. `{ try await client.projects() }`).
    static func load<T: Decodable>(
        _ current: LoadState<T>,
        cacheKey: String,
        client: HSCCClient?,
        _ fetch: () async throws -> T
    ) async -> LoadState<T> {
        guard let client else { return current }

        do {
            let fresh = try await fetch()
            // The client already wrote this response to StateCache inside get();
            // no need to write again here.
            return .loaded(fresh)
        } catch {
            // 1) Persisted last-known value (survives app relaunch) — best.
            if let cached = client.cachedValue(T.self, for: cacheKey) {
                return .stale(cached, stateAgeMessage(cacheKey, client: client))
            }
            // 2) A value from earlier in this session — last known is last known.
            if let held = current.value {
                return .stale(held, "showing state from 0s ago")
            }
            // 3) Nothing at all — honest never-fetched failure.
            return .failed((error as? HSCCError)?.localizedDescription ?? "Something went wrong.")
        }
    }

    private static func stateAgeMessage(_ cacheKey: String, client: HSCCClient) -> String {
        let age = client.cacheAge(for: cacheKey) ?? 0
        return "showing state from \(Offline.agePhrase(age)) ago"
    }
}
