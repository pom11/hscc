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

// ===========================================================================
// PART 3 — the cluster-health activity (t_0120475a).
//
// NOTHING here is transcribed either. `ClusterHealthSignals`,
// `ClusterHealthTone`, `ClusterHealthReading` and `ClusterHealthOrphanRule` are
// SLICED out of Sources/Shared/ClusterHealthActivity.swift by the .sh that
// compiles this file, so what is under test is the rule
// `ClusterHealthActivitySweep` applies at launch and the copy, tally and colour
// `ClusterHealthLiveActivity` draws. `ClusterState` comes from the real
// Sources/Shared/SharedModels.swift and is used ONLY by the anti-drift
// assertion at the end of this part.
// ===========================================================================

/// Builds the pushed signals. Argument order is the field order of
/// `ClusterHealthSignals`, so a renamed or reordered field is a compile error
/// here rather than a silently wrong table.
func clusterHealth(_ servingState: String = "serving",
                   _ up: Int? = 3,
                   _ down: Int? = 0,
                   _ total: Int? = 4,
                   _ failureLabel: String? = nil,
                   _ downNodes: [String] = []) -> ClusterHealthSignals {
    ClusterHealthSignals(servingState: servingState,
                         nodesUp: up,
                         nodesDown: down,
                         nodesTotal: total,
                         failureLabel: failureLabel,
                         downNodes: downNodes)
}

let healthy = clusterHealth()                                  // 3/4 up, nothing down
let fleetDown = clusterHealth("down", 0, 4, 4)                  // the server said zero
let blindNumbers = clusterHealth("serving", 3, 0, 4, "No connection")
let empty = clusterHealth("unknown", nil, nil, nil, nil, [])

print("\nCluster health — orphan decision (real ClusterHealthOrphanRule, sliced from source)\n")

let clusterBudget = ClusterHealthOrphanRule.maxSilence

// A signal plus a recent push => a live feeder. Ending it blanks the surface.
check("3/4 up + 2m -> KEEP", !ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: quiet2m, now: now))
// THE difference from the running-cards rule: a fleet the server stated is DOWN
// is the most useful thing this surface can be showing, so `nodesUp == 0` must
// NOT end it. Zero rows means "no work to be ambient about"; zero nodes up means
// the cluster is broken.
check("down fleet (0/4 up) + 2m -> KEEP (a stated zero is an answer)",
      !ClusterHealthOrphanRule.isOrphan(fleetDown, updatedAt: quiet2m, now: now))
check("blind read with last-known numbers + 2m -> KEEP",
      !ClusterHealthOrphanRule.isOrphan(blindNumbers, updatedAt: quiet2m, now: now))
check("3/4 up + exactly 6h -> KEEP (<=)",
      !ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: now.addingTimeInterval(-clusterBudget), now: now))
check("3/4 up + 6h0.001s -> end (>budget)",
      ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: now.addingTimeInterval(-(clusterBudget + 0.001)), now: now))
check("3/4 up + 12h -> end", ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: quiet12h, now: now))
check("down fleet + 12h -> end (silence outranks the news)",
      ClusterHealthOrphanRule.isOrphan(fleetDown, updatedAt: quiet12h, now: now))

// No signal at all is an orphan whatever its age: no region can draw anything
// from it, and no real driver pushes one.
check("empty state + 2m -> end (freshness cannot save it)",
      ClusterHealthOrphanRule.isOrphan(empty, updatedAt: quiet2m, now: now))
check("empty state + 12h -> end", ClusterHealthOrphanRule.isOrphan(empty, updatedAt: quiet12h, now: now))
check("empty state + future stamp -> end",
      ClusterHealthOrphanRule.isOrphan(empty, updatedAt: now.addingTimeInterval(300), now: now))
// The two ways a bubble stops being empty: a number the server stated, or a label.
check("unknown + a counted 0/0 + 2m -> KEEP (zero is a number, not a gap)",
      !ClusterHealthOrphanRule.isOrphan(clusterHealth("unknown", 0, 0, 0), updatedAt: quiet2m, now: now))
check("unknown + failure label + 2m -> KEEP (a blind bubble says so)",
      !ClusterHealthOrphanRule.isOrphan(clusterHealth("unknown", nil, nil, nil, "No connection"),
                                        updatedAt: quiet2m, now: now))

print("\nCluster health — what counts as a signal\n")

check("healthy is a signal", !healthy.isEmpty)
check("no state, no numbers, no label -> empty", empty.isEmpty)
check("a counted zero is NOT empty", !clusterHealth("unknown", 0, 0, 0).isEmpty)
check("a failure label is NOT empty", !clusterHealth("unknown", nil, nil, nil, "No connection").isEmpty)
check("a BLANK label is not a signal", clusterHealth("unknown", nil, nil, nil, "").isEmpty)
check("a state word alone is a signal", !clusterHealth("serving", nil, nil, nil).isEmpty)

