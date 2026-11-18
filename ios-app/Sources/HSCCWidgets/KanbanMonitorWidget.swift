import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// KanbanMonitorWidget.swift — `hscc kanban monitor`'s board table on the Home
// Screen and Lock Screen. READ-ONLY: one GET through `WidgetDataClient`, no
// actions, and only the projected board rows are persisted.
//
// WHAT IT SHOWS, AND WHERE EACH NUMBER COMES FROM
//
// The CLI's board table (hscc_daemon/kanban_live.py::boards_table) prints one
// row per registered board with Running, Ready, Todo, Scheduled, Review,
// Blocked, Triage and Done 24h. The data contract's compact route,
// `GET /v1/mobile/summary`, is the same collection projected by
// `kanban_live.board_status_counts`: per board, `counts.{running,ready,todo,
// blocked,done,archived}` plus `total`. So every number this file draws is a
// direct field of `boards[].counts`:
//
//   running  <- boards[].counts.running   (CLI column "Running")
//   ready    <- boards[].counts.ready     (CLI column "Ready")
//   todo     <- boards[].counts.todo      (CLI column "Todo")
//   blocked  <- boards[].counts.blocked   (CLI column "Blocked")
//   done     <- boards[].counts.done      (CLI column "Done 24h", i.e. done today)
//
// The board name is `boards[].board`; the row order is the CLI's
// `(-running, name)`, and a board with no open card and no done-today card is
// skipped exactly as `boards_table` skips it. Scheduled/Review/Triage are not
// drawn: the card asks for the five above and the route folds any other status
// into `other`, which is never rendered as one of them.
//
// WHY NOT THE KANBAN TRIO. `/v1/kanban/{running,blocked,stale}` has no `done`
// and no per-board histogram; it cannot answer "done today exactly as the CLI".
// The summary route is the contract's compact board table and costs one request
// instead of three. `/v1/mobile/summary` is served by the same
// `kanban_live.collect(registered_only=True)` pass as `hscc kanban monitor`, so
// orphan sqlite boards never appear here.
//
// FAILURE RULE. `WidgetData.read` owns live/stale/failed. A stale entry draws
// its real rows with an explicit "updated Nm ago · reason"; a failed entry draws
// the reason, never five dashes dressed up as a quiet board. A truncated or
// per-board-error payload is refused rather than drawn as an under-count
// (§6 rules 4/5 of WIDGET_DATA_CONTRACT.md). The provider decides pause BEFORE
// it builds a client, so a paused surface sends zero requests.
// ---------------------------------------------------------------------------

// MARK: - One board row

/// One row of the CLI's board table, projected to the five columns this card
/// owns. All counts are optional so a payload that did not carry a board cannot
/// be rendered as `0`; only rows built from a real `counts` map have values.
struct KanbanBoardRow: Codable, Identifiable, Equatable, Hashable, Sendable {
    let board: String
    let running: Int?
    let ready: Int?
    let todo: Int?
    let blocked: Int?
    let done: Int?

    var id: String { board }

    /// The CLI's idle-board rule: a board with nothing open and nothing finished
    /// today is skipped by `boards_table`, so it is skipped here too.
    var hasActivity: Bool {
        [running, ready, todo, blocked, done].contains { ($0 ?? 0) > 0 }
    }

    /// The response → rows. nil counts (a board-level `error`) are dropped, not
    /// zeroed; the caller refuses the whole read when the envelope is truncated.
    static func rows(from summary: MobileSummaryResponse) -> [KanbanBoardRow] {
        summary.entries.compactMap { tile -> KanbanBoardRow? in
            guard tile.error == nil, let counts = tile.counts else { return nil }
            return KanbanBoardRow(board: tile.board,
                                  running: counts.running,
                                  ready: counts.ready,
                                  todo: counts.todo,
                                  blocked: counts.blocked,
                                  done: counts.done)
        }
        .filter(\.hasActivity)
        .sorted { lhs, rhs in
            let l = lhs.running ?? -1
            let r = rhs.running ?? -1
            return l == r ? lhs.board < rhs.board : l > r
        }
    }

