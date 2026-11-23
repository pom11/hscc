import Foundation

// ---------------------------------------------------------------------------
// NeedsMeReading.swift — the "Needs me" surface's data half.
//
// The main orchestrator records what needs the operator in the alert feed
// (`hscc notify`, `GET /v1/alerts`). This file is the ONE projection from that
// wire response to what a tile or the macOS dropdown draws, so the iOS widget,
// the macOS widget (compiled by path) and the menu bar section cannot disagree
// about the order, the unacknowledged filter or an age.
//
// ORDER. `GET /v1/alerts` answers ascending by id. `hscc notify list` reverses
// that page, so an operator reads the newest alert first; this projection does
// the same. `NeedsMeFeed.items` is therefore newest first.
//
// FILTER. A "needs me" surface is about what still needs the operator, so rows
// whose `acked` is true are omitted. An acked row's only remaining purpose is
// history, which `hscc notify list` still shows with its "(acked)" marker.
//
// NO RAW RESPONSE IS STORED. `NeedsMeSnapshotStore` persists `NeedsMeItem`
// values — id, timestamp, title, first body line and level — never the decoded
// wire object and never the server's `speak` prose.
// ---------------------------------------------------------------------------

/// The three alert levels, normalized once. An unknown future level is honest
/// `info` rather than a colour-less row that looks broken.
enum NeedsMeLevel: String, Codable, CaseIterable, Sendable {
    case critical
    case action
    case info

    init(raw: String?) {
        switch raw?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "critical": self = .critical
        case "action": self = .action
        default: self = .info
        }
    }

    /// Critical and action are the operator-facing work; info is FYI.
    var isUrgent: Bool { self != .info }

    var label: String { rawValue.capitalized }
}

/// One alert exactly as `GET /v1/alerts` sends it. `body` and the level are
/// optional so an older/partial row still decodes; an alert without an id,
/// timestamp or title is not renderable and is refused by the decoder.
struct AlertLite: Codable, Equatable, Identifiable, Sendable {
    let id: Int
    let ts: String
    let title: String
    let body: String?
    let level: String?
    let key: String?
    let acked: Bool?
    let ackedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, ts, title, body, level, key, acked
        case ackedAt = "acked_at"
    }
}

/// The route's list envelope. `has_more`/`latest_id` are kept because they are
/// the feed's own paging truth; no surface invents a count from them.
struct AlertsLite: Codable, Equatable, Sendable {
    let alerts: [AlertLite]
    let count: Int?
    let latestID: Int?
    let hasMore: Bool?
    /// The cursor to send as `since=` for the next ascending page. Present on
    /// the route; kept so the client can prove paging advanced (audit W15).
    let nextSince: Int?
    let speak: String?

    enum CodingKeys: String, CodingKey {
        case alerts, count, speak
        case latestID = "latest_id"
        case hasMore = "has_more"
        case nextSince = "next_since"
    }
}

/// One row the "Needs me" surface renders. The body is reduced to its first
/// line at projection time: a widget row is one line, and the rest of a long
/// alert body has no place on a timeline entry.
struct NeedsMeItem: Codable, Equatable, Identifiable, Hashable, Sendable {
    let id: Int
    let ts: String
    let title: String
    let bodyFirstLine: String
    let level: NeedsMeLevel

    init(id: Int, ts: String, title: String, bodyFirstLine: String,
         level: NeedsMeLevel) {
        self.id = id
        self.ts = ts
        self.title = title
        self.bodyFirstLine = bodyFirstLine
        self.level = level
    }

    init(alert: AlertLite) {
        self.init(id: alert.id,
                  ts: alert.ts,
                  title: alert.title,
                  bodyFirstLine: NeedsMeReading.firstLine(alert.body ?? ""),
                  level: NeedsMeLevel(raw: alert.level))
    }

    /// `2m` / `3h` / `4d`, the same abbreviations `hscc notify list` prints.
    func ageLabel(now: Date = Date()) -> String {
        NeedsMeReading.ageLabel(ts: ts, now: now)
    }
}

/// The unacknowledged rows, newest first, plus the envelope facts a caller may
/// want to display. Empty is a real answer ("nothing needs you"), not a failure.
struct NeedsMeFeed: Codable, Equatable, Sendable {
    let items: [NeedsMeItem]
    let latestID: Int?
    let hasMore: Bool?

    static let empty = NeedsMeFeed(items: [], latestID: nil, hasMore: nil)

    var count: Int { items.count }
    var newest: NeedsMeItem? { items.first }

    /// The ONE wire→surface projection: keep unacknowledged rows, newest first.
    static func from(_ payload: AlertsLite) -> NeedsMeFeed {
        let unacknowledged = payload.alerts
            .filter { $0.acked != true }
            .sorted { $0.id > $1.id }
            .map(NeedsMeItem.init(alert:))
        return NeedsMeFeed(items: unacknowledged,
                           latestID: payload.latestID,
                           hasMore: payload.hasMore)
    }
}

/// Pure formatting rules, executable outside WidgetKit (the decode gate runs
/// this file against the committed fixture).
enum NeedsMeReading {
    static let nothingNeedsYou = "Nothing needs you"
    static let largeRowLimit = 8
    static let mediumRowLimit = 3

    static func firstLine(_ body: String) -> String {
        body.split(whereSeparator: { $0.isNewline })
            .first
            .map { String($0).trimmingCharacters(in: .whitespaces) } ?? ""
    }

    static func date(from ts: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: ts)
    }

    /// The CLI's age vocabulary: seconds, minutes, hours, days.
    static func ageLabel(ts: String, now: Date = Date()) -> String {
        guard let stamp = date(from: ts) else { return "?" }
        let seconds = max(0, Int(now.timeIntervalSince(stamp)))
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86400 { return "\(seconds / 3600)h" }
        return "\(seconds / 86400)d"
    }

    /// The Lock Screen inline line: `"3 need you"` / `"nothing needs you"`.
    static func inlineLine(count: Int) -> String {
        count <= 0 ? "nothing needs you" : "\(count) need you"
    }

    /// The freshness line every family can afford. `ageMinutes` is nil when
    /// this pass is live.
    static func updatedLine(ageMinutes: Int?) -> String {
        guard let ageMinutes else { return "updated just now" }
        return "updated \(ageMinutes)m ago"
    }
}

/// Persists the last-known projection so an unreachable cluster keeps real rows
/// with their age instead of a blank tile. An empty feed is stored too: "nothing
/// needs you" is an answer, and pretending the last non-empty snapshot is still
/// current would invent work.
enum NeedsMeSnapshotStore {
    static let storageKey = "hscc.needsme.feed"

    struct Snapshot: Codable {
        let date: Date
        let feed: NeedsMeFeed
    }

    static func save(_ feed: NeedsMeFeed, at date: Date = Date()) {
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

    static func lastKnown() -> (value: NeedsMeFeed, at: Date)? {
        guard let snapshot = load() else { return nil }
        return (snapshot.feed, snapshot.date)
    }
}
