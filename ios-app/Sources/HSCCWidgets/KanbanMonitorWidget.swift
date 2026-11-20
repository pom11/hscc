import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// KanbanMonitorWidget.swift — the Boards widget (B3 Stacked bars, 2026-11-20).
//
// The WidgetKit kind stays `KanbanMonitorWidget` so placed widgets survive the
// consolidation; the display name is now "Boards". It replaces three surfaces:
// Kanban Monitor, Board Status and Projects.
//
// WHAT IT SHOWS
//
//   small  — "Open cards" and the top four boards by open count: name + total,
//            a stacked bar split running (accent) / todo (secondary) / blocked
//            (orange), length proportional to the largest open count.
//   medium — "Open cards by board" + as-of, four rows with the board name,
//            stacked bar and "N done" today, plus the legend row.
//   large  — every active board (up to ten) as medium rows, the worker-slot
//            strip "R of C slots" and the footer "D done today · B blocked".
//   scoped — the same bar for ONE board (the existing scope intent), its
//            column counts and the titles of that board's running cards.
//
// EVERY NUMBER IS A ROUTE FIELD
//
// `GET /v1/mobile/summary` (optionally narrowed with `?boards=`) supplies
// `boards[].counts.{running,todo,blocked,done}` and `max_in_progress`, the
// server's own `kanban.max_in_progress` cap. The widget never carries a
// literal slot count: with no cap on the wire it says "R running" rather than
// inventing "of 6". `/v1/kanban/running` is read a second time ONLY when the
// operator scoped this surface to one board, because that is the only size
// that draws running titles.
//
// FAILURE RULE. `WidgetData.read` owns live/stale/failed. A stale entry keeps
// its real rows, dims them, and says "offline · HH:mm"; a failed read says so
// and never dresses an empty table up as a quiet fleet. Truncation and a
// board-level error are refusals, not an under-count. The provider decides
// pause BEFORE it builds a client, so a paused surface sends zero requests.
// ---------------------------------------------------------------------------

// MARK: - One board row

/// One row of the CLI's board table, projected to the columns the Boards
/// widget draws. Counts stay optional so a payload that did not carry a board
/// cannot be rendered as `0`.
struct KanbanBoardRow: Codable, Identifiable, Equatable, Hashable, Sendable {
    let board: String
    let running: Int?
    let ready: Int?
    let todo: Int?
    let blocked: Int?
    let done: Int?

    var id: String { board }

    /// The B3 bar: running + todo + blocked. `ready` and `done` are shown as
    /// separate facts, never folded into the bar length.
    var openCards: Int { (running ?? 0) + (todo ?? 0) + (blocked ?? 0) }

    /// The CLI's idle-board rule: a board with nothing open and nothing finished
    /// today is skipped by `boards_table`, so it is skipped here too.
    var hasActivity: Bool {
        [running, ready, todo, blocked, done].contains { ($0 ?? 0) > 0 }
    }

    /// The response → rows, ordered by open cards (the B3 question), then name.
    /// nil counts (a board-level `error`) are dropped, not zeroed; the caller
    /// refuses the whole read when the envelope is truncated.
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
            lhs.openCards == rhs.openCards
                ? lhs.board < rhs.board
                : lhs.openCards > rhs.openCards
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
                                         unreadableBoards: summary.unreadable_boards,
                                         maxInProgress: summary.max_in_progress,
                                         generatedAtRaw: summary.generated_at))
    }
}

/// The compact summary's board half: active rows plus the envelope's own counts
/// and the server's worker-slot cap.
struct KanbanBoardTable {
    let rows: [KanbanBoardRow]
    let boardCount: Int?
    let unreadableBoards: Int?
    let maxInProgress: Int?
    let generatedAtRaw: String?
    var runningTitles: [String]

