import Foundation

/// HSCC HTTP API client — async/await URLSession.
///
/// Talks to the HSCC API (Phase A) which lives on the owner's Tailscale tailnet
/// at a configurable host + port. Tailscale is the encrypted transport, so the
/// app uses plain `http://` URLs — no TLS, no pinning, no certificate handling
/// (design §D explicitly rules TLS out of scope).
///
/// Behaviors this client guarantees:
///   * `Authorization: Bearer <token>` is sent on EVERY request (reads included)
///     — the API authenticates reads too.
///   * URLs are built from the caller-supplied host + port each call, so the
///     client always reflects the current settings.
///   * The unified error shape `{ "error": { code, message, speak } }` is
///     decoded and surfaced as a readable `HSCCError` (401 → "check your
///     token"; connection failure → "can't reach the cluster — is Tailscale
///     connected?").
///   * Read responses expose the `speak` field via the `Speakable` models
///     (B5 consumes it).
///   * The token is NEVER logged — not in this file, not in debug output.
struct HSCCClient {
    private let host: String
    private let port: Int
    private let token: String
    private let session: URLSession

    /// `portFallback` is used when `port` isn't a valid Int.
    init(host: String, port: Int, token: String,
         session: URLSession = .shared) {
        self.host = host
        self.port = port
        self.token = token
        self.session = session
    }

    // MARK: - URL building (host + port, no hardcoded tailnet address)

    /// Build a URL for `path` (e.g. "/v1/ping") against the configured host+port.
    private func url(for path: String) throws -> URL {
        guard var components = URLComponents() as URLComponents? else {
            throw HSCCError.invalidURL
        }
        components.scheme = "http"
        components.host = host
        components.port = port
        components.path = path
        guard let url = components.url else {
            throw HSCCError.invalidURL
        }
        return url
    }

    // MARK: - Request construction

    private func request(for path: String) throws -> URLRequest {
        var req = URLRequest(url: try url(for: path))
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        return req
    }

    // MARK: - Core GET helper

    /// Perform a GET and decode into `T` (a Decodable read response).
    ///
    /// - Throws: `HSCCError` on transport failure, HTTP error, or decoding failure.
    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        let req = try request(for: path)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            // A transport-level error (connection refused, DNS, timeout) means
            // we never reached the cluster.
            throw HSCCError.transport(underlying: error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw HSCCError.decoding("non-HTTP response")
        }

        let status = http.statusCode
        // 2xx: decode the body into the requested type.
        if (200...299).contains(status) {
            do {
                return try Self.decoder.decode(T.self, from: data)
            } catch {
                throw HSCCError.decoding(String(describing: error))
            }
        }

        // Non-2xx: attempt to decode the unified error shape.
        throw Self.error(from: data, status: status)
    }

    // MARK: - Error decoding (design §C)

    /// Decode the unified error envelope `{ "error": { code, message, speak } }`.
    private static func error(from data: Data, status: Int) -> HSCCError {
        if let envelope = try? decoder.decode(APIErrorEnvelope.self, from: data) {
            return .api(code: envelope.error.code,
                        message: envelope.error.message,
                        status: status)
        }
        // Fall back to a transport-style error when the body isn't the expected
        // error shape (e.g. a proxy or firewall returned something opaque).
        return .api(code: "http_\(status)", message: "HTTP \(status)", status: status)
    }

    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        return d
    }()

    // MARK: - Endpoint methods

    /// GET /v1/ping — the API's own liveness probe. Used by "Test connection".
    func ping() async throws -> PingResponse {
        try await get("/v1/ping", as: PingResponse.self)
    }

    /// GET /v1/cluster/status — workloads + idle/total hosts (B2).
    func clusterStatus() async throws -> ClusterStatusResponse {
        try await get("/v1/cluster/status", as: ClusterStatusResponse.self)
    }

    /// GET /v1/cards — kanban cards (B3).
    func cards() async throws -> CardsResponse {
        try await get("/v1/cards", as: CardsResponse.self)
    }

    /// GET /v1/health — fleet smoke test (B2).
    func health() async throws -> HealthResponse {
        try await get("/v1/health", as: HealthResponse.self)
    }

    /// Generic read for endpoints without a dedicated typed model yet.
    /// Every read carries `speak` (design §B), so callers always get a speech
    /// summary even before the strong type exists.
    func read(_ path: String) async throws -> ReadResponse {
        try await get(path, as: ReadResponse.self)
    }
}
