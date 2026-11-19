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
          CardsRunningReading.headline(count: nil, failureLabel: "API offline"),
          "API offline")
checkText("blind read can never say 0 running",
          CardsRunningReading.headline(count: nil, failureLabel: "API offline").contains("0 running") ? "0 running" : "honest",
          "honest")
checkText("live zero is a fact",
          CardsRunningReading.headline(count: 0, failureLabel: nil),
          "0 running")
checkText("one card reads singular",
          CardsRunningReading.headline(count: 1, failureLabel: nil),
          "1 running")
checkText("the headline is the running count, never a blocked total",
          CardsRunningReading.headline(count: 2, failureLabel: nil),
          "2 running")
checkText("stale rows keep their number (the warn line carries the failure)",
          CardsRunningReading.headline(count: 4, failureLabel: "API offline"),
          "4 running")
checkText("no count, no label -> em dash, not zero",
          CardsRunningReading.headline(count: nil, failureLabel: nil),
          "—")
checkText("an empty label is not a headline",
          CardsRunningReading.headline(count: nil, failureLabel: ""),
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

// ---------------------------------------------------------------------------
// PART 4 — the projection the driver pushes through (t_f2867fb3).
//
// `ClusterHealthActivityDriver` is ActivityKit + BackgroundTasks and cannot be
// compiled here — nor should it need to be. Every decision it makes about WHAT
// the two reads mean is the sliced `ClusterHealthProjection`; every decision
// about WHEN to push is a scheduling one no macOS harness can exercise. This is
// the table for the first kind, which is the kind that can lie about a degraded
// fleet: a route that degraded to `{speak}`, a host list with errors in it, a
// read that timed out, a section the operator switched off.
//
// `ClusterNodeTally` is compiled in rather than sliced: it is a Foundation-only
// FILE (the one host tally the cluster widget also runs), and `cluster_widget_check.sh`
// executes the same code from the widget's side. Asserting it here too is what
// keeps the two surfaces from drifting apart without anything noticing.
//
// Host strings are 192.0.2.N — RFC 5737 TEST-NET-1, the same documentation range
// the committed /v1/cluster/monitor fixture uses — so no real address can reach
// this file, and the labels drawn below must be `.N` tails whatever the input.
// ---------------------------------------------------------------------------
print("\nCluster health — the projection the driver pushes (sliced)\n")

func autodown(_ state: String?) -> AutodownStatusResponse? {
    var body = "{\"speak\":\"ok\""
    if let state { body += ",\"state\":\"\(state)\"" }
    body += "}"
    return try? JSONDecoder().decode(AutodownStatusResponse.self, from: Data(body.utf8))
}

/// nil hosts = the down-fleet shape §4.4 measured: the route answers, `json`
/// is simply not in the body, and a required key here would decode-fail a fleet
/// the server just told us is off.
func monitor(_ hosts: [String]?) -> MonitorResponse? {
    let body = hosts.map { hosts in
        "{\"speak\":\"ok\",\"json\":{\"hosts\":[\(hosts.joined(separator: ","))]}}"
    } ?? "{\"speak\":\"cluster monitor unavailable\"}"
    return try? JSONDecoder().decode(MonitorResponse.self, from: Data(body.utf8))
}

func upHost(_ tail: String) -> String { "{\"host\":\"192.0.2.\(tail)\",\"error\":null}" }
func badHost(_ tail: String) -> String { "{\"host\":\"192.0.2.\(tail)\",\"error\":\"ssh timeout\"}" }

let fourHosts = [upHost("1"), upHost("2"), badHost("3"), badHost("4")]

// -- the state word, from the state read alone --------------------------------
check("autodown up -> serving",
      ClusterHealthProjection.servingState(from: autodown("up")) == "serving")
check("autodown waking -> waking",
      ClusterHealthProjection.servingState(from: autodown("waking")) == "waking")
check("autodown down -> down (the server's word, not our guess)",
      ClusterHealthProjection.servingState(from: autodown("down")) == "down")
check("a state the app does not model -> unknown",
      ClusterHealthProjection.servingState(from: autodown("sideways")) == "unknown")
check("an answer with no state key -> unknown",
      ClusterHealthProjection.servingState(from: autodown(nil)) == "unknown")
check("a state read that answered NOTHING -> unreachable",
      ClusterHealthProjection.servingState(from: nil) == "unreachable")
check("only a failed read can print unreachable — a live one never does",
      ClusterHealthProjection.servingState(from: autodown("up")) != "unreachable")

// -- the tally, shared with the cluster widget -------------------------------
let noResponse = ClusterNodeTally.project(nil)
check("no response at all -> no counts, not zeros",
      noResponse.nodesUp == nil && noResponse.nodesDown == nil
      && noResponse.nodesTotal == nil && noResponse.downLabels.isEmpty)
let degraded = ClusterNodeTally.project(monitor(nil))
check("a route that degraded to {speak} -> no counts",
      degraded.nodesUp == nil && degraded.nodesTotal == nil)
check("an empty host list -> no counts (ClusterFleet's rule, unchanged)",
      ClusterNodeTally.project(monitor([])) == .noAnswer)
let half = ClusterNodeTally.project(monitor(fourHosts))
check("4 listed / 2 errored -> 2 up", half.nodesUp == 2)
check("... and 2 down, counted rather than assumed", half.nodesDown == 2)
check("... against the 4 the route listed", half.nodesTotal == 4)
check("the two that failed are named, in the order listed", half.downLabels == [".3", ".4"])
check("a down-node label is never an address (§7)",
      !half.downLabels.contains { $0.contains("192.0.2") })
let allUp = ClusterNodeTally.project(monitor([upHost("1"), upHost("2")]))
check("a stated 0 down is a fact, not a gap",
      allUp.nodesDown == 0 && allUp.downLabels.isEmpty)

// -- the signals one push carries --------------------------------------------
let live = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                           monitor: monitor(fourHosts), monitorLabel: nil,
                                           showNodeTally: true)
