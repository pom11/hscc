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

// ---- boundary: exactly at budget is still <=, so keep. Just past is end.
let exact = now.addingTimeInterval(-wakeMaxInflight)
let justOver = now.addingTimeInterval(-(wakeMaxInflight + 0.001))
check("'waking' + exactly 600s -> KEEP (<=)", isGenuinelyInFlight(stateLabel: "waking", startedAt: exact, now: now))
check("'waking' + 600.001s -> end (>0)", !isGenuinelyInFlight(stateLabel: "waking", startedAt: justOver, now: now))

// ===========================================================================
// PART 2 — the running-cards activity (t_f9cc34e0).
//
// NOTHING in this section is transcribed. `CardsRunningOrphanRule` and
// `CardsRunningReading` are SLICED out of
// Sources/Shared/CardsRunningActivity.swift by the .sh that compiles this file
// (marker-bracketed blocks, Foundation-only), so the code under test here is
// the code the app's launch sweep calls and the extension's views draw — a
// rename or a moved marker fails the script before this table ever runs.
// ===========================================================================

print("\nRunning-cards — orphan decision (real CardsRunningOrphanRule, sliced from source)\n")

let budget = CardsRunningOrphanRule.maxSilence
let quiet2m   = now.addingTimeInterval(-2 * 60)
let quiet5h59 = now.addingTimeInterval(-(6 * 60 * 60 - 60))
let quiet6h   = now.addingTimeInterval(-budget)
let quiet6h01 = now.addingTimeInterval(-(budget + 0.001))
let quiet12h  = now.addingTimeInterval(-12 * 60 * 60)

// Rows + a recent push => a live feeder. Ending it would blank a working surface.
check("3 rows + 2m -> KEEP", !CardsRunningOrphanRule.isOrphan(rowCount: 3, updatedAt: quiet2m, now: now))
check("1 row + 5h59m -> KEEP (inside budget)", !CardsRunningOrphanRule.isOrphan(rowCount: 1, updatedAt: quiet5h59, now: now))
check("1 row + exactly 6h -> KEEP (<=)", !CardsRunningOrphanRule.isOrphan(rowCount: 1, updatedAt: quiet6h, now: now))

// Rows but silence past the budget => the poll loop is gone.
check("1 row + 6h0.001s -> end (>budget)", CardsRunningOrphanRule.isOrphan(rowCount: 1, updatedAt: quiet6h01, now: now))
check("3 rows + 12h -> end", CardsRunningOrphanRule.isOrphan(rowCount: 3, updatedAt: quiet12h, now: now))

// No rows is an orphan whatever its age: the driver ends the activity the
// moment it learns nothing runs, and a blind read with no fallback rows names
// no card either. Both cases must end, and neither may be "kept" by being new.
check("0 rows + 2m -> end (fresh does not save it)", CardsRunningOrphanRule.isOrphan(rowCount: 0, updatedAt: quiet2m, now: now))
check("0 rows + 12h -> end", CardsRunningOrphanRule.isOrphan(rowCount: 0, updatedAt: quiet12h, now: now))
check("0 rows + future stamp -> end (age cannot rescue it)", CardsRunningOrphanRule.isOrphan(rowCount: 0, updatedAt: now.addingTimeInterval(300), now: now))

print("\nRunning-cards — what a surface may print (real CardsRunningReading)\n")

func checkText(_ name: String, _ got: String, _ want: String) {
    check(name, got == want, "got \"\(got)\", want \"\(want)\"")
}
func checkCount(_ name: String, _ got: Int?, _ want: Int?) {
    check(name, got == want, "got \(String(describing: got)), want \(String(describing: want))")
}

// The count: the server's own number wins; rows are counted only when they are
// the whole set; otherwise there is no number to print.
checkCount("server count beats the rows",
           CardsRunningReading.displayCount(runningCount: 7, rowCount: 3, rowsAreComplete: true), 7)
