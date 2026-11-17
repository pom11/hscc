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

    /// The fixture `placeholder` draws: plausible, clearly not a real cluster,
    /// and the same shape a live entry takes so the families can be eyeballed.
    static let sample = BoardStatusEntry(
        date: .now,
        boardSlug: "hscc",
        projection: .preview,
        ageMinutes: nil,
        failure: nil,
        configured: true
    )

    static func unconfigured(boardSlug: String?) -> BoardStatusEntry {
        BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: nil,
                         ageMinutes: nil, failure: .notConfigured, configured: false)
    }

    // MARK: Text the families draw (kept here so it is one rule per string)

    var titleText: String { boardSlug ?? BoardStatusProjection.allBoardsLabel }

    /// `accessoryInline`: one short line. §5.2 budgets ~25 characters, and a
    /// long slug is dropped rather than truncated — half a board name is
    /// ambiguous, the counts are not.
    var inlineLine: String {
        if !configured { return "Set up in HSCC" }
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
    /// Cadence, not a poll. The operator's per-surface cadence lives in
    /// `AppGroupWidgetSettings`, whose `WidgetSurfaceKind` has no case for this
    /// widget yet (adding one is a `SharedStore.swift` + config-screen change,
    /// outside this card's file set), so the widget uses the conservative
    /// quarter-hour and lets WidgetKit's own budget have the last word.
    private static let refreshMinutes = 15

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
        await entry(for: configuration)
    }

    func timeline(for configuration: BoardStatusConfigurationIntent,
                  in context: Context) async -> Timeline<BoardStatusEntry> {
        let entry = await entry(for: configuration)
        let next = Calendar.current.date(byAdding: .minute, value: Self.refreshMinutes, to: .now)
            ?? .now.addingTimeInterval(900)
        return Timeline(entries: [entry], policy: .after(next))
    }

    // MARK: Fetch

    private func entry(for configuration: BoardStatusConfigurationIntent) async -> BoardStatusEntry {
        let boardSlug = configuration.board?.id
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
                                    ageMinutes: nil, failure: nil, configured: true)
        case .stale(let projection, let ageMinutes, let reason):
            return BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: projection,
                                    ageMinutes: ageMinutes, failure: reason, configured: true)
        case .failed(let failure):
            return BoardStatusEntry(date: .now, boardSlug: boardSlug, projection: nil,
                                    ageMinutes: nil, failure: failure, configured: true)
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
        if !entry.configured {
            BoardStatusInviteView(compact: true)
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
        if !entry.configured {
            BoardStatusInviteView(compact: false)
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
        if !entry.configured {
            BoardStatusInviteView(compact: false)
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
        if !entry.configured {
            BoardStatusInviteView(compact: false)
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
        if !entry.configured {
            BoardStatusInviteView(compact: false)
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

/// The unconfigured state — the only thing an unconfigured app may show
/// (t_5c554c5b, enforced for the cluster widget by `widget_view_dispatch_check`).
private struct BoardStatusInviteView: View {
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: "gearshape")
                .font(compact ? .system(size: 12) : .title3)
                .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            if !compact {
                Text("Set up the app to see this board")
                    .font(.caption2)
                    .foregroundStyle(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}