check("serving fleet, a node down: state, tally and the node named",
      live.servingState == "serving" && live.nodesUp == 2
      && live.nodesTotal == 4 && live.downNodes == [".3", ".4"] && live.failureLabel == nil)
check("and the colour that earns — amber, because serving is not healthy",
      ClusterHealthReading.tone(live) == .warn)

let stateBlind = ClusterHealthProjection.signals(autodown: nil, autodownLabel: "No connection",
                                                 monitor: monitor([upHost("1")]), monitorLabel: nil,
                                                 showNodeTally: true)
check("a failed state read gives the headline and its own label",
      stateBlind.servingState == "unreachable" && stateBlind.failureLabel == "No connection")
check("while the read that DID answer keeps its numbers",
      stateBlind.nodesUp == 1 && stateBlind.nodesTotal == 1)

let nodeBlind = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                monitor: nil, monitorLabel: "Timed out",
                                                showNodeTally: true)
check("a failed node read nils the counts — it never zeroes them (§6 rule 1)",
      nodeBlind.nodesUp == nil && nodeBlind.nodesDown == nil && nodeBlind.nodesTotal == nil)
check("and names which read went blind", nodeBlind.failureLabel == "Timed out")
check("the headline is the gap, never 0/0 up",
      ClusterHealthReading.headline(nodeBlind) == "Timed out"
      && ClusterHealthReading.headline(nodeBlind) != "0/0 up")

let bothBlind = ClusterHealthProjection.signals(autodown: nil, autodownLabel: "No connection",
                                                monitor: nil, monitorLabel: "Timed out",
                                                showNodeTally: true)
check("both reads blind: the state read's label wins (it is the headline)",
      bothBlind.failureLabel == "No connection")
check("a blind push is NOT an empty bubble — the sweep must keep it",
      !bothBlind.isEmpty
      && !ClusterHealthOrphanRule.isOrphan(bothBlind, updatedAt: Date(), now: Date()))

let tallyOff = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                               monitor: nil, monitorLabel: "Timed out",
                                               showNodeTally: false)
check("tally switched off: no counts, and not zeros",
      tallyOff.nodesUp == nil && tallyOff.downNodes.isEmpty)
check("tally off: a read this surface is not drawing cannot set its headline",
      tallyOff.failureLabel == nil)
check("tally off still renders the state it was told",
      ClusterHealthReading.headline(tallyOff) == "Serving")
check("and is never the empty bubble the launch sweep ends",
      !ClusterHealthOrphanRule.isOrphan(tallyOff, updatedAt: Date(), now: Date()))

check("a read that did not answer is — up; a stated zero is 0/4 up",
      ClusterHealthReading.tallyText(up: nil, total: nil) == "— up"
      && ClusterHealthReading.tallyText(up: 0, total: 4) == "0/4 up")
// ===========================================================================
// PART 4b — the LOAD signals the push carries (t_b009f32d).
//
// The per-host rows, the fleet's total draw, the peak GPU and the serving
// run/wait totals are decided by the SAME sliced `ClusterHealthProjection` as
// the tally above, and drawn by the Lock Screen and all three Island regions.
// Two things make this section worth its rows:
//
//   1. It runs against the COMMITTED `/v1/cluster/monitor` fixture — the same
//      bytes `model_decode_check` decodes and the same bytes
//      `cluster_monitor_cli_parity.py` generated the CLI's goldens from. So the
//      cell strings asserted below are the CLI's own cells, checked as literals
//      rather than restated from the code under test, and a fixture the file
//      cannot be read for fails the run BY NAME instead of skipping quietly.
//   2. Every case that can lie is asserted on both sides: what a told zero
//      draws, and what a not-told draws.
//
// Host strings here are 192.0.2.N (RFC 5737 TEST-NET-1) like the fixture, so no
// real address can reach this file either.
// ===========================================================================

print("\nCluster health — the per-host load signals (sliced, run against the committed fixture)\n")

