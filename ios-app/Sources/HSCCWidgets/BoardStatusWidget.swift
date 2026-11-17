import AppIntents
import Foundation
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// BoardStatusWidget.swift — one board, five numbers.
//
// `docs/WIDGET_DATA_CONTRACT.md` §5.2 is the spec: todo / ready / running /
// blocked / done for ONE board the operator picks in the widget's own
// configuration sheet, rendered across the Home Screen families and the three
// accessory families. Read-only, like every surface in the pivot: this file
// fetches through `WidgetDataClient` (GET-only, closed read allowlist) and
// mutates nothing.
//
// WHY A NEW WIDGET RATHER THAN AN EXTRA ROW ON `ClusterWidget`: the cluster
// widget answers "is the fleet up". This one answers "what does this board need
// from me", which is per-board and five numbers deep — the §7 hygiene rules make
// those two different projections with different lifetimes, so they get
// different stores and different timelines.
//
// THE `done` PROBLEM, AND WHAT THIS FILE DOES ABOUT IT (§5.2, §6 rule 1).
// `done` is not in any route an extension may read: `/v1/cards?status=done`
// answers with ZERO cards and the archived listing costs 8.6 MB, so a widget
// that derived counts from a card list would render `done: 0` forever — the
// honesty failure §5.2 refuses. `GET /v1/mobile/summary` (§6) counts it
// server-side and is the primary read here. It is not deployed on every cluster
// yet, so the read plan is two steps and the fallback is the §5.2-granted
// interim, not an invention of this file:
//
//   1. `mobileSummary(boards:)` — one request, all five numbers, real `done`.
//      `?boards=` scopes it (§6 rule 2); with no board chosen the route returns
//      every board and the tile is the sum of them, which is a real answer
//      rather than a guess.
//   2. Only when that produced no counts (404 on an older cluster, a degraded
//      §6-rule-7 response, or one board carrying its own `error` per §6 rule 4):
//      the interim §5.2 grants — `kanbanStale(olderThanDays: 0)` for todo/ready/
//      running (it carries every non-terminal card with its `status`), and
//      `kanbanBlocked()` for `blocked`, run in parallel with it. `blocked` cannot
//      come from the stale list: measured against the live daemon on 2026-11-17
//      it answered `count: 18` with 18 rows while `stale?older_than=0` answered
//      15 rows with no `blocked` status among them, so histogramming the stale
//      list would have drawn `blocked: 0` next to eighteen stuck cards. Board
//      scope is client-side because `?board=` is measured-ignored by these
//      routes (§4.3), and a list that sends fewer rows than its own `count`
//      refuses the read rather than under-counting.
//
// So: 1 request per refresh on a cluster with §6's route (the steady state), 3
// on one without it — exactly the three §5.2 authorises for the open-only
// subset, in two parallel round trips — and `done` renders `—`, never `0`, until
// the compact route lands. `WidgetDataPath` is the only way any of these URLs
// gets built, and all three of these paths are already in its allowlist.
//
// CONFIGURATION: `AppIntentConfiguration` + a `WidgetConfigurationIntent` with
// one `@Parameter` board, so the picker is the system's own (long-press the
// widget → Edit Widget) and needs no screen in the app — which matters while the
// console is being retired (`CONSOLE_RETIREMENT.md`). `AppEntity`/`EntityQuery`
// are conformed to BY HAND: this SDK's `@AppEntity` macro takes only
// `schema:` (the `name:` form is gone), and nothing here needs the schema bridge.
// The picker's rows come from `GET /v1/projects` — the sanctioned board-name
// source (§5.1: `{name, board}` only is decoded, `repo` never) — falling back to
// the slugs this device last saw, because a picker that is empty while the
// cluster is down is useless.
//
// CADENCE AND PAUSE (t_09cd8abf): this surface has a registry entry —
// `WidgetSurfaceKind.widgetBoardStatus` — so the app's "Widgets & Live
// Activities" screen owns both: the Active toggle and the cadence picker. A
// paused surface returns a `paused` entry BEFORE any read (zero requests, zero
// numbers, never a stale snapshot and never a blank) and an Off cadence yields
// `policy: .atEnd`, both matching `ClusterWidgetTimelineProvider`. Until this
// entry existed the widget hard-coded a quarter-hour and could not be paused at
// all; an install whose registry blob has never been written still gets the
// shared widget default (ON, 5 min), so adding the key cannot silently pause a
// widget that is already on a Home Screen.
//
// STORAGE (§7): the projection only — board slug, five optional ints, the
// source, and the fetch date. No response body, no card text, no path, no
// address, no token, and the writes go through `SharedStore.suite` (never a
// `.standard` fallback). A snapshot is only ever kept for the board it belongs
// to: last week's `hscc` numbers are not last-known data for `radio`.
// ---------------------------------------------------------------------------

// MARK: - The projection (what is stored, and what the views draw)

