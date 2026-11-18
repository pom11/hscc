import AppIntents
import Foundation
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// ProjectsWidget.swift — the projects surface: which projects the cluster has,
// and which board each one lives on. READ-ONLY, no actions.
//
// Card t_d739d163, part of the 2026-11-17 pivot (`ios-app/docs/WIDGET_DATA_
// CONTRACT.md` is the authority; §5.1 is this surface's row). §3 row 1's verdict
// for projects is `reuse`: `GET /v1/projects` is 1,854 B for 18 projects, so a
// widget can afford the existing route and no server work is needed. This file
// therefore makes exactly ONE read per refresh — `WidgetDataClient.projects()` —
// and draws `{name, board}`, the two keys §4.1 says a family can render.
//
// WHAT IT DELIBERATELY DOES NOT SHOW:
//
//   * `repo`. It is an absolute path on the operator's machine and 58% of the
//     array's bytes (§4.1). `ProjectLite` does not declare the key, so it is
//     never decoded, never stored, never rendered — and `ProjectRow` below is a
//     separate type so the App Group write cannot accidentally widen to the
//     response either. §7.
//   * per-project card counts. No route gives them: `done` is reachable only
//     through `?include_archived=1` (8.6 MB), and `/v1/cards?status=done`
//     answers zero (§4.2). §5.1 says the pairing belongs to the board-status
//     card and its §6 counts block — "rather than invent a third read". So a
//     project row here carries its board, not a number this app cannot get.
//   * `speak` / `count` from the response. Server prose and a number the server
//     counted rather than this surface observed; the row count the widget prints
//     comes from the rows it drew, so the two cannot disagree.
//
// The status half of the surface is the honest one: `.live`, `.stale` with its
// age, or a `WidgetDataFailure` the operator can act on. `WidgetData.read` owns
// that rule, so this widget cannot invent a fallback (t_5c554c5b: an
// unconfigured app must invite setup, never claim "can't reach the cluster").
//
// WIRED TO THE ACTIVE TOGGLE / CADENCE (t_5f246177). Its registry entry is
// `WidgetSurfaceKind.widgetProjects` — the last widget in this directory to get
// one. The provider reads it BEFORE it builds a client, so a paused surface
// sends ZERO requests (WIDGET_DATA_CONTRACT §2's budget) and draws the honest
// "Paused in Settings" in every family: never its last-known rows, which is
// exactly what a paused tile claiming an operator still has 18 projects would
// be. Cadence 0 (Off in the picker) means `policy: .atEnd`, not a 0-minute loop.
// It borrows nothing from `.widgetCluster` or `.widgetMonitor` — pausing those
// must not blank this list, which is why one key per surface is the rule.
// ---------------------------------------------------------------------------

// MARK: - The stored projection (§7)

/// The projection this surface keeps, and the only shape that can be written to
/// the App Group. It carries the registry identity plus the board truth the
/// widget draws; `repo`/`topic`/`speak` are deliberately absent, so the response
/// is decodable-only and the encode path can never pick them up.
struct ProjectRow: Codable, Identifiable, Equatable, Hashable, Sendable {
    let name: String
    let board: String?
    /// The CLI's health token for the registry row (ok | partial | absent).
    let health: String?
    /// The board histogram, last activity and orchestrator turn — the same
    /// values `hscc project list` + `hscc kanban monitor` report. Optional so a
    /// snapshot written by an older build (or an older server) still decodes.
    let counts: ProjectCounts?
    let lastActivity: Int?
    let orchestrator: ProjectOrchestrator?

    var id: String { name }

    init(name: String, board: String?, health: String? = nil,
         counts: ProjectCounts? = nil, lastActivity: Int? = nil,
         orchestrator: ProjectOrchestrator? = nil) {
        self.name = name
        self.board = board
        self.health = health
        self.counts = counts
        self.lastActivity = lastActivity
        self.orchestrator = orchestrator
    }

