import SwiftUI

// MARK: - Home + dock (IOS_REDESIGN 2026-10-14 §3.2, §3.3)
//
// Home is the only screen that carries the dock. It is deliberately cheap to
// lose: the hero is a shortcut to Fleet and every tile is reachable from the
// drawer too, so Home never becomes a dependency (design §3.3).
//
// The dock is a START-LINE, not a tab bar — it does not persist selection on
// other screens, which is what keeps it from re-becoming the tab bar §3 removed.

/// The five dock destinations (design §3.2): the daily verbs of a cluster
/// operator — talk to the REPL, is it healthy, what is on the nodes, what
/// needs my yes, change a setting. Home is deliberately NOT in the dock.
enum DockItem: String, CaseIterable, Identifiable {
    case chat, fleet, cluster, approvals, settings

    var id: String { rawValue }

    var destination: AppDestination {
        switch self {
        case .chat: .projects
        case .fleet: .fleet
        case .cluster: .clusterHub
        case .approvals: .approvals
        case .settings: .settings
        }
    }

    var title: String {
        switch self {
        case .chat: "Chat"
        case .fleet: "Fleet"
        case .cluster: "Cluster"
        case .approvals: "Approvals"
        case .settings: "Settings"
        }
    }

    var symbol: String {
        switch self {
        case .chat: "bubble.left.and.bubble.right"
        case .fleet: "chart.bar"
        case .cluster: "bolt"
        case .approvals: "checkmark.circle"
        case .settings: "gearshape"
        }
    }
}

struct HomeView: View {
    @Binding var selection: AppDestination?
    let approvalCount: Int?

