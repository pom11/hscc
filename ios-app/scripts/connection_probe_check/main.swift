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

// 4) Cluster switch: AppShell's reset() puts .unknown again — the probe loop
//     restarts on connectionIdentity and re-resolves it. The monitor itself
//     must not spontaneously "remember" the old cluster's reachability.
m.reset()
assert(m.status == .unknown, "reset() returns to .unknown for the new cluster")

print("ALL connection-probe assertions passed.")
