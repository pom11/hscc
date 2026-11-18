import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// KanbanMonitorWidget.swift — `hscc kanban monitor --live` on the Home Screen
// and Lock Screen. READ-ONLY: three GETs through `WidgetDataClient`, no actions,
// and nothing written anywhere (the entry is built in memory per refresh, so §7
// is satisfied by never storing at all rather than by scrubbing).
//
// WHAT THE CLI SHOWS, AND WHAT THIS PANEL MAY SHOW
//
// `hscc kanban monitor --live` (hscc_daemon/kanban_live.py) draws per pass: a
// headline of `n/cap slots`, a tokens table, the RUNNING table (`Board Card
// Worker Run Beat Tokens` + title), a `board read failed` panel for a pass that
// read no board at all, `ready, waiting for a slot (N)`, a per-board status
// table with `done 24h`, and a `last hour` event table. The CLI reads the board
// databases and the profiles' state.db directly. A widget may read only what §3's
// allowlist serves (`WIDGET_DATA_CONTRACT.md`), so this panel carries the columns
// the three kanban routes actually send — board, card, worker, run/age, title,
// block reason — and draws NO COLUMN for the rest. Heartbeat age, tokens, the
// `max_in_progress` slot cap, orchestrators, `done 24h` and the event stream are
// not in the trio payload: here they are ABSENT, not zero and not guessed
// (§6 rule 1 — a status the payload did not send renders `—`).
//
// THE THREE READS ARE NOT INTERCHANGEABLE. Measured 2026-11-17 (fleet up, one
// pass): `/v1/kanban/blocked` answered `count: 18` while
// `/v1/kanban/stale?older_than=0` answered `count: 15` with the histogram
// `6 todo / 6 ready / 3 running` — zero `blocked`. So `blocked` comes from
// `/v1/kanban/blocked` and nowhere else: deriving it from stale's histogram
// prints a confident 0 next to eighteen stuck cards. stale's `running` rows (3)
// did match `/v1/kanban/running`'s `count` exactly, so the other open statuses
// ARE trustworthy from that one cheap read — and the same card id can arrive on
// both routes, which is why `mergedRows` dedupes by id with the running route as
// authority for live work.
//
// ONE FAILURE RULE, NOT FIVE. Every read goes through `WidgetData.read`, so the
// stale-while-error decision (and the "an unconfigured app invites setup, it does
// not claim the cluster is down" rule) lives in `WidgetDataClient.swift`, not
// here. The three reads resolve independently: a count whose read failed renders
// `—` with a named strip, and only a pass that got NOTHING back draws the error
// pane. That mirrors the CLI, which prints its error lines beside the tables it
// did manage to build (`render()`'s `blind` distinguishes exactly these two).
//
// WIRED TO THE ACTIVE TOGGLE / CADENCE (t_fa7926fb). Its registry entry is
// `WidgetSurfaceKind.widgetKanbanMonitor`: the provider reads it before it builds
// a client, so a paused surface sends ZERO requests and draws the honest "Paused
// in Settings" pane in every family — never its last-known numbers, which is what
// a paused panel showing a busy board would be. It deliberately does NOT borrow
// `.widgetMonitor`'s entry: switching off the cluster monitor must not silently
// blank the board monitor.
// ---------------------------------------------------------------------------

// MARK: - The projection

/// One row of the panel's row list: the trio merged into what a row draws.
/// This is the storage shape too — a projection of decoded payloads, never a
/// response (§7). `title` is the only free text a row carries and it is never
/// stored; `why` / `comments` / `body` are not even decoded (`SharedModels.swift`).
struct KanbanMonitorRow: Identifiable {
    /// Which section of the CLI's panel this row belongs to, and therefore how
    /// it is coloured: live work, stuck, or queued waiting.
    enum Source {
        case liveWork
        case stuck
        case waiting

        func color(_ style: WidgetStyle) -> Color {
            switch self {
            case .liveWork: return style.okColor
            case .stuck: return style.badColor
            case .waiting: return style.secondaryTextColor
            }
        }

        /// The glyph the row leads with. All three names are already used in
        /// this target, so `sf_symbol_check.sh` has a host-system precedent.
        var symbol: String {
            switch self {
            case .liveWork: return "play.fill"
            case .stuck: return "exclamationmark.triangle.fill"
            case .waiting: return "square.stack.3d.up"
            }
        }
    }

    let id: String
    let board: String?
    let title: String?
    let source: Source
    /// The status string the payload sent (`ready`, `todo`, `triage`, …). nil
    /// means it sent none, which renders `—` rather than an invented word.
    let status: String?
    /// Days stuck, the route's own `age_days`. nil is "not sent", never 0.
    let ageDays: Int?
    /// The worker profile — the running route's `assignee`, i.e. the CLI's
    /// `Worker` column. nil when the row's route did not send one.
    let worker: String?
    /// Seconds the worker has held the card, derived from `started_at`. nil when
    /// the timestamp is absent or unparseable — the `Run` column then reads `—`.
    let runSeconds: Int?
    /// The block reason's CATEGORY (`needs_input`, `dependency`). The prose that
    /// explains it is deliberately unavailable (§7).
    let blockKind: String?

