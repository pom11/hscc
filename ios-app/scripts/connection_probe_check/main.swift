import Foundation

// connection_probe_check — PROVE the probe state machine that resolves
// ConnectionMonitor's eternal `.unknown` (t_e5d24c6d).
//
// Field report (v2.9.0): the drawer pill sat on "checking…" FOREVER with the
// API up — `status` changed only via real requests, the drawer makes none, and
// the old launch ping had been deleted precisely because a one-shot cold-start
// FAILURE pinned the banner. The fix is a self-scheduled cheap probe
// (GET /v1/health) run while the drawer header is visible. This gate pins the
// contract that fix must obey:
//   * the probe endpoint stays "/v1/health" (confirmed route handle_health —
//     never a new endpoint invented at the client layer);
//   * the committed live capture of /v1/health still decodes into the type
//     the probe actually uses (HealthResponse via client.health()) — decode
//     drift fails this gate instead of silently re-stranding the pill;
//   * classification: 2xx/any HTTP response -> reachable; 401/403 or the
//     "unauthorized" machine slug -> auth refusal; no HTTP response at all
//     (transport failure, unbuildable URL) -> transport failure;
//   * the first real probe result FLIPS .unknown (that is the whole bug);
//   * a failed probe NEVER pins the banner: any later real success
//     (requestSucceeded, the HSCCClient path) clears it — same no-pin
//     contract t_4889e978 established for the old ping.
//
// It compiles the REAL Sources/HSCC/ConnectionMonitor.swift (never redeclared
// here) plus the models the classification touches (Models/SharedModels/
// APIError) into a plain macOS CLI — same runner as connection_banner_check,
// chat_state_check, reconnect_check: the logic is pure Foundation + Combine
// re-exports, and no iOS runtime claim is made here.

func assert(_ cond: Bool, _ label: String) {
    if cond {
        print("PASS: \(label)")
    } else {
        print("FAIL: \(label)")
        exit(1)
    }
}

let m = ConnectionMonitor.shared  // the real shared instance

// 0) The probe hits the confirmed lightest authed route — never an invented one.
assert(ConnectionProbe.probePath == "/v1/health",
       "probe endpoint is /v1/health (the confirmed handle_health route)")

// 1) The committed live capture of /v1/health decodes into the exact type the
//    probe's client call uses. If the server shape drifts, `client.health()`
//    would throw `.decoding` on a 200 and the pill could never turn green —
//    this catches that here, not on the operator's phone.
let fixtureDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
assert(!fixtureDir.isEmpty, "gate passes the fixture directory")
let healthData: Data
do {
    healthData = try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/v1_health.json"))
} catch {
    print("FAIL: cannot read fixtures/v1_health.json — \(error)")
    exit(1)
}
do {
    let health = try JSONDecoder().decode(HealthResponse.self, from: healthData)
    assert(health.ok == true && !health.checks.isEmpty,
           "fixtures/v1_health.json decodes into HealthResponse (the probe's type)")
} catch {
    print("FAIL: /v1/health capture no longer decodes into HealthResponse — \(error)")
    exit(1)
}

// 1b) The PR #486 freshness keys (t_67c5b0f2). The capture above is a PRE-#486
//     body and carries none of them, so it is the backward-compatibility half of
//     the contract: the four new keys must decode as nil, and `stale == nil`
//     must never be read as "fresh".
//
//     The other fixtures below are SYNTHESISED from the payload `handle_health`
//     returns at 96a5cc14 — `age_seconds` = max(0, now - generated_at),
//     `stale` = age_seconds > 120, `source` = "cache" | "inline" — and are
//     fixtures, not captures: this card never called the live API.
func decodeHealthFixture(_ file: String) -> HealthResponse {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/" + file)) else {
        print("FAIL: cannot read fixtures/\(file) — the gate's fixture set is incomplete")
        exit(1)
    }
    do {
        return try JSONDecoder().decode(HealthResponse.self, from: data)
    } catch {
        print("FAIL: fixtures/\(file) no longer decodes into HealthResponse — \(error)")
        exit(1)
    }
}

