import Foundation
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// CardsRunningWidget.swift — "Running" (R3 Longest), the Native redesign.
//
// The widget answers one question: is any card taking too long? The longest
// running card leads with its elapsed clock in the kit's rounded hero type,
// its title, and the worker heartbeat. The rest of the fleet is supporting
// detail — a count on medium, a row list on large. Empty is its own calm state
// ("Nothing running"), never a fabricated zero.
//
// DATA. The projection still comes from `GET /v1/kanban/running` plus the
// blocked read (`Sources/Shared/CardsRunningProjection.swift`), shared with the
// Live Activity. This file adds no route. `hb_s` is projected into the stored
// row's optional `heartbeatSeconds`; a snapshot written before that field
// existed decodes with nil and draws `beat —`, never a fresh-looking beat.
//
// LOOK. The 2026-11-20 Native kit: system background, `.primary`/`.secondary`
// ink, colour only for state (green heartbeat under five minutes, orange after
// that). Numbers use `NativeStyle` and the data's own clock is shown through
// `AsOfStamp`; `StaleState` dims cached data and names it stale/offline.
//
// GATES. Setup and pause stay the first step in every family; pause is decided
// before the client exists, so a paused surface spends zero requests.
// ---------------------------------------------------------------------------

// MARK: - The timeline entry

struct CardsRunningEntry: TimelineEntry {
    let date: Date
    /// When the data on screen was read. Equal to `date` for a live entry; the
    /// snapshot's timestamp for stale data.
    let updatedAt: Date
    /// nil = nothing usable came back; `failure` then says why, and no family
    /// may draw a count.
    let projection: CardsRunningProjection?
    /// Set only for stale-while-error: minutes since the stored rows were read.
    let ageMinutes: Int?
    /// Why there are no live numbers. Rendered, never swallowed.
    let failure: WidgetDataFailure?
    /// False only when the operator has no host/token yet — the setup invite.
    let configured: Bool
    /// True when the operator paused THIS surface in Settings.
    let paused: Bool
    /// The operator's cadence, so staleness is measured against the surface's
    /// own refresh interval rather than a literal.
    let refreshMinutes: Int

    static let sample = CardsRunningEntry(
        date: .now, updatedAt: .now, projection: .preview,
        ageMinutes: nil, failure: nil, configured: true, paused: false,
        refreshMinutes: 10)

    static func unconfigured() -> CardsRunningEntry {
        CardsRunningEntry(date: .now, updatedAt: .now, projection: nil,
                          ageMinutes: nil, failure: .notConfigured,
                          configured: false, paused: false, refreshMinutes: 10)
    }

    /// The paused state: nothing but the fact that it is paused. No projection,
    /// no snapshot, no failure, and no request spent.
    static func paused() -> CardsRunningEntry {
        CardsRunningEntry(date: .now, updatedAt: .now, projection: nil,
                          ageMinutes: nil, failure: nil,
                          configured: true, paused: true, refreshMinutes: 10)
    }

    // MARK: Gate copy

    var gateLine: String {
        if !configured { return "Set up the app to see running cards." }
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
        return updatedAt
    }

    var staleState: StaleState? {
        guard configured, !paused else { return nil }
        return StaleState(dataAt: dataAt, fetchedAt: Date(),
                          refreshMinutes: refreshMinutes,
                          failed: failure != nil || ageMinutes != nil)
    }

    // MARK: The values the families draw

    var rows: [CardsRunningRow] { projection?.rows ?? [] }
    var longestRow: CardsRunningRow? { rows.first }
    var otherRows: [CardsRunningRow] { Array(rows.dropFirst()) }

    /// The server's count when it sent one; otherwise the rows it did send,
    /// but only when they are the whole set.
    var runningCount: Int? {
        guard let projection else { return nil }
        if let count = projection.runningCount { return count }
        return projection.rowsAreComplete ? projection.rows.count : nil
    }

    var moreCount: Int { max(0, (runningCount ?? rows.count) - 1) }

    /// A live answer that counted nothing is the only place "Nothing running"
    /// may be drawn. A blind entry takes the failure branch first.
    var isCalmEmpty: Bool {
        failure == nil && projection != nil && rows.isEmpty
    }

    /// `accessoryInline`: elapsed and title, or the gate word. The gate copy is
    /// owned here so an unconfigured device can never read as paused.
    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        guard let row = longestRow else { return "nothing running" }
        return "\(row.heroAgeText) · \(row.headline(max: 20))"
    }

    var runningText: String? {
        guard let runningCount else { return nil }
        return runningCount == 1 ? "1 running" : "\(runningCount) running"
    }
}

// MARK: - Timeline provider

struct CardsRunningTimelineProvider: TimelineProvider {
    private static let defaultRefreshMinutes = 10

