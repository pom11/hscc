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
// ---------------------------------------------------------------------------

// MARK: - The stored projection (§7)

/// The two keys §5.1 allows this surface to keep, and the only shape that can
/// be written to the App Group. A separate type from `ProjectLite` on purpose:
/// the response is decodable-only, so the encode path has nowhere to pick up a
/// third field even if the payload grows one.
struct ProjectRow: Codable, Identifiable, Equatable, Hashable, Sendable {
    let name: String
    let board: String?

    var id: String { name }
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
        payload.projects.prefix(rowLimit).map { ProjectRow(name: $0.name, board: $0.board) }
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

    /// The t_5c554c5b invariant, expressed once: an unconfigured app shows the
    /// setup invite, never "can't reach the cluster". Every family checks this
    /// FIRST.
    var isUnconfigured: Bool { status.failure == .notConfigured }

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
}

// MARK: - Timeline provider

/// The provider. One read per refresh, the outcome resolved by `WidgetData.read`,
/// and a usable entry on every path — there is no branch that returns a blank.
struct ProjectsTimelineProvider: AppIntentTimelineProvider {
    /// Cadence. `WidgetSurfaceKind` has no `widgetProjects` case yet, so this
    /// surface is not in the shared cadence/Active registry the other widgets
    /// read; 5 minutes is the contract's stated widget default (§2) and the same
    /// cadence `ClusterWidget`/`MonitorWidget` use until the config-screen slice
    /// adds the surface (then this reads `setting(for:)` like they do).
    static let refreshMinutes = 5

    func placeholder(in context: Context) -> ProjectsEntry { .placeholderEntry }

    func snapshot(for configuration: ProjectFilterIntent, in context: Context) async -> ProjectsEntry {
        await fetch(for: configuration)
    }

    func timeline(for configuration: ProjectFilterIntent, in context: Context) async -> Timeline<ProjectsEntry> {
        let entry = await fetch(for: configuration)
        let next = Calendar.current.date(byAdding: .minute, value: Self.refreshMinutes, to: .now)
            ?? .now.addingTimeInterval(300)
        return Timeline(entries: [entry], policy: .after(next))
    }

    /// One recommendation so the gallery has something to show without the
    /// operator opening the sheet first.
    func recommendations() -> [AppIntentRecommendation<ProjectFilterIntent>] {
        [AppIntentRecommendation(intent: ProjectFilterIntent(), description: Text("All projects"))]
    }

    // MARK: Fetch

    private func fetch(for configuration: ProjectFilterIntent) async -> ProjectsEntry {
        let selectedName = configuration.selectedName
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

/// The Home Screen projects widget. Three families, one read, no actions.
struct ProjectsWidget: Widget {
    let kind = "ProjectsWidget"

    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind,
                              intent: ProjectFilterIntent.self,
                              provider: ProjectsTimelineProvider()) { entry in
            ProjectsWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    Theme.Semantic.surface
                }
        }
        .configurationDisplayName("Projects")
        .description("The cluster's projects and the board each one is on. Read-only.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

// MARK: - Views

/// Picks the layout by family. Every family runs the same dispatch order:
/// unconfigured → failure → nothing to draw → rows.
struct ProjectsWidgetView: View {
    let entry: ProjectsEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .systemSmall:
            SmallProjectsWidget(entry: entry)
        case .systemLarge:
            LargeProjectsWidget(entry: entry)
        default:
            MediumProjectsWidget(entry: entry)
        }
    }
}

// MARK: Shared parts

/// One project row: the name, and the board beside it when the family has room.
private struct ProjectRowView: View {
    let row: ProjectRow
    var showsBoard = false

    var body: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: "folder")
                .font(.system(size: 9))
                .foregroundColor(Theme.Semantic.neutral)
            Text(row.name)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
            if showsBoard, let board = row.board, !board.isEmpty {
                Spacer(minLength: Theme.Spacing.xs.rawValue)
                Text(board)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
        }
    }
}

/// The t_5c554c5b invite. Shown for `.notConfigured` only — a bad host/port or a
/// rejected token is a different sentence, and it comes from `operatorLine`.
private struct ProjectsSetupView: View {
    var compact = false

