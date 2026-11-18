import Foundation

// ---------------------------------------------------------------------------
// BoardStatusReading.swift — the executable half of the Board Status widget.
//
// WHY THIS FILE EXISTS. `BoardStatusWidget.swift` imports WidgetKit + AppIntents,
// so no gate on this host can compile it: the macOS SDK marks the accessory
// families unavailable, and `build_check.sh` only ever COMPILES the iOS target —
// it never runs a provider. That combination is how a widget ships a false zero
// with every gate green. So every decision that produces a NUMBER lives here, on
// Foundation only, and `scripts/board_status_widget_check.sh` compiles THIS file
// against the real `SharedModels.swift` / `SharedStore.swift` /
// `WidgetDataClient.swift` and EXECUTES those rules headlessly. Same split as
// `ClusterWidgetMetrics.swift` for the cluster widget.
//
// The rules this file owns, each one an honesty rule from
// `docs/WIDGET_DATA_CONTRACT.md` (the gate asserts all of them):
//   * `—` is not `0`: an absent status is nil all the way to the text (§6 rule 1),
//     so `numberText` is the only place a count becomes a string.
//   * the interim path never invents `done` (§5.2), and never derives `blocked`
//     from the `kanban/stale` list either — measured 2026-11-17: `kanban/blocked`
//     answered `count: 18` while `stale?older_than=0` answered 15 rows with no
//     `blocked` status among them.
//   * a list that sends fewer rows than its own `count` REFUSES the read instead
//     of under-counting, which keeps the last-known snapshot on screen.
//   * `openCount` exists only when all four open operands do — a partial sum is
//     a number that means something else.
//   * a snapshot belongs to the board it was read for, and never stores numbers
//     it does not have (§7).
//
// Nothing here renders UI: no `Color`, no `View`, no WidgetKit or AppIntents
// type. The widget file keeps the views and delegates every number and
// number-string decision to this file, so the gate cannot drift from what the
// widget actually draws.
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
    // Internal, not private: `board_status_widget_check.sh` captures and restores
    // these two keys around its run, because the store reads the REAL App Group
    // suite and a gate must not leave operator state behind.
    static let snapshotKey = "hscc.boardstatus.snapshot"
    static let boardsKey = "hscc.boardstatus.boards"

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

// MARK: - The read plan's decisions (extracted so the gate can execute them)

/// The decisions `BoardStatusTimelineProvider.readCounts` makes, as functions.
///
/// They are static funcs rather than private methods of the provider for exactly
/// one reason: the provider is `AppIntentTimelineProvider`-conforming and lives in
/// the WidgetKit file, so nothing on this host could call them. The provider now
/// delegates here — there is no second implementation to keep in step, and
/// `scripts/board_status_widget_check.sh` compiles this file and runs them.
enum BoardStatusReading {
    /// The five statuses, in board order. One list so no family can quietly drop
    /// one (a widget that shows four of five is a widget that lies), and so the
    /// gate can assert the list and `BoardStatusProjection.value(for:)` cannot
    /// drift apart.
    static let statuses: [(key: String, label: String)] = [
        ("todo", "todo"),
        ("ready", "ready"),
        ("running", "run"),
        ("blocked", "blocked"),
        ("done", "done 24h"),
    ]

    /// The ONE place a count becomes a string (§6 rule 1). `nil` is "this payload
    /// did not carry that status" and draws `—`; `0` is "the server counted none".
    /// Every family renders numbers through this, so a missing status cannot be
    /// drawn as a zero by construction rather than by review.
    static func numberText(_ value: Int?) -> String {
        value.map { "\($0)" } ?? "—"
    }

    /// True when a list holds fewer rows than its own envelope claims — the
    /// shape of a truncated payload, where counting client-side under-reports.
    /// Measured 2026-11-17 both routes send `count` == rows, so this is a guard
    /// for the day one of them starts capping, not a case in the current data.
    static func isTruncated(returned: Int, total: Int?) -> Bool {
        guard let total else { return false }
        return returned < total
    }

