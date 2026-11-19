import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// NeedsMeWidget.swift — the main orchestrator's alert feed on the Home Screen
// and Lock Screen. READ-ONLY: one GET through `WidgetDataClient`, no ack, no
// notification, and only the `NeedsMeItem` projection is persisted.
//
// WHAT IT SHOWS, AND WHERE EACH VALUE COMES FROM
//
// `GET /v1/alerts` is the same feed `hscc notify` writes and `hscc notify list`
// reads. `NeedsMeFeed.from` is the one projection (Sources/Shared/
// NeedsMeReading.swift): newest first like the CLI, unacknowledged rows only,
// body reduced to its first line. So:
//
//   count        <- NeedsMeFeed.count (unacknowledged rows)
//   newest title <- items[0].title
//   age          <- items[].ts through NeedsMeReading.ageLabel (2m / 3h / 4d)
//   level        <- items[].level, normalized info|action|critical
//   body line    <- items[].bodyFirstLine (the large family)
//
// COLOUR. Critical and action rows use the operator's accent (#FF5722 in the
// default style); info rows use the secondary text role. Lock Screen families
// are system-tinted, so they carry the same hierarchy as primary/secondary.
//
// FAILURE RULE. `WidgetData.read` owns live/stale/failed. A stale tile keeps
// its real rows and names the reason and its age; a failed tile names the
// reason instead of drawing a fabricated zero. Empty is its own honest state:
// "Nothing needs you" with the update age. Paused is decided before the client
// exists, so a paused surface sends zero requests.
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

    // MARK: Text the families draw

    var items: [NeedsMeItem] { feed.items }
    var count: Int { feed.count }
    var newest: NeedsMeItem? { feed.newest }
    var isEmpty: Bool { failure == nil && feed.items.isEmpty }

    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        return NeedsMeReading.inlineLine(count: count)
    }

    /// The freshness line every family can afford. Empty is still a real state:
    /// it says when the answer was last read.
    var updatedLine: String {
        if !configured { return "Set up the app to see what needs you" }
        if paused { return "No data while paused" }
        if let ageMinutes {
            let reason = partialReason?.shortLabel ?? "kept last known"
            return "updated \(ageMinutes)m ago · \(reason)"
        }
        if let failure { return failure.operatorLine }
        if feed.items.isEmpty {
            return "updated just now · \(NeedsMeReading.nothingNeedsYou.lowercased())"
        }
        return "updated just now"
    }

    var partialLine: String? {
        partialReason.map { "stale · \($0.shortLabel)" }
    }

    static func rowLimit(for family: WidgetFamily) -> Int {
        switch family {
        case .systemLarge, .systemExtraLarge: return NeedsMeReading.largeRowLimit
        case .systemMedium: return NeedsMeReading.mediumRowLimit
        default: return 1
        }
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
    static let defaultRefreshMinutes = 5

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
            switch await c.alerts(limit: 100) {
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
        let style = WidgetStyle.resolved(for: .widgetNeedsMe)
        return StaticConfiguration(kind: kind, provider: NeedsMeTimelineProvider()) { entry in
            NeedsMeWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Needs Me")
        .description("Alerts from the main orchestrator that still need you. Read-only.")
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
            } else {
                Text(entry.inlineLine)
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
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry
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

private struct NeedsMeErrorPane: View {
    @Environment(\.widgetStyle) private var style
    let failure: WidgetDataFailure
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                    .font(style.font(.subheadline))
                    .foregroundColor(style.badColor)
                Text("alert feed failed")
                    .font(style.font(.caption, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
            }
            Text(detailed ? failure.operatorLine : failure.shortLabel)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(detailed ? 4 : 2)
            Text("needs-you state unknown")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

private struct NeedsMeHeader: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            if style.showIcons {
                Image(systemName: "bell.badge").widgetIcon(style)
                    .font(style.font(size: 10))
                    .foregroundColor(style.accentColor)
            }
            Text("Needs me")
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

private struct NeedsMeEmptyView: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Text(NeedsMeReading.nothingNeedsYou)
                .font(style.font(.caption, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            Text(entry.updatedLine)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(2)
        }
    }
}

/// One alert row. The level is carried by its own label colour; the title stays
/// primary so a long title is readable even when the level is `info`.
private struct NeedsMeRowView: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry
    let item: NeedsMeItem
    var showBody = false

    private var levelColor: Color {
        item.level.isUrgent ? style.accentColor : style.secondaryTextColor
    }

    var body: some View {
        HStack(alignment: .top, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                    Text(item.level.label.uppercased())
                        .font(style.font(size: 8, weight: .semibold))
                        .foregroundColor(levelColor)
                    Text(item.title)
                        .font(style.font(.caption2, weight: .medium))
                        .foregroundColor(style.primaryTextColor)
                        .lineLimit(1)
                }
                if showBody, !item.bodyFirstLine.isEmpty {
                    Text(item.bodyFirstLine)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            Text(item.ageLabel(now: entry.date))
                .font(style.font(size: 8))
                .foregroundColor(levelColor)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.level.label) \(item.title), \(item.ageLabel(now: entry.date))")
    }
}

// MARK: - Accessory families

struct NeedsMeCircular: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

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
                Text("\(entry.count)")
                    .font(style.font(.body))
                    .fontWeight(.semibold)
                    .foregroundStyle(.primary)
                Text("need you")
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct NeedsMeRectangular: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(style.font(.caption2, weight: .semibold))
                .foregroundStyle(.secondary)
        } else if let failure = entry.failure {
            VStack(alignment: .leading, spacing: 1) {
                Text("alert feed failed")
                    .font(style.font(.caption2, weight: .semibold))
                Text(failure.shortLabel)
                    .font(style.font(.caption2))
                Text(entry.updatedLine)
                    .font(style.font(.caption2))
                    .lineLimit(1)
            }
        } else if let newest = entry.newest {
            VStack(alignment: .leading, spacing: 1) {
                Text(newest.level.isUrgent ? "Needs you now" : "Needs you")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(newest.title)
                    .font(style.font(.caption2))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text("\(newest.ageLabel(now: entry.date)) · \(NeedsMeReading.inlineLine(count: entry.count))")
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text(NeedsMeReading.nothingNeedsYou)
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                Text(entry.updatedLine)
                    .font(style.font(.caption2))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }
}

// MARK: - Home Screen families

struct SmallNeedsMe: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                NeedsMeGateView(entry: entry)
            } else if let failure = entry.failure {
                NeedsMeErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    NeedsMeHeader(entry: entry)
                    if let newest = entry.newest {
                        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                            Text("\(entry.count)")
                                .font(style.font(.largeTitle, weight: .bold))
                                .foregroundColor(style.accentColor)
                                .lineLimit(1)
                                .minimumScaleFactor(0.6)
                            Text("need you")
                                .font(style.font(.caption, weight: .semibold))
                                .foregroundColor(style.secondaryTextColor)
                        }
                        Text(newest.title)
                            .font(style.font(.caption2, weight: .medium))
                            .foregroundColor(newest.level.isUrgent
                                             ? style.accentColor : style.secondaryTextColor)
                            .lineLimit(2)
                        Text(newest.ageLabel(now: entry.date))
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                        Spacer(minLength: 0)
                        if let partial = entry.partialLine {
                            Text(partial)
                                .font(style.font(size: 8))
                                .foregroundColor(style.warnColor)
                                .lineLimit(1)
                        }
                    } else {
                        NeedsMeEmptyView(entry: entry)
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }
}

struct MediumNeedsMe: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                NeedsMeGateView(entry: entry)
            } else if let failure = entry.failure {
                NeedsMeErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                    NeedsMeHeader(entry: entry)
                    if entry.items.isEmpty {
                        NeedsMeEmptyView(entry: entry)
                    } else {
                        ForEach(entry.items.prefix(NeedsMeReading.mediumRowLimit)) { item in
                            NeedsMeRowView(entry: entry, item: item)
                        }
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

struct LargeNeedsMe: View {
    @Environment(\.widgetStyle) private var style
    let entry: NeedsMeEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                NeedsMeGateView(entry: entry)
            } else if let failure = entry.failure {
                NeedsMeErrorPane(failure: failure, detailed: true)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    NeedsMeHeader(entry: entry)
                    Text("\(entry.count) need you")
                        .font(style.font(.caption, weight: .semibold))
                        .foregroundColor(entry.count > 0 ? style.accentColor : style.secondaryTextColor)
                    if entry.items.isEmpty {
                        NeedsMeEmptyView(entry: entry)
                    } else {
                        ForEach(entry.items.prefix(NeedsMeReading.largeRowLimit)) { item in
                            NeedsMeRowView(entry: entry, item: item, showBody: true)
                        }
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