    /// What the row says under its name: running/blocked/ready, then how long
    /// ago the board last moved, then the live orchestrator state. Missing
    /// pieces are omitted — never rendered as a zero or an invented age.
    var detailLine: String? {
        var parts: [String] = []
        // `hscc project list`'s HEALTH column: ok | partial | absent.
        if let health, !health.isEmpty { parts.append(health) }
        if let status = counts?.statusLine { parts.append(status) }
        if let age = activityAge { parts.append("active \(age)") }
        if let state = orchestrator?.display { parts.append("orch: \(state)") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private var activityAge: String? {
        guard let lastActivity else { return nil }
        let seconds = max(0, Int(Date().timeIntervalSince1970) - lastActivity)
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(seconds / 60)m ago" }
        if seconds < 86400 { return "\(seconds / 3600)h ago" }
        return "\(seconds / 86400)d ago"
    }
}

/// The last-known projection: the rows of the last read that worked, plus when
/// it worked. Stale-while-error needs a date next to the rows — an undated tile
/// is a lie about how current the data is.
enum ProjectsSnapshotStore {
    /// Bounded: 18 projects measured (§4.1), 50 stored. A cluster with thousands
    /// of projects must not be able to grow a widget's App Group value without
    /// limit; the families draw 3/6/8 rows anyway.
    static let rowLimit = 50

    private static let storageKey = "hscc.projects.projection"

    struct Snapshot: Codable {
        let date: Date
        let rows: [ProjectRow]
    }

    static func save(_ rows: [ProjectRow], at date: Date = Date()) {
        let snapshot = Snapshot(date: date, rows: Array(rows.prefix(rowLimit)))
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        UserDefaults(suiteName: AppGroup.suiteName)?.set(data, forKey: storageKey)
    }

    static func load() -> Snapshot? {
        guard let data = UserDefaults(suiteName: AppGroup.suiteName)?.data(forKey: storageKey),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else { return nil }
        return snapshot
    }

    /// `WidgetData.read`'s `lastKnown` shape, which the tuple labels are part of.
    static func lastKnown() -> (value: [ProjectRow], at: Date)? {
        guard let snapshot = load() else { return nil }
        return (value: snapshot.rows, at: snapshot.date)
    }

    /// The response → the rows. The one place the projection happens, so §7 is
    /// auditable in one line: `name` and `board`, nothing else.
    static func project(from payload: ProjectsLite) -> [ProjectRow] {
        payload.projects.prefix(rowLimit).map {
            ProjectRow(name: $0.name, board: $0.board, health: $0.health,
                       counts: $0.counts, lastActivity: $0.lastActivity,
                       orchestrator: $0.orchestrator)
        }
    }
}

// MARK: - Configuration — the project the operator picks

/// One picker option. Identity is the project NAME, because that is what the
/// route keys on (`GET /v1/projects/{name}`) and what survives a rename of
/// nothing else; `board` is carried so the sheet can show which board it picked.
struct ProjectEntity: AppEntity {
    /// The sentinel option: every project. Its identity is not a project name,
    /// so it can never collide with one.
    static let allID = "*"
    static let all = ProjectEntity(id: allID, name: "All projects", board: nil)

    let id: String
    let name: String
    let board: String?

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Project")
    static var defaultQuery = ProjectEntityQuery()

    var displayRepresentation: DisplayRepresentation {
        if let board, !board.isEmpty {
            return DisplayRepresentation(title: "\(name)", subtitle: "\(board)")
        }
        return DisplayRepresentation(title: "\(name)")
    }
}

/// Where the sheet's options come from: the live list if the cluster answers,
/// otherwise the rows the widget last stored. A brand-new widget on a cluster it
/// has never reached still gets a usable sheet — it just has the one option.
///
/// This fetch is the operator opening the configuration sheet, not a timeline
/// refresh, so it does not count against the widget's ≤ 2 requests per refresh
/// (§2) — one read, on an action they took.
struct ProjectEntityQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [ProjectEntity] {
        let pool = await candidates()
        var found: [ProjectEntity] = []
        for identifier in identifiers {
            if identifier == ProjectEntity.allID {
                found.append(.all)
            } else if let hit = pool.first(where: { $0.id == identifier }) {
                found.append(hit)
            }
            // An identifier with no match is dropped, not fabricated: the project
            // was renamed or removed. The widget renders that honestly (see
            // `ProjectsEmptyView`), the sheet cannot invent a row for it.
        }
        return found
    }

    func suggestedEntities() async throws -> [ProjectEntity] {
        let pool = await candidates()
        return [.all] + pool
    }

    func allEntities() async throws -> [ProjectEntity] {
        try await suggestedEntities()
    }

    /// Live first, stored projection second, empty last. Never an error: a
    /// throwing options provider gives the sheet a spinner, and "no projects to
    /// pick yet" is the truth the operator needs.
    private func candidates() async -> [ProjectEntity] {
        let outcome = await WidgetData.read(using: WidgetDataClient.makeIfConfigured(),
                                            lastKnown: ProjectsSnapshotStore.lastKnown) { client in
            switch await client.projects() {
            case .success(let payload): return .success(ProjectsSnapshotStore.project(from: payload))
            case .failure(let failure): return .failure(failure)
            }
        }
        let rows: [ProjectRow]
        switch outcome {
        case .live(let value, _), .stale(let value, _, _): rows = value
        case .failed: rows = []
        }
        return rows.map { ProjectEntity(id: $0.name, name: $0.name, board: $0.board) }
    }
}

/// The widget's configuration. One option, because that is the whole choice the
/// card asks for; `perform()` is the `WidgetConfigurationIntent` default — a
/// configuration never performs anything.
struct ProjectFilterIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Project"