/// Where a set of numbers came from, which decides whether `done` is a number.
enum BoardStatusSource: String, Codable {
    /// §6's compact summary route: all five statuses, `done` included.
    case compact
    /// The §5.2 interim: open counts from `kanban/stale`, `done` unknown.
    case openOnly
}

/// The five numbers for one board (or for every board together).
///
/// Every status is OPTIONAL on purpose: `nil` means "this payload did not carry
/// that status", which draws `—`, while `0` means "the server counted none".
/// §6 rule 1 is exactly that distinction, and it is the reason
/// `BoardStatusCounts` (the shared wire type, all-required) is not reused as the
/// storage shape — the interim path genuinely does not know `done`.
struct BoardStatusProjection: Codable, Equatable {
    /// The board these numbers belong to; nil = every board together. Kept as a
    /// slug so a snapshot can never be handed to the wrong board's tile.
    let boardSlug: String?
    var todo: Int?
    var ready: Int?
    var running: Int?
    var blocked: Int?
    var done: Int?
    let source: BoardStatusSource

    /// What the header draws when no board was picked.
    static let allBoardsLabel = "All boards"

    /// The designated init. Written out rather than relying on the memberwise
    /// one, because the two payload inits below remove the implicit memberwise
    /// initializer — and the preview fixture needs one.
    init(boardSlug: String?, todo: Int?, ready: Int?, running: Int?,
         blocked: Int?, done: Int?, source: BoardStatusSource) {
        self.boardSlug = boardSlug
        self.todo = todo
        self.ready = ready
        self.running = running
        self.blocked = blocked
        self.done = done
        self.source = source
    }

    /// What `placeholder(in:)` draws: plausible numbers, clearly not a real
    /// cluster, and the same shape a live entry takes so every family can be
    /// eyeballed in the gallery. A preview fixture, never a stored state.
    static let preview = BoardStatusProjection(boardSlug: "hscc", todo: 17, ready: 2,
                                               running: 3, blocked: 0, done: 750, source: .compact)

    var displayName: String { boardSlug ?? Self.allBoardsLabel }

    /// Whether anything at all is drawable. False means draw the failure, not zeros.
    var hasNumbers: Bool { todo != nil || ready != nil || running != nil || blocked != nil || done != nil }

    /// The "N open" figure, only when all four operands are known — a partial
    /// sum would be a number that means something else.
    var openCount: Int? {
        guard let todo, let ready, let running, let blocked else { return nil }
        return todo + ready + running + blocked
    }

    /// True when `blocked` is `—` because its own read did not answer — the
    /// surface says so, rather than leaving a dash to be read as a design choice.
    var blockedUnknown: Bool { source == .openOnly && blocked == nil }

    /// The §6 tile for this board, if the payload carried one that is usable.
    /// nil = degraded (§6 rule 7), a board-level `error` (§6 rule 4), or the
    /// board simply was not in the response.
    init?(summary: MobileSummaryResponse, boardSlug: String?) {
        guard !summary.degraded else { return nil }
        source = .compact
        self.boardSlug = boardSlug
        if let slug = boardSlug {
            guard let tile = summary.board(slug), let counts = tile.counts, tile.error == nil else { return nil }
            todo = counts.todo
            ready = counts.ready
            running = counts.running
            blocked = counts.blocked
            done = counts.done
        } else {
            // Every board: sum the tiles that carry counts. A tile with an
            // `error` is skipped rather than counted as nothing.
            let usable = summary.entries.filter { $0.counts != nil && $0.error == nil }
            guard !usable.isEmpty else { return nil }
            todo = usable.reduce(0) { $0 + ($1.counts?.todo ?? 0) }
            ready = usable.reduce(0) { $0 + ($1.counts?.ready ?? 0) }
            running = usable.reduce(0) { $0 + ($1.counts?.running ?? 0) }
            blocked = usable.reduce(0) { $0 + ($1.counts?.blocked ?? 0) }
            done = usable.reduce(0) { $0 + ($1.counts?.done ?? 0) }
        }
    }

