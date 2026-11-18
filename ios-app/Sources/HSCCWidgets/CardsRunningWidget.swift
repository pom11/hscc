import Foundation
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// CardsRunningWidget.swift — what is moving, and who is moving it.
//
// The phone answer to `hscc cards monitor --live`. That view prints one panel
// per running card and its header is `board · card · worker`, then run time,
// heartbeat age and the worker session's tokens, dropping fields right-to-left
// as the terminal narrows (`hscc_daemon/cards_live.py`, module docstring). A
// widget is the narrowest terminal there is, so it keeps the same three fields
// in the same order — board, card, worker — plus the one age it can actually
// know (see AGE below).
//
// `docs/WIDGET_DATA_CONTRACT.md` §5.4 is the spec and §8.3 the rule: a card
// list comes from the kanban trio, never `/v1/cards`. The reason is measured,
// not stylistic — `/v1/cards` has no field projection, so 8 rows cost 81,855 B
// of which 98.6 % is discarded (`body` alone is 81 %), while
// `/v1/kanban/running` sends the same rows already projected
// (`{board,id,title,assignee,status,started_at}`) for ~2 KB. This file therefore
// reads exactly TWO routes, which is also §2's per-refresh ceiling:
//
//   1. `GET /v1/kanban/running` — the rows and the server's own `count`.
//      Required: without it there is nothing to draw.
//   2. `GET /v1/kanban/blocked` — §5.4's "the failure rows". It is not a second
//      list, it is the answer to "the running list is empty, is the board idle,
//      or is everything stuck?". A widget that cannot answer that reads an idle
//      fleet and a jammed one the same way, so its absence nulls the blocked
//      number (`—`) rather than failing the surface.
//
// AGE — what this file does and does not know. `hscc cards monitor --live`
// prints TWO ages: run time and heartbeat age. The route now exposes both as
// `run_s` / `hb_s` (`kanban_live.CARD_VIEW_FIELDS`); this surface shows the run
// time exactly as the CLI's `run` column does, preferring the server's `run_s`
// and falling back to `started_at` on the older 2.15.11 server. A row never
// renders a heartbeat age it was not asked to show, and a missing or
// unparseable clock draws `—`, never `0m` (§6 rule 1, one step further than zero: an
// unparseable timestamp is not a card that started this instant).
//
// THE LOUD FAILURE (§5.4 + the card's own acceptance rule: a blind read must
// never look like an idle board). Four distinct states, never collapsed:
//
//   * not configured       → the setup invite, checked FIRST in every family
//                            (the audit-3 invariant `widget_view_dispatch_check`
//                            enforces for the cluster widget).
//   * paused in Settings    → "Paused in Settings", taken in the SAME step as
//                            the invite (t_fa7926fb, once `WidgetSurfaceKind`
//                            gained `widget.cardsRunning`). No rows, no counts,
//                            no last-known snapshot, and no request sent for
//                            any of it: a panel that kept drawing the fleet
//                            after the operator turned it off is the failure
//                            this state exists to refuse.
//   * read failed, nothing
//     usable               → the failure, rendered: `exclamationmark.triangle.fill`
//                            in `style.badColor` plus
//                            `WidgetDataFailure.shortLabel` / `.operatorLine`.
//                            No count, no row, and above all no `0 running` —
//                            `runningText` returns nil with no projection
//                            precisely so a zero cannot be produced from a blind
//                            read.
//   * read failed, last-known
//     snapshot             → the rows AND their age AND why this refresh did not
//                            answer, in `style.warnColor`. Stale is not quiet.
//
// STORAGE (§7): `CardsRunningProjection` (in `Sources/Shared/CardsRunningProjection.swift`,
// shared with the Live Activity driver) only — ids, board slugs, titles, worker
// names, statuses, minutes. No `body`, no `workspace_path`, no `branch`, no
// address, no token, no raw response `Data`, and writes go through
// `SharedStore.suite` (never a `.standard` fallback). Card titles are sanctioned
// widget copy (§5.3 budgets `tasks[].title` for every family); card prose is not,
// and `RunningCardLite` never decodes it.
//
// No configuration parameter, so no `AppIntentConfiguration`: the fleet-wide
// running set is one question with one answer, and `hscc cards monitor` with no
// `--board` is the very view this surface stands for. A board picker would
// narrow the same route, but the widget is deliberately the fleet answer, and
// the operator already has the board-scoped BoardStatus tile for one board.
// ---------------------------------------------------------------------------

