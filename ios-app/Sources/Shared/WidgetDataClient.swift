import Foundation

// ---------------------------------------------------------------------------
// WidgetDataClient.swift — the ONE read-only HTTP client for the extension
// surfaces: widget timeline providers, and the app-side drivers that feed a
// Live Activity's ContentState.
//
// The 2026-11-17 pivot made ios-app a widget + Live Activity client
// (`docs/WIDGET_DATA_CONTRACT.md` is the authority on what may be read and what
// may be rendered). This file is the only path from an extension to the cluster,
// and it is READ-ONLY BY CONSTRUCTION, three ways over:
//
//   1. it can build exactly one HTTP method — GET. `httpMethod` is a literal
//      below; there is no body parameter and no method-name parameter anywhere
//      in the type, so there is nothing to pass a mutation through.
//   2. it only fetches a path named in `WidgetDataPath.all` — the closed set of
//      routes §3's five datasets are served by. Anything else fails with
//      `.pathNotAllowed` BEFORE a request is built, so a later card cannot reach
//      a mutating route by inventing a string.
//   3. it decodes into the shared payload types (`SharedModels.swift`) and hands
//      them back. It writes nothing: no App Group value, no `Data` blob of a
//      response — persisting the projection rather than the response is
//      `SharedStore.swift`'s job, per §7.
//
// Connection settings and auth are REUSED UNCHANGED, not re-derived. Host + port
// come from `APIConfig.load()` (the App Group suite the app's `SettingsStore`
// writes) and the bearer token from the shared keychain (`KeychainShared`), read
// per client — so every surface talks to the same cluster with the same
// credential the app has. The headers match `HSCCClient` exactly:
// `Authorization: Bearer <token>` + `Accept: application/json`, over plain
// `http://`, because Tailscale is the transport and design §D rules TLS out of
// scope. The token is never logged, never rendered, and never carried in a
// failure value.
//
// WHAT A TIMELINE PROVIDER GETS — the two things a nil-returning GET cannot
// express, and which every widget re-implemented by hand before this file:
//
//   * `WidgetDataFailure` — which kind of failure it was. `nil` cannot tell
//     "not configured" from "cluster down" from "the server answered and we
//     could not read it", and those three need different widgets: the first
//     invites setup, the second shows last-known data with its age, the third
//     says the app and the cluster disagree. `showsStaleData` is where that rule
//     lives, so no surface can invent its own (t_5c554c5b: an UNCONFIGURED app
//     must never render as "can't reach the cluster").
//   * `WidgetDataOutcome` + `WidgetData.read(using:lastKnown:_:)` — the ONE
//     stale-while-error rule: a successful read is `.live`; a failure that is
//     allowed to fall back yields `.stale(value, ageMinutes:, reason:)` from the
//     caller's snapshot; anything else is `.failed`. A provider switches on the
//     outcome and never has to decide whether a stale tile is honest.
//
// `timeout` is a per-request wall-clock ceiling, not URLSession's 60 s default:
// an extension that overruns its own budget is killed silently (§2), which is a
// worse outcome than a `.timeout` a surface can render.
// ---------------------------------------------------------------------------

/// The closed set of routes the five widget datasets are served by (§3), and
/// nothing else. A read that is not listed here is refused — that is what makes
/// "no mutation calls at all" a property of this file rather than a promise.
enum WidgetDataPath {
    /// Dataset 1 — projects (§5.1, `reuse`).
    static let projects = "/v1/projects"
    /// Dataset 2 — board status counts (§5.2, §6). The one route no older
    /// endpoint can serve; `done` lives here or nowhere.
    static let mobileSummary = "/v1/mobile/summary"
    /// Datasets 3 + 4 — the kanban trio (§5.3, §5.4). Also the ONLY sanctioned
    /// card-list route: §8.3 forbids `/v1/cards` in an extension.
    static let kanbanRunning = "/v1/kanban/running"
    static let kanbanBlocked = "/v1/kanban/blocked"
    static let kanbanStale = "/v1/kanban/stale"
    /// Dataset 5 — cluster monitor (§5.5): the per-node sample, the daemon-host
    /// sidecar, and the two fleet-state reads a `large` cluster tile renders.
    static let clusterMonitor = "/v1/cluster/monitor"
    static let clusterStatus = "/v1/cluster/status"
    /// `GET /v1/cluster/history` — the recorder's stored series (the one route
    /// here that answers about the past instead of the present).
    static let clusterHistory = "/v1/cluster/history"
    static let daemonHost = "/v1/daemon/host"
    static let autodownStatus = "/v1/autodown/status"
    /// Dataset 6 — `GET /v1/metrics/history` (t_5ae8cfcf): the recorder's three
    /// tiers, so a surface can chart history instead of re-reading one
    /// snapshot. `tier` is required by the route (it answers 400 without it)
    /// because which resolution a chart draws is the one thing a client may not
    /// guess. This read is a surface of its own: §2 budgets ≤ 2 requests per
    /// surface per refresh, and the cluster surface already spends both.
    static let metricsHistory = "/v1/metrics/history"

