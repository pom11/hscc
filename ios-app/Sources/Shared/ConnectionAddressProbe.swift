import Foundation

// ---------------------------------------------------------------------------
// ConnectionAddressProbe.swift — "Test connection" probes EVERY address.
//
// Before this file the shared test path (`QRPairing.test` on iOS, a single
// `WidgetDataClient` read on macOS) stopped at the first address that answered.
// An operator with a primary host and two fallbacks therefore learned nothing
// about the rest of the cluster: a dead primary was reported as "connected"
// (because the fallback answered), and a typo in a fallback stayed invisible
// until the day it was needed.
//
// The rules, in one place so both apps report identically:
//   * probe the primary and every fallback CONCURRENTLY, one `GET /v1/ping`
//     per address, each under the existing per-attempt ceiling
//     (`HostFailover.perAttemptTimeout`, capped with `attemptTimeout`);
//   * the operator's order is preserved in the report, whatever order the
//     concurrent tasks finish in;
//   * three outcomes per address: `.ok` (the ping decoded, `ok == true`, with
//     service/version and the measured latency), `.unreachable` (timeout,
//     refused, DNS, TLS — no HTTP answer at all), `.rejected` (the daemon
//     answered but refused the token, spoke the wrong API version, or returned
//     some other status);
//   * the overall test succeeds when AT LEAST ONE address is `.ok` — that is
//     the same availability rule the failover loop uses. The fastest `.ok`
//     address is remembered as the App Group's last-working host, so widgets
//     and the next request start on the link that measured best.
//
// The transport is injectable (`run(hosts:probe:)`) so the headless gate can
// exercise the orchestration without opening a socket. The URL itself is built
// by `pingRequest`, exactly like `HSCCClient` builds its requests: plain HTTP
// over Tailscale, bearer token, JSON accept.
// ---------------------------------------------------------------------------

/// Why one address produced no HTTP answer at all.
enum AddressUnreachableReason: Equatable {
    /// The per-attempt ceiling expired (`URLError.timedOut`).
    case timeout
    /// The host answered with a connection refusal.
    case refused
    /// The name could not be resolved.
    case dns
    /// TLS failed (the transport is normally plain HTTP over Tailscale; this
    /// is reported distinctly when a middlebox/proxy terminates TLS badly).
    case tls
    /// No network route on this device (offline, airplane mode, no tailnet).
    case offline
    /// The saved address could not form a URL.
    case invalidAddress
    /// Any other transport failure.
    case other

    /// The short reason a result row shows instead of a latency.
    var shortReason: String {
        switch self {
        case .timeout: return "timed out"
        case .refused: return "refused"
        case .dns: return "DNS failed"
        case .tls: return "TLS failed"
        case .offline: return "offline"
        case .invalidAddress: return "bad address"
        case .other: return "no connection"
        }
    }
}

/// Why an address that DID answer is not a working cluster link.
enum AddressRejectionReason: Equatable {
    /// 401/403 (or the `unauthorized`/`invalid_token` machine slug).
    case token
    /// A route/decode mismatch: the daemon answered, but not with the `/v1/ping`
    /// shape this app speaks — an app/cluster version skew.
    case version
    /// Any other HTTP status. `status` is the code; never server prose.
    case server(status: Int)

    /// The short reason a result row shows instead of a latency.
    var shortReason: String {
        switch self {
        case .token: return "token rejected"
        case .version: return "wrong version"
        case .server(let status): return "server \(status)"
        }
    }
}

/// One address's probe outcome.
enum AddressProbeOutcome: Equatable {
    /// The daemon answered `/v1/ping` with `ok == true`.
    case ok(service: String?, version: String?, latencyMs: Int)
    /// No HTTP answer at all.
    case unreachable(AddressUnreachableReason)
    /// The daemon answered, but this address is not usable.
    case rejected(AddressRejectionReason)
}

/// One address's row in the report, in the operator's order.
struct AddressProbeResult: Equatable, Identifiable {
    let host: String
    let outcome: AddressProbeOutcome

    /// Stable row identity: the operator's ordered list has unique hosts
    /// (`HostFailover.orderedHosts` folds duplicates before probing).
    var id: String { host }

    var isOK: Bool {
        if case .ok = outcome { return true }
        return false
    }

