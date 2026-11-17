import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// CardsRunningActivitySweep — the re-hydration half of the running-cards
// Live Activity.
//
// A Live Activity outlives the process that started it. If the app dies
// (force-quit, OS eviction) while a running-cards activity is on screen, the
// bubble keeps its last pushed rows forever: releasing the `Activity` object
// does not end it, and no `deinit` ran to end it. So the app reconciles
// whatever it finds at launch, exactly as `LiveActivityManager.sweepLeftoverWakes`
// does for the wake and `MonitorActivityDriver.sweepLeftoverMonitors` does for
// the monitor.
//
// The DECISION is not made here: it is `CardsRunningOrphanRule.isOrphan`, a
// pure rule that lives beside the content state in
// `Sources/Shared/CardsRunningActivity.swift` so the activity's own file owns
// its vocabulary, and which `scripts/live_activity_rehydration_check.sh` slices
// out of that file and executes as a decision table. This file's job is only to
// read the found activities and act on the answer.
// ---------------------------------------------------------------------------

enum CardsRunningActivitySweep {
    /// End any leftover running-cards activity the launch found to be an orphan.
    ///
    /// Call once at launch (the delegate's `didFinishLaunching`, beside the
    /// monitor sweep). Safe on every launch: it only reads the static
    /// `Activity.activities` snapshot and ends the ones the rule flagged, and
    /// ends run on their own Tasks because `end` is async.
    static func sweepLeftoverCards() {
        let now = Date()
        for activity in Activity<CardsRunningActivityAttributes>.activities {
            let state = activity.content.state
            guard CardsRunningOrphanRule.isOrphan(rowCount: state.rows.count,
                                                  updatedAt: state.updatedAt,
                                                  now: now) else {
                continue  // rows are real and the push is recent — the loop is alive
            }
            Task { @MainActor in
                await activity.end(nil, dismissalPolicy: .immediate)
            }
        }
    }
}