    /// §5.2's interim: the four OPEN numbers.
    ///
    /// `staleTasks` is `GET /v1/kanban/stale?older_than=0` — every non-terminal
    /// card, each with its own `status`, which is what the queue-depth and
    /// running numbers key on (measured 2026-11-17: `count` 15 for 15 rows, and
    /// its `running` rows agree with `GET /v1/kanban/running`'s `count` of 3).
    ///
    /// `blocked` does NOT come from that list, and that is a measured fact, not a
    /// guess: the same fleet answered `/v1/kanban/blocked` with `count: 18` and
    /// 18 rows while `stale?older_than=0` answered with 15 rows and **no
    /// `blocked` status among them**. Histogramming `blocked` out of the stale
    /// list would therefore have drawn a confident `0` next to eighteen stuck
    /// cards — the exact §6 rule 1 failure this dataset exists to avoid. So the
    /// blocked number comes from `/v1/kanban/blocked`: `blockedRows` scoped to
    /// the board, `blockedTotal` (the fleet-wide `count`) for the all-boards
    /// tile. Both nil — or a list shorter than its own `count`, i.e. truncated —
    /// means this refresh does not know `blocked`, and it draws `—`.
    ///
    /// `done` stays nil because no route an extension may read can count it
    /// (§5.2), and `—` is the only honest way to draw that.
    init(interimStale staleTasks: [StaleCardLite],
         blockedRows: [BlockedCardLite]?,
         blockedTotal: Int?,
         boardSlug: String?) {
        source = .openOnly
        self.boardSlug = boardSlug
        var todo = 0, ready = 0, running = 0
        for card in staleTasks {
            // `?board=` is IGNORED by the kanban routes (measured, §4.3), so the
            // scope happens here. A card whose `board` is nil is kept for the
            // all-boards tile and skipped for a named one — never guessed at.
            if let boardSlug, card.board != boardSlug { continue }
            switch card.status?.lowercased() {
            case "todo": todo += 1
            case "ready": ready += 1
            // `claimed`/`in_progress` are the same fact in another dialect — the
            // same folding `Theme.cardStatusColor` already does for colour.
            case "running", "claimed", "in_progress": running += 1
            // No `blocked` case on purpose: the route does not list them, and
            // counting a status it never sends is how a zero becomes a lie.
            default: break  // `review` and anything else are not one of the five
            }
        }
        self.todo = todo
        self.ready = ready
        self.running = running
        if let boardSlug {
            self.blocked = blockedRows?.reduce(0) { $0 + ($1.board == boardSlug ? 1 : 0) }
        } else {
            self.blocked = blockedTotal ?? blockedRows?.count
        }
        self.done = nil
    }
}

// MARK: - The timeline entry

struct BoardStatusEntry: TimelineEntry {
    let date: Date
    /// nil = every board together (the picker's default).
    let boardSlug: String?
    /// nil when nothing usable came back — `failure` then says why.
    let projection: BoardStatusProjection?
    /// Set only for stale-while-error: minutes since the stored numbers were fetched.
    let ageMinutes: Int?
    /// The reason there are no live numbers. Rendered, never swallowed.
    let failure: WidgetDataFailure?
    /// False only when the operator has no host/token yet — the setup invite.
    let configured: Bool
    /// True when the operator paused this surface in Settings (t_09cd8abf). A
    /// paused entry carries NO projection and NO failure: it is neither live
    /// data nor stale data nor an error, and drawing any of those instead of
    /// an honest "paused" is the failure mode `ClusterEntry.paused` exists to
    /// avoid. The widget cannot remove itself programmatically, so the paused
    /// pane IS the honest pause.
    let paused: Bool

    /// The fixture `placeholder` draws: plausible, clearly not a real cluster,
    /// and the same shape a live entry takes so the families can be eyeballed.
    static let sample = BoardStatusEntry(
        date: .now,
        boardSlug: "hscc",
        projection: .preview,
        ageMinutes: nil,
        failure: nil,
        configured: true,
        paused: false
    )