print("\nCluster health — what a surface may print (real ClusterHealthReading)\n")

func checkList(_ name: String, _ got: [String], _ want: [String]) {
    check(name, got == want, "got \(got), want \(want)")
}
func checkOpt(_ name: String, _ got: String?, _ want: String?) {
    check(name, got == want, "got \(String(describing: got)), want \(String(describing: want))")
}
func checkTone(_ name: String, _ got: ClusterHealthTone, _ want: ClusterHealthTone) {
    check(name, got == want, "got \(got.rawValue), want \(want.rawValue)")
}

// The state words. An unrecognised one must not be dressed up as a modelled one.
checkText("serving", ClusterHealthReading.stateLabel("serving"), "Serving")
checkText("waking", ClusterHealthReading.stateLabel("waking"), "Waking")
checkText("down", ClusterHealthReading.stateLabel("down"), "Down")
checkText("unreachable", ClusterHealthReading.stateLabel("unreachable"), "Can't reach")
checkText("unknown", ClusterHealthReading.stateLabel("unknown"), "Unknown")
checkText("a state we do not model is not shown as one we do",
          ClusterHealthReading.stateLabel("quantum"), "Unknown")
checkText("an empty state word is unknown", ClusterHealthReading.stateLabel(""), "Unknown")

// The tallies: `—` is what "not told" looks like, and `0` is what "asked and
// the answer was none" looks like. They never substitute for each other.
checkText("tally: up of total", ClusterHealthReading.tallyText(up: 3, total: 4), "3/4 up")
checkText("tally: no total named", ClusterHealthReading.tallyText(up: 3, total: nil), "3 up")
checkText("tally: not told -> em dash, never a zero",
          ClusterHealthReading.tallyText(up: nil, total: 4), "— up")
checkText("tally: a counted zero is a zero", ClusterHealthReading.tallyText(up: 0, total: 4), "0/4 up")
checkText("down: nil is not zero", ClusterHealthReading.downText(down: nil), "— down")
checkText("down: zero is zero", ClusterHealthReading.downText(down: 0), "0 down")
checkText("down: singular", ClusterHealthReading.downText(down: 1), "1 down")
checkText("down: plural", ClusterHealthReading.downText(down: 3), "3 down")

// The named down nodes: clipped to what a fixed-height region can hold, and the
// clipped remainder is counted rather than dropped.
checkList("names clipped to the region's limit",
          ClusterHealthReading.downNodeList([".244", ".246", ".247"], limit: 2), [".244", ".246"])
checkList("a blank label never takes a slot",
          ClusterHealthReading.downNodeList([".244", "", ".246"], limit: 2), [".244", ".246"])
checkList("a zero limit draws nothing", ClusterHealthReading.downNodeList([".244"], limit: 0), [])
checkList("a negative limit cannot crash the view", ClusterHealthReading.downNodeList([".244"], limit: -1), [])
checkOpt("dropped names are counted", ClusterHealthReading.moreText(shown: 2, down: 4), "+2 more")
checkOpt("nothing dropped -> nothing appended", ClusterHealthReading.moreText(shown: 2, down: 2), nil)
checkOpt("no count -> nothing appended", ClusterHealthReading.moreText(shown: 2, down: nil), nil)
checkOpt("a count below the shown names never reads negative",
         ClusterHealthReading.moreText(shown: 2, down: 1), nil)

// The headline.
checkText("serving with a tally", ClusterHealthReading.headline(healthy), "Serving · 3/4 up")
checkText("a down fleet states its zero", ClusterHealthReading.headline(fleetDown), "Down · 0/4 up")
checkText("waking with a tally",
          ClusterHealthReading.headline(clusterHealth("waking", 3, 1, 4)), "Waking · 3/4 up")
checkText("no total named -> the number it has, never a guessed denominator",
          ClusterHealthReading.headline(clusterHealth("serving", 3, 0, nil)), "Serving · 3 up")
checkText("a blind read leads with its own label",
          ClusterHealthReading.headline(clusterHealth("unknown", nil, nil, nil, "No connection")),
          "No connection")
checkText("an empty state is an em dash, never '0/0 up'", ClusterHealthReading.headline(empty), "—")
checkText("a blank label is not a headline",
          ClusterHealthReading.headline(clusterHealth("unknown", nil, nil, nil, "")), "—")
checkText("a stale read keeps its state word (the age + label carry the staleness)",
          ClusterHealthReading.headline(blindNumbers), "Serving · 3/4 up")

// The compact Island region: ~4 characters, and still four distinct answers.
checkText("compact: up of total", ClusterHealthReading.compactTrailing(healthy), "3/4")
checkText("compact: a counted zero is shown", ClusterHealthReading.compactTrailing(fleetDown), "0/4")
checkText("compact: clipped, never wrapped",
          ClusterHealthReading.compactTrailing(clusterHealth("serving", 12, 1, 14)), "9+")