    init(rows: [KanbanBoardRow], boardCount: Int?, unreadableBoards: Int?,
         maxInProgress: Int? = nil, generatedAtRaw: String? = nil,
         runningTitles: [String] = []) {
        self.rows = rows
        self.boardCount = boardCount
        self.unreadableBoards = unreadableBoards
        self.maxInProgress = maxInProgress
        self.generatedAtRaw = generatedAtRaw
        self.runningTitles = runningTitles
    }
}

/// The one place a count becomes a string. nil is "the payload did not carry
/// that board", never a zero. Kept as a type so the widget and the gate use the
/// same rule rather than two format strings.
enum KanbanMonitorReading {
    static func text(_ value: Int?) -> String { value.map { "\($0)" } ?? "—" }

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

/// The board rows, the server cap and the date they were read. Board slugs +
/// integers only — no card title, no path, no address, no token (§7). Running
/// titles are deliberately not persisted: the next timeline asks the server.
enum KanbanMonitorSnapshotStore {
    static let storageKey = "hscc.kanbanmonitor.rows"

    struct Snapshot: Codable {
        let date: Date
        let rows: [KanbanBoardRow]
        var maxInProgress: Int? = nil
    }

    static func save(_ rows: [KanbanBoardRow], maxInProgress: Int?,
                     at date: Date = Date()) {
        guard !rows.isEmpty,
              let data = try? JSONEncoder().encode(
                Snapshot(date: date, rows: rows, maxInProgress: maxInProgress))
        else { return }
        SharedStore.suite?.set(data, forKey: storageKey)
    }

    static func load() -> Snapshot? {
        guard let data = SharedStore.suite?.data(forKey: storageKey),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data)
        else { return nil }
        return snapshot
    }

    static func lastKnown() -> (value: KanbanBoardTable, at: Date)? {
        guard let snapshot = load(), !snapshot.rows.isEmpty else { return nil }
        return (KanbanBoardTable(rows: snapshot.rows,
                                 boardCount: snapshot.rows.count,
                                 unreadableBoards: nil,
                                 maxInProgress: snapshot.maxInProgress,
                                 generatedAtRaw: nil),
                snapshot.date)
    }
}

// MARK: - Entry

/// One rendered pass of the board table. Unconfigured, paused, stale and failed
/// are first-class; no path leaves a family blank.
struct KanbanMonitorEntry: TimelineEntry {
    let date: Date
    let configured: Bool
    /// The rows the views draw, already filtered to active boards and sorted
    /// by open count. Empty with `failure == nil` is an honest "nothing open".
    let rows: [KanbanBoardRow]
    let boardCount: Int?
    let unreadableBoards: Int?
    /// The server's `kanban.max_in_progress`; nil when unset or unreadable.
    let maxInProgress: Int?
    /// The summary's `generated_at`, the data's own clock.
    let generatedAtRaw: String?
    /// The one board this surface is pointed at, when the scope intent chose one.
    let scopeBoard: String?
    /// Titles of that board's running cards; only read for a scoped surface.
    let runningTitles: [String]
    /// Set only when NOTHING usable came back. A partial read keeps its rows
    /// and names the gap in `partialReason` instead of hiding the whole table.
    let failure: WidgetDataFailure?
    let partialReason: WidgetDataFailure?
    /// Minutes since the stored rows were read; nil when this pass is live.
    let ageMinutes: Int?
    let refreshMinutes: Int
    let paused: Bool

    init(date: Date, configured: Bool, rows: [KanbanBoardRow],
         boardCount: Int?, unreadableBoards: Int?,
         maxInProgress: Int? = nil, generatedAtRaw: String? = nil,
         scopeBoard: String? = nil,
         runningTitles: [String] = [], failure: WidgetDataFailure?,
         partialReason: WidgetDataFailure?, ageMinutes: Int?,
         refreshMinutes: Int, paused: Bool) {
        self.date = date
        self.configured = configured
        self.rows = rows
        self.boardCount = boardCount
        self.unreadableBoards = unreadableBoards
        self.maxInProgress = maxInProgress
        self.generatedAtRaw = generatedAtRaw
        self.scopeBoard = scopeBoard
        self.runningTitles = runningTitles
        self.failure = failure
        self.partialReason = partialReason
        self.ageMinutes = ageMinutes
        self.refreshMinutes = refreshMinutes
        self.paused = paused
    }

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

