import SwiftUI

// MARK: - Drawer (IOS_REDESIGN 2026-10-14 §3.1)
//
// The drawer replaces the old three-tab bar. It is presented by AppShell as
// a slide-over panel the shell itself opens and closes (t_5ed5d281): on
// iPhone compact the NavigationSplitView sidebar column proved to be a dead
// end — zero dismiss affordances once it takes the screen, and inert column
// controls — so the shell owns the presentation and every row activation
// closes the panel through `go`.
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
                // The "Cluster" row is gone (t_65d791da, spec §3.1/§5/§8): the
                // topology strip it existed for is now the header Section of
                // "Nodes & serving", so one row covers machines AND what runs on
                // them. `.clusterHub` still resolves — as a mapped alias in
                // AppShell.normalized, never as a second row.
                row(.nodesServing)
                // The "Fleet Control" row is gone (t_2a29a7ab, spec §8 row
                // "FleetView · FleetControlView | drawer · Fleet (+ Control
                // section)"): the applied-template read and the confirm-gated
                // fleet up/down are Sections of Fleet's List, so one row covers
                // both. `.fleetControl` still resolves — as a mapped alias in
                // AppShell.normalized, never as a second row.
                row(.opsHealth)
                // spec §3.1: "Autodown & self-heal (was Autodown; history
                // becomes a section)" — ONE row. The old `.selfHeal` row is
                // gone; its destination still resolves (see AppShell.go).
                row(.autodown)
                row(.cron)
                row(.logs)
                // spec §8: BoardHygiene is merged into Approvals — its row is
                // gone; the `.boardHygiene` destination still resolves (see
                // AppShell.go), landing on the Approvals List.
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
                // Stable, UNIQUE test/AX address: while the drawer is up the
                // screen behind it still renders its own nav bar title
                // (e.g. "Home") into the AX tree, so a bare label query is
                // ambiguous by construction (iPhone 16 XCUITest, 2026-10-15).
                .accessibilityIdentifier("drawerRow:\(destination.rawValue)")
        } icon: {
            Image(systemName: destination.symbol)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .tag(destination)
        .badge(badge > 0 ? "\(badge)" : nil)
        .accessibilityLabel(accessibilityText(for: destination, badge: badge))
        // THE dead-end mechanism, fixed here (t_5ed5d281): List(selection:)
        // only writes the binding when the tapped tag DIFFERS from the
        // current selection — re-tapping the already-selected row is silent,
        // so `go` never ran and the drawer never closed. An explicit tap
        // handler fires for EVERY activation, same value included; the
        // setter is idempotent, so a row tap that also moves the selection
        // just runs `go` twice with identical effect. maxWidth .infinity so
        // the tap target spans the full row, not just the label.
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .onTapGesture {
            selection = destination
        }
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
