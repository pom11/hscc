import SwiftUI

// MARK: - Drawer (IOS_REDESIGN 2026-10-14 §3.1)
//
// The sidebar replaces the old three-tab bar. Inside a NavigationSplitView on
// iPhone it renders as a slide-over drawer with the system's own top-left
// toggle and edge-swipe reveal, so the gesture is the OS one and never fights
// the transcript's scroll.
//
// Grouped by task frequency for a single operator: Work / Cluster / Knowledge /
// App. Every screen that lived under the tab bar or buried in ClusterView's
// long scroll has a row here — the migration map in the spec (§8) is the
// contract that nothing was dropped.

struct Sidebar: View {
    @Binding var selection: AppDestination?
    let approvalCount: Int?
    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var unread: ProjectUnreadCenter
    /// The shared monitor, OBSERVED: the header dot/text must redraw when the
    /// most-recent-request outcome changes, not only when this view is built.
    @ObservedObject private var monitor = ConnectionMonitor.shared

    private var pendingBadge: Int { approvalCount ?? 0 }
    private var chatBadge: Int { unread.totalUnread }

    var body: some View {
        List(selection: $selection) {
            Section {
                clusterHeader
            }
            group("Work") {
                row(.home)
                row(.chats, badge: chatBadge)
                row(.projects)
                row(.cards)
                row(.approvals, badge: pendingBadge)
            }
            group("Cluster") {
                row(.fleet)
                row(.clusterHub)
                row(.nodesServing)
                row(.fleetControl)
                row(.opsHealth)
                // spec §3.1: "Autodown & self-heal (was Autodown; history
                // becomes a section)" — ONE row. The old `.selfHeal` row is
                // gone; its destination still resolves (see AppShell.go).
                row(.autodown)
                row(.cron)
                row(.logs)
                row(.boardHygiene)
            }
            group("Knowledge") {
                row(.memory)
                row(.templates)
                row(.sessions)
                row(.activity)
            }
            group("App") {
                row(.search)
                row(.settings)
            }
        }
        .listStyle(.sidebar)
        .accessibilityLabel("HSCC navigation")
    }

    /// The drawer's own header: which cluster am I looking at, is it up.
    private var clusterHeader: some View {
        HStack(spacing: Theme.Spacing.md.rawValue) {
            HSCCMark(size: 34)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(settings.activeCluster?.name ?? "HSCC")
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                HStack(spacing: Theme.Spacing.xs.rawValue) {
                    HSStatusDot(dotTint)
                    // The word MUST match the dot — "connected" printed under
                    // a grey or red dot is the colour-lie the spec bans (§9).
                    Text(dotWord)
                        .font(.footnote)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            Spacer()
        }
        .padding(.vertical, Theme.Spacing.xs.rawValue)
        .accessibilityElement(children: .combine)
    }

    private func group<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        Section(title) { content() }
    }

    private func row(_ destination: AppDestination, badge: Int = 0) -> some View {
        Label {
            Text(destination.title)
        } icon: {
            Image(systemName: destination.symbol)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .tag(destination)
        .badge(badge > 0 ? "\(badge)" : nil)
        .accessibilityLabel(accessibilityText(for: destination, badge: badge))
    }

    /// A pending-approval count must not be colour-only (spec §9): the number
    /// is spoken as well as shown.
    private func accessibilityText(for destination: AppDestination, badge: Int) -> String {
        badge > 0
            ? "\(destination.title), \(badge) waiting"
            : destination.title
    }
}


extension Sidebar {
    /// Header dot colour: honest three-state (unknown is grey, not green).
    /// Reads the OBSERVED monitor, so the dot follows the most recent request.
    private var dotTint: Color {
        guard settings.isConfigured else { return Theme.Semantic.neutral }
        switch monitor.status {
        case .reachable: return Theme.Semantic.ok
        case .unreachable: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.neutral
        }
    }

    /// The dot's word — never colour alone (spec §9). Matches `dotTint`'s
    /// three states exactly.
    private var dotWord: String {
        guard settings.isConfigured else { return "not configured" }
        switch monitor.status {
        case .reachable: return "connected"
        case .unreachable: return "unreachable"
        case .unknown: return "checking…"
        }
    }
}