    static func unconfigured(boardSlug: String?) -> BoardStatusEntry {
        BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: nil,
                         ageMinutes: nil, failure: .notConfigured, configured: false,
                         paused: false)
    }

    /// The paused state: the slug it belongs to and nothing else. No
    /// projection, no snapshot, no failure — the operator asked for no data,
    /// so the surface shows none (and the provider never sends a request for
    /// it; see `BoardStatusTimelineProvider.entry`).
    static func paused(boardSlug: String?) -> BoardStatusEntry {
        BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: nil,
                         ageMinutes: nil, failure: nil, configured: true,
                         paused: true)
    }

    // MARK: Gate copy (unconfigured / paused)

    /// The line the gate pane draws in the Home Screen and rectangular
    /// families.
    var gateLine: String {
        paused ? "Paused in Settings" : "Set up the app to see this board"
    }

    /// The label the accessory families draw for a gate state. Unconfigured is
    /// checked FIRST — the audit-3 rule that an unconfigured app invites setup
    /// in every family, before a paused or failing state can be drawn.
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        // A paused accessory still says WHICH board is paused, inside the same
        // ~25-character budget as the live line, and drops the slug rather than
        // truncating it (the same rule `inlineLine` follows below).
        let withBoard = "\(titleText) · paused"
        return withBoard.count <= 25 ? withBoard : "Paused"
    }

    // MARK: Text the families draw (kept here so it is one rule per string)

    var titleText: String { boardSlug ?? BoardStatusProjection.allBoardsLabel }

    /// `accessoryInline`: one short line. §5.2 budgets ~25 characters, and a
    /// long slug is dropped rather than truncated — half a board name is
    /// ambiguous, the counts are not.
    var inlineLine: String {
        // One call for both gate states, and `shortGateLabel` itself puts the
        // setup invite first — an unconfigured app never reads as paused.
        if !configured || paused { return shortGateLabel }
        guard let projection = projection, projection.hasNumbers else { return failure?.shortLabel ?? "No data" }
        let counts = Self.countsShortLine(projection)
        let withBoard = "\(titleText) · \(counts)"
        return withBoard.count <= 25 ? withBoard : counts
    }

    /// `accessoryRectangular`'s second line and the small/medium footer: at most
    /// two operands, most urgent first. §5.2's example is "17 todo · 6 run";
    /// a nonzero `blocked` outruns everything, because it is the number that
    /// means "someone is waiting on you".
    var countsShortLine: String {
        guard let projection, projection.hasNumbers else { return failure?.shortLabel ?? "No data" }
        return Self.countsShortLine(projection)
    }

    private static func countsShortLine(_ projection: BoardStatusProjection) -> String {
        var parts: [String] = []
        if let blocked = projection.blocked, blocked > 0 { parts.append("\(blocked) blocked") }
        if let running = projection.running { parts.append("\(running) run") }
        if let todo = projection.todo { parts.append("\(todo) todo") }
        if let ready = projection.ready { parts.append("\(ready) ready") }
        if let done = projection.done, parts.isEmpty { parts.append("\(done) done") }
        guard !parts.isEmpty else { return "quiet" }
        return parts.prefix(2).joined(separator: " · ")
    }

    /// The number the `accessoryCircular` draws: §5.2 says one scalar, and the
    /// one worth a glance is what is stuck. Falls back to the open total when
    /// `blocked` was not in the payload.
    var circularNumber: Int? {
        if let blocked = projection?.blocked { return blocked }
        return projection?.openCount
    }

    /// The ring's colour is the one question a circular complication answers:
    /// does this board need me? (The five TILES use `Theme.cardStatusColor`,
    /// where a blocked card is amber — that is a status among many, and Theme
    /// documents the difference on purpose.)
    var circularIsUrgent: Bool { (projection?.blocked ?? 0) > 0 }

    /// The age / failure line every home-screen family can show.
    var footerLine: String {
        if !configured { return "Set up the app to see this board." }
        if let age = ageMinutes { return "last known \(age) min ago · \(failure?.shortLabel ?? "no answer")" }
        if let failure { return failure.operatorLine }
        // On the interim path a dash is a fact about the cluster, and the footer
        // names it: `done` needs the compact route, `blocked` needs its own read
        // to have answered. Silence here would let a `—` look like the design.
        if let projection, projection.source == .openOnly {
            var gaps: [String] = []
            if projection.done == nil { gaps.append("done needs /v1/mobile/summary on this cluster") }
            if projection.blockedUnknown { gaps.append("blocked unreadable this refresh") }
            if !gaps.isEmpty { return gaps.joined(separator: " · ") }
        }
        return "counts from this refresh"
    }

    /// The five tiles in the order the board reads them.
    var tiles: [BoardStatusTile] {
        guard let projection else { return BoardStatusTile.empty() }
        return BoardStatusTile.statuses.map { status in
            BoardStatusTile(status: status.label,
                            value: projection.value(for: status.key),
                            color: Theme.cardStatusColor(status.key))
        }
    }
}

/// One status tile. `value == nil` draws `—`, never `0` (§6 rule 1).
struct BoardStatusTile: Identifiable {
    let status: String
    let value: Int?
    let color: Color

    var id: String { status }

    /// The five statuses, in board order. One list so no family can quietly
    /// drop one (a widget that shows four of five is a widget that lies).
    static let statuses: [(key: String, label: String)] = [
        ("todo", "todo"),
        ("ready", "ready"),
        ("running", "run"),
        ("blocked", "blocked"),
        ("done", "done"),
    ]

    /// The no-data shape: five `—` tiles. The reason for the gap is the
    /// footer/header's job (it carries `WidgetDataFailure.shortLabel`), so a
    /// tile never invents its own copy.
    static func empty() -> [BoardStatusTile] {
        statuses.map { item in BoardStatusTile(status: item.label, value: nil, color: Theme.Semantic.neutral) }
    }
}

extension BoardStatusProjection {
    /// Look a status up by its key, so the tile list and the payload cannot
    /// drift apart silently.
    func value(for key: String) -> Int? {
        switch key {
        case "todo": return todo
        case "ready": return ready
        case "running": return running
        case "blocked": return blocked
        case "done": return done
        default: return nil
        }
    }
}

// MARK: - The last-known snapshot (the stale-while-error half)

