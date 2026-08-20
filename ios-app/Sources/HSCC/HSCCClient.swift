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

    // MARK: - Core POST helper (mutating, confirm-gated)

    /// Perform a mutating POST with a JSON body and decode into `T`.
    ///
    /// Every B4 mutating endpoint requires `"confirm": true` in the body and
    /// returns 409 without it. Each mutating method below ALWAYS includes
    /// `confirm: true`, so there is no code path in this client that can send a
    /// mutating request without an explicit confirmation — the caller (the view)
    /// is responsible for gating the call behind the confirm UI first.
    ///
    /// - Throws: `HSCCError` on transport failure, HTTP error (409 for a missing
    ///   confirm, 502 for a failed merge/apply/stop), or decoding failure. A
    ///   non-2xx NEVER yields a decoded success value.
    func post<T: Decodable>(_ path: String,
                            body: [String: Any],
                            as type: T.Type = T.self) async throws -> T {
        var req = try request(for: path)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        } catch {
            throw HSCCError.decoding(String(describing: error))
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw HSCCError.transport(underlying: error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw HSCCError.decoding("non-HTTP response")
        }

        let status = http.statusCode
        if (200...299).contains(status) {
            do {
                return try Self.decoder.decode(T.self, from: data)
            } catch {
                throw HSCCError.decoding(String(describing: error))
            }
        }

        // Non-2xx (409 confirm missing/refused, 502 merge/apply/stop failed):
        // surface the real error. Never a success value.
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

    /// GET /v1/cluster/hosts — registered hosts + saved clusters + live status (B2).
    func clusterHosts() async throws -> ClusterHostsResponse {
        try await get("/v1/cluster/hosts", as: ClusterHostsResponse.self)
    }

    /// GET /v1/cluster/monitor — fleet monitor snapshot (aggregate metrics).
    /// Shape is dynamic; use the generic `ReadResponse` for `speak` + payload.
    func clusterMonitor() async throws -> ReadResponse {
        try await read("/v1/cluster/monitor")
    }

    /// GET /v1/cluster/jobs — Spark job list. Shape is dynamic; `ReadResponse`.
    func clusterJobs() async throws -> ReadResponse {
        try await read("/v1/cluster/jobs")
    }

    /// GET /v1/cluster/info — cluster configuration summary. `ReadResponse`.
    func clusterInfo() async throws -> ReadResponse {
        try await read("/v1/cluster/info")
    }

    /// GET /v1/cards/{card_id} — one card's full detail (B3).
    func cardDetail(_ cardID: String) async throws -> CardDetailResponse {
        try await get("/v1/cards/\(cardID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? cardID)",
                      as: CardDetailResponse.self)
    }

    /// GET /v1/standup — the daily digest (B3).
    func standup() async throws -> StandupResponse {
        try await get("/v1/standup", as: StandupResponse.self)
    }

    /// GET /v1/review/queue — cards genuinely awaiting review (B3).
    func reviewQueue() async throws -> ReviewQueueResponse {
        try await get("/v1/review/queue", as: ReviewQueueResponse.self)
    }

    /// GET /v1/review/{card_id} — DRY-RUN review facts, read-only (B3).
    /// This endpoint never merges or closes; only the confirm-gated POST in B4
    /// can mutate, and no view here calls it.
    func reviewDetail(_ cardID: String) async throws -> ReviewDetailResponse {
        try await get("/v1/review/\(cardID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? cardID)",
                      as: ReviewDetailResponse.self)
    }

    /// GET /v1/qa/queue — the pre-merge QA queue + manual-QA store (B3).
    func qaQueue() async throws -> QAQueueResponse {
        try await get("/v1/qa/queue", as: QAQueueResponse.self)
    }

    /// GET /v1/health — fleet smoke test (B2).
    func health() async throws -> HealthResponse {
        try await get("/v1/health", as: HealthResponse.self)
    }

    /// GET /v1/fleet/stats?days=N — fleet completions & tool activity (B2).
    func fleetStats(days: Int = 7) async throws -> FleetStatsResponse {
        try await get("/v1/fleet/stats?days=\(days)", as: FleetStatsResponse.self)
    }

    /// GET /v1/fleet/throughput — vLLM token throughput + per-node queue depth (B2).
    func fleetThroughput() async throws -> FleetThroughputResponse {
        try await get("/v1/fleet/throughput", as: FleetThroughputResponse.self)
    }

    /// GET /v1/fleet/streams — daemon stream health (B2).
    func fleetStreams() async throws -> FleetStreamsResponse {
        try await get("/v1/fleet/streams", as: FleetStreamsResponse.self)
    }

    /// GET /v1/autoscale — scaling advice, read-only (B2).
    func autoscale() async throws -> AutoscaleResponse {
        try await get("/v1/autoscale", as: AutoscaleResponse.self)
    }

    /// Generic read for endpoints without a dedicated typed model yet.
    /// Every read carries `speak` (design §B), so callers always get a speech
    /// summary even before the strong type exists.
    func read(_ path: String) async throws -> ReadResponse {
        try await get(path, as: ReadResponse.self)
    }

    // MARK: - Mutating endpoints (B4, ALL confirm-gated)

    /// POST /v1/cards — dispatch a card (create a kanban card).
    ///
    /// Body: `{ board, title, assignee?, body?, confirm: true }`. Never fires
    /// unless the caller has walked the user through the confirm UI. B5 reuses
    /// this for voice dispatch.
    func dispatchCard(board: String,
                      title: String,
                      assignee: String? = nil,
                      body: String? = nil) async throws -> DispatchCardResponse {
        var payload: [String: Any] = [
            "board": board,
            "title": title,
            "confirm": true,
        ]
        if let assignee, !assignee.isEmpty {
            payload["assignee"] = assignee
        }
        if let body, !body.isEmpty {
            payload["body"] = body
        }
        return try await post("/v1/cards", body: payload, as: DispatchCardResponse.self)
    }

    /// POST /v1/review/{card_id}/merge — merge + close a card.
    ///
    /// Body: `{ confirm: true }`. On success returns `{ merged, card_closed }`.
    /// A 502 (merge failed) throws and the card stays open — it is NEVER
    /// presented as merged. Never fires without the confirm UI gate.
    func mergeCard(_ cardID: String) async throws -> MergeCardResponse {
        // URL-encode the card id so a slash or space can't break the route.
        let encoded = cardID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? cardID
        return try await post("/v1/review/\\(encoded)/merge",
                              body: ["confirm": true],
                              as: MergeCardResponse.self)
    }

    /// POST /v1/template/apply — apply a cluster template.
    ///
    /// Body: `{ name, force_recreate?, confirm: true }`. A blocked or partially
    /// applied template returns a non-2xx / `success: false`, which throws and
    /// is surfaced as a failure (never a success checkmark). Never fires without
    /// the confirm UI gate.
    func applyTemplate(name: String,
                       forceRecreate: Bool = false) async throws -> TemplateApplyResponse {
        var payload: [String: Any] = ["name": name, "confirm": true]
        if forceRecreate {
            payload["force_recreate"] = true
        }
        return try await post("/v1/template/apply", body: payload, as: TemplateApplyResponse.self)
    }

    /// POST /v1/cluster/stop — stop a running workload.
    ///
    /// Body: `{ container_id, confirm: true }`. A failure throws and is surfaced
    /// as a failure. Never fires without the confirm UI gate.
    func stopCluster(containerID: String) async throws -> StopClusterResponse {
        return try await post("/v1/cluster/stop",
                              body: ["container_id": containerID, "confirm": true],
                              as: StopClusterResponse.self)
    }
}