    /// Optional, so "no choice made" and "chose All" stay distinguishable and the
    /// sheet can leave it unset. Both render as the full list.
    @Parameter(title: "Project",
               description: "Show one project, or all of them.")
    var project: ProjectEntity?

    /// The project to filter to, nil for the whole list. The sentinel is folded
    /// to nil here so no view downstream has to know it exists.
    var selectedName: String? {
        guard let project, project.id != ProjectEntity.allID else { return nil }
        return project.name
    }
}

// MARK: - Timeline entry

/// What went wrong with the surface, resolved by `WidgetData.read` — so the
/// distinction between "the data is old but real" and "there is no data" is
/// made in one place for the whole app, not per widget.
enum ProjectsStatus: Equatable {
    /// Read now.
    case live
    /// Real rows, `minutes` old, kept because this read failed in a way honest
    /// staleness covers. `reason` says why it is not live.
    case stale(minutes: Int, reason: WidgetDataFailure)
    /// Nothing usable to draw.
    case failed(WidgetDataFailure)
    /// The operator paused THIS surface in Settings (t_5f246177). Neither live
    /// data, nor stale data, nor an error — and deliberately a case of THIS
    /// enum rather than a second `paused` flag next to it, because two
    /// representations of one state are two things that can disagree. It is
    /// never produced by `WidgetData.read`: the provider returns the paused
    /// entry before a client exists, so no read can have happened.
    case paused

    /// The failure that left this entry with nothing. Not the reason a `.stale`
    /// entry is stale — those are different views and must not share an accessor.
    var failure: WidgetDataFailure? {
        if case .failed(let failure) = self { return failure }
        return nil
    }

    /// The reason a `.stale` entry is stale, for the footer line.
    var staleReason: WidgetDataFailure? {
        if case .stale(_, let reason) = self { return reason }
        return nil
    }

    var ageMinutes: Int? {
        if case .stale(let minutes, _) = self { return minutes }
        return nil
    }

    var isStale: Bool { ageMinutes != nil }
}

/// One refresh's worth of this surface. `rows` is already filtered to the
/// selected project, so what the view draws is exactly what was read — the
/// family decides only how many of those rows fit.
struct ProjectsEntry: TimelineEntry {
    let date: Date
    /// WidgetKit hands the configuration back on every entry; nil in the
    /// placeholder, which has none.
    let configuration: ProjectFilterIntent?
    /// The rows to draw, in server order.
    let rows: [ProjectRow]
    /// The project this entry is scoped to, nil for all of them.
    let selectedName: String?
    let status: ProjectsStatus

    // MARK: Gate state (unconfigured / paused)

    /// False when the app has no target configured. Derived from the ONE owner
    /// of that fact — `status == .failed(.notConfigured)`, which is what
    /// `WidgetData.read` returns for a nil client — rather than a second stored
    /// flag that could disagree with it. The t_5c554c5b invariant lives here:
    /// an unconfigured app shows the setup invite, never "can't reach the
    /// cluster".
    var configured: Bool { status.failure != .notConfigured }

    /// True when the operator paused THIS surface in Settings (t_5f246177).
    /// Read off `status` for the same single-owner reason as `configured`.
    var paused: Bool { status == .paused }

    /// The line the gate pane draws. One owner for the copy, so six families
    /// cannot invent six paused messages, and the setup invite is checked
    /// FIRST: an app that was never configured has not "paused" anything
    /// (audit-3 / t_5c554c5b).
    var gateLine: String {
        if !configured { return "Set up the app to see your projects" }
        return "Paused in Settings"
    }

    /// The label the accessory families draw for a gate state — the ~25-character
    /// budget the Lock Screen gives an inline row and a ring. The setup invite is
    /// checked FIRST, so a device that was simply never set up can never render
    /// "paused" (audit-3 / t_5c554c5b). A scoped pause says which project is
    /// paused while it fits; past that, "Paused" is the honest short form.
    var shortGateLabel: String {
        if !configured { return "Set up in HSCC" }
        if let selectedName, !selectedName.isEmpty {
            let scoped = "\(selectedName) · paused"
            if scoped.count <= 25 { return scoped }
        }
        return "Paused"
    }