    /// The provider's read rule, extracted so the gate can execute it: a
    /// degraded, truncated, or wholly-unreadable payload is refused (the
    /// last-known rows stay on screen); a single board-level error drops only
    /// that board, and the entry announces the count.
    static func readResult(from summary: MobileSummaryResponse) -> Result<KanbanBoardTable, WidgetDataFailure> {
        guard !summary.degraded else {
            return .failure(.decoding(detail: "mobile/summary carried no boards"))
        }
        if summary.truncated == true || (summary.omitted_boards ?? 0) > 0 {
            return .failure(.decoding(detail: "mobile/summary was truncated"))
        }
        let usable = summary.entries.filter { $0.error == nil && $0.counts != nil }
        guard !usable.isEmpty || summary.entries.isEmpty else {
            return .failure(.decoding(detail: "every board carried an error"))
        }
        return .success(KanbanBoardTable(rows: rows(from: summary),
                                         boardCount: summary.board_count,
                                         unreadableBoards: summary.unreadable_boards))
    }

    /// The five numbers as one short line for the rectangular accessory.
    var countsLine: String {
        [
            "\(KanbanMonitorReading.text(running)) run",
            "\(KanbanMonitorReading.text(ready)) ready",
            "\(KanbanMonitorReading.text(todo)) todo",
            "\(KanbanMonitorReading.text(blocked)) blk",
            "\(KanbanMonitorReading.text(done)) done",
        ].joined(separator: " · ")
    }
}

/// The compact summary's board half: active rows plus the envelope's own counts.
/// `unreadableBoards` is a COUNT only — the route never echoes a slug that
/// failed its shape gate.
struct KanbanBoardTable {
    let rows: [KanbanBoardRow]
    let boardCount: Int?
    let unreadableBoards: Int?
}

/// The one place a count becomes a string. nil is "the payload did not carry
/// that board", never a zero. Kept as a type so the widget and the gate use the
/// same rule rather than two format strings.
enum KanbanMonitorReading {
    static func text(_ value: Int?) -> String { value.map { "\($0)" } ?? "—" }

    /// Fleet running is the CLI title's `n_run`: the sum of the per-board
    /// `Running` cells in the table below it. It is the one aggregate the CLI
    /// itself prints (`n/cap slots`).
    static func runningTotal(_ rows: [KanbanBoardRow]) -> Int {
        rows.reduce(0) { $0 + ($1.running ?? 0) }
    }

    static func blockedTotal(_ rows: [KanbanBoardRow]) -> Int {
        rows.reduce(0) { $0 + ($1.blocked ?? 0) }
    }

    static func doneTotal(_ rows: [KanbanBoardRow]) -> Int {
        rows.reduce(0) { $0 + ($1.done ?? 0) }
    }
}

// MARK: - Last-known board rows

/// The board rows and the date they were read. Board slugs + integers only —
/// no card title, no path, no address, no token (§7).
enum KanbanMonitorSnapshotStore {
    static let storageKey = "hscc.kanbanmonitor.rows"

    struct Snapshot: Codable {
        let date: Date
        let rows: [KanbanBoardRow]
    }

    static func save(_ rows: [KanbanBoardRow], at date: Date = Date()) {
        guard !rows.isEmpty,
              let data = try? JSONEncoder().encode(Snapshot(date: date, rows: rows)) else { return }
        SharedStore.suite?.set(data, forKey: storageKey)
    }

    static func load() -> Snapshot? {
        guard let data = SharedStore.suite?.data(forKey: storageKey),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { return nil }
        return snapshot
    }

    static func lastKnown() -> (value: KanbanBoardTable, at: Date)? {
        guard let snapshot = load(), !snapshot.rows.isEmpty else { return nil }
        return (KanbanBoardTable(rows: snapshot.rows,
                                 boardCount: snapshot.rows.count,
                                 unreadableBoards: nil), snapshot.date)
    }
}

// MARK: - Entry

/// One rendered pass of the board table. Unconfigured, paused, stale and failed
/// are first-class; no path leaves a family blank.
struct KanbanMonitorEntry: TimelineEntry {
    let date: Date
    let configured: Bool
    /// The rows the views draw, already filtered to active boards and sorted
    /// like the CLI. Empty with `failure == nil` is an honest "nothing open".
    let rows: [KanbanBoardRow]
    /// Registered boards the payload carried, `nil` when the read failed.
    let boardCount: Int?
    /// Boards the server could not read; announced, never silently dropped.
    let unreadableBoards: Int?
    /// Set only when NOTHING usable came back. A partial read keeps its rows
    /// and names the gap in `partialReason` instead of hiding the whole table.
    let failure: WidgetDataFailure?
    let partialReason: WidgetDataFailure?
    /// Minutes since the stored rows were read; nil when this pass is live.
    let ageMinutes: Int?
    let refreshMinutes: Int
    let paused: Bool

    static let unconfigured = KanbanMonitorEntry(
        date: .now, configured: false, rows: [], boardCount: nil,
        unreadableBoards: nil, failure: nil, partialReason: nil,
        ageMinutes: nil, refreshMinutes: KanbanMonitorTimelineProvider.defaultRefreshMinutes,
        paused: false)

