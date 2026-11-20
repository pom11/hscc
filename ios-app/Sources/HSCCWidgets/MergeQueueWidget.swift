import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// MergeQueueWidget.swift — "Merge Queue" (M1 Pipeline), the Native redesign.
//
// One question: where is the PR in front right now? The widget draws the head
// PR, a four-stage track (rebase -> gates -> push -> merge), how many wait
// behind it, and the last result. Large adds the FIFO list and the last five
// outcomes. When every project's queue is disabled the surface says
// "Merge queue off for <project>" instead of drawing that as an empty queue —
// the route's `projects` map is the only place that fact lives.
//
// READ-ONLY. One GET /v1/merge-queue through WidgetDataClient; no queue
// steering, no git/gh. Rows and stages come from `MergeQueueReading.swift`,
// the same projection the CLI table and the desktop tile use.
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

    // MARK: The data's own clock

    var dataAt: Date? {
        if let ageMinutes {
            return Date().addingTimeInterval(-Double(ageMinutes) * 60)
        }
        if let generated = Self.parseStamp(feed.generatedAt) { return generated }
        if failure != nil || feed.isUnavailable { return nil }
        return date
    }

    static func parseStamp(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: raw) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: raw)
    }

    var isOffline: Bool {
        failure != nil || partialReason != nil || feed.isUnavailable
    }

    /// Draw the failure panel when there is no usable data. A stale snapshot
    /// WITH rows keeps its rows and the stale overlay; an `ok:false` answer is
    /// a failure, never an empty queue.
    var showsFailure: Bool {
        failure != nil || feed.isUnavailable
    }

    var staleState: StaleState? {
        guard configured, !paused else { return nil }
        return StaleState(dataAt: dataAt, fetchedAt: Date(),
                          refreshMinutes: refreshMinutes,
                          failed: isOffline || ageMinutes != nil)
    }

    // MARK: The values the families draw

    var rows: [MergeQueueRow] { feed.rows }
    var isOff: Bool { feed.isOff && failure == nil && !feed.isUnavailable }
    var offProject: String? { feed.offProject }
    var head: MergeQueueRow? { feed.headRow }

    var projectLabel: String {
        head?.project ?? offProject ?? "—"
    }

    var stageProgress: Int {
        MergeQueueReading.stageProgress(head?.state ?? "queued")
    }

    var waitingText: String {
        let waiting = feed.waitingCount
        return waiting == 1 ? "1 waiting" : "\(waiting) waiting"
    }

    var lastResultText: String {
        guard let last = feed.lastTerminal else { return "no result yet" }
        return "\(last.prLabel) \(MergeQueueReading.resultMark(last.state))"
    }

    var lastMergedText: String {
        guard let last = feed.lastMerged else { return "no merge yet" }
        return "\(last.prLabel) merged \(last.ageLabel) ago"
    }

    /// `accessoryInline`: the head PR and its stage, or the off/empty state.
    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        if feed.isUnavailable { return "merge queue offline" }
        if isOff { return "merge queue off for \(offProject ?? "project")" }
        guard let head else { return "merge queue empty" }
        return "⇢ \(head.prLabel) \(MergeQueueReading.stageName(head.state)) · \(feed.waitingCount) waiting"
    }

    /// The freshness line every family can afford.
    var updatedLine: String {
        if !configured { return "Set up the app to see the merge queue" }
        if paused { return "No data while paused" }
        if isOffline || ageMinutes != nil {
            return "offline · \(NativeStyle.hhmm(dataAt))"
        }
        return isOff ? "off" : "as of \(NativeStyle.hhmm(dataAt))"
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
        StaticConfiguration(kind: kind, provider: MergeQueueTimelineProvider()) { entry in
            MergeQueueWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Merge Queue")
        .description("The PR at the front of the merge queue, its stage, and the results behind it. Read-only.")
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
                    .font(NativeStyle.small())
            } else {
                Text(entry.inlineLine)
                    .font(NativeStyle.small())
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
    let entry: MergeQueueEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Merge queue")
                .font(NativeStyle.label())
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            Text(entry.shortGateLabel)
                .font(NativeStyle.hero(24))
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .minimumScaleFactor(0.7)
            Text(entry.gateLine)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Spacer(minLength: 0)
            AsOfStamp(date: nil)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct MergeQueueFailureView: View {
    let entry: MergeQueueEntry
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(NativeStyle.bad)
                Text(entry.failure?.shortLabel ?? "No data")
                    .font(NativeStyle.label())
                    .foregroundStyle(.primary)
            }
            Text(entry.failure?.operatorLine ?? "The merge queue did not answer.")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(detailed ? 3 : 2)
            Text("merge-queue state unknown")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct MergeQueueOffView: View {
    let entry: MergeQueueEntry
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Merge queue")
                .font(NativeStyle.label())
                .foregroundStyle(.secondary)
            Text("Merge queue off for \(entry.offProject ?? "project")")
                .font(NativeStyle.hero(detailed ? 26 : 20))
                .foregroundStyle(.primary)
                .lineLimit(2)
                .minimumScaleFactor(0.7)
            Text("Turn it on for this project in the flightdeck registry.")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct MergeQueueEmptyView: View {
    let entry: MergeQueueEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Nothing queued")
                .font(NativeStyle.hero(24))
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Text("No PR is waiting to land.")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

/// The four-stage M1 track. Completed stages are green, the current stage is
/// the accent, the rest stay on the system track.
private struct PipelineTrack: View {
    let progress: Int
    var showLabels = false

    var body: some View {
        HStack(alignment: .top, spacing: 4) {
            ForEach(Array(MergeQueueReading.stageLabels.enumerated()), id: \.offset) { index, label in
                VStack(alignment: .leading, spacing: 3) {
                    Capsule()
                        .fill(color(index))
                        .frame(height: 6)
                    if showLabels {
                        Text(label)
                            .font(NativeStyle.small())
                            .foregroundStyle(index == progress ? .primary : .secondary)
                            .lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("stage \(progress) of 4")
    }

    private func color(_ index: Int) -> Color {
        if index < progress { return NativeStyle.ok }
        if index == progress { return NativeStyle.accent }
        return Color.secondary.opacity(0.2)
    }
}

// MARK: - Accessory families

struct MergeQueueCircular: View {
    let entry: MergeQueueEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else if entry.isOffline && entry.rows.isEmpty {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.body)
                .foregroundStyle(.primary)
        } else if entry.isOff {
            VStack(spacing: 0) {
                Text("off")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text("queue")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else {
            VStack(spacing: 0) {
                Text("\(entry.feed.queued)")
                    .font(NativeStyle.hero(22))
                    .foregroundStyle(.primary)
                Text("queued")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct MergeQueueRectangular: View {
    let entry: MergeQueueEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
        } else if entry.isOffline && entry.rows.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                Text("Offline")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text("merge-queue state unknown")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else if entry.isOff {
            VStack(alignment: .leading, spacing: 1) {
                Text("Merge queue off")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text(entry.offProject ?? "project")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else if let head = entry.head {
            VStack(alignment: .leading, spacing: 1) {
                Text("⇢ \(head.prLabel) \(MergeQueueReading.stageName(head.state)) · \(entry.feed.waitingCount) waiting")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                    .widgetAccentable()
                Text(head.headline(max: 30))
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .stale(entry.staleState)
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text("Merge queue empty")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text("nothing queued")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Home Screen families

struct SmallMergeQueue: View {
    let entry: MergeQueueEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                MergeQueueGateView(entry: entry)
            } else if entry.showsFailure {
                MergeQueueFailureView(entry: entry)
            } else if entry.isOff {
                MergeQueueOffView(entry: entry)
            } else if let head = entry.head {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Merge queue")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Text(head.project)
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Text(head.prLabel)
                        .font(NativeStyle.hero(26))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                    PipelineTrack(progress: entry.stageProgress)
                    Spacer(minLength: 0)
                    HStack(alignment: .firstTextBaseline) {
                        Text(entry.waitingText)
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Text(entry.lastResultText)
                            .font(NativeStyle.number())
                            .foregroundStyle(.secondary)
                    }
                    AsOfStamp(date: entry.dataAt)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            } else {
                MergeQueueEmptyView(entry: entry)
            }
        }
    }
}

struct MediumMergeQueue: View {
    let entry: MergeQueueEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                MergeQueueGateView(entry: entry)
            } else if entry.showsFailure {
                MergeQueueFailureView(entry: entry)
            } else if entry.isOff {
                MergeQueueOffView(entry: entry)
            } else if let head = entry.head {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Merge queue · \(head.project)")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        AsOfStamp(date: entry.dataAt)
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(head.prLabel)
                            .font(NativeStyle.number(22, weight: .bold))
                            .foregroundStyle(.primary)
                        Text(head.headline(max: 52))
                            .font(NativeStyle.sub())
                            .foregroundStyle(.primary)
                            .lineLimit(2)
                        Spacer(minLength: 0)
                    }
                    PipelineTrack(progress: entry.stageProgress, showLabels: true)
                    Spacer(minLength: 0)
                    HStack(alignment: .firstTextBaseline) {
                        Text(entry.waitingText)
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        Text(entry.lastMergedText)
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            } else {
                MergeQueueEmptyView(entry: entry)
            }
        }
    }
}

struct LargeMergeQueue: View {
    let entry: MergeQueueEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                MergeQueueGateView(entry: entry)
            } else if entry.showsFailure {
                MergeQueueFailureView(entry: entry, detailed: true)
            } else if entry.isOff {
                MergeQueueOffView(entry: entry, detailed: true)
            } else if let head = entry.head {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Merge queue · \(head.project)")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        AsOfStamp(date: entry.dataAt)
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(head.prLabel)
                            .font(NativeStyle.number(22, weight: .bold))
                            .foregroundStyle(.primary)
                        Text(head.headline(max: 60))
                            .font(NativeStyle.sub())
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        Text(entry.waitingText)
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                    }
                    PipelineTrack(progress: entry.stageProgress, showLabels: true)
                    Divider()
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(Array(entry.rows.prefix(6).enumerated()), id: \.element.id) { index, row in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text("\(index + 1)")
                                    .font(NativeStyle.number())
                                    .foregroundStyle(.secondary)
                                    .frame(width: 14, alignment: .trailing)
                                Text(row.prLabel)
                                    .font(NativeStyle.number())
                                    .foregroundStyle(.primary)
                                    .frame(width: 44, alignment: .leading)
                                Text(row.headline(max: 46))
                                    .font(NativeStyle.small())
                                    .foregroundStyle(.primary)
                                    .lineLimit(1)
                                Spacer(minLength: 0)
                                Text(MergeQueueReading.stageName(row.state))
                                    .font(NativeStyle.small())
                                    .foregroundStyle(row.isInFlight ? NativeStyle.accent : .secondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                    Spacer(minLength: 0)
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("last:")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                        ForEach(Array(entry.feed.recentResults.enumerated()), id: \.element.id) { _, row in
                            Text("\(row.prLabel) \(MergeQueueReading.resultMark(row.state))")
                                .font(NativeStyle.number())
                                .foregroundStyle(row.isMerged ? NativeStyle.ok : NativeStyle.bad)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        AsOfStamp(date: entry.dataAt)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            } else {
                MergeQueueEmptyView(entry: entry)
            }
        }
    }
}
