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
//
// FILE SPLIT (t_1b09701e): `BoardStatusSource`, `BoardStatusProjection`, the
// interim read rules (`BoardStatusReading`) and `BoardStatusSnapshotStore` live
// in `BoardStatusReading.swift`, on Foundation only, so
// `scripts/board_status_widget_check.sh` can compile them and EXECUTE the count
// rules — this file imports WidgetKit + AppIntents, which no macOS harness can
// build, so a rule kept here is a rule no gate can run. What stays here is the
// timeline provider, the board picker and the six family views, and every number
// they draw comes from the other file.
// ---------------------------------------------------------------------------

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
        let counts = BoardStatusReading.countsShortLine(projection)
        let withBoard = "\(titleText) · \(counts)"
        return withBoard.count <= 25 ? withBoard : counts
    }

    /// `accessoryRectangular`'s second line and the small/medium footer: at most
    /// two operands, most urgent first. The ORDER rule lives in
    /// `BoardStatusReading.countsShortLine`, which the gate executes; what is
    /// decided here is only that an entry with nothing to show says why instead
    /// of drawing zeros.
    var countsShortLine: String {
        guard let projection, projection.hasNumbers else { return failure?.shortLabel ?? "No data" }
        return BoardStatusReading.countsShortLine(projection)
    }

    /// The number the `accessoryCircular` draws — see
    /// `BoardStatusReading.circularNumber` for the rule (blocked first, open
    /// total as the fallback, nil when neither is known so it draws `—`).
    var circularNumber: Int? { BoardStatusReading.circularNumber(projection) }

    /// The ring's colour is the one question a circular complication answers:
    /// does this board need me? (The five TILES use `Theme.cardStatusColor`,
    /// where a blocked card is amber — that is a status among many, and Theme
    /// documents the difference on purpose.)
    var circularIsUrgent: Bool { BoardStatusReading.circularIsUrgent(projection) }

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
    /// drop one (a widget that shows four of five is a widget that lies). The
    /// list itself lives in `BoardStatusReading.statuses`, which the gate
    /// executes against `BoardStatusProjection.value(for:)`.
    static let statuses: [(key: String, label: String)] = BoardStatusReading.statuses

    /// The no-data shape: five `—` tiles. The reason for the gap is the
    /// footer/header's job (it carries `WidgetDataFailure.shortLabel`), so a
    /// tile never invents its own copy.
    static func empty() -> [BoardStatusTile] {
        statuses.map { item in BoardStatusTile(status: item.label, value: nil, color: Theme.Semantic.neutral) }
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

        // The queue read is the one the interim cannot do without, its failure is
        // the failure the surface renders; a truncated payload refuses the read;
        // and a failed blocked read nils only the blocked tile. All three rules
        // are `BoardStatusReading.interim`, which the executed gate runs.
        return BoardStatusReading.interim(stale: stale, blocked: blocked, boardSlug: boardSlug)
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
                Text(BoardStatusReading.numberText(entry.circularNumber))
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
                        Text(BoardStatusReading.numberText(tile.value))
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
                    Text(BoardStatusReading.numberText(tile.value))
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