    static func paused(refreshMinutes: Int) -> KanbanMonitorEntry {
        KanbanMonitorEntry(date: .now, configured: true, rows: [], boardCount: nil,
                           unreadableBoards: nil, failure: nil, partialReason: nil,
                           ageMinutes: nil, refreshMinutes: refreshMinutes, paused: true)
    }

    // MARK: Gate copy

    var gateLine: String {
        if !configured { return "Set up the app to see the board" }
        return "Paused in Settings"
    }

    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        return "Paused"
    }

    // MARK: Text the families draw

    var runningTotal: Int { KanbanMonitorReading.runningTotal(rows) }
    var blockedTotal: Int { KanbanMonitorReading.blockedTotal(rows) }
    var doneTotal: Int { KanbanMonitorReading.doneTotal(rows) }
    var headlineNumber: Int? { failure == nil ? runningTotal : nil }
    var headlineLabel: String { "running" }

    /// `accessoryInline`: one short line, setup invite first when unconfigured.
    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        guard let first = rows.first else { return "No open cards" }
        // One board's exact cells, not a fleet sum: the Lock Screen line is a
        // pointer to the busiest board, and every number on it is a field of
        // that row in the CLI's table. A name that will not fit is dropped
        // rather than cut in half.
        let withBoard = "\(first.board) \(KanbanMonitorReading.text(first.running)) run · \(KanbanMonitorReading.text(first.blocked)) blocked"
        if withBoard.count <= 25 { return withBoard }
        return "\(KanbanMonitorReading.text(first.running)) run · \(KanbanMonitorReading.text(first.blocked)) blocked"
    }

    /// `accessoryRectangular`'s second line: the busiest board, verbatim.
    var rectangularLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        guard let first = rows.first else { return "No open cards" }
        return "\(first.board) \(KanbanMonitorReading.text(first.running)) run · \(KanbanMonitorReading.text(first.blocked)) blocked"
    }

    /// The freshness line every family can afford.
    var updatedLine: String {
        if !configured { return "Set up the app to see the board" }
        if paused { return "No data while paused" }
        if let ageMinutes { return "updated \(ageMinutes)m ago · \(partialReason?.shortLabel ?? "kept last known")" }
        if let failure { return failure.operatorLine }
        if rows.isEmpty { return "updated just now · no open cards" }
        return "updated just now"
    }

    /// Why some rows may be missing even though the read succeeded.
    var partialLine: String? {
        if let partialReason { return "partial · \(partialReason.shortLabel)" }
        if let unreadableBoards, unreadableBoards > 0 {
            return "\(unreadableBoards) board\(unreadableBoards == 1 ? "" : "s") unreadable"
        }
        return nil
    }

    static func rowLimit(for family: WidgetFamily) -> Int {
        switch family {
        case .systemSmall: return 2
        case .systemLarge, .systemExtraLarge: return 8
        default: return 5
        }
    }

    static var placeholder: KanbanMonitorEntry {
        KanbanMonitorEntry(
            date: .now, configured: true,
            rows: [
                KanbanBoardRow(board: "hscc", running: 2, ready: 0, todo: 0, blocked: 5, done: 73),
                KanbanBoardRow(board: "radio", running: 1, ready: 2, todo: 3, blocked: 0, done: 4),
            ],
            boardCount: 2, unreadableBoards: nil, failure: nil, partialReason: nil,
            ageMinutes: nil,
            refreshMinutes: KanbanMonitorTimelineProvider.defaultRefreshMinutes,
            paused: false)
    }
}

// MARK: - Timeline provider