/// The committed live-fleet payload. Read relative to `ios-app`, which is the
/// directory the .sh has already cd'd into.
func loadFixtureData(_ name: String) -> Data? {
    try? Data(contentsOf: URL(fileURLWithPath: "scripts/model_decode_check/fixtures/" + name))
}
func checkLevel(_ name: String, _ got: ClusterMetricLevel?, _ want: ClusterMetricLevel?) {
    check(name, got == want, "got \(String(describing: got)), want \(String(describing: want))")
}
func checkGlance(_ name: String, _ got: ClusterHealthGlance,
                 _ text: String, _ kind: ClusterHealthGlance.Kind,
                 _ level: ClusterMetricLevel? = nil, _ secondary: String? = nil) {
    check(name, got.text == text && got.kind == kind && got.level == level && got.secondary == secondary,
          "got (text: \(got.text), kind: \(got.kind.rawValue), level: \(String(describing: got.level)), secondary: \(String(describing: got.secondary))), want (\(text), \(kind.rawValue), \(String(describing: level)), \(String(describing: secondary)))")
}

let liveFixtureData = loadFixtureData("v1_cluster_monitor_live.json")
check("the committed cluster-monitor fixture is readable (a skipped fixture is not a passing one)",
      liveFixtureData != nil)

/// A host with a sample, built the way the route sends it: every sparkrun metric
/// is a STRING, so an empty one is the node's own "could not read", not a zero.
func sampleHost(_ tail: String, util: String? = nil, gpuTemp: String? = nil,
                power: String? = nil, limit: String? = nil, error: String? = nil) -> String {
    var fields: [String] = []
    if let util { fields.append("\"gpu_util_pct\":\"\(util)\"") }
    if let gpuTemp { fields.append("\"gpu_temp_c\":\"\(gpuTemp)\"") }
    if let power { fields.append("\"gpu_power_w\":\"\(power)\"") }
    if let limit { fields.append("\"gpu_power_limit_w\":\"\(limit)\"") }
    let sample = fields.isEmpty ? "null" : "{\(fields.joined(separator: ","))}"
    let err = error.map { "\"error\":\"\($0)\"" } ?? "\"error\":null"
    return "{\"host\":\"192.0.2.\(tail)\",\(err),\"sample\":\(sample)}"
}
func monitorHosts(_ hosts: [String], serving: [String]? = nil) -> MonitorResponse? {
    var body = "{\"speak\":\"ok\",\"json\":{\"hosts\":[\(hosts.joined(separator: ","))]"
    if let serving { body += ",\"serving\":[\(serving.joined(separator: ","))]" }
    body += "}}"
    return try? JSONDecoder().decode(MonitorResponse.self, from: Data(body.utf8))
}
func servingRow(node: String, model: String, running: Int?, waiting: Int?) -> String {
    var fields = ["\"node\":\"\(node)\"", "\"model\":\"\(model)\""]
    if let running { fields.append("\"running\":\(running)") }
    if let waiting { fields.append("\"waiting\":\(waiting)") }
    return "{\(fields.joined(separator: ","))}"
}

// -- the committed fixture: the CLI's own cells, on the activity's own surface --
if let liveBytes = liveFixtureData,
   let liveMonitor = try? JSONDecoder().decode(MonitorResponse.self, from: liveBytes) {
    let fromFixture = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                     monitor: liveMonitor, monitorLabel: nil,
                                                     showNodeTally: true)
    let rows = ClusterHealthReading.hostLineList(fromFixture.hostLines, limit: 4)
    // The fixture's hosts carry `hostname` INSIDE the sample, so the view-model's
    // `displayLabel` — which the CLI's `host_label` also prefers — is the reported
    // name, not the `.NNN` tail of the address.
    checkList("one line per host the route listed, in the route's order",
              rows.map { $0.label }, ["node-1", "node-2", "node-3", "node-4"])
    check("a host line is never an address (§7)", !rows.contains { $0.label.contains("192.0.2") })
    // These literals are `v1_cluster_monitor_live.cli.json`'s `GPU%`, `GPU Temp`
    // and `GPU Power` cells for the same bytes — the CLI's own output, generated
    // by cluster_monitor_cli_parity.py, not transcribed from this view-model.
    checkText("the first line's GPU% cell is the CLI's", rows.first?.gpuPct ?? "", "93")
    checkText("the first line's GPU Temp cell is the CLI's (a space and a C, never a degree)",
              rows.first?.gpuTemp ?? "", "49 C")
    checkText("the first line's GPU Power cell is the CLI's (two decimals, W)",
              rows.first?.gpuPower ?? "", "9.49 W")
    let viewRows = ClusterMonitorViewModel.project(liveMonitor)?.hostRows ?? []
    check("every cell IS ClusterMonitorViewModel's, not a re-formatting of the number",
          rows.count == viewRows.count
          && zip(rows, viewRows).allSatisfy { line, row in
              line.gpuPct == row.gpuPct && line.gpuTemp == row.gpuTemp
              && line.gpuPower == row.gpuPower && line.label == row.displayLabel
          })
    check("none of the four hosts answered badly", !rows.contains { $0.isDown })
    checkText("total draw is the sum of the four hosts that reported one, in the CLI's power format",
              ClusterHealthReading.totalPowerText(fromFixture.totalPowerW) ?? "nil", "79.63 W")
    checkLevel("watts with NO limit read is .unknown, never .ok", fromFixture.powerLevel, .unknown)
    check("and specifically not green", fromFixture.powerLevel != .ok)
    check("the compact's load number is the BUSIEST host (94), not the first (93)",
          fromFixture.peakGpuPct == 94)
    checkGlance("so the compact leads with 94%", ClusterHealthReading.glance(fromFixture),
                "94%", .load, nil, "4/4")
    check("the hottest GPU is the hottest host's (62), and its verdict is its own",
          fromFixture.peakGpuTempC == 62 && fromFixture.peakGpuTempLevel == .ok)
    checkText("serving totals are the CLI total row's run/wait",
              ClusterHealthReading.servingLoadText(running: fromFixture.servingRunning,
                                                   waiting: fromFixture.servingWaiting) ?? "nil",
              "6 run · 0 wait")
    check("a queue with work in it is the CLI's warn; an idle busy engine is ok",
          CLIMonitorFormat.loadLevel(running: fromFixture.servingRunning ?? 0,
                                     waiting: fromFixture.servingWaiting ?? 0) == .ok)
    checkOpt("4 of 4 drawn -> no +n",
             ClusterHealthReading.hostsMoreText(shown: 4, listed: fromFixture.nodesTotal), nil)
}