checkText("compact: a told up with no total still prints",
          ClusterHealthReading.compactTrailing(clusterHealth("serving", 3, nil, nil)), "3")
checkText("compact: a read that produced nothing",
          ClusterHealthReading.compactTrailing(clusterHealth("unknown", nil, nil, nil, "No connection")), "!")
checkText("compact: nothing told at all", ClusterHealthReading.compactTrailing(empty), "—")

// The colour. The order of these rows IS the rule's order.
checkTone("serving, nothing down -> ok", ClusterHealthReading.tone(healthy), .ok)
checkTone("serving WITH a node down -> warn, not mint",
          ClusterHealthReading.tone(clusterHealth("serving", 3, 1, 4)), .warn)
checkTone("serving, down count not told -> ok (silence is not a fault)",
          ClusterHealthReading.tone(clusterHealth("serving", 3, nil, 4)), .ok)
checkTone("fleet down -> bad", ClusterHealthReading.tone(fleetDown), .bad)
checkTone("fleet down with no node numbers -> bad",
          ClusterHealthReading.tone(clusterHealth("down", nil, nil, nil)), .bad)
checkTone("waking -> warn even with nothing down",
          ClusterHealthReading.tone(clusterHealth("waking", 4, 0, 4)), .warn)
checkTone("unreachable -> neutral",
          ClusterHealthReading.tone(clusterHealth("unreachable", nil, nil, nil)), .neutral)
checkTone("unknown -> neutral (an unknown state is not a healthy one)",
          ClusterHealthReading.tone(clusterHealth("unknown", 4, 0, 4)), .neutral)
checkTone("blind with last-known numbers -> warn", ClusterHealthReading.tone(blindNumbers), .warn)
checkTone("blind with nothing -> bad",
          ClusterHealthReading.tone(clusterHealth("serving", nil, nil, nil, "Token rejected")), .bad)

// Age: a clock ahead of this device is "now", not a negative age.
checkText("age: seconds", ClusterHealthReading.pushedAgeText(updatedAt: now, now: now), "now")
checkText("age: 90s", ClusterHealthReading.pushedAgeText(updatedAt: now.addingTimeInterval(-90), now: now), "1m ago")
checkText("age: 59m", ClusterHealthReading.pushedAgeText(updatedAt: now.addingTimeInterval(-59 * 60), now: now), "59m ago")
checkText("age: 61m crosses the hour", ClusterHealthReading.pushedAgeText(updatedAt: now.addingTimeInterval(-61 * 60), now: now), "1h01m ago")
checkText("age: a future stamp is now", ClusterHealthReading.pushedAgeText(updatedAt: now.addingTimeInterval(600), now: now), "now")

// ---- the anti-drift assertion ---------------------------------------------
// `stateLabel` is a copy table, and a copy table is the shape this directory has
// twice had to delete. So its coverage is proven against the REAL `ClusterState`
// (compiled from Sources/Shared/SharedModels.swift, the same enum the app and
// the widget use): a raw value this enum no longer has must not keep a word
// here. The copy itself is deliberately NOT identical — `ClusterState.label`
// says "Can't reach the cluster" and a compact Island region cannot fit that —
// so what is asserted is coverage, not wording.
//
// Known limit, stated rather than hidden: `ClusterState` is not CaseIterable, so
// an ADDED sixth case cannot be enumerated from here. A renamed or removed one
// is caught by the first check below.
print("\nCluster health — the state table against the real ClusterState\n")

for raw in ["serving", "waking", "down", "unreachable", "unknown"] {
    check("\"\(raw)\" is still a real ClusterState", ClusterState(rawValue: raw) != nil)
    // `unknown` is the one state whose copy IS the fallback word, so it cannot
    // be the drift probe; the other four must have copy of their own.
    if raw != "unknown" {
        check("stateLabel(\"\(raw)\") is not the fallback",
              ClusterHealthReading.stateLabel(raw) != "Unknown",
              "got \"\(ClusterHealthReading.stateLabel(raw))\"")
    }
}
check("every real state has copy of its own (none empty)",
      ["serving", "waking", "down", "unreachable", "unknown"]
        .allSatisfy { !ClusterHealthReading.stateLabel($0).isEmpty })
check("no two states share a word (a surface cannot render two states alike)",
      Set(["serving", "waking", "down", "unreachable", "unknown"]
            .map { ClusterHealthReading.stateLabel($0) }).count == 5)
check("a state ClusterState does not have gets no word here",
      ClusterState(rawValue: "quantum") == nil
      && ClusterHealthReading.stateLabel("quantum") == "Unknown")

print()
if failures == 0 {
    print("ALL PASS — wake decider (transcribed) + running-cards and cluster-health orphan rules and reading rules (sliced from source) cover every documented case")
} else {
    print("\(failures) FAILURE(S)")
}
exit(failures == 0 ? 0 : 1)
