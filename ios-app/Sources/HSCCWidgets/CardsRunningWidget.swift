import Foundation
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// CardsRunningWidget.swift — what is moving, and who is moving it.
//
// The phone answer to `hscc cards monitor --live`. That view prints one panel
// per running card and its header is `board · card · worker`, then run time,
// heartbeat age and the worker session's tokens, dropping fields right-to-left
// as the terminal narrows (`hscc_daemon/cards_live.py`, module docstring). A
// widget is the narrowest terminal there is, so it keeps the same three fields
// in the same order — board, card, worker — plus the one age it can actually
// know (see AGE below).
//
// `docs/WIDGET_DATA_CONTRACT.md` §5.4 is the spec and §8.3 the rule: a card
// list comes from the kanban trio, never `/v1/cards`. The reason is measured,
// not stylistic — `/v1/cards` has no field projection, so 8 rows cost 81,855 B
// of which 98.6 % is discarded (`body` alone is 81 %), while
// `/v1/kanban/running` sends the same rows already projected
// (`{board,id,title,assignee,status,started_at}`) for ~2 KB. This file therefore
// reads exactly TWO routes, which is also §2's per-refresh ceiling:
//
//   1. `GET /v1/kanban/running` — the rows and the server's own `count`.
//      Required: without it there is nothing to draw.
//   2. `GET /v1/kanban/blocked` — §5.4's "the failure rows". It is not a second
//      list, it is the answer to "the running list is empty, is the board idle,
//      or is everything stuck?". A widget that cannot answer that reads an idle
//      fleet and a jammed one the same way, so its absence nulls the blocked
//      number (`—`) rather than failing the surface.
//
// AGE — what this file does and does not know. `hscc cards monitor --live`
// prints TWO ages: run time and heartbeat age. Only the first is in this
// payload: `/v1/kanban/running` carries `started_at` (ISO-8601 UTC, from
// `kill_switch._started_iso`) and no heartbeat field at all, so a row renders
// minutes-since-start and never renders a heartbeat age it cannot know — a
// heartbeat dot would be decoration claiming a fact. `started_at` missing or
// unparseable draws `—`, never `0m` (§6 rule 1, one step further than zero:
// an unparseable timestamp is not a card that started this instant).
//
// THE LOUD FAILURE (§5.4 + the card's own acceptance rule: a blind read must
// never look like an idle board). Three distinct states, never collapsed:
//
//   * not configured       → the setup invite, checked FIRST in every family
//                            (the audit-3 invariant `widget_view_dispatch_check`
//                            enforces for the cluster widget).
//   * read failed, nothing
//     usable               → the failure, rendered: `exclamationmark.triangle.fill`
//                            in `Theme.Semantic.bad` plus
//                            `WidgetDataFailure.shortLabel` / `.operatorLine`.
//                            No count, no row, and above all no `0 running` —
//                            `runningText` returns nil with no projection
//                            precisely so a zero cannot be produced from a blind
//                            read.
//   * read failed, last-known
//     snapshot             → the rows AND their age AND why this refresh did not
//                            answer, in `Theme.Semantic.warn`. Stale is not quiet.
//
// STORAGE (§7): `CardsRunningProjection` only — ids, board slugs, titles, worker
// names, statuses, minutes. No `body`, no `workspace_path`, no `branch`, no
// address, no token, no raw response `Data`, and writes go through
// `SharedStore.suite` (never a `.standard` fallback). Card titles are sanctioned
// widget copy (§5.3 budgets `tasks[].title` for every family); card prose is not,
// and `RunningCardLite` never decodes it.
//
// No configuration parameter, so no `AppIntentConfiguration`: the fleet-wide
// running set is one question with one answer, and a picker that cannot narrow
// the route (measured: `?board=` is ignored by `/v1/kanban/running`, §4.3) would
// offer a choice the network cannot honour.
// ---------------------------------------------------------------------------

// MARK: - The projection (what is stored, and what the views draw)