// -- what silence looks like: every case that must NOT draw a zero -------------
let speakOnly = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                monitor: monitor(nil), monitorLabel: nil,
                                                showNodeTally: true)
check("a route that degraded to {speak} carries no host lines (nil, not an empty table)",
      speakOnly.hostLines == nil)
check("... and no total draw, no verdict, no peaks",
      speakOnly.totalPowerW == nil && speakOnly.powerLevel == nil
      && speakOnly.peakGpuPct == nil && speakOnly.peakGpuTempC == nil)
checkOpt("... and no total line at all",
         ClusterHealthReading.totalPowerText(speakOnly.totalPowerW), nil)
checkGlance("the compact falls back to the gap mark, not to 0%",
            ClusterHealthReading.glance(speakOnly), "—", .mark)
let emptyHosts = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                 monitor: monitorHosts([]), monitorLabel: nil,
                                                 showNodeTally: true)
check("an empty host list is the powered-off fleet's silence, exactly as ClusterNodeTally reads it",
      emptyHosts.hostLines == nil && emptyHosts.totalPowerW == nil)

let idleFleet = ClusterHealthProjection.signals(
    autodown: autodown("up"), autodownLabel: nil,
    monitor: monitorHosts([sampleHost("1", util: "0", power: "0.005", limit: "200"),
                           sampleHost("2", util: "0", power: "0.005", limit: "200")]),
    monitorLabel: nil, showNodeTally: true)
checkText("an idle fleet that DID report draws its real zero, not an em dash",
          ClusterHealthReading.totalPowerText(idleFleet.totalPowerW) ?? "nil", "0.01 W")
checkLevel("... and the verdict belongs to the wattage against its own limit",
           idleFleet.powerLevel, .ok)

let oneDown = ClusterHealthProjection.signals(
    autodown: autodown("up"), autodownLabel: nil,
    monitor: monitorHosts([sampleHost("1", util: "40", power: "10.00", limit: "200"),
                           sampleHost("2", error: "ssh: connection refused")]),
    monitorLabel: nil, showNodeTally: true)
let downRows = ClusterHealthReading.hostLineList(oneDown.hostLines, limit: 4)
check("the host that never sampled is named as down, not dropped",
      downRows.count == 2 && downRows[1].isDown && !downRows[0].isDown)
checkText("and its cells are the CLI's dashes", downRows.count > 1 ? downRows[1].gpuPower : "?", "-")
checkLevel("a dash carries no verdict — .unknown, never .ok",
           downRows.count > 1 ? downRows[1].gpuPowerLevel : .ok, .unknown)
checkText("the total is the sum of the hosts that ANSWERED: 10.00 W, never 10.00 + nothing",
          ClusterHealthReading.totalPowerText(oneDown.totalPowerW) ?? "nil", "10.00 W")

// -- thresholds: the number and its colour must come from the same host --------
let hotFleet = ClusterHealthProjection.signals(
    autodown: autodown("up"), autodownLabel: nil,
    monitor: monitorHosts([sampleHost("1", gpuTemp: "50"), sampleHost("2", gpuTemp: "95")]),
    monitorLabel: nil, showNodeTally: true)
check("the hottest GPU's verdict is the verdict of the host that IS the hottest",
      hotFleet.peakGpuTempC == 95 && hotFleet.peakGpuTempLevel == .critical)
checkGlance("and with no utilisation told, the compact draws that temperature in its own "
            + "colour, keeping the tally it was told",
            ClusterHealthReading.glance(hotFleet), "95 C", .heat, .critical, "2/2")