    /// Every path this client will fetch. Additive only, and only for a read.
    static let all: Set<String> = [
        projects, mobileSummary,
        kanbanRunning, kanbanBlocked, kanbanStale,
        clusterMonitor, clusterStatus, clusterHistory,
        daemonHost, autodownStatus,
        metricsHistory,
    ]

    static func isReadPath(_ path: String) -> Bool { all.contains(path) }
}

/// The read-only client. Construct one per timeline refresh (it is a value type
/// holding a session), read the datasets, hand the decoded payloads to the
/// surface's own projection — and throw the response away.
struct WidgetDataClient {
    /// Per-request ceiling for a read an extension performs.
    ///
    /// 8 s, measured against the payloads §4 sizes: the largest read a surface
    /// makes is 10.7 KB (`cluster/monitor`, up-state) and the budget is ≤ 2
    /// requests per refresh (§2). 8 s each leaves an extension room to decode,
    /// project and render inside its own wall-clock ceiling instead of being
    /// killed mid-load. It is also long enough that a slow-but-working tailnet
    /// link is a `.timeout` a human can read, not a silent blank.
    static let defaultTimeout: TimeInterval = 8

    let config: APIConfig
    /// The ceiling this client puts on every request it builds.
    let timeout: TimeInterval
    private let session: URLSession

    /// `session` is injectable so the headless gate
    /// (`scripts/widget_data_client_check.sh`) can drive this exact type with a
    /// stubbed transport. Production call sites pass nothing.
    init(config: APIConfig,
         timeout: TimeInterval = WidgetDataClient.defaultTimeout,
         session: URLSession? = nil) {
        // Clamped, not trusted: a 0 or negative ceiling would fall through to
        // URLSession's own 60 s default — the value this field exists to avoid.
        let ceiling = timeout > 1 ? timeout : 1
        self.config = config
        self.timeout = ceiling
        self.session = session ?? WidgetDataClient.makeSession(timeout: ceiling)
    }

    /// A client for the shared settings, or nil when the operator has not
    /// configured a target yet (no host, or no token). Nil here is the honest
    /// "invite setup" signal — see `WidgetDataFailure.notConfigured`.
    static func makeIfConfigured(timeout: TimeInterval = WidgetDataClient.defaultTimeout) -> WidgetDataClient? {
        guard let config = APIConfig.load() else { return nil }
        return WidgetDataClient(config: config, timeout: timeout)
    }

    private static func makeSession(timeout: TimeInterval) -> URLSession {
        let configuration = URLSessionConfiguration.default
        // BOTH knobs, because they measure different things:
        // `timeoutIntervalForRequest` is "no bytes for N s" — a trickle resets it,
        // so a slow server could hold an extension open indefinitely;
        // `timeoutIntervalForResource` is the ceiling on the whole load.
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout
        // A refresh must resolve, not wait. `waitsForConnectivity` would hold the
        // request for a route that may never come back and spend the extension's
        // whole budget on a down tailnet.
        configuration.waitsForConnectivity = false
        // Never replay a cached 200 as if it were live: age is rendered as an age,
        // from the snapshot store, or not at all.
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.urlCache = nil
        // The API is bearer-token authenticated and stateless; no cookie should
        // ever ride along with an extension's read.
        configuration.httpShouldSetCookies = false
        return URLSession(configuration: configuration)
    }