    // MARK: Text the accessory families draw (one rule per string)

    /// `accessoryInline`: one short line. The Lock Screen row has room for a
    /// count or one name, not a list, so a scoped widget names the project and
    /// an unfiltered one counts the rows it actually holds. A name that does not
    /// fit the ~25-character budget falls back to the count rather than a
    /// truncated name.
    var inlineLine: String {
        // Both gate states through ONE copy rule, which itself puts the setup
        // invite first — an unconfigured device can never read as "paused".
        if !configured || paused { return shortGateLabel }
        if let failure = status.failure { return failure.shortLabel }
        let countText = rows.count == 1 ? "1 project" : "\(rows.count) projects"
        guard !rows.isEmpty else {
            return selectedName.map { "No \($0)" } ?? "No projects"
        }
        guard let selectedName else { return countText }
        return selectedName.count <= 25 ? selectedName : countText
    }

    /// The `accessoryCircular` scalar: how many projects this entry holds. nil
    /// when there is nothing to count, so the ring draws `—`, never `0` — a
    /// failed or empty read is not "this cluster has no projects".
    var circularNumber: Int? { rows.isEmpty ? nil : rows.count }

    /// The top rows `accessoryRectangular` can draw: the names plus their board
    /// as the short status. Three is the family's height on the Lock Screen.
    func rectangularRows(limit: Int = 3) -> [ProjectRow] {
        Array(rows.prefix(limit))
    }

    /// Rows this family can afford (§5.1: small 3, medium 6, large 8).
    static func rowLimit(for family: WidgetFamily) -> Int {
        switch family {
        case .systemSmall: return 3
        case .systemLarge: return 8
        default: return 6
        }
    }

    /// Sample rows for the placeholder only. Real-looking but nothing here is
    /// claimed to be the operator's cluster: the placeholder is what shows before
    /// the first read resolves.
    static let sampleRows: [ProjectRow] = [
        ProjectRow(name: "hscc", board: "hscc"),
        ProjectRow(name: "radio", board: "radio"),
        ProjectRow(name: "memori", board: "memori"),
    ]

    static let placeholderEntry = ProjectsEntry(date: .now,
                                                configuration: nil,
                                                rows: sampleRows,
                                                selectedName: nil,
                                                status: .live)

    /// The paused state (t_5f246177): the project the operator had scoped to,
    /// and nothing else. No rows, no snapshot, no age, no failure — the operator
    /// asked for no data, so the surface draws none, and the provider builds this
    /// BEFORE a client exists, which is what makes "a paused surface sends zero
    /// requests" a property of the code rather than of the rendering.
    static func paused(selectedName: String?,
                       configuration: ProjectFilterIntent? = nil) -> ProjectsEntry {
        ProjectsEntry(date: .now,
                      configuration: configuration,
                      rows: [],
                      selectedName: selectedName,
                      status: .paused)
    }
}

// MARK: - Timeline provider

/// The provider. One read per refresh, the outcome resolved by `WidgetData.read`,
/// and a usable entry on every path — there is no branch that returns a blank.
struct ProjectsTimelineProvider: AppIntentTimelineProvider {
    /// The cadence when the operator has never touched the settings screen: the
    /// shared widget default (`AppGroupWidgetSettings.setting(for:)` gives a
    /// self-fetching widget 5 minutes), which is what this file hard-coded while
    /// `WidgetSurfaceKind` had no case for this surface. Since t_5f246177 the
    /// registry's answer wins and this is only the fallback for a store that was
    /// never written — the same value, so a fresh install changes nothing.
    static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: projects_cadence
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
    // GATE-SLICE-END: projects_cadence

