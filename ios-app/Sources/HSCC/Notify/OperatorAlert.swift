import Foundation

/// A single "the operator needs you" alert raised by `NeedsOperatorNotifier`.
///
/// This is the pure, transport-agnostic payload that both the foreground
/// coordinator and a future APNs-backed sender can deliver. It carries the
/// fields `UNUserNotificationCenter` needs to present a banner (title, body,
/// sound, a `threadIdentifier` group) plus the targets/conditions that
/// produced it, so a delivery layer never has to re-derive intent.
struct OperatorAlert: Equatable {
    /// The class of condition that produced this alert. Each kind maps to one
    /// Settings toggle and one notification thread, so the operator can mute
    /// exactly the class of interruption they don't want.
    enum Kind: String, Codable, CaseIterable {
        /// A card newly entered the review queue.
        case needsReview
        /// A card newly failed/blocked, or the pending-escalation count rose.
        case cardFailedBlocked
        /// The fleet/API became unreachable (a confident reachable→unreachable
        /// transition), or its daemon stopped.
        case fleetUnreachable

        /// The thread identifier that groups delivered notifications by class,
        /// so iOS threads (rather than piles) each condition's alerts.
        var threadIdentifier: String {
            "hscc.notify." + rawValue
        }
    }

    let kind: Kind
    let title: String
    let body: String
    /// Whether the delivered banner should play its sound / vibrate.
    let sound: Bool
    let threadIdentifier: String
    /// The card ids / targets that produced this alert (empty for fleet-down).
    let targetIDs: [String]

    init(kind: Kind,
         title: String,
         body: String,
         sound: Bool = true,
         threadIdentifier: String? = nil,
         targetIDs: [String] = []) {
        self.kind = kind
        self.title = title
        self.body = body
        self.sound = sound
        self.threadIdentifier = threadIdentifier ?? kind.threadIdentifier
        self.targetIDs = targetIDs
    }
}