    // MARK: - The five datasets (§3), one method each

    /// Dataset 1 — `GET /v1/projects`. `{name, board}` only is decoded
    /// (`ProjectsLite`); `repo` is an absolute local path and is never decoded,
    /// so it can neither be rendered nor reach the App Group container (§7).
    func projects() async -> Result<ProjectsLite, WidgetDataFailure> {
        await load(WidgetDataPath.projects, as: ProjectsLite.self)
    }

    /// Dataset 2 — `GET /v1/mobile/summary`, the §6 compact counts.
    /// `boards` narrows it (§6 rule 2: default is every board, one request);
    /// pass an empty array to ask for all. A response whose `degraded` is true
    /// carried no counts at all — render the snapshot, never zeros (§6 rule 7).
    func mobileSummary(boards: [String] = []) async -> Result<MobileSummaryResponse, WidgetDataFailure> {
        var query: [URLQueryItem] = []
        if !boards.isEmpty {
            query.append(URLQueryItem(name: "boards", value: boards.joined(separator: ",")))
        }
        return await load(WidgetDataPath.mobileSummary, as: MobileSummaryResponse.self, queryItems: query)
    }

    /// Dataset 3 — `GET /v1/kanban/running`. Fleet-wide: `?board=` is IGNORED by
    /// this route (measured, §4.3), so board scope happens client-side and the
    /// param is deliberately not offered here.
    func kanbanRunning() async -> Result<KanbanRunningLite, WidgetDataFailure> {
        await load(WidgetDataPath.kanbanRunning, as: KanbanRunningLite.self)
    }

    /// Dataset 3 — `GET /v1/kanban/blocked`. `why` and `comments` are not
    /// decoded: card prose may quote anything a customer, a codebase or a
    /// credential ever said (§7).
    func kanbanBlocked() async -> Result<KanbanBlockedLite, WidgetDataFailure> {
        await load(WidgetDataPath.kanbanBlocked, as: KanbanBlockedLite.self)
    }

    /// Datasets 3 + 4 — `GET /v1/kanban/stale`. `olderThanDays: 0` is every
    /// non-terminal card, which is already dataset 4's row projection
    /// (title/status/board/age_days, no `body`) — the sanctioned card list.
    func kanbanStale(olderThanDays: Int = 0) async -> Result<KanbanStaleLite, WidgetDataFailure> {
        await load(WidgetDataPath.kanbanStale, as: KanbanStaleLite.self,
                   queryItems: [URLQueryItem(name: "older_than", value: String(max(0, olderThanDays)))])
    }

    /// Dataset 5 — `GET /v1/cluster/monitor`. `output` (54% of the body, an
    /// ANSI table) is not declared in `MonitorResponse`, so it is never decoded.
    /// When the fleet is DOWN the route answers 38 B with no `json` key: that is
    /// a successful read with no nodes, not a transport failure.
    func clusterMonitor() async -> Result<MonitorResponse, WidgetDataFailure> {
        await load(WidgetDataPath.clusterMonitor, as: MonitorResponse.self)
    }

    /// Dataset 5 — `GET /v1/cluster/status` (the `large` cluster family's
    /// host/workload line).
    func clusterStatus() async -> Result<ClusterStatusResponse, WidgetDataFailure> {
        await load(WidgetDataPath.clusterStatus, as: ClusterStatusResponse.self)
    }

    /// Dataset 5 — `GET /v1/cluster/history`, the recorder's stored series: the
    /// only read here that answers about the past instead of the now.
    ///
    /// `window` picks which tier the server reads — `15m` the 2 s ring,
    /// `1h`/`6h`/`24h` the 1-minute roll-ups, `7d` the 5-minute ones — and the
    /// response carries `tier`, `resolution_s`, `resolution`, `tier_summary` and
    /// `max_fields`, because a 2-second point and a 5-minute point must never be
    /// plotted as if they were the same width. `15m` additionally carries
    /// `store.tier0_resident`: that ring is memory-only and belongs to the daemon
    /// process, so a `false` there means "this server cannot see it", which a
    /// chart must render as a gap with a reason, not as a quiet fleet.
    func clusterHistory(window: String = "1h",
                        node: String = "all",
                        metrics: [String] = []) async -> Result<ClusterHistoryResponse, WidgetDataFailure> {
        var query: [URLQueryItem] = [URLQueryItem(name: "window", value: window),
                                     URLQueryItem(name: "node", value: node)]
        if !metrics.isEmpty {
            query.append(URLQueryItem(name: "metrics",
                                      value: metrics.joined(separator: ",")))
        }
        return await load(WidgetDataPath.clusterHistory,
                          as: ClusterHistoryResponse.self, queryItems: query)
    }