checkCount("no count + complete rows -> row count",
           CardsRunningReading.displayCount(runningCount: nil, rowCount: 3, rowsAreComplete: true), 3)
checkCount("no count + SUBSET rows -> nil (never the row count)",
           CardsRunningReading.displayCount(runningCount: nil, rowCount: 3, rowsAreComplete: false), nil)
checkCount("a counted zero stays a zero",
           CardsRunningReading.displayCount(runningCount: 0, rowCount: 0, rowsAreComplete: true), 0)

// The headline: a blind read leads with its own label; a number only ever comes
// from a read that answered.
checkText("blind read leads with its label",
          CardsRunningReading.headline(count: nil, blockedCount: nil, failureLabel: "API offline"),
          "API offline")
checkText("blind read can never say 0 running",
          CardsRunningReading.headline(count: nil, blockedCount: 3, failureLabel: "API offline").contains("0 running") ? "0 running" : "honest",
          "honest")
checkText("live zero is a fact",
          CardsRunningReading.headline(count: 0, blockedCount: nil, failureLabel: nil),
          "0 running")
checkText("one card reads singular",
          CardsRunningReading.headline(count: 1, blockedCount: nil, failureLabel: nil),
          "1 running")
checkText("blocked joins when known",
          CardsRunningReading.headline(count: 2, blockedCount: 1, failureLabel: nil),
          "2 running · 1 blocked")
checkText("stale rows keep their number (the warn line carries the failure)",
          CardsRunningReading.headline(count: 4, blockedCount: nil, failureLabel: "API offline"),
          "4 running")
checkText("no count, no label -> em dash, not zero",
          CardsRunningReading.headline(count: nil, blockedCount: nil, failureLabel: nil),
          "—")
checkText("an empty label is not a headline",
          CardsRunningReading.headline(count: nil, blockedCount: nil, failureLabel: ""),
          "—")

// The compact Island region: ~4 characters, and still three distinct answers.
checkText("compact: a counted set", CardsRunningReading.compactTrailing(count: 12, failureLabel: nil), "12")
checkText("compact: clipped, never wrapped", CardsRunningReading.compactTrailing(count: 120, failureLabel: nil), "99+")
checkText("compact: a read that produced nothing", CardsRunningReading.compactTrailing(count: nil, failureLabel: "API offline"), "!")
checkText("compact: a subset with no count", CardsRunningReading.compactTrailing(count: nil, failureLabel: nil), "—")
checkText("compact: a counted zero is shown", CardsRunningReading.compactTrailing(count: 0, failureLabel: nil), "0")

// Blocked: a route that did not answer never reads as "nothing blocked".
checkText("blocked: nil is not zero", CardsRunningReading.blockedText(nil), "— blocked")
checkText("blocked: zero is zero", CardsRunningReading.blockedText(0), "0 blocked")
checkText("blocked: singular", CardsRunningReading.blockedText(1), "1 blocked")

// Age: a clock ahead of this device is "now", not a negative age.
checkText("age: seconds", CardsRunningReading.pushedAgeText(updatedAt: now, now: now), "now")
checkText("age: 90s", CardsRunningReading.pushedAgeText(updatedAt: now.addingTimeInterval(-90), now: now), "1m ago")
checkText("age: 59m", CardsRunningReading.pushedAgeText(updatedAt: now.addingTimeInterval(-59 * 60), now: now), "59m ago")
checkText("age: 61m crosses the hour", CardsRunningReading.pushedAgeText(updatedAt: now.addingTimeInterval(-61 * 60), now: now), "1h01m ago")
checkText("age: a future stamp is now", CardsRunningReading.pushedAgeText(updatedAt: now.addingTimeInterval(600), now: now), "now")

print()
if failures == 0 {
    print("ALL PASS — wake decider (transcribed) + running-cards orphan rule and reading rules (sliced from source) cover every documented case")
} else {
    print("\(failures) FAILURE(S)")
}
exit(failures == 0 ? 0 : 1)
