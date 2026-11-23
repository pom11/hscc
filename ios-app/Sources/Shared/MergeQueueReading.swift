import Foundation

// ---------------------------------------------------------------------------
// MergeQueueReading.swift — the merge-queue surface's data half.
//
// `GET /v1/merge-queue` is the read-only projection of the local merge queue
// (`hscc_daemon/merge_queue.py::view()`). This file is the ONE wire→surface
// projection: the iOS widget, the macOS widget (compiled by path) and the menu
// bar dropdown all read `MergeQueueFeed`, so they cannot disagree about a row,
// its FIFO position, its state or its age.
//
// WHAT THE SURFACE MIRRORS. `hscc kanban monitor` draws one table per project
// with the columns Pos, PR, Card, Branch, State, Age and Last result
// (`hscc_daemon/kanban_live.py::merge_queue_table`). `MergeQueueTableReading`
// holds those exact column titles and returns those exact cell strings, in the
// CLI's order, so a widget row IS a CLI row rather than a re-derivation of it.
// `ios-app/scripts/merge_queue_cli_parity.py` builds the committed golden from
// the real CLI table, and `model_decode_check.sh` compares this file's cells
// against it.
//
// ORDER. The route flattens every project's rows in registry order; inside one
// project that is FIFO queued rows, then the in-flight claim, then the last
// terminal outcome. The surface draws that array verbatim — no sort, no fold.
//
// NO RAW RESPONSE IS STORED. `MergeQueueSnapshotStore` persists `MergeQueueRow`
// values and the two counts, never the decoded envelope or the server's prose.
// ---------------------------------------------------------------------------

/// One row exactly as `GET /v1/merge-queue` sends it. Every field but the
/// project is optional because the CLI's own row projection fills missing
/// values with `-` rather than refusing the row.
struct MergeQueueRowLite: Decodable, Equatable, Sendable {
    let project: String
    let position: Int?
    let pr: Int?
    let card: String?
    let branch: String?
    let state: String?
    let ageS: Double?
    let lastResult: String?
    let detail: String?
    let isLast: Bool?

    enum CodingKeys: String, CodingKey {
        case project, position, pr, card, branch, state, detail
        case ageS = "age_s"
        case lastResult = "last_result"
        case isLast = "is_last"
    }
}

/// One project's queue state from the route's `projects` map. Only the two
/// flags an off/disabled surface needs are decoded: the rows themselves stay
/// on the flattened array, so there is still one representation of a row.
struct MergeQueueProjectLite: Decodable, Equatable, Sendable {
    let enabled: Bool?
    let paused: Bool?
    let queue_len: Int?
    let repo: String?
}

/// The route's envelope. `projects` carries the per-project opt-in, which is
/// the one fact the flattened `rows` array cannot express: a queue that is off
/// has no rows, and M1 must say "Merge queue off for <project>" rather than
/// draw that as an empty queue. Rows still come only from `rows`.
struct MergeQueueLite: Decodable, Equatable, Sendable {
    let ok: Bool?
    let generatedAt: String?
    let rows: [MergeQueueRowLite]
    let count: Int?
    let queued: Int?
    let projects: [String: MergeQueueProjectLite]?
    let speak: String?

    enum CodingKeys: String, CodingKey {
        case ok, rows, count, queued, projects, speak
        case generatedAt = "generated_at"
    }
}

/// One row the surface draws, with the CLI's cells precomputed from the same
/// strings its `Table.add_row` receives.
struct MergeQueueRow: Codable, Equatable, Identifiable, Sendable {
    let project: String
    let position: Int?
    let pr: Int?
    let card: String?
    let branch: String?
    let state: String
    let ageS: Double?
    let lastResult: String?
    let detail: String?
    let isLast: Bool

    /// Stable identity for `ForEach`: project + PR + position + terminal flag.
    /// `view()` folds a project's last outcome into an already-queued PR, so
    /// this pair cannot repeat inside one payload.
    var id: String {
        "\(project)#\(pr.map(String.init) ?? "-")@\(position.map(String.init) ?? "-")"
            + (isLast ? "-last" : "")
    }