// MARK: - The projection lives in Shared

// `CardsRunningRow`, `CardsRunningProjection` and
// `CardsRunningSnapshotStore` were moved to
// `Sources/Shared/CardsRunningProjection.swift` by t_71deac83, unchanged:
// the app-side running-cards Live Activity driver needs the same projection
// and the same last-known store, and one App Group key must have one decoder.
// The views below draw what that file projects.

// MARK: - The timeline entry

struct CardsRunningEntry: TimelineEntry {
    let date: Date
    /// When the data on screen was read. Equal to `date` for a live entry; the
    /// snapshot's timestamp for stale data. The footer draws it as a
    /// self-updating relative date, so "updated 3m ago" cannot stay frozen at
    /// "just now" while WidgetKit holds the entry.
    let updatedAt: Date
    /// nil = nothing usable came back; `failure` then says why, and no family
    /// may draw a count.
    let projection: CardsRunningProjection?
    /// Set only for stale-while-error: minutes since the stored rows were read.
    let ageMinutes: Int?
    /// Why there are no live numbers. Rendered, never swallowed.
    let failure: WidgetDataFailure?
    /// False only when the operator has no host/token yet — the setup invite.
    let configured: Bool
    /// True when the operator paused THIS surface in Settings (t_fa7926fb).
    ///
    /// A paused entry carries no projection and no failure: it is neither live
    /// data, nor stale data, nor an error, and drawing any of those instead of
    /// an honest "paused" is the failure `ClusterEntry.paused` exists to avoid.
    /// The widget cannot remove itself programmatically, so this pane IS the
    /// pause — and the provider never sends the two requests that could have
    /// produced rows to draw here.
    let paused: Bool

    static let sample = CardsRunningEntry(date: .now, updatedAt: .now, projection: .preview,
                                          ageMinutes: nil, failure: nil,
                                          configured: true, paused: false)

    static func unconfigured() -> CardsRunningEntry {
        CardsRunningEntry(date: .now, updatedAt: .now, projection: nil, ageMinutes: nil,
                          failure: .notConfigured, configured: false, paused: false)
    }

    /// The paused state (t_fa7926fb): nothing but the fact that it is paused.
    /// No projection, no snapshot, no failure — the operator asked for no data,
    /// so the surface shows none, and `CardsRunningSnapshotStore` is deliberately
    /// not consulted (stale rows on a paused panel would be the lie this state
    /// exists to refuse).
    static func paused() -> CardsRunningEntry {
        CardsRunningEntry(date: .now, updatedAt: .now, projection: nil, ageMinutes: nil,
                          failure: nil, configured: true, paused: true)
    }

    // MARK: Gate copy (unconfigured / paused)

    /// The line the gate pane draws. One owner, so six families cannot invent
    /// six paused messages, and the setup invite wins when both apply
    /// (audit-3 / t_5c554c5b): an app that was never configured has not paused
    /// anything.
    var gateLine: String {
        if !configured { return "Set up the app to see running cards." }
        return "Paused in Settings"
    }