/// The projection + its age, in the App Group suite, keyed by board.
///
/// Two shapes are stored and both are §7-clean: the numbers for one board (or
/// for all of them) with the date they were read, and the board slugs the board
/// picker offers while the cluster is unreachable. A board slug is a name the
/// contract calls public (§7), and neither shape can hold a card body, a path,
/// an address or a token.
enum BoardStatusSnapshotStore {
    private static let snapshotKey = "hscc.boardstatus.snapshot"
    private static let boardsKey = "hscc.boardstatus.boards"

    private struct Stored: Codable {
        let projection: BoardStatusProjection
        let at: Date
    }

    /// Keep the numbers for the board they belong to. A snapshot of another
    /// board is not "last known" for this one — it is someone else's board.
    static func load(boardSlug: String?) -> (value: BoardStatusProjection, at: Date)? {
        guard let data = SharedStore.suite?.data(forKey: snapshotKey),
              let stored = try? JSONDecoder().decode(Stored.self, from: data),
              stored.projection.boardSlug == boardSlug,
              stored.projection.hasNumbers else { return nil }
        return (stored.projection, stored.at)
    }

    static func save(_ projection: BoardStatusProjection, at date: Date) {
        guard let defaults = SharedStore.suite, projection.hasNumbers else { return }
        guard let data = try? JSONEncoder().encode(Stored(projection: projection, at: date)) else { return }
        defaults.set(data, forKey: snapshotKey)
    }

    /// Slugs the picker falls back to when the cluster is not answering.
    static func loadBoardSlugs() -> [String] {
        SharedStore.suite?.stringArray(forKey: boardsKey) ?? []
    }

    static func saveBoardSlugs(_ slugs: [String]) {
        guard let defaults = SharedStore.suite, !slugs.isEmpty else { return }
        defaults.set(Array(slugs.prefix(50)), forKey: boardsKey)
    }
}

// MARK: - Configuration: the board picker

/// One board in the picker. Hand-written `AppEntity` conformance — see the
/// header for why the macro is not used.
struct BoardStatusBoardEntity: AppEntity {
    static var typeDisplayRepresentation: TypeDisplayRepresentation = TypeDisplayRepresentation(name: "Board")
    static var defaultQuery = BoardStatusBoardQuery()

    let id: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(id)") }
}

/// Where the picker's rows come from: `GET /v1/projects` while the cluster
/// answers (its `{name, board}` pairs are the sanctioned board-name source,
/// §5.1), the last-known slugs when it does not.
struct BoardStatusBoardQuery: EntityQuery {
    func entities(for identifiers: [BoardStatusBoardEntity.ID]) async throws -> [BoardStatusBoardEntity] {
        identifiers.map { BoardStatusBoardEntity(id: $0) }
    }

    func suggestedEntities() async throws -> [BoardStatusBoardEntity] {
        let slugs = await liveBoardSlugs() ?? BoardStatusSnapshotStore.loadBoardSlugs()
        return slugs.map { BoardStatusBoardEntity(id: $0) }
    }

    /// nil = "no board picked", which the widget renders as every board together.
    func defaultResult() async -> BoardStatusBoardEntity? { nil }

    private func liveBoardSlugs() async -> [String]? {
        guard let client = WidgetDataClient.makeIfConfigured() else { return nil }
        guard case .success(let projects) = await client.projects() else { return nil }
        let slugs = Set(projects.projects.compactMap { $0.board }).sorted()
        guard !slugs.isEmpty else { return nil }
        BoardStatusSnapshotStore.saveBoardSlugs(Array(slugs))
        return Array(slugs)
    }
}

/// The widget's only configuration: which board.
struct BoardStatusConfigurationIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Board"
    static var description = IntentDescription("Pick the board whose counts this widget shows. Leave it unset for every board together.")

    @Parameter(title: "Board")
    var board: BoardStatusBoardEntity?
}

// MARK: - Timeline provider

struct BoardStatusTimelineProvider: AppIntentTimelineProvider {
    /// The cadence when the operator has never touched the settings screen:
    /// the shared widget default (`AppGroupWidgetSettings.setting(for:)` gives
    /// a self-fetching widget 5 minutes), not the quarter-hour this file
    /// hard-coded while `WidgetSurfaceKind` had no case for this surface.
    /// t_09cd8abf added `widget.boardStatus`, so the operator's choice now
    /// wins and this is only the fallback for a store that was never written.
    private static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: board_status_cadence
    /// The ONE cadence decision, kept free of WidgetKit so a gate can execute
    /// it: nil means Off — the timeline gets `policy: .atEnd` and the OS wakes
    /// the widget when it likes — and a date means `.after(date)`. Written
    /// inline, the Off branch (the one that becomes a refresh loop if it is
    /// wrong) would be the only branch in this file no gate ever runs.
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: board_status_cadence