/// One running card, narrowed to the five fields a row draws.
///
/// Deliberately NOT `RunningCardLite`: that is the wire shape, it owns `pid`-
/// adjacent keys the pivot dropped, and storing it would put a payload type in
/// the App Group container, which §7 forbids. The mapping below is the whole
/// projection — five scalars, nothing nested, nothing that can carry prose.
struct CardsRunningRow: Codable, Equatable, Identifiable {
    /// Board slug, or nil when the row did not carry one.
    let board: String?
    /// The card id (`t_…`), rendered monospaced — it is machine-produced.
    let cardID: String
    /// The card title. Human-written, so it renders in the system font.
    let title: String?
    /// The worker profile running it — `assignee` on the wire, `worker` in
    /// `hscc cards monitor --live`'s header. They name the same thing.
    let worker: String?
    let status: String?
    /// Minutes since `started_at`, computed once at fetch time. nil = the row
    /// had no usable `started_at`, which draws `—`.
    let ageMinutes: Int?

    /// Board-qualified, because the same card id on two boards is two rows and
    /// a `ForEach` id must not collide.
    var id: String { "\(board ?? "?")/\(cardID)" }

    init(board: String?, cardID: String, title: String?, worker: String?,
         status: String?, ageMinutes: Int?) {
        self.board = board
        self.cardID = cardID
        self.title = title
        self.worker = worker
        self.status = status
        self.ageMinutes = ageMinutes
    }

    /// Project one wire row. `startedAt` is the raw `started_at` string, parsed
    /// here so the parse failure is visible as a nil age rather than a crash.
    init(task: RunningCardLite, now: Date) {
        board = task.board
        cardID = task.id
        title = task.title
        worker = task.assignee
        status = task.status
        ageMinutes = Self.age(from: task.started_at, now: now)
    }

    /// The row's headline: its title, or its id when the card sent no title.
    /// An id is a real identifier; a placeholder word would be a guess.
    func headline(max: Int) -> String {
        let raw = (title?.isEmpty == false) ? title! : cardID
        return Self.truncate(raw, max: max)
    }

    /// The worker, or `—` ("nobody was named"). Never the board slug: that
    /// would let a row read as if a board were a worker.
    var workerText: String {
        guard let worker, !worker.isEmpty else { return "—" }
        return worker
    }

    /// `—` when unknown; `12m` under an hour; `2h07m` above it.
    var ageText: String {
        guard let minutes = ageMinutes else { return "—" }
        if minutes < 60 { return "\(minutes)m" }
        return String(format: "%dh%02dm", minutes / 60, minutes % 60)
    }

    static func truncate(_ value: String, max: Int) -> String {
        guard max > 1, value.count > max else { return value }
        return String(value.prefix(max - 1)) + "…"
    }

    /// Minutes since an ISO-8601 `started_at`, floored at zero.
    ///
    /// Two formatters because the server builds this string with
    /// `datetime.fromtimestamp(int(ts), utc).isoformat()`
    /// (`kill_switch._started_iso`), which is second-precision today; the
    /// fractional variant is the same string the day a producer keeps
    /// microseconds, and silently failing to parse it is exactly how a row
    /// starts claiming `—` for every card.
    static func age(from startedAt: String?, now: Date) -> Int? {
        guard let startedAt, !startedAt.isEmpty else { return nil }
        guard let started = Self.dateParser.date(from: startedAt)
            ?? Self.fractionalParser.date(from: startedAt)
        else { return nil }
        // A timestamp ahead of this clock (skew between the daemon host and the
        // phone) is "just started", not a negative age.
        return max(0, Int(now.timeIntervalSince(started) / 60))
    }

    private static let dateParser: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let fractionalParser: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}

