import AppKit
import SwiftUI

/// The dropdown behind the menu bar H. It is a read-only dashboard: cluster
/// health, `hscc cluster monitor`'s host and serving tables, `hscc cluster
/// status`'s workloads and slots, the cards being worked, board totals and the
/// project list — all from the shared client's existing GET routes, and every
/// CLI-shaped string drawn out of the shared view-model rather than formatted
/// here (`DashboardFleetSection.swift`, `DashboardFleetReading.swift`). Actions
/// are limited to refresh, opening Settings and quitting.
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
                needsMeSection
                mergeQueueSection
                clusterSection
                clusterMonitorSection
                clusterStatusSection
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

    // MARK: - Needs you (alert feed, card R3)

    /// The main orchestrator's unacknowledged alerts. Hidden when the feed is
    /// empty — there is no "nothing needs you" row in a menu an operator opens
    /// to look at work. A failed read is not empty: it says why, in the shared
    /// client's address-free words.
    @ViewBuilder
    private var needsMeSection: some View {
        if let failure = dashboard.snapshot.needsMeFailure {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                sectionTitle("Needs you")
                Label("Alert feed unavailable: \(failure)",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else if let feed = dashboard.snapshot.needsMe, !feed.items.isEmpty {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                HStack {
                    sectionTitle("Needs you")
                    Spacer()
                    Text("\(feed.count)")
                        .font(.caption.monospacedDigit().weight(.semibold))
                        .foregroundColor(Theme.Semantic.accent)
                }
                ForEach(feed.items.prefix(3)) { item in
                    HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
                        Circle()
                            .fill(item.level.isUrgent
                                  ? Theme.Semantic.accent : Theme.Semantic.onSurfaceMuted)
                            .frame(width: 6, height: 6)
                            .padding(.top, 4)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(item.title)
                                .font(.caption.weight(.medium))
                                .foregroundColor(Theme.Semantic.onSurface)
                                .lineLimit(1)
                            if !item.bodyFirstLine.isEmpty {
                                Text(item.bodyFirstLine)
                                    .font(.caption2)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                    .lineLimit(1)
                            }
                        }
                        Spacer(minLength: 0)
                        Text(item.ageLabel())
                            .font(.caption2.monospacedDigit())
                            .foregroundColor(item.level.isUrgent
                                             ? Theme.Semantic.accent : Theme.Semantic.onSurfaceMuted)
                    }
                }
                if feed.count > 3 {
                    Text("+\(feed.count - 3) more")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
    }

    // MARK: - Merge queue (card MQ5)

    /// The local queue's rows, in the CLI's order. Hidden when the queue is
    /// empty — there is no "nothing queued" row in a menu an operator opens to
    /// look at work. A failed read is not empty: it says why.
    @ViewBuilder
    private var mergeQueueSection: some View {
        if let failure = dashboard.snapshot.mergeQueueFailure {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                sectionTitle("Merge queue")
                Label("Merge queue unavailable: \(failure)",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else if let feed = dashboard.snapshot.mergeQueue, !feed.rows.isEmpty {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                HStack {
                    sectionTitle("Merge queue")
                    Spacer()
                    Text("\(feed.queued) queued")
                        .font(.caption.monospacedDigit().weight(.semibold))
                        .foregroundColor(feed.queued > 0
                                         ? Theme.Semantic.accent : Theme.Semantic.onSurfaceMuted)
                }
                ForEach(feed.rows.prefix(4)) { row in
                    HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
                        VStack(alignment: .leading, spacing: 1) {
                            Text("\(row.project) · \(row.prLabel)")
                                .font(.caption.weight(.medium).monospacedDigit())
                                .foregroundColor(Theme.Semantic.onSurface)
                                .lineLimit(1)
                            Text("\(row.cells[2]) · \(row.cells[3]) · \(row.cells[6])")
                                .font(.caption2)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        VStack(alignment: .trailing, spacing: 1) {
                            Text(row.state)
                                .font(.caption2.weight(.semibold))
                                .foregroundColor(mergeStateColor(row.state))
                            Text(row.ageLabel)
                                .font(.caption2.monospacedDigit())
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                }
                if feed.rows.count > 4 {
                    Text("+\(feed.rows.count - 4) more")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
    }

    private func mergeStateColor(_ state: String) -> Color {
        switch state {
        case "merged": return Theme.Semantic.ok
        case "resolve", "failed": return Theme.Semantic.bad
        case "rebasing", "gating", "merging": return Theme.Semantic.warn
        default: return Theme.Semantic.onSurfaceMuted
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

    /// The section header style, shared by the sections in this file, the two
    /// CLI-surface sections in `DashboardFleetSection.swift` AND the graphs
    /// section in `DashboardGraphsView.swift`. It is internal on purpose:
    /// `private` in Swift is file-scoped, so a second file extending this view
    /// could not reuse it, and a second copy of the header style would be a
    /// second design system.
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