let preCache = decodeHealthFixture("v1_health.json")
assert(preCache.age_seconds == nil && preCache.generated_at == nil
       && preCache.stale == nil && preCache.source == nil,
       "the pre-#486 capture decodes with all four PR #486 keys nil (strict superset holds)")
assert(!preCache.serverSaysVerdictIsStale,
       "a server that cannot say stale is NOT reported as stale (nil is not false)")
assert(preCache.serverVerdictAgeSeconds == nil,
       "a server that sent no age yields no age — never a defaulted 0s")

let staleCached = decodeHealthFixture("v1_health_stale.json")
assert(staleCached.stale == true && staleCached.source == "cache",
       "the stale cached verdict decodes stale + source")
assert(staleCached.generated_at != nil,
       "generated_at decodes as the epoch the verify ran")
assert(staleCached.age_seconds == 260 && staleCached.serverVerdictAgeSeconds == 260,
       "the server's own age_seconds is what the view is handed (260s)")
assert(staleCached.speak.contains("Health from 4m ago."),
       "the server's own age sentence stays inside speak, untouched")
assert(staleCached.serverSaysVerdictIsStale,
       "the stale cached verdict is the case the screen must flag")
// The sentence the screen renders. "4m" is what fleet_offline_check proves
// Offline.agePhrase yields for 260 s — the SAME string the server's own
// _humanize_age puts in speak for the same age — so the app repeats the
// server's number rather than restating it in a second age dialect.
assert(staleCached.serverVerdictAgeLine(agePhrase: "4m")
       == "Server says this verdict is 4m old — served from the server's cache.",
       "the stale line states the server's age and where the verdict came from")

let staleNoAge = decodeHealthFixture("v1_health_stale_no_age.json")
assert(staleNoAge.serverSaysVerdictIsStale,
       "stale without an age still flags the verdict stale")
assert(staleNoAge.serverVerdictAgeSeconds == nil,
       "stale without an age yields NO number — rendering 0s here is the lie this prevents")
assert(staleNoAge.serverVerdictAgeLine(agePhrase: nil)
       == "Server says this verdict is stale — served from the server's cache.",
       "stale without an age says 'stale' and prints no number at all")
assert(!staleNoAge.serverVerdictAgeLine(agePhrase: nil)!.contains("0s"),
       "the no-age line never falls back to a fabricated '0s' (that reads as just-checked)")

let freshCached = decodeHealthFixture("v1_health_fresh_cache.json")
assert(freshCached.age_seconds == 45 && !freshCached.serverSaysVerdictIsStale,
       "an age under the TTL flags nothing (a fresh cached verdict is not bad news)")
assert(freshCached.serverVerdictAgeLine(agePhrase: "45s") == nil,
       "a verdict the server did not flag renders no age line — no crying wolf")

let coldStart = decodeHealthFixture("v1_health_inline.json")
assert(coldStart.source == "inline" && !coldStart.serverSaysVerdictIsStale,
       "the cold-start inline verdict is never flagged stale — it ran for this very request")
assert(coldStart.serverVerdictAgeLine(agePhrase: "0s") == nil,
       "an inline verdict shows no age line either (0s old would be noise, not honesty)")
// An unrecognised source is echoed, never folded onto cache/inline prose, so a
// source added server-side later cannot be silently mislabelled on screen.
// Inline JSON rather than a fixture file: this shape is not a wire capture, it
// is the defensive arm of the mapping.
let unknownSource: HealthResponse
do {
    let unknownSourceBody = """
    {"ok":true,"checks":[],"speak":"x","age_seconds":300,"stale":true,"source":"ring"}
    """
    unknownSource = try JSONDecoder().decode(HealthResponse.self,
                                             from: Data(unknownSourceBody.utf8))
} catch {
    print("FAIL: an unknown source value must still decode — \(error)")
    exit(1)
}
assert(unknownSource.serverVerdictAgeLine(agePhrase: "5m")
       == "Server says this verdict is 5m old — source: ring.",
       "an unrecognised source is echoed verbatim, not folded onto the cache/inline wording")