/// The whole surface's stored state: the rows, and the two numbers.
///
/// `runningCount` and `blockedCount` are OPTIONAL and that optionality is the
/// point (§6 rule 1): `nil` means "this refresh was not told", which draws `—`,
/// while `0` means "the server counted none", which draws `0`. A struct with
/// `let runningCount: Int` would have to invent a zero for a read that answered
/// nothing, which is the failure this card exists to avoid.
struct CardsRunningProjection: Codable, Equatable {
    /// Longest-running first (§5.3's sort, applied to run time), capped at
    /// `rowCap` — the projection is stored, so the cap is what keeps a 40-card
    /// fleet from turning the App Group blob into the card list §7 refuses.
    var rows: [CardsRunningRow]
    /// The route's own `count`. nil = the payload carried no count.
    var runningCount: Int?
    /// From `/v1/kanban/blocked`. nil = that read did not answer this refresh.
    var blockedCount: Int?
    /// True when the stored rows are the whole running set (rows ≥ `count`), so
    /// a family may say "nothing else"; false means a subset is shown and the
    /// footer has to say so.
    var rowsAreComplete: Bool
    /// Whether the blocked number came from a row list or from that route's own
    /// `count` — kept so the footer can be exact about what was counted.
    var blockedFromRows: Bool

    /// The designated init, written out because the payload init below removes
    /// the implicit memberwise one — and the `placeholder(in:)` fixture needs it.
    init(rows: [CardsRunningRow], runningCount: Int?, blockedCount: Int?,
         rowsAreComplete: Bool, blockedFromRows: Bool) {
        self.rows = rows
        self.runningCount = runningCount
        self.blockedCount = blockedCount
        self.rowsAreComplete = rowsAreComplete
        self.blockedFromRows = blockedFromRows
    }

    /// The most a `large` family draws, and so the most worth storing (§5.4's
    /// large row: 8).
    static let rowCap = 8

    /// Live projection from the two reads. `blocked` nil = that read failed,
    /// which nulls one number rather than the whole surface.
    init(running: KanbanRunningLite, blocked: KanbanBlockedLite?, now: Date) {
        let tasks = running.tasks ?? []
        let projected = tasks.map { CardsRunningRow(task: $0, now: now) }
        // Longest run first; an unknown age is not "the oldest", so it sorts
        // last rather than to the top of the list.
        rows = Array(projected.sorted { lhs, rhs in
            (lhs.ageMinutes ?? -1) > (rhs.ageMinutes ?? -1)
        }.prefix(Self.rowCap))
        // The server's `count` is the number; when it is missing, the rows this
        // payload did send are the only evidence, and counting them is honest
        // only when nothing was withheld — hence `rowsAreComplete` below.
        runningCount = running.count ?? tasks.count
        if let sent = running.count {
            rowsAreComplete = tasks.count >= sent
        } else {
            rowsAreComplete = true
        }
        if let blocked {
            let sent = blocked.tasks?.count
            blockedCount = blocked.count ?? sent
            blockedFromRows = (blocked.count == nil) && (sent != nil)
        } else {
            blockedCount = nil
            blockedFromRows = false
        }
    }

    /// The `placeholder(in:)` fixture: plausible, clearly not a real cluster,
    /// and the same shape a live entry takes so every family can be eyeballed.
    static let preview = CardsRunningProjection(
        rows: [
            CardsRunningRow(board: "hscc", cardID: "t_a873a0a2",
                            title: "Widget / Live Activity data contract audit",
                            worker: "ios-engineer", status: "running", ageMinutes: 14),
            CardsRunningRow(board: "radio", cardID: "t_455e857f",
                            title: "Cards monitor panels",
                            worker: "reviewer", status: "running", ageMinutes: 52),
            CardsRunningRow(board: "hscc", cardID: "t_d045e612",
                            title: "Heartbeat age in the panel header",
                            worker: "researcher", status: "claimed", ageMinutes: nil),
        ],
        runningCount: 3, blockedCount: 1, rowsAreComplete: true, blockedFromRows: false
    )

    /// Whether any number at all is drawable. False means draw the failure.
    var hasNumbers: Bool { runningCount != nil || blockedCount != nil || !rows.isEmpty }

    var isIdle: Bool { (runningCount ?? 0) == 0 }