    /// Dataset 5 — `GET /v1/daemon/host`, the 439 B sidecar the monitor's
    /// daemon-host line renders.
    func daemonHost() async -> Result<MonitorDaemonHost, WidgetDataFailure> {
        await load(WidgetDataPath.daemonHost, as: MonitorDaemonHost.self)
    }

    /// Dataset 5 — `GET /v1/autodown/status` (fleet power state + idle minutes).
    func autodownStatus() async -> Result<AutodownStatusResponse, WidgetDataFailure> {
        await load(WidgetDataPath.autodownStatus, as: AutodownStatusResponse.self)
    }

    /// Dataset 6 — `GET /v1/metrics/history` (t_5ae8cfcf).
    ///
    /// `tier` is required (0 = the 2 s ring / 15 min, 1 = 1-minute buckets /
    /// 24 h, 2 = 5-minute buckets / 7 d) and is clamped into range rather than
    /// forwarded raw: an out-of-range tier is a coding bug in the surface, and a
    /// 400 on every refresh is a worse outcome than the chart drawing the
    /// nearest real tier. `host` filters node rows, `node` filters engine rows
    /// — the two names the same subject has in the store's two tables.
    /// `since`/`until` are inclusive epoch-second bucket starts.
    ///
    /// A caller drawing the live 2 s window must check
    /// ``MetricsHistoryResponse/hasLiveTier0``: the ring lives in the daemon's
    /// memory, so an empty tier-0 `hosts` with a real `tier0.points` is a
    /// process boundary, not an idle fleet.
    func metricsHistory(tier: Int, host: String? = nil, node: String? = nil,
                        since: Int? = nil,
                        until: Int? = nil) async -> Result<MetricsHistoryResponse, WidgetDataFailure> {
        var query: [URLQueryItem] = [
            URLQueryItem(name: "tier", value: String(min(max(tier, 0), 2)))
        ]
        if let host, !host.isEmpty {
            query.append(URLQueryItem(name: "host", value: host))
        }
        if let node, !node.isEmpty {
            query.append(URLQueryItem(name: "node", value: node))
        }
        if let since {
            query.append(URLQueryItem(name: "since", value: String(since)))
        }
        if let until {
            query.append(URLQueryItem(name: "until", value: String(until)))
        }
        return await load(WidgetDataPath.metricsHistory,
                          as: MetricsHistoryResponse.self, queryItems: query)
    }

    // MARK: - Core read