assert(preCache.stale == nil, "the pre-#486 capture genuinely has no stale flag to phrase")
assert(staleNoAge.serverVerdictAgeLine(agePhrase: "4m") != nil,
       "the no-age line is still produced when a phrase is offered (age wins when known)")

// 2) Classification: the file's long-standing ANY-HTTP-RESPONSE contract holds
//    (a 500 or a 404 proves reachability), while auth refusal is the ONE
//    non-transport failure that counts as unreachable (operator spec:
//    "transport/auth failure -> unreachable").
assert(ConnectionProbe.classify(status: nil, code: nil) == .transportFailure,
       "no HTTP response at all classifies as transport failure")
assert(ConnectionProbe.classify(status: 401, code: nil) == .authRefused,
       "401 classifies as auth refusal")
assert(ConnectionProbe.classify(status: 403, code: nil) == .authRefused,
       "403 classifies as auth refusal")
assert(ConnectionProbe.classify(status: 400, code: "unauthorized") == .authRefused,
       "the 'unauthorized' machine slug wins even on a non-401 status")
assert(ConnectionProbe.classify(status: 404, code: nil) == .reachable,
       "a 404 still proves the API answered (any-HTTP-response contract)")
assert(ConnectionProbe.classify(status: 500, code: nil) == .reachable,
       "a 500 still proves the API answered (any-HTTP-response contract)")
assert(ConnectionProbe.classify(status: 200, code: nil) == .reachable,
       "a 200 classifies as reachable")

// 3) The loop step: run a probe, APPLY the honest result to the monitor, and
//    hand the report back for pacing. Every case below drives the real
//    `runReachabilityProbe` through the same error values HSCCClient throws.

// 3a) Cold start: drawer opens, monitor never touched -> .unknown ("checking…").
m.reset()
assert(m.status == .unknown, "cold start is .unknown (neutral, no alarm)")

// 3b) API is up: the FIRST probe flips the pill (this is the field bug).
let okReport = await ConnectionProbe.runReachabilityProbe(on: m) {
    _ = try JSONDecoder().decode(HealthResponse.self, from: healthData) // client.health() succeeded
}
assert(okReport == .reachable, "a successful probe reports .reachable")
assert(m.status == .reachable, "the FIRST real probe flips .unknown -> .reachable (pill turns green)")

// 3c) API is down (transport): the pill must show unreachable with the SAME
//     reason the request path uses — dotWord/banner stay consistent.
let tfReport = await ConnectionProbe.runReachabilityProbe(on: m) {
    throw HSCCError.transport(underlying: nil)
}
assert(tfReport == .transportFailure, "a transport failure reports .transportFailure")
if case .unreachable(let reason) = m.status {
    assert(reason == ConnectionMonitor.transportReason,
           "transport probe failure sets .unreachable with the shared transport reason")
} else {
    assert(false, "transport probe failure must set .unreachable")
}

// 3d) NO-PIN contract (t_4889e978, must survive the probe): a real request
//     success after a probe failure clears the alarm immediately.
m.requestSucceeded()
assert(m.status == .reachable, "a real request success after a probe failure CLEARS the alarm (no pin)")

// 3e) Wrong token: server answers, refuses the credential. Honest three-state:
//     that is .unreachable (operator spec), with a token-specific reason so the
//     banner never blames Tailscale for a settings problem.
let arReport = await ConnectionProbe.runReachabilityProbe(on: m) {
    throw HSCCError.api(code: "unauthorized", message: "bad token", status: 401)
}
assert(arReport == .authRefused, "a 401 'unauthorized' probe reports .authRefused (caller backs off)")
if case .unreachable(let reason) = m.status {
    assert(reason == ConnectionMonitor.authRefusedReason,
           "auth refusal sets .unreachable with the token-specific reason")
} else {
    assert(false, "auth refusal must set .unreachable")
}

// 3f) The no-pin contract holds for auth refusals too: fix the token (any real
//     success) and the banner clears — a refused probe can never strand it.
m.requestSucceeded()
assert(m.status == .reachable, "a real request success after an auth refusal CLEARS the alarm (no pin)")