let hotNoTally = ClusterHealthProjection.signals(
    autodown: autodown("up"), autodownLabel: nil,
    monitor: nil, monitorLabel: nil, showNodeTally: true)
checkGlance("with no tally either, a heat glance carries no secondary",
            ClusterHealthReading.glance(
                ClusterHealthSignals(servingState: "serving", nodesUp: nil, nodesDown: nil,
                                     nodesTotal: nil, failureLabel: nil, downNodes: [],
                                     peakGpuTempC: 95, peakGpuTempLevel: .critical)),
            "95 C", .heat, .critical, nil)
_ = hotNoTally
let warnPower = ClusterHealthProjection.signals(
    autodown: autodown("up"), autodownLabel: nil,
    monitor: monitorHosts([sampleHost("1", power: "80", limit: "100"),    // 80% -> warn
                           sampleHost("2", power: "50", limit: "200")]),  // 25% -> ok
    monitorLabel: nil, showNodeTally: true)
checkLevel("the fleet's power colour is the WORST per-host verdict, not the average",
           warnPower.powerLevel, .warn)
checkLevel("and a critical anywhere is critical everywhere it is drawn",
           ClusterHealthProjection.powerLevel(from: ClusterMonitorViewModel.project(
            monitorHosts([sampleHost("1", power: "80", limit: "100"),
                          sampleHost("2", power: "106", limit: "110")]))!.hostRows), .critical)

// -- the clip: the drawn list must never move the headline numbers -------------
let sixHosts = (1...5).map { sampleHost("\($0)", util: "40", gpuTemp: "50", power: "10.00", limit: "200") }
       + [sampleHost("6", util: "99", gpuTemp: "88", power: "180.00", limit: "200")]
let sixSignals = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                 monitor: monitorHosts(sixHosts), monitorLabel: nil,
                                                 showNodeTally: true)
check("a push carries the Lock Screen's budget and no more",
      ClusterHealthReading.hostLineList(sixSignals.hostLines,
                                        limit: ClusterHealthProjection.hostLineLimit).count == 4)
checkOpt("and counts what it left out against the route's own host count",
         ClusterHealthReading.hostsMoreText(shown: 4, listed: sixSignals.nodesTotal), "+2 more")
check("the busiest GPU is still the busiest even though the clip hid it",
      sixSignals.peakGpuPct == 99
      && ClusterHealthReading.glance(sixSignals).text == "99%")
check("the hottest GPU too, and it carries the warn band the view-model gives 88 C",
      sixSignals.peakGpuTempC == 88 && sixSignals.peakGpuTempLevel == .warn)
checkText("the total is over all six hosts (50.00 + 180.00), not over the four drawn",
          ClusterHealthReading.totalPowerText(sixSignals.totalPowerW) ?? "nil", "230.00 W")
check("the expanded Island clips tighter and still says what it hid",
      ClusterHealthReading.hostLineList(sixSignals.hostLines,
                                        limit: ClusterHealthProjection.islandHostLineLimit).count == 3
      && ClusterHealthReading.hostsMoreText(shown: 3,
                                            listed: sixSignals.nodesTotal) == "+3 more")

// -- serving totals: absence, zero and a switched-off section ------------------
let noServing = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                monitor: monitorHosts([sampleHost("1", util: "5")]),
                                                monitorLabel: nil, showNodeTally: true)
check("no serving section -> no run/wait, not 0/0",
      noServing.servingRunning == nil && noServing.servingWaiting == nil)
checkOpt("and no line",
         ClusterHealthReading.servingLoadText(running: nil, waiting: nil), nil)
checkText("an EMPTY serving section is the route telling us there are no backends: a real zero",
          ClusterHealthReading.servingLoadText(running: 0, waiting: 0) ?? "nil", "0 run · 0 wait")
checkText("a queue that is told without a running count draws only what was told",
          ClusterHealthReading.servingLoadText(running: nil, waiting: 3) ?? "nil", "3 wait")
let tallyOffLoad = ClusterHealthProjection.signals(
    autodown: autodown("up"), autodownLabel: nil,
    monitor: monitorHosts([sampleHost("1", util: "40", power: "10.00", limit: "200")],
                          serving: [servingRow(node: "192.0.2.1", model: "M", running: 2, waiting: 1)]),
    monitorLabel: "Timed out", showNodeTally: false)
check("tally switched off: the host data goes nil with the counts, it is not zeroed",
      tallyOffLoad.hostLines == nil && tallyOffLoad.totalPowerW == nil
      && tallyOffLoad.peakGpuPct == nil && tallyOffLoad.peakGpuTempC == nil)
check("and the monitor's failure label goes with it",
      tallyOffLoad.failureLabel == nil)

