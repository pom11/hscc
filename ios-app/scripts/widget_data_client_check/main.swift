import Foundation

// ===========================================================================
// widget_data_client_check — drives the REAL Sources/Shared/WidgetDataClient
// (compiled verbatim by scripts/widget_data_client_check.sh, together with the
// real SharedModels.swift + SharedStore.swift) through a stubbed transport and
// asserts the four promises the pivot rests on:
//
//   1. READ-ONLY: the fetch allowlist is exactly the nine audited read routes,
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
        "/v1/cluster/monitor", "/v1/cluster/status", "/v1/daemon/host",
        "/v1/autodown/status",
    ]
    check("allowlist is exactly the audited reads", WidgetDataPath.all == audited)
    for path in audited { check("allowlisted \(path)", WidgetDataPath.isReadPath(path)) }
    for mutation in ["/v1/cards", "/v1/cards/ABC123/kill", "/v1/sessions/ABC/reply",
                     "/v1/kanban/ABC123/assign", "/v1/cluster/autodown/up",
                     "/v1/projects/hscc/sync"] {
        check("refused \(mutation)", !WidgetDataPath.isReadPath(mutation))
    }

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
        check("ProjectLite carries only {name, board}", fields == ["name", "board"])
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
