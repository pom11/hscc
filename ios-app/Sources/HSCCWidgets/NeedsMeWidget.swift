import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// NeedsMeWidget.swift — "Needs Me" (N3 Calm when clear), the Native redesign.
//
// The surface is quiet until something needs the operator. Empty is only
// allowed to say "All clear" after a SUCCESSFUL fetch: a failed or stale read
// draws the offline state instead, because a blind read must never look like a
// calm queue. Open items lead with the count and the newest alert's title; the
// large family lists each alert with a severity bar, its level and its age.
//
// READ-ONLY. One GET /v1/alerts through WidgetDataClient; no ack, no
// notification. The projection (`NeedsMeReading.swift`) keeps unacknowledged
// rows newest first, as `hscc notify list` does.
// ---------------------------------------------------------------------------

// MARK: - Entry

struct NeedsMeEntry: TimelineEntry {
    let date: Date
    let configured: Bool
    let feed: NeedsMeFeed
    /// Set only when NOTHING usable came back.
    let failure: WidgetDataFailure?
    /// Set when real rows came from the snapshot because this read failed.
    let partialReason: WidgetDataFailure?
    /// Minutes since the stored rows were read; nil when this pass is live.
    let ageMinutes: Int?
    let refreshMinutes: Int
    let paused: Bool

    static let unconfigured = NeedsMeEntry(
        date: .now, configured: false, feed: .empty, failure: nil,
        partialReason: nil, ageMinutes: nil,
        refreshMinutes: NeedsMeTimelineProvider.defaultRefreshMinutes,
        paused: false)

    static func paused(refreshMinutes: Int) -> NeedsMeEntry {
        NeedsMeEntry(date: .now, configured: true, feed: .empty, failure: nil,
                     partialReason: nil, ageMinutes: nil,
                     refreshMinutes: refreshMinutes, paused: true)
    }

    // MARK: Gate copy

    var gateLine: String {
        if !configured { return "Set up the app to see what needs you" }
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
        if failure != nil { return nil }
        return date
    }

    /// A failed or stale read, whether or not cached rows are drawn. Nil here
    /// is the ONLY state in which "All clear" is true.
    var isOffline: Bool { failure != nil || partialReason != nil }

    var staleState: StaleState? {
        guard configured, !paused else { return nil }
        return StaleState(dataAt: dataAt, fetchedAt: Date(),
                          refreshMinutes: refreshMinutes,
                          failed: isOffline || ageMinutes != nil)
    }

    // MARK: The values the families draw

    var items: [NeedsMeItem] { feed.items }
    var count: Int { feed.count }
    var newest: NeedsMeItem? { feed.newest }

    /// The N3 contract: empty is a claim only a successful fetch may make.
    var isAllClear: Bool {
        failure == nil && partialReason == nil && feed.items.isEmpty
    }

    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if isOffline { return failure?.shortLabel ?? "offline" }
        return NeedsMeReading.inlineLine(count: count)
    }

    /// The freshness/offline line every family can afford.
    var updatedLine: String {
        if !configured { return "Set up the app to see what needs you" }
        if paused { return "No data while paused" }
        if isOffline || ageMinutes != nil {
            return "offline · \(NativeStyle.hhmm(dataAt))"
        }
        return isAllClear ? "Nothing waiting since \(NativeStyle.hhmm(dataAt))"
                          : "as of \(NativeStyle.hhmm(dataAt))"
    }

    static var placeholder: NeedsMeEntry {
        NeedsMeEntry(
            date: .now, configured: true,
            feed: NeedsMeFeed(items: [
                NeedsMeItem(id: 2, ts: "2026-11-19T12:28:00Z",
                            title: "Example alert title",
                            bodyFirstLine: "Example body line one.",
                            level: .critical),
                NeedsMeItem(id: 1, ts: "2026-11-19T12:00:00Z",
                            title: "Example info title",
                            bodyFirstLine: "Example body line one.",
                            level: .info),
            ], latestID: 2, hasMore: false),
            failure: nil, partialReason: nil, ageMinutes: nil,
            refreshMinutes: NeedsMeTimelineProvider.defaultRefreshMinutes,
            paused: false)
    }
}

// MARK: - Timeline provider

struct NeedsMeTimelineProvider: TimelineProvider {
    static let defaultRefreshMinutes = 30