    /// A row is never blank: an absent title falls back to the card id, which is
    /// the identifier the operator can actually act on in the CLI.
    var label: String {
        if let title, !title.isEmpty { return title }
        return id
    }

    /// The row's trailing time — the CLI's `Run` column for live work and its
    /// `Waiting`/age column for everything else. Live work has a run time and no
    /// `age_days`; a stuck card has the reverse. Choosing per row is what keeps
    /// the `large` family from printing `—` beside every running card just
    /// because it asked for ages.
    var timeText: String {
        source == .liveWork ? kanbanRunText(runSeconds) : kanbanAgeText(ageDays)
    }

    /// The row's status in the payload's own word when it sent one, else the
    /// section it was drawn in. Never invented, because the a11y line is read
    /// aloud as fact.
    var spokenStatus: String {
        if let status, !status.isEmpty { return status }
        switch source {
        case .liveWork: return "running"
        case .stuck: return "blocked"
        case .waiting: return "waiting"
        }
    }
}

/// The merged, capped row list plus the three counts, in the order §5.3 binds
/// them: running / ready / blocked.
struct KanbanMonitorCounts {
    /// nil = this read did not come back, so the chip renders `—`.
    let running: Int?
    let ready: Int?
    let blocked: Int?
}

// MARK: - Entry

/// One rendered pass of the kanban monitor panel. Follows `ClusterEntry`'s and
/// `MonitorEntry`'s honesty pattern: unconfigured, failed and stale are
/// first-class states, so there is no code path that leaves the panel blank.
struct KanbanMonitorEntry: TimelineEntry {
    let date: Date
    /// Whether host/port/token exist in the App Group. Checked FIRST by every
    /// family (`widget_view_dispatch_check.sh`'s invariant): an unconfigured app
    /// invites setup, it never claims "can't reach the cluster".
    let configured: Bool
    let counts: KanbanMonitorCounts
    /// The rows to draw, already merged, deduped, sorted and capped at
    /// `maxRows` — the largest list any family renders (§5.3: large = 7 rows).
    let rows: [KanbanMonitorRow]
    /// Set when NO dataset came back: the family draws the error pane. nil
    /// whenever at least one count or row on screen is real.
    let failure: WidgetDataFailure?
    /// Set when SOME datasets came back and others did not: the counts that
    /// failed render `—` and this names why, so a partial panel is never
    /// mistaken for a quiet board.
    let partialFailure: WidgetDataFailure?
    /// Minutes since the oldest value on screen — set only when a value was kept
    /// through `WidgetDataOutcome.stale`. nil means everything shown is from
    /// this pass, so no age is claimed.
    let ageMinutes: Int?
    /// The refresh cadence for the timeline (0 = let the OS decide).
    let refreshMinutes: Int
    /// True when the operator paused THIS surface in Settings (t_fa7926fb).
    ///
    /// A paused entry carries no counts, no rows and no failure: it is neither
    /// live data, nor stale data, nor an error, and drawing any of those instead
    /// of an honest "paused" is exactly the failure `ClusterEntry.paused` exists
    /// to avoid. A widget cannot remove itself programmatically, so the paused
    /// pane IS the pause — and the provider never sends a request that could
    /// have produced a number to draw here.
    let paused: Bool

    /// The largest row list any family draws (§5.3's `large` row count).
    static let maxRows = 7
    /// Title caps per §5.3: 40 characters for small/medium, 60 for large.
    static let titleCap = 40
    static let titleCapLarge = 60

    static let unconfigured = KanbanMonitorEntry(date: .now,
                                                 configured: false,
                                                 counts: KanbanMonitorCounts(running: nil, ready: nil, blocked: nil),
                                                 rows: [],
                                                 failure: nil,
                                                 partialFailure: nil,
                                                 ageMinutes: nil,
                                                 refreshMinutes: KanbanMonitorTimelineProvider.defaultRefreshMinutes,
                                                 paused: false)

    /// The paused state (t_fa7926fb): the cadence it was built with and nothing
    /// else. No counts, no rows, no failure, no snapshot — the operator asked
    /// for no data, so the surface shows none.
    static func paused(refreshMinutes: Int) -> KanbanMonitorEntry {
        KanbanMonitorEntry(date: .now,
                           configured: true,
                           counts: KanbanMonitorCounts(running: nil, ready: nil, blocked: nil),
                           rows: [],
                           failure: nil,
                           partialFailure: nil,
                           ageMinutes: nil,
                           refreshMinutes: refreshMinutes,
                           paused: true)
    }

    // MARK: Gate copy (unconfigured / paused)

    /// The line the gate pane draws in the Home Screen families. One owner for
    /// the copy, so six families cannot invent six paused messages — and the
    /// setup invite still wins when both gates apply (audit-3, t_5c554c5b): an
    /// app that was never configured has not "paused" anything.
    var gateLine: String {
        if !configured { return "Set up the app to see the board" }
        return "Paused in Settings"
    }

