import Foundation

// ---------------------------------------------------------------------------
// CarPlay reading (card W5g): the Foundation-only half of the CarPlay scene.
//
// The operator picked design C ("Rows with gauges") on 2026-11-20. The root
// template is ONE CPListTemplate with five rows: Cluster (four node gauges as
// a CPListImageRowItem), Boards, Running, Needs you and Merge queue. Every
// number on that screen comes from the same readers the apps 1.4 widgets use —
// `WidgetDataClient` for the reads, `ClusterNodeTally` / `ServingSummary` for
// the fleet, `NeedsMeReading` for the alert feed and `MergeQueueReading` for
// the queue — so the car display cannot disagree with the tiles.
//
// WHY THIS FILE IS FOUNDATION-ONLY. CarPlay itself does not exist on macOS and
// a CarPlay template cannot be drawn headlessly, but the five rows' copy, the
// temperature bands, the 30 s staleness rule and the "never All clear after a
// failed fetch" rule are pure data. Keeping them here lets
// `scripts/carplay_check.sh` COMPILE AND RUN the exact strings the scene
// renders, instead of transcribing them into a golden that drifts.
//
// READ-ONLY. Nothing here writes: no App Group suite, no keychain, no HTTP
// method but the client's GET. The detail templates are lists, not actions;
// the operator acks on the phone, never from the car.
// ---------------------------------------------------------------------------

enum CarPlayRefresh {
    /// The scene refreshes every 30 s while connected (SPEC CarPlay §Refresh).
    /// Staleness is 2× that, the Native kit's rule.
    static let seconds: TimeInterval = 30
    static let staleAfter: TimeInterval = 2 * seconds
}

// MARK: - Temperature bands (the Native kit contract)

enum CarPlayTempBand: String, Equatable {
    case ok
    case warn
    case bad
    case unknown
}

enum CarPlayTemperature {
    /// < 70 green, 70-74 orange, >= 75 red. Nil is "no reading", never 0 °C.
    static func band(_ celsius: Double?) -> CarPlayTempBand {
        guard let celsius else { return .unknown }
        if celsius >= 75 { return .bad }
        if celsius >= 70 { return .warn }
        return .ok
    }

    static func celsius(_ value: Double?) -> String {
        guard let value else { return "—" }
        return "\(Int(value.rounded()))°"
    }
}

// MARK: - One node

/// A node row in the car screen: identity plus the four numbers C1 prints.
/// Every field is a route field (or the serving row that owns the node).
struct CarPlayNodeReading: Equatable, Identifiable {
    let label: String
    let model: String?
    let isUp: Bool
    let isOrchestrator: Bool
    let utilPct: Double?
    let tempC: Double?
    let powerW: Double?
    let tps: Double?

    init(_ node: FleetNodeLine) {
        self.label = node.label
        self.model = node.model
        self.isUp = node.isUp
        self.isOrchestrator = node.isOrchestrator
        self.utilPct = node.gpuPct
        self.tempC = node.gpuTempC ?? node.cpuTempC
        self.powerW = node.gpuPowerW
        self.tps = node.genTps
    }

    var id: String { label }

    var tempText: String { CarPlayTemperature.celsius(tempC) }
    var utilText: String { utilPct.map { "\(Int($0.rounded()))%" } ?? "—" }
    var tpsText: String { tps.map { "\(Int($0.rounded())) t/s" } ?? "idle" }
    var powerText: String { powerW.map { "\(Int($0.rounded())) W" } ?? "— W" }
    var tempBand: CarPlayTempBand { CarPlayTemperature.band(tempC) }

    /// 0...90 °C, the gauge range the SPEC fixes.
    var gaugeValue: Double { min(max(tempC ?? 0, 0), 90) }

    /// The image row's title: the short name, with the data's own ◆ mark for
    /// the orchestrator (never a hard-coded address or position).
    var rowTitle: String { label + (isOrchestrator ? " ◆" : "") }

    /// The image row's subtitle: "34% · 41 t/s".
    var rowSubtitle: String { "\(utilText) · \(tpsText)" }