    // MARK: The data's own clock

    /// HH:mm of the server's `generated_at` (the data), never of the render.
    var dataAt: Date? {
        if let ageMinutes {
            return Date().addingTimeInterval(-Double(ageMinutes) * 60)
        }
        if let generatedAt {
            return generatedAt
        }
        if failure != nil { return nil }
        return date
    }

    /// Parsed once per entry; a malformed stamp falls back to the fetch time.
    private var generatedAt: Date? { Self.parseStamp(generatedAtRaw) }

    static func parseStamp(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: raw) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: raw)
    }

    var staleState: StaleState? {
        guard configured, !paused else { return nil }
        return StaleState(dataAt: dataAt,
                          fetchedAt: Date(),
                          refreshMinutes: refreshMinutes,
                          failed: failure != nil || ageMinutes != nil)
    }

    // MARK: Aggregates the families draw

    var runningTotal: Int { KanbanMonitorReading.runningTotal(rows) }
    var blockedTotal: Int { KanbanMonitorReading.blockedTotal(rows) }
    var doneTotal: Int { KanbanMonitorReading.doneTotal(rows) }

    /// The largest open count, the denominator of every B3 bar.
    var maxOpenCards: Int { max(rows.map(\.openCards).max() ?? 0, 1) }

    var topBoards: [KanbanBoardRow] { Array(rows.prefix(4)) }

    var scopedRow: KanbanBoardRow? {
        guard let scopeBoard else { return nil }
        return rows.first { $0.board == scopeBoard }
    }

    /// `accessoryInline`: one short line, setup invite first when unconfigured.
    var inlineLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        return "\(KanbanMonitorReading.text(runningTotal)) running · "
            + "\(KanbanMonitorReading.text(blockedTotal)) blocked"
    }

    /// `accessoryRectangular`'s second line.
    var rectangularLine: String {
        if !configured || paused { return shortGateLabel }
        if let failure { return failure.shortLabel }
        return "\(KanbanMonitorReading.text(doneTotal)) done today · "
            + "\(KanbanMonitorReading.text(blockedTotal)) blocked"
    }

    // MARK: L2 Lock Screen copy

    /// The server's worker-slot cap, widened to the running count when a stale
    /// row claims more workers than the cap. Nil means the route sent no cap;
    /// the ring then says "N running" instead of inventing "of 6".
    var slotCap: Int? {
        guard let maxInProgress, maxInProgress > 0 else { return nil }
        return max(maxInProgress, runningTotal)
    }

    /// `accessoryCircular`: "2/6" when the cap is known, otherwise "2".
    var slotValueText: String {
        slotCap.map { "\(runningTotal)/\($0)" } ?? "\(runningTotal)"
    }

    /// `accessoryCircular`: "slots" when the cap is known, otherwise "running".
    var slotCaption: String { slotCap == nil ? "running" : "slots" }

    var slotGaugeRange: ClosedRange<Double> {
        let upper = Double(slotCap ?? max(runningTotal, 1))
        return 0...max(upper, 1)
    }

    var slotGaugeValue: Double {
        min(Double(runningTotal), slotGaugeRange.upperBound)
    }

    /// The rectangular header: "BOARDS · N RUNNING".
    var rectangularHeadline: String {
        "BOARDS · \(KanbanMonitorReading.text(runningTotal)) RUNNING"
    }

    /// The freshness line the Home Screen families can afford.
    var updatedLine: String {
        if !configured { return "Set up the app to see the board" }
        if paused { return "No data while paused" }
        if ageMinutes != nil { return "offline · \(NativeStyle.hhmm(dataAt))" }
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
        case .systemSmall: return 4
        case .systemLarge, .systemExtraLarge: return 10
        default: return 4
        }
    }

    static var placeholder: KanbanMonitorEntry {
        KanbanMonitorEntry(
            date: .now, configured: true,
            rows: [
                KanbanBoardRow(board: "hscc", running: 2, ready: 0, todo: 0, blocked: 5, done: 73),
                KanbanBoardRow(board: "radio", running: 1, ready: 2, todo: 3, blocked: 0, done: 4),
            ],
            boardCount: 2, unreadableBoards: nil, maxInProgress: 6,
            failure: nil, partialReason: nil, ageMinutes: nil,
            refreshMinutes: KanbanMonitorTimelineProvider.defaultRefreshMinutes,
            paused: false)
    }
}