    /// The label the accessory families draw for a gate state. `accessoryInline`
    /// and `accessoryCircular` have no room for a pane, so they draw this, and
    /// because the check inside it puts the setup invite FIRST, neither can ever
    /// render "paused" on a device that was simply never set up.
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        return "Paused"
    }

    /// A first-render / placeholder entry: real shape, sample rows, no claim of
    /// liveness beyond the fact that it is a placeholder the system replaces.
    static var placeholder: KanbanMonitorEntry {
        KanbanMonitorEntry(date: .now,
                           configured: true,
                           counts: KanbanMonitorCounts(running: 3, ready: 6, blocked: 1),
                           rows: [
                            KanbanMonitorRow(id: "t_0a786117", board: "hscc",
                                             title: "ios: Kanban monitor widget",
                                             source: .liveWork, status: "running", ageDays: nil,
                                             worker: "ios-engineer", runSeconds: 1_800, blockKind: nil),
                            KanbanMonitorRow(id: "t_049d6986", board: "default",
                                             title: "QA-3: surface pending manual items",
                                             source: .stuck, status: "blocked", ageDays: 11,
                                             worker: "backend-engineer", runSeconds: nil, blockKind: "needs_input"),
                           ],
                           failure: nil,
                           partialFailure: nil,
                           ageMinutes: nil,
                           refreshMinutes: KanbanMonitorTimelineProvider.defaultRefreshMinutes,
                           paused: false)
    }
}

// MARK: - Timeline provider

/// Fetches the trio and turns the outcomes into an entry. Every read is
/// read-only, named, and goes through `WidgetDataClient` — which is GET-only by
/// construction and refuses any path outside its allowlist before a URL exists.
struct KanbanMonitorTimelineProvider: TimelineProvider {
    /// The cadence when the operator has never touched the settings screen: the
    /// shared widget default (`AppGroupWidgetSettings.setting(for:)` gives a
    /// self-fetching widget 5 minutes), which is what this file used while
    /// `WidgetSurfaceKind` had no case for this surface. Since t_fa7926fb the
    /// registry's answer wins and this is only the fallback for a store that was
    /// never written.
    static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: kanban_monitor_cadence
    /// The ONE cadence decision, kept free of WidgetKit so a gate can execute
    /// it: nil means Off (cadence picker at 0) — the timeline gets
    /// `policy: .atEnd` and the OS wakes the widget when it likes — and a date
    /// means `.after(date)`. Written inline, the Off branch (the one that
    /// becomes a refresh loop if it is wrong) would be the only branch in this
    /// file no gate ever runs.
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: kanban_monitor_cadence

    /// The operator's entry for THIS surface, or nil when the registry blob has
    /// never been written. `setting(for:)` fills an absent surface in with ON at
    /// the widget cadence, so nil here means "the app has never run its settings
    /// screen" — never "paused". That distinction is why a fresh install still
    /// renders: reading nil as paused would blank every widget on first boot.
    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetKanbanMonitor)
    }

    func placeholder(in context: Context) -> KanbanMonitorEntry {
        .placeholder
    }

    func getSnapshot(in context: Context, completion: @escaping (KanbanMonitorEntry) -> Void) {
        // The gallery preview reads the SAME registry entry the timeline does, so
        // a paused widget previews as paused instead of showing a busy board the
        // operator explicitly turned off.
        let surface = Self.surface()
        Task { completion(await fetchEntry(surface: surface)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<KanbanMonitorEntry>) -> Void) {
        Task {
            let surface = Self.surface()
            let entry = await fetchEntry(surface: surface)
            // 0 = Off in the cadence picker: no scheduled refresh, the OS
            // decides. Same shape as `ClusterWidgetTimelineProvider.getTimeline`.
            guard let next = Self.nextRefreshDate(
                refreshMinutes: surface?.refreshMinutes ?? Self.defaultRefreshMinutes
            ) else {
                completion(Timeline(entries: [entry], policy: .atEnd))
                return
            }
            completion(Timeline(entries: [entry], policy: .after(next)))
        }
    }

    // MARK: Fetch

    /// Always returns a renderable entry: unconfigured, paused and failed are
    /// states, not blanks. The three reads run together (they are independent and
    /// the extension's budget is short) and each resolves on its own.
    private func fetchEntry(surface: WidgetSurfaceSettings?) async -> KanbanMonitorEntry {
        // Paused is decided BEFORE the client is built and before any read: a
        // paused surface spends zero of WIDGET_DATA_CONTRACT §2's request budget
        // and draws zero numbers — no live counts, no last-known snapshot, no
        // failure pane. `!isActive` can only come from a blob the app wrote, so
        // an install that never opened Settings cannot land here.
        if let surface, !surface.isActive {
            return .paused(refreshMinutes: surface.refreshMinutes)
        }
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else {
            // Not configured → the widget's ONE job is to invite setup. Never
            // fabricate a failure pane (t_5c554c5b, now structural in
            // `WidgetDataFailure.showsStaleData`: a snapshot does not mean the
            // operator configured a target).
            return .unconfigured
        }

        async let runningOutcome = WidgetData.read(using: client) { await $0.kanbanRunning() }
        async let blockedOutcome = WidgetData.read(using: client) { await $0.kanbanBlocked() }
        async let staleOutcome = WidgetData.read(using: client) { await $0.kanbanStale(olderThanDays: 0) }
        let (running, blocked, stale) = await (runningOutcome, blockedOutcome, staleOutcome)

        return KanbanMonitorEntry.resolve(running: running, blocked: blocked, stale: stale,
                                          refreshMinutes: surface?.refreshMinutes
                                              ?? Self.defaultRefreshMinutes)
    }
}

