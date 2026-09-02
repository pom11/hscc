import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// LiveActivityManager — the app side of the fleet-wake Live Activity.
//
// The app starts the activity when a wake begins (autodown state → waking),
// updates it as units report up, and ENDS it with an explicit success/failure
// message when the state settles. Ending with a message matters: a Live
// Activity that just disappears is worse than none.
//
// Honest progress only: the wake has no reliable progress signal, so the
// content carries elapsed time (rendered by the activity on a timer) and the
// per-unit readiness the API actually reports — never a fabricated percentage.
//
// Uses the CLASS-based ActivityKit API (`Activity.request`, then instance
// `update` / `end`), which is what the iOS 26 SDK ships.
// ---------------------------------------------------------------------------

/// The canonical node labels, in the topology-pair order the UI draws.
private let wakeNodeLabels = [".244", ".246", ".247", ".248"]

@MainActor
final class LiveActivityManager {
    /// The in-flight activity, if any. Exactly one wake activity at a time.
    private var current: Activity<WakingActivityAttributes>?

    /// Whether a start/poll is already running (guard against double-run).
    private var isRunning = false
    /// When the current wake started — drives the enforced elapsed-time gauge.
    private var wakeStart = Date()

    /// The operator can leave the Autodown screen (or the app) mid-wake; the
    /// wake can take ~9 minutes. If this manager is torn down while a wake is
    /// live — the view that owns it (@State in AutodownView) is popped and the
    /// polling Task holds `[weak self]`, so it dies with us — an ActivityKit
    /// activity that is not ended keeps an orphaned, never-updated bubble on
    /// the Lock Screen/Dynamic Island ("started and never ended"). Releasing
    /// the `Activity` object does NOT end it. So end it from `deinit` (an
    /// `onDisappear` can't be relied on — TabViews and nav pops fire it at
    /// unpredictable times, and SwiftUI may destroy the view before `end`
    /// reaches the system).
    deinit {
        if let activity = current {
            Task { @MainActor in
                await activity.end(nil, dismissalPolicy: .immediate)
            }
        }
    }