    func recommendations() -> [AppIntentRecommendation<BoardStatusConfigurationIntent>] {
        // The Smart Stack suggestions have to exist without a network, so they
        // come from what this device has already seen.
        let known = Array(BoardStatusSnapshotStore.loadBoardSlugs().prefix(3))
        if known.isEmpty {
            return [AppIntentRecommendation(intent: BoardStatusConfigurationIntent(),
                                            description: BoardStatusProjection.allBoardsLabel)]
        }
        // `let`, not `var`: a `@Parameter` setter writes through the wrapper's
        // own box rather than mutating the struct, so this is a `let` as far as
        // the compiler is concerned (it warns on the `var`).
        return known.map { slug in
            let intent = BoardStatusConfigurationIntent()
            intent.board = BoardStatusBoardEntity(id: slug)
            return AppIntentRecommendation(intent: intent, description: slug)
        }
    }

    func placeholder(in context: Context) -> BoardStatusEntry { .sample }

    func snapshot(for configuration: BoardStatusConfigurationIntent,
                  in context: Context) async -> BoardStatusEntry {
        // The gallery preview reads the same registry entry the timeline does,
        // so a paused widget previews as paused instead of showing a live board
        // the operator explicitly turned off.
        await entry(for: configuration, surface: Self.surface())
    }

    func timeline(for configuration: BoardStatusConfigurationIntent,
                  in context: Context) async -> Timeline<BoardStatusEntry> {
        let surface = Self.surface()
        let entry = await entry(for: configuration, surface: surface)
        // 0 = Off in the cadence picker: no scheduled refresh, the OS decides.
        // Same shape as `ClusterWidgetTimelineProvider.getTimeline`.
        guard let next = Self.nextRefreshDate(
            refreshMinutes: surface?.refreshMinutes ?? Self.defaultRefreshMinutes
        ) else {
            return Timeline(entries: [entry], policy: .atEnd)
        }
        return Timeline(entries: [entry], policy: .after(next))
    }

    /// The operator's entry for THIS surface, or nil when the registry blob has
    /// never been written. `setting(for:)` fills in an absent surface with ON at
    /// the widget cadence, so nil here means "the app has never run its settings
    /// screen" — never "paused", which is why a fresh install still renders.
    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetBoardStatus)
    }

    // MARK: Fetch

    private func entry(for configuration: BoardStatusConfigurationIntent,
                       surface: WidgetSurfaceSettings?) async -> BoardStatusEntry {
        let boardSlug = configuration.board?.id
        // Paused is checked BEFORE the client is built and before any read: a
        // paused surface spends zero requests (§2's budget) and draws zero
        // numbers — no live counts, no last-known snapshot, no failure pane.
        // `!isActive` can only come from a blob the app wrote, so an install
        // that never opened Settings cannot land here.
        if let surface, !surface.isActive {
            return .paused(boardSlug: boardSlug)
        }
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else {
            // No host, no token: the ONE job here is the setup invite, and the
            // snapshot stays out of it — `WidgetData.read` encodes that rule
            // (`.notConfigured` never falls back), and this early return keeps
            // the invite ahead of every other state in every family.
            return .unconfigured(boardSlug: boardSlug)
        }

        let outcome = await WidgetData.read(using: client,
                                           lastKnown: { BoardStatusSnapshotStore.load(boardSlug: boardSlug) },
                                           { c in await Self.readCounts(client: c, boardSlug: boardSlug) })
        switch outcome {
        case .live(let projection, let fetchedAt):
            BoardStatusSnapshotStore.save(projection, at: fetchedAt)
            return BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: projection,
                                    ageMinutes: nil, failure: nil, configured: true, paused: false)
        case .stale(let projection, let ageMinutes, let reason):
            return BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: projection,
                                    ageMinutes: ageMinutes, failure: reason, configured: true, paused: false)
        case .failed(let failure):
            return BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: nil,
                                    ageMinutes: nil, failure: failure, configured: true, paused: false)
        }
    }

    /// §6's route first, §5.2's interim second.
    ///
    /// Requests per refresh: ONE on a cluster that has the compact route, three
    /// on one that does not (the summary attempt, then the two interim reads) —
    /// and §5.2 grants exactly that: "the board-status card may ship the
    /// open-only subset … from `/v1/kanban/{running,blocked,stale}` counts
    /// (3 reads, 11.7 KB)". The two interim reads run in parallel, so the interim
    /// costs two round trips of wall clock, not three. `/v1/mobile/summary`
    /// answered 404 live on 2026-11-17 (PR #310 is the server half), so the
    /// interim is the path that runs today; it self-heals to one read the moment
    /// that route deploys, because the summary is always attempted first.
    private static func readCounts(client: WidgetDataClient,
                                   boardSlug: String?) async -> Result<BoardStatusProjection, WidgetDataFailure> {
        switch await client.mobileSummary(boards: boardSlug.map { [$0] } ?? []) {
        case .success(let response):
            if let projection = BoardStatusProjection(summary: response, boardSlug: boardSlug) {
                return .success(projection)
            }
            // Degraded (§6 rule 7: `speak` and no `boards`, because empty counts
            // would read as "every board is quiet"), or our board carried its own
            // `error`. Neither is a transport failure, so the interim path is
            // tried before the surface gives up — and it may still know the open
            // four.
        case .failure:
            break  // 404 on a cluster without the route, unreachable, token bad: the interim path decides what to say
        }

        async let staleRead = client.kanbanStale(olderThanDays: 0)
        async let blockedRead = client.kanbanBlocked()
        let (stale, blocked) = await (staleRead, blockedRead)

        switch stale {
        case .failure(let failure):
            // The queue read is the one the interim cannot do without, so its
            // failure is the failure the surface renders.
            return .failure(failure)

        case .success(let value):
            guard let staleTasks = value.tasks else {
                // A 2xx with no `tasks` array is not "the board is empty", it is
                // a payload this build cannot read a count out of. `decoding`
                // keeps the last-known numbers instead of drawing zeros.
                return .failure(.decoding(detail: "kanban/stale carried no tasks array"))
            }
            guard !Self.isTruncated(returned: staleTasks.count, total: value.count) else {
                // Fewer rows than the envelope advertises means a subset, and a
                // subset histogram under-counts quietly. Keep the snapshot rather
                // than draw a number that is low by construction.
                return .failure(.decoding(detail: "kanban/stale sent fewer rows than its count"))
            }

            // `blocked` has its own read — see `init(interimStale:…)`, which
            // carries the measurement for why the stale list cannot answer it.
            // This read failing nils only the blocked tile (`—`), never the
            // whole board: the numbers that DID arrive are still true.
            var blockedRows: [BlockedCardLite]?
            var blockedTotal: Int?
            if case .success(let blockedValue) = blocked {
                let rows = blockedValue.tasks
                let truncated = rows.map { Self.isTruncated(returned: $0.count, total: blockedValue.count) } ?? false
                if !truncated {
                    blockedRows = rows
                    blockedTotal = blockedValue.count
                }
            }
            return .success(BoardStatusProjection(interimStale: staleTasks,
                                                  blockedRows: blockedRows,
                                                  blockedTotal: blockedTotal,
                                                  boardSlug: boardSlug))
        }
    }

    /// True when a list holds fewer rows than its own envelope claims — the
    /// shape of a truncated payload, where counting client-side under-reports.
    /// Measured today both routes send `count` == rows, so this is a guard for
    /// the day one of them starts capping, not a case in the current data.
    private static func isTruncated(returned: Int, total: Int?) -> Bool {
        guard let total else { return false }
        return returned < total
    }
}