// MARK: - Resolving three outcomes into one entry

extension KanbanMonitorEntry {
    /// Fold the three outcomes into the entry. Pure, so the shape of the panel is
    /// a function of the reads and nothing else.
    ///
    /// The failure this panel names is the FIRST one seen, in the order the CLI's
    /// headline depends on (running, then blocked, then stale). All three go to
    /// the same host through the same client, so the first failure is
    /// representative; `WidgetDataFailure` carries no prose and no address, so it
    /// is safe to render as is.
    static func resolve(running: WidgetDataOutcome<KanbanRunningLite>,
                        blocked: WidgetDataOutcome<KanbanBlockedLite>,
                        stale: WidgetDataOutcome<KanbanStaleLite>,
                        refreshMinutes: Int) -> KanbanMonitorEntry {
        let firstFailure = running.failure ?? blocked.failure ?? stale.failure

        guard running.value != nil || blocked.value != nil || stale.value != nil else {
            // Nothing usable. `firstFailure` cannot be nil here (an outcome with
            // no value is a `.failed`), and `.notConfigured` is only the
            // compile-time fallback — the unconfigured case never reaches here.
            return KanbanMonitorEntry(date: .now,
                                      configured: true,
                                      counts: KanbanMonitorCounts(running: nil, ready: nil, blocked: nil),
                                      rows: [],
                                      failure: firstFailure ?? .notConfigured,
                                      partialFailure: nil,
                                      ageMinutes: nil,
                                      refreshMinutes: refreshMinutes,
                                      // Every state `resolve` can produce is a
                                      // READ — live, stale or failed. Paused is
                                      // never built here: the provider returns
                                      // `.paused` before it has a client.
                                      paused: false)
        }

        let rows = mergedRows(running: running.value?.tasks ?? [],
                              blocked: blocked.value?.tasks ?? [],
                              waiting: stale.value?.tasks ?? [])

        return KanbanMonitorEntry(date: .now,
                                  configured: true,
                                  counts: KanbanMonitorCounts(running: runningCount(running.value),
                                                              ready: queueDepth(stale.value),
                                                              blocked: blockedCount(blocked.value)),
                                  rows: rows,
                                  failure: nil,
                                  partialFailure: firstFailure,
                                  ageMinutes: oldestAge(running: running, blocked: blocked, stale: stale),
                                  refreshMinutes: refreshMinutes,
                                  paused: false)
    }

    /// The route's own `count` when it sent one, else the rows it sent. Both
    /// absent means the payload never answered the question, so the chip renders
    /// `—` — a zero here would be the false-quiet-board failure §6 rule 1 forbids.
    static func runningCount(_ payload: KanbanRunningLite?) -> Int? {
        guard let payload else { return nil }
        return payload.count ?? payload.tasks?.count
    }

    static func blockedCount(_ payload: KanbanBlockedLite?) -> Int? {
        guard let payload else { return nil }
        return payload.count ?? payload.tasks?.count
    }

    /// Cards waiting for a slot: `ready` + `todo` rows counted from
    /// `stale?older_than=0`, which is the route that carries those statuses
    /// (measured: its histogram had 6 todo / 6 ready and NO blocked). Same rule
    /// `ClusterWidget.queueDepth(stale:)` uses, so the two widgets cannot
    /// disagree about what "waiting" means.
    static func queueDepth(_ payload: KanbanStaleLite?) -> Int? {
        guard let tasks = payload?.tasks else { return nil }
        return tasks.reduce(0) { acc, card in
            guard let s = card.status?.lowercased() else { return acc }
            return (s == "ready" || s == "todo") ? acc + 1 : acc
        }
    }

    /// The age to announce when any dataset on screen was kept through an error:
    /// the OLDEST one, because understating staleness is the dishonest direction.
    static func oldestAge(running: WidgetDataOutcome<KanbanRunningLite>,
                          blocked: WidgetDataOutcome<KanbanBlockedLite>,
                          stale: WidgetDataOutcome<KanbanStaleLite>) -> Int? {
        let ages = [running.ageMinutes, blocked.ageMinutes, stale.ageMinutes].compactMap { $0 }
        return ages.max()
    }