    /// The operator's entry for THIS surface, or nil when the registry blob has
    /// never been written. `setting(for:)` fills an absent surface in with ON at
    /// the widget cadence, so nil here means "the app has never run its settings
    /// screen" — never "paused". Reading nil as paused would blank every widget
    /// on first boot, which is the failure this distinction exists to stop.
    private static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetProjects)
    }

    func placeholder(in context: Context) -> ProjectsEntry { .placeholderEntry }

    func snapshot(for configuration: ProjectFilterIntent, in context: Context) async -> ProjectsEntry {
        // The gallery preview reads the SAME registry entry the timeline does, so
        // a paused widget previews as paused instead of showing a project list the
        // operator explicitly turned off.
        await fetch(for: configuration, surface: Self.surface())
    }

    func timeline(for configuration: ProjectFilterIntent, in context: Context) async -> Timeline<ProjectsEntry> {
        let surface = Self.surface()
        let entry = await fetch(for: configuration, surface: surface)
        // 0 = Off in the cadence picker: no scheduled refresh, the OS decides.
        // Same shape as `KanbanMonitorTimelineProvider.getTimeline`.
        guard let next = Self.nextRefreshDate(
            refreshMinutes: surface?.refreshMinutes ?? Self.defaultRefreshMinutes
        ) else {
            return Timeline(entries: [entry], policy: .atEnd)
        }
        return Timeline(entries: [entry], policy: .after(next))
    }

    /// One recommendation so the gallery has something to show without the
    /// operator opening the sheet first.
    func recommendations() -> [AppIntentRecommendation<ProjectFilterIntent>] {
        [AppIntentRecommendation(intent: ProjectFilterIntent(), description: Text("All projects"))]
    }

    // MARK: Fetch

    /// Always returns a renderable entry: unconfigured, paused and failed are
    /// states, not blanks.
    ///
    /// One note on the OTHER read in this file — `ProjectEntityQuery.candidates()`
    /// builds a client too, and it stays that way on purpose. That fetch runs when
    /// the operator opens the configuration sheet (an action they took, not a
    /// timeline refresh, so it is outside §2's per-refresh budget, as the comment
    /// on that type says). Pausing a surface governs what it does on its own
    /// schedule; it does not silence the sheet the operator just opened, and the
    /// options there are what let them pick a project.
    private func fetch(for configuration: ProjectFilterIntent,
                       surface: WidgetSurfaceSettings?) async -> ProjectsEntry {
        let selectedName = configuration.selectedName
        // Paused is decided BEFORE the client is built and before any read: a
        // paused surface spends zero of WIDGET_DATA_CONTRACT §2's request budget
        // and draws zero rows — no live list, no last-known snapshot, no failure
        // pane. `!isActive` can only come from a blob the app wrote, so an
        // install that never opened Settings cannot land here.
        if let surface, !surface.isActive {
            return .paused(selectedName: selectedName, configuration: configuration)
        }
        let outcome = await WidgetData.read(using: WidgetDataClient.makeIfConfigured(),
                                           lastKnown: ProjectsSnapshotStore.lastKnown) { client in
            switch await client.projects() {
            case .success(let payload): return .success(ProjectsSnapshotStore.project(from: payload))
            case .failure(let failure): return .failure(failure)
            }
        }

        switch outcome {
        case .live(let rows, let fetchedAt):
            // Record the projection on the way out, so the next unreachable
            // window has real rows and a real date to show.
            ProjectsSnapshotStore.save(rows, at: fetchedAt)
            return ProjectsEntry(date: fetchedAt,
                                 configuration: configuration,
                                 rows: Self.scope(rows, to: selectedName),
                                 selectedName: selectedName,
                                 status: .live)

        case .stale(let rows, let minutes, let reason):
            // The snapshot is NOT rewritten here — its date is the point of it.
            return ProjectsEntry(date: .now,
                                 configuration: configuration,
                                 rows: Self.scope(rows, to: selectedName),
                                 selectedName: selectedName,
                                 status: .stale(minutes: minutes, reason: reason))

        case .failed(let failure):
            return ProjectsEntry(date: .now,
                                 configuration: configuration,
                                 rows: [],
                                 selectedName: selectedName,
                                 status: .failed(failure))
        }
    }

    /// The filter. An empty result for a selected project is kept as empty
    /// rather than falling back to the full list: silently widening the scope the
    /// operator picked would make a deleted project look like a live one.
    static func scope(_ rows: [ProjectRow], to selectedName: String?) -> [ProjectRow] {
        guard let selectedName else { return rows }
        return rows.filter { $0.name == selectedName }
    }
}

// MARK: - Widget

/// The projects widget: three Home Screen sizes and three Lock Screen
/// accessories. One read, no actions.
struct ProjectsWidget: Widget {
    let kind = "ProjectsWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetProjects)
        return AppIntentConfiguration(kind: kind,
                              intent: ProjectFilterIntent.self,
                              provider: ProjectsTimelineProvider()) { entry in
            ProjectsWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Projects")
        .description("The cluster's projects and the board each one is on. Read-only.")
        #if os(iOS)
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryInline, .accessoryCircular, .accessoryRectangular])
        #else
        // The macOS desktop widget (t_1143c19b) offers the three system
        // sizes; WidgetKit marks the accessory families unavailable there.
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
        #endif
    }
}

// MARK: - Views