// -- the empty / orphan boundary, moved by the new signals --------------------
let onlyRows = ClusterHealthSignals(servingState: "unknown", nodesUp: nil, nodesDown: nil,
                                    nodesTotal: nil, failureLabel: nil, downNodes: [],
                                    hostLines: [ClusterHealthHostLine(label: ".1", gpuPct: "40",
                                                                       gpuTemp: "50 C",
                                                                       gpuPower: "10.00 W",
                                                                       gpuTempLevel: .ok,
                                                                       gpuPowerLevel: .ok,
                                                                       isDown: false)])
check("a bubble carrying ONLY a host line is not the empty one the launch sweep ends",
      !onlyRows.isEmpty
      && !ClusterHealthOrphanRule.isOrphan(onlyRows, updatedAt: Date(), now: Date()))
check("and it still has no tally to draw, so the compact falls back to the gap mark",
      ClusterHealthReading.glance(onlyRows).kind == .mark)
check("the stateless, numberless, rowless bubble is still the one the sweep ends",
      ClusterHealthOrphanRule.isOrphan(ClusterHealthSignals(servingState: "unknown", nodesUp: nil,
                                                            nodesDown: nil, nodesTotal: nil,
                                                            failureLabel: nil, downNodes: []),
                                       updatedAt: Date(), now: Date()))

// -- the payload that crosses the process boundary ------------------------------
// The extension is a separate binary that decodes JSON the app encoded. `hostLines`
// and the peaks are declared `var … ? = nil` on purpose: a `let` with an initial
// value is dropped from the synthesized CodingKeys (the compiler warns and silently
// never decodes it) and a NON-optional with a default makes the key REQUIRED, so an
// activity pushed by an older app version would fail to decode at all. Both
// directions are asserted rather than argued. `ContentState` is these signals plus
// the push date, so the signals are where the keys live.
let roundTrip = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                monitor: liveFixtureData.flatMap {
                                                    try? JSONDecoder().decode(MonitorResponse.self, from: $0)
                                                },
                                                monitorLabel: nil, showNodeTally: true)
let encoded = try? JSONEncoder().encode(roundTrip)
let decoded = encoded.flatMap { try? JSONDecoder().decode(ClusterHealthSignals.self, from: $0) }
check("a pushed payload with the load signals survives encode -> decode intact",
      decoded == roundTrip)
check("and the host lines and the peaks are still there after the trip",
      decoded?.hostLines?.count == 4 && decoded?.peakGpuPct == 94)
let preLoadPayload = ("{\"servingState\":\"serving\",\"nodesUp\":3,\"nodesDown\":0,"
    + "\"nodesTotal\":3,\"downNodes\":[]}").data(using: .utf8)!
let oldState = try? JSONDecoder().decode(ClusterHealthSignals.self, from: preLoadPayload)
check("an OLD payload without any of the new keys still decodes (the app and the "
      + "extension are separate binaries; a required new key would drop the bubble)",
      oldState != nil && oldState?.hostLines == nil && oldState?.peakGpuPct == nil)
check("and the keys the new payload DOES carry are the ones declared (no silent drop)",
      String(data: try! JSONEncoder().encode(ClusterHealthSignals(
        servingState: "serving", nodesUp: nil, nodesDown: nil, nodesTotal: nil,
        failureLabel: nil, downNodes: [], hostLines: [], totalPowerW: 1,
        powerLevel: .warn, peakGpuPct: 2, peakGpuTempC: 3, peakGpuTempLevel: .ok,
        servingRunning: 4, servingWaiting: 5)), encoding: .utf8)!
      .contains("\"peakGpuTempLevel\":\"ok\""))

// -- the clipping and ranking primitives, at their edges ----------------------
check("hostLineList: nothing told draws nothing", ClusterHealthReading.hostLineList(nil, limit: 4).isEmpty)
check("hostLineList: a zero or negative limit cannot crash the view",
      ClusterHealthReading.hostLineList(roundTrip.hostLines, limit: 0).isEmpty
      && ClusterHealthReading.hostLineList(roundTrip.hostLines, limit: -3).isEmpty)
checkLevel("worstLevel of nothing is no verdict", ClusterHealthReading.worstLevel([]), nil)
checkLevel("an unknown verdict loses to a warn", ClusterHealthReading.worstLevel([.unknown, .warn]), .warn)
checkLevel("a critical beats a warn", ClusterHealthReading.worstLevel([.warn, .critical]), .critical)
checkLevel("and a lone unknown stays unknown — it is not evidence of health",
           ClusterHealthReading.worstLevel([.unknown]), .unknown)
checkText("the total power renders 0 as a fact, in the CLI's two-decimal format",
          ClusterHealthReading.totalPowerText(0) ?? "nil", "0.00 W")

// ===========================================================================
// PART 5 — the running-cards FEED rule (t_71deac83), sliced, plus the
// projection the driver pushes FROM (compiled from the same source file).
//
// The driver is ActivityKit code, so no macOS harness can run it. What it
// *decides* is in `CardsRunningFeedRule` (sliced out of
// Sources/Shared/CardsRunningActivity.swift by the script that runs this
// program), and what it *pushes* is built by `CardsRunningProjection`
// (Sources/Shared/CardsRunningProjection.swift, compiled here as-is). Those two
// are the whole claim of the surface: a refresh that did not answer can neither
// start a bubble nor print a zero, and a refresh that found nothing running
// takes the bubble down. Everything below asserts exactly that, over the code
// the app and the widget actually build.
// ===========================================================================