// MARK: - Timeline provider

struct KanbanMonitorTimelineProvider: TimelineProvider {
    static let defaultRefreshMinutes = 15

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

        let scope = AppGroupWidgetSettings.load()?.scope(for: .widgetKanbanMonitor)
        let board = scope?.board
        let outcome = await WidgetData.read(
            using: client,
            lastKnown: KanbanMonitorSnapshotStore.lastKnown
        ) { c in
            await Self.readBoards(client: c, board: board)
        }

        switch outcome {
        case .live(let table, let fetchedAt):
            KanbanMonitorSnapshotStore.save(table.rows, maxInProgress: table.maxInProgress,
                                            at: fetchedAt)
            return KanbanMonitorEntry(date: fetchedAt, configured: true, rows: table.rows,
                                      boardCount: table.boardCount,
                                      unreadableBoards: table.unreadableBoards,
                                      maxInProgress: table.maxInProgress,
                                      scopeBoard: board,
                                      runningTitles: table.runningTitles,
                                      failure: nil, partialReason: nil, ageMinutes: nil,
                                      refreshMinutes: surface?.refreshMinutes
                                          ?? Self.defaultRefreshMinutes,
                                      paused: false)
        case .stale(let table, let age, let reason):
            return KanbanMonitorEntry(date: .now, configured: true, rows: table.rows,
                                      boardCount: table.boardCount,
                                      unreadableBoards: table.unreadableBoards,
                                      maxInProgress: table.maxInProgress,
                                      scopeBoard: board,
                                      failure: nil, partialReason: reason, ageMinutes: age,
                                      refreshMinutes: surface?.refreshMinutes
                                          ?? Self.defaultRefreshMinutes,
                                      paused: false)
        case .failed(let failure):
            return KanbanMonitorEntry(date: .now, configured: true, rows: [],
                                      boardCount: nil, unreadableBoards: nil,
                                      maxInProgress: nil, scopeBoard: board,
                                      failure: failure, partialReason: nil, ageMinutes: nil,
                                      refreshMinutes: surface?.refreshMinutes
                                          ?? Self.defaultRefreshMinutes,
                                      paused: false)
        }
    }

    /// The summary is the CLI's board table. A one-board scope narrows it and
    /// pays for a second read only for the running titles that size draws.
    private static func readBoards(client: WidgetDataClient,
                                   board: String?) async -> Result<KanbanBoardTable, WidgetDataFailure> {
        let requested = board.map { [$0] } ?? []
        switch await client.mobileSummary(boards: requested) {
        case .failure(let failure):
            return .failure(failure)
        case .success(let summary):
            let result = KanbanBoardRow.readResult(from: summary)
            guard case .success(var table) = result else { return result }
            if let board, !board.isEmpty,
               case .success(let running) = await client.kanbanRunning() {
                table.runningTitles = (running.tasks ?? [])
                    .filter { $0.board == board }
                    .compactMap { $0.title }
            }
            return .success(table)
        }
    }
}

// MARK: - Widget

