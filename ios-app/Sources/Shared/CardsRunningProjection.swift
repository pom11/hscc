import Foundation

// ---------------------------------------------------------------------------
// The running-cards projection — the row, the stored state, and the last-known
// snapshot — shared by the two surfaces that draw it.
//
// This is the SAME information the Home Screen widget renders and the
// running-cards Live Activity pushes: `hscc cards monitor --live`'s header
// fields (board · card · worker · run time) plus the two counts, projected out
// of the kanban pair (`GET /v1/kanban/running` + `GET /v1/kanban/blocked`) and
// nothing else. WIDGET_DATA_CONTRACT §5.4 is the spec; §8.3 the rule that a
// card list comes from the kanban trio, never `/v1/cards`.
//
// WHY IT LIVES IN Shared AND NOT IN THE WIDGET TARGET (t_71deac83): the widget
// timeline and the app-side Live Activity driver both need this type and both
// need `CardsRunningSnapshotStore` — the driver's `lastKnown:` argument in
// `WidgetData.read` is literally this store's `load()`. Two decoders of one
// App Group key is how two surfaces start disagreeing about what "last known"
// means, so there is one file, compiled into the app and the widget extension.
//
// It stays Foundation-only on purpose (no WidgetKit, no Theme, no ActivityKit):
// that is what lets `scripts/live_activity_rehydration_check.sh` COMPILE this
// file as-is on the macOS SDK and execute the count rules below against real
// wire fixtures, instead of transcribing them into a table that can drift.
//
// STORAGE (§7): the projection declares six scalars per row and so cannot
// hold a card body, a workspace path, a branch, an address or a token even if a
// later card wanted to persist one by accident. Writes go through
// `SharedStore.suite` — never a `.standard` fallback.
// ---------------------------------------------------------------------------

// MARK: - The projection (what is stored, and what every surface draws)

/// One running card, narrowed to the five fields a row draws.
///
/// Deliberately NOT `RunningCardLite`: that is the wire shape, it owns `pid`-
/// adjacent keys the pivot dropped, and storing it would put a payload type in
/// the App Group container, which §7 forbids. The mapping below is the whole
/// projection — six scalars, nothing nested, nothing that can carry prose.
struct CardsRunningRow: Codable, Equatable, Identifiable {
    /// Board slug, or nil when the row did not carry one.
    let board: String?
    /// The card id (`t_…`), rendered monospaced — it is machine-produced.
    let cardID: String
    /// The card title. Human-written, so it renders in the system font.
    let title: String?
    /// The worker profile running it — `worker` on the wire (the CLI's panel
    /// header), falling back to `assignee` on a server that predates it.
    let worker: String?
    let status: String?
    /// Seconds since `started_at` (or the route's `run_s`), computed once at
    /// fetch time. nil = the row had no usable clock, which draws `—`.
    ///
    /// Seconds, not minutes: `hscc cards monitor` prints `30s` for a card under
    /// a minute, and storing whole minutes would round that to `0m` — a number
    /// the CLI never shows.
    let ageSeconds: Int?
    /// Seconds since the worker's last heartbeat (`hb_s`). nil = the route did
    /// not send one; the R3 heartbeat dot then draws the unknown state instead
    /// of claiming the worker is alive. Optional so a snapshot written before
    /// this field existed still decodes.
    let heartbeatSeconds: Int?

    /// Board-qualified, because the same card id on two boards is two rows and
    /// a `ForEach` id must not collide.
    var id: String { "\(board ?? "?")/\(cardID)" }

    /// Whole minutes, for the callers that sort or gate on a minute figure.
    var ageMinutes: Int? { ageSeconds.map { $0 / 60 } }

    init(board: String?, cardID: String, title: String?, worker: String?,
         status: String?, ageSeconds: Int?, heartbeatSeconds: Int? = nil) {
        self.board = board
        self.cardID = cardID
        self.title = title
        self.worker = worker
        self.status = status
        self.ageSeconds = ageSeconds
        self.heartbeatSeconds = heartbeatSeconds
    }

    /// Convenience for previews/tests that speak in minutes. Whole minutes only:
    /// a fixture that needs `30s` uses the seconds initializer.
    init(board: String?, cardID: String, title: String?, worker: String?,
         status: String?, ageMinutes: Int?, heartbeatSeconds: Int? = nil) {
        self.init(board: board, cardID: cardID, title: title, worker: worker,
                  status: status, ageSeconds: ageMinutes.map { $0 * 60 },
                  heartbeatSeconds: heartbeatSeconds)
    }

    /// Project one wire row. `worker` is the CLI's `worker` column when the
    /// route sends it (`collect()`'s profile-or-assignee), with `assignee` as
    /// the fallback for the older server; `run_s` is the CLI's run clock when
    /// present, with `started_at` as the fallback.
    init(task: RunningCardLite, now: Date) {
        board = task.board
        cardID = task.id
        title = task.title
        worker = task.worker ?? task.assignee
        status = task.status
        ageSeconds = Self.ageSeconds(task: task, now: now)
        heartbeatSeconds = task.hb_s.map { max(0, Int($0)) }
    }