    var isStuck: Bool { (blockedCount ?? 0) > 0 }
}

// MARK: - The timeline entry

struct CardsRunningEntry: TimelineEntry {
    let date: Date
    /// nil = nothing usable came back; `failure` then says why, and no family
    /// may draw a count.
    let projection: CardsRunningProjection?
    /// Set only for stale-while-error: minutes since the stored rows were read.
    let ageMinutes: Int?
    /// Why there are no live numbers. Rendered, never swallowed.
    let failure: WidgetDataFailure?
    /// False only when the operator has no host/token yet — the setup invite.
    let configured: Bool

    static let sample = CardsRunningEntry(date: .now, projection: .preview,
                                          ageMinutes: nil, failure: nil, configured: true)

    static func unconfigured() -> CardsRunningEntry {
        CardsRunningEntry(date: .now, projection: nil, ageMinutes: nil,
                          failure: .notConfigured, configured: false)
    }

    // MARK: Text the families draw (one rule per string, so six families cannot
    // disagree about what the same entry means)

    /// `accessoryInline`: one short line, counts only (§5.4's inline row is
    /// "counts only — use row 2/3"). A long variant drops the board-free
    /// commentary rather than the numbers.
    var inlineLine: String {
        if !configured { return "Set up in HSCC" }
        guard let projection, projection.hasNumbers else { return failure?.shortLabel ?? "No data" }
        var parts: [String] = []
        if let running = projection.runningCount { parts.append("\(running) running") }
        if let blocked = projection.blockedCount, blocked > 0 { parts.append("\(blocked) blocked") }
        guard !parts.isEmpty else { return "idle" }
        let line = parts.joined(separator: " · ")
        return line.count <= 25 ? line : (parts.first ?? line)
    }

    /// The `accessoryCircular` scalar: running is the number this surface is
    /// about. nil draws the glyph in `CardsRunningCircularView`, never `0`.
    var circularNumber: Int? { projection?.runningCount }

    /// The ring's colour is the question a complication answers: is the fleet
    /// working, is something stuck, or did we not get to look?
    enum Glance { case idle, working, stuck, unreadable, stale, invite }

    var circularGlance: Glance {
        if !configured { return .invite }
        guard let projection else { return .unreadable }
        if ageMinutes != nil { return .stale }
        return projection.isStuck ? .stuck : (projection.isIdle ? .idle : .working)
    }

    /// The one row `accessoryRectangular` draws, when there is one.
    var headlineRow: CardsRunningRow? { projection?.rows.first }

    /// At most `limit` rows, longest-running first (already sorted in the
    /// projection, so a family cannot reorder the truth).
    func rows(limit: Int) -> [CardsRunningRow] {
        Array((projection?.rows ?? []).prefix(limit))
    }

    /// The header count: "3 running", or nil when the surface is blind. Every
    /// family draws this through one door, which is what makes "a blind read
    /// never renders `0 running`" a property of the type rather than of six
    /// views behaving themselves.
    var runningText: String? {
        guard let running = projection?.runningCount else { return nil }
        return "\(running) running"
    }

    /// The blocked count, or `—` when the failure read did not answer. A dash
    /// here is a fact about the read; a zero would be a claim about the fleet.
    var blockedText: String? {
        guard let projection else { return nil }
        guard let blocked = projection.blockedCount else { return "blocked —" }
        return "\(blocked) blocked"
    }

    /// The footer every home-screen family can show: the age, or the failure,
    /// or what the numbers do not cover.
    var footerLine: String {
        if !configured { return "Set up the app to see running cards." }
        if let age = ageMinutes {
            return "last known \(age) min ago · \(failure?.shortLabel ?? "no answer")"
        }
        if let failure { return failure.operatorLine }
        guard let projection else { return "no data" }
        var notes: [String] = []
        if !projection.rowsAreComplete, let total = projection.runningCount {
            notes.append("showing \(projection.rows.count) of \(total)")
        }
        if projection.blockedCount == nil { notes.append("blocked unreadable this refresh") }
        if notes.isEmpty { return "read just now" }
        return notes.joined(separator: " · ")
    }