struct KanbanMonitorWidget: Widget {
    let kind = "KanbanMonitorWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: KanbanMonitorTimelineProvider()) { entry in
            KanbanMonitorWidgetView(entry: entry)
                .containerBackground(.fill.tertiary, for: .widget)
        }
        .configurationDisplayName("Boards")
        .description("Open cards by board: running, todo and blocked, plus the worker slots. Read-only.")
        #if os(iOS)
        .supportedFamilies([.accessoryInline, .accessoryCircular, .accessoryRectangular,
                            .systemSmall, .systemMedium, .systemLarge])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge])
        #endif
    }
}

struct KanbanMonitorWidgetView: View {
    let entry: KanbanMonitorEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            if !entry.configured || entry.paused {
                Text(entry.shortGateLabel).font(NativeStyle.small())
            } else {
                Text(entry.inlineLine).font(NativeStyle.small())
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
    let entry: KanbanMonitorEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Boards")
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

struct KanbanMonitorErrorPane: View {
    let failure: WidgetDataFailure
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(NativeStyle.bad)
                Text("board read failed")
                    .font(NativeStyle.label())
                    .foregroundStyle(.primary)
            }
            Text(detailed ? failure.operatorLine : failure.shortLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .lineLimit(detailed ? 3 : 2)
            Text("counts unknown")
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

private struct BoardsLegend: View {
    var body: some View {
        HStack(spacing: 8) {
            legend("running", NativeStyle.accent)
            legend("todo", Color.secondary)
            legend("blocked", NativeStyle.warn)
        }
        .font(NativeStyle.small())
        .foregroundStyle(.secondary)
        .lineLimit(1)
    }

    private func legend(_ label: String, _ color: Color) -> some View {
        HStack(spacing: 3) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(color)
                .frame(width: 6, height: 6)
            Text(label)
        }
    }
}

/// A B3 bar: the capsule track is the largest open count; the filled part is
/// this board's open cards, split running / todo / blocked. Length is
/// proportional to open cards, so a growing orange segment stands out.
private struct StackedBoardBar: View {
    let row: KanbanBoardRow
    let maxOpen: Int
    var height: CGFloat = 8

    var body: some View {
        GeometryReader { geometry in
            let total = row.openCards
            let width = maxOpen > 0
                ? geometry.size.width * CGFloat(total) / CGFloat(maxOpen)
                : 0
            HStack(spacing: 0) {
                segment(row.running ?? 0, total: total, width: width, color: NativeStyle.accent)
                segment(row.todo ?? 0, total: total, width: width, color: Color.secondary)
                segment(row.blocked ?? 0, total: total, width: width, color: NativeStyle.warn)
            }
            .frame(width: max(2, width), alignment: .leading)
            .background(Capsule().fill(.quaternary))
            .clipShape(Capsule())
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }

    @ViewBuilder
    private func segment(_ value: Int, total: Int, width: CGFloat,
                         color: Color) -> some View {
        if value > 0, total > 0 {
            Rectangle()
                .fill(color)
                .frame(width: width * CGFloat(value) / CGFloat(total))
        }
    }
}

private struct BoardBarRow: View {
    let row: KanbanBoardRow
    let maxOpen: Int
    var showDone = true

    var body: some View {
        HStack(spacing: 8) {
            Text(row.board)
                .font(NativeStyle.small())
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: 100, alignment: .leading)
            StackedBoardBar(row: row, maxOpen: maxOpen, height: 10)
            if showDone {
                Text("\(row.done ?? 0) done")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .frame(width: 54, alignment: .trailing)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLine)
    }

    private var accessibilityLine: String {
        "\(row.board): \(row.openCards) open, \(row.running ?? 0) running, "
            + "\(row.todo ?? 0) todo, \(row.blocked ?? 0) blocked, "
            + "\(row.done ?? 0) done today"
    }
}

private struct BoardsHeader: View {
    let entry: KanbanMonitorEntry
    var title: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(title)
                .font(NativeStyle.label())
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
            AsOfStamp(date: entry.dataAt)
        }
    }
}

private struct NoOpenCards: View {
    var body: some View {
        Text("No open cards")
            .font(NativeStyle.small())
            .foregroundStyle(.secondary)
    }
}

/// The worker-slot strip: one segment per server-advertised slot. The cap is
/// the API's `max_in_progress`, never a literal in this file.
private struct WorkerSlotStrip: View {
    let running: Int
    let cap: Int?

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let cap, cap > 0 {
                HStack(spacing: 3) {
                    ForEach(0..<min(cap, 24), id: \.self) { index in
                        RoundedRectangle(cornerRadius: 2)
                            .fill(index < running ? NativeStyle.accent : Color.secondary.opacity(0.2))
                            .frame(height: 8)
                    }
                }
                Text("\(running) of \(cap) slots")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            } else {
                Text("\(running) running")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
    }
}

// MARK: - One-board scope

/// The B4 half of B3: one board's stacked bar, its column counts and the
/// running card titles. `family` trims the list to what the size can hold.
private struct BoardScopeBody: View {
    let entry: KanbanMonitorEntry
    let row: KanbanBoardRow
    let family: WidgetFamily

