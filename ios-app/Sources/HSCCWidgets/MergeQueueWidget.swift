import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// MergeQueueWidget.swift — the local merge queue, at a glance. READ-ONLY:
// one GET through `WidgetDataClient`, no queue steering, no git/gh.
//
// WHAT IT SHOWS, AND WHERE EACH VALUE COMES FROM
//
// `GET /v1/merge-queue` answers `hscc_daemon.merge_queue.view()` — the SAME
// rows `hscc kanban monitor` draws as its per-project "merge queue" tables.
// `MergeQueueReading.swift` is the one projection, and `MergeQueueTableReading`
// holds the CLI's exact columns and cells (Pos, PR, Card, Branch, State, Age,
// Last result), so the medium and large families draw the CLI's table rather
// than a re-derivation of it. Order is the route's order: registry project
// order, FIFO inside a project, the in-flight claim after the queued rows, then
// the project's last terminal outcome.
//
//   systemSmall  <- queued count + what is merging now
//   systemMedium <- the CLI's table, first 3 rows
//   systemLarge  <- the CLI's table, first 8 rows + the last merged row
//   accessoryCircular / Rectangular / Inline <- the same facts, Lock Screen copy
//
// COLOUR. State carries the one colour rule: merged -> ok, resolve/failed ->
// bad, rebasing/gating/merging -> warn, queued -> secondary. Lock Screen
// families are system-tinted and carry the same hierarchy as primary/secondary.
//
// FAILURE RULE. `WidgetData.read` owns live/stale/failed. A stale tile keeps
// its real rows and names the reason and its age; a failed tile names the
// reason instead of drawing a fabricated zero. Empty is its own honest state:
// "Nothing queued" with the update age. Paused is decided before the client
// exists, so a paused surface sends zero requests.
// ---------------------------------------------------------------------------

// MARK: - Entry

struct MergeQueueEntry: TimelineEntry {
    let date: Date
    let configured: Bool
    let feed: MergeQueueFeed
    /// Set only when NOTHING usable came back.
    let failure: WidgetDataFailure?
    /// Set when real rows came from the snapshot because this read failed.
    let partialReason: WidgetDataFailure?
    /// Minutes since the stored rows were read; nil when this pass is live.
    let ageMinutes: Int?
    let refreshMinutes: Int
    let paused: Bool

    static let unconfigured = MergeQueueEntry(
        date: .now, configured: false, feed: .empty, failure: nil,
        partialReason: nil, ageMinutes: nil,
        refreshMinutes: MergeQueueTimelineProvider.defaultRefreshMinutes,
        paused: false)

    static func paused(refreshMinutes: Int) -> MergeQueueEntry {
        MergeQueueEntry(date: .now, configured: true, feed: .empty, failure: nil,
                        partialReason: nil, ageMinutes: nil,
                        refreshMinutes: refreshMinutes, paused: true)
    }

    // MARK: Gate copy

    var gateLine: String {
        if !configured { return "Set up the app to see the merge queue" }
        return "Paused in Settings"
    }

    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        return "Paused"
    }

    // MARK: Text the families draw

    var rows: [MergeQueueRow] { feed.rows }
    var queued: Int { feed.queued }
    var isEmpty: Bool { failure == nil && feed.rows.isEmpty }

    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        return feed.inlineLine
    }

    /// The freshness line every family can afford. Empty is still a real state:
    /// it says when the answer was last read.
    var updatedLine: String {
        if !configured { return "Set up the app to see the merge queue" }
        if paused { return "No data while paused" }
        if let ageMinutes {
            let reason = partialReason?.shortLabel ?? "kept last known"
            return "updated \(ageMinutes)m ago · \(reason)"
        }
        if let failure { return failure.operatorLine }
        if feed.isEmpty { return "updated just now · nothing queued" }
        return "updated just now"
    }

    var partialLine: String? {
        partialReason.map { "stale · \($0.shortLabel)" }
    }

    static func rowLimit(for family: WidgetFamily) -> Int {
        switch family {
        case .systemLarge, .systemExtraLarge: return MergeQueueReading.largeRowLimit
        case .systemMedium: return MergeQueueReading.mediumRowLimit
        default: return 1
        }
    }

    static var placeholder: MergeQueueEntry {
        let rows = [
            MergeQueueRow(lite: MergeQueueRowLite(
                project: "alpha", position: 1, pr: 101, card: "t_example1",
                branch: "wt/t_example1", state: "queued", ageS: 90,
                lastResult: nil, detail: nil, isLast: false)),
            MergeQueueRow(lite: MergeQueueRowLite(
                project: "alpha", position: nil, pr: 102, card: "t_example2",
                branch: "wt/t_example2", state: "gating", ageS: 40,
                lastResult: nil, detail: nil, isLast: false)),
            MergeQueueRow(lite: MergeQueueRowLite(
                project: "beta", position: 1, pr: 201, card: "t_example3",
                branch: "wt/t_example3", state: "queued", ageS: 300,
                lastResult: nil, detail: nil, isLast: false)),
            MergeQueueRow(lite: MergeQueueRowLite(
                project: "alpha", position: nil, pr: 99, card: "t_example0",
                branch: "wt/t_example0", state: "merged", ageS: 7200,
                lastResult: "merged", detail: nil, isLast: true)),
        ]
        return MergeQueueEntry(
            date: .now, configured: true,
            feed: MergeQueueFeed(rows: rows, count: rows.count, queued: 2,
                                 generatedAt: nil),
            failure: nil, partialReason: nil, ageMinutes: nil,
            refreshMinutes: MergeQueueTimelineProvider.defaultRefreshMinutes,
            paused: false)
    }
}