// 3g) A 200 whose body no longer matches HealthResponse (decode drift) still
//     PROVED an HTTP answer arrived -> reachable, per the file's contract.
let decReport = await ConnectionProbe.runReachabilityProbe(on: m) {
    throw HSCCError.decoding("keyNotFound")
}
assert(decReport == .reachable, "a decode failure of a real HTTP response is still reachable")

// 3h) A host so broken no request can even be built behaves like no answer.
let urlReport = await ConnectionProbe.runReachabilityProbe(on: m) {
    throw HSCCError.invalidURL
}
assert(urlReport == .transportFailure, "an unbuildable URL classifies as transport failure (no HTTP answer)")

// 3i) The SAME multi-address loop the clients use: transport failures advance
//     to the next address, an HTTP ANSWER stops the walk, and all-down yields
//     exactly one failure. A probe against a dead first address therefore ends
//     with one honest report after trying the rest, never a per-address alarm.
let failoverDefaults = UserDefaults(suiteName: AppGroup.suiteName)
failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
failoverDefaults?.synchronize()
assert(HostFailover.orderedHosts(primary: "host-a.example",
                                 fallbacks: ["host-b.example", "host-c.example"])
       == ["host-a.example", "host-b.example", "host-c.example"],
       "saved failover order is primary -> fallbacks")

var attempted: [String] = []
let advanced = await HostFailover.run(hosts: ["host-a.example", "host-b.example"]) { host in
    attempted.append(host)
    if host == "host-b.example" { return .answered("answered") }
    return .transport(URLError(.cannotConnectToHost))
}
if case .success(let value, let host) = advanced {
    assert(value == "answered" && host == "host-b.example",
           "a transport failure advances to the next address and returns its answer")
} else {
    assert(false, "the second address should have answered")
}
assert(attempted == ["host-a.example", "host-b.example"],
       "failover tried the primary then the fallback, in order")
assert(HostFailover.lastWorkingHost() == "host-b.example",
       "the answering address is remembered in the App Group")

failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
failoverDefaults?.synchronize()
attempted = []
let answered = await HostFailover.run(hosts: ["host-c.example", "host-d.example"]) { host in
    attempted.append(host)
    return .answered("HTTP 401")
}
if case .success(_, let host) = answered {
    assert(host == "host-c.example", "an HTTP answer is accepted from the first address")
} else {
    assert(false, "an HTTP answer must stop the failover walk")
}
assert(attempted == ["host-c.example"],
       "an HTTP answer (even 401) is NOT a transport failure and never fails over")

failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
failoverDefaults?.synchronize()
attempted = []
let allDown = await HostFailover.run(hosts: ["host-a.example", "host-b.example"]) { host -> HostFailover.Attempt<String> in
    attempted.append(host)
    return .transport(URLError(.cannotConnectToHost))
}
if case .failed(let lastError) = allDown {
    assert((lastError as? URLError)?.code == .cannotConnectToHost,
           "all addresses down returns ONE failure carrying the last transport error")
} else {
    assert(false, "all addresses down must fail")
}
assert(attempted == ["host-a.example", "host-b.example"],
       "all addresses were tried exactly once each")
HostFailover.remember("host-b.example")
assert(HostFailover.orderedHosts(["host-a.example", "host-b.example", "host-c.example"])
       == ["host-b.example", "host-a.example", "host-c.example"],
       "the last-working address leads, then the rest keep their saved order")
failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
failoverDefaults?.synchronize()

// 4) Cluster switch: ConfigView's reset() puts .unknown again — the probe loop
//     restarts on connectionIdentity and re-resolves it. The monitor itself
//     must not spontaneously "remember" the old cluster's reachability.
m.reset()
assert(m.status == .unknown, "reset() returns to .unknown for the new cluster")

// 5) "Test connection" probes EVERY address (operator 2026-11-18). The shared
//    implementation is compiled from Sources/Shared/ConnectionAddressProbe.swift
//    — never redeclared here — and the transport is injected, so this gate
//    opens no socket.