    /// Seconds of run time, from the fields `hscc cards monitor` prints.
    ///
    /// `run_s` wins when the route sent it: it is the server's own measurement
    /// of the same clock (`kanban_live.collect()`), so the widget and the CLI
    /// cannot disagree by the phone/daemon clock skew. `started_at` is the
    /// fallback the live 2.15.11 server leaves us. A negative value is clamped
    /// to "just started", never a future age.
    static func ageSeconds(task: RunningCardLite, now: Date) -> Int? {
        if let run = task.run_s {
            return max(0, Int(run))
        }
        return ageSeconds(from: task.started_at, now: now)
    }

    /// Seconds since an ISO-8601 `started_at`, floored at zero.
    static func ageSeconds(from startedAt: String?, now: Date) -> Int? {
        guard let startedAt, !startedAt.isEmpty else { return nil }
        guard let started = Self.dateParser.date(from: startedAt)
            ?? Self.fractionalParser.date(from: startedAt)
        else { return nil }
        // A timestamp ahead of this clock (skew between the daemon host and the
        // phone) is "just started", not a negative age.
        return max(0, Int(now.timeIntervalSince(started)))
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

    /// `—` when unknown, otherwise the CLI's own `fmt_age`: `30s` under a
    /// minute, `12m` under an hour, `2h07m` under a day, `1d2h` above it.
    var ageText: String {
        guard let seconds = ageSeconds else { return "—" }
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86400 {
            return String(format: "%dh%02dm", seconds / 3600, (seconds % 3600) / 60)
        }
        return String(format: "%dd%dh", seconds / 86400, (seconds % 86400) / 3600)
    }

    /// The R3 hero clock: `32m` / `1h 05m` / `2h 07m`. Hours gain a space so
    /// the large type reads as two units rather than an id.
    var heroAgeText: String {
        guard let seconds = ageSeconds else { return "—" }
        if seconds < 3600 { return ageText }
        if seconds < 86400 {
            return String(format: "%dh %02dm", seconds / 3600, (seconds % 3600) / 60)
        }
        return String(format: "%dd %dh", seconds / 86400, (seconds % 86400) / 3600)
    }

    /// The R3 heartbeat line. Nil is drawn as `beat —`, never as a fresh beat.
    var heartbeatText: String {
        guard let seconds = heartbeatSeconds else { return "beat —" }
        if seconds < 60 { return "beat \(seconds)s" }
        if seconds < 300 { return "beat \(seconds / 60)m" }
        if seconds < 3600 { return "beat \(seconds / 60)m" }
        return "beat \(seconds / 3600)h"
    }

    /// Green under five minutes, orange once the heartbeat is older. nil (the
    /// route sent no `hb_s`) has no state colour at all.
    var heartbeatIsFresh: Bool? {
        guard let seconds = heartbeatSeconds else { return nil }
        return seconds < 300
    }

    static func truncate(_ value: String, max: Int) -> String {
        guard max > 1, value.count > max else { return value }
        return String(value.prefix(max - 1)) + "…"
    }

    /// Minutes since an ISO-8601 `started_at`, floored at zero. Kept for the
    /// gates and callers that only need the minute figure.
    static func age(from startedAt: String?, now: Date) -> Int? {
        ageSeconds(from: startedAt, now: now).map { $0 / 60 }
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

    /// The designated init, written out because the payload init below removes
    /// the implicit memberwise one — and the `placeholder(in:)` fixture needs it.
    init(rows: [CardsRunningRow], runningCount: Int?, blockedCount: Int?,
         rowsAreComplete: Bool) {
        self.rows = rows
        self.runningCount = runningCount
        self.blockedCount = blockedCount
        self.rowsAreComplete = rowsAreComplete
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
            (lhs.ageSeconds ?? -1) > (rhs.ageSeconds ?? -1)
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
        // A blocked read that answered gives that route's own `count`, with its
        // row list as the fallback; `nil` means the read did not answer at all,
        // which draws `—` rather than a zero. Nothing here may be counted out of
        // the running list — blocked cards are not in it by definition.
        if let blocked {
            blockedCount = blocked.count ?? blocked.tasks?.count
        } else {
            blockedCount = nil
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
        runningCount: 3, blockedCount: 1, rowsAreComplete: true
    )

    /// Whether any number at all is drawable. False means draw the failure.
    var hasNumbers: Bool { runningCount != nil || blockedCount != nil || !rows.isEmpty }

    var isIdle: Bool { (runningCount ?? 0) == 0 }

    var isStuck: Bool { (blockedCount ?? 0) > 0 }
}

// MARK: - The words every surface prints

/// The numeric copy, in one Foundation-only place so the widget's family
/// bodies, the Live Activity, and the headless `cards_running_widget_check`
/// all print the same answer for the same projection.
///
/// Gate copy (setup/paused) stays on `CardsRunningEntry`: it is a property of
/// the operator's settings, not of the payload, and `widget_view_dispatch`
/// asserts the invite-first order there. This enum owns only the strings a
/// fixture can prove.
enum CardsRunningCopy {
    /// `3 running`, or nil when the refresh was not told. Never a fabricated
    /// zero: the count is `/v1/kanban/running`'s own `count`, which the route
    /// builds as `len(running)` — the same number `hscc cards monitor` puts
    /// beside "slots".
    static func runningText(_ projection: CardsRunningProjection?) -> String? {
        guard let running = projection?.runningCount else { return nil }
        return "\(running) running"
    }

    /// The secondary blocked figure, or `—` when that route did not answer.
    /// Never the headline: `hscc cards monitor` draws running cards, and a
    /// blocked count is the separate `hscc kanban blocked` read.
    static func blockedText(_ projection: CardsRunningProjection?) -> String? {
        guard let projection else { return nil }
        guard let blocked = projection.blockedCount else { return "blocked —" }
        return "\(blocked) blocked"
    }

    /// The one number the circular complication and the small tile lead with.
    static func headlineNumber(_ projection: CardsRunningProjection?) -> Int? {
        projection?.runningCount
    }

    /// `accessoryInline`'s counts-only line. Gate copy is prepended by the
    /// entry, which owns setup/paused; this is what a readable entry shows.
    /// The running count is the whole line: `hscc cards monitor` draws running
    /// cards, and a blocked total would be a different command's number.
    static func inlineLine(projection: CardsRunningProjection?, failure: WidgetDataFailure?) -> String {
        guard let projection, projection.hasNumbers else { return failure?.shortLabel ?? "No data" }
        if let running = runningText(projection) { return running }
        if let blocked = projection.blockedCount { return "\(blocked) blocked" }
        return failure?.shortLabel ?? "No data"
    }

    /// The qualifiers a live footer adds beside its age: a capped row list or a
    /// blocked read that did not answer. nil when there is nothing to qualify.
    static func notesLine(_ projection: CardsRunningProjection?) -> String? {
        guard let projection else { return nil }
        var notes: [String] = []
        if !projection.rowsAreComplete, let total = projection.runningCount {
            notes.append("showing \(projection.rows.count) of \(total)")
        }
        if projection.blockedCount == nil { notes.append("blocked unreadable") }
        return notes.isEmpty ? nil : notes.joined(separator: " · ")
    }

    /// The age or reason line for the Home Screen families.
    ///
    /// A stale snapshot says how old it is AND why this refresh did not answer;
    /// a blind read says the failure and never a number. Live data says
    /// "updated just now" — the view pairs that with a self-updating relative
    /// timestamp, so it keeps ageing while WidgetKit holds the entry.
    static func footerLine(projection: CardsRunningProjection?, ageMinutes: Int?,
                           failure: WidgetDataFailure?) -> String {
        if let age = ageMinutes {
            return "last known \(age) min ago · \(failure?.shortLabel ?? "no answer")"
        }
        if let failure { return failure.operatorLine }
        guard let projection else { return "no data" }
        if let notes = notesLine(projection) { return notes }
        return "updated just now"
    }

    /// Whether the secondary blocked figure is a warning. A nil count is not a
    /// warning: nothing was read, so nothing is coloured red.
    static func blockedIsWarning(_ projection: CardsRunningProjection?) -> Bool {
        (projection?.blockedCount ?? 0) > 0
    }
}

// MARK: - The two reads, in parallel, into one projection

/// The pair of routes this surface is built from, read once and projected.
///
/// TWO requests, which is WIDGET_DATA_CONTRACT §2's ceiling for one surface and
/// exactly the pairing §5.4 names (`/v1/kanban/running` for the live workers,
/// `/v1/kanban/blocked` for the failure rows). `/v1/kanban/stale` is NOT read:
/// dataset 4's full open list is the board-status widget's job, and pulling it
/// would buy rows this surface never draws. `/v1/cards` is §8.3's forbidden
/// route — it has no field projection, so 8 rows cost 81,855 B of which 98.6 %
/// is discarded, while these two answer the same question in ~2 KB.
///
/// This is the ONE implementation of that rule. The widget's timeline provider
/// and the app's Live Activity driver both call it, because the guard below —
/// a 2xx with no `tasks` array is a read this build cannot count, not an idle
/// fleet — is the thing that stops a blind refresh becoming a false `0 running`,
/// and a surface that grew its own copy of it could get it wrong alone.
enum CardsRunningFeed {
    static func read(client: WidgetDataClient) async -> Result<CardsRunningProjection, WidgetDataFailure> {
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
                // `0 running` for it is the one failure this surface exists to
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

// MARK: - The last-known snapshot (the stale-while-error half)

/// The projection + its age, in the App Group suite.
///
/// Fleet-wide, so one slot: the surface is the CLI's fleet answer (no board
/// scope), and a snapshot of a different question would not be last-known data
/// for this one. The stored shape is
/// `CardsRunningProjection`, which declares six scalars per row and so cannot
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
