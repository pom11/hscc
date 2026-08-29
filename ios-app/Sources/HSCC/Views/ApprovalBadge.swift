import SwiftUI

/// Pending-approval badge for the Cluster tab (t_9a5cfc3b).
///
/// The approvals inbox is the operator's phone-shaped "is there something
/// needing me right now?" signal, so the pending count is surfaced as a badge
/// on the Cluster tab even before the operator opens the inbox.
///
/// Design (simple over clever):
///   * Polls `/v1/kanban/blocked` every `pollInterval` while a client is set,
///     filtering with the SAME `BlockedCard.isPendingApproval` classification
///     the on-screen inbox and the Siri intent use — one source of truth.
///   * `pendingCount == nil` means "don't know yet" (no badge), NOT zero — we
///     never claim "no approvals" before we've actually fetched.
///   * A fetch failure leaves the last-known count in place; the badge is
///     informational, so a transient offline moment must not clear it to zero.
///   * Purely observational — it never mutates anything.
@MainActor
final class ApprovalPoller: ObservableObject {
    @Published private(set) var pendingCount: Int?

    private static let pollInterval: TimeInterval = 60

    private var client: HSCCClient?
    private var timer: Timer?

    /// Attach the current client (or nil when unconfigured). Restarts the
    /// polling loop around the new client.
    func setClient(_ client: HSCCClient?) {
        self.client = client
        timer?.invalidate()
        timer = nil
        guard client != nil else { return }
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval,
                                     repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refresh() }
        }
    }

    func refresh() async {
        guard let client else { return }
        do {
            let blocked = try await client.kanbanBlocked()
            pendingCount = (blocked.tasks ?? []).filter(\.isPendingApproval).count
        } catch {
            // Keep the last-known count; do NOT clear to zero on a transient
            // offline moment (the badge is informational).
        }
    }
}