    /// The only place a request is built. Private, and the ONLY non-GET-free
    /// word in this type: everything above is a thin named wrapper over it.
    private func load<T: Decodable>(_ path: String, as type: T.Type,
                                    queryItems: [URLQueryItem] = []) async -> Result<T, WidgetDataFailure> {
        guard WidgetDataPath.isReadPath(path) else {
            // Refused before a URL is built, so a mutating path never reaches the
            // transport — no request, no side effect, an honest failure.
            return .failure(.pathNotAllowed(path: path))
        }

        let hosts = config.hosts
        let attemptCeiling = HostFailover.attemptTimeout(base: timeout,
                                                         hostCount: hosts.count)
        let outcome = await HostFailover.run(hosts: hosts) { host -> HostFailover.Attempt<Result<T, WidgetDataFailure>> in
            guard let url = self.url(for: path, host: host, queryItems: queryItems) else {
                // A bad address is a transport-class failure: try the next one
                // rather than condemning the whole cluster on one typo.
                return .transport(nil)
            }

            var request = URLRequest(url: url)
            request.httpMethod = "GET"                       // the only method in this file
            request.timeoutInterval = attemptCeiling
            request.cachePolicy = .reloadIgnoringLocalCacheData
            request.setValue("Bearer \(self.config.token)", forHTTPHeaderField: "Authorization")
            request.setValue("application/json", forHTTPHeaderField: "Accept")

            do {
                let (data, response) = try await self.session.data(for: request)
                guard let http = response as? HTTPURLResponse else {
                    // A non-HTTP response is "we did not get to the API", not a
                    // status the surface can render.
                    return .answered(.failure(.unreachable(code: nil)))
                }
                guard (200...299).contains(http.statusCode) else {
                    // ANY HTTP answer proves the daemon is reachable. A 401/404
                    // or 5xx must be surfaced as-is, never retried on another
                    // address (same token, same daemon).
                    return .answered(.failure(Self.httpFailure(status: http.statusCode,
                                                               body: data, path: path)))
                }
                do {
                    return .answered(.success(try JSONDecoder().decode(T.self, from: data)))
                } catch {
                    // A 2xx we cannot read. The detail is kept for the gate/diagnostics
                    // ONLY — `shortLabel` and `operatorLine` never interpolate it, for
                    // the same reason `HSCCError.decoding` does not: it is internal
                    // symbol noise, and a decode failure of a 2xx body means the app
                    // and the cluster disagree on the schema.
                    return .answered(.failure(.decoding(detail: String(describing: error))))
                }
            } catch {
                // Transport only: timeout, refused, DNS, TLS/connection lost.
                return .transport(error)
            }
        }

        switch outcome {
        case .success(let result, _):
            return result
        case .failed(let lastError):
            // Every address failed the transport. ONE failure leaves this
            // function, so a surface renders one honest line, not N errors.
            if let error = lastError as? URLError, error.code == .timedOut {
                return .failure(.timeout(seconds: attemptCeiling))
            }
            if lastError == nil { return .failure(.invalidURL) }
            // `code` is a URLError integer — a diagnostic, never an address, and
            // never the token. Nothing here may carry a LAN/tailnet host (§7).
            return .failure(.unreachable(code: (lastError as? URLError)?.code.rawValue))
        }
    }

    /// Nil-returning read for the two pre-pivot providers
    /// (`ClusterWidget`, `MonitorWidget`), which treat a failed read as "nil this
    /// field, keep the rest of the surface". New code should take the typed
    /// result: `nil` is exactly the ambiguity `WidgetDataFailure` removes.
    /// RETIRE (widget pivot): goes when those two providers take outcomes.
    func get<T: Decodable>(_ path: String, as type: T.Type,
                           queryItems: [URLQueryItem] = []) async -> T? {
        switch await load(path, as: type, queryItems: queryItems) {
        case .success(let value): return value
        case .failure: return nil
        }
    }

    // MARK: - URL + HTTP status

    /// Built the same way `HSCCClient` builds it: `http` scheme (Tailscale is the
    /// transport), caller-supplied host + port, query items through
    /// `URLQueryItem` so a `?` in a path can never be percent-encoded into the
    /// path itself.
    private func url(for path: String, host: String,
                     queryItems: [URLQueryItem]) -> URL? {
        var components = URLComponents()
        components.scheme = "http"
        components.host = host
        components.port = config.port
        components.path = path
        if !queryItems.isEmpty { components.queryItems = queryItems }
        return components.url
    }

    /// Status → failure, keeping the app's semantics: 401/403 is a token problem
    /// the operator can fix, 404 is a route this build does not have (`/v1/mobile
    /// /summary` answers 404 until PR #310 deploys — a real case, not a
    /// hypothesis), anything else is the server's.
    private static func httpFailure(status: Int, body: Data, path: String) -> WidgetDataFailure {
        switch status {
        case 401, 403: return .unauthorized
        case 404: return .notFound(path: path)
        default: return .http(status: status, code: errorCode(from: body))
        }
    }

    /// The API's unified error envelope is `{ "error": { "code", "message",
    /// "speak" } }`. Only `code` is kept: `message`/`speak` are server prose and
    /// §7 keeps server text out of an extension's storage and out of a widget's
    /// two lines of text.
    private static func errorCode(from body: Data) -> String? {
        struct Envelope: Decodable {
            struct Inner: Decodable { let code: String? }
            let error: Inner?
        }
        return (try? JSONDecoder().decode(Envelope.self, from: body))?.error?.code
    }
}