struct KanbanMonitorTimelineProvider: TimelineProvider {
    static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: kanban_monitor_cadence
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: kanban_monitor_cadence

    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetKanbanMonitor)
    }

    func placeholder(in context: Context) -> KanbanMonitorEntry { .placeholder }

    func getSnapshot(in context: Context, completion: @escaping (KanbanMonitorEntry) -> Void) {
        let surface = Self.surface()
        Task { completion(await fetchEntry(surface: surface)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<KanbanMonitorEntry>) -> Void) {
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

    private func fetchEntry(surface: WidgetSurfaceSettings?) async -> KanbanMonitorEntry {
        // Paused is decided before the client exists: zero requests, zero rows.
        if let surface, !surface.isActive {
            return .paused(refreshMinutes: surface.refreshMinutes)
        }
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else { return .unconfigured }

        let outcome = await WidgetData.read(
            using: client,
            lastKnown: KanbanMonitorSnapshotStore.lastKnown
        ) { c in
            await Self.readBoards(client: c)
        }

        switch outcome {
        case .live(let table, let fetchedAt):
            KanbanMonitorSnapshotStore.save(table.rows, at: fetchedAt)
            return KanbanMonitorEntry(date: fetchedAt, configured: true, rows: table.rows,
                                      boardCount: table.boardCount,
                                      unreadableBoards: table.unreadableBoards,
                                      failure: nil, partialReason: nil, ageMinutes: nil,
                                      refreshMinutes: surface?.refreshMinutes
                                          ?? Self.defaultRefreshMinutes,
                                      paused: false)
        case .stale(let table, let age, let reason):
            return KanbanMonitorEntry(date: .now, configured: true, rows: table.rows,
                                      boardCount: table.boardCount,
                                      unreadableBoards: table.unreadableBoards,
                                      failure: nil, partialReason: reason, ageMinutes: age,
                                      refreshMinutes: surface?.refreshMinutes
                                          ?? Self.defaultRefreshMinutes,
                                      paused: false)
        case .failed(let failure):
            return KanbanMonitorEntry(date: .now, configured: true, rows: [],
                                      boardCount: nil, unreadableBoards: nil,
                                      failure: failure, partialReason: nil, ageMinutes: nil,
                                      refreshMinutes: surface?.refreshMinutes
                                          ?? Self.defaultRefreshMinutes,
                                      paused: false)
        }
    }

    /// One read; the summary is the CLI's board table. Truncation and a
    /// board-level error are refusals, not an under-count: a table missing
    /// boards would look like those boards went quiet.
    private static func readBoards(client: WidgetDataClient) async -> Result<KanbanBoardTable, WidgetDataFailure> {
        switch await client.mobileSummary() {
        case .failure(let failure):
            return .failure(failure)
        case .success(let summary):
            return KanbanBoardRow.readResult(from: summary)
        }
    }
}

// MARK: - Widget

struct KanbanMonitorWidget: Widget {
    let kind = "KanbanMonitorWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetKanbanMonitor)
        return StaticConfiguration(kind: kind, provider: KanbanMonitorTimelineProvider()) { entry in
            KanbanMonitorWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Kanban Monitor")
        .description("Registered project boards: running, ready, todo, blocked and done today. Read-only.")
        #if os(iOS)
        .supportedFamilies([.accessoryInline, .accessoryCircular, .accessoryRectangular,
                            .systemSmall, .systemMedium, .systemLarge])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge])
        #endif
    }
}

struct KanbanMonitorWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            if !entry.configured || entry.paused {
                Text(entry.shortGateLabel).font(style.font(.caption2))
            } else {
                Text(entry.inlineLine).font(style.font(.caption2))
            }
        case .accessoryCircular:
            KanbanMonitorCircular(entry: entry)
        case .accessoryRectangular:
            KanbanMonitorRectangular(entry: entry)
        #endif
        case .systemLarge, .systemExtraLarge:
            LargeKanbanMonitor(entry: entry)
        case .systemMedium:
            MediumKanbanMonitor(entry: entry)
        default:
            SmallKanbanMonitor(entry: entry)
        }
    }
}

// MARK: - Shared panes

struct KanbanMonitorGateView: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry
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