/// Picks the layout by family. Every family runs the same dispatch order:
/// (unconfigured OR paused) → failure → nothing to draw → rows. The first step
/// is one test, not two, so no family can gate one state and forget the other.
struct ProjectsWidgetView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        #if os(iOS)
        case .accessoryInline:
            Text(entry.inlineLine)
                .font(style.font(.caption2))
        case .accessoryCircular:
            ProjectsCircularView(entry: entry)
        case .accessoryRectangular:
            ProjectsRectangularView(entry: entry)
        #endif
        case .systemSmall:
            SmallProjectsWidget(entry: entry)
        case .systemLarge:
            LargeProjectsWidget(entry: entry)
        case .systemExtraLarge:
            LargeProjectsWidget(entry: entry)
        default:
            MediumProjectsWidget(entry: entry)
        }
    }
}

// MARK: Shared parts

/// One project row: the name, and the board beside it when the family has room.
private struct ProjectRowView: View {
    @Environment(\.widgetStyle) private var style
    let row: ProjectRow
    var showsBoard = false
    var showsDetail = true
    var showsHealth = false

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                Image(systemName: "folder").widgetIcon(style)
                    .font(style.font(size: 9))
                    .foregroundColor(style.secondaryTextColor)
                Text(row.name)
                    .font(style.font(.caption))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                if showsBoard, let board = row.board, !board.isEmpty {
                    Spacer(minLength: style.spacing(Theme.Spacing.xs.rawValue))
                    Text(board)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                        .lineLimit(1)
                }
                if showsHealth, let health = row.health, !health.isEmpty {
                    Spacer(minLength: style.spacing(Theme.Spacing.xs.rawValue))
                    Text(health)
                        .font(style.font(size: 8, weight: .semibold))
                        .foregroundColor(health == "ok" ? style.okColor : style.warnColor)
                        .lineLimit(1)
                }
            }
            if showsDetail, let detail = row.detailLine {
                Text(detail)
                    .font(style.font(size: 8))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }
        }
    }
}

/// The gate a family draws when it has nothing to draw: the setup invite
/// (t_5c554c5b — the ONLY thing an unconfigured app may show) or the honest
/// paused state (t_5f246177). One pane, two copies, chosen by the entry, so a
/// paused tile can never fall through to its last-known rows or to a blank.
/// The words belong to `ProjectsEntry.gateLine`, not to this view, which is how
/// six families are kept from inventing six sentences.
///
/// `compact` stays (unlike the running-cards pane, where #341 could drop it):
/// the small family really does draw the tighter version of this pane, so it has
/// two values in use.
private struct ProjectsGateView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry
    var compact = false

    /// Accessory families are system-tinted: fonts still follow the style,
    /// colours stay the system's.
    var systemTinted = false
    private var primaryText: Color { systemTinted ? .primary : style.primaryTextColor }
    private var secondaryText: Color { systemTinted ? .secondary : style.secondaryTextColor }
    private var statusText: Color { systemTinted ? .primary : style.badColor }

    var body: some View {
        VStack(alignment: compact ? .leading : .center, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Image(systemName: entry.paused ? "pause.circle" : "gearshape").widgetIcon(style)
                .font(style.font(size: compact ? 12 : 20))
                .foregroundColor(secondaryText)
            Text(entry.gateLine)
                .font(style.font(compact ? .caption2 : .footnote, weight: compact ? .semibold : nil))
                .foregroundColor(primaryText)
                .fixedSize(horizontal: false, vertical: true)
            if !compact {
                Text(entry.paused
                     ? "Turn it back on under Widgets & Live Activities in the HSCC app."
                     : WidgetDataFailure.notConfigured.operatorLine)
                    .font(style.font(.caption2))
                    .foregroundColor(secondaryText)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: compact ? .leading : .center)
    }
}

/// Nothing usable: either the read failed, or it worked and the answer really is
/// an empty list. Those are different sentences and the caller has already told
/// them apart by which initialiser it used.
private struct ProjectsEmptyView: View {
    @Environment(\.widgetStyle) private var style
    /// The failure, when there is one. nil means the read worked and returned no
    /// projects — an honest empty, not a zero dressed up as data.
    let failure: WidgetDataFailure?
    /// The project the operator picked, when the list came back without it.
    var selectedName: String?
    var compact = false

    /// Accessory families are system-tinted: fonts still follow the style,
    /// colours stay the system's.
    var systemTinted = false
    private var primaryText: Color { systemTinted ? .primary : style.primaryTextColor }
    private var secondaryText: Color { systemTinted ? .secondary : style.secondaryTextColor }
    private var statusText: Color { systemTinted ? .primary : style.badColor }

