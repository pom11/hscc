import Foundation

// ===========================================================================
// notify_check harness — headless proof of the notify-operator decision engine.
//
// Card t_0454eb56: "Phase 1 decision engine + foreground local notifications,
// headlessly proven". This harness compiles the REAL engine sources (never
// redeclared here) plus a slice of the REAL AppGroup enum out of SharedModels
// into a plain macOS CLI, then drives `NeedsOperatorNotifier.compute/nextState`
// through every differential rule in the plan:
//
//   * FIRST observation anchors silently (no wall of notifications on install);
//   * needs-review fires only on a NEW queue id; unchanged stays silent; a
//     card that clears and re-enters re-announces;
//   * card-failed/blocked fires on a new blocked id or escalations 0→n; an
//     unchanged count stays silent; a cleared-then-rose count re-announces;
//   * fleet-unreachable fires on a confident reachable→unreachable transition;
//     stays-down does NOT re-fire; a nil (inconclusive) poll neither fires nor
//     clears; recovery clears so a later down re-announces;
//   * LastSeenState + its store round-trip through App-Group JSON.
//
// The engine is pure Foundation, so a macOS CLI is a faithful runner (same
// pattern as chat_state_check.sh). No iOS runtime claim is made here.
// ===========================================================================

// ---- tiny test harness ---------------------------------------------------
var failures = 0
func check(_ cond: Bool, _ label: String,
           _ file: String = #file, _ line: Int = #line) {
    if cond {
        print("  PASS \(label)")
    } else {
        failures += 1
        print("  FAIL \(label) (\(file):\(line))")
    }
}

func expect(_ got: Int, _ want: Int, _ label: String) {
    check(got == want, "\(label) — expected \(want) alerts, got \(got)")
}

// ---- helper factories ----------------------------------------------------
func prior(observedOnce: Bool = true,
           review: Set<String> = [],
           blocked: Set<String> = [],
           escalations: Int = 0,
           unreachable: Bool = false) -> LastSeenState {
    var s = LastSeenState()
    s.observedOnce = observedOnce
    s.announced.review = review
    s.announced.blocked = blocked
    s.announced.escalations = escalations
    s.announced.unreachable = unreachable
    return s
}

func now(review: Set<String> = [],
         blocked: Set<String> = [],
         escalations: Int = 0,
         apiReachable: Bool? = true,
         daemonRunning: Bool? = nil) -> ObservedState {
    var s = ObservedState()
    s.reviewQueue = review
    s.blocked = blocked
    s.escalationsCount = escalations
    s.apiReachable = apiReachable
    s.daemonRunning = daemonRunning
    return s
}

func kinds(_ alerts: [OperatorAlert]) -> Set<OperatorAlert.Kind> {
    Set(alerts.map { $0.kind })
}

func targets(_ alerts: [OperatorAlert], _ kind: OperatorAlert.Kind) -> Set<String> {
    Set(alerts.filter { $0.kind == kind }.flatMap { $0.targetIDs })
}

print("== first observation anchors silently ==")
do {
    let prior = LastSeenState.empty            // never observed
    let state = now(review: ["A", "B", "C"], blocked: ["X"], escalations: 2, apiReachable: false)
    let alerts = NeedsOperatorNotifier.compute(prior: prior, now: state)
    expect(alerts.count, 0, "first observation fires nothing")
    let next = NeedsOperatorNotifier.nextState(prior: prior, now: state)
    check(next.observedOnce, "nextState.observedOnce becomes true after first poll")
    check(next.observed == state, "nextState records the observation")
}

print("== needs-review: new id fires once ==")
do {
    let p = prior(review: ["A"])
    let alerts = NeedsOperatorNotifier.compute(prior: p, now: now(review: ["A", "B"]))
    expect(alerts.count, 1, "queue gained B")
    check(targets(alerts, .needsReview) == ["B"], "alert targets the NEW id only")
}

print("== needs-review: unchanged queue silent ==")
do {
    let p = prior(review: ["A", "B"])
    expect(NeedsOperatorNotifier.compute(prior: p, now: now(review: ["A", "B"])).count,
           0, "no change → no alerts")
}

print("== needs-review: recurrence re-announces after clear ==")
do {
    // queue {A} → announced {A}. Then queue empties { } → announced { }.
    let cleared = NeedsOperatorNotifier.nextState(
        prior: prior(review: ["A"]),
        now: now(review: [])
    )
    check(cleared.announced.review.isEmpty, "clear empties announced.review")
    // {A} returns → new relative to empty announced → fires again.
    let alerts = NeedsOperatorNotifier.compute(
        prior: prior(review: []), now: now(review: ["A"]))
    expect(alerts.count, 1, "recurring A re-announces")
}

print("== card blocked: new id fires once ==")
do {
    let p = prior(blocked: ["X"])
    let alerts = NeedsOperatorNotifier.compute(prior: p, now: now(blocked: ["X", "Y"]))
    expect(alerts.count, 1, "blocked gained Y")
    check(targets(alerts, .cardFailedBlocked) == ["Y"], "targets only the NEW blocked id")
}

print("== card blocked: unchanged silent ==")
do {
    let p = prior(blocked: ["X"])
    expect(NeedsOperatorNotifier.compute(prior: p, now: now(blocked: ["X"])).count,
           0, "no new blocked id → silent")
}

