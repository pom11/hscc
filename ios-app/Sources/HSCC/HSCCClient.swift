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
}

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

    /// Build a URL with explicit query items (never embedded in `path`).
    ///
    /// The plain `url(for:)` used by the core `get` sets `components.path` to
    /// the whole string; if that string contains a literal `?` it is
    /// percent-encoded into `%3F` and silently becomes part of the path. Query
    /// parameters that matter — like a paging `before` cursor — must go
    /// through `URLQueryItem`, which percent-encodes name/value and keeps the
    /// path clean.
    private func url(for path: String, queryItems: [URLQueryItem]) throws -> URL {
        var components = URLComponents()
        components.scheme = "http"
        components.host = host
        components.port = port
        components.path = path
        components.queryItems = queryItems
        guard let url = components.url else {
            throw HSCCError.invalidURL
        }
        return url
    }

    /// Percent-encode a single path segment (project names etc.) so it is safe
    /// to interpolate into a URL path.
    func pathComponent(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }

    // MARK: - Request construction

    /// Build a request. `timeout` overrides URLSession's 60s default — the
    /// orchestrator chat shells out to `hermes chat` and a real answer takes
    /// 30-90s (measured floor: 16.8s for a two-word reply), so the default
    /// would abort a request the server is still successfully working on.
    private func request(for path: String, timeout: TimeInterval? = nil) throws -> URLRequest {
        try request(for: try url(for: path), timeout: timeout)
    }

    /// Build a request from an already-constructed `URL` (used by the
    /// query-string GET variant).
    private func request(for url: URL, timeout: TimeInterval? = nil) throws -> URLRequest {
        var req = URLRequest(url: url)
        if let timeout { req.timeoutInterval = timeout }
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
                let decoded = try Self.decoder.decode(T.self, from: data)
                // Persist as last-known state so an unreachable cluster can
                // still surface this data, clearly marked stale (offline
                // feature). Only successful reads are cached — never a failure.
                StateCache.store(data, for: path)
                return decoded
            } catch {
                throw HSCCError.decoding(String(describing: error))
            }
        }

        // Non-2xx: attempt to decode the unified error shape.
        throw Self.error(from: data, status: status)
    }

    /// Perform a GET with explicit query items and decode into `T`.
    ///
    /// Same semantics as `get(_:as:)` but builds the URL via `URLQueryItem`s
    /// (see `url(for:queryItems:)`). The response is cached under the plain
    /// `path` ONLY when `queryItems` is empty — i.e. the newest-page (tail)
    /// read — so the offline last-known cache always holds the freshest page
    /// and a paging read never clobbers it.
    func get<T: Decodable>(path: String,
                           queryItems: [URLQueryItem],
                           as type: T.Type = T.self) async throws -> T {
        let req = try request(for: try url(for: path, queryItems: queryItems))
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
                let decoded = try Self.decoder.decode(T.self, from: data)
                if queryItems.isEmpty {
                    StateCache.store(data, for: path)
                }
                return decoded
            } catch {
                throw HSCCError.decoding(String(describing: error))
            }
        }

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
                            as type: T.Type = T.self,
                            timeout: TimeInterval? = nil) async throws -> T {
        var req = try request(for: path, timeout: timeout)
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

    // MARK: - Offline last-known-state cache accessors

    /// The last-known decoded value for a read `path` (e.g. "/v1/projects"),
    /// or nil if that endpoint was never fetched successfully. Used by
    /// `Offline.load` to show stale data when the cluster is unreachable.
    func cachedValue<T: Decodable>(_ type: T.Type, for path: String) -> T? {
        StateCache.value(type, for: path)
    }

    /// The age in seconds of the last-known value for `path`, or nil if never
    /// fetched.
    func cacheAge(for path: String) -> TimeInterval? {
        StateCache.age(for: path)
    }

    /// Whether `path` was ever fetched successfully (has last-known data).
    func hasCache(for path: String) -> Bool {
        StateCache.hasValue(for: path)
    }

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
        try await get(path: "/v1/fleet/stats",
                      queryItems: [URLQueryItem(name: "days", value: String(days))],
                      as: FleetStatsResponse.self)
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

    // MARK: - C6 reads (autodown / projects / ops / board hygiene / fleet control)

    /// GET /v1/autodown/status — the autodown report (operator's most-used surface).
    func autodownStatus() async throws -> AutodownStatusResponse {
        try await get("/v1/autodown/status", as: AutodownStatusResponse.self)
    }

    /// GET /v1/projects — the registry list.
    func projects() async throws -> ProjectsResponse {
        try await get("/v1/projects", as: ProjectsResponse.self)
    }

    /// GET /v1/projects/{name} — per-project detail.
    func projectDetail(_ name: String) async throws -> ProjectDetailResponse {
        try await get("/v1/projects/\(name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name)",
                      as: ProjectDetailResponse.self)
    }

    /// GET /v1/verify — same shape as /v1/health ({ok, checks, speak}).
    func verify() async throws -> VerifyResponse {
        try await get("/v1/verify", as: VerifyResponse.self)
    }

    /// GET /v1/daemon/status — daemon + every health stream.
    func daemonStatus() async throws -> DaemonStatusResponse {
        try await get("/v1/daemon/status", as: DaemonStatusResponse.self)
    }

    /// GET /v1/triggers — trigger rules + last run + recent events.
    func triggers() async throws -> TriggersResponse {
        try await get("/v1/triggers", as: TriggersResponse.self)
    }

    /// GET /v1/escalate — pending escalations (read-only).
    func escalations() async throws -> EscalationsResponse {
        try await get("/v1/escalate", as: EscalationsResponse.self)
    }

    /// POST /v1/triggers/run — force re-evaluate all trigger rules now.
    /// Body: `{ confirm: true }`. Returns the fresh read state after the run.
    func triggersRun() async throws -> TriggersResponse {
        try await post("/v1/triggers/run", body: ["confirm": true],
                       as: TriggersResponse.self)
    }

    /// POST /v1/escalate — actually perform pending failure escalations
    /// (reassign + notify), not the read-only dry-run.
    /// Body: `{ confirm: true }`. Returns the actions taken.
    func escalateRun() async throws -> EscalationsResponse {
        try await post("/v1/escalate", body: ["confirm": true],
                       as: EscalationsResponse.self)
    }

    /// GET /v1/profiles — running task counts per profile.
    func profiles() async throws -> ProfilesResponse {
        try await get("/v1/profiles", as: ProfilesResponse.self)
    }

    // MARK: - Profile editor (per-project profile read / edit)

    /// GET /v1/profile/editor/{profile} — read a profile's editable fields.
    ///
    /// The per-project editor targets the orchestrator's `<project>-orch`
    /// profile (the project's bot). Read-only (no `confirm`). The profile
    /// name is URL-encoded so a slash can't break the path.
    func profileEditor(profile: String) async throws -> ProfileEditorResponse {
        let encoded = profile.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? profile
        return try await get("/v1/profile/editor/\(encoded)", as: ProfileEditorResponse.self)
    }

    /// POST /v1/profile/editor/{profile} — edit a profile's editable fields.
    ///
    /// Body: `{ model?, provider?, toolsets?, preload_skills?, description?,
    /// compression?, confirm: true }`. Only supplied fields are written; the
    /// rest of each YAML file is preserved verbatim. Confirm-gated — the
    /// editor passes `confirm: true` only after the operator hits save.
    func updateProfile(_ profile: String,
                       model: String? = nil,
                       provider: String? = nil,
                       toolsets: [String]? = nil,
                       preloadSkills: [String]? = nil,
                       description: String? = nil,
                       compression: [String: Any]? = nil) async throws -> ProfileEditorResponse {
        let encoded = profile.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
            ?? profile
        var body: [String: Any] = ["confirm": true]
        if let model { body["model"] = model }
        if let provider { body["provider"] = provider }
        if let toolsets { body["toolsets"] = toolsets }
        if let preloadSkills { body["preload_skills"] = preloadSkills }
        if let description { body["description"] = description }
        if let compression { body["compression"] = compression }
        return try await post("/v1/profile/editor/\(encoded)", body: body,
                              as: ProfileEditorResponse.self)
    }

    // MARK: - Sessions manager (list / retire / compact)

    /// GET /v1/sessions?profile=<name> — a profile's sessions with message
    /// count, token totals, compaction signals + headroom, and a bloat verdict.
    ///
    /// Read-only (no `confirm`). Each row's `bloated`/`reason` come from the
    /// SAME verdict the orchestrator bloat-guard uses. Requires the profile
    /// whose state.db to inspect; the profile name is URL-encoded so a slash
    /// or space can't break the query string.
    func sessions(profile: String) async throws -> SessionsListResponse {
        // `?` in a path string is percent-encoded to %3F by `components.path`,
        // and "\\(" is an ESCAPED backslash — not interpolation — so this used to
        // send the literal text \(encoded) and no profile filter at all.
        return try await get(path: "/v1/sessions",
                             queryItems: [URLQueryItem(name: "profile", value: profile)],
                             as: SessionsListResponse.self)
    }

    /// POST /v1/sessions/{id}/retire — non-destructive retirement.
    ///
    /// Body: `{ profile, confirm: true }`. Retitles the session to
    /// `<title>-retired-<ts>` so it drops out of the live list while its full
    /// history stays on disk. Requires explicit profile + the confirm UI gate
    /// (MutationButton) before calling.
    func retireSession(id: String, profile: String) async throws -> SessionMutationResponse {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return try await post("/v1/sessions/\(encoded)/retire",
                              body: ["profile": profile, "confirm": true],
                              as: SessionMutationResponse.self)
    }

    /// POST /v1/sessions/{id}/compact — re-arm native compaction.
    ///
    /// Body: `{ profile, confirm: true }`. KEEPS the session (continuity
    /// preserved): clears the compaction-failure latch so Hermes' own
    /// compressor retakes the floor on its next turn and shrinks it for real.
    /// Requires explicit profile + the confirm UI gate before calling.
    func compactSession(id: String, profile: String) async throws -> SessionMutationResponse {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        return try await post("/v1/sessions/\(encoded)/compact",
                              body: ["profile": profile, "confirm": true],
                              as: SessionMutationResponse.self)
    }

    // MARK: - Memory viewer (list / correct / delete)

    /// GET /v1/memory?profile=<name> — a profile's memory cards.
    ///
    /// Read-only (no `confirm`). Returns every card the agent remembers, each
    /// with its stable graph node id (`memory:<memory|profile>:<index>`) that
    /// the operator passes back to correct/delete. The profile whose memories
    /// to inspect is chosen by the operator in the view's field at the top.
    func memories(profile: String) async throws -> MemoryListResponse {
        // Same defect as sessions(profile:): escaped backslash, not interpolation,
        // plus a literal `?` the path setter percent-encodes.
        return try await get(path: "/v1/memory",
                             queryItems: [URLQueryItem(name: "profile", value: profile)],
                             as: MemoryListResponse.self)
    }

    /// POST /v1/memory/{node_id}/delete — delete one memory card.
    ///
    /// Body: `{ profile, confirm: true }`. Permanently removes the card from
    /// the profile's memory file. Requires the confirm UI gate (MutationButton)
    /// before calling — this is destructive.
    func deleteMemory(nodeID: String, profile: String) async throws -> MemoryMutationResponse {
        let encoded = nodeID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? nodeID
        return try await post("/v1/memory/\(encoded)/delete",
                              body: ["profile": profile, "confirm": true],
                              as: MemoryMutationResponse.self)
    }

    /// POST /v1/memory/{node_id}/edit — correct one memory card.
    ///
    /// Body: `{ profile, content, confirm: true }`. Replaces the card's content
    /// with the corrected text. Requires the confirm UI gate before calling.
    func editMemory(nodeID: String, profile: String,
                    content: String) async throws -> MemoryMutationResponse {
        let encoded = nodeID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? nodeID
        return try await post("/v1/memory/\(encoded)/edit",
                              body: ["profile": profile,
                                     "content": content,
                                     "confirm": true],
                              as: MemoryMutationResponse.self)
    }

    /// GET /v1/kanban/blocked — blocked cards across all boards.
    func kanbanBlocked() async throws -> KanbanBlockedResponse {
        try await get("/v1/kanban/blocked", as: KanbanBlockedResponse.self)
    }

    /// GET /v1/kanban/stale?older_than=N — non-terminal cards (0 = all).
    func kanbanStale(olderThan: Int = 0) async throws -> KanbanStaleResponse {
        try await get(path: "/v1/kanban/stale",
                      queryItems: [URLQueryItem(name: "older_than", value: String(olderThan))],
                      as: KanbanStaleResponse.self)
    }

    // MARK: - Live agent activity feed

    /// GET /v1/activity/feed — the live agent activity feed (flight recorder).
    ///
    /// Read-only (no `confirm`): who is running, which tool they just called,
    /// on which card — newest first. ``limit`` (default 50, capped at 200 by
    /// the server) bounds the returned entries. Each entry carries the
    /// ``profile``, ``card_id`` and ``session_id`` an operator uses to
    /// tap-to-trace.
    func activityFeed(limit: Int = 50) async throws -> ActivityFeedResponse {
        // A literal `?` in the path is percent-encoded to %3F by
        // `components.path = path`, so the server never sees the query and
        // `limit` was silently ignored. Pass it as a real query item.
        try await get(path: "/v1/activity/feed",
                      queryItems: [URLQueryItem(name: "limit", value: String(limit))],
                      as: ActivityFeedResponse.self)
    }

    /// GET /v1/projects/{name}/session/events — page the project's chat log.
    ///
    /// On open (no `before`) this fetches the NEWEST page (the tail) so the
    /// operator sees context that predates this install — the thing that makes
    /// a project's chat a SESSION, not a log the app happens to own. Pass
    /// `before` = the returned `next_before` to page further BACK (strictly
    /// older seq). `limit` defaults to the server's 200 and is capped at 1000.
    ///
    /// Query parameters are built with `URLQueryItem` (not embedded in the
    /// path) so `before`/`limit` survive percent-decoding exactly — unlike a
    /// literal `?` in the path string, which URLComponents would encode as
    /// `%3F` and turn into a wrong path.
    func sessionEvents(project: String,
                       before: Int? = nil,
                       limit: Int = 200) async throws -> SessionHistoryResponse {
        var query: [URLQueryItem] = []
        if let before { query.append(URLQueryItem(name: "before", value: String(before))) }
        query.append(URLQueryItem(name: "limit", value: String(limit)))
        return try await get(path: "/v1/projects/\(pathComponent(project))/session/events",
                             queryItems: query,
                             as: SessionHistoryResponse.self)
    }

    /// GET /v1/template/list — available cluster templates.
    func templateList() async throws -> TemplateListResponse {
        try await get("/v1/template/list", as: TemplateListResponse.self)
    }

    /// GET /v1/template/status — the currently-applied template.
    func templateStatus() async throws -> TemplateStatusResponse {
        try await get("/v1/template/status", as: TemplateStatusResponse.self)
    }

    /// GET /v1/template/preview/{name} — dry-run of what applying a template
    /// would change (READ-ONLY; never mutates).
    ///
    /// A template with no preview yet returns a minimal `{ speak }` body; one
    /// with a preview returns the full changes + routing. Unknown template →
    /// 404. This is the browse/preview step BEFORE any confirmed apply.
    func templatePreview(name: String) async throws -> TemplatePreviewResponse {
        // Inline the encoding expression (matches `cardDetail`/`reviewDetail`
        // so a slash or space can't break the route).
        let encoded = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        return try await get("/v1/template/preview/\(encoded)", as: TemplatePreviewResponse.self)
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
        return try await post("/v1/review/\(encoded)/merge",
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

    /// POST /v1/orchestrator/chat — START an async orchestrator chat job.
    ///
    /// Body: `{ project: "<name>"|null, prompt: "...", confirm: true }`.
    /// This is a MUTATION: the orchestrator can decompose the prompt and
    /// dispatch real work onto its board, so `confirm` is ALWAYS sent as `true`
    /// here and the caller (the chat view) MUST gate the call behind the same
    /// explicit confirm UI as every other mutating surface before calling.
    ///
    /// `project` may be `nil` (or `"general"`) → the catch-all `general`
    /// orchestrator (`general-orch` / `general` session / `default` board).
    /// Unknown project → 400; missing/empty prompt → 400; missing confirm →
    /// 409.
    ///
    /// Returns **immediately** (202 Accepted) with a `job_id` — NOT the reply.
    /// The actual invocation runs in a background thread on the server; collect
    /// it with `orchestratorChatPoll(jobID:)`, which reports queued/running/
    /// done + honest `elapsed` and the reply once finished. Because the POST
    /// returns in milliseconds (no 90 s dead wait), and the server keeps the
    /// job alive independently of this connection, a dropped/backgrounded
    /// connection no longer loses an answer the server is still computing.
    ///
    /// Honest failures — a non-2xx ALWAYS throws and is surfaced as a failure,
    /// never as a reply:
    ///   * 409 — confirm missing/refused (shouldn't happen here; we always send it)
    ///   * 400 bad_request / unknown_project — bad prompt or unknown project
    /// The timeout/502/503/504 conditions now surface through the JOB's terminal
    /// error state (poll), not as an HTTP error on the POST.
    func orchestratorChatStart(project: String? = nil,
                               prompt: String) async throws -> OrchestratorChatJobResponse {
        var payload: [String: Any] = ["prompt": prompt, "confirm": true]
        if let project, !project.isEmpty {
            payload["project"] = project
        }
        // `project` absent ⇒ the general orchestrator. Always confirms.
        // 30s, not the old 300s: the POST now returns in milliseconds (it only
        // validates + spawns a thread), so a 30s cap is plenty — anything
        // longer means the server itself misbehaved, and the job is still
        // running regardless so nothing is lost.
        return try await post("/v1/orchestrator/chat",
                              body: payload,
                              as: OrchestratorChatJobResponse.self,
                              timeout: 30)
    }

    /// GET /v1/orchestrator/chat/{id} — poll an async chat job.
    ///
    /// Read-only (the orchestrator was already messaged by the POST), so no
    /// `confirm` is required — only bearer auth. Returns the job's current
    /// state: `queued`/`running`, `done` (with `reply`), or a terminal failure
    /// state (`timeout`/`unavailable`/`error`) carrying a unified `error`.
    /// Unknown job id → 404 throws.
    func orchestratorChatPoll(jobID: String) async throws -> OrchestratorChatJobStatus {
        try await get("/v1/orchestrator/chat/\(jobID)", as: OrchestratorChatJobStatus.self)
    }

    // MARK: - C6 mutations (confirm-gated)

    /// POST /v1/autodown/enable — arm idle autodown.
    ///
    /// Body: `{ idle_minutes, force?, confirm: true }`. Non-2xx (409 for a
    /// cron conflict unless force, 400 for a bad idle_minutes) always throws.
    func autodownEnable(idleMinutes: Int, force: Bool = false) async throws -> AutodownEnableResponse {
        var payload: [String: Any] = ["idle_minutes": idleMinutes, "confirm": true]
        if force { payload["force"] = true }
        return try await post("/v1/autodown/enable", body: payload, as: AutodownEnableResponse.self)
    }

    /// POST /v1/autodown/disable — disarm + release the intentional block.
    /// Body: `{ confirm: true }`. Always confirms.
    func autodownDisable() async throws -> AutodownDisableResponse {
        try await post("/v1/autodown/disable", body: ["confirm": true], as: AutodownDisableResponse.self)
    }

    /// POST /v1/autodown/wake — force autoup (running in the background).
    ///
    /// Body: `{ confirm: true }`. Returns promptly with `state: waking`; autoup
    /// can take ~9 minutes. The view polls /v1/autodown/status rather than
    /// blocking. Always confirms.
    func autodownWake() async throws -> AutodownWakeResponse {
        try await post("/v1/autodown/wake", body: ["confirm": true], as: AutodownWakeResponse.self)
    }

    /// POST /v1/autodown/cancel — abort an in-progress teardown.
    /// Body: `{ confirm: true }`. Always confirms.
    func autodownCancel() async throws -> AutodownCancelResponse {
        try await post("/v1/autodown/cancel", body: ["confirm": true], as: AutodownCancelResponse.self)
    }

    /// POST /v1/kanban/blocked/{id}/recover — recover ONE blocked card.
    ///
    /// Body: `{ reason?, confirm: true }`. Non-2xx (404 for a card that isn't
    /// blocked, 502 for a failed recover) always throws. Always confirms.
    func recoverBlockedCard(_ cardID: String, reason: String? = nil) async throws -> RecoverCardResponse {
        let encoded = cardID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? cardID
        var payload: [String: Any] = ["confirm": true]
        if let reason, !reason.isEmpty { payload["reason"] = reason }
        return try await post("/v1/kanban/blocked/\(encoded)/recover",
                              body: payload,
                              as: RecoverCardResponse.self)
    }

    /// POST /v1/cluster/up — bring the whole serving fleet up.
    ///
    /// Body: `{ dry_run?, confirm: true }`. A 502 on a failed up always throws.
    func clusterUp() async throws -> ClusterUpResponse {
        try await post("/v1/cluster/up", body: ["confirm": true], as: ClusterUpResponse.self)
    }

    /// POST /v1/cluster/down — stop ALL workloads fleet-wide.
    ///
    /// Body: `{ confirm: true }`. A 502 on a failed down always throws.
    func clusterDown() async throws -> ClusterDownResponse {
        try await post("/v1/cluster/down", body: ["confirm": true], as: ClusterDownResponse.self)
    }
}