// 5a) The request is the same GET /v1/ping + bearer token + timeout the
//     QRPairing test uses (the probe endpoint is fixed, not caller-supplied).
let probeRequest = ClusterConnectionTest.pingRequest(
    host: "host-a.example", port: 8788, token: "token-a", timeout: 4)
assert(probeRequest?.url?.path == "/v1/ping",
       "the per-address probe is GET /v1/ping (the confirmed route)")
assert(probeRequest?.httpMethod == "GET", "the probe request is a GET")
assert(probeRequest?.value(forHTTPHeaderField: "Authorization") == "Bearer token-a",
       "the probe carries the same bearer token every client uses")
assert(probeRequest?.timeoutInterval == 4,
       "the probe uses the per-attempt connect ceiling")

// 5b) Classification keeps QRPairing's token/version checks: 401/403 (or the
//     machine slug) is a token rejection, a 404/decode drift is a version
//     mismatch, and a good ping is OK with service/version/latency.
assert(ClusterConnectionTest.classify(status: 401, data: Data(), latencyMs: 9)
       == .rejected(.token), "401 is a token rejection, never 'unreachable'")
assert(ClusterConnectionTest.classify(status: 403, data: Data(), latencyMs: 9)
       == .rejected(.token), "403 is a token rejection")
assert(ClusterConnectionTest.classify(
           status: 400,
           data: Data(#"{"error":{"code":"invalid_token"}}"#.utf8),
           latencyMs: 9) == .rejected(.token),
       "the invalid_token machine slug is a token rejection on any status")
assert(ClusterConnectionTest.classify(status: 404, data: Data(), latencyMs: 9)
       == .rejected(.version),
       "a missing /v1/ping route is a version mismatch, not unreachable")
assert(ClusterConnectionTest.classify(
           status: 200,
           data: Data(#"{"ok":true,"service":"hscc-api","version":"3.0.0"}"#.utf8),
           latencyMs: 42)
       == .ok(service: "hscc-api", version: "3.0.0", latencyMs: 42),
       "a good ping reports service + version + latency")
assert(ClusterConnectionTest.classify(
           status: 200, data: Data(#"{"ok":true}"#.utf8), latencyMs: 1)
       == .rejected(.version),
       "a 2xx body that isn't /v1/ping is a version mismatch")
assert(ClusterConnectionTest.classify(error: URLError(.timedOut))
       == .unreachable(.timeout), "a timeout is unreachable(timeout)")
assert(ClusterConnectionTest.classify(error: URLError(.cannotConnectToHost))
       == .unreachable(.refused), "a refusal is unreachable(refused)")
assert(ClusterConnectionTest.classify(error: URLError(.cannotFindHost))
       == .unreachable(.dns), "a bad name is unreachable(dns)")
assert(ClusterConnectionTest.classify(error: URLError(.serverCertificateUntrusted))
       == .unreachable(.tls), "a certificate failure is unreachable(tls)")

// Helper: clear the remembered address so each case starts honestly.
func clearLastWorking() {
    failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
    failoverDefaults?.synchronize()
}

// 5c) All addresses OK: one row each, overall success, fastest remembered.
clearLastWorking()
let allOK = await ClusterConnectionTest.run(
    hosts: ["host-a.example", "host-b.example", "host-c.example"]
) { host in
    switch host {
    case "host-a.example": return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 40)
    case "host-b.example": return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 12)
    default: return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 25)
    }
}
let allOKReport = ClusterConnectionTest.report(from: allOK)
assert(allOKReport.total == 3 && allOKReport.okCount == 3,
       "all-OK: three addresses produce three OK rows")
assert(allOKReport.overallSuccess, "all-OK: overall test succeeds")
assert(allOKReport.overallLine == "Connected — 3 of 3 addresses reachable",
       "all-OK: the overall line names n of m reachable")
assert(allOKReport.selectedHost == "host-b.example",
       "all-OK: the fastest OK address is the one the app will use")
assert(HostFailover.lastWorkingHost() == "host-b.example",
       "all-OK: the fastest OK address is remembered for widgets")

// 5d) First down + second OK: BOTH rows are reported, overall still succeeds.
clearLastWorking()
let firstDown = await ClusterConnectionTest.run(
    hosts: ["host-a.example", "host-b.example"]
) { host in
    if host == "host-a.example" { return .unreachable(.timeout) }
    return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 18)
}
let firstDownReport = ClusterConnectionTest.report(from: firstDown)
assert(firstDownReport.total == 2,
       "first down: both the down and the working address are reported")