print("== escalations: 0→n fires once ==")
do {
    let p = prior(escalations: 0)
    let alerts = NeedsOperatorNotifier.compute(prior: p, now: now(escalations: 2))
    expect(alerts.count, 1, "0→2 fires")
    check(kinds(alerts).contains(.cardFailedBlocked), "escalation alert is cardFailedBlocked class")
}

print("== escalations: unchanged count silent ==")
do {
    let p = prior(escalations: 2)
    expect(NeedsOperatorNotifier.compute(prior: p, now: now(escalations: 2)).count,
           0, "2→2 silent")
}

print("== escalations: cleared then rose re-announces ==")
do {
    // Count cleared to 0 → announced forgotten.
    let cleared = NeedsOperatorNotifier.nextState(
        prior: prior(escalations: 3), now: now(escalations: 0))
    check(cleared.announced.escalations == 0, "clear forgets escalations state")
    let alerts = NeedsOperatorNotifier.compute(
        prior: prior(escalations: 0), now: now(escalations: 2))
    expect(alerts.count, 1, "0→2 after clear fires again")
}

print("== fleet-unreachable: reachable→unreachable fires once ==")
do {
    let p = prior(unreachable: false)
    let alerts = NeedsOperatorNotifier.compute(prior: p, now: now(apiReachable: false))
    expect(alerts.count, 1, "down transition fires")
    check(kinds(alerts).contains(.fleetUnreachable), "alert is fleetUnreachable class")
    let next = NeedsOperatorNotifier.nextState(prior: p, now: now(apiReachable: false))
    check(next.announced.unreachable, "down episode recorded as announced")
}

print("== fleet-unreachable: stays down does NOT re-fire ==")
do {
    let p = prior(unreachable: true)
    expect(NeedsOperatorNotifier.compute(prior: p, now: now(apiReachable: false)).count,
           0, "persistent down does not spam")
}

print("== fleet-unreachable: nil (inconclusive) neither fires nor clears ==")
do {
    // Down was announced; now an inconclusive poll (nil) arrives.
    let p = prior(unreachable: true)
    expect(NeedsOperatorNotifier.compute(prior: p, now: now(apiReachable: nil)).count,
           0, "nil poll does not fire")
    let next = NeedsOperatorNotifier.nextState(prior: p, now: now(apiReachable: nil))
    check(next.announced.unreachable == true, "nil poll does not clear announced down")
}

print("== fleet-unreachable: recovery clears so later down re-fires ==")
do {
    let p = prior(unreachable: true)
    let next = NeedsOperatorNotifier.nextState(prior: p, now: now(apiReachable: true))
    check(next.announced.unreachable == false, "recovery clears the down flag")
    let later = NeedsOperatorNotifier.compute(
        prior: prior(unreachable: false), now: now(apiReachable: false))
    expect(later.count, 1, "a SECOND down after recovery fires again")
}

print("== fleet-unreachable: daemon stopped counts as down ==")
do {
    let p = prior(unreachable: false)
    let alerts = NeedsOperatorNotifier.compute(
        prior: p, now: now(apiReachable: true, daemonRunning: false))
    expect(alerts.count, 1, "daemon not running while API reachable fires fleet-down")
}

print("== priority: several new conditions fire together, deduped per class ==")
do {
    let p = prior(review: [], blocked: [], escalations: 0, unreachable: false)
    let alerts = NeedsOperatorNotifier.compute(
        prior: p,
        now: now(review: ["A", "B"], blocked: ["X"], escalations: 1, apiReachable: false))
    check(kinds(alerts) == [.needsReview, .cardFailedBlocked, .fleetUnreachable],
          "all three classes fire together")
    check(targets(alerts, .needsReview) == ["A", "B"], "two new review ids")
    check(targets(alerts, .cardFailedBlocked) == ["X"], "one new blocked id")
}

print("== LastSeenStateStore App-Group JSON round-trip ==")
do {
    // Use the REAL suite name (AppGroup is sliced from SharedModels) but a
    // scratch suite would be cleaner; the store reads AppGroup.suiteName, so we
    // round-trip through the real shared suite and clean up after ourselves.
    var s = LastSeenState()
    s.observedOnce = true
    s.observed.reviewQueue = ["A", "B"]
    s.observed.blocked = ["X"]
    s.observed.escalationsCount = 3
    s.announced.review = ["A"]
    s.announced.blocked = ["X"]
    s.announced.escalations = 3
    s.announced.unreachable = true
    LastSeenStateStore.save(s)
    let loaded = LastSeenStateStore.load()
    check(loaded == s, "saved LastSeenState reloads equal (Codable round-trip)")
    // Tear down so a real install isn't polluted by the check's state.
    let d = UserDefaults(suiteName: AppGroup.suiteName)
    d?.removeObject(forKey: LastSeenStateStore.lastSeenKey)
    d?.removeObject(forKey: LastSeenStateStore.announcedKey)
    d?.removeObject(forKey: LastSeenStateStore.observedOnceKey)
    check(LastSeenStateStore.load() == LastSeenState.empty,
          "cleared store loads as empty")
}

print("")
if failures == 0 {
    print("ALL NOTIFY CHECKS PASSED")
    exit(0)
} else {
    print("\(failures) NOTIFY CHECK(S) FAILED")
    exit(1)
}