    // GATE-SLICE-BEGIN: needs_me_cadence
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: needs_me_cadence

    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetNeedsMe)
    }

    func placeholder(in context: Context) -> NeedsMeEntry { .placeholder }

    func getSnapshot(in context: Context, completion: @escaping (NeedsMeEntry) -> Void) {
        let surface = Self.surface()
        Task { completion(await fetchEntry(surface: surface)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<NeedsMeEntry>) -> Void) {
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

    private func fetchEntry(surface: WidgetSurfaceSettings?) async -> NeedsMeEntry {
        // Paused is decided before the client exists: zero requests, zero rows.
        if let surface, !surface.isActive {
            return .paused(refreshMinutes: surface.refreshMinutes)
        }
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else { return .unconfigured }

        let outcome = await WidgetData.read(
            using: client,
            lastKnown: NeedsMeSnapshotStore.lastKnown
        ) { c in
            switch await c.allAlerts() {
            case .success(let payload):
                return .success(NeedsMeFeed.from(payload))
            case .failure(let failure):
                return .failure(failure)
            }
        }

        switch outcome {
        case .live(let feed, let fetchedAt):
            NeedsMeSnapshotStore.save(feed, at: fetchedAt)
            return NeedsMeEntry(date: fetchedAt, configured: true, feed: feed,
                                failure: nil, partialReason: nil, ageMinutes: nil,
                                refreshMinutes: surface?.refreshMinutes
                                    ?? Self.defaultRefreshMinutes,
                                paused: false)
        case .stale(let feed, let age, let reason):
            return NeedsMeEntry(date: .now, configured: true, feed: feed,
                                failure: nil, partialReason: reason,
                                ageMinutes: age,
                                refreshMinutes: surface?.refreshMinutes
                                    ?? Self.defaultRefreshMinutes,
                                paused: false)
        case .failed(let failure):
            return NeedsMeEntry(date: .now, configured: true, feed: .empty,
                                failure: failure, partialReason: nil,
                                ageMinutes: nil,
                                refreshMinutes: surface?.refreshMinutes
                                    ?? Self.defaultRefreshMinutes,
                                paused: false)
        }
    }
}

// MARK: - Widget

struct NeedsMeWidget: Widget {
    let kind = "NeedsMeWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: NeedsMeTimelineProvider()) { entry in
            NeedsMeWidgetView(entry: entry)
                .containerBackground(NativeStyle.surface, for: .widget)
        }
        .configurationDisplayName("Needs Me")
        .description("Alerts from the main orchestrator that still need you, and a calm all-clear when none do. Read-only.")
        #if os(iOS)
        .supportedFamilies([.accessoryInline, .accessoryCircular, .accessoryRectangular,
                            .systemSmall, .systemMedium, .systemLarge])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
        #endif
    }
}

struct NeedsMeWidgetView: View {
    let entry: NeedsMeEntry
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
            NeedsMeCircular(entry: entry)
        case .accessoryRectangular:
            NeedsMeRectangular(entry: entry)
        #endif
        case .systemLarge, .systemExtraLarge:
            LargeNeedsMe(entry: entry)
        case .systemMedium:
            MediumNeedsMe(entry: entry)
        default:
            SmallNeedsMe(entry: entry)
        }
    }
}

// MARK: - Shared panes

private struct NeedsMeGateView: View {
    let entry: NeedsMeEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Needs Me")
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

private struct NeedsMeOfflineView: View {
    let entry: NeedsMeEntry
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(NativeStyle.bad)
                Text(entry.failure?.shortLabel ?? "Offline")
                    .font(NativeStyle.label())
                    .foregroundStyle(.primary)
            }
            Text(entry.failure?.operatorLine ?? "The alert feed did not answer.")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(detailed ? 3 : 2)
            Text("needs-you state unknown")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct NeedsMeAllClearView: View {
    let entry: NeedsMeEntry
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Needs you")
                .font(NativeStyle.label())
                .foregroundStyle(.secondary)
            Text("All clear")
                .font(NativeStyle.hero(compact ? 26 : 34))
                .foregroundStyle(NativeStyle.ok)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text("Nothing waiting since \(NativeStyle.hhmm(entry.dataAt))")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(2)
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct NeedsMeRowView: View {
    let entry: NeedsMeEntry
    let item: NeedsMeItem
    var showBody = false

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(Self.color(item.level))
                .frame(width: 3)
                .frame(maxHeight: .infinity)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(NativeStyle.small())
                    .fontWeight(.semibold)
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                if showBody, !item.bodyFirstLine.isEmpty {
                    Text(item.bodyFirstLine)
                        .font(NativeStyle.small())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Text("\(item.ageLabel(now: entry.date)) ago · \(item.level.label.lowercased())")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.level.label) \(item.title), \(item.ageLabel(now: entry.date)) old")
    }

    static func color(_ level: NeedsMeLevel) -> Color {
        switch level {
        case .critical: return NativeStyle.bad
        case .action: return NativeStyle.warn
        case .info: return .secondary
        }
    }
}

// MARK: - Accessory families

struct NeedsMeCircular: View {
    let entry: NeedsMeEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else if entry.isOffline && entry.items.isEmpty {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.body)
                .foregroundStyle(.primary)
        } else if entry.count == 0 {
            Gauge(value: 0.0, in: 0...1) {
                Text("Need you")
            } currentValueLabel: {
                Image(systemName: "checkmark")
                    .font(NativeStyle.number(20, weight: .bold))
                    .foregroundStyle(NativeStyle.ok)
            }
            .gaugeStyle(.accessoryCircular)
            .tint(NativeStyle.ok)
            .widgetAccentable()
            .stale(entry.staleState)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("All clear, nothing needs you")
        } else {
            L2Gauge(value: Double(min(entry.count, 99)),
                    range: 0...Double(max(entry.count, 1)),
                    tint: NativeStyle.bad,
                    valueText: "\(entry.count)",
                    caption: "need you",
                    label: "\(entry.count) need you")
                .stale(entry.staleState)
        }
    }
}