    /// Latency in milliseconds for a working address; nil otherwise.
    var latencyMillis: Int? {
        if case .ok(_, _, let ms) = outcome { return ms }
        return nil
    }

    /// What the row shows after the address: the latency when it worked, the
    /// short reason when it did not.
    var detail: String {
        switch outcome {
        case .ok(_, _, let ms): return "\(ms) ms"
        case .unreachable(let reason): return reason.shortReason
        case .rejected(let reason): return reason.shortReason
        }
    }
}

/// The whole test-connection result: one row per address plus the overall line.
struct ConnectionTestReport: Equatable {
    /// One result per address, in the operator's saved order.
    let results: [AddressProbeResult]
    /// The fastest address that answered `.ok` — the one the app will use (it
    /// was just written as the App Group's last-working host). Nil when no
    /// address worked.
    let selectedHost: String?

    var total: Int { results.count }
    var okCount: Int { results.reduce(0) { $0 + ($1.isOK ? 1 : 0) } }

    /// Success means "the cluster is usable": at least one address answered.
    var overallSuccess: Bool { okCount > 0 }

    /// The overall line the operator sees. Exactly "Connected — n of m
    /// addresses reachable" on success; otherwise the existing failure copy,
    /// chosen by the most actionable reason in the rows.
    var overallLine: String {
        if overallSuccess {
            return "Connected — \(okCount) of \(total) addresses reachable"
        }
        if results.contains(where: { $0.outcome == .rejected(.token) }) {
            return "Token rejected: the cluster answered but refused the token. Check it in Settings and try again."
        }
        if results.contains(where: { $0.outcome == .rejected(.version) }) {
            return "Wrong version: the cluster answered but does not speak this app's API version."
        }
        return "Can't reach the cluster — is Tailscale connected?"
    }

    /// Whether `host` is the address the app will use next.
    func willUse(_ host: String) -> Bool {
        guard let selectedHost else { return false }
        return host.caseInsensitiveCompare(selectedHost) == .orderedSame
    }
}

/// The one shared "Test connection" implementation both apps call.
enum ClusterConnectionTest {
    /// The body `/v1/ping` returns. Only the fields the test reports are
    /// decoded; `speak` is server prose and deliberately not required here.
    private struct PingBody: Decodable {
        let ok: Bool
        // Required: a 2xx without the service/version pair is not the
        // `/v1/ping` this app speaks, so it is reported as a version mismatch
        // rather than an OK row with two blanks.
        let service: String
        let version: String
    }

