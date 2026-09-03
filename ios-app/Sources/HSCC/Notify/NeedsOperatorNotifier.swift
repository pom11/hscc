import Foundation

// ===========================================================================
// NeedsOperatorNotifier — the pure, testable, APNs-ready decision engine.
//
// Turns "(prior state, current observations)" into "a set of notifications to
// fire". The plan (docs/notify-operator-plan.md) makes this the SHARED seam:
// foreground local notifications and a future APNs-backed sender both call the
// same `compute(prior:now:)` with the same inputs, so no transport rework is
// ever needed to add a push server.
//
// Purity: this type does no I/O, no network, no UserNotifications. It reads
// two Codable value types and returns value types, so it compiles+run as a
// plain macOS CLI (like chat_state_check.sh) — no iOS runtime needed to prove
// the logic, and it is table-driven unit-testable.
//
// The engine is DIFFERENTIAL, never absolute: it only fires on conditions that
// are NEW relative to the prior, and the returned `nextState` lets the caller
// persist exactly what was announced so the same occurrence is never
// re-announced. Clear-on-clear means a later recurrence announces again.
// ===========================================================================

enum NeedsOperatorNotifier {
    /// Pure: derive the alerts to fire for `now` given the `prior` state.
    /// Returns `[]` when nothing is new.
    static func compute(prior: LastSeenState, now: ObservedState) -> [OperatorAlert] {
        evaluate(prior: prior, now: now).alerts
    }

    /// Pure: derive the next persisted state (observed + announced) for `now`
    /// given the `prior`. The coordinator persists this after firing, so
    /// `compute` never needs to mutate shared state to be correct.
    static func nextState(prior: LastSeenState, now: ObservedState) -> LastSeenState {
        evaluate(prior: prior, now: now).state
    }

    // MARK: - Evaluation

    /// The single source of truth for the differential logic. Both `compute`
    /// and `nextState` derive from it so the dedup rules can never drift apart
    /// between "what to say" and "what to record".
    private static func evaluate(prior: LastSeenState,
                                 now: ObservedState)
        -> (alerts: [OperatorAlert], state: LastSeenState) {
        var alerts: [OperatorAlert] = []
        var announced = prior.announced

        // FIRST observation anchors silently: pre-existing conditions on a
        // fresh install are "prior art", not "new since you last looked" —
        // announcing them would spray a wall of notifications the moment the
        // operator opens the app, when they are already looking. We still
        // persist the observation so the NEXT genuinely-new condition fires.
        if prior.observedOnce {
            alerts += needsReviewAlerts(now: now, announced: &announced)
            alerts += cardFailedBlockedAlerts(now: now, announced: &announced)
            alerts += fleetUnreachableAlert(now: now, announced: &announced)
        }

        var state = prior
        state.observed = now
        state.announced = announced
        state.observedOnce = true
        return (alerts, state)
    }

    // MARK: - Per-condition differential logic

    private static func needsReviewAlerts(now: ObservedState,
                                          announced: inout AnnouncedState) -> [OperatorAlert] {
        // announced.review tracks ids already told to the operator; a queue
        // that gains a new id fires for the new id only. Assigning the full
        // current queue to announced each poll means a card that leaves and
        // later re-enters is treated as a fresh occurrence (clear-on-clear).
        let newReview = now.reviewQueue.subtracting(announced.review)
        let alerts = newReview.sorted().map { id in
            OperatorAlert(
                kind: .needsReview,
                title: "New card needs review",
                body: "\(id) is waiting in the review queue.",
                targetIDs: [id]
            )
        }
        announced.review = now.reviewQueue
        return alerts
    }

    private static func cardFailedBlockedAlerts(now: ObservedState,
                                                announced: inout AnnouncedState) -> [OperatorAlert] {
        var alerts: [OperatorAlert] = []

        let newBlocked = now.blocked.subtracting(announced.blocked)
        alerts += newBlocked.sorted().map { id in
            OperatorAlert(
                kind: .cardFailedBlocked,
                title: "Card blocked",
                body: "\(id) is blocked and needs attention.",
                targetIDs: [id]
            )
        }
        announced.blocked = now.blocked

        if now.escalationsCount > announced.escalations {
            alerts.append(OperatorAlert(
                kind: .cardFailedBlocked,
                title: "Card failed",
                body: "\(now.escalationsCount) pending escalation"
                    + (now.escalationsCount == 1 ? "" : "s")
                    + " — a worker failed and needs reassignment."
            ))
            announced.escalations = now.escalationsCount
        } else if now.escalationsCount == 0 {
            // Condition cleared → forget what was announced so a LATER spike
            // from zero re-announces.
            announced.escalations = 0
        }

        return alerts
    }

    private static func fleetUnreachableAlert(now: ObservedState,
                                              announced: inout AnnouncedState) -> [OperatorAlert] {
        // Confident "down" = a definite API failure OR the daemon reported not
        // running. A nil `apiReachable` (inconclusive poll) is NOT a confident
        // down and must never spam — it neither fires nor clears.
        let fleetDown = (now.apiReachable == false) || (now.daemonRunning == false)
        if fleetDown {
            if !announced.unreachable {
                announced.unreachable = true
                return [OperatorAlert(
                    kind: .fleetUnreachable,
                    title: "Cluster unreachable",
                    body: "The HSCC cluster stopped responding — is Tailscale connected?"
                )]
            }
        } else if now.apiReachable == true {
            // Confident reachable again → clear so a later down re-announces.
            announced.unreachable = false
        }
        // else: inconclusive (nil) — leave the down-announcement state alone.
        return []
    }
}