    // GATE-SLICE-BEGIN: cards_running_cadence
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: cards_running_cadence

    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetCardsRunning)
    }

    func placeholder(in context: Context) -> CardsRunningEntry { .sample }

    func getSnapshot(in context: Context, completion: @escaping (CardsRunningEntry) -> Void) {
        let surface = Self.surface()
        Task { completion(await entry(surface: surface)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<CardsRunningEntry>) -> Void) {
        Task {
            let surface = Self.surface()
            let entry = await entry(surface: surface)
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

    private func entry(surface: WidgetSurfaceSettings?) async -> CardsRunningEntry {
        // Paused is checked BEFORE the client is built and before any read: a
        // paused surface spends zero requests and draws zero rows.
        if let surface, !surface.isActive {
            return .paused()
        }
        let refresh = surface?.refreshMinutes ?? Self.defaultRefreshMinutes
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else {
            return CardsRunningEntry(date: .now, updatedAt: .now, projection: nil,
                                     ageMinutes: nil, failure: .notConfigured,
                                     configured: false, paused: false,
                                     refreshMinutes: refresh)
        }
        let outcome = await WidgetData.read(
            using: client,
            lastKnown: { CardsRunningSnapshotStore.load() },
            { c in await CardsRunningFeed.read(client: c) })
        switch outcome {
        case .live(let projection, let fetchedAt):
            CardsRunningSnapshotStore.save(projection, at: fetchedAt)
            return CardsRunningEntry(date: .now, updatedAt: fetchedAt,
                                     projection: projection, ageMinutes: nil,
                                     failure: nil, configured: true,
                                     paused: false, refreshMinutes: refresh)
        case .stale(let projection, let ageMinutes, let reason):
            return CardsRunningEntry(
                date: .now,
                updatedAt: Date().addingTimeInterval(-Double(ageMinutes) * 60),
                projection: projection, ageMinutes: ageMinutes,
                failure: reason, configured: true, paused: false,
                refreshMinutes: refresh)
        case .failed(let failure):
            return CardsRunningEntry(date: .now, updatedAt: .now,
                                     projection: nil, ageMinutes: nil,
                                     failure: failure, configured: true,
                                     paused: false, refreshMinutes: refresh)
        }
    }
}

// MARK: - Widget + families

struct CardsRunningWidget: Widget {
    let kind = "CardsRunningWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: CardsRunningTimelineProvider()) { entry in
            CardsRunningWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Running")
        .description("The longest-running card with its elapsed time and worker heartbeat. Read-only.")
        #if os(iOS)
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge])
        #endif
    }
}

struct CardsRunningWidgetView: View {
    let entry: CardsRunningEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            Text(entry.inlineLine)
                .font(NativeStyle.small())
        case .accessoryCircular:
            CardsRunningCircularView(entry: entry)
        case .accessoryRectangular:
            CardsRunningRectangularView(entry: entry)
        #endif
        case .systemLarge:
            CardsRunningLargeView(entry: entry)
        case .systemExtraLarge:
            CardsRunningLargeView(entry: entry)
        case .systemMedium:
            CardsRunningMediumView(entry: entry)
        default:
            CardsRunningSmallView(entry: entry)
        }
    }
}

// MARK: - Shared panes

private struct CardsRunningGateView: View {
    let entry: CardsRunningEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Running")
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

private struct CardsRunningFailureView: View {
    let entry: CardsRunningEntry
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
            Text(entry.failure?.operatorLine ?? "The cluster did not answer.")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(detailed ? 3 : 2)
            if detailed {
                Text("No running cards are shown because this read did not answer. A count of zero would be a guess.")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct CardsRunningEmptyView: View {
    let entry: CardsRunningEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Nothing running")
                .font(NativeStyle.hero(26))
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .minimumScaleFactor(0.75)
            if let blocked = entry.projection?.blockedCount, blocked > 0 {
                Text(blocked == 1 ? "1 card blocked" : "\(blocked) cards blocked")
                    .font(NativeStyle.small())
                    .foregroundStyle(NativeStyle.warn)
            } else {
                Text("No cards are being worked.")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

// MARK: - Accessory families

struct CardsRunningCircularView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else if entry.failure != nil && entry.projection == nil {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.body)
                .foregroundStyle(.primary)
        } else if let row = entry.longestRow {
            VStack(spacing: 0) {
                Text(row.heroAgeText)
                    .font(NativeStyle.hero(20))
                    .foregroundStyle(.primary)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                Text("running")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else {
            Image(systemName: "checkmark.circle")
                .font(.body)
                .foregroundStyle(.primary)
        }
    }
}

struct CardsRunningRectangularView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
        } else if entry.failure != nil && entry.projection == nil {
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.failure?.shortLabel ?? "No data")
                    .font(NativeStyle.small())
                    .foregroundStyle(.primary)
                Text(entry.failure?.operatorLine ?? "")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        } else if let row = entry.longestRow {
            VStack(alignment: .leading, spacing: 1) {
                Text("\(row.heroAgeText) · \(row.headline(max: 24))")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                    .lineLimit(2)
                Text(row.heartbeatText)
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text("Nothing running")
                    .font(NativeStyle.sub())
                    .foregroundStyle(.primary)
                Text(entry.runningText ?? "no cards")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
            }
        }
    }
}