// MARK: - Timeline provider

struct MergeQueueTimelineProvider: TimelineProvider {
    static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: merge_queue_cadence
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: merge_queue_cadence

    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetMergeQueue)
    }

    func placeholder(in context: Context) -> MergeQueueEntry { .placeholder }

    func getSnapshot(in context: Context, completion: @escaping (MergeQueueEntry) -> Void) {
        let surface = Self.surface()
        Task { completion(await fetchEntry(surface: surface)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<MergeQueueEntry>) -> Void) {
        Task {
            let surface = Self.surface()
            let entry = await fetchEntry(surface: surface)
            guard let next = Self.nextRefreshDate(
                refreshMinutes: surface?.refreshMinutes ?? Self.defaultRefreshMinutes
            ) else {
                completion(Timeline(entries: [entry], policy: .atEnd))
                return
            }
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    // MARK: Fetch

    private func fetchEntry(surface: WidgetSurfaceSettings?) async -> MergeQueueEntry {
        // Paused is decided before the client exists: zero requests, zero rows.
        if let surface, !surface.isActive {
            return .paused(refreshMinutes: surface.refreshMinutes)
        }
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else { return .unconfigured }

        let outcome = await WidgetData.read(
            using: client,
            lastKnown: MergeQueueSnapshotStore.lastKnown
        ) { c in
            switch await c.mergeQueue() {
            case .success(let payload):
                return .success(MergeQueueFeed.from(payload))
            case .failure(let failure):
                return .failure(failure)
            }
        }

        switch outcome {
        case .live(let feed, let fetchedAt):
            MergeQueueSnapshotStore.save(feed, at: fetchedAt)
            return MergeQueueEntry(date: fetchedAt, configured: true, feed: feed,
                                   failure: nil, partialReason: nil, ageMinutes: nil,
                                   refreshMinutes: surface?.refreshMinutes
                                       ?? Self.defaultRefreshMinutes,
                                   paused: false)
        case .stale(let feed, let age, let reason):
            return MergeQueueEntry(date: .now, configured: true, feed: feed,
                                   failure: nil, partialReason: reason,
                                   ageMinutes: age,
                                   refreshMinutes: surface?.refreshMinutes
                                       ?? Self.defaultRefreshMinutes,
                                   paused: false)
        case .failed(let failure):
            return MergeQueueEntry(date: .now, configured: true, feed: .empty,
                                   failure: failure, partialReason: nil,
                                   ageMinutes: nil,
                                   refreshMinutes: surface?.refreshMinutes
                                       ?? Self.defaultRefreshMinutes,
                                   paused: false)
        }
    }
}

// MARK: - Widget

struct MergeQueueWidget: Widget {
    let kind = "MergeQueueWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetMergeQueue)
        return StaticConfiguration(kind: kind, provider: MergeQueueTimelineProvider()) { entry in
            MergeQueueWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Merge Queue")
        .description("The local merge queue: FIFO rows, live stages and last results. Read-only.")
        #if os(iOS)
        .supportedFamilies([.accessoryInline, .accessoryCircular, .accessoryRectangular,
                            .systemSmall, .systemMedium, .systemLarge])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
        #endif
    }
}

struct MergeQueueWidgetView: View {
    let entry: MergeQueueEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            if !entry.configured || entry.paused {
                Text(entry.shortGateLabel)
            } else {
                Text(entry.inlineLine)
            }
        case .accessoryCircular:
            MergeQueueCircular(entry: entry)
        case .accessoryRectangular:
            MergeQueueRectangular(entry: entry)
        #endif
        case .systemLarge, .systemExtraLarge:
            LargeMergeQueue(entry: entry)
        case .systemMedium:
            MediumMergeQueue(entry: entry)
        default:
            SmallMergeQueue(entry: entry)
        }
    }
}

// MARK: - Shared panes