assert(firstDownReport.results[0].outcome == .unreachable(.timeout),
       "first down: the dead primary is reported as unreachable(timeout)")
assert(firstDownReport.results[1].isOK,
       "first down: the fallback is reported as OK")
assert(firstDownReport.overallSuccess
       && firstDownReport.overallLine == "Connected — 1 of 2 addresses reachable",
       "first down + second OK is an overall success naming 1 of 2")
assert(firstDownReport.selectedHost == "host-b.example",
       "first down + second OK selects the only working address")

// 5e) One 401 among OKs: the refusal is a row, never a reason to stop probing,
//     and the overall test still succeeds on the working addresses.
clearLastWorking()
let oneRejected = await ClusterConnectionTest.run(
    hosts: ["host-a.example", "host-b.example", "host-c.example"]
) { host in
    switch host {
    case "host-a.example": return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 30)
    case "host-b.example": return .rejected(.token)
    default: return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 8)
    }
}
let oneRejectedReport = ClusterConnectionTest.report(from: oneRejected)
assert(oneRejectedReport.total == 3 && oneRejectedReport.okCount == 2,
       "one 401 among OKs: the rejected row does not hide the working ones")
assert(oneRejectedReport.results[1].outcome == .rejected(.token),
       "one 401 among OKs: the address is reported as token-rejected")
assert(oneRejectedReport.results[1].detail == "token rejected",
       "one 401 among OKs: the row shows a short reason, not a latency")
assert(oneRejectedReport.overallSuccess
       && oneRejectedReport.overallLine == "Connected — 2 of 3 addresses reachable",
       "one 401 among OKs is still overall success naming 2 of 3")
assert(oneRejectedReport.selectedHost == "host-c.example",
       "one 401 among OKs: the fastest working address is selected")

// 5f) All down: every address gets a row, the overall test fails with the
//     existing failure copy, and the remembered address is NOT overwritten.
clearLastWorking()
HostFailover.remember("host-b.example")
let allDownReport = ClusterConnectionTest.report(
    from: await ClusterConnectionTest.run(
        hosts: ["host-a.example", "host-b.example", "host-c.example"]
    ) { host in
        switch host {
        case "host-a.example": return .unreachable(.refused)
        case "host-b.example": return .unreachable(.dns)
        default: return .unreachable(.timeout)
        }
    })
assert(allDownReport.total == 3 && allDownReport.okCount == 0,
       "all down: one row per address, zero OK")
assert(!allDownReport.overallSuccess,
       "all down: the overall test is a failure")
assert(!allDownReport.overallLine.contains("Connected"),
       "all down: the overall line never claims a connection")
assert(allDownReport.selectedHost == nil,
       "all down: no address is marked as the one the app will use")
assert(HostFailover.lastWorkingHost() == "host-b.example",
       "all down: a failed test does not overwrite the remembered address")
clearLastWorking()

// 5g) Order preservation under out-of-order completion: the slow first probe
//     finishes after the others, but the rows stay in the operator's order.
let outOfOrder = await ClusterConnectionTest.run(
    hosts: ["host-a.example", "host-b.example", "host-c.example"]
) { host in
    if host == "host-a.example" {
        try? await Task.sleep(nanoseconds: 30_000_000)
        return .ok(service: "hscc-api", version: "3.0.0", latencyMs: 3)
    }
    return .unreachable(.refused)
}
assert(outOfOrder.map(\.host)
       == ["host-a.example", "host-b.example", "host-c.example"],
       "report rows keep the operator's primary -> fallback order")
assert(outOfOrder[0].isOK && !outOfOrder[1].isOK && !outOfOrder[2].isOK,
       "order preservation: each row keeps its own outcome")
clearLastWorking()

print("ALL connection-probe assertions passed.")