struct KanbanMonitorErrorPane: View {
    @Environment(\.widgetStyle) private var style
    let failure: WidgetDataFailure
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                    .font(style.font(.subheadline))
                    .foregroundColor(style.badColor)
                Text("board read failed")
                    .font(style.font(.caption, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
            }
            Text(detailed ? failure.operatorLine : failure.shortLabel)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(detailed ? 3 : 2)
            Text("counts unknown")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

private struct KanbanMonitorHeader: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Text("Boards")
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

/// One board row with all five columns. `compact` drops the labels (the header
/// above names the columns) for the two-row small family.
private struct KanbanBoardRowView: View {
    @Environment(\.widgetStyle) private var style
    let row: KanbanBoardRow

    private func value(_ v: Int?) -> Text {
        Text(KanbanMonitorReading.text(v))
            .font(style.font(.caption2, weight: .medium).monospacedDigit())
            .foregroundColor(v == nil ? style.secondaryTextColor : style.primaryTextColor)
    }

    private func cell(_ v: Int?, width: CGFloat) -> some View {
        value(v).frame(width: style.scaled(width), alignment: .trailing)
    }

    var body: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Text(row.board)
                .font(style.font(.caption2, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
                .lineLimit(1)
            Spacer(minLength: style.spacing(Theme.Spacing.xs.rawValue))
            cell(row.running, width: 18)
            cell(row.ready, width: 18)
            cell(row.todo, width: 20)
            cell(row.blocked, width: 20)
            cell(row.done, width: 22)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(rowAccessibility)
    }

    private var rowAccessibility: String {
        "\(row.board): \(KanbanMonitorReading.text(row.running)) running, "
            + "\(KanbanMonitorReading.text(row.ready)) ready, "
            + "\(KanbanMonitorReading.text(row.todo)) todo, "
            + "\(KanbanMonitorReading.text(row.blocked)) blocked, "
            + "\(KanbanMonitorReading.text(row.done)) done today"
    }
}

private struct KanbanColumnHeader: View {
    @Environment(\.widgetStyle) private var style

    private func label(_ text: String, width: CGFloat) -> some View {
        Text(text)
            .font(style.font(size: 7, weight: .semibold))
            .foregroundColor(style.secondaryTextColor)
            .frame(width: style.scaled(width), alignment: .trailing)
    }

    var body: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Text("BOARD")
                .font(style.font(size: 7, weight: .semibold))
                .foregroundColor(style.secondaryTextColor)
            Spacer(minLength: 0)
            label("RUN", width: 18)
            label("RDY", width: 18)
            label("TODO", width: 20)
            label("BLK", width: 20)
            label("DONE", width: 22)
        }
        .widgetHeader(style)
    }
}

// MARK: - Accessory families

struct KanbanMonitorCircular: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

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
                Text(KanbanMonitorReading.text(entry.runningTotal))
                    .font(style.font(.body))
                    .fontWeight(.semibold)
                    .foregroundStyle(.primary)
                Text("running")
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct KanbanMonitorRectangular: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            KanbanMonitorGateView(entry: entry, systemTinted: true)
        } else if let failure = entry.failure {
            VStack(alignment: .leading, spacing: 1) {
                Text("board read failed")
                    .font(style.font(.caption2, weight: .semibold))
                Text(failure.shortLabel)
                    .font(style.font(.caption2))
                Text(entry.updatedLine)
                    .font(style.font(.caption2))
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text("Kanban · \(KanbanMonitorReading.text(entry.runningTotal)) running")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(entry.rectangularLine)
                    .font(style.font(.caption2))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(entry.updatedLine)
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }
}

// MARK: - Home Screen families

struct SmallKanbanMonitor: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                KanbanMonitorGateView(entry: entry)
            } else if let failure = entry.failure {
                KanbanMonitorErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    KanbanMonitorHeader(entry: entry)
                    HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                        Text(KanbanMonitorReading.text(entry.headlineNumber))
                            .font(style.font(.largeTitle, weight: .bold))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                            .minimumScaleFactor(0.6)
                        Text(entry.headlineLabel)
                            .font(style.font(.caption, weight: .semibold))
                            .foregroundColor(style.secondaryTextColor)
                    }
                    if entry.rows.isEmpty {
                        Text("No open cards")
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    } else {
                        ForEach(entry.rows.prefix(KanbanMonitorEntry.rowLimit(for: .systemSmall))) { row in
                            KanbanBoardRowView(row: row)
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

struct MediumKanbanMonitor: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                KanbanMonitorGateView(entry: entry)
            } else if let failure = entry.failure {
                KanbanMonitorErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                    KanbanMonitorHeader(entry: entry)
                    KanbanColumnHeader()
                    ForEach(entry.rows.prefix(KanbanMonitorEntry.rowLimit(for: .systemMedium))) { row in
                        KanbanBoardRowView(row: row)
                    }
                    if entry.rows.isEmpty {
                        Text("No open cards")
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
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

struct LargeKanbanMonitor: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                KanbanMonitorGateView(entry: entry)
            } else if let failure = entry.failure {
                KanbanMonitorErrorPane(failure: failure, detailed: true)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    KanbanMonitorHeader(entry: entry)
                    // The CLI title's `n_run`: the one fleet aggregate the
                    // command prints. Everything below is a per-board row.
                    Text("\(KanbanMonitorReading.text(entry.runningTotal)) running")
                        .font(style.font(.caption, weight: .semibold))
                        .foregroundColor(style.primaryTextColor)
                    KanbanColumnHeader()
                    ForEach(entry.rows.prefix(KanbanMonitorEntry.rowLimit(for: .systemLarge))) { row in
                        KanbanBoardRowView(row: row)
                    }
                    if entry.rows.isEmpty {
                        Text("No open cards")
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
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