private struct MergeQueueGateView: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry
    var systemTinted = false

    private var primaryText: Color { systemTinted ? .primary : style.primaryTextColor }
    private var secondaryText: Color { systemTinted ? .secondary : style.secondaryTextColor }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
                .font(style.font(.subheadline))
                .foregroundColor(secondaryText)
            Text(entry.gateLine)
                .font(style.font(.caption2, weight: .semibold))
                .foregroundColor(primaryText)
            Text(entry.paused
                 ? "Turn it back on under Widgets & Live Activities in the HSCC app."
                 : WidgetDataFailure.notConfigured.operatorLine)
                .font(style.font(.caption2))
                .foregroundColor(secondaryText)
                .lineLimit(3)
        }
    }
}

private struct MergeQueueErrorPane: View {
    @Environment(\.widgetStyle) private var style
    let failure: WidgetDataFailure
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                    .font(style.font(.subheadline))
                    .foregroundColor(style.badColor)
                Text("merge queue failed")
                    .font(style.font(.caption, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
            }
            Text(detailed ? failure.operatorLine : failure.shortLabel)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(detailed ? 4 : 2)
            Text("merge-queue state unknown")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

private struct MergeQueueHeader: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            if style.showIcons {
                Image(systemName: "arrow.triangle.merge").widgetIcon(style)
                    .font(style.font(size: 10))
                    .foregroundColor(style.accentColor)
            }
            Text("Merge queue")
                .font(style.font(.caption, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            Spacer(minLength: 0)
            Text(entry.updatedLine)
                .font(style.font(size: 8))
                .foregroundColor(entry.ageMinutes == nil ? style.secondaryTextColor : style.warnColor)
                .lineLimit(1)
        }
        .widgetHeader(style)
    }
}

private struct MergeQueueEmptyView: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Text("Nothing queued")
                .font(style.font(.caption, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            Text(entry.updatedLine)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(2)
        }
    }
}

/// The CLI's table. The header and every cell come from
/// `MergeQueueTableReading`; this view only chooses ink and layout. A project
/// caption appears wherever the route's project changes, mirroring the CLI's
/// one-table-per-project titles.
private struct MergeQueueTableView: View {
    @Environment(\.widgetStyle) private var style
    let rows: [MergeQueueRow]
    let limit: Int

    private var visible: [MergeQueueRow] {
        MergeQueueTableReading.visibleRows(rows, limit: limit)
    }

    private var hidden: Int {
        MergeQueueTableReading.hiddenRowCount(rows, limit: limit)
    }

    private func stateColor(_ state: String) -> Color {
        switch state {
        case "merged": return style.okColor
        case "resolve", "failed": return style.badColor
        case "rebasing", "gating", "merging": return style.warnColor
        default: return style.secondaryTextColor
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Grid(alignment: .leading,
                 horizontalSpacing: style.spacing(2),
                 verticalSpacing: 1) {
                GridRow {
                    ForEach(MergeQueueTableReading.columns, id: \.self) { title in
                        Text(title)
                            .font(style.font(size: 6.5, weight: .semibold))
                            .foregroundColor(style.secondaryTextColor)
                            .lineLimit(1)
                    }
                }
                ForEach(Array(visible.enumerated()), id: \.element.id) { index, row in
                    if index == 0 || visible[index - 1].project != row.project {
                        GridRow {
                            Text(row.project)
                                .font(style.font(size: 6.5, weight: .semibold))
                                .foregroundColor(style.accentColor)
                                .lineLimit(1)
                                .gridCellColumns(MergeQueueTableReading.columns.count)
                        }
                    }
                    GridRow {
                        ForEach(Array(row.cells.enumerated()), id: \.offset) { column, cell in
                            Text(cell)
                                .font(style.font(size: 7.5))
                                .foregroundColor(column == 4
                                                 ? stateColor(row.state)
                                                 : style.primaryTextColor)
                                .lineLimit(1)
                                .minimumScaleFactor(0.6)
                        }
                    }
                }
            }
            if hidden > 0 {
                Text("+\(hidden) more")
                    .font(style.font(size: 8))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
    }
}

/// `queued` plus the live stage, the pair every non-compact family leads with.
private struct MergeQueueCounts: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Text("\(entry.queued)")
                .font(style.font(.title2, weight: .bold))
                .foregroundColor(entry.feed.current == nil
                                 ? style.primaryTextColor : style.warnColor)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text("queued")
                .font(style.font(.caption2, weight: .semibold))
                .foregroundColor(style.secondaryTextColor)
            Spacer(minLength: 0)
            Text(entry.feed.currentLine)
                .font(style.font(.caption2))
                .foregroundColor(entry.feed.current == nil
                                 ? style.secondaryTextColor : style.warnColor)
                .lineLimit(1)
        }
    }
}

// MARK: - Accessory families