print("\nRunning cards — the feed rule (sliced) and the projection it pushes from\n")

let feedLive: String? = nil
let feedBlind = "No connection"

// A live read that named cards always pushes: up or not up, that is a surface
// with something true to say.
check("live + rows + up -> push",
      CardsRunningFeedRule.decide(activityIsUp: true, rowCount: 2, failureLabel: feedLive) == .push)
check("live + rows + not up -> push (starts the activity)",
      CardsRunningFeedRule.decide(activityIsUp: false, rowCount: 1, failureLabel: feedLive) == .push)

// A live read that counted nothing is a REAL zero, and a Live Activity about
// work with no work is noise: END it, do not leave an empty bubble up.
check("live + zero rows + up -> end",
      CardsRunningFeedRule.decide(activityIsUp: true, rowCount: 0, failureLabel: feedLive) == .end)
check("live + zero rows + not up -> idle",
      CardsRunningFeedRule.decide(activityIsUp: false, rowCount: 0, failureLabel: feedLive) == .idle)

// A stale snapshot may HOLD a bubble (with its label), but it must never START
// one: a bubble that was never live has no age the operator can see.
check("stale + rows + up -> push",
      CardsRunningFeedRule.decide(activityIsUp: true, rowCount: 2, failureLabel: feedBlind) == .push)
check("stale + rows + not up -> idle (a blind read never starts a bubble)",
      CardsRunningFeedRule.decide(activityIsUp: false, rowCount: 2, failureLabel: feedBlind) == .idle)
check("blind + no rows + up -> end",
      CardsRunningFeedRule.decide(activityIsUp: true, rowCount: 0, failureLabel: feedBlind) == .end)
check("blind + no rows + not up -> idle",
      CardsRunningFeedRule.decide(activityIsUp: false, rowCount: 0, failureLabel: feedBlind) == .idle)

// The driver and the launch sweep must reach the SAME conclusion about an empty
// bubble, or the app pushes what the next launch immediately kills.
check("driver and sweep agree: zero rows is gone either way",
      CardsRunningFeedRule.decide(activityIsUp: true, rowCount: 0, failureLabel: feedBlind) == .end
        && CardsRunningOrphanRule.isOrphan(rowCount: 0, updatedAt: now, now: now))
check("driver and sweep agree: rows + a fresh push is alive",
      CardsRunningFeedRule.decide(activityIsUp: true, rowCount: 1, failureLabel: feedLive) == .push
        && !CardsRunningOrphanRule.isOrphan(rowCount: 1, updatedAt: now, now: now))

// The label: nil only for a live read, and never empty otherwise — that label
// is the only thing standing between a blind refresh and a quiet, current
// looking board.
check("a live push carries no label", CardsRunningFeedRule.label(reason: nil, live: true) == nil)
check("a non-live push keeps its reason", CardsRunningFeedRule.label(reason: feedBlind, live: false) == feedBlind)
check("a non-live push with no reason still says something",
      CardsRunningFeedRule.label(reason: nil, live: false) == "No answer")
check("an empty reason still says something",
      CardsRunningFeedRule.label(reason: "", live: false) == "No answer")
// Every failure this surface can actually hit has a label short enough for the
// Island region and long enough to read — the shortLabel list is the vocabulary
// `failureLabel` is built from, so it is checked rather than assumed.
let feedFailures: [WidgetDataFailure] = [
    .notConfigured, .invalidURL, .pathNotAllowed(path: "/v1/nope"), .timeout(seconds: 8),
    .unreachable(code: nil), .unauthorized, .notFound(path: "/v1/nope"),
    .http(status: 500, code: nil), .decoding(detail: "x"),
]
for failure in feedFailures {
    check("shortLabel \"\(failure.shortLabel)\" is printable in the Island",
          !failure.shortLabel.isEmpty && failure.shortLabel.count <= 18,
          "count \(failure.shortLabel.count)")
    check("label(\(failure.shortLabel)) survives the feed rule",
          CardsRunningFeedRule.label(reason: failure.shortLabel, live: false) == failure.shortLabel)
}

