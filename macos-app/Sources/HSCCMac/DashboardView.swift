import AppKit
import SwiftUI

/// The dropdown behind the menu bar H. It is a read-only dashboard: cluster
/// health, the cards being worked, board totals and the project list, all from
/// the shared client's existing GET routes. Actions are limited to refresh,
/// opening Settings and quitting.
struct DashboardView: View {
    @ObservedObject var dashboard: DashboardMonitor
    @EnvironmentObject private var settings: SettingsStore
    @EnvironmentObject private var preferences: MacPreferences
    /// Sparkle state (t_2a1e5913): the update badge/item and the manual check.
    @EnvironmentObject private var updates: SparkleUpdater

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.md.rawValue) {
            header
            Divider().overlay(Theme.Semantic.surfaceElevated)

            if let version = updates.availableVersion {
                Button {
                    updates.checkForUpdates()
                } label: {
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        Label("Update \(version) available", systemImage: "arrow.down.circle.fill")
                            .font(.caption.weight(.semibold))
                        Spacer()
                        Text("Install…")
                            .font(.caption)
                    }
                    .foregroundColor(Theme.Semantic.accent)
                }
                .buttonStyle(.plain)
                .accessibilityHint("Opens Sparkle's update window")
            }

            if !dashboard.snapshot.configured {
                setupInvite
            } else {
                clusterSection
                graphsSection
                runningSection
                boardSection
                projectsSection
            }

            if let failure = dashboard.snapshot.failure {
                Label(failure, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider().overlay(Theme.Semantic.surfaceElevated)
            footer
        }
        .padding(Theme.Spacing.lg.rawValue)
        .frame(width: 360)
        .background(Theme.Semantic.surface)
        .preferredColorScheme(.dark)
        // The dropdown is rebuilt every time it opens, so a Settings write
        // (cluster switch, cadence, appearance) is visible on the next open
        // instead of waiting for the menu bar loop's next tick.
        .onAppear {
            Task { await dashboard.refresh() }
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Image("MenuBarIcon")
                .renderingMode(.template)
                .resizable()
                .scaledToFit()
                .frame(width: 20, height: 20)
                .foregroundColor(Theme.Semantic.accent)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text("HSCC")
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                Text(settings.activeCluster?.name ?? "No cluster selected")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
            Spacer()
            if dashboard.isRefreshing {
                ProgressView()
                    .controlSize(.small)
            } else if let updated = dashboard.snapshot.lastRefresh {
                Text(updated.formatted(date: .omitted, time: .shortened))
                    .font(.caption.monospacedDigit())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    // MARK: - Setup

    private var setupInvite: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            Label("No cluster configured", systemImage: "plus.circle")
                .font(.subheadline.weight(.semibold))
                .foregroundColor(Theme.Semantic.onSurface)
            Text("Add a host, port and token in Settings. Saved clusters keep their own Keychain token and fall back through every address you list.")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .fixedSize(horizontal: false, vertical: true)
            SettingsLink {
                Text("Open HSCC Settings…")
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.Semantic.accent)
        }
    }

    // MARK: - Cluster health

    private var clusterSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            sectionTitle("Cluster")
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                Circle()
                    .fill(dashboard.snapshot.clusterState.color)
                    .frame(width: 8, height: 8)
                Text(dashboard.snapshot.clusterState.label)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                Spacer()
                Text(nodesText)
                    .font(.caption.monospacedDigit())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            HStack(spacing: Theme.Spacing.lg.rawValue) {
                metric("Models", modelsText)
                metric("Running", runningCountText)
                metric("Projects", projectCountText)
            }
        }
    }

    private var nodesText: String {
        guard let up = dashboard.snapshot.nodesUp,
              let total = dashboard.snapshot.nodesTotal else { return "nodes —" }
        return "\(up)/\(total) nodes up"
    }

    private var modelsText: String {
        guard let count = dashboard.snapshot.modelCount else { return "—" }
        return count == 1 ? "1" : "\(count)"
    }

    private var runningCountText: String {
        guard let running = dashboard.snapshot.running else { return "—" }
        let count = running.runningCount ?? (running.rows.isEmpty ? nil : running.rows.count)
        guard let count else { return "—" }
        return "\(count)"
    }

    private var projectCountText: String {
        guard let count = dashboard.snapshot.projectCount else { return "—" }
        return "\(count)"
    }

    // MARK: - Running cards

    private var runningSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            sectionTitle("Running cards")
            if let running = dashboard.snapshot.running, !running.rows.isEmpty {
                ForEach(running.rows.prefix(5)) { card in
                    VStack(alignment: .leading, spacing: 1) {
                        Text(card.headline(max: 46))
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurface)
                            .lineLimit(1)
                        Text("\(card.workerText) · \(card.ageText)")
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                    }
                }
            } else {
                Text(dashboard.snapshot.running == nil ? "—" : "No cards running")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    // MARK: - Board totals

    private var boardSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            sectionTitle("Kanban")
            if let totals = dashboard.snapshot.boardTotals {
                HStack(spacing: Theme.Spacing.md.rawValue) {
                    countCell("Todo", totals.todo, Theme.Semantic.neutral)
                    countCell("Ready", totals.ready, Theme.Semantic.accent)
                    countCell("Running", totals.running, Theme.Semantic.ok)
                    countCell("Blocked", totals.blocked, Theme.Semantic.bad)
                    countCell("Done", totals.done, Theme.Semantic.ok)
                }
            } else {
                Text("—")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    private func countCell(_ label: String, _ value: Int, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("\(value)")
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundColor(color)
            Text(label)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Projects

    private var projectsSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            sectionTitle("Projects")
            if dashboard.snapshot.projects.isEmpty {
                Text(dashboard.snapshot.projectCount == nil ? "—" : "No projects")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            } else {
                ForEach(dashboard.snapshot.projects) { project in
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        Text(project.name)
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurface)
                            .lineLimit(1)
                        Spacer()
                        if let board = project.board, !board.isEmpty {
                            Text(board)
                                .font(.caption2)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .lineLimit(1)
                        }
                    }
                }
            }
        }
    }

    // MARK: - Footer

    private var footer: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Button {
                updates.checkForUpdates()
            } label: {
                if updates.isChecking {
                    Label("Checking for updates…", systemImage: "arrow.triangle.2.circlepath")
                } else {
                    Label("Check for Updates…", systemImage: "arrow.triangle.2.circlepath")
                }
            }
            .buttonStyle(.plain)
            .foregroundColor(Theme.Semantic.onSurface)
            .disabled(updates.isChecking)

            SettingsLink {
                Label("Open HSCC Settings…", systemImage: "gearshape")
            }
            .buttonStyle(.plain)
            .foregroundColor(Theme.Semantic.onSurface)

            Button {
                Task { await dashboard.refresh() }
            } label: {
                Label("Refresh now", systemImage: "arrow.clockwise")
            }
            .buttonStyle(.plain)
            .foregroundColor(Theme.Semantic.onSurface)
            .disabled(dashboard.isRefreshing)

            Button {
                NSApplication.shared.terminate(nil)
            } label: {
                Label("Quit", systemImage: "power")
            }
            .buttonStyle(.plain)
            .foregroundColor(Theme.Semantic.onSurface)
        }
        .font(.caption)
    }

    // MARK: - Small pieces

    /// Internal, not private: `DashboardGraphsView.swift` draws its section
    /// header with this exact type (Swift `private` is file-scoped, and the
    /// graphs section lives in its own file so the dropdown body stays a
    /// plain sequence of sections). One section-header style, one owner.
    func sectionTitle(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.caption2.weight(.semibold))
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
    }

    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value)
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundColor(Theme.Semantic.onSurface)
            Text(label)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }
}