    var body: some View {
        VStack(alignment: compact ? .leading : .center, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
            Image(systemName: iconName).widgetIcon(style)
                .font(style.font(size: compact ? 12 : 20))
                .foregroundColor(secondaryText)
            Text(title)
                .font(style.font(compact ? .caption2 : .footnote, weight: compact ? .semibold : nil))
                .foregroundColor(primaryText)
                .fixedSize(horizontal: false, vertical: true)
            if !compact {
                Text(detail)
                    .font(style.font(.caption2))
                    .foregroundColor(secondaryText)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: compact ? .leading : .center)
    }

    private var iconName: String {
        guard let failure else { return "tray" }
        return failure.shortLabel == "No connection" ? "wifi.slash" : "exclamationmark.triangle"
    }

    private var title: String {
        guard let failure else {
            if let selectedName { return "No project \"\(selectedName)\"" }
            return "No projects"
        }
        return failure.shortLabel
    }

    private var detail: String {
        if let failure { return failure.operatorLine }
        if selectedName != nil { return "It is not on this cluster any more." }
        return "Nothing is registered on this cluster yet."
    }
}

/// The age + why footer a stale entry owes the operator. Absent when the entry
/// is live, because "just now" is not an age.
private struct ProjectsAgeLine: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry
    var font: Font = .caption2

    var body: some View {
        if let minutes = entry.status.ageMinutes {
            HStack(spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                Image(systemName: "clock.arrow.circlepath").widgetIcon(style)
                    .font(style.font(size: 8))
                Text("updated \(minutes)m ago · \(entry.status.staleReason?.shortLabel ?? "stale")")
            }
            .font(font)
            .foregroundColor(style.warnColor)
        }
    }
}

/// The dot that says whether what is on screen is live, stale, or nothing.
private struct ProjectsStatusDot: View {
    @Environment(\.widgetStyle) private var style
    let status: ProjectsStatus

    var body: some View {
        Circle()
            .fill(status.failure != nil ? style.badColor
                : (status.isStale ? style.warnColor : style.okColor))
            .frame(width: 6, height: 6)
    }
}

// MARK: accessoryCircular — how many projects this entry holds

private struct ProjectsCircularView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry

    var body: some View {
        if !entry.configured || entry.paused {
            // A label, never a number: a paused ring that still drew a count
            // would be the false zero §6 rule 1 refuses. The copy rule inside
            // `shortGateLabel` keeps the setup invite first.
            Text(entry.shortGateLabel)
                .font(style.font(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
                .minimumScaleFactor(0.6)
                .multilineTextAlignment(.center)
                .accessibilityLabel(entry.shortGateLabel)
        } else {
            VStack(spacing: 0) {
                Text(entry.circularNumber.map { "\($0)" } ?? "—")
                    .font(style.font(.body))
                    .fontWeight(.semibold)
                    .foregroundStyle(color)
                Text("projects")
                    .font(style.font(size: 8))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var color: Color {
        if entry.status.failure != nil { return .primary }
        if entry.status.isStale { return .primary }
        return .primary
    }
}

// MARK: accessoryRectangular — the top three names and their board

private struct ProjectsRectangularView: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry

    var body: some View {
        if !entry.configured || entry.paused {
            ProjectsGateView(entry: entry, compact: true, systemTinted: true)
        } else if let failure = entry.status.failure {
            ProjectsEmptyView(failure: failure, compact: true, systemTinted: true)
        } else if entry.rows.isEmpty {
            ProjectsEmptyView(failure: nil, selectedName: entry.selectedName, compact: true, systemTinted: true)
        } else {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(entry.rectangularRows()) { row in
                    HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                        Text(row.name).widgetHeader(style)
                            .font(style.font(.caption2, weight: .semibold))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                        Spacer(minLength: style.spacing(Theme.Spacing.xs.rawValue))
                        if let board = row.board, !board.isEmpty {
                            Text(board)
                                .font(style.font(.caption2))
                                .foregroundStyle(entry.status.isStale ? .primary
                                                    : .secondary)
                                .lineLimit(1)
                        }
                    }
                }
            }
        }
    }
}

// MARK: small — count + up to 3 names (§5.1)

private struct SmallProjectsWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                // ONE step for both gate states (t_5f246177): a body that tests
                // them in two branches is a body that can take the invite step
                // and still fall through to rows on a paused tile. The copy
                // inside the pane puts the invite first (`gateLine` checks
                // `configured` before any paused line), so this does not swap
                // the audit-3 order — it only widens the first step.
                ProjectsGateView(entry: entry, compact: true)
            } else if let failure = entry.status.failure {
                ProjectsEmptyView(failure: failure, compact: true)
            } else if entry.rows.isEmpty {
                ProjectsEmptyView(failure: nil, selectedName: entry.selectedName, compact: true)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    HStack(spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                        ProjectsStatusDot(status: entry.status)
                        Text(headline)
                            .font(style.font(.caption2, weight: .semibold))
                            .foregroundColor(style.secondaryTextColor)
                            .lineLimit(1)
                    }
                    .widgetHeader(style)
                    ForEach(entry.rows.prefix(ProjectsEntry.rowLimit(for: .systemSmall))) { row in
                        // Name + CLI health token only: the small family has
                        // room for a list, not a paragraph per project.
                        ProjectRowView(row: row, showsDetail: false, showsHealth: true)
                    }
                    Spacer(minLength: 0)
                    ProjectsAgeLine(entry: entry)
                }
            }
        }
    }

    /// The count comes from the rows this entry drew, so the number and the list
    /// under it cannot disagree. With a project selected, its name is the
    /// headline — "1 project" would be the same fact said worse.
    private var headline: String {
        if let selected = entry.selectedName { return selected }
        return "\(entry.rows.count) projects"
    }
}