// MARK: - Widget + families

/// The Board Status widget: five counts for one board.
struct BoardStatusWidget: Widget {
    let kind = "BoardStatusWidget"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: BoardStatusConfigurationIntent.self,
                               provider: BoardStatusTimelineProvider()) { entry in
            BoardStatusWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    Theme.Semantic.surface
                }
        }
        .configurationDisplayName("Board Status")
        .description("Todo, ready, running, blocked and done for one board you pick.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
    }
}

/// One entry, six families. The dispatch order is the audit-3 invariant the
/// cluster widget also obeys: the setup invite is checked BEFORE any failure or
/// stale state, so an unconfigured app never renders as "can't reach the cluster".
/// A paused entry (t_09cd8abf) joins the same gate step — it renders the
/// "Paused in Settings" pane, never a stale number and never a blank — and the
/// gate copy itself puts the setup invite first, so an unconfigured app cannot
/// read as "paused" either.
struct BoardStatusWidgetView: View {
    let entry: BoardStatusEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryInline:
            Text(entry.inlineLine)
        case .accessoryCircular:
            BoardStatusCircularView(entry: entry)
        case .accessoryRectangular:
            BoardStatusRectangularView(entry: entry)
        case .systemLarge:
            BoardStatusLargeView(entry: entry)
        case .systemMedium:
            BoardStatusMediumView(entry: entry)
        default:
            BoardStatusSmallView(entry: entry)
        }
    }
}

// MARK: Accessory families

struct BoardStatusCircularView: View {
    let entry: BoardStatusEntry