    init(lite: MergeQueueRowLite) {
        project = lite.project
        position = lite.position
        pr = lite.pr
        card = lite.card
        branch = lite.branch
        state = MergeQueueReading.nonEmpty(lite.state) ?? "failed"
        ageS = lite.ageS
        lastResult = lite.lastResult
        detail = lite.detail
        isLast = lite.isLast == true
    }

    /// A row that is being worked by the queue right now.
    var isInFlight: Bool {
        state == "rebasing" || state == "gating" || state == "merging"
    }

    var isMerged: Bool { state == "merged" }

    /// The CLI's seven cells, in its column order.
    var cells: [String] { MergeQueueTableReading.cells(for: self) }

    /// `2m` / `1h02m` / `1d2h`, exactly `kanban_live.fmt_age`.
    var ageLabel: String { MergeQueueReading.ageLabel(seconds: ageS) }

    /// `#103`, or `-` when the route carried no PR.
    var prLabel: String { pr.map { "#\($0)" } ?? "-" }

    /// The row's title stand-in: the card id the route sends. The queue row
    /// carries no PR title (its CLI columns are Pos/PR/Card/Branch/State/Age),
    /// so the card id is the honest line rather than an invented title.
    func headline(max limit: Int) -> String {
        let raw = card ?? prLabel
        guard limit > 1, raw.count > limit else { return raw }
        return String(raw.prefix(limit - 1)) + "…"
    }
}

/// The queue as one surface draws it: the CLI's rows in the CLI's order, plus
/// the two counts the route already folded.
struct MergeQueueFeed: Codable, Equatable, Sendable {
    let rows: [MergeQueueRow]
    let count: Int
    let queued: Int
    let generatedAt: String?
    /// The route's `ok`. Optional so a snapshot written before this field
    /// decodes; `false` means the daemon could not produce the queue and the
    /// empty rows are an error, not a genuine empty queue.
    let ok: Bool?
    /// The project named in an off state. Non-nil ONLY when every project the
    /// route knows has `enabled: false`; a mixed fleet still draws its live
    /// queues. Optional so a snapshot written before this field decodes.
    let offProject: String?

    init(rows: [MergeQueueRow], count: Int, queued: Int,
         generatedAt: String?, ok: Bool = true, offProject: String? = nil) {
        self.rows = rows
        self.count = count
        self.queued = queued
        self.generatedAt = generatedAt
        self.ok = ok
        self.offProject = offProject
    }

    static let empty = MergeQueueFeed(rows: [], count: 0, queued: 0,
                                      generatedAt: nil)

    var isEmpty: Bool { rows.isEmpty }

    /// True when the route answered with `ok: false`. The widget must draw
    /// this as offline/failed, never as a fresh empty queue.
    var isUnavailable: Bool { ok == false }

    /// True when the route told us every project's queue is disabled.
    var isOff: Bool { offProject != nil }

    /// The row M1 leads with: the in-flight claim, else the next FIFO row,
    /// else the first historical row.
    var headRow: MergeQueueRow? { current ?? nextQueued ?? rows.first }

    /// Rows behind the head (the "+N waiting" figure).
    var waitingCount: Int { max(0, queued - (current == nil ? 1 : 0)) }

    /// The last terminal row, or nil when nothing has ever run.
    var lastTerminal: MergeQueueRow? {
        lastMerged ?? rows.last(where: \.isLast)
    }

    /// The last five terminal rows (one per project at most, since the route
    /// folds each project's last outcome into one `is_last` row).
    var recentResults: [MergeQueueRow] {
        Array(rows.filter(\.isLast).suffix(5).reversed())
    }

    /// The row the daemon is working right now, if any. `view()` puts the
    /// in-flight claim after the queued rows, so this is not necessarily first.
    var current: MergeQueueRow? { rows.first(where: \.isInFlight) }

    /// The next FIFO row, when nothing is in flight.
    var nextQueued: MergeQueueRow? { rows.first { $0.state == "queued" } }