    /// Whether the blocked number says someone is waiting. Used for colour only:
    /// a nil `blockedCount` is `false` here and draws a dash, so an unreadable
    /// read never colours a surface red.
    var isStuckGlance: Bool { projection?.isStuck ?? false }

    /// True when the failure must be drawn rather than a count. `projection`
    /// nil is the only blind state, and `failure` is non-nil in all of them —
    /// the views assert on this rather than re-deriving it.
    var showsFailureLoudly: Bool { configured && projection == nil }
}

// MARK: - The last-known snapshot (the stale-while-error half)

/// The projection + its age, in the App Group suite.
///
/// Fleet-wide, so one slot: unlike the board-status tile there is no scope to
/// key on (the route cannot be scoped — §4.3), and a snapshot of a different
/// question would not be last-known data for this one. The stored shape is
/// `CardsRunningProjection`, which declares five scalars per row and so cannot
/// hold a card body, a path, an address or a token even if a later card wanted
/// to persist one by accident.
enum CardsRunningSnapshotStore {
    private static let snapshotKey = "hscc.cardsrunning.snapshot"

    private struct Stored: Codable {
        let projection: CardsRunningProjection
        let at: Date
    }

    static func load() -> (value: CardsRunningProjection, at: Date)? {
        guard let data = SharedStore.suite?.data(forKey: snapshotKey),
              let stored = try? JSONDecoder().decode(Stored.self, from: data),
              stored.projection.hasNumbers else { return nil }
        return (stored.projection, stored.at)
    }

    /// Only a projection with at least one real number is worth keeping: an
    /// all-nil blob would later be offered back as "last known" and draw three
    /// dashes under an age, which reads as data.
    static func save(_ projection: CardsRunningProjection, at date: Date) {
        guard let defaults = SharedStore.suite, projection.hasNumbers else { return }
        guard let data = try? JSONEncoder().encode(Stored(projection: projection, at: date)) else { return }
        defaults.set(data, forKey: snapshotKey)
    }
}

// MARK: - Timeline provider

struct CardsRunningTimelineProvider: TimelineProvider {
    /// Cadence, not a poll. The operator's per-surface cadence lives in
    /// `AppGroupWidgetSettings`, and `WidgetSurfaceKind` has no case for this
    /// surface yet — honouring the shared toggle/cadence needs `SharedStore.swift`
    /// plus the app's config screen, i.e. another card's file set, so this widget
    /// uses the conservative quarter-hour and lets WidgetKit's budget have the
    /// last word. Same choice and same reason as the board-status widget.
    private static let refreshMinutes = 15

    func placeholder(in context: Context) -> CardsRunningEntry { .sample }