    /// The label the accessory families draw for a gate state — the ~25-character
    /// budget §5.4 gives the inline row. `configured` is checked FIRST inside
    /// it, so a device that was simply never set up can never render "paused".
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        return "Paused"
    }

    // MARK: Text the families draw (one rule per string, so six families cannot
    // disagree about what the same entry means)

    /// `accessoryInline`: one short line, counts only (§5.4's inline row is
    /// "counts only — use row 2/3"). The entry owns the gate word; the numeric
    /// half is `CardsRunningCopy`, which the fixture check executes.
    var inlineLine: String {
        // Both gate states through ONE copy rule, and the rule itself puts the
        // setup invite first — an unconfigured device can never read as "paused"
        // (audit-3, t_5c554c5b).
        if !configured || paused { return shortGateLabel }
        return CardsRunningCopy.inlineLine(projection: projection, failure: failure)
    }

    /// The `accessoryCircular` scalar: running is the number this surface is
    /// about. nil draws the glyph in `CardsRunningCircularView`, never `0`.
    var circularNumber: Int? { CardsRunningCopy.headlineNumber(projection) }

    /// The ring's colour is the question a complication answers: is the fleet
    /// working, is something stuck, or did we not get to look?
    ///
    /// No `.paused` case, on purpose: `CardsRunningCircularView` decides
    /// `entry.paused` in the SAME step as `!entry.configured` and draws the gate
    /// LABEL before it ever asks for a glance (a paused ring drawing a count
    /// would be the false zero §6 rule 1 refuses). A case no body can reach is a
    /// case that rots, so the pause stays out of the glance vocabulary.
    enum Glance { case idle, working, stuck, unreadable, stale, invite }

    var circularGlance: Glance {
        if !configured { return .invite }
        guard let projection else { return .unreadable }
        if ageMinutes != nil { return .stale }
        return projection.isStuck ? .stuck : (projection.isIdle ? .idle : .working)
    }

    /// The one row `accessoryRectangular` draws, when there is one.
    var headlineRow: CardsRunningRow? { projection?.rows.first }

    /// At most `limit` rows, longest-running first (already sorted in the
    /// projection, so a family cannot reorder the truth).
    func rows(limit: Int) -> [CardsRunningRow] {
        Array((projection?.rows ?? []).prefix(limit))
    }

    /// The header count: "3 running", or nil when the surface is blind. Every
    /// family draws this through one door, which is what makes "a blind read
    /// never renders `0 running`" a property of the type rather than of six
    /// views behaving themselves.
    var runningText: String? { CardsRunningCopy.runningText(projection) }

    /// The blocked count, or `—` when the failure read did not answer. A dash
    /// here is a fact about the read; a zero would be a claim about the fleet.
    var blockedText: String? { CardsRunningCopy.blockedText(projection) }

    /// The footer every home-screen family can show: the age, or the failure,
    /// or what the numbers do not cover. Gate copy is owned here so the setup
    /// invite is checked first in every family.
    var footerLine: String {
        if !configured { return "Set up the app to see running cards." }
        return CardsRunningCopy.footerLine(projection: projection,
                                           ageMinutes: ageMinutes,
                                           failure: failure)
    }

    /// Whether the blocked number says someone is waiting. Used for colour only:
    /// a nil `blockedCount` is `false` here and draws a dash, so an unreadable
    /// read never colours a surface red.
    var isStuckGlance: Bool { CardsRunningCopy.blockedIsWarning(projection) }

    /// True when the failure must be drawn rather than a count. `projection`
    /// nil is the only blind state, and `failure` is non-nil in all of them —
    /// the views assert on this rather than re-deriving it. `!paused` because a
    /// paused entry has no projection either and is NOT blind: nothing was
    /// refused, nothing was asked for. Every body gates the pause before it
    /// reaches this, and the type says so too so a future body cannot get it
    /// wrong on the entry's word alone.
    var showsFailureLoudly: Bool { configured && !paused && projection == nil }
}

// MARK: - The last-known snapshot lives in Shared

// `CardsRunningSnapshotStore` (the App Group slot this provider saves to and
// the app-side Live Activity driver passes to `WidgetData.read(lastKnown:)`)
// is in `Sources/Shared/CardsRunningProjection.swift`.

// MARK: - Timeline provider

struct CardsRunningTimelineProvider: TimelineProvider {
    /// The cadence when the operator has never touched the settings screen: the
    /// shared widget default (`AppGroupWidgetSettings.setting(for:)` gives a
    /// self-fetching widget 5 minutes). This file used a conservative
    /// quarter-hour while `WidgetSurfaceKind` had no case for the surface; since
    /// t_fa7926fb added `widget.cardsRunning` the operator's choice wins and
    /// this is only the fallback for a store that was never written.
    private static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: cards_running_cadence
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
    // GATE-SLICE-END: cards_running_cadence