    /// running rows first (the CLI's headline table is the RUNNING table), then
    /// everything stuck or waiting, oldest first — §5.3's row order, kept within
    /// the section rather than across it so live work is never pushed off a
    /// 7-row list by cards that have been idle for a week. Deduped by card id
    /// with the running route as authority: a running card appears on BOTH
    /// `/v1/kanban/running` and `/v1/kanban/stale` (measured: stale's 3 running
    /// rows matched running's `count`), and drawing it twice would double the
    /// one thing the operator scans for.
    static func mergedRows(running: [RunningCardLite],
                           blocked: [BlockedCardLite],
                           waiting: [StaleCardLite]) -> [KanbanMonitorRow] {
        var seen = Set<String>()
        var live: [KanbanMonitorRow] = []
        for card in running where seen.insert(card.id).inserted {
            live.append(KanbanMonitorRow(id: card.id,
                                         board: card.board,
                                         title: card.title,
                                         source: .liveWork,
                                         status: card.status,
                                         ageDays: nil,
                                         worker: card.assignee,
                                         runSeconds: secondsSince(card.started_at),
                                         blockKind: nil))
        }

        var stuck: [KanbanMonitorRow] = []
        for card in blocked where seen.insert(card.id).inserted {
            stuck.append(KanbanMonitorRow(id: card.id,
                                          board: card.board,
                                          title: card.title,
                                          source: .stuck,
                                          status: card.status,
                                          ageDays: card.age_days,
                                          worker: card.assignee,
                                          runSeconds: nil,
                                          blockKind: card.block_kind))
        }
        for card in waiting where seen.insert(card.id).inserted {
            stuck.append(KanbanMonitorRow(id: card.id,
                                          board: card.board,
                                          title: card.title,
                                          source: waitingSource(for: card.status),
                                          status: card.status,
                                          ageDays: card.age_days,
                                          worker: card.assignee,
                                          runSeconds: nil,
                                          blockKind: nil))
        }
        // An absent age sorts LAST: "the payload did not say how old" is not the
        // same fact as "it is new", and promoting it would hide the old cards.
        stuck.sort { ($0.ageDays ?? -1) > ($1.ageDays ?? -1) }

        return Array((live + stuck).prefix(maxRows))
    }

    /// A stale row whose own status says the card is being worked is live work,
    /// not queue depth — colour it like the CLI colours a `running` cell.
    static func waitingSource(for status: String?) -> KanbanMonitorRow.Source {
        let s = status?.lowercased() ?? ""
        return (s == "running" || s == "in_progress") ? .liveWork : .waiting
    }

    /// The `Run` column, from the running route's `started_at`.
    ///
    /// The route sends `2026-11-17T09:34:24+00:00`
    /// (`fixtures/v1_kanban_running.json`). `ISO8601DateFormatter` with
    /// `.withInternetDateTime` parses that form, the `Z` form and a non-zero
    /// offset (probed against the macOS SDK); anything else returns nil and the
    /// column renders `—` rather than a run time of 0.
    static func secondsSince(_ timestamp: String?) -> Int? {
        guard let timestamp, !timestamp.isEmpty else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        guard let date = formatter.date(from: timestamp) else { return nil }
        return max(0, Int(Date().timeIntervalSince(date)))
    }
}

// MARK: - Formatting (one spelling for every family)

/// A count that may not have been sent. `—`, never `0`.
private func kanbanCountText(_ value: Int?) -> String {
    guard let value else { return "—" }
    return "\(value)"
}

/// The `Run` column from seconds: `<1m` / `12m` / `3h` / `2d`.
private func kanbanRunText(_ seconds: Int?) -> String {
    guard let seconds else { return "—" }
    if seconds < 60 { return "<1m" }
    if seconds < 3_600 { return "\(seconds / 60)m" }
    if seconds < 86_400 { return "\(seconds / 3_600)h" }
    return "\(seconds / 86_400)d"
}

/// The age column from the route's `age_days`. `0` is "today", not "zero days
/// of a problem", so it says `<1d` rather than `0d`.
private func kanbanAgeText(_ days: Int?) -> String {
    guard let days else { return "—" }
    return days <= 0 ? "<1d" : "\(days)d"
}

/// Clip to the family's budget without an ellipsis eating the card id.
private func kanbanClip(_ text: String, _ limit: Int) -> String {
    text.count <= limit ? text : String(text.prefix(limit - 1)) + "…"
}

// MARK: - Widget

/// The `hscc kanban monitor --live` panel: what the workers on every board are
/// doing, read-only.
struct KanbanMonitorWidget: Widget {
    let kind = "KanbanMonitorWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetKanbanMonitor)
        return StaticConfiguration(kind: kind, provider: KanbanMonitorTimelineProvider()) { entry in
            KanbanMonitorWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Kanban Monitor")
        .description("What the board workers are doing — running, waiting, blocked. Read-only.")
        // The accessory families are iOS-only; the macOS desktop tile offers
        // the three system sizes (t_1143c19b).
        #if os(iOS)
        .supportedFamilies([.accessoryInline, .accessoryCircular, .accessoryRectangular,
                            .systemSmall, .systemMedium, .systemLarge])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
        #endif
    }
}

