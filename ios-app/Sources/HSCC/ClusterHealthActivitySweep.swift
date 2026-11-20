import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// ClusterHealthActivitySweep — the re-hydration half of the cluster-health
// Live Activity.
//
// A Live Activity outlives the process that started it. If the app dies
// (force-quit, OS eviction) while a cluster-health bubble is on screen, the
// bubble keeps its last pushed state forever — a mint "Serving · 4/4 up" from
// before the kill, with nothing behind it. Releasing the `Activity` object does
// not end it and no `deinit` ran to end it, so the app reconciles whatever it
// finds at launch.
//
// The DECISION is not made here. It is `ClusterHealthOrphanRule.isOrphan`, a
// pure rule that lives beside the content state in
// `Sources/Shared/ClusterHealthActivity.swift`, and which
// `scripts/live_activity_rehydration_check.sh` slices out of that file and
// executes as a decision table. This file's job is only to read the found
// activities and act on the answer.
//
// This is also why the sweep does not consult `AppGroupWidgetSettings`: the
// rule is pure, so the launch sweep and the gate execute the same decision
// instead of two copies of a settings read.
// ---------------------------------------------------------------------------

enum ClusterHealthActivitySweep {
    /// End any leftover cluster-health activity the launch found to be an orphan.
    ///
    /// Call once at launch (the delegate's `didFinishLaunching`). Safe on every
    /// launch: it only reads the static
    /// `Activity.activities` snapshot and ends the ones the rule flagged, and
    /// ends run on their own Tasks because `end` is async. A no-op while no
    /// cluster-health activity exists.
    static func sweepLeftoverClusterHealth() {
        let now = Date()
        for activity in Activity<ClusterHealthActivityAttributes>.activities {
            let state = activity.content.state
            guard ClusterHealthOrphanRule.isOrphan(state.signals,
                                                   updatedAt: state.updatedAt,
                                                   now: now) else {
                continue  // there is a signal and the push is recent — the loop is alive
            }
            Task { @MainActor in
                await activity.end(nil, dismissalPolicy: .immediate)
            }
        }
    }
}