    /// The operator's entry for THIS surface, or nil when the registry blob has
    /// never been written. `setting(for:)` fills an absent surface in with ON at
    /// the widget cadence, so nil here means "the app has never run its settings
    /// screen" — never "paused", which is why a fresh install still renders.
    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetCardsRunning)
    }

    func placeholder(in context: Context) -> CardsRunningEntry { .sample }

    func getSnapshot(in context: Context, completion: @escaping (CardsRunningEntry) -> Void) {
        // The gallery preview reads the SAME registry entry the timeline does, so
        // a paused widget previews as paused instead of showing a fleet the
        // operator explicitly switched off.
        let surface = Self.surface()
        Task { completion(await entry(surface: surface)) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<CardsRunningEntry>) -> Void) {
        Task {
            let surface = Self.surface()
            let entry = await entry(surface: surface)
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

    private func entry(surface: WidgetSurfaceSettings?) async -> CardsRunningEntry {
        // Paused is checked BEFORE the client is built and before any read: a
        // paused surface spends zero requests (§2's ceiling is two here, and it
        // now spends none) and draws zero rows — no live counts, no last-known
        // snapshot, no failure pane. `!isActive` can only come from a blob the
        // app wrote, so an install that never opened Settings cannot land here.
        if let surface, !surface.isActive {
            return .paused()
        }
        let client = WidgetDataClient.makeIfConfigured()
        guard let client else {
            // No host, no token: the ONE job here is the setup invite. The
            // snapshot stays out of it — `WidgetData.read` encodes that rule
            // (`.notConfigured` never falls back), and this early return keeps
            // the invite ahead of every other state in every family.
            return .unconfigured()
        }
        let outcome = await WidgetData.read(using: client,
                                           lastKnown: { CardsRunningSnapshotStore.load() },
                                           { c in await CardsRunningFeed.read(client: c) })
        switch outcome {
        case .live(let projection, let fetchedAt):
            CardsRunningSnapshotStore.save(projection, at: fetchedAt)
            return CardsRunningEntry(date: .now, updatedAt: fetchedAt, projection: projection,
                                     ageMinutes: nil, failure: nil, configured: true,
                                     paused: false)
        case .stale(let projection, let ageMinutes, let reason):
            return CardsRunningEntry(date: .now,
                                     updatedAt: Date().addingTimeInterval(-Double(ageMinutes) * 60),
                                     projection: projection,
                                     ageMinutes: ageMinutes, failure: reason, configured: true,
                                     paused: false)
        case .failed(let failure):
            return CardsRunningEntry(date: .now, updatedAt: .now, projection: nil,
                                     ageMinutes: nil, failure: failure, configured: true,
                                     paused: false)
        }
    }

    /// The two reads are `CardsRunningFeed.read` — see
    /// `Sources/Shared/CardsRunningProjection.swift`. The provider calls it and
    /// nothing else: the pair of routes, the two-request budget and the guard
    /// that turns a `tasks`-less 2xx into a `decoding` failure (rather than a
    /// `0 running`) are one rule shared with the app's Live Activity driver, so
    /// the two surfaces cannot drift about what a blind read means.
}

// MARK: - Widget + families

/// The Cards widget: the cards being worked right now, with their worker and
/// their run time.
struct CardsRunningWidget: Widget {
    let kind = "CardsRunningWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetCardsRunning)
        return StaticConfiguration(kind: kind, provider: CardsRunningTimelineProvider()) { entry in
            CardsRunningWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Running Cards")
        .description("Running cards with their worker and run time, plus a clear running count.")
        // The accessory families are iOS-only; the macOS desktop tile offers
        // the three system sizes plus XL, which reuses the large renderer with
        // room for the same eight rows.
        #if os(iOS)
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
        #else
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .systemExtraLarge])
        #endif
    }
}

/// One entry, six families. Dispatch order is the audit-3 invariant: the
/// unconfigured invite is checked BEFORE any failure or stale state in every
/// family body, so an app that was never configured never says "can't reach the
/// cluster" (it is not the cluster's problem, and inviting setup is the fix).
struct CardsRunningWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            Text(entry.inlineLine)
                .font(style.font(.caption2))
        case .accessoryCircular:
            CardsRunningCircularView(entry: entry)
        case .accessoryRectangular:
            CardsRunningRectangularView(entry: entry)
        #endif
        case .systemLarge:
            CardsRunningLargeView(entry: entry)
        case .systemExtraLarge:
            CardsRunningLargeView(entry: entry)
        case .systemMedium:
            CardsRunningMediumView(entry: entry)
        default:
            CardsRunningSmallView(entry: entry)
        }
    }
}