struct NeedsMeRectangular: View {
    let entry: NeedsMeEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
        } else if entry.isOffline && entry.items.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                Text("Offline")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text("needs-you state unknown")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else if entry.count == 0 {
            VStack(alignment: .leading, spacing: 1) {
                Text("All clear")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text("nothing needs you")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.count == 1 ? "1 needs you" : "\(entry.count) need you")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                    .widgetAccentable()
                if let newest = entry.newest {
                    Text(newest.title)
                        .font(NativeStyle.small())
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            .stale(entry.staleState)
        }
    }
}

// MARK: - Home Screen families

struct SmallNeedsMe: View {
    let entry: NeedsMeEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                NeedsMeGateView(entry: entry)
            } else if entry.isOffline && entry.items.isEmpty {
                NeedsMeOfflineView(entry: entry)
            } else if entry.isAllClear {
                NeedsMeAllClearView(entry: entry, compact: true)
            } else if let newest = entry.newest {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Needs you")
                        .font(NativeStyle.label())
                        .foregroundStyle(.secondary)
                    Text("\(entry.count)")
                        .font(NativeStyle.hero())
                        .foregroundStyle(NativeStyle.bad)
                        .lineLimit(1)
                    Text(newest.title)
                        .font(NativeStyle.small())
                        .fontWeight(.semibold)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                    Text("\(newest.ageLabel(now: entry.date)) ago")
                        .font(NativeStyle.small())
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    AsOfStamp(date: entry.dataAt)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            }
        }
    }
}

struct MediumNeedsMe: View {
    let entry: NeedsMeEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                NeedsMeGateView(entry: entry)
            } else if entry.isOffline && entry.items.isEmpty {
                NeedsMeOfflineView(entry: entry)
            } else if entry.isAllClear {
                NeedsMeAllClearView(entry: entry)
            } else if let newest = entry.newest {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Needs you now")
                            .font(NativeStyle.label())
                            .foregroundStyle(NativeStyle.bad)
                        Text(newest.title)
                            .font(NativeStyle.sub())
                            .foregroundStyle(.primary)
                            .lineLimit(3)
                        Spacer(minLength: 0)
                        Text("\(newest.ageLabel(now: entry.date)) ago · \(newest.level.label.lowercased())")
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(NativeStyle.bad.opacity(0.14),
                                in: RoundedRectangle(cornerRadius: 12, style: .continuous))

                    VStack(spacing: 2) {
                        Text("\(entry.count)")
                            .font(NativeStyle.hero(34))
                            .foregroundStyle(NativeStyle.bad)
                            .lineLimit(1)
                        Text("open")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                    }
                    .frame(width: 74)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            }
        }
    }
}

struct LargeNeedsMe: View {
    let entry: NeedsMeEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                NeedsMeGateView(entry: entry)
            } else if entry.isOffline && entry.items.isEmpty {
                NeedsMeOfflineView(entry: entry, detailed: true)
            } else if entry.isAllClear {
                NeedsMeAllClearView(entry: entry)
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(entry.count == 1 ? "Needs you · 1" : "Needs you · \(entry.count)")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                        AsOfStamp(date: entry.dataAt)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(entry.items.prefix(NeedsMeReading.largeRowLimit)) { item in
                            NeedsMeRowView(entry: entry, item: item, showBody: true)
                        }
                    }
                    Spacer(minLength: 0)
                    AsOfStamp(date: entry.dataAt, prefix: "as of")
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            }
        }
    }
}