// MARK: - Home Screen families

struct CardsRunningSmallView: View {
    let entry: CardsRunningEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                CardsRunningGateView(entry: entry)
            } else if entry.projection == nil {
                CardsRunningFailureView(entry: entry)
            } else if entry.isCalmEmpty {
                CardsRunningEmptyView(entry: entry)
            } else if let row = entry.longestRow {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Longest running")
                        .font(NativeStyle.label())
                        .foregroundStyle(.secondary)
                    Text(row.heroAgeText)
                        .font(NativeStyle.hero())
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                    Text(row.headline(max: 34))
                        .font(NativeStyle.small())
                        .fontWeight(.semibold)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                    Spacer(minLength: 0)
                    HeartbeatView(row: row)
                    AsOfStamp(date: entry.dataAt)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            }
        }
    }
}

struct CardsRunningMediumView: View {
    let entry: CardsRunningEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                CardsRunningGateView(entry: entry)
            } else if entry.projection == nil {
                CardsRunningFailureView(entry: entry)
            } else if entry.isCalmEmpty {
                CardsRunningEmptyView(entry: entry)
            } else if let row = entry.longestRow {
                HStack(alignment: .top, spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Longest")
                            .font(NativeStyle.label())
                            .foregroundStyle(.secondary)
                        Text(row.heroAgeText)
                            .font(NativeStyle.hero())
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.7)
                        HeartbeatView(row: row)
                        Spacer(minLength: 0)
                        Text(entry.moreCount == 0 ? "no others" : "+\(entry.moreCount) more")
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                    }
                    .frame(width: 110, alignment: .leading)

                    VStack(alignment: .leading, spacing: 4) {
                        Text(row.headline(max: 52))
                            .font(NativeStyle.sub())
                            .foregroundStyle(.primary)
                            .lineLimit(2)
                        Text("\(row.cardID) · \(row.workerText)")
                            .font(NativeStyle.small())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        AsOfStamp(date: entry.dataAt)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            }
        }
    }
}

struct CardsRunningLargeView: View {
    let entry: CardsRunningEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                CardsRunningGateView(entry: entry)
            } else if entry.projection == nil {
                CardsRunningFailureView(entry: entry, detailed: true)
            } else if entry.isCalmEmpty {
                CardsRunningEmptyView(entry: entry)
            } else if let row = entry.longestRow {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .top, spacing: 14) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Longest running")
                                .font(NativeStyle.label())
                                .foregroundStyle(.secondary)
                            Text(row.heroAgeText)
                                .font(NativeStyle.hero())
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                            HeartbeatView(row: row)
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            Text(row.headline(max: 60))
                                .font(NativeStyle.sub())
                                .foregroundStyle(.primary)
                                .lineLimit(2)
                            Text("\(row.cardID) · \(row.workerText)")
                                .font(NativeStyle.small())
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                            if let board = row.board, !board.isEmpty {
                                Text(board)
                                    .font(NativeStyle.small())
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    if !entry.otherRows.isEmpty {
                        Divider()
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(entry.otherRows.prefix(6)) { other in
                                HStack(alignment: .firstTextBaseline, spacing: 8) {
                                    Text(other.headline(max: 44))
                                        .font(NativeStyle.small())
                                        .foregroundStyle(.primary)
                                        .lineLimit(1)
                                    Spacer(minLength: 0)
                                    Text(other.heroAgeText)
                                        .font(NativeStyle.number(11))
                                        .foregroundStyle(.secondary)
                                    Text(other.heartbeatText)
                                        .font(NativeStyle.small())
                                        .foregroundStyle(heartbeatColor(other))
                                        .lineLimit(1)
                                }
                            }
                        }
                    }
                    Spacer(minLength: 0)
                    AsOfStamp(date: entry.dataAt)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
                .stale(entry.staleState)
            }
        }
    }

    private func heartbeatColor(_ row: CardsRunningRow) -> Color {
        guard let fresh = row.heartbeatIsFresh else { return .secondary }
        return fresh ? NativeStyle.ok : NativeStyle.warn
    }
}

private struct HeartbeatView: View {
    let row: CardsRunningRow

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 6, height: 6)
            Text(row.heartbeatText)
                .font(NativeStyle.small())
                .foregroundStyle(color)
                .lineLimit(1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(row.heartbeatText)
    }

    private var color: Color {
        guard let fresh = row.heartbeatIsFresh else { return .secondary }
        return fresh ? NativeStyle.ok : NativeStyle.warn
    }
}