// MARK: medium — up to 6 rows of {name, board} (§5.1)

private struct MediumProjectsWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                // Same ONE gate step as the small body above, for both states.
                ProjectsGateView(entry: entry)
            } else if let failure = entry.status.failure {
                ProjectsEmptyView(failure: failure)
            } else if entry.rows.isEmpty {
                ProjectsEmptyView(failure: nil, selectedName: entry.selectedName)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xs.rawValue)) {
                    HStack(spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                        ProjectsStatusDot(status: entry.status)
                        Text(headline)
                            .font(style.font(.caption, weight: .semibold))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                        Spacer(minLength: style.spacing(Theme.Spacing.xs.rawValue))
                        ProjectsAgeLine(entry: entry)
                    }
                    .widgetHeader(style)
                    ForEach(entry.rows.prefix(ProjectsEntry.rowLimit(for: .systemMedium))) { row in
                        ProjectRowView(row: row, showsBoard: true)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    /// Unfiltered depth belongs in the headline: six rows out of eighteen is a
    /// fact about the list, and "6 projects" would be a count of the crop.
    private var headline: String {
        if let selected = entry.selectedName { return selected }
        return "\(entry.rows.count) projects"
    }
}

// MARK: large — up to 8 rows with the board label (§5.1)

private struct LargeProjectsWidget: View {
    @Environment(\.widgetStyle) private var style
    let entry: ProjectsEntry

    var body: some View {
        Group {
            if !entry.configured || entry.paused {
                // Same ONE gate step as the small body above, for both states.
                ProjectsGateView(entry: entry)
            } else if let failure = entry.status.failure {
                ProjectsEmptyView(failure: failure)
            } else if entry.rows.isEmpty {
                ProjectsEmptyView(failure: nil, selectedName: entry.selectedName)
            } else {
                VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                    HStack(alignment: .firstTextBaseline, spacing: style.spacing(Theme.Spacing.sm.rawValue)) {
                        VStack(alignment: .leading, spacing: style.spacing(Theme.Spacing.xxs.rawValue)) {
                            Text("Projects")
                                .font(style.font(.headline))
                                .foregroundColor(style.primaryTextColor)
                            Text(subline)
                                .font(style.font(.caption2))
                                .foregroundColor(style.secondaryTextColor)
                        }
                        Spacer(minLength: style.spacing(Theme.Spacing.sm.rawValue))
                        ProjectsAgeLine(entry: entry, font: .caption2)
                    }
                    .widgetHeader(style)
                    Divider().background(style.hairlineColor)
                    ForEach(entry.rows.prefix(ProjectsEntry.rowLimit(for: .systemLarge))) { row in
                        ProjectRowView(row: row, showsBoard: true)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    /// Board(s) as well as depth: with room to say it, "18 projects across 4
    /// boards" is the honest frame for the rows under it.
    private var subline: String {
        let boards = Set(entry.rows.compactMap { $0.board?.isEmpty == false ? $0.board : nil })
        let depth = "\(entry.rows.count) projects"
        guard let selected = entry.selectedName else {
            if let only = boards.first, boards.count == 1 { return "\(depth) on board \(only)" }
            return boards.isEmpty ? depth : "\(depth) across \(boards.count) boards"
        }
        if let board = entry.rows.first?.board, !board.isEmpty { return "\(selected) · board \(board)" }
        return selected
    }
}