// ---------------------------------------------------------------------------
// The failure a surface can render
// ---------------------------------------------------------------------------

/// What went wrong reading the cluster, in the kinds a widget or a Live Activity
/// has to distinguish. Deliberately contains NO server prose, NO address and NO
/// token — a value of this type is safe to render and safe to hold.
enum WidgetDataFailure: Error, Equatable {
    /// No host, or no token in the shared keychain. Not a connectivity problem.
    case notConfigured
    /// The saved host/port cannot form a URL (a settings problem, not the cluster's).
    case invalidURL
    /// A path outside `WidgetDataPath.all` — a coding problem in this app, and
    /// proof the read-only rule is enforced rather than intended.
    case pathNotAllowed(path: String)
    /// The per-request ceiling was hit. `seconds` is the ceiling that expired.
    case timeout(seconds: TimeInterval)
    /// Never got to the API: refused, DNS, no route, connection lost. `code` is
    /// a URLError integer for diagnostics; it is never an address.
    case unreachable(code: Int?)
    /// 401/403 — the cluster is reachable and the token is not good.
    case unauthorized
    /// 404 — the route is not on this cluster (an app/cluster version skew).
    case notFound(path: String)
    /// Any other HTTP status. `code` is the API's error slug when it sent one.
    case http(status: Int, code: String?)
    /// A 2xx body that does not match the payload type. This is the case that
    /// must NEVER render as a zero: a missing required key (§6 rule 1) lands
    /// here, and the surface keeps its last-known value instead.
    case decoding(detail: String)
}

extension WidgetDataFailure {
    /// Whether a surface should keep showing its last-known snapshot with an age.
    ///
    /// The rule that used to live in each widget, in one place:
    ///   * `.notConfigured` and `.invalidURL` → NO. The operator has not given us
    ///     a target; dressing that up as "can't reach the cluster" is a lie about
    ///     whose problem it is (the fix t_5c554c5b made in `ClusterWidget`, now
    ///     structural so no new surface can get it wrong).
    ///   * `.pathNotAllowed` → NO. A bug in this app is not the cluster's outage,
    ///     and stale tiles would hide it.
    ///   * everything else → YES. The data we have is real and its age is
    ///     rendered next to it; that is stale-while-error.
    var showsStaleData: Bool {
        switch self {
        case .notConfigured, .invalidURL, .pathNotAllowed: return false
        case .timeout, .unreachable, .unauthorized, .notFound, .http, .decoding: return true
        }
    }

    /// The one short string the accessory families can afford (≤ ~15 characters).
    /// Never a number, never "0", never server prose.
    var shortLabel: String {
        switch self {
        case .notConfigured: return "Set up in HSCC"
        case .invalidURL: return "Bad host/port"
        case .pathNotAllowed: return "Read not allowed"
        case .timeout: return "Timed out"
        case .unreachable: return "No connection"
        case .unauthorized: return "Token rejected"
        case .notFound: return "Route missing"
        case .http(let status, _): return "Server \(status)"
        case .decoding: return "Can't read data"
        }
    }

    /// One honest sentence for the small/medium/large footer line: what happened
    /// and what the operator can do. Mirrors `HSCCError.localizedDescription`'s
    /// tone, shortened for a widget, and — like it — never interpolates a
    /// decoding detail or a server message.
    var operatorLine: String {
        switch self {
        case .notConfigured:
            return "Add the cluster host, port and token in HSCC to see live data."
        case .invalidURL:
            return "The saved host or port is not a valid address — fix it in HSCC settings."
        case .pathNotAllowed:
            return "This surface asked for a read the app is not allowed to make."
        case .timeout(let seconds):
            return "The cluster did not answer within \(Int(seconds)) seconds."
        case .unreachable:
            return "Can't reach the cluster — is Tailscale connected?"
        case .unauthorized:
            return "The cluster rejected the token — check it in HSCC settings."
        case .notFound(let path):
            return "This cluster has no \(path) route — the app and cluster versions disagree."
        case .http(let status, let code):
            if let code { return "The cluster returned an error (\(status): \(code))." }
            return "The cluster returned an error (\(status))."
        case .decoding:
            return "The cluster returned something this app can't read — likely an app/cluster version mismatch."
        }
    }
}