    private var titleLimit: Int {
        switch family {
        case .systemSmall: return 2
        case .systemLarge, .systemExtraLarge: return 8
        default: return 3
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            BoardsHeader(entry: entry, title: entry.scopeBoard ?? row.board)
            StackedBoardBar(row: row, maxOpen: max(row.openCards, 1), height: 8)
            HStack(spacing: 10) {
                count(row.running, "run", NativeStyle.accent)
                count(row.todo, "todo", Color.secondary)
                count(row.blocked, "blk", NativeStyle.warn)
                Spacer(minLength: 0)
                count(row.done, "done", Color.secondary)
            }
            if family == .systemSmall {
                if let first = entry.runningTitles.first {
                    Text(first)
                        .font(NativeStyle.small())
                        .lineLimit(1)
                }
            } else {
                if entry.runningTitles.isEmpty {
                    Text("Nothing running")
                        .font(NativeStyle.small())
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(Array(entry.runningTitles.prefix(titleLimit)), id: \.self) { title in
                        Text("▸ \(title)")
                            .font(NativeStyle.small())
                            .lineLimit(1)
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .stale(entry.staleState)
    }

    private func count(_ value: Int?, _ label: String, _ color: Color) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("\(value ?? 0)")
                .font(NativeStyle.number(16))
                .foregroundStyle(color)
            Text(label)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Accessory families

struct KanbanMonitorCircular: View {
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            Text(entry.shortGateLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else if let failure = entry.failure {
            Text(failure.shortLabel)
                .font(NativeStyle.small())
                .foregroundStyle(.primary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
        } else {
            L2Gauge(value: entry.slotGaugeValue,
                    range: entry.slotGaugeRange,
                    valueText: entry.slotValueText,
                    caption: entry.slotCaption,
                    label: "Boards, \(entry.slotValueText) \(entry.slotCaption)")
                .stale(entry.staleState)
        }
    }
}

struct KanbanMonitorRectangular: View {
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            KanbanMonitorGateView(entry: entry)
        } else if let failure = entry.failure {
            VStack(alignment: .leading, spacing: 1) {
                Text("board read failed")
                    .font(NativeStyle.small())
                Text(failure.shortLabel)
                    .font(NativeStyle.small())
                Text(entry.updatedLine)
                    .font(NativeStyle.small())
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.rectangularHeadline)
                    .font(NativeStyle.label())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .widgetAccentable()
                Text(entry.rectangularLine)
                    .font(NativeStyle.small())
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(entry.updatedLine)
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .stale(entry.staleState)
        }
    }
}

// MARK: - Home Screen families

struct SmallKanbanMonitor: View {
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            KanbanMonitorGateView(entry: entry)
        } else if let failure = entry.failure {
            KanbanMonitorErrorPane(failure: failure)
        } else if let row = entry.scopedRow {
            BoardScopeBody(entry: entry, row: row, family: .systemSmall)
        } else {
            smallBody
        }
    }

    private var smallBody: some View {
        VStack(alignment: .leading, spacing: 6) {
            BoardsHeader(entry: entry, title: "Open cards")
            Spacer(minLength: 0)
            if entry.rows.isEmpty {
                NoOpenCards()
            } else {
                ForEach(entry.topBoards) { row in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 4) {
                            Text(row.board)
                                .font(NativeStyle.small())
                                .lineLimit(1)
                            Spacer(minLength: 0)
                            Text("\(row.openCards)")
                                .font(NativeStyle.number())
                        }
                        StackedBoardBar(row: row, maxOpen: entry.maxOpenCards, height: 6)
                    }
                }
            }
            Spacer(minLength: 0)
            if let partial = entry.partialLine {
                Text(partial)
                    .font(NativeStyle.small())
                    .foregroundStyle(NativeStyle.warn)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .stale(entry.staleState)
    }
}

struct MediumKanbanMonitor: View {
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            KanbanMonitorGateView(entry: entry)
        } else if let failure = entry.failure {
            KanbanMonitorErrorPane(failure: failure)
        } else if let row = entry.scopedRow {
            BoardScopeBody(entry: entry, row: row, family: .systemMedium)
        } else {
            mediumBody
        }
    }