/// The single view rendered for every family, choosing the layout by size.
struct KanbanMonitorWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            KanbanMonitorInline(entry: entry)
        case .accessoryCircular:
            KanbanMonitorCircular(entry: entry)
        case .accessoryRectangular:
            KanbanMonitorRectangular(entry: entry)
        #endif
        case .systemLarge:
            LargeKanbanMonitor(entry: entry)
        case .systemExtraLarge:
            LargeKanbanMonitor(entry: entry)
        case .systemMedium:
            MediumKanbanMonitor(entry: entry)
        default:
            SmallKanbanMonitor(entry: entry)
        }
    }
}

// MARK: - Shared panes
//
// The gate order below is the invariant `widget_view_dispatch_check.sh` proves
// for the cluster widget, and every family here repeats it in the same order:
// unconfigured (invites setup) BEFORE failed (can't read the board) BEFORE
// content. Swapping the first two is the t_5c554c5b bug.
//
// Paused (t_fa7926fb) shares that FIRST step rather than joining after it: every
// body tests `!entry.configured || entry.paused` in ONE step, so no family can
// forget the second state and reach a number. The ordering above still holds
// inside the gate, because the copy both panes draw (`gateLine` /
// `shortGateLabel`) checks `configured` first.

/// The gate a family draws when it has nothing to draw: the setup invite
/// (t_5c554c5b — the ONLY thing an unconfigured app may show) or the honest
/// paused state (t_fa7926fb). One pane, two copies, chosen by `entry.paused`,
/// exactly as `ClusterWidgetViews`' gate and `BoardStatusGateView` do, so a
/// paused surface can never fall through to live counts, to stale numbers, or to
/// a blank. The accessory families draw `shortGateLabel` as text instead — a ring
/// has no room for three lines, and a paused gauge that still drew the running
/// count would be the false number §6 rule 1 refuses.
struct KanbanMonitorGateView: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    /// Accessory families are system-tinted: fonts still follow the style,
    /// colours stay the system's.
    var systemTinted = false
    private var primaryText: Color { systemTinted ? .primary : style.primaryTextColor }
    private var secondaryText: Color { systemTinted ? .secondary : style.secondaryTextColor }
    private var statusText: Color { systemTinted ? .primary : style.badColor }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
                .font(style.font(.subheadline))
                .foregroundColor(secondaryText)
            Text(entry.gateLine)
                .font(style.font(.caption2, weight: .semibold))
                .foregroundColor(primaryText)
            Text(entry.paused
                 ? "Turn it back on under Widgets & Live Activities in the HSCC app."
                 : WidgetDataFailure.notConfigured.operatorLine)
                .font(style.font(.caption2))
                .foregroundColor(secondaryText)
                .lineLimit(3)
        }
    }
}

/// The error pane: a pass that got NOTHING back. The title is the CLI's own
/// (`kanban_live.py::board_error_panel`), so the widget and the terminal blame
/// the same thing in the same words. No count on this pane is `0`: nothing was
/// read, and a zero would claim a quiet board.
struct KanbanMonitorErrorPane: View {
    @Environment(\.widgetStyle) private var style
    let failure: WidgetDataFailure
    /// `large` has room for the sentence; the tighter families take the label.
    var detailed = false

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                    .font(style.font(.subheadline))
                    .foregroundColor(style.badColor)
                Text("board read failed")
                    .font(style.font(.caption, weight: .semibold))
                    .foregroundColor(style.primaryTextColor)
            }
            Text(detailed ? failure.operatorLine : failure.shortLabel)
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
                .lineLimit(detailed ? 3 : 1)
            Text("counts unknown")
                .font(style.font(.caption2))
                .foregroundColor(style.secondaryTextColor)
        }
    }
}

/// The three headline counts (§5.3). A count whose read failed is `—`;
/// `compact` is the single-line spelling the small family and the rectangular
/// accessory can afford.
struct KanbanMonitorCountsLine: View {
    @Environment(\.widgetStyle) private var style
    let counts: KanbanMonitorCounts
    var compact = false

    var body: some View {
        HStack(spacing: compact ? style.spacing(Theme.Spacing.sm.rawValue) : style.spacing(Theme.Spacing.lg.rawValue)) {
            chip("play.fill", "run", counts.running, style.okColor)
            chip("square.stack.3d.up", "ready", counts.ready, style.secondaryTextColor)
            chip("exclamationmark.triangle.fill", "blocked", counts.blocked,
                 (counts.blocked ?? 0) > 0 ? style.badColor : style.secondaryTextColor)
            Spacer(minLength: 0)
        }
    .widgetHeader(style)
    }