    /// The most recent terminal merge. `age_s` is seconds since the outcome,
    /// so the smallest age is the latest merge; a row with no age sorts last.
    var lastMerged: MergeQueueRow? {
        rows.filter(\.isMerged).min { lhs, rhs in
            (lhs.ageS ?? .greatestFiniteMagnitude) < (rhs.ageS ?? .greatestFiniteMagnitude)
        }
    }

    /// Rows grouped by project, project order and row order preserved. The CLI
    /// draws one table per project; the widget prints the project title whenever
    /// the group changes.
    var groups: [(project: String, rows: [MergeQueueRow])] {
        var order: [String] = []
        var byProject: [String: [MergeQueueRow]] = [:]
        for row in rows {
            if byProject[row.project] == nil { order.append(row.project) }
            byProject[row.project, default: []].append(row)
        }
        return order.map { ($0, byProject[$0] ?? []) }
    }

    /// The ONE wire→surface projection: keep the route's order and counts.
    static func from(_ payload: MergeQueueLite) -> MergeQueueFeed {
        let rows = payload.rows.map(MergeQueueRow.init(lite:))
        let queued = payload.queued
            ?? rows.filter { $0.state == "queued" }.count
        let count = payload.count ?? rows.count
        let projects = payload.projects ?? [:]
        let disabled = projects
            .filter { $0.value.enabled == false }
            .keys.sorted()
        let offProject = (!projects.isEmpty && disabled.count == projects.count)
            ? disabled.first : nil
        return MergeQueueFeed(rows: rows, count: count, queued: queued,
                              generatedAt: payload.generatedAt,
                              ok: payload.ok ?? true,
                              offProject: offProject)
    }

    /// One-line Lock Screen copy. Empty is its own honest state, never a
    /// fabricated zero.
    var inlineLine: String {
        if isUnavailable { return "merge queue offline" }
        if rows.isEmpty { return "merge queue empty" }
        if let current {
            return "\(queued) queued · \(current.state) \(current.prLabel)"
        }
        if let nextQueued {
            return "\(queued) queued · next \(nextQueued.prLabel)"
        }
        if let lastMerged {
            return "merge queue idle · last \(lastMerged.prLabel)"
        }
        return "\(queued) queued"
    }

    /// What the small tile prints under the queued count.
    var currentLine: String {
        if isUnavailable { return "merge queue offline" }
        if let current {
            return "\(current.state) \(current.prLabel)"
        }
        if let nextQueued {
            return "next \(nextQueued.prLabel)"
        }
        return rows.isEmpty ? "nothing queued" : "idle"
    }
}

/// The CLI's merge-queue table, as data. Columns and cells come from
/// `kanban_live.merge_queue_table`; the committed golden for
/// `model_decode_check.sh` is generated from that function, not transcribed.
enum MergeQueueTableReading {
    /// `merge_queue_table`'s `add_column` titles, in its order.
    static let columns = ["Pos", "PR", "Card", "Branch", "State", "Age",
                          "Last result"]

    /// One row's seven cells, exactly as `merge_queue_table` builds them:
    /// `-` for missing position/PR/card/branch, the raw state (default
    /// `failed`), the CLI's age abbreviation, and the last result with
    /// newlines flattened to spaces.
    static func cells(for row: MergeQueueRow) -> [String] {
        [row.position.map(String.init) ?? "-",
         row.prLabel,
         MergeQueueReading.nonEmpty(row.card) ?? "-",
         MergeQueueReading.nonEmpty(row.branch) ?? "-",
         row.state,
         row.ageLabel,
         lastResultCell(row)]
    }

    /// `result = last_result or "-"; if detail and not result: result = detail`
    /// followed by `str(result).replace("\n", " ")`. The `or` means an absent
    /// result prints `-` even when the route carried a detail string; that is
    /// the CLI's own rule, and the widget mirrors it rather than inventing a
    /// fallback.
    private static func lastResultCell(_ row: MergeQueueRow) -> String {
        let result = MergeQueueReading.nonEmpty(row.lastResult) ?? "-"
        return result.replacingOccurrences(of: "\n", with: " ")
    }