    /// The `/v1/ping` request for one address. Mirrors `HSCCClient`: GET, plain
    /// HTTP over Tailscale, bearer token, JSON accept, caller-supplied ceiling.
    static func pingRequest(host: String, port: Int, token: String,
                            timeout: TimeInterval) -> URLRequest? {
        var components = URLComponents()
        components.scheme = "http"
        components.host = host
        components.port = port
        components.path = "/v1/ping"
        guard let url = components.url else { return nil }

        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    /// Probe every address concurrently, preserving the caller's order.
    ///
    /// `probe` is injected so the gate can drive the orchestration without a
    /// socket; production calls `test`, which supplies the real URLSession
    /// probe below.
    static func run(
        hosts: [String],
        probe: @escaping @Sendable (String) async -> AddressProbeOutcome
    ) async -> [AddressProbeResult] {
        guard !hosts.isEmpty else { return [] }

        return await withTaskGroup(of: (Int, AddressProbeResult).self) { group in
            for (index, host) in hosts.enumerated() {
                group.addTask {
                    let outcome = await probe(host)
                    return (index, AddressProbeResult(host: host, outcome: outcome))
                }
            }

            var collected: [(Int, AddressProbeResult)] = []
            for await item in group {
                collected.append(item)
            }
            // Concurrent completion order is irrelevant: the operator's order
            // is the order of the rows.
            return collected.sorted { $0.0 < $1.0 }.map(\.1)
        }
    }

    /// Run the shared test against a real cluster config.
    ///
    /// All addresses get the same port and token (the saved-cluster contract),
    /// each attempt is capped by the existing per-attempt ceiling, and the
    /// fastest working address is remembered for widgets/the next request.
    static func test(
        hosts: [String],
        port: Int,
        token: String,
        timeout: TimeInterval = HostFailover.perAttemptTimeout,
        session: URLSession = .shared
    ) async -> ConnectionTestReport {
        let ordered = HostFailover.orderedHosts(primary: hosts.first ?? "",
                                                fallbacks: Array(hosts.dropFirst()))
        guard !ordered.isEmpty else {
            return report(from: [])
        }
        let ceiling = HostFailover.attemptTimeout(base: timeout,
                                                  hostCount: ordered.count)
        let results = await run(hosts: ordered) { host in
            await probe(host: host, port: port, token: token,
                        timeout: ceiling, session: session)
        }
        return report(from: results)
    }

    /// Build the report from finished rows and remember the fastest working
    /// address. This is the ONE place the last-working value moves, so a
    /// successful test always leaves widgets on the best measured link.
    static func report(from results: [AddressProbeResult]) -> ConnectionTestReport {
        var fastest: AddressProbeResult?
        for result in results where result.isOK {
            guard let ms = result.latencyMillis else { continue }
            if let current = fastest, let currentMs = current.latencyMillis,
               currentMs <= ms {
                continue
            }
            fastest = result
        }
        if let fastest {
            HostFailover.remember(fastest.host)
        }
        return ConnectionTestReport(results: results,
                                    selectedHost: fastest?.host)
    }

    // MARK: - Real transport

    /// One address's real probe. Never throws: every failure becomes a row.
    static func probe(host: String, port: Int, token: String,
                      timeout: TimeInterval,
                      session: URLSession) async -> AddressProbeOutcome {
        guard let request = pingRequest(host: host, port: port, token: token,
                                        timeout: timeout) else {
            return .unreachable(.invalidAddress)
        }

        let start = Date()
        do {
            let (data, response) = try await session.data(for: request)
            let latency = Int((Date().timeIntervalSince(start) * 1000).rounded())
            guard let http = response as? HTTPURLResponse else {
                return .unreachable(.other)
            }
            return classify(status: http.statusCode, data: data,
                            latencyMs: max(0, latency))
        } catch {
            return classify(error: error)
        }
    }

    /// HTTP status + body -> one row outcome. Pure, so the gate pins it.
    static func classify(status: Int, data: Data,
                         latencyMs: Int) -> AddressProbeOutcome {
        switch status {
        case 200...299:
            guard let body = try? JSONDecoder().decode(PingBody.self, from: data),
                  body.ok else {
                // A 2xx we cannot read as /v1/ping is an app/cluster version
                // skew, not an unreachable host.
                return .rejected(.version)
            }
            return .ok(service: body.service, version: body.version,
                       latencyMs: latencyMs)
        case 401, 403:
            return .rejected(.token)
        case 404:
            // The daemon answered but this build's route is not there.
            return .rejected(.version)
        default:
            // The same machine-slug check QRPairing.classify performs: a 400
            // carrying "unauthorized" is still a token rejection.
            if let code = errorCode(from: data),
               code == "unauthorized" || code == "invalid_token" {
                return .rejected(.token)
            }
            return .rejected(.server(status: status))
        }
    }

    /// Transport error -> one row outcome. Pure, so the gate pins every
    /// distinct reason the operator's row copy claims to distinguish.
    static func classify(error: Error) -> AddressProbeOutcome {
        guard let urlError = error as? URLError else {
            return .unreachable(.other)
        }
        switch urlError.code {
        case .timedOut:
            return .unreachable(.timeout)
        case .cannotConnectToHost:
            return .unreachable(.refused)
        case .cannotFindHost, .dnsLookupFailed:
            return .unreachable(.dns)
        case .secureConnectionFailed, .serverCertificateUntrusted,
             .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot,
             .serverCertificateNotYetValid, .clientCertificateRejected,
             .clientCertificateRequired,
             .appTransportSecurityRequiresSecureConnection:
            return .unreachable(.tls)
        case .notConnectedToInternet, .dataNotAllowed:
            return .unreachable(.offline)
        case .badURL, .unsupportedURL:
            return .unreachable(.invalidAddress)
        default:
            return .unreachable(.other)
        }
    }

    /// The API's unified error envelope `{ "error": { "code" } }`. Only the
    /// machine slug is read: message/speak are server prose and never rendered.
    private static func errorCode(from data: Data) -> String? {
        struct Envelope: Decodable {
            struct Inner: Decodable { let code: String? }
            let error: Inner?
        }
        return (try? JSONDecoder().decode(Envelope.self, from: data))?.error?.code
    }
}