    private func chip(_ symbol: String, _ name: String, _ value: Int?, _ color: Color) -> some View {
        HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Image(systemName: symbol).widgetIcon(style)
                .font(style.font(size: compact ? 7 : 9))
                .foregroundColor(color)
            Text(kanbanCountText(value))
                .font(style.font(compact ? .caption2 : .subheadline, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            Text(name)
                .font(style.font(size: compact ? 7 : 9))
                .foregroundColor(style.secondaryTextColor)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(name) \(kanbanCountText(value))")
    }
}

/// One row: glyph, clipped title, board, and the time it can prove.
/// `showWorker` / `showBoard` / `showTime` / `showBlockKind` are the family
/// budgets §5.3 binds (`medium` = title + board, `large` adds age + `block_kind`).
struct KanbanMonitorRowView: View {
    @Environment(\.widgetStyle) private var style
    let row: KanbanMonitorRow
    var titleLimit = KanbanMonitorEntry.titleCap
    var showWorker = false
    var showBoard = true
    var showTime = false
    var showBlockKind = false

    var body: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Image(systemName: row.source.symbol).widgetIcon(style)
                .font(style.font(size: 7))
                .foregroundColor(row.source.color(style))
            VStack(alignment: .leading, spacing: 0) {
                Text(kanbanClip(row.label, titleLimit))
                    .font(style.font(.caption2, weight: .medium))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                if showWorker, let worker = row.worker, !worker.isEmpty {
                    // The CLI's `Worker` column — which profile holds the card.
                    Text(worker)
                        .font(style.font(size: 7))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: style.spacing(Theme.Spacing.xxs.rawValue))
            if showBlockKind, let kind = row.blockKind, !kind.isEmpty {
                Text(kind)
                    .font(style.font(size: 7))
                    .foregroundColor(style.badColor)
                    .lineLimit(1)
            }
            if showBoard, let board = row.board, !board.isEmpty {
                Text(board)
                    .font(style.font(size: 7))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }
            if showTime {
                Text(row.timeText)
                    .font(style.font(size: 7).monospacedDigit())
                    .foregroundColor(style.secondaryTextColor)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityText)
    }

    /// An unknown time says so in words — a dash read aloud is silence, and a
    /// `0` read aloud would be a lie.
    private var accessibilityText: String {
        var parts = [row.spokenStatus, row.label]
        if let board = row.board, !board.isEmpty { parts.append("board \(board)") }
        if row.source == .liveWork {
            parts.append(row.runSeconds.map { "running \($0) seconds" } ?? "run time unknown")
        } else {
            parts.append(row.ageDays.map { "stuck \($0) days" } ?? "age unknown")
        }
        return parts.joined(separator: ", ")
    }
}

/// The age line: shown only when something on screen is kept through an error.
struct KanbanMonitorAgeLine: View {
    @Environment(\.widgetStyle) private var style
    let ageMinutes: Int?

    var body: some View {
        if let ageMinutes {
            HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                Image(systemName: "calendar.badge.clock").widgetIcon(style)
                    .font(style.font(size: 7))
                Text("last known · \(ageMinutes) min ago")
                    .font(style.font(size: 7))
            }
            .foregroundColor(style.warnColor)
        }
    }
}

/// The strip that says the panel is PARTIAL: some counts are `—` because their
/// read failed, and this is why. Without it a missing count reads as a quiet
/// board, which is the failure this panel exists to avoid.
struct KanbanMonitorPartialStrip: View {
    @Environment(\.widgetStyle) private var style
    let failure: WidgetDataFailure
    var detailed = false

    var body: some View {
        HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Image(systemName: "exclamationmark.triangle").widgetIcon(style)
                .font(style.font(size: 7))
            Text(detailed ? "partial · \(failure.operatorLine)" : "partial · \(failure.shortLabel)")
                .font(style.font(size: 7))
                .lineLimit(detailed ? 2 : 1)
        }
        .foregroundColor(style.warnColor)
    }
}

// MARK: - Accessory families (Lock Screen / Dynamic Island)

/// §5.3: `"6 running · 15 blocked"` from the two counts the accessory can afford.
/// An unread count is an em dash here too — the inline glyph never guesses.
struct KanbanMonitorInline: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            // Both gate states through ONE copy rule, and that rule itself puts
            // the setup invite first — an unconfigured device can never read as
            // "paused" (audit-3, t_5c554c5b).
            Text(entry.shortGateLabel).font(style.font(.caption2))
        } else if let failure = entry.failure {
            Text(failure.shortLabel).font(style.font(.caption2))
        } else {
            Text("\(kanbanCountText(entry.counts.running)) run · \(kanbanCountText(entry.counts.blocked)) blocked")
                .font(style.font(.caption2))
        }
    }
}