// ---- the projection the push is built from (compiled, not transcribed) -----
// `CardsRunningProjection` is the type both the widget provider and the driver
// fill, and it is where "not told" stays nil instead of becoming 0. The
// `tasks == nil` case is NOT reachable through it — `CardsRunningFeed.read`
// turns that payload into a `decoding` failure before a projection exists — so
// the projection's job is the honest zero and the honest subset.
func feedStamp(_ minutesAgo: Int) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.string(from: now.addingTimeInterval(-Double(minutesAgo) * 60))
}
func runningCard(_ id: String, board: String, startedMinutesAgo: Int?) -> RunningCardLite {
    RunningCardLite(board: board, id: id, title: "Card \(id)", assignee: "ios-engineer",
                    status: "running",
                    started_at: startedMinutesAgo.map(feedStamp))
}
func running(tasks: [RunningCardLite]?, count: Int?) -> KanbanRunningLite {
    KanbanRunningLite(tasks: tasks, count: count, speak: nil, boards: nil)
}
func blocked(tasks: [BlockedCardLite]?, count: Int?) -> KanbanBlockedLite {
    KanbanBlockedLite(tasks: tasks, count: count, speak: nil)
}
func blockedCard(_ id: String) -> BlockedCardLite {
    BlockedCardLite(board: "hscc", id: id, status: "blocked", assignee: "reviewer",
                    age_days: 2, block_kind: "needs_input", title: "Stuck \(id)")
}

// The real zero: the route answered `tasks: []`, so the fleet IS idle, and a
// zero here is a fact the server sent — the one case a zero is allowed.
let idleProjection = CardsRunningProjection(running: running(tasks: [], count: nil),
                                            blocked: nil, now: now)
check("an answered empty list is a real zero (runningCount 0, not nil)",
      idleProjection.runningCount == 0 && idleProjection.rows.isEmpty)
check("the empty list is the whole set", idleProjection.rowsAreComplete)

// A count the server sent wins over counting the rows, and a short list is
// marked a subset so no family claims it is everything.
let subset = CardsRunningProjection(running: running(tasks: [runningCard("t_a", board: "hscc", startedMinutesAgo: 5)],
                                                     count: 5),
                                    blocked: nil, now: now)
check("the server's count beats the row count", subset.runningCount == 5)
check("a short list is a subset, not the whole fleet", !subset.rowsAreComplete)
let matched = CardsRunningProjection(running: running(tasks: [runningCard("t_a", board: "hscc", startedMinutesAgo: 5)],
                                                      count: 1),
                                     blocked: nil, now: now)
check("rows that match the count are the whole set", matched.rowsAreComplete)

// The blocked route is a SEPARATE answer: down means `—`, never 0, and never a
// claim that the board has nothing stuck.
let blockedDown = CardsRunningProjection(running: running(tasks: [], count: 0), blocked: nil, now: now)
check("a blocked read that did not answer yields nil, never 0", blockedDown.blockedCount == nil)
let blockedCounted = CardsRunningProjection(
    running: running(tasks: [], count: 0),
    blocked: blocked(tasks: [blockedCard("t_b")], count: 3), now: now)
check("the blocked route's own count wins", blockedCounted.blockedCount == 3)
let blockedUncounted = CardsRunningProjection(
    running: running(tasks: [], count: 0),
    blocked: blocked(tasks: [blockedCard("t_b"), blockedCard("t_c")], count: nil), now: now)
check("a blocked payload with no count falls back to its rows", blockedUncounted.blockedCount == 2)

// Rows are ordered by run time with the unknown age LAST (an unknown age is not
// "the oldest"), and capped — the blob the App Group holds must not be the
// whole card list (§7).
let ordered = CardsRunningProjection(
    running: running(tasks: [runningCard("t_new", board: "hscc", startedMinutesAgo: 1),
                             runningCard("t_old", board: "radio", startedMinutesAgo: 90),
                             runningCard("t_none", board: "hscc", startedMinutesAgo: nil)],
                     count: 3),
    blocked: nil, now: now)
check("longest-running first", ordered.rows.first?.cardID == "t_old")
check("an unknown age sorts LAST, not first", ordered.rows.last?.cardID == "t_none")
check("a row with no started_at has no age (draws —, never 0m)",
      ordered.rows.last?.ageMinutes == nil)
check("the stored row cap is the large family's row limit", CardsRunningProjection.rowCap == 8)
let many = (1...12).map { runningCard("t_\($0)", board: "hscc", startedMinutesAgo: $0) }
check("a 12-card fleet stores 8 rows, not the whole board",
      CardsRunningProjection(running: running(tasks: many, count: 12), blocked: nil, now: now).rows.count == 8)

// An unparseable start is "no age", which draws `—`; a stamp ahead of this
// clock is "just started", never a negative.
check("an unparseable started_at yields no age",
      CardsRunningRow.age(from: "not-a-date", now: now) == nil)
check("a missing started_at yields no age", CardsRunningRow.age(from: nil, now: now) == nil)
check("a stamp ahead of this clock reads 0 minutes, never negative",
      CardsRunningRow.age(from: feedStamp(-30), now: now) == 0)
check("30 minutes ago reads 30", CardsRunningRow.age(from: feedStamp(30), now: now) == 30)

print()
if failures == 0 {
    print("ALL PASS — wake decider (transcribed) + running-cards and cluster-health orphan rules and reading rules (sliced from source) + the cluster-health projection and the running-cards feed rule (sliced) and the projection the cards push is built from (compiled from source) cover every documented case")
} else {
    print("\(failures) FAILURE(S)")
}
exit(failures == 0 ? 0 : 1)
