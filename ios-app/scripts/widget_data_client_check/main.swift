import Foundation

// ===========================================================================
// widget_data_client_check — drives the REAL Sources/Shared/WidgetDataClient
// (compiled verbatim by scripts/widget_data_client_check.sh, together with the
// real SharedModels.swift + SharedStore.swift) through a stubbed transport and
// asserts the four promises the pivot rests on:
//
//   1. READ-ONLY: the fetch allowlist is exactly the ten audited read routes,
//      a mutating-looking path is refused BEFORE the transport sees anything,
//      and every request that does go out is a GET with the app's headers.
//   2. TIMEOUT: the per-request ceiling is on the request (and clamped), so an
//      extension is never left holding URLSession's 60 s default.
//   3. FAILURE TYPES: each transport/status/decode outcome maps to the one
//      `WidgetDataFailure` case a surface can render — including the §6 rule-1
//      case (a 2xx with a missing required key is `.decoding`, never a zero).
//   4. STALE-WHILE-ERROR: `WidgetData.read` yields `.live` / `.stale(value,
//      age, reason)` / `.failed`, and NEVER falls back to a snapshot when the
//      real problem is that nobody configured a target (t_5c554c5b).
//
// It runs on macOS: the client is pure Foundation, and `URLProtocol` is the
// supported way to own the transport. Honest limits, stated rather than hidden:
// the stub transport does not itself enforce `timeoutIntervalForRequest` (that
// is URLSession's job, and it cannot be exercised headlessly) — what is proven
// here is that the client PUTS the deadline on the request and that a timed-out
// load maps to `.timeout`. No network is touched by this harness: every request
// is answered by `StubTransport`.
// ===========================================================================

// MARK: - Stub transport

final class StubTransport: URLProtocol {
    enum Outcome {
        case http(status: Int, body: Data)
        case error(Error)
    }

    /// The next request's fate. Sequential tests, so one slot is enough; the
    /// failover cases arm a SEQUENCE so host 1 can fail and host 2 answer.
    static var outcome: Outcome = .error(URLError(.cannotConnectToHost))
    static var queued: [Outcome] = []
    private(set) static var requests: [URLRequest] = []

    static func arm(_ outcome: Outcome) {
        requests = []
        queued = []
        Self.outcome = outcome
    }

    static func armSequence(_ outcomes: [Outcome]) {
        requests = []
        queued = outcomes
        outcome = outcomes.last ?? .error(URLError(.cannotConnectToHost))
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        StubTransport.requests.append(request)
        let next: Outcome
        if StubTransport.queued.count > 1 {
            next = StubTransport.queued.removeFirst()
        } else {
            next = StubTransport.queued.first ?? StubTransport.outcome
        }
        switch next {
        case .error(let error):
            client?.urlProtocol(self, didFailWithError: error)
        case .http(let status, let body):
            let url = request.url ?? URL(string: "http://cluster.test:8788/")!
            let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: body)
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}
}

func stubSession() -> URLSession {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubTransport.self]
    return URLSession(configuration: configuration, delegate: nil, delegateQueue: nil)
}

// MARK: - Fixtures