    private var mediumBody: some View {
        VStack(alignment: .leading, spacing: 7) {
            BoardsHeader(entry: entry, title: "Open cards by board")
            if entry.rows.isEmpty {
                Spacer(minLength: 0)
                NoOpenCards()
                Spacer(minLength: 0)
            } else {
                ForEach(entry.topBoards) { row in
                    BoardBarRow(row: row, maxOpen: entry.maxOpenCards)
                }
            }
            Spacer(minLength: 0)
            HStack(spacing: 6) {
                BoardsLegend()
                Spacer(minLength: 0)
                if let partial = entry.partialLine {
                    Text(partial)
                        .font(NativeStyle.small())
                        .foregroundStyle(NativeStyle.warn)
                        .lineLimit(1)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .stale(entry.staleState)
    }
}

struct LargeKanbanMonitor: View {
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            KanbanMonitorGateView(entry: entry)
        } else if let failure = entry.failure {
            KanbanMonitorErrorPane(failure: failure, detailed: true)
        } else if let row = entry.scopedRow {
            BoardScopeBody(entry: entry, row: row, family: .systemLarge)
        } else {
            largeBody
        }
    }

    private var largeBody: some View {
        VStack(alignment: .leading, spacing: 7) {
            BoardsHeader(entry: entry, title: "Boards")
            if entry.rows.isEmpty {
                Spacer(minLength: 0)
                NoOpenCards()
                Spacer(minLength: 0)
            } else {
                ForEach(Array(entry.rows.prefix(KanbanMonitorEntry.rowLimit(for: .systemLarge)))) { row in
                    BoardBarRow(row: row, maxOpen: entry.maxOpenCards)
                }
            }
            Spacer(minLength: 0)
            WorkerSlotStrip(running: entry.runningTotal, cap: entry.maxInProgress)
            HStack(spacing: 6) {
                Text("\(KanbanMonitorReading.text(entry.doneTotal)) done today · "
                     + "\(KanbanMonitorReading.text(entry.blockedTotal)) blocked")
                    .font(NativeStyle.small())
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                Spacer(minLength: 0)
                BoardsLegend()
            }
            if let partial = entry.partialLine {
                Text(partial)
                    .font(NativeStyle.small())
                    .foregroundStyle(NativeStyle.warn)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .stale(entry.staleState)
    }
}