// ---------------------------------------------------------------------------
// The one stale-while-error rule a timeline provider switches on
// ---------------------------------------------------------------------------

/// The result of one widget read, already resolved against the surface's
/// last-known snapshot: fresh data, real data with its age, or a failure a
/// surface can name. There is no `.blank` case, because a blank surface is never
/// the answer — `failed` carries what to say instead.
enum WidgetDataOutcome<Value> {
    /// Read now, from the cluster.
    case live(Value, fetchedAt: Date)
    /// Real data, fetched `ageMinutes` ago, kept because this read failed in a
    /// way that honest staleness covers. `reason` says why it is not live.
    case stale(Value, ageMinutes: Int, reason: WidgetDataFailure)
    /// Nothing usable. The surface renders `failure.shortLabel` /
    /// `.operatorLine`, or the setup invite for `.notConfigured`.
    case failed(WidgetDataFailure)

    /// The value to draw, live or stale. nil means render the failure.
    var value: Value? {
        switch self {
        case .live(let value, _), .stale(let value, _, _): return value
        case .failed: return nil
        }
    }

    /// Minutes since the value was fetched; nil when it was just now (so a view
    /// can show an age only when there is one).
    var ageMinutes: Int? {
        switch self {
        case .live: return nil
        case .stale(_, let minutes, _): return minutes
        case .failed: return nil
        }
    }

    var isStale: Bool {
        if case .stale = self { return true }
        return false
    }

    var failure: WidgetDataFailure? {
        if case .failed(let failure) = self { return failure }
        return nil
    }
}

/// The stale-while-error path every timeline provider uses, so "fall back to the
/// snapshot" is one rule with one age formula instead of five hand-rolled ones.
enum WidgetData {
    /// Run one read and resolve it against the caller's last-known value.
    ///
    ///   - client: pass `WidgetDataClient.makeIfConfigured()`. A nil client is
    ///     `.failed(.notConfigured)` and — deliberately — NEVER `.stale`: a
    ///     snapshot sitting in the App Group does not mean the operator
    ///     configured a target (t_5c554c5b).
    ///   - lastKnown: the surface's own projection read (`SnapshotStore`,
    ///     `MonitorSnapshotStore`, …). It is consulted only when
    ///     `failure.showsStaleData` is true, and the caller decides which value
    ///     it may keep — the client never reads or writes the container.
    ///   - body: one dataset read, e.g. `{ await $0.kanbanRunning() }` (the
    ///     closure is `async`, so the `await` is written, not implied).
    static func read<Value>(
        using client: WidgetDataClient?,
        lastKnown: () -> (value: Value, at: Date)? = { nil },
        _ body: (WidgetDataClient) async -> Result<Value, WidgetDataFailure>
    ) async -> WidgetDataOutcome<Value> {
        guard let client else { return .failed(.notConfigured) }
        switch await body(client) {
        case .success(let value):
            return .live(value, fetchedAt: Date())
        case .failure(let failure) where failure.showsStaleData:
            guard let last = lastKnown() else { return .failed(failure) }
            return .stale(last.value, ageMinutes: ageMinutes(from: last.at), reason: failure)
        case .failure(let failure):
            return .failed(failure)
        }
    }

    /// Minutes since `date`, floored at zero — the same age the widgets render
    /// today, so a Live Activity and a widget cannot disagree about how old a
    /// fact is.
    static func ageMinutes(from date: Date) -> Int {
        max(0, Int(Date().timeIntervalSince(date) / 60))
    }
}

// ---------------------------------------------------------------------------
// Compatibility alias — RETIRE (widget pivot)
// ---------------------------------------------------------------------------
//
// The pre-pivot extension client was `ExtensionClient`: 57 lines, GET-only, a
// hardcoded 15 s timeout and a nil-on-any-failure `get`. It is gone rather than
// kept beside this one, because two clients for one surface is how the timeout
// and the failure semantics end up disagreeing between a widget and a Live
// Activity. The alias keeps `ClusterWidget`/`MonitorWidget` (and any widget card
// still mid-flight) compiling unchanged while they move to the typed reads.
typealias ExtensionClient = WidgetDataClient