// MARK: Accessory families

struct CardsRunningCircularView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            // The ring draws a LABEL, never a number: `0` or `—` in this ring on
            // a paused surface would both claim something about the fleet that
            // nothing was asked about (§6 rule 1), and the copy rule inside
            // `shortGateLabel` keeps the setup invite first.
            Text(entry.shortGateLabel)
                .font(style.font(.caption2))
                .foregroundStyle(.secondary)
                .accessibilityLabel(entry.shortGateLabel)
        } else if entry.projection == nil {
            // The blind state: a glyph, not a zero. `0` in this ring would read
            // "nothing is running", which is the opposite of what is known.
            Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                .font(style.font(.body))
                .foregroundStyle(.primary)
        } else {
            VStack(spacing: 0) {
                Text(entry.circularNumber.map { "\($0)" } ?? "—")
                    .font(style.font(size: 22, weight: .bold))
                    .monospacedDigit()
                    .foregroundStyle(Self.glanceColor(entry.circularGlance))
                Text(Self.glanceLabel(entry.circularGlance)).widgetHeader(style)
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
            }
        }
    }

    static func glanceColor(_ glance: CardsRunningEntry.Glance) -> Color {
        switch glance {
        case .invite, .unreadable: return .secondary
        case .stale: return .primary
        case .stuck: return .primary
        case .idle: return .secondary
        case .working: return .primary
        }
    }

    static func glanceLabel(_ glance: CardsRunningEntry.Glance) -> String {
        switch glance {
        case .invite: return "set up"
        case .unreadable: return "no data"
        case .stale: return "stale"
        case .stuck: return "blocked"
        case .idle: return "idle"
        case .working: return "running"
        }
    }
}

struct CardsRunningRectangularView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            CardsRunningGateView(entry: entry, systemTinted: true)
        } else if entry.projection == nil {
            VStack(alignment: .leading, spacing: 1) {
                Label(entry.failure?.shortLabel ?? "No data",
                      systemImage: "exclamationmark.triangle.fill")
                    .labelStyle(.widget)
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(entry.failure?.operatorLine ?? "")
                    .font(style.font(size: 9))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        } else if let row = entry.headlineRow {
            // §5.4's rectangular row: one row, `title≤20`, plus status — with
            // the worker and the run time this surface is named for.
            VStack(alignment: .leading, spacing: 1) {
                Text(row.headline(max: 20)).widgetHeader(style)
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text("\(row.workerText) · \(row.ageText)")
                    .font(style.font(.caption2, weight: .medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        } else {
            VStack(alignment: .leading, spacing: 1) {
                Text(entry.runningText ?? "—")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(.primary)
                Text(entry.blockedText ?? "blocked —")
                    .font(style.font(size: 9))
                    .foregroundStyle(entry.isStuckGlance ? .primary : .secondary)
            }
        }
    }
}

// MARK: Home Screen families

struct CardsRunningSmallView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            CardsRunningGateView(entry: entry)
        } else if entry.projection == nil {
            CardsRunningFailureView(entry: entry, compact: true)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                // One clear headline number: the count `hscc cards monitor`
                // prints beside "slots". Rows and age are supporting detail.
                CardsRunningHeadlineView(entry: entry, size: 34)
                CardsRunningRowList(rows: entry.rows(limit: 2), showBoard: false,
                                    titleMax: 30)
                Spacer(minLength: 0)
                CardsRunningFooterView(entry: entry)
            }
        }
    }
}

struct CardsRunningMediumView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            CardsRunningGateView(entry: entry)
        } else if entry.projection == nil {
            CardsRunningFailureView(entry: entry, compact: false)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                CardsRunningHeadlineView(entry: entry, size: 28)
                // Three two-line rows fit one medium tile beside the headline
                // and the age footer; a fourth would push the age off.
                CardsRunningRowList(rows: entry.rows(limit: 3), showBoard: true,
                                    titleMax: 42)
                Spacer(minLength: 0)
                CardsRunningFooterView(entry: entry)
            }
        }
    }
}

