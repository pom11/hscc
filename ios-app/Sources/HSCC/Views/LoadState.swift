import SwiftUI

/// A generic async-load container for a single resource fetch.
///
/// Standard lifecycle: `idle → loading → loaded(Value) | failed(message)`.
/// On refresh we re-enter `loading`; if a previous value is cached we keep it
/// on screen while refreshing (see `keepValueWhileRefreshing` view modifier in
/// `ClusterView.swift`).
///
/// The `failed` message is ALWAYS a human-readable string derived from
/// `HSCCError.localizedDescription` (or a stable placeholder) — never a raw
/// error dump.
enum LoadState<Value> {
    case idle
    case loading
    case loaded(Value)
    case failed(String)

    /// The currently-held value, if any (nil when we never loaded successfully).
    var value: Value? {
        if case .loaded(let v) = self { return v }
        return nil
    }

    /// The human-readable failure message, if any.
    var errorMessage: String? {
        if case .failed(let m) = self { return m }
        return nil
    }

    /// Whether we should show a spinner (no value to fall back on).
    var isBusy: Bool {
        switch self {
        case .idle, .loading: return true
        case .loaded, .failed: return false
        }
    }

    /// Whether we are currently mid-fetch (true for both the first load and a
    /// refresh).
    var isLoading: Bool {
        if case .loading = self { return true }
        return false
    }
}
