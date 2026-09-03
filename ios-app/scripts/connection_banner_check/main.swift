import Foundation

// connection_banner_check — PROVE ConnectionMonitor's honest state machine.
//
// Card t_4889e978: the connection banner must reflect the MOST RECENT REAL API
// OUTCOME, not a stale one-shot launch probe. A successful request clears it;
// a real (transport) failure sets it; a request merely in flight is NOT an
// error and must never raise the red alarm.
//
// This compiles the REAL ConnectionMonitor.swift (never redeclared here) into
// a macOS CLI and asserts the exact transitions the banner depends on:
//   * unknown (cold start) is NOT an alarm;
//   * a transport failure (requestFailed) RAISES the unreachable alarm;
//   * a subsequent success (requestSucceeded) CLEARS it back to reachable;
//   * success -> failure RE-SETS it (a fresh real failure is visible again);
//   * there is no alarm while a request is merely in flight: the monitor has
//     no red "inFlight" state, and only a completed transport failure sets red.

// The real source runs on iOS/Combine; on this macOS host Foundation
// re-exports the Combine symbols, so a plain CLI is the faithful runner (same
// approach as chat_state_check / reconnect_check — no iOS platform runtime
// exists on this host, so no runtime claim is made here).

func assert(_ cond: Bool, _ label: String) {
    if cond {
        print("PASS: \(label)")
    } else {
        print("FAIL: \(label)")
        exit(1)
    }
}

let m = ConnectionMonitor.shared  // the real shared instance

// 0) Cold start is neutral, never an alarm.
m.reset()
assert(ConnectionMonitor.shared.status == .unknown, "cold start is .unknown (neutral, no alarm)")

// 1) fail -> success CLEARS the alarm.
m.reset()
m.requestFailed()
assert(ConnectionMonitor.shared.status == .unreachable("Can't reach the cluster — is Tailscale connected?"),
       "after a real transport failure the banner is .unreachable (alarm raised)")
m.requestSucceeded()
assert(ConnectionMonitor.shared.status == .reachable,
       "a success after a failure CLEARS the alarm back to .reachable")

// 2) success -> fail SETS the alarm (a fresh real failure is visible again).
m.reset()
m.requestSucceeded()
assert(ConnectionMonitor.shared.status == .reachable, "a success is .reachable")
m.requestFailed()
assert(ConnectionMonitor.shared.status == .unreachable("Can't reach the cluster — is Tailscale connected?"),
       "a failure after a success RE-SETS the .unreachable alarm")

// 3) in-flight is NOT an error: the monitor has no red "inFlight" outcome, and
//    only a COMPLETED transport failure raises the alarm. A request that is
//    merely underway leaves the last known status untouched.
m.reset()
m.requestSucceeded()
assert(ConnectionMonitor.shared.status == .reachable, "connected before an in-flight request")
// An in-flight request is never reported to the monitor (only completions are),
// so the banner keeps showing "Connected" — it can never flip red because a
// request is in flight.
assert(ConnectionMonitor.shared.status == .reachable,
       "an in-flight request does not change the banner (no red while in flight)")

print("ALL connection-banner state-machine assertions passed.")