    /// Rows this surface has room for, capped at `limit`.
    static func visibleRows(_ rows: [MergeQueueRow], limit: Int) -> [MergeQueueRow] {
        guard limit > 0 else { return [] }
        return Array(rows.prefix(limit))
    }

    /// Rows beyond the cap, so a clipped table can say `+n` instead of
    /// silently reading as the whole queue.
    static func hiddenRowCount(_ rows: [MergeQueueRow], limit: Int) -> Int {
        max(0, rows.count - max(0, limit))
    }

    /// The row drawn as `project · Pos PR State Age` in the compact families,
    /// built from the same cells the table draws.
    static func compactLine(for row: MergeQueueRow) -> String {
        let cells = cells(for: row)
        return "\(row.project) · \(cells[0]) \(cells[1]) \(cells[4]) \(cells[5])"
    }
}

/// Pure formatting rules, executable outside WidgetKit.
enum MergeQueueReading {
    static let mediumRowLimit = 3
    static let largeRowLimit = 8

    /// M1's four stages, in pipeline order.
    static let stageLabels = ["rebase", "gates", "push", "merge"]

    /// The stage a row is in, in M1's vocabulary. A queued row has not started
    /// rebasing yet, so it draws rebase as the current stage.
    static func stageName(_ state: String) -> String {
        switch state {
        case "gating": return "gates"
        case "merging": return "push"
        case "merged": return "merge"
        default: return "rebase"
        }
    }

    /// How many stage bars are complete. `merged` completes all four, so the
    /// track ends entirely green rather than leaving merge "current".
    static func stageProgress(_ state: String) -> Int {
        switch state {
        case "rebasing", "queued": return 0
        case "gating": return 1
        case "merging": return 2
        case "merged": return 4
        default: return 0
        }
    }

    /// `#484 ✓` / `#484 ✗`, the compact last-result copy.
    static func resultMark(_ state: String) -> String {
        switch state {
        case "merged": return "✓"
        case "resolve", "failed": return "✗"
        default: return "·"
        }
    }

    static func nonEmpty(_ value: String?) -> String? {
        guard let value, !value.isEmpty else { return nil }
        return value
    }

    /// `hscc_daemon.kanban_live.fmt_age`, character for character.
    static func ageLabel(seconds: Double?) -> String {
        guard let seconds else { return "-" }
        let s = max(0, Int(seconds))
        if s < 60 { return "\(s)s" }
        if s < 3_600 { return "\(s / 60)m" }
        if s < 86_400 { return "\(s / 3_600)h\(String(format: "%02d", (s % 3_600) / 60))m" }
        return "\(s / 86_400)d\((s % 86_400) / 3_600)h"
    }

    /// The Lock Screen inline line.
    static func inlineLine(_ feed: MergeQueueFeed) -> String { feed.inlineLine }

    /// The freshness line every family can afford.
    static func updatedLine(ageMinutes: Int?) -> String {
        guard let ageMinutes else { return "updated just now" }
        return "updated \(ageMinutes)m ago"
    }
}

/// Persists the last-known projection so an unreachable daemon keeps the real
/// rows with their age instead of a blank tile. An empty queue is stored too:
/// "nothing queued" is an answer, and replaying a stale merged row as current
/// would invent work.
enum MergeQueueSnapshotStore {
    static let storageKey = "hscc.mergequeue.feed"

    struct Snapshot: Codable {
        let date: Date
        let feed: MergeQueueFeed
    }

    static func save(_ feed: MergeQueueFeed, at date: Date = Date()) {
        guard let data = try? JSONEncoder().encode(Snapshot(date: date, feed: feed)) else {
            return
        }
        SharedStore.suite?.set(data, forKey: storageKey)
    }

    static func load() -> Snapshot? {
        guard let data = SharedStore.suite?.data(forKey: storageKey),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            return nil
        }
        return snapshot
    }

    static func lastKnown() -> (value: MergeQueueFeed, at: Date)? {
        guard let snapshot = load() else { return nil }
        return (snapshot.feed, snapshot.date)
    }
}