    /// The SPEC's subtitle-equivalent, used in the pushed node list and by the
    /// gate: "S1 · 34% · 41 t/s".
    var detailLine: String { "\(label) · \(utilText) · \(tpsText)" }

    /// The pushed node list's second line: throughput, temp and power.
    var detailText: String {
        var parts = [tpsText, tempText, powerText]
        if let model { parts.append(model) }
        if isOrchestrator { parts.append("orchestrator") }
        if !isUp { parts.insert("down", at: 0) }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Cluster

struct CarPlayClusterReading: Equatable {
    let nodes: [CarPlayNodeReading]
    let tally: ClusterNodeTally
    let tokPerSec: Double?
    let dataAt: Date?

    /// Nil when the route answered degraded: a failed sample is not an empty
    /// fleet, and the scene keeps the last good rows instead.
    init?(response: MonitorResponse, fetchedAt: Date) {
        guard !response.isDegraded else { return nil }
        let serving = ServingSummary(rows: response.serving)
        self.nodes = (response.json?.hosts ?? []).map {
            CarPlayNodeReading(FleetNodeLine(host: $0, serving: response.serving))
        }
        self.tally = ClusterNodeTally.project(response)
        self.tokPerSec = serving.genTps
        self.dataAt = CarPlayDate.monitor(response, fetchedAt: fetchedAt)
    }

    var up: Int? { tally.nodesUp }
    var total: Int? { tally.nodesTotal }
    var tokText: String {
        guard let tokPerSec else { return "— t/s" }
        return "\(Int(tokPerSec.rounded())) t/s"
    }

    /// "4/4 up", "3/4 up", "Down" or "No data" — the C1 health line's words.
    var headline: String {
        guard let up, let total else { return "No data" }
        if total > 0 && up == 0 { return "Down" }
        return "\(up)/\(total) up"
    }

    var detailLine: String { "\(headline) · \(tokText)" }

    /// `name ×N` per model, in route order — the same footer rule C1 uses.
    var modelLine: String {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for node in nodes {
            guard let model = node.model else { continue }
            if counts[model] == nil { order.append(model) }
            counts[model, default: 0] += 1
        }
        if order.isEmpty { return "no models" }
        return order.map { "\($0) ×\(counts[$0] ?? 0)" }.joined(separator: " · ")
    }

    /// The four gauges the image row draws, in route order.
    var gauges: [CarPlayNodeReading] { Array(nodes.prefix(4)) }

    var hottest: CarPlayNodeReading? {
        nodes.filter { $0.tempC != nil }
            .max { ($0.tempC ?? 0) < ($1.tempC ?? 0) }
    }
}

// MARK: - Boards (B3 stacked bars)

enum CarPlayBarKind: String, Equatable {
    case running
    case todo
    case blocked
}

struct CarPlayBarSegment: Equatable {
    let kind: CarPlayBarKind
    let count: Int
}

/// One board's B3 row. Counts stay optional so a board the payload did not
/// carry cannot be rendered as zero.
struct CarPlayBoardReading: Equatable, Identifiable {
    let board: String
    let running: Int?
    let todo: Int?
    let blocked: Int?
    let done: Int?

    var id: String { board }
    var openCards: Int { (running ?? 0) + (todo ?? 0) + (blocked ?? 0) }
    var hasActivity: Bool {
        [running, todo, blocked, done].contains { ($0 ?? 0) > 0 }
    }

    /// running (accent) / todo (secondary) / blocked (orange), in that order.
    var segments: [CarPlayBarSegment] {
        [CarPlayBarSegment(kind: .running, count: running ?? 0),
         CarPlayBarSegment(kind: .todo, count: todo ?? 0),
         CarPlayBarSegment(kind: .blocked, count: blocked ?? 0)]
    }

    var detailLine: String {
        "\(running ?? 0) running · \(todo ?? 0) todo · "
            + "\(blocked ?? 0) blocked · \(done ?? 0) done"
    }
}

struct CarPlayBoardsReading: Equatable {
    let boards: [CarPlayBoardReading]
    let running: Int
    let blocked: Int
    let done: Int
    let dataAt: Date?

    /// Nil for a degraded envelope or a truncated one: those are refusals, not
    /// an under-count, so the scene keeps the last good rows.
    init?(summary: MobileSummaryResponse) {
        guard !summary.degraded else { return nil }
        if summary.truncated == true || (summary.omitted_boards ?? 0) > 0 { return nil }
        let usable = summary.entries.filter { $0.error == nil && $0.counts != nil }
        guard !usable.isEmpty || summary.entries.isEmpty else { return nil }
        let rows = usable.compactMap { tile -> CarPlayBoardReading? in
            guard let counts = tile.counts else { return nil }
            return CarPlayBoardReading(board: tile.board,
                                       running: counts.running,
                                       todo: counts.todo,
                                       blocked: counts.blocked,
                                       done: counts.done)
        }
        self.boards = rows.filter(\.hasActivity).sorted { lhs, rhs in
            lhs.openCards == rhs.openCards
                ? lhs.board < rhs.board
                : lhs.openCards > rhs.openCards
        }
        self.running = rows.reduce(0) { $0 + ($1.running ?? 0) }
        self.blocked = rows.reduce(0) { $0 + ($1.blocked ?? 0) }
        self.done = rows.reduce(0) { $0 + ($1.done ?? 0) }
        self.dataAt = CarPlayDate.parse(summary.generated_at)
    }

    /// The image draws the top three boards; the detail list draws every row.
    var topBoards: [CarPlayBoardReading] { Array(boards.prefix(3)) }
    var maxOpen: Int { max(boards.map(\.openCards).max() ?? 0, 1) }

    /// "2 running · 36 blocked · 89 done 24h" — the SPEC's row detail.
    var detailLine: String {
        "\(running) running · \(blocked) blocked · \(done) done 24h"
    }
}

// MARK: - Running (R3 longest)

enum CarPlayHeartbeat: Equatable {
    case fresh(seconds: Int)
    case late(seconds: Int)
    case unknown

    var isLate: Bool { if case .late = self { return true }; return false }
}

enum CarPlayDuration {
    /// R3's elapsed vocabulary: "32m", "1h 05m", "2d 3h".
    static func elapsed(_ seconds: Int?) -> String {
        guard let seconds else { return "—" }
        let s = max(0, seconds)
        if s < 60 { return "\(s)s" }
        if s < 3_600 { return "\(s / 60)m" }
        if s < 86_400 {
            return "\(s / 3_600)h " + String(format: "%02dm", (s % 3_600) / 60)
        }
        return "\(s / 86_400)d \((s % 86_400) / 3_600)h"
    }

    /// The R3 heartbeat dot: green under five minutes, orange after.
    static func heartbeat(_ seconds: Int?) -> CarPlayHeartbeat {
        guard let seconds else { return .unknown }
        let s = max(0, seconds)
        if s < 60 { return .fresh(seconds: s) }
        if s < 300 { return .fresh(seconds: s) }
        return .late(seconds: s)
    }

    static func heartbeatText(_ heartbeat: CarPlayHeartbeat) -> String {
        switch heartbeat {
        case .fresh(let s), .late(let s):
            if s < 60 { return "beat \(s)s" }
            return "beat \(s / 60)m"
        case .unknown:
            return "beat —"
        }
    }
}

struct CarPlayRunningCardReading: Equatable, Identifiable {
    let board: String?
    let cardID: String
    let title: String
    let worker: String?
    let elapsedSeconds: Int?
    let heartbeatSeconds: Int?

    init(_ row: CardsRunningRow) {
        self.board = row.board
        self.cardID = row.cardID
        self.title = row.title ?? row.cardID
        self.worker = row.worker
        self.elapsedSeconds = row.ageSeconds
        self.heartbeatSeconds = row.heartbeatSeconds
    }

    var id: String { "\(board ?? "?")/\(cardID)" }
    var elapsedText: String { CarPlayDuration.elapsed(elapsedSeconds) }
    var heartbeat: CarPlayHeartbeat { CarPlayDuration.heartbeat(heartbeatSeconds) }
    var heartbeatText: String { CarPlayDuration.heartbeatText(heartbeat) }
    var detailLine: String {
        [cardID, worker, elapsedText, heartbeatText]
            .compactMap { $0 }
            .joined(separator: " · ")
    }
}

struct CarPlayRunningReading: Equatable {
    let cards: [CarPlayRunningCardReading]
    let runningCount: Int?
    let blockedCount: Int?
    let dataAt: Date?

    init(projection: CardsRunningProjection, fetchedAt: Date) {
        self.cards = projection.rows.map(CarPlayRunningCardReading.init)
        self.runningCount = projection.runningCount
        self.blockedCount = projection.blockedCount
        self.dataAt = fetchedAt
    }

    /// `CardsRunningProjection` already sorts longest-running first.
    var longest: CarPlayRunningCardReading? { cards.first }

    /// The row's detail: the longest card's title, or the calm empty state.
    var detailLine: String {
        guard let longest else { return "Nothing running" }
        return longest.title
    }

    /// The image's big number: the longest elapsed time, or "—".
    var imageText: String { longest?.elapsedText ?? "—" }
}

// MARK: - Needs me (N3 calm when clear)

struct CarPlayNeedsReading: Equatable {
    let items: [NeedsMeItem]
    let count: Int
    let dataAt: Date?
    /// A failed fetch is a first-class state: `isClear` must be false even
    /// when the cached feed is empty, so the car never says "All clear" for a
    /// cluster it could not reach.
    let failed: Bool

    init(feed: NeedsMeFeed, failed: Bool, fetchedAt: Date) {
        self.items = feed.items
        self.count = feed.count
        self.failed = failed
        // The feed's own age is the age of THIS read, never the age of the
        // newest alert: a three-hour-old alert fetched one second ago is fresh
        // data saying something old. The item's age is rendered separately.
        self.dataAt = failed ? nil : fetchedAt
    }

    var topItem: NeedsMeItem? { items.first }
    var isClear: Bool { !failed && items.isEmpty }
    var imageText: String { failed ? "—" : (items.isEmpty ? "✓" : "\(count)") }
    var countText: String { "\(count) need you" }

    var detailLine: String {
        if failed { return "offline" }
        if let topItem { return topItem.title }
        if items.isEmpty { return "All clear" }
        return "\(count) need you"
    }

    /// The N3 caption under the count on the empty state.
    var clearLine: String {
        guard !failed else { return "offline" }
        guard items.isEmpty else { return detailLine }
        return "All clear"
    }
}

// MARK: - Merge queue (M1 pipeline)

struct CarPlayQueueReading: Equatable {
    let feed: MergeQueueFeed
    let dataAt: Date?

    init(feed: MergeQueueFeed) {
        self.feed = feed
        self.dataAt = CarPlayDate.parse(feed.generatedAt)
    }

    var isOff: Bool { feed.isOff }
    var offLine: String {
        "Merge queue off for \(feed.offProject ?? "this project")"
    }

    /// "off" wins over "empty": a disabled queue has no rows, and drawing that
    /// as an empty queue would say the opposite of what the route stated.
    var detailLine: String {
        if feed.isUnavailable { return "offline" }
        if feed.isOff { return offLine }
        if let current = feed.current {
            return "\(current.prLabel) in "
                + "\(MergeQueueReading.stageName(current.state)) · "
                + "\(feed.waitingCount) waiting"
        }
        if let next = feed.nextQueued {
            return "next \(next.prLabel) · \(feed.waitingCount) waiting"
        }
        if let last = feed.lastMerged {
            return "last \(last.prLabel) merged \(last.ageLabel) ago"
        }
        return "Merge queue empty"
    }

    /// The four-stage track's fill: done stages are green, the current one is
    /// accent, the rest stay track. A finished queue ends fully green.
    var stageProgress: Int {
        if let current = feed.current {
            return MergeQueueReading.stageProgress(current.state)
        }
        if let last = feed.lastMerged, feed.isEmpty || feed.rows.allSatisfy(\.isLast) {
            return MergeQueueReading.stageProgress(last.state)
        }
        return 0
    }

    var stageLabels: [String] { MergeQueueReading.stageLabels }
}

// MARK: - The state the scene renders

/// Every section keeps its last known value when a refresh fails, and the
/// failed flag is part of the state — so a row can be dimmed and said to be
/// offline without pretending the cached value is fresh.
struct CarPlayState: Equatable {
    var cluster: CarPlayClusterReading?
    var boards: CarPlayBoardsReading?
    var running: CarPlayRunningReading?
    var needs: CarPlayNeedsReading?
    var queue: CarPlayQueueReading?

    var clusterFailed = false
    var boardsFailed = false
    var runningFailed = false
    var needsFailed = false
    var queueFailed = false

    var fetchedAt: Date?

    init() {}

    mutating func applyCluster(_ value: CarPlayClusterReading?, failed: Bool, now: Date = Date()) {
        if let value { cluster = value }
        clusterFailed = failed
        fetchedAt = now
    }

    mutating func applyBoards(_ value: CarPlayBoardsReading?, failed: Bool, now: Date = Date()) {
        if let value { boards = value }
        boardsFailed = failed
        fetchedAt = now
    }

    mutating func applyRunning(_ value: CarPlayRunningReading?, failed: Bool, now: Date = Date()) {
        if let value { running = value }
        runningFailed = failed
        fetchedAt = now
    }

    mutating func applyNeeds(_ value: CarPlayNeedsReading?, failed: Bool, now: Date = Date()) {
        if let value { needs = value }
        needsFailed = failed
        fetchedAt = now
    }

    mutating func applyQueue(_ value: CarPlayQueueReading?, failed: Bool, now: Date = Date()) {
        if let value { queue = value }
        queueFailed = failed
        fetchedAt = now
    }

    /// Unconfigured / disconnected with no client: every row is offline and
    /// the Needs row can never claim "All clear".
    mutating func markAllFailed(now: Date = Date()) {
        clusterFailed = true
        boardsFailed = true
        runningFailed = true
        needsFailed = true
        queueFailed = true
        if needs == nil {
            needs = CarPlayNeedsReading(feed: .empty, failed: true, fetchedAt: now)
        }
        fetchedAt = now
    }

    /// The data's own clock for the header: the cluster sample is the primary
    /// fact; a section's own timestamp takes over when cluster is absent.
    var dataAt: Date? {
        cluster?.dataAt
            ?? boards?.dataAt
            ?? queue?.dataAt
            ?? running?.dataAt
            ?? needs?.dataAt
            ?? fetchedAt
    }

    /// "as of 06:34", or the stale/offline label when the cluster read is not
    /// fresh. The root template's section header carries it.
    var headerLabel: String {
        CarPlayFreshness.headerLabel(dataAt: cluster?.dataAt ?? dataAt,
                                     fetchedAt: fetchedAt,
                                     failed: clusterFailed)
    }

    var needsDetail: String {
        let label = CarPlayFreshness.label(dataAt: needs?.dataAt,
                                           fetchedAt: fetchedAt,
                                           failed: needsFailed)
        if needsFailed {
            // A failed fetch is never "All clear": cached items may still be
            // listed, but an empty cache shows only the offline label.
            if let needs, !needs.items.isEmpty {
                return [needs.detailLine, label].compactMap { $0 }.joined(separator: " · ")
            }
            return label ?? "offline"
        }
        return needs?.detailLine ?? "—"
    }

    /// The Needs detail list's calm-state row. `CarPlayNeedsReading` carries the
    /// fetch result it was built from, but the STATE knows the latest failure,
    /// which is what must win here.
    var needsEmptyText: String {
        if needsFailed { return "offline" }
        return needs?.clearLine ?? "—"
    }

    var boardsDetail: String {
        let base = boards?.detailLine ?? "—"
        return CarPlayFreshness.combine(base,
                                        dataAt: boards?.dataAt,
                                        fetchedAt: fetchedAt,
                                        failed: boardsFailed)
    }

    var runningDetail: String {
        let base = running?.detailLine ?? "Nothing running"
        return CarPlayFreshness.combine(base,
                                        dataAt: running?.dataAt,
                                        fetchedAt: fetchedAt,
                                        failed: runningFailed)
    }

    var queueDetail: String {
        let base = queue?.detailLine ?? "—"
        return CarPlayFreshness.combine(base,
                                        dataAt: queue?.dataAt,
                                        fetchedAt: fetchedAt,
                                        failed: queueFailed)
    }

    var clusterText: String {
        let base = cluster.map { "Cluster · \($0.headline) · \($0.tokText)" } ?? "Cluster"
        return CarPlayFreshness.combine(base,
                                        dataAt: cluster?.dataAt,
                                        fetchedAt: fetchedAt,
                                        failed: clusterFailed)
    }

    /// The Native kit's dim rule for rendered images: stale or offline content
    /// is drawn at 50%, exactly like `StaleModifier` does on the widgets.
    var clusterDimmed: Bool { isDimmed(cluster?.dataAt, clusterFailed) }
    var boardsDimmed: Bool { isDimmed(boards?.dataAt, boardsFailed) }
    var runningDimmed: Bool { isDimmed(running?.dataAt, runningFailed) }
    var needsDimmed: Bool { isDimmed(needs?.dataAt, needsFailed) }
    var queueDimmed: Bool { isDimmed(queue?.dataAt, queueFailed) }

    private func isDimmed(_ dataAt: Date?, _ failed: Bool) -> Bool {
        CarPlayFreshness.label(dataAt: dataAt, fetchedAt: fetchedAt,
                               failed: failed) != nil
    }
}

// MARK: - Freshness (the Native kit's stale/offline rule)

enum CarPlayFreshness {
    static func hhmm(_ date: Date?) -> String {
        guard let date else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }

    /// "stale · HH:mm" after 2× the 30 s refresh, "offline · HH:mm" after a
    /// failed fetch, nil while fresh. Cached data is never labelled fresh.
    static func label(dataAt: Date?, fetchedAt: Date?, failed: Bool,
                      now: Date = Date()) -> String? {
        if failed {
            return "offline · \(hhmm(dataAt ?? fetchedAt))"
        }
        guard let reference = dataAt ?? fetchedAt else { return nil }
        if now.timeIntervalSince(reference) > CarPlayRefresh.staleAfter {
            return "stale · \(hhmm(reference))"
        }
        return nil
    }

    static func headerLabel(dataAt: Date?, fetchedAt: Date?, failed: Bool,
                            now: Date = Date()) -> String {
        if let label = label(dataAt: dataAt, fetchedAt: fetchedAt,
                             failed: failed, now: now) {
            return label
        }
        return "as of \(hhmm(dataAt ?? fetchedAt))"
    }

    /// A row's detail with its freshness appended. When the base is already an
    /// offline/stale placeholder (or absent), the label stands alone so a row
    /// never reads "offline · offline · 06:34".
    static func combine(_ base: String, dataAt: Date?, fetchedAt: Date?,
                        failed: Bool, now: Date = Date()) -> String {
        guard let label = label(dataAt: dataAt, fetchedAt: fetchedAt,
                                failed: failed, now: now) else { return base }
        if base == "—" || base == "offline" || base == "stale" { return label }
        return "\(base) · \(label)"
    }
}

// MARK: - Dates

enum CarPlayDate {
    static func parse(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: raw) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: raw)
    }

    /// `/v1/cluster/monitor`'s `json.timestamp` is epoch seconds; a malformed
    /// or implausible stamp falls back to the fetch time (the data's clock is
    /// still the best fact we have).
    static func monitor(_ response: MonitorResponse, fetchedAt: Date) -> Date {
        if let raw = response.json?.timestamp,
           raw > 1_000_000_000,
           raw < Date().timeIntervalSince1970 + 86_400 {
            return Date(timeIntervalSince1970: raw)
        }
        return fetchedAt
    }
}