/// §5.3: the running count, red when anything is blocked.
struct KanbanMonitorCircular: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        if !entry.configured || entry.paused {
            // A LABEL, not a gauge: a paused ring that still drew the running
            // count would be the false number §6 rule 1 refuses, and the same
            // copy rule as the inline family puts setup first.
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
            Text(entry.shortGateLabel)
        } else if let failure = entry.failure {
            Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
            Text(failure.shortLabel)
        } else {
            Gauge(value: gaugeValue, in: 0...1) {
                EmptyView()
            } currentValueLabel: {
                Text(kanbanCountText(entry.counts.running))
            }
            .gaugeStyle(.accessoryCircularCapacity)
            .tint(tint)
        }
    }

    /// A blocked board is the alarming case, so it wins the colour even when the
    /// running count is healthy. An unknown count gauges at zero and shows `—`.
    private var tint: Color {
        if (entry.counts.blocked ?? 0) > 0 { return .primary }
        if entry.counts.running == nil { return .secondary }
        return .primary
    }

    private var gaugeValue: Double {
        guard let running = entry.counts.running else { return 0 }
        // Capacity fill is a shape, not a claim about a capacity the payload
        // never sent: the cap lives in the app config the trio cannot read, so
        // the ring saturates at 8 live workers rather than implying a total.
        return min(1, Double(running) / 8)
    }
}

/// §5.3: running / blocked on two lines, plus the top row's title.
struct KanbanMonitorRectangular: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            if !entry.configured || entry.paused {
                Text(entry.gateLine)
                    .font(style.font(.caption2, weight: .semibold))
            } else if let failure = entry.failure {
                Text("board read failed")
                    .font(style.font(.caption2, weight: .semibold))
                Text(failure.shortLabel)
                    .font(style.font(.caption2))
            } else {
                HStack(spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                    Text("\(kanbanCountText(entry.counts.running)) run")
                        .font(style.font(.caption, weight: .semibold))
                    Text("\(kanbanCountText(entry.counts.blocked)) blocked")
                        .font(style.font(.caption, weight: .semibold))
                        .foregroundColor((entry.counts.blocked ?? 0) > 0
                                         ? .primary
                                         : .primary)
                }
                .widgetHeader(style)
                if let row = entry.rows.first {
                    Text(kanbanClip(row.label, 20))
                        .font(style.font(.caption2))
                        .foregroundColor(.secondary)
                        .lineLimit(1)
                }
                KanbanMonitorAgeLine(ageMinutes: entry.ageMinutes)
            }
        }
    }
}

// MARK: - Home Screen families

/// §5.3 small: 3 counts + 1 title (≤40).
struct SmallKanbanMonitor: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                KanbanMonitorGateView(entry: entry)
            } else if let failure = entry.failure {
                KanbanMonitorErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                    KanbanMonitorCountsLine(counts: entry.counts, compact: true)
                    if let row = entry.rows.first {
                        KanbanMonitorRowView(row: row, showWorker: true, showBoard: false, showTime: true)
                    } else {
                        // No rows is a fact the payload sent (nothing open), not a
                        // failure — so it says so instead of leaving the tile empty.
                        Text("no open cards")
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    }
                    Spacer(minLength: style.spacing(Theme.Spacing.xs.rawValue))
                    if let failure = entry.partialFailure {
                        KanbanMonitorPartialStrip(failure: failure)
                    }
                    KanbanMonitorAgeLine(ageMinutes: entry.ageMinutes)
                }
            }
        }
    }
}

/// §5.3 medium: 3 counts + up to 4 rows `{title≤40, board}`.
struct MediumKanbanMonitor: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                KanbanMonitorGateView(entry: entry)
            } else if let failure = entry.failure {
                KanbanMonitorErrorPane(failure: failure)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    KanbanMonitorCountsLine(counts: entry.counts)
                    ForEach(entry.rows.prefix(4)) { row in
                        KanbanMonitorRowView(row: row)
                    }
                    if entry.rows.isEmpty {
                        Text("no open cards")
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    }
                    Spacer(minLength: 0)
                    if let failure = entry.partialFailure {
                        KanbanMonitorPartialStrip(failure: failure)
                    }
                    KanbanMonitorAgeLine(ageMinutes: entry.ageMinutes)
                }
            }
        }
    }
}

/// §5.3 large: 3 counts + up to 7 rows `{title≤60, board, age_days}`, blocked
/// rows also carrying `block_kind` — the category, never the prose behind it.
struct LargeKanbanMonitor: View {
    @Environment(\.widgetStyle) private var style
    let entry: KanbanMonitorEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                KanbanMonitorGateView(entry: entry)
            } else if let failure = entry.failure {
                KanbanMonitorErrorPane(failure: failure, detailed: true)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    KanbanMonitorCountsLine(counts: entry.counts)
                    ForEach(entry.rows) { row in
                        KanbanMonitorRowView(row: row,
                                             titleLimit: KanbanMonitorEntry.titleCapLarge,
                                             showWorker: true,
                                             showTime: true,
                                             showBlockKind: row.source == .stuck)
                    }
                    if entry.rows.isEmpty {
                        Text("no open cards")
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    }
                    Spacer(minLength: 0)
                    if let failure = entry.partialFailure {
                        KanbanMonitorPartialStrip(failure: failure, detailed: true)
                    }
                    KanbanMonitorAgeLine(ageMinutes: entry.ageMinutes)
                }
            }
        }
    }
}