struct CardsRunningLargeView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    var body: some View {
        if !entry.configured || entry.paused {
            CardsRunningGateView(entry: entry)
        } else if entry.projection == nil {
            CardsRunningFailureView(entry: entry, compact: false)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                CardsRunningHeadlineView(entry: entry, size: 30)
                // 8, which is also the stored cap, so the footer's "showing N
                // of M" and the App Group blob can never disagree.
                CardsRunningRowList(rows: entry.rows(limit: 8), showBoard: true,
                                    titleMax: 52)
                Spacer(minLength: 0)
                CardsRunningFooterView(entry: entry)
            }
        }
    }
}

// MARK: Parts

/// The one number a glance should land on: running cards. Blocked is a
/// secondary, clearly-labelled warning and never replaces the headline.
struct CardsRunningHeadlineView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry
    /// Point size from the family, scaled by the operator's text scale in
    /// `style.font(size:)`; no family hard-codes a font.
    var size: CGFloat = 30

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Image(systemName: "play.circle.fill").widgetIcon(style)
                .font(style.font(size: 10))
                .foregroundStyle(entry.circularNumber == nil ? style.secondaryTextColor : style.okColor)
            if let number = entry.circularNumber {
                Text("\(number)")
                    .font(style.font(size: size, weight: .bold))
                    .monospacedDigit()
                    .foregroundStyle(style.primaryTextColor)
                    .lineLimit(1)
                Text("running")
                    .font(style.font(.caption2, weight: .semibold))
                    .foregroundStyle(style.secondaryTextColor)
                    .lineLimit(1)
            } else {
                // No count was sent: a dash, never a fabricated zero.
                Text("—")
                    .font(style.font(size: size, weight: .bold))
                    .foregroundStyle(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
            if entry.isStuckGlance, let blocked = entry.blockedText {
                Text(blocked)
                    .font(style.font(size: 9, weight: .semibold))
                    .foregroundStyle(style.badColor)
                    .lineLimit(1)
            }
        }
        .widgetHeader(style)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(entry.runningText ?? "no running count")
    }
}

/// One running card, two lines: the CLI's `title`, then `worker` · board ·
/// run time. `hscc cards monitor --short`'s panel header order — board, card,
/// worker — with the age the route can prove.
struct CardsRunningRowList: View {
    @Environment(\.widgetStyle) private var style
    let rows: [CardsRunningRow]
    let showBoard: Bool
    let titleMax: Int

    var body: some View {
        if rows.isEmpty {
            // Reached only on a LIVE entry that counted nothing: a real answer,
            // and the only place "nothing is running" may be drawn. The blind
            // state never gets here — its family body takes the failure branch.
            Label("Nothing running", systemImage: "circle.dashed")
                .labelStyle(.widget)
                .font(style.font(.caption2))
                .foregroundStyle(style.secondaryTextColor)
        } else {
            VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                ForEach(rows) { row in
                    HStack(alignment: .top, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                        Circle()
                            .fill(style.cardStatusColor(row.status ?? ""))
                            .frame(width: 5, height: 5)
                            .padding(.top, 3)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(row.headline(max: titleMax))
                                .font(style.font(.caption2, weight: .medium))
                                .foregroundStyle(style.primaryTextColor)
                                .lineLimit(1)
                            HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                                Text(row.workerText)
                                    .font(style.font(size: 9, weight: .medium))
                                    .foregroundStyle(style.secondaryTextColor)
                                    .lineLimit(1)
                                if showBoard, let board = row.board, !board.isEmpty {
                                    Text("·")
                                        .font(style.font(size: 9))
                                        .foregroundStyle(style.secondaryTextColor)
                                    Text(board)
                                        .font(style.font(size: 9))
                                        .foregroundStyle(style.secondaryTextColor)
                                        .lineLimit(1)
                                }
                                Spacer(minLength: 0)
                                Text(row.ageText)
                                    .font(style.font(size: 9, weight: .semibold))
                                    .monospacedDigit()
                                    .foregroundStyle(row.ageMinutes == nil
                                                     ? style.warnColor : style.secondaryTextColor)
                            }
                        }
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(row.headline(max: titleMax)), \(row.workerText), \(row.ageText)")
                }
            }
        }
    }
}