    /// Begin tracking a wake. Starts a Live Activity and polls the API until
    /// the autodown state leaves "waking", then ends with success/failure.
    ///
    /// - Parameter client: configured HSCC client (the wake was triggered
    ///   through it).
    func beginWake(client: HSCCClient) {
        guard !isRunning else { return }
        isRunning = true
        wakeStart = Date()
        // Only one wake can be presented at a time — drop any prior one first
        // so a new wake replaces an old, now-stale activity.
        endCurrentSilently()

        // Start the Live Activity (throws if the system can't show one).
        let attributes = WakingActivityAttributes()
        let initialState = WakingActivityAttributes.ContentState(
            state: "waking",
            upNodes: [],
            startedAt: wakeStart,
            succeeded: false,
            failed: false,
            message: nil
        )
        do {
            current = try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: initialState, staleDate: nil)
            )
        } catch {
            // The system refused — continue polling so we still surface the
            // outcome somewhere (the Autodown screen), just without a Live
            // Activity bubble.
            current = nil
        }

        // Poll in the background; the Live Activity update must hop onto the
        // main actor (we are already @MainActor).
        Task { [weak self] in
            await self?.pollUntilSettled(client: client)
            self?.isRunning = false
        }
    }

    /// Poll the autodown state + per-unit readiness until the state leaves
    /// "waking", updating the activity as it goes.
    private func pollUntilSettled(client: HSCCClient) async {
        // Poll every 30s — a wake is minutes-scale; anything tighter just burns
        // battery and API calls for no perceived gain.
        while current != nil {
            let outcome = await fetchOutcome(client: client)
            guard let outcome else {
                // Network hiccup — keep polling; don't end on a transient error.
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                continue
            }

            // Update the content with the latest readiness.
            let content = WakingActivityAttributes.ContentState(
                state: outcome.state,
                upNodes: outcome.upNodes,
                startedAt: wakeStart,
                succeeded: outcome.succeeded,
                failed: outcome.failed,
                message: outcome.message
            )
            await update(content: content)

            // Settled: end the activity with the outcome message.
            if outcome.isSettled {
                await end(reason: content)
                return
            }
            try? await Task.sleep(nanoseconds: 30_000_000_000)
        }
    }

    /// One poll: read autodown state + which nodes verify. Returns nil on a
    /// transient network failure (keep polling), an outcome otherwise.
    private func fetchOutcome(client: HSCCClient) async -> WakeOutcome? {
        do {
            let auto = try await client.autodownStatus()
            let state = auto.state ?? "unknown"
            // Per-unit readiness: which of the four units verify healthy, so we
            // can honestly show the topology coming online.
            let upNodes = try await upNodes(client: client)

            switch state {
            case "waking":
                return WakeOutcome(state: "waking", upNodes: upNodes,
                                   succeeded: false, failed: false,
                                   message: nil, isSettled: false)
            case "up":
                return WakeOutcome(state: "up", upNodes: upNodes,
                                   succeeded: true, failed: false,
                                   message: "Fleet is up.",
                                   isSettled: true)
            default:
                // down / other → the wake did not succeed. Say so.
                let reason: String = {
                    if let r = auto.reason, !r.isEmpty { return r }
                    return state
                }()
                return WakeOutcome(state: state, upNodes: upNodes,
                                   succeeded: false, failed: true,
                                   message: "Wake didn't reach serving — \(reason).",
                                   isSettled: true)
            }
        } catch {
            return nil
        }
    }

    /// Which of the four nodes are currently serving, from /v1/cluster/status.
    /// The cluster engine reports `idle_hosts` as text lines containing the
    /// node's private IP (e.g. "node_0  10.0.0.244 ... Up 3 hours"). We
    /// map those back to the canonical short labels so the topology can honestly
    /// show each pair coming online as its node reports up.
    private func upNodes(client: HSCCClient) async throws -> [String] {
        let cluster = try await client.clusterStatus()
        let upIPs = cluster.idle_hosts.flatMap { line -> [String] in
            // Pull every dotted-quad private IP out of the line.
            line.split(whereSeparator: { !$0.isNumber && $0 != "." })
                .map(String.init)
                .filter { $0.split(separator: ".").count == 4 }
        }
        return wakeNodeLabels.filter { label in
            let tail = label.replacingOccurrences(of: ".", with: "")   // "244"
            return upIPs.contains { $0.hasSuffix("." + tail) || $0.hasSuffix(tail) }
        }
    }

    /// Push the latest content to the currently-active activity (no-op if none).
    private func update(content: WakingActivityAttributes.ContentState) async {
        guard let current else { return }
        let contentState = ActivityContent(state: content, staleDate: nil)
        await current.update(contentState)
    }

    /// End the activity with the final success/failure content + message.
    private func end(reason content: WakingActivityAttributes.ContentState) async {
        guard let current else { return }
        let contentState = ActivityContent(state: content, staleDate: nil)
        await current.end(contentState, dismissalPolicy: .immediate)
        self.current = nil
    }

    /// Drop any leftover activity without a message (cleanup between wakes).
    ///
    /// Runs the `end` on a background Task. It must NOT blindly nil `self.current`
    /// afterwards: `beginWake` calls this and then immediately assigns the NEW
    /// activity to `self.current`. Because this Task only runs after `beginWake`
    /// yields, an unguarded `self.current = nil` here would clobber the reference
    /// to the freshly-started activity, leaving it on the lock screen forever —
    /// never updated, never ended (an orphan). Only clear the reference if it is
    /// still the same activity we ended.
    private func endCurrentSilently() {
        guard let activity = current else { return }
        Task {
            await activity.end(nil, dismissalPolicy: .immediate)
            if self.current === activity {
                self.current = nil
            }
        }
    }
}

/// A single wake poll outcome.
private struct WakeOutcome {
    let state: String
    let upNodes: [String]
    let succeeded: Bool
    let failed: Bool
    let message: String?
    let isSettled: Bool
}