    func getSnapshot(in context: Context, completion: @escaping (CardsRunningEntry) -> Void) {
        Task { completion(await entry()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<CardsRunningEntry>) -> Void) {
        Task {
            let entry = await entry()
            let next = Calendar.current.date(byAdding: .minute, value: Self.refreshMinutes, to: .now)
                ?? .now.addingTimeInterval(900)
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    // MARK: Fetch

    private func entry() async -> CardsRunningEntry {
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else {
            // No host, no token: the ONE job here is the setup invite. The
            // snapshot stays out of it — `WidgetData.read` encodes that rule
            // (`.notConfigured` never falls back), and this early return keeps
            // the invite ahead of every other state in every family.
            return .unconfigured()
        }
        let outcome = await WidgetData.read(using: client,
                                           lastKnown: { CardsRunningSnapshotStore.load() },
                                           { c in await Self.readRunning(client: c) })
        switch outcome {
        case .live(let projection, let fetchedAt):
            CardsRunningSnapshotStore.save(projection, at: fetchedAt)
            return CardsRunningEntry(date: .now, projection: projection,
                                     ageMinutes: nil, failure: nil, configured: true)
        case .stale(let projection, let ageMinutes, let reason):
            return CardsRunningEntry(date: .now, projection: projection,
                                     ageMinutes: ageMinutes, failure: reason, configured: true)
        case .failed(let failure):
            return CardsRunningEntry(date: .now, projection: nil,
                                     ageMinutes: nil, failure: failure, configured: true)
        }
    }

    /// The two reads, in parallel, into one projection.
    ///
    /// Requests per refresh: TWO — §2's ceiling for a surface, and exactly the
    /// pairing §5.4 names ("pair with `/v1/kanban/running` for live workers and
    /// `/v1/kanban/blocked` for the failure rows"). `/v1/kanban/stale` is NOT
    /// read here: dataset 4's full open list is the board-status widget's job,
    /// and pulling it would buy rows this surface never draws.
    private static func readRunning(client: WidgetDataClient) async -> Result<CardsRunningProjection, WidgetDataFailure> {
        async let blockedRead = client.kanbanBlocked()
        let running = await client.kanbanRunning()
        let blocked = await blockedRead

        switch running {
        case .failure(let failure):
            // The rows are the surface. No last-known substitution happens here
            // — `WidgetData.read` owns that, and only for a failure whose
            // staleness is honest to show.
            return .failure(failure)

        case .success(let value):
            guard value.tasks != nil else {
                // A 2xx with no `tasks` array is not "the fleet is idle". It is
                // a payload this build cannot read a count out of, and drawing
                // `0 running` for it is the one failure this card was written to
                // refuse. `decoding` keeps the snapshot and says so instead.
                return .failure(.decoding(detail: "kanban/running carried no tasks array"))
            }
            // A blocked read that failed nulls one number; a blocked read that
            // answered is never allowed to fail the surface the rows came from.
            var blockedValue: KanbanBlockedLite?
            if case .success(let resolved) = blocked { blockedValue = resolved }
            return .success(CardsRunningProjection(running: value, blocked: blockedValue, now: Date()))
        }
    }
}

// MARK: - Widget + families

/// The Cards widget: the cards being worked right now, with their worker and
/// their run time.
struct CardsRunningWidget: Widget {
    let kind = "CardsRunningWidget"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: CardsRunningTimelineProvider()) { entry in
            CardsRunningWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    Theme.Semantic.surface
                }
        }
        .configurationDisplayName("Running Cards")
        .description("Cards being worked right now, with the worker running each and how long it has been going.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
    }
}

/// One entry, six families. Dispatch order is the audit-3 invariant: the
/// unconfigured invite is checked BEFORE any failure or stale state in every
/// family body, so an app that was never configured never says "can't reach the
/// cluster" (it is not the cluster's problem, and inviting setup is the fix).
struct CardsRunningWidgetView: View {
    let entry: CardsRunningEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryInline:
            Text(entry.inlineLine)
        case .accessoryCircular:
            CardsRunningCircularView(entry: entry)
        case .accessoryRectangular:
            CardsRunningRectangularView(entry: entry)
        case .systemLarge:
            CardsRunningLargeView(entry: entry)
        case .systemMedium:
            CardsRunningMediumView(entry: entry)
        default:
            CardsRunningSmallView(entry: entry)
        }
    }
}

// MARK: Accessory families