    /// §5.2's interim: resolve the two parallel reads into the four open numbers.
    ///
    /// The queue read is the one the interim cannot do without, so ITS failure is
    /// the failure the surface renders. The blocked read is not: it failing nils
    /// only the blocked tile (`—`), because the numbers that DID arrive are still
    /// true. A truncated payload refuses the whole read instead of drawing a
    /// number that is low by construction — `WidgetData.read` then keeps the
    /// last-known snapshot on screen.
    static func interim(stale: Result<KanbanStaleLite, WidgetDataFailure>,
                        blocked: Result<KanbanBlockedLite, WidgetDataFailure>,
                        boardSlug: String?) -> Result<BoardStatusProjection, WidgetDataFailure> {
        switch stale {
        case .failure(let failure):
            return .failure(failure)

        case .success(let value):
            guard let staleTasks = value.tasks else {
                // A 2xx with no `tasks` array is not "the board is empty", it is
                // a payload this build cannot read a count out of. `decoding`
                // keeps the last-known numbers instead of drawing zeros.
                return .failure(.decoding(detail: "kanban/stale carried no tasks array"))
            }
            guard !isTruncated(returned: staleTasks.count, total: value.count) else {
                // Fewer rows than the envelope advertises means a subset, and a
                // subset histogram under-counts quietly.
                return .failure(.decoding(detail: "kanban/stale sent fewer rows than its count"))
            }

            // `blocked` has its own read — see `init(interimStale:…)`, which
            // carries the measurement for why the stale list cannot answer it.
            var blockedRows: [BlockedCardLite]?
            var blockedTotal: Int?
            if case .success(let blockedValue) = blocked {
                let rows = blockedValue.tasks
                let truncated = rows.map { isTruncated(returned: $0.count, total: blockedValue.count) } ?? false
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

    /// The small family's supporting line: all five CLI columns in one short
    /// sentence, with their real labels. `—` is only used when the payload did
    /// not carry a status, never as a stand-in for zero.
    static func smallCountsLine(_ projection: BoardStatusProjection?) -> String {
        guard let projection, projection.hasNumbers else { return "No board counts" }
        return [
            "\(numberText(projection.todo)) todo",
            "\(numberText(projection.ready)) ready",
            "\(numberText(projection.running)) run",
            "\(numberText(projection.blocked)) blocked",
            "\(numberText(projection.done)) done 24h",
        ].joined(separator: " · ")
    }

    /// `accessoryRectangular`'s second line and the small/medium footer: at most
    /// two operands, most urgent first. §5.2's example is "17 todo · 6 run";
    /// a nonzero `blocked` outruns everything, because it is the number that
    /// means "someone is waiting on you".
    static func countsShortLine(_ projection: BoardStatusProjection) -> String {
        var parts: [String] = []
        if let blocked = projection.blocked, blocked > 0 { parts.append("\(blocked) blocked") }
        if let running = projection.running { parts.append("\(running) run") }
        if let todo = projection.todo { parts.append("\(todo) todo") }
        if let ready = projection.ready { parts.append("\(ready) ready") }
        if let done = projection.done, parts.isEmpty { parts.append("\(done) done") }
        guard !parts.isEmpty else { return "quiet" }
        return parts.prefix(2).joined(separator: " · ")
    }

    /// The number the `accessoryCircular` draws: one scalar that is always a
    /// field the CLI prints. Blocked wins (it is the number that means someone is
    /// waiting), then running, then done-today. Nothing known draws `—`, never a
    /// reassuring zero.
    static func circularNumber(_ projection: BoardStatusProjection?) -> Int? {
        if let blocked = projection?.blocked, blocked > 0 { return blocked }
        if let running = projection?.running { return running }
        if let done = projection?.done { return done }
        return projection?.blocked
    }

    /// The word under `circularNumber`, chosen with the same precedence so the
    /// ring can never label a done-today count as "blocked".
    static func circularLabel(_ projection: BoardStatusProjection?) -> String {
        if let blocked = projection?.blocked, blocked > 0 { return "blocked" }
        if projection?.running != nil { return "running" }
        if projection?.done != nil { return "done 24h" }
        return "blocked"
    }

    /// The one large number the small family leads with. Blocked is the urgent
    /// case; otherwise running is what the board is doing. nil means the read
    /// carried neither, and the view says why instead of drawing a zero.
    static func headlineNumber(_ projection: BoardStatusProjection?) -> Int? {
        if let blocked = projection?.blocked, blocked > 0 { return blocked }
        if let running = projection?.running { return running }
        return projection?.done
    }

    static func headlineLabel(_ projection: BoardStatusProjection?) -> String {
        if let blocked = projection?.blocked, blocked > 0 { return "blocked" }
        if projection?.running != nil { return "running" }
        return "done 24h"
    }

    /// The ring's colour is the one question a circular complication answers:
    /// does this board need me? (The five TILES use `Theme.cardStatusColor`,
    /// where a blocked card is amber — that is a status among many, and Theme
    /// documents the difference on purpose.)
    static func circularIsUrgent(_ projection: BoardStatusProjection?) -> Bool {
        (projection?.blocked ?? 0) > 0
    }
}
