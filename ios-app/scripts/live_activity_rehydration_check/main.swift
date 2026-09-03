import Foundation

// ===========================================================================
// live_activity_rehydration_check — prove the re-hydration STALENESS DECIDER.
//
// The launch sweep (LiveActivityManager.sweepLeftoverWakes) reads the live
// `Activity.activities` list, which only exists on iOS at runtime — it cannot
// be executed on this macOS host (no ActivityKit). What CAN be proven headlessly
// is the decision rule: given a found wake's (stateLabel, age), does the sweep
// end it or leave it alone?
//
// This script reproduces the EXACT predicate used in sweepLeftoverWakes and
// asserts the full decision table, so the arithmetic + Boolean algebra behind
// the "genuinely in flight vs stale orphan" heuristic is executed and proven.
//
// Run via scripts/live_activity_rehydration_check.sh.
// ===========================================================================

// ---- mirror of LiveActivityManager.swift constants ----
let wakeMaxInflight: TimeInterval = 10 * 60       // 600s — matches source

// ---- THE decider, transcribed verbatim from sweepLeftoverWakes ----
func isGenuinelyInFlight(stateLabel: String, startedAt: Date?, now: Date) -> Bool {
    stateLabel == "waking"
        && startedAt.map { now.timeIntervalSince($0) <= wakeMaxInflight } ?? false
}

var failures = 0
func check(_ name: String, _ cond: @autoclosure () -> Bool, _ detail: String = "") {
    if cond() {
        print("  ok: \(name)")
    } else {
        failures += 1
        print("FAIL: \(name) \(detail)")
    }
}

// ---- decision table ----
let now = Date()
let recent = now.addingTimeInterval(-2 * 60)        // 2 min ago — well within budget
let almost = now.addingTimeInterval(-9 * 60)        // 9 min — still within budget
let over = now.addingTimeInterval(-11 * 60)         // just over budget -> stale
print("Decision table (stateLabel, age) -> end? (inFlight == keep alive)\n")

// State NOT "waking" => settled-but-orphaned => NOT in flight => END.
check("'up' + recent -> end", !isGenuinelyInFlight(stateLabel: "up", startedAt: recent, now: now))
check("'down' + recent -> end", !isGenuinelyInFlight(stateLabel: "down", startedAt: recent, now: now))
check("'unknown'/other + recent -> end", !isGenuinelyInFlight(stateLabel: "other", startedAt: recent, now: now))

// 'waking' + startedAt nil => a real wake always records startedAt, so => END.
check("'waking' + nil startedAt -> end", !isGenuinelyInFlight(stateLabel: "waking", startedAt: nil, now: now))

// 'waking' + recent => genuinely in flight => KEEP (do not kill a legitimate wake).
check("'waking' + 2min -> KEEP", isGenuinelyInFlight(stateLabel: "waking", startedAt: recent, now: now))
check("'waking' + 9min -> KEEP (boundary inside budget)", isGenuinelyInFlight(stateLabel: "waking", startedAt: almost, now: now))

// 'waking' + over budget => stuck orphan => END.
check("'waking' + 11min -> end", !isGenuinelyInFlight(stateLabel: "waking", startedAt: over, now: now))

// Boundary: EXACTLY at budget (600s) is still <=, so keep. Just past is end.
let exact = now.addingTimeInterval(-wakeMaxInflight)
let justOver = now.addingTimeInterval(-(wakeMaxInflight + 0.001))
check("'waking' + exactly 600s -> KEEP (<=)", isGenuinelyInFlight(stateLabel: "waking", startedAt: exact, now: now))
check("'waking' + 600.001s -> end (>0)", !isGenuinelyInFlight(stateLabel: "waking", startedAt: justOver, now: now))

print()
if failures == 0 {
    print("ALL PASS — re-hydration staleness decider covers every documented case")
} else {
    print("\(failures) FAILURE(S)")
}
exit(failures == 0 ? 0 : 1)