    @EnvironmentObject private var settings: SettingsStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.xl.rawValue) {
                header
                statusHero
                tileGrid
            }
            .padding(.horizontal, Theme.Spacing.lg.rawValue)
            .padding(.top, Theme.Spacing.md.rawValue)
            // Keep content clear of the floating dock.
            .padding(.bottom, 120)
        }
        .background(Theme.Semantic.surface)
        .navigationTitle("Home")
        .navigationBarTitleDisplayMode(.inline)
        .overlay(alignment: .bottom) { dock }
    }

    // MARK: Header (§3.3: greeting + which cluster)

    private var header: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            Text(greeting)
                .font(.title2.weight(.bold))
                .foregroundColor(Theme.Semantic.onSurface)
            Text(settings.activeCluster?.name ?? "No cluster selected")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .accessibilityElement(children: .combine)
    }

    private var greeting: String {
        switch Calendar.current.component(.hour, from: Date()) {
        case 5..<12: "Good morning"
        case 12..<18: "Good afternoon"
        default: "Good evening"
        }
    }

    // MARK: Status hero (the single "everything is fine" glance → Fleet)

    private var statusHero: some View {
        Button {
            selection = .fleet
        } label: {
            HStack(spacing: Theme.Spacing.lg.rawValue) {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        HSStatusDot(heroTint)
                        Text(heroTitle)
                            .font(.headline)
                            .foregroundColor(Theme.Semantic.onSurface)
                    }
                    Text(heroCaption)
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .padding(Theme.Spacing.lg.rawValue)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.Semantic.surfaceRaised)
            .clipShape(RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Cluster status: \(heroTitle). \(heroCaption). Open Fleet.")
    }

    /// The hero is honest: it reports what the last real request proved, and
    /// says "unknown" until one has (ConnectionMonitor's own contract — never
    /// a guess, never a green light on an untested connection).
    private var heroTint: Color {
        guard settings.isConfigured else { return Theme.Semantic.neutral }
        switch ConnectionMonitor.shared.status {
        case .reachable: return Theme.Semantic.ok
        case .unreachable: return Theme.Semantic.bad
        case .unknown: return Theme.Semantic.neutral
        }
    }

    private var heroTitle: String {
        guard settings.isConfigured else { return "Not connected" }
        switch ConnectionMonitor.shared.status {
        case .reachable: return "Cluster is up"
        case .unreachable: return "Can't reach the cluster"
        case .unknown: return "Checking the cluster"
        }
    }

    private var heroCaption: String {
        guard settings.isConfigured else {
            return "Add your cluster in Settings to begin."
        }
        switch ConnectionMonitor.shared.status {
        case .reachable: return "Last request succeeded · tap for fleet detail"
        case .unreachable(let reason): return reason
        case .unknown: return "Send a request or pull to refresh"
        }
    }

    // MARK: Tiles (§3.3: 2x2 of the most-tapped non-dock destinations)

    private var tileGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: Theme.Spacing.md.rawValue),
                            GridItem(.flexible(), spacing: Theme.Spacing.md.rawValue)],
                  spacing: Theme.Spacing.md.rawValue) {
            tile(.projects, "Projects", "folder")
            tile(.approvals, "Approvals", "checkmark.circle", badge: approvalCount)
            tile(.activity, "Activity", "waveform.path.ecg")
            tile(.search, "Search", "magnifyingglass")
        }
    }

    private func tile(_ destination: AppDestination, _ title: String, _ symbol: String,
                      badge: Int? = nil) -> some View {
        Button {
            selection = destination
        } label: {
            VStack(alignment: .leading, spacing: Theme.Spacing.md.rawValue) {
                HStack {
                    Image(systemName: symbol)
                        .font(.title3)
                        .foregroundColor(Theme.Semantic.accent)
                    Spacer()
                    if let badge, badge > 0 {
                        Text("\(badge)")
                            .font(.caption2.weight(.bold))
                            .foregroundColor(Theme.Semantic.onAccent)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Theme.Semantic.accent)
                            .clipShape(Capsule())
                            .accessibilityLabel("\(badge) pending")
                    }
                }
                Text(title)
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .multilineTextAlignment(.leading)
            }
            .padding(Theme.Spacing.lg.rawValue)
            .frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
            .background(Theme.Semantic.surfaceRaised)
            .clipShape(RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
    }

    // MARK: Dock (§3.2)

    private var dock: some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            ForEach(DockItem.allCases) { item in
                dockItem(item)
            }
        }
        .padding(.horizontal, Theme.Spacing.md.rawValue)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .background(.regularMaterial)
        .clipShape(RoundedRectangle(cornerRadius: Theme.Corner.dock.rawValue, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Corner.dock.rawValue, style: .continuous)
                .strokeBorder(Theme.Semantic.onSurface.opacity(0.08), lineWidth: 1)
        )
        .shadow(color: Theme.Elevation.midShadow,
                 radius: Theme.Elevation.midRadius, y: Theme.Elevation.midY)
        .padding(.horizontal, Theme.Spacing.lg.rawValue)
        .padding(.bottom, Theme.Spacing.xl.rawValue)
        .accessibilityLabel("Dock")
    }

    private func dockItem(_ item: DockItem) -> some View {
        let isActive = selection == item.destination
        return Button {
            selection = item.destination
        } label: {
            VStack(spacing: Theme.Spacing.xxs.rawValue) {
                ZStack(alignment: .topTrailing) {
                    Image(systemName: item.symbol)
                        .font(.title3)
                        .foregroundColor(isActive ? Theme.Semantic.accent : Theme.Semantic.onSurfaceMuted)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                    if item == .approvals, let count = approvalCount, count > 0 {
                        Text("\(count)")
                            .font(.caption2.weight(.bold))
                            .foregroundColor(Theme.Semantic.onAccent)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Theme.Semantic.accent)
                            .clipShape(Capsule())
                            .offset(x: 8, y: -2)
                            .accessibilityLabel("\(count) pending approvals")
                    }
                }
                Text(item.title)
                    .font(.caption2)
                    .foregroundColor(isActive ? Theme.Semantic.accent : Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                // Active indicator BELOW the label (design §3.2), not a fill —
                // so the dock never reads as a selected tab bar.
                RoundedRectangle(cornerRadius: 2)
                    .fill(isActive ? Theme.Semantic.accent : Color.clear)
                    .frame(width: 18, height: 3)
            }
        }
        .buttonStyle(.plain)
        .frame(minWidth: 52, minHeight: 60)
        .accessibilityLabel(item.title)
        .accessibilityAddTraits(isActive ? .isSelected : [])
    }
}
