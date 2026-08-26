import Foundation

// ---------------------------------------------------------------------------
// A lean GET-only HTTP client shared by the extensions (widget + Live
// Activity). Extensions must NEVER mutate — this client is read-only by
// construction (no POST method exists). It mirrors the app's HSCCClient for
// the handful of reads a widget/activity needs, reusing the SAME credential
// source (App Group + shared Keychain) so it talks to the same cluster.
// ---------------------------------------------------------------------------

/// Fetch helper for the widget/Live Activity extension targets.
///
/// `APIConfig` is loaded from the App Group store (host/port) + shared Keychain
/// (token), exactly like the app, so the extensions are configured identically.
struct ExtensionClient {
    let config: APIConfig

    /// Load a client from shared settings, or nil when not configured/useful.
    static func makeIfConfigured() -> ExtensionClient? {
        guard let config = APIConfig.load() else { return nil }
        return ExtensionClient(config: config)
    }

    private func url(for path: String) -> URL? {
        var components = URLComponents()
        components.scheme = "http"
        components.host = config.host
        components.port = config.port
        components.path = path
        return components.url
    }

    /// Perform a read-only GET and decode into `T`. Returns nil on any failure
    /// (transport, HTTP error, or decode) — the caller decides how to surface it.
    func get<T: Decodable>(_ path: String, as type: T.Type) async -> T? {
        guard let url = url(for: path) else { return nil }
        var req = URLRequest(url: url)
        req.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.timeoutInterval = 15
        // Guard: extensions are READ-ONLY. Refuse anything but a GET.
        guard req.httpMethod == nil || req.httpMethod == "GET" else { return nil }
        do {
            let (data, response) = try await URLSession.shared.data(for: req)
            guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
                return nil
            }
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            return nil
        }
    }
}