struct CardsRunningCircularView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured {
            CardsRunningInviteView(compact: true)
        } else if entry.projection == nil {
            // The blind state: a glyph, not a zero. `0` in this ring would read
            // "nothing is running", which is the opposite of what is known.
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(.body, design: .monospaced))
                .foregroundStyle(Theme.Semantic.bad)
        } else {
            VStack(spacing: 0) {
                Text(entry.circularNumber.map { "\($0)" } ?? "—")
                    .font(.system(.body, design: .monospaced))
                    .fontWeight(.semibold)
                    .foregroundStyle(Self.glanceColor(entry.circularGlance))
                Text(Self.glanceLabel(entry.circularGlance))
                    .font(.system(size: 8))
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    static func glanceColor(_ glance: CardsRunningEntry.Glance) -> Color {
        switch glance {
        case .invite, .unreadable: return Theme.Semantic.neutral
        case .stale: return Theme.Semantic.warn
        case .stuck: return Theme.Semantic.bad
        case .idle: return Theme.Semantic.neutral
        case .working: return Theme.Semantic.ok
        }
    }

    static func glanceLabel(_ glance: CardsRunningEntry.Glance) -> String {
        switch glance {
        case .invite: return "set up"
        case .unreadable: return "no data"
        case .stale: return "stale"
        case .stuck: return "blocked"
        case .idle: return "idle"
        case .working: return "running"
        }
    }
}

struct CardsRunningRectangularView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured {
            CardsRunningInviteView(compact: false)
        } else if entry.projection == nil {
            VStack(alignment: .leading, spacing: 1) {
                Label(entry.failure?.shortLabel ?? "No data",
                      systemImage: "exclamationmark.triangle.fill")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Theme.Semantic.bad)
                    .lineLimit(1)
                Text(entry.failure?.operatorLine ?? "")
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(2)
            }
        } else if let row = entry.headlineRow {
            // §5.4's rectangular row: one row, `title≤20`, plus status — with
            // the worker and the run time this surface is named for.
            VStack(alignment: .leading, spacing: 1) {
                Text(row.headline(max: 20))
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Text("\(row.workerText) · \(row.ageText)")
                    .font(.hsccMono(.caption2, weight: .medium))
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.runningText ?? "—")
                    .font(.hsccMono(.caption2, weight: .semibold))
                    .foregroundStyle(Theme.Semantic.onSurface)
                Text(entry.blockedText ?? "blocked —")
                    .font(.system(size: 9))
                    .foregroundStyle(entry.isStuckGlance ? Theme.Semantic.bad : Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

// MARK: Home Screen families

struct CardsRunningSmallView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured {
            CardsRunningInviteView(compact: false)
        } else if entry.projection == nil {
            CardsRunningFailureView(entry: entry, compact: true)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                CardsRunningHeaderView(entry: entry)
                // §5.4's small family: 2-3 rows. Two here, because the small
                // tile also carries the header count and the age footer, and a
                // third row would push the footer off the widget — the age is
                // not optional, rows are.
                CardsRunningRowList(rows: entry.rows(limit: 2), showBoard: false, titleMax: 32)
                Spacer(minLength: 0)
                CardsRunningFooterView(entry: entry)
            }
        }
    }
}

struct CardsRunningMediumView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured {
            CardsRunningInviteView(compact: false)
        } else if entry.projection == nil {
            CardsRunningFailureView(entry: entry, compact: false)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                CardsRunningHeaderView(entry: entry)
                // §5.4's medium family: 4 rows, with the board.
                CardsRunningRowList(rows: entry.rows(limit: 4), showBoard: true, titleMax: 40)
                Spacer(minLength: 0)
                CardsRunningFooterView(entry: entry)
            }
        }
    }
}

struct CardsRunningLargeView: View {
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured {
            CardsRunningInviteView(compact: false)
        } else if entry.projection == nil {
            CardsRunningFailureView(entry: entry, compact: false)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                CardsRunningHeaderView(entry: entry)
                // §5.4's large family: 7-8 rows with board and age. 8, which is
                // also the stored cap, so the footer's "showing N of M" and the
                // blob can never disagree.
                CardsRunningRowList(rows: entry.rows(limit: 8), showBoard: true, titleMax: 60)
                Spacer(minLength: 0)
                CardsRunningFooterView(entry: entry)
            }
        }
    }
}

// MARK: Parts