struct MergeQueueCircular: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else if let failure = entry.failure {
            Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
            Text(failure.shortLabel)
        } else {
            VStack(spacing: 0) {
                Text("\(entry.queued)")
                    .font(style.font(.body))
                    .fontWeight(.semibold)
                    .foregroundStyle(.primary)
                Text("queued")
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct MergeQueueRectangular: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(style.font(.caption2, weight: .semibold))
                .foregroundStyle(.secondary)
        } else if let failure = entry.failure {
            VStack(alignment: .leading, spacing: 1) {
                Text("merge queue failed")
                    .font(style.font(.caption2, weight: .semibold))
                Text(failure.shortLabel)
                    .font(style.font(.caption2))
                Text(entry.updatedLine)
                    .font(style.font(.caption2))
                    .lineLimit(1)
            }
        } else if let current = entry.feed.current {
            VStack(alignment: .leading, spacing: 1) {
                Text("Merge queue")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text("\(entry.queued) queued · \(current.state) \(current.prLabel)")
                    .font(style.font(.caption2))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(lastMergedLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        } else if entry.feed.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                Text("Merge queue")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                Text("nothing queued")
                    .font(style.font(.caption2))
                    .foregroundStyle(.secondary)
                Text(entry.updatedLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text("Merge queue")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                Text("\(entry.queued) queued · \(entry.feed.currentLine)")
                    .font(style.font(.caption2))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(lastMergedLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }

    private var lastMergedLine: String {
        guard let last = entry.feed.lastMerged else { return "no merge yet" }
        return "last merged \(last.prLabel) · \(last.ageLabel)"
    }
}

// MARK: - Home Screen families

struct SmallMergeQueue: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                MergeQueueGateView(entry: entry)
            } else if let failure = entry.failure {
                MergeQueueErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    MergeQueueHeader(entry: entry)
                    if entry.feed.isEmpty {
                        MergeQueueEmptyView(entry: entry)
                        Spacer(minLength: 0)
                    } else {
                        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                            Text("\(entry.queued)")
                                .font(style.font(.largeTitle, weight: .bold))
                                .foregroundColor(entry.feed.current == nil
                                                 ? style.accentColor : style.warnColor)
                                .lineLimit(1)
                                .minimumScaleFactor(0.6)
                            Text("queued")
                                .font(style.font(.caption, weight: .semibold))
                                .foregroundColor(style.secondaryTextColor)
                        }
                        Text(entry.feed.currentLine)
                            .font(style.font(.caption2, weight: .medium))
                            .foregroundColor(entry.feed.current == nil
                                             ? style.secondaryTextColor : style.warnColor)
                            .lineLimit(1)
                        if let last = entry.feed.lastMerged {
                            Text("last merged \(last.prLabel) · \(last.ageLabel)")
                                .font(style.font(.caption2))
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        if let partial = entry.partialLine {
                            Text(partial)
                                .font(style.font(size: 8))
                                .foregroundColor(style.warnColor)
                                .lineLimit(1)
                        }
                    }
                }
            }
        }
    }
}

struct MediumMergeQueue: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                MergeQueueGateView(entry: entry)
            } else if let failure = entry.failure {
                MergeQueueErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                    MergeQueueHeader(entry: entry)
                    if entry.feed.isEmpty {
                        MergeQueueEmptyView(entry: entry)
                    } else {
                        MergeQueueCounts(entry: entry)
                        MergeQueueTableView(rows: entry.feed.rows,
                                            limit: MergeQueueReading.mediumRowLimit)
                    }
                    Spacer(minLength: 0)
                    if let partial = entry.partialLine {
                        Text(partial)
                            .font(style.font(size: 8))
                            .foregroundColor(style.warnColor)
                            .lineLimit(1)
                    }
                }
            }
        }
    }
}

struct LargeMergeQueue: View {
    @Environment(\.widgetStyle) private var style
    let entry: MergeQueueEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                MergeQueueGateView(entry: entry)
            } else if let failure = entry.failure {
                MergeQueueErrorPane(failure: failure, detailed: true)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    MergeQueueHeader(entry: entry)
                    if entry.feed.isEmpty {
                        MergeQueueEmptyView(entry: entry)
                    } else {
                        MergeQueueCounts(entry: entry)
                        MergeQueueTableView(rows: entry.feed.rows,
                                            limit: MergeQueueReading.largeRowLimit)
                        Text(lastMergedLine)
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 0)
                    if let partial = entry.partialLine {
                        Text(partial)
                            .font(style.font(size: 8))
                            .foregroundColor(style.warnColor)
                            .lineLimit(1)
                    }
                }
            }
        }
    }

    private var lastMergedLine: String {
        guard let last = entry.feed.lastMerged else {
            return "last merged — none yet"
        }
        return "last merged \(last.prLabel) · \(last.project) · \(last.ageLabel)"
    }
}