/// The age / failure line. A live tile shows a self-updating relative age, so
/// "updated 3m ago" keeps ageing while WidgetKit holds the entry. A stale tile
/// swaps that for the stored age and the reason; a blind one never reaches here.
struct CardsRunningFooterView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    var body: some View {
        if entry.ageMinutes != nil || entry.failure != nil {
            HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                Image(systemName: "exclamationmark.triangle.fill").widgetIcon(style)
                    .font(style.font(size: 8))
                    .foregroundStyle(entry.ageMinutes != nil ? style.warnColor : style.badColor)
                Text(entry.footerLine)
                    .font(style.font(size: 9))
                    .foregroundStyle(entry.ageMinutes != nil ? style.warnColor : style.badColor)
                    .lineLimit(2)
                Spacer(minLength: 0)
            }
        } else {
            HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                Image(systemName: "clock").widgetIcon(style)
                    .font(style.font(size: 8))
                    .foregroundStyle(style.secondaryTextColor)
                Text("updated")
                    .font(style.font(size: 9))
                    .foregroundStyle(style.secondaryTextColor)
                Text(entry.updatedAt, style: .relative)
                    .font(style.font(size: 9, weight: .medium))
                    .foregroundStyle(style.secondaryTextColor)
                if let notes = CardsRunningCopy.notesLine(entry.projection) {
                    Text("· \(notes)")
                        .font(style.font(size: 8))
                        .foregroundStyle(style.secondaryTextColor)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("updated \(entry.footerLine)")
        }
    }
}

/// The blind state for the home-screen families: loud, named, and with no
/// number anywhere that could be read as "the fleet is quiet".
struct CardsRunningFailureView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry
    let compact: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Label(entry.failure?.shortLabel ?? "No data",
                  systemImage: "exclamationmark.triangle.fill")
                .labelStyle(.widget)
                .font(style.font(compact ? .subheadline : .headline, weight: .semibold))
                .foregroundStyle(style.badColor)
                .lineLimit(2)
            Text(entry.failure?.operatorLine ?? "The cluster did not answer.")
                .font(style.font(.caption))
                .foregroundStyle(style.primaryTextColor)
            if !compact {
                // The one thing an operator can still trust from a blind tile:
                // what the last good read said, and how old it was. The rows
                // themselves are gone (there is no snapshot), so say plainly
                // that nothing here is current.
                Text("No running cards are shown because this read did not answer. A count of zero would be a guess.")
                    .font(style.font(size: 9))
                    .foregroundStyle(style.secondaryTextColor)
            }
            Spacer(minLength: 0)
        }
    }
}

/// The gate a family draws when it has nothing to draw: the setup invite
/// (audit-3 — what to do, not what broke) or the honest paused state
/// (t_fa7926fb). One pane, two copies, chosen by `entry.paused`, exactly as
/// `ClusterWidgetViews`' gate and `KanbanMonitorGateView` do, so a paused surface
/// can never fall through to live rows, to a stale snapshot, or to a blank. It
/// kept `CardsRunningInviteView`'s typography exactly: every caller passed
/// `compact: false`, and the ring that used `true` now draws `shortGateLabel`
/// itself (a paused complication showing a count would be the false zero §6
/// rule 1 refuses), so the parameter had one value left and is gone.
struct CardsRunningGateView: View {
    @Environment(\.widgetStyle) private var style
    let entry: CardsRunningEntry

    /// Accessory families are system-tinted: fonts still follow the style,
    /// colours stay the system's.
    var systemTinted = false
    private var primaryText: Color { systemTinted ? .primary : style.primaryTextColor }
    private var secondaryText: Color { systemTinted ? .secondary : style.secondaryTextColor }
    private var statusText: Color { systemTinted ? .primary : style.badColor }

    var body: some View {
        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
            Label("Running cards",
                  systemImage: entry.paused ? "pause.circle" : "square.stack.3d.up")
                .labelStyle(.widget)
                .font(style.font(.subheadline, weight: .semibold))
                .foregroundStyle(primaryText)
                .lineLimit(1)
            Text(entry.gateLine)
                .font(style.font(size: 9))
                .foregroundStyle(secondaryText)
                .lineLimit(2)
        }
    }
}