/// The count line: `3 running · 1 blocked`, with the blocked half drawn `—`
/// when its own read did not answer.
struct CardsRunningHeaderView: View {
    let entry: CardsRunningEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
            Text(entry.runningText ?? "—")
                .font(.hsccMono(.callout, weight: .semibold))
                .foregroundStyle(Theme.Semantic.onSurface)
                .lineLimit(1)
            if let blocked = entry.blockedText {
                Text(blocked)
                    .font(.hsccMono(.caption2, weight: .medium))
                    .foregroundStyle(entry.isStuckGlance ? Theme.Semantic.bad : Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
    }
}

/// One row per running card: `worker` and run time on the same line as the
/// title, board when there is room for it — `hscc cards monitor --live`'s
/// header order, dropped right-to-left exactly the way that view drops it.
struct CardsRunningRowList: View {
    let rows: [CardsRunningRow]
    let showBoard: Bool
    let titleMax: Int

    var body: some View {
        if rows.isEmpty {
            // Reached only on a LIVE entry that counted nothing: a real answer,
            // and the only place "nothing is running" may be drawn. The blind
            // state never gets here — its family body takes the failure branch.
            Label("Nothing running", systemImage: "circle.dashed")
                .font(.caption2)
                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                ForEach(rows) { row in
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
                        Circle()
                            .fill(Theme.cardStatusColor(row.status ?? ""))
                            .frame(width: 5, height: 5)
                        Text(row.headline(max: titleMax))
                            .font(.caption2)
                            .foregroundStyle(Theme.Semantic.onSurface)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        if showBoard, let board = row.board {
                            Text(board)
                                .font(.hsccMono(.caption2, weight: .regular))
                                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                                .lineLimit(1)
                        }
                        Text(row.workerText)
                            .font(.hsccMono(.caption2, weight: .medium))
                            .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                        Text(row.ageText)
                            .font(.hsccMono(.caption2, weight: .semibold))
                            .foregroundStyle(row.ageMinutes == nil ? Theme.Semantic.warn : Theme.Semantic.onSurface)
                    }
                }
            }
        }
    }
}

/// The age / failure line. Warn-coloured whenever what is on screen is not from
/// this refresh — a stale surface must not look like a live one at a glance.
struct CardsRunningFooterView: View {
    let entry: CardsRunningEntry

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xxs.rawValue) {
            if entry.ageMinutes != nil || entry.failure != nil {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(entry.ageMinutes != nil ? Theme.Semantic.warn : Theme.Semantic.bad)
            }
            Text(entry.footerLine)
                .font(.system(size: 9))
                .foregroundStyle(entry.ageMinutes != nil ? Theme.Semantic.warn
                                    : (entry.failure != nil ? Theme.Semantic.bad : Theme.Semantic.onSurfaceMuted))
                .lineLimit(2)
            Spacer(minLength: 0)
        }
    }
}

/// The blind state for the home-screen families: loud, named, and with no
/// number anywhere that could be read as "the fleet is quiet".
struct CardsRunningFailureView: View {
    let entry: CardsRunningEntry
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Label(entry.failure?.shortLabel ?? "No data",
                  systemImage: "exclamationmark.triangle.fill")
                .font(compact ? .subheadline.weight(.semibold) : .headline.weight(.semibold))
                .foregroundStyle(Theme.Semantic.bad)
                .lineLimit(2)
            Text(entry.failure?.operatorLine ?? "The cluster did not answer.")
                .font(.caption)
                .foregroundStyle(Theme.Semantic.onSurface)
            if !compact {
                // The one thing an operator can still trust from a blind tile:
                // what the last good read said, and how old it was. The rows
                // themselves are gone (there is no snapshot), so say plainly
                // that nothing here is current.
                Text("No running cards are shown because this read did not answer. A count of zero would be a guess.")
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            }
            Spacer(minLength: 0)
        }
    }
}

/// The unconfigured invite: what to do, not what broke (audit-3).
struct CardsRunningInviteView: View {
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            Label("Running cards", systemImage: "square.stack.3d.up")
                .font(compact ? .caption2 : .subheadline.weight(.semibold))
                .foregroundStyle(Theme.Semantic.onSurface)
                .lineLimit(1)
            Text("Set up the app to see running cards.")
                .font(.system(size: 9))
                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                .lineLimit(2)
        }
    }
}