    var body: some View {
        if !entry.configured || entry.paused {
            // Same shape the cluster widget's circular takes for a gate state:
            // a label, not a number. A paused ring that still drew a count
            // would be exactly the false zero §6 rule 1 refuses.
            Text(entry.shortGateLabel)
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.shortGateLabel)
        } else {
            let color = entry.circularIsUrgent ? Theme.Semantic.bad : Theme.Semantic.ok
            VStack(spacing: 0) {
                Text(entry.circularNumber.map { "\($0)" } ?? "—")
                    .font(.system(.body, design: .monospaced))
                    .fontWeight(.semibold)
                    .foregroundStyle(color)
                Text(entry.circularIsUrgent ? "blocked" : "open")
                    .font(.system(size: 8))
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

struct BoardStatusRectangularView: View {
    let entry: BoardStatusEntry

    var body: some View {
        if !entry.configured || entry.paused {
            BoardStatusGateView(entry: entry)
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.titleText)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Text(entry.countsShortLine)
                    .font(.hsccMono(.caption2, weight: .medium))
                    .foregroundStyle(entry.circularIsUrgent ? Theme.Semantic.bad : Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
        }
    }
}

// MARK: Home Screen families

struct BoardStatusSmallView: View {
    let entry: BoardStatusEntry

    var body: some View {
        if !entry.configured || entry.paused {
            BoardStatusGateView(entry: entry)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                BoardStatusHeaderView(entry: entry)
                BoardStatusTileGrid(tiles: entry.tiles, columns: 3)
                Spacer(minLength: 0)
            }
        }
    }
}

struct BoardStatusMediumView: View {
    let entry: BoardStatusEntry

    var body: some View {
        if !entry.configured || entry.paused {
            BoardStatusGateView(entry: entry)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                BoardStatusHeaderView(entry: entry)
                BoardStatusTileGrid(tiles: entry.tiles, columns: 5)
                Text(entry.footerLine)
                    .font(.caption2)
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(2)
            }
        }
    }
}

struct BoardStatusLargeView: View {
    let entry: BoardStatusEntry

    var body: some View {
        if !entry.configured || entry.paused {
            BoardStatusGateView(entry: entry)
        } else {
            VStack(alignment: .leading, spacing: Theme.Spacing.md.rawValue) {
                BoardStatusHeaderView(entry: entry)
                // `large` has room for the five numbers read as rows, so each
                // one gets its own line and its own share of the open total.
                // Card ROWS deliberately do not live here: they would cost a
                // second read on every refresh (§2's budget), and the kanban
                // monitor widget is already the surface that lists cards.
                ForEach(entry.tiles) { tile in
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        Circle()
                            .fill(tile.color)
                            .frame(width: 7, height: 7)
                        Text(tile.status)
                            .font(.subheadline)
                            .foregroundStyle(Theme.Semantic.onSurface)
                        Spacer(minLength: Theme.Spacing.xs.rawValue)
                        Text(tile.value.map { "\($0)" } ?? "—")
                            .font(.hsccMono(.title3, weight: .semibold))
                            .foregroundStyle(tile.value == nil ? Theme.Semantic.onSurfaceMuted : Theme.Semantic.onSurface)
                    }
                }
                Spacer(minLength: 0)
                Text(entry.footerLine)
                    .font(.caption)
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

// MARK: Shared pieces

private struct BoardStatusHeaderView: View {
    let entry: BoardStatusEntry

    var body: some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: entry.circularIsUrgent ? "exclamationmark.triangle.fill" : "square.stack.3d.up")
                .font(.system(size: 10))
                .foregroundStyle(entry.circularIsUrgent ? Theme.Semantic.bad : Theme.Semantic.neutral)
            Text(entry.titleText)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.Semantic.onSurface)
                .lineLimit(1)
            Spacer(minLength: 0)
            if let age = entry.ageMinutes {
                Text("\(age) min")
                    .font(.hsccMono(.caption2, weight: .medium))
                    .foregroundStyle(Theme.Semantic.warn)
            }
        }
    }
}

private struct BoardStatusTileGrid: View {
    let tiles: [BoardStatusTile]
    let columns: Int

    var body: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: Theme.Spacing.xs.rawValue), count: columns),
                  spacing: Theme.Spacing.xs.rawValue) {
            ForEach(tiles) { tile in
                VStack(spacing: 1) {
                    Text(tile.value.map { "\($0)" } ?? "—")
                        .font(.hsccMono(.headline, weight: .semibold))
                        .foregroundStyle(tile.value == nil ? Theme.Semantic.onSurfaceMuted : tile.color)
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)
                    Text(tile.status)
                        .font(.system(size: 8))
                        .foregroundStyle(Theme.Semantic.onSurfaceMuted)
                        .lineLimit(1)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 2)
                .background(RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                    .fill(Theme.Semantic.tintOverlay(tile.color)))
            }
        }
    }
}

/// The gate a family draws when it has nothing to draw: the setup invite
/// (t_5c554c5b — the ONLY thing an unconfigured app may show) or the honest
/// paused state (t_09cd8abf). One pane, two copies, chosen by `entry.paused`
/// exactly as `ClusterWidgetViews`' gate does, so a paused surface can never
/// fall through to live data, to a stale snapshot, or to a blank. The accessory
/// circular draws `shortGateLabel` as text instead — there is no room for an
/// icon in a ring.
private struct BoardStatusGateView: View {
    let entry: BoardStatusEntry

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape")
                .font(.title3)
                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            Text(entry.gateLine)
                .font(.caption2)
                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
        }
    }
}
