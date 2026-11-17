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
// STORAGE (§7): the projection declares five scalars per row and so cannot
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