/// The real `/v1/projects` shape, including the two keys the widget must never
/// see: `repo` (an absolute path on the operator's machine) and `topic`.
let projectsBody = Data(#"""
{"projects":[{"name":"hscc","board":"hscc","repo":"/srv/hscc/private/checkout","topic":3},
             {"name":"inc","board":"inc","repo":"/srv/hscc/private/inc","topic":7}],
 "count":2,"speak":"2 projects"}
"""#.utf8)

/// §6's counts payload: all six statuses as integers.
let summaryBody = Data(#"""
{"generated_at":"2026-11-17T08:30:00Z",
 "boards":[{"board":"hscc","counts":{"todo":17,"ready":0,"running":3,"blocked":0,
             "done":750,"archived":114},"total":884}],
 "speak":"hscc: 20 open, 3 running, 750 done."}
"""#.utf8)

/// The real `GET /v1/metrics/history?tier=1` shape (t_5ae8cfcf), built the way
/// the recorder writes it: two node buckets where the first answered twice
/// (so the bucket has a mean AND a peak) and the second host did not answer at
/// all (so every metric is null and `error` carries why), plus one engine row.
let metricsHistoryBody = Data(#"""
{"tier":1,"tier_meta":{"tier":1,"resolution_s":60,"window_s":86400,"capacity":1440},
 "since":null,"until":null,"host":null,"node":null,"source":"sqlite_store",
 "hosts":[{"tier":1,"ts":1795000060,"host":"dgx-01","n":2,"error":null,
           "cpu_usage_pct":30.0,"cpu_temp_c":null,"gpu_util_pct":80.0,
           "gpu_temp_c":75.0,"gpu_power_w":160.0,"mem_used_pct":40.0,
           "swap_used_mb":null,"swap_total_mb":null,"swap_used_pct":null,
           "gpu_mem_used_mb":30000.0,"gpu_clock_mhz":1900.0,
           "net_recv_bytes_s":null,"net_send_bytes_s":null,"sparkrun_jobs":3.0,
           "cpu_temp_c_max":null,"gpu_temp_c_max":90.0,"gpu_power_w_max":200.0},
          {"tier":1,"ts":1795000120,"host":"dgx-02","n":1,
           "error":"ssh: collect failed","cpu_usage_pct":null,"cpu_temp_c":null,
           "gpu_util_pct":null,"gpu_temp_c":null,"gpu_power_w":null,
           "mem_used_pct":null,"swap_used_mb":null,"swap_total_mb":null,
           "swap_used_pct":null,"gpu_mem_used_mb":null,"gpu_clock_mhz":null,
           "net_recv_bytes_s":null,"net_send_bytes_s":null,"sparkrun_jobs":null,
           "cpu_temp_c_max":null,"gpu_temp_c_max":null,"gpu_power_w_max":null}],
 "engines":[{"tier":1,"ts":1795000060,"node":"dgx-01","port":"8000",
             "model":"qwen3-coder","n":2,"error":null,"run":2.0,"wait":0.0,
             "kv":61.5,"gen_tps":31.0,"prompt_tps":120.0}],
 "counts":{"host_rows":2,"engine_rows":1},
 "fields":{"host":["cpu_usage_pct","gpu_temp_c"],"host_peaks":["gpu_temp_c_max"],
           "engine":["run","kv"],"engine_key":["node","port"],
           "engine_identity":"(node, port)"},
 "tier0":{"points":450,"capacity":450,"readable_here":false,
          "source":"recorder_ring_absent",
          "reason":"the 2-second ring lives in the daemon process, which is not this one"},
 "stats":{"path":"/Users/op/.hscc/metrics/history.db","db_bytes":3355443,
          "bound_bytes":10485760,"tier1_rows":5760,"tier2_rows":2016,"exists":true},
 "speak":"1-minute metrics history: 2 node rows, 1 engine row."}
"""#.utf8)

/// The same payload with `done` removed — §6 rule 1 says this must NOT be
/// readable as zero.
let summaryMissingDoneBody = Data(#"""
{"generated_at":"2026-11-17T08:30:00Z",
 "boards":[{"board":"hscc","counts":{"todo":17,"ready":0,"running":3,"blocked":0,
             "archived":114},"total":884}],
 "speak":"hscc: 20 open, 3 running."}
"""#.utf8)

/// §5.5's down-state: 38 B, no `json` key. A SUCCESSFUL read with no nodes.
let monitorDownBody = Data(#"{"speak":"fleet monitor unavailable"}"#.utf8)

/// The API's unified error envelope. `message` carries server prose that must
/// not reach a rendered widget string (§7).
let errorBody = Data(#"""
{"error":{"code":"internal","message":"Traceback in /srv/hscc/private/routes.py line 12","speak":"Server error"}}
"""#.utf8)

let garbageBody = Data("<html><body>502 Bad Gateway</body></html>".utf8)

/// `/v1/kanban/running`'s envelope (§4.3): `tasks[]` + `count` + `speak`, and the
/// `pid`/`host_local`/`boards[]` keys the narrowed model does not declare.
let kanbanRunningBody = Data(#"""
{"tasks":[{"id":"t_1","board":"hscc","title":"Widget client","status":"running",
           "assignee":"ios-engineer","started_at":"2026-11-17T08:00:00Z",
           "pid":4242,"host_local":"gateway-1"}],
 "count":1,"speak":"1 running","boards":["hscc"]}
"""#.utf8)

/// `/v1/cluster/history`'s answer for `window=6h` (tier 1): the recorder's rows
/// with the recorder's own key names, including one node whose GPU never
/// reported — a `null` that must stay nil, because a 0.0 would draw a node that
/// never answered as an idle one. `store.path` is on the wire and deliberately
/// NOT declared by `ClusterHistoryStore`, so it is never decoded (§7).
let historyBody = Data(#"""
{"window":"6h","window_s":21600,"tier":1,"resolution_s":60,"resolution":"60s",
 "tier_summary":"1-minute averages, with peaks for cpu_temp_c_max, gpu_temp_c_max, gpu_power_w_max",
 "max_fields":["cpu_temp_c_max","gpu_temp_c_max","gpu_power_w_max"],
 "since":1794978300,"until":1794999900,"node":"192.0.2.10",
 "metrics":["gpu_util_pct","gen_tps"],"hosts_included":true,"engines_included":true,
 "hosts":[{"tier":1,"ts":1794978300,"host":"192.0.2.10","gpu_util_pct":94.0,"gpu_power_w":160.5,"gpu_power_w_max":480.0,"n":30,"error":null},
          {"tier":1,"ts":1794978360,"host":"192.0.2.10","gpu_util_pct":0.0,"gpu_power_w":null,"gpu_power_w_max":null,"n":12,"error":"collect failed"}],
 "engines":[{"tier":1,"ts":1794978300,"node":"192.0.2.10","port":"8000","model":"org/Qwen-Test","run":2,"wait":1,"kv":12.5,"gen_tps":40.0,"n":30,"error":null}],
 "counts":{"hosts":2,"engines":1},
 "store":{"path":"/Users/example/.hscc/metrics/history.db","exists":true,"db_bytes":3355443,"tier0_resident":true},
 "note":"","speak":"Last 6h at 60s resolution: 2 node points, 1 engine point."}
"""#.utf8)

// MARK: - Harness

var failures: [String] = []
func check(_ name: String, _ condition: Bool) {
    if condition { print("  ok: \(name)") }
    else { failures.append(name); print("FAIL: \(name)") }
}

let token = "sekrit-token-xyz"
let testConfig = APIConfig(host: "cluster.test", port: 8788, token: token)
func stubbedClient(timeout: TimeInterval = WidgetDataClient.defaultTimeout) -> WidgetDataClient {
    WidgetDataClient(config: testConfig, timeout: timeout, session: stubSession())
}

/// A client for ONE cluster with several addresses, all through the same
/// stubbed transport. Used by the failover cases below.
func multiClient(host: String = "host-a.example",
                 fallbacks: [String] = ["host-b.example"],
                 timeout: TimeInterval = WidgetDataClient.defaultTimeout) -> WidgetDataClient {
    let config = APIConfig(host: host, port: 8788, token: token,
                           fallbackHosts: fallbacks)
    return WidgetDataClient(config: config, timeout: timeout, session: stubSession())
}

func requestedHosts() -> [String] {
    StubTransport.requests.compactMap { $0.url?.host }
}

struct Opaque: Decodable { let anything: Int }

/// The payload types are `Decodable`, not `Equatable`, so a whole `Result` cannot
/// be compared — pull the failure out and compare that (`WidgetDataFailure` is
/// `Equatable`, which is also what a surface switches on).
func failureOf<T>(_ result: Result<T, WidgetDataFailure>) -> WidgetDataFailure? {
    if case .failure(let failure) = result { return failure }
    return nil
}

/// Clear the App Group's failover memory between cases, and flush it so the
/// next `HostFailover` read cannot see a stale value.
func forgetLastWorkingHost() {
    let defaults = UserDefaults(suiteName: AppGroup.suiteName)
    defaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
    defaults?.synchronize()
}

func runAll() async {
    let client = stubbedClient()

    // 1. The read allowlist is exactly the audited routes, and it is a closed
    //    set: adding one has to be a deliberate act in this file too.
    let audited: Set<String> = [
        "/v1/projects", "/v1/mobile/summary",
        "/v1/kanban/running", "/v1/kanban/blocked", "/v1/kanban/stale",
        "/v1/cluster/monitor", "/v1/cluster/status", "/v1/cluster/history",
        "/v1/daemon/host", "/v1/autodown/status",
        "/v1/metrics/history",
    ]
    check("allowlist is exactly the audited reads", WidgetDataPath.all == audited)
    for path in audited { check("allowlisted \(path)", WidgetDataPath.isReadPath(path)) }
    for mutation in ["/v1/cards", "/v1/cards/ABC123/kill", "/v1/sessions/ABC/reply",
                     "/v1/kanban/ABC123/assign", "/v1/cluster/autodown/up",
                     "/v1/projects/hscc/sync"] {
        check("refused \(mutation)", !WidgetDataPath.isReadPath(mutation))
    }

    // 1b. The history read: which tier answered must survive decoding (a chart
    //     cannot tell a 2 s point from a 5 min one any other way), a stored null
    //     must stay nil, and the window must travel as a real query item.
    StubTransport.arm(.http(status: 200, body: historyBody))
    let history = await client.clusterHistory(window: "6h", node: "192.0.2.10",
                                              metrics: ["gpu_util_pct", "gen_tps"])
    switch history {
    case .success(let payload):
        check("history states the tier and resolution it served",
              payload.window == "6h" && payload.tier == 1
                && payload.resolution_s == 60 && payload.resolution == "60s")
        check("history keeps a stored null as nil, not as a zero",
              payload.hosts.count == 2 && payload.hosts[1].gpu_power_w == nil
                && payload.hosts[1].error == "collect failed")
        check("history keeps the peak the average erased",
              payload.hosts[0].gpu_power_w == 160.5
                && payload.hosts[0].gpu_power_w_max == 480.0)
        check("history names the peak columns it carried",
              payload.max_fields == ["cpu_temp_c_max", "gpu_temp_c_max", "gpu_power_w_max"])
        check("history reports whether the 2 s ring was readable",
              payload.tier0Resident == true)
        let historyFields = Mirror(reflecting: payload.store!).children.map { $0.label ?? "" }
        check("the store's server path is never decoded",
              !historyFields.contains("path"))
    case .failure(let failure):
        check("history should decode, got \(String(reflecting: failure))", false)
    }
    let historyRequest = StubTransport.requests.last
    check("history's window/node/metrics are real query items",
          historyRequest?.url?.query?.contains("window=6h") == true
            && historyRequest?.url?.query?.contains("node=192.0.2.10") == true
            && historyRequest?.url?.query?.contains("metrics=") == true)
    check("history's path carries no query string of its own",
          historyRequest?.url?.path == "/v1/cluster/history")

    // 2. A refused path never reaches the transport — no request, no side effect.
    StubTransport.arm(.http(status: 200, body: Data("{}".utf8)))
    let refused = await client.get("/v1/cards/ABC123/kill", as: Opaque.self)
    check("refused path yields nil from the shim", refused == nil)
    check("refused path built NO request", StubTransport.requests.isEmpty)
    let refusedTyped = await WidgetData.read(using: client,
                                            lastKnown: { nil as (ProjectsLite, Date)? },
                                            { _ in .failure(.pathNotAllowed(path: "/v1/cards")) })
    check("pathNotAllowed does not fall back to a snapshot", refusedTyped.failure != nil)
    check("pathNotAllowed reports showsStaleData false",
          !WidgetDataFailure.pathNotAllowed(path: "/v1/cards").showsStaleData)

    // 3. A real read: GET, the app's headers, http scheme, the deadline on it.
    StubTransport.arm(.http(status: 200, body: projectsBody))
    let projects = await client.projects()
    switch projects {
    case .success(let payload):
        check("projects decoded", payload.count == 2 && payload.projects.count == 2)
        let fields = Mirror(reflecting: payload.projects[0]).children.map { $0.label ?? "" }
        check("ProjectLite carries identity + board truth, never repo",
              fields == ["name", "board", "health", "counts", "lastActivity",
                         "orchestrator"])
        check("an old {name, board} row still decodes (counts nil, not zero)",
              payload.projects[0].counts == nil
                && payload.projects[0].lastActivity == nil)
        let rendered = String(describing: payload.projects)
        check("repo is not decoded anywhere in the value", !rendered.contains("/srv/hscc"))
    case .failure(let failure):
        check("projects should decode, got \(String(reflecting: failure))", false)
    }
    let request = StubTransport.requests.last
    check("exactly one request was built", StubTransport.requests.count == 1)
    check("the method is GET", request?.httpMethod == "GET")
    check("bearer auth header matches the app",
          request?.value(forHTTPHeaderField: "Authorization") == "Bearer \(token)")
    check("json accept header",
          request?.value(forHTTPHeaderField: "Accept") == "application/json")
    check("plain http (Tailscale is the transport)", request?.url?.scheme == "http")
    check("built from the configured host + port",
          request?.url?.absoluteString == "http://cluster.test:8788/v1/projects")
    check("the request carries the client's deadline",
          request?.timeoutInterval == WidgetDataClient.defaultTimeout)
    check("no body on any request this client can build", request?.httpBody == nil)

    // 4. Timeout: the ceiling is the client's, not URLSession's, and it clamps.
    check("default ceiling is 8 s", WidgetDataClient.defaultTimeout == 8)
    check("a fresh client uses the default ceiling", client.timeout == 8)
    check("a non-positive ceiling is clamped, not trusted",
          WidgetDataClient(config: testConfig, timeout: 0).timeout == 1)
    check("an explicit ceiling survives", stubbedClient(timeout: 3).timeout == 3)
    StubTransport.arm(.error(URLError(.timedOut)))
    let timedOut = await client.projects()
    check("a timed-out load maps to .timeout",
          failureOf(timedOut) == .timeout(seconds: 8))
    check(".timeout is stale-while-error", WidgetDataFailure.timeout(seconds: 8).showsStaleData)
    StubTransport.arm(.error(URLError(.timedOut)))
    let shortDeadline = await stubbedClient(timeout: 3).projects()
    check("the failure names the ceiling that expired",
          failureOf(shortDeadline) == .timeout(seconds: 3))

    // 5. Transport failure that is not a timeout.
    StubTransport.arm(.error(URLError(.cannotConnectToHost)))
    let unreachable = await client.projects()
    check("connection refused maps to .unreachable",
          failureOf(unreachable) == .unreachable(code: URLError.Code.cannotConnectToHost.rawValue))
    check(".unreachable is stale-while-error", WidgetDataFailure.unreachable(code: nil).showsStaleData)
    check("an unreachable failure renders no address",
          !WidgetDataFailure.unreachable(code: 1).operatorLine.contains("cluster.test"))

    // 6. Status mapping — auth, missing route, server.
    StubTransport.arm(.http(status: 401, body: errorBody))
    let unauthorized = await client.projects()
    check("401 maps to .unauthorized", failureOf(unauthorized) == .unauthorized)
    check("401 says what to fix",
          WidgetDataFailure.unauthorized.shortLabel == "Token rejected")
    check("an auth failure never renders the token",
          !WidgetDataFailure.unauthorized.operatorLine.contains(token)
            && !WidgetDataFailure.unauthorized.shortLabel.contains(token))
    StubTransport.arm(.http(status: 404, body: errorBody))
    let notFound = await client.mobileSummary()
    check("404 maps to .notFound with the route",
          failureOf(notFound) == .notFound(path: "/v1/mobile/summary"))
    StubTransport.arm(.http(status: 503, body: errorBody))
    let server = await client.projects()
    check("5xx keeps the API error code",
          failureOf(server) == .http(status: 503, code: "internal"))
    switch server {
    case .failure(let failure):
        check("5xx prose is not rendered",
              !failure.shortLabel.contains("Traceback")
                && !failure.operatorLine.contains("Traceback")
                && !failure.operatorLine.contains("/srv/hscc"))
        check("5xx still says which status", failure.shortLabel.contains("503"))
    default: check("5xx should fail", false)
    }

    // 7. A 2xx we cannot read is a failure, never a blank and never a zero.
    StubTransport.arm(.http(status: 200, body: garbageBody))
    let unreadable = await client.projects()
    if case .failure(.decoding) = unreadable {
        check("a garbage 2xx maps to .decoding", true)
    } else {
        check("a garbage 2xx maps to .decoding, got \(String(reflecting: unreadable))", false)
    }
    check(".decoding is stale-while-error",
          WidgetDataFailure.decoding(detail: "x").showsStaleData)
    check("a decode failure renders no internal detail",
          WidgetDataFailure.decoding(detail: "keyNotFound(BasicCodingKeys.done)").shortLabel == "Can't read data")

    // 8. §6 rule 1 end to end: a counts payload missing `done` must throw, so
    //    the surface keeps its last-known numbers instead of rendering 0.
    StubTransport.arm(.http(status: 200, body: summaryMissingDoneBody))
    let missingDone = await client.mobileSummary()
    if case .failure(.decoding) = missingDone {
        check("a missing `done` is a decode failure, not a zero", true)
    } else {
        check("a missing `done` is a decode failure, got \(String(reflecting: missingDone))", false)
    }
    StubTransport.arm(.http(status: 200, body: summaryBody))
    let summary = await client.mobileSummary()
    if case .success(let payload) = summary {
        check("complete counts decode", payload.entries.first?.counts?.done == 750)
        check("not degraded", !payload.degraded)
        check("open total is the server's own arithmetic",
              payload.entries.first?.openTotal == 20)
    } else {
        check("complete counts should decode, got \(String(reflecting: summary))", false)
    }

    // 9. §5.5's down-state is a successful read with no nodes, not a failure.
    StubTransport.arm(.http(status: 200, body: monitorDownBody))
    let monitor = await client.clusterMonitor()
    if case .success(let payload) = monitor {
        check("fleet-down monitor decodes with no nodes", payload.nodes.isEmpty)
        check("fleet-down monitor keeps speak", payload.speak == "fleet monitor unavailable")
    } else {
        check("fleet-down monitor should succeed, got \(String(reflecting: monitor))", false)
    }

    // 10. Query items go through URLQueryItem, never welded into the path.
    StubTransport.arm(.http(status: 200, body: Data(#"{"tasks":[],"count":0,"speak":""}"#.utf8)))
    _ = await client.kanbanStale(olderThanDays: 0)
    check("stale carries older_than=0",
          StubTransport.requests.last?.url?.absoluteString.contains("older_than=0") == true)
    check("stale path stayed clean",
          StubTransport.requests.last?.url?.path == "/v1/kanban/stale")
    StubTransport.arm(.http(status: 200, body: summaryBody))
    _ = await client.mobileSummary(boards: ["hscc", "inc"])
    let scoped = StubTransport.requests.last?.url?.absoluteString ?? ""
    check("mobileSummary scopes boards", scoped.contains("boards=") && scoped.contains("hscc") && scoped.contains("inc"))

    // 10b. Dataset 6 — `GET /v1/metrics/history` (t_5ae8cfcf). Three things
    //      this gate exists to keep true: `tier` leaves as a query item (the
    //      route 400s without it), an out-of-range tier is clamped rather than
    //      forwarded (a 400 on every refresh is worse than the nearest real
    //      tier), and a metric the node never reported stays `nil` — it must
    //      not reach a chart as a flat 0.
    StubTransport.arm(.http(status: 200, body: metricsHistoryBody))
    let metricsRead = await client.metricsHistory(tier: 1, host: "dgx-01",
                                                  since: 1795000000,
                                                  until: 1795000120)
    let metricsURL = StubTransport.requests.last?.url?.absoluteString ?? ""
    check("history carries tier=1", metricsURL.contains("tier=1"))
    check("history carries host and the inclusive window",
          metricsURL.contains("host=dgx-01") && metricsURL.contains("since=1795000000")
            && metricsURL.contains("until=1795000120"))
    check("history path stayed clean",
          StubTransport.requests.last?.url?.path == "/v1/metrics/history")
    if case .success(let payload) = metricsRead {
        check("history decodes its node rows", payload.hosts.count == 2 && payload.tier == 1)
        check("an unreported metric is nil, never 0",
              payload.hosts[1].gpu_temp_c == nil
                && payload.hosts[1].error == "ssh: collect failed")
        check("the peak rides beside the average",
              payload.hosts[0].gpu_temp_c == 75.0 && payload.hosts[0].gpu_temp_c_max == 90.0)
        check("engine identity is node+port, model is not the key",
              payload.engines.first?.engineKey == "dgx-01:8000"
                && payload.engines.first?.model == "qwen3-coder")
        check("rows from the store read as a stored window", payload.hasStoredRows)
        check("a ring in another process is not a live window",
              payload.tier0?.readable_here == false && !payload.hasLiveTier0)
        let trend = MetricsTrend.make(payload, field: .gpuTemp, host: "dgx-01")
        check("the trend keeps the bucket's mean and its peak",
              trend.points.count == 1 && trend.points[0].value == 75.0
                && trend.points[0].peak == 90.0 && trend.hasPeaks)
        check("the axis scales by the peak, not the mean", trend.upperBound == 90.0)
        let unreported = MetricsTrend.make(payload, field: .gpuPower, host: "dgx-02")
        check("a host that never answered is a gap, not a zero",
              unreported.points.count == 1 && unreported.gaps == 1
                && unreported.points[0].value == nil && unreported.upperBound == nil)
        check("the caption names the gap instead of hiding it",
              unreported.statusLine.contains("1 not reported"))
        check("with no host named the series takes the first host present",
              MetricsTrend.make(payload, field: .gpuTemp).host == "dgx-01")
        check("the field vocabulary comes from the server, not a hardcoded list",
              payload.fields?.host_peaks == ["gpu_temp_c_max"])
    } else {
        check("history should decode, got \(String(reflecting: metricsRead))", false)
    }
    StubTransport.arm(.http(status: 200, body: metricsHistoryBody))
    _ = await client.metricsHistory(tier: 9)
    check("an out-of-range tier is clamped, not forwarded",
          StubTransport.requests.last?.url?.absoluteString.contains("tier=2") == true)
    // A tier-0 read with nothing in it must not be able to claim a live window.
    StubTransport.arm(.http(status: 200, body: Data(#"""
    {"tier":0,"hosts":[],"engines":[],"tier0":{"points":0,"capacity":450,
     "readable_here":true,"source":"recorder_ring"},"speak":"empty"}
    """#.utf8)))
    if case .success(let empty) = await client.metricsHistory(tier: 0) {
        check("an empty ring is not a live window",
              !empty.hasLiveTier0 && !empty.hasStoredRows)
    } else {
        check("an empty tier-0 response should still decode", false)
    }

    // 11. The nil-shim the two pre-pivot providers still use keeps working.
    StubTransport.arm(.http(status: 200, body: kanbanRunningBody))
    let shimmed = await client.get("/v1/kanban/running", as: KanbanRunningLite.self)
    check("the nil-shim still decodes",
          shimmed?.count == 1 && shimmed?.tasks?.first?.id == "t_1")
    let rowText = String(describing: shimmed?.tasks ?? [])
    check("running's pid/host_local/boards are never decoded",
          !rowText.contains("4242") && !rowText.contains("gateway-1") && !rowText.contains("boards"))
    StubTransport.arm(.http(status: 500, body: errorBody))
    let shimmedFail = await client.get("/v1/kanban/running", as: KanbanRunningLite.self)
    check("the nil-shim nils on failure", shimmedFail == nil)
    let aliased: ExtensionClient? = stubbedClient()
    check("ExtensionClient alias still names the client", aliased?.timeout == 8)

    // 12. The stale-while-error rule itself.
    let snapshotAt = Date().addingTimeInterval(-900)
    let snapshotValue = ProjectsLite(projects: [ProjectLite(name: "hscc", board: "hscc")],
                                     count: 1, speak: nil)
    func snapshot() -> (value: ProjectsLite, at: Date)? { (value: snapshotValue, at: snapshotAt) }

    StubTransport.arm(.http(status: 200, body: projectsBody))
    let live = await WidgetData.read(using: client, lastKnown: snapshot) { await $0.projects() }
    check("a good read is .live", { if case .live = live { return true }; return false }())
    check(".live reports no age", live.ageMinutes == nil)
    check(".live has the value", live.value?.projects.count == 2)

    StubTransport.arm(.http(status: 503, body: errorBody))
    let stale = await WidgetData.read(using: client, lastKnown: snapshot) { await $0.projects() }
    if case .stale(let value, let age, let reason) = stale {
        check("a 5xx falls back to the snapshot", value.projects.count == 1)
        check("the age is minutes since the snapshot", age == 15)
        check("the stale reason is the real failure", reason == .http(status: 503, code: "internal"))
    } else {
        check("expected .stale, got \(String(reflecting: stale))", false)
    }

    StubTransport.arm(.http(status: 503, body: errorBody))
    let noSnapshot = await WidgetData.read(using: client,
                                          lastKnown: { nil as (ProjectsLite, Date)? }) { await $0.projects() }
    check("no snapshot means .failed, not a blank", noSnapshot.failure != nil)
    check("the failure survives for the surface to name",
          noSnapshot.failure == .http(status: 503, code: "internal"))

    // t_5c554c5b, as a structural rule: an unconfigured app never renders as
    // stale data, even with a real snapshot sitting in the container.
    StubTransport.arm(.http(status: 200, body: projectsBody))  // clears the request log
    let unconfigured = await WidgetData.read(using: nil, lastKnown: snapshot) { await $0.projects() }
    check("unconfigured is .failed(.notConfigured)", unconfigured.failure == .notConfigured)
    check("unconfigured NEVER shows stale data", unconfigured.value == nil)
    check("the setup invite is honest",
          WidgetDataFailure.notConfigured.shortLabel == "Set up in HSCC")
    let clientWasUsed = !StubTransport.requests.isEmpty
    check("an unconfigured read makes no request", !clientWasUsed)

    // 13. Multi-address failover: transport advances, ANY HTTP answer does not.
    forgetLastWorkingHost()
    check("saved order is primary -> fallbacks",
          HostFailover.orderedHosts(primary: "host-a.example",
                                    fallbacks: ["host-b.example", "host-c.example"])
            == ["host-a.example", "host-b.example", "host-c.example"])
    HostFailover.remember("host-b.example")
    check("last-working address is tried first",
          HostFailover.orderedHosts(["host-a.example", "host-b.example", "host-c.example"])
            == ["host-b.example", "host-a.example", "host-c.example"])
    forgetLastWorkingHost()

    StubTransport.armSequence([.error(URLError(.cannotConnectToHost)),
                               .http(status: 200, body: projectsBody)])
    let failedOver = await multiClient().projects()
    check("a transport failure advances to the next address",
          requestedHosts() == ["host-a.example", "host-b.example"])
    if case .success(let payload) = failedOver {
        check("the second address's payload is the one returned", payload.projects.count == 2)
    } else {
        check("failover should succeed on the second address, got \(String(reflecting: failedOver))", false)
    }
    check("the answering address is remembered for next time",
          HostFailover.lastWorkingHost() == "host-b.example")

    StubTransport.arm(.http(status: 401, body: errorBody))
    let authRefusedResult = await multiClient(host: "host-c.example",
                                              fallbacks: ["host-d.example"]).projects()
    check("HTTP 401 does NOT fail over to another address",
          requestedHosts() == ["host-c.example"])
    check("HTTP 401 is still surfaced as .unauthorized",
          failureOf(authRefusedResult) == .unauthorized)

    StubTransport.arm(.error(URLError(.cannotConnectToHost)))
    let allDown = await multiClient(host: "host-e.example",
                                    fallbacks: ["host-f.example"]).projects()
    check("all addresses are tried exactly once each",
          requestedHosts() == ["host-e.example", "host-f.example"])
    check("all addresses down yields ONE unreachable failure",
          failureOf(allDown) == .unreachable(code: URLError.Code.cannotConnectToHost.rawValue))

    StubTransport.armSequence([.error(URLError(.timedOut)),
                               .http(status: 200, body: projectsBody)])
    let shortFirst = await multiClient(host: "host-g.example",
                                       fallbacks: ["host-h.example"]).projects()
    check("a multi-address attempt carries the short per-host deadline",
          StubTransport.requests.first?.timeoutInterval == HostFailover.perAttemptTimeout)
    if case .success = shortFirst {
        check("a timed-out first address still lets the second answer", true)
    } else {
        check("a timed-out first address should let the second answer, got \(String(reflecting: shortFirst))", false)
    }

    StubTransport.arm(.error(URLError(.timedOut)))
    let allTimedOut = await multiClient(host: "host-i.example",
                                        fallbacks: ["host-j.example"]).projects()
    check("all timed out yields ONE failure naming the per-attempt ceiling",
          failureOf(allTimedOut) == .timeout(seconds: HostFailover.perAttemptTimeout))

    HostFailover.remember("host-l.example")
    StubTransport.arm(.http(status: 200, body: projectsBody))
    _ = await multiClient(host: "host-k.example", fallbacks: ["host-l.example"]).projects()
    check("the remembered address is the first one requested",
          requestedHosts() == ["host-l.example"])
    forgetLastWorkingHost()

    // 14. Every failure case is renderable and safe: short, no token, no host.
    for failure in [WidgetDataFailure.notConfigured, .invalidURL,
                    .pathNotAllowed(path: "/v1/cards"), .timeout(seconds: 8),
                    .unreachable(code: 1), .unauthorized,
                    .notFound(path: "/v1/mobile/summary"),
                    .http(status: 503, code: "internal"),
                    .decoding(detail: "decoding detail") ] {
        check("\(String(reflecting: failure)) renders a short label",
              !failure.shortLabel.isEmpty && failure.shortLabel.count <= 18)
        check("\(String(reflecting: failure)) leaks nothing",
              !failure.shortLabel.contains(token) && !failure.operatorLine.contains(token)
                && !failure.operatorLine.contains("decoding detail"))
    }
    check("age never goes negative", WidgetData.ageMinutes(from: Date().addingTimeInterval(60)) == 0)
    check("age counts whole minutes", WidgetData.ageMinutes(from: Date().addingTimeInterval(-125)) == 2)
}

await runAll()

print("")
if failures.isEmpty {
    print("✅ REAL WidgetDataClient — read-only allowlist, timeout, failure mapping and stale-while-error all hold")
    exit(0)
} else {
    print("❌ WIDGET DATA CLIENT CHECK FAILED — \(failures.count) assertion(s): \(failures.joined(separator: " | "))")
    exit(1)
}