    var body: some View {
        VStack(alignment: compact ? .leading : .center, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: "gearshape")
                .font(.system(size: compact ? 12 : 20))
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text("Set up in HSCC")
                .font(compact ? .caption2.weight(.semibold) : .footnote)
                .foregroundColor(Theme.Semantic.onSurface)
                .fixedSize(horizontal: false, vertical: true)
            if !compact {
                Text(WidgetDataFailure.notConfigured.operatorLine)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
    /// The failure, when there is one. nil means the read worked and returned no
    /// projects — an honest empty, not a zero dressed up as data.
    let failure: WidgetDataFailure?
    /// The project the operator picked, when the list came back without it.
    var selectedName: String?
    var compact = false

    var body: some View {
        VStack(alignment: compact ? .leading : .center, spacing: Theme.Spacing.xs.rawValue) {
            Image(systemName: iconName)
                .font(.system(size: compact ? 12 : 20))
                .foregroundColor(Theme.Semantic.neutral)
            Text(title)
                .font(compact ? .caption2.weight(.semibold) : .footnote)
                .foregroundColor(Theme.Semantic.onSurface)
                .fixedSize(horizontal: false, vertical: true)
            if !compact {
                Text(detail)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
    let entry: ProjectsEntry
    var font: Font = .caption2

    var body: some View {
        if let minutes = entry.status.ageMinutes {
            HStack(spacing: Theme.Spacing.xxs.rawValue) {
                Image(systemName: "clock.arrow.circlepath")
                    .font(.system(size: 8))
                Text("\(minutes)m ago · \(entry.status.staleReason?.shortLabel ?? "stale")")
            }
            .font(font)
            .foregroundColor(Theme.Semantic.warn)
        }
    }
}

/// The dot that says whether what is on screen is live, stale, or nothing.
private struct ProjectsStatusDot: View {
    let status: ProjectsStatus

    var body: some View {
        Circle()
            .fill(status.failure != nil ? Theme.Semantic.bad
                : (status.isStale ? Theme.Semantic.warn : Theme.Semantic.ok))
            .frame(width: 6, height: 6)
    }
}

// MARK: small — count + up to 3 names (§5.1)

private struct SmallProjectsWidget: View {
    let entry: ProjectsEntry

    var body: some View {
        Group {
            if entry.isUnconfigured {
                ProjectsSetupView(compact: true)
            } else if let failure = entry.status.failure {
                ProjectsEmptyView(failure: failure, compact: true)
            } else if entry.rows.isEmpty {
                ProjectsEmptyView(failure: nil, selectedName: entry.selectedName, compact: true)
            } else {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                    HStack(spacing: Theme.Spacing.xs.rawValue) {
                        ProjectsStatusDot(status: entry.status)
                        Text(headline)
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .lineLimit(1)
                    }
                    ForEach(entry.rows.prefix(ProjectsEntry.rowLimit(for: .systemSmall))) { row in
                        ProjectRowView(row: row)
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
    let entry: ProjectsEntry

    var body: some View {
        Group {
            if entry.isUnconfigured {
                ProjectsSetupView()
            } else if let failure = entry.status.failure {
                ProjectsEmptyView(failure: failure)
            } else if entry.rows.isEmpty {
                ProjectsEmptyView(failure: nil, selectedName: entry.selectedName)
            } else {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        ProjectsStatusDot(status: entry.status)
                        Text(headline)
                            .font(.caption.weight(.semibold))
                            .foregroundColor(Theme.Semantic.onSurface)
                            .lineLimit(1)
                        Spacer(minLength: Theme.Spacing.xs.rawValue)
                        ProjectsAgeLine(entry: entry)
                    }
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
    let entry: ProjectsEntry

    var body: some View {
        Group {
            if entry.isUnconfigured {
                ProjectsSetupView()
            } else if let failure = entry.status.failure {
                ProjectsEmptyView(failure: failure)
            } else if entry.rows.isEmpty {
                ProjectsEmptyView(failure: nil, selectedName: entry.selectedName)
            } else {
                VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
                        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                            Text("Projects")
                                .font(.headline)
                                .foregroundColor(Theme.Semantic.onSurface)
                            Text(subline)
                                .font(.caption2)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        Spacer(minLength: Theme.Spacing.sm.rawValue)
                        ProjectsAgeLine(entry: entry, font: .caption2)
                    }
                    Divider().background(Theme.Elevation.hairline)
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
