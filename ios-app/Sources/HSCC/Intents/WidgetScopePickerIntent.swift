import AppIntents
import Foundation

// ---------------------------------------------------------------------------
// WidgetScopePickerIntent.swift — pointing a widget at ONE thing (t_98fb8726)
//
// The configuration half of the pivot: the widgets show data, this intent says
// WHICH slice of it — one board, one project, or one node — and persists that
// choice through the App Group so every extension's timeline reads it back.
// It is the sheet the operator answers when the cluster has 23 boards, 18
// projects and a handful of nodes (`WIDGET_DATA_CONTRACT.md` §4); "which one"
// is the only configuration the read-only surfaces genuinely need.
//
// WRITE PATH — one blob, one key, one rule. `perform()` folds the parameters
// and calls `WidgetScope.apply(_:)`, which does a read-modify-write of the
// SAME `AppGroupWidgetSettings` blob (`hscc.widgetSettings`) the extensions
// already load for cadence and sections. A widget therefore reads the scope on
// the exact path cadence arrives on — there is no second store to drift. The
// fold itself lives as statics on `WidgetScope` (`Sources/Shared/SharedStore.swift`)
// precisely so `widget_settings_check.sh` can extract the real code verbatim
// and EXECUTE it against a real named suite: the gate proves the intent's own
// write lands where `AppGroupWidgetSettings.load()` reads, not a copy of it.
//
// WHY HERE, APP-SIDE. `perform()` runs in the APP process (the configuration
// sheet calls back into the app; with the app uninstalled nothing runs and the
// sheet keeps its own copy of the parameter — which is why consumers also
// honour the intent's own `configuration` value and treat the App-Group scope
// as the fleet-wide default). That makes this an HSCC-target file: it uses the
// same App Group / Keychain plumbing every other intent here uses
// (`IntentClient.swift`), and no extension binary links it.
//
// READERS. A surface scoped by this intent renders `configuration`'s value
// when its own sheet set one, and `AppGroupWidgetSettings.load()?.scope`
// otherwise; the sentinel never reaches either, and `nil` means "everything".
// The widget-side adoption (BoardStatus/Projects/Cards/KanbanMonitor sheets
// switching onto this one intent) is the follow-up named in the PR body.
//
// PICKER DATA. Each query fetches live through `WidgetDataClient` — GET-only,
// closed allowlist — because the sheet is an operator action, not a timeline
// refresh: it does not spend the contract's ≤2-requests-per-refresh budget (§2,
// measured on the timeline path). Every query ALWAYS includes the "All"
// sentinel, so a never-configured or unreachable cluster still yields a
// usable sheet; an empty list on failure is honest ("nothing was fetched"),
// never a fabricated row. `entities(for:)` echoes the stored identifier for
// the same reason #319's picker does: a board renamed since the widget was
// configured must stay visible to the widget so it can render `No board "x"`,
// the truth the sheet cannot express.
//
// §7 hygiene: a node is identified by `MonitorNode.displayLabel` (hostname,
// else the last octet) — the address never enters this file's parameters and
// never reaches the App Group. `AppEntity`/`EntityQuery` are conformed by
// hand (this SDK's `@AppEntity` macro takes only `schema:`, the `name:` form
// is gone) and no schema bridge is needed because nothing outside this file
// persists an entity reference.
// ---------------------------------------------------------------------------

// MARK: - The picker entity

/// One row in a scope picker. Its `id` IS the identifier that gets persisted
/// (board slug / project name / node label), so what the operator taps and
/// what the store holds are the same string — no second mapping to get wrong.
struct WidgetScopeOption: AppEntity {
    static var typeDisplayRepresentation: TypeDisplayRepresentation =
        TypeDisplayRepresentation(name: "Widget Scope")
    static var defaultQuery = WidgetScopeAllQuery()

    let id: String
    /// What kind of thing this is ("board", "project", "node"), shown as the
    /// row's subtitle so the three pickers cannot be confused for one list.
    let subtitle: String?

    init(id: String, subtitle: String? = nil) {
        self.id = id
        self.subtitle = subtitle
    }

    var displayRepresentation: DisplayRepresentation {
        // The sentinel draws a word, never `*` — the operator chose "All",
        // not a board literally named star.
        if id == WidgetScope.wildcard {
            return DisplayRepresentation(title: "All")
        }
        if let subtitle {
            return DisplayRepresentation(title: "\(id)", subtitle: "\(subtitle)")
        }
        return DisplayRepresentation(title: "\(id)")
    }
}

/// The fallback query (and the query of the `WidgetScopeOption.defaultQuery`
/// bridge): resolves the sentinel explicitly and nothing else, so every picker
/// can offer "All" without a network.
struct WidgetScopeAllQuery: EntityQuery {
    func entities(for identifiers: [WidgetScopeOption.ID]) async throws -> [WidgetScopeOption] {
        identifiers.contains(WidgetScope.wildcard)
            ? [WidgetScopeOption(id: WidgetScope.wildcard)]
            : []
    }

    func suggestedEntities() async throws -> [WidgetScopeOption] {
        [WidgetScopeOption(id: WidgetScope.wildcard)]
    }

    /// Pre-selecting "All" states the current truth: nothing has been picked.
    func defaultResult() async -> WidgetScopeOption? {
        WidgetScopeOption(id: WidgetScope.wildcard)
    }
}

// MARK: - The three pickers' queries

/// Boards: the names `GET /v1/kanban/running` carries (23 strings, 264 B —
/// §4.3), topped up with the `board` of every project so a board with zero
/// kanban traffic is still pickable. On any failure: sentinel only.
struct WidgetScopeBoardQuery: EntityQuery {
    func entities(for identifiers: [WidgetScopeOption.ID]) async throws -> [WidgetScopeOption] {
        identifiers.map { WidgetScopeOption(id: $0, subtitle: "board") }
    }

    func suggestedEntities() async throws -> [WidgetScopeOption] {
        let boards = await liveBoards()
        return [WidgetScopeOption(id: WidgetScope.wildcard)]
            + boards.map { WidgetScopeOption(id: $0, subtitle: "board") }
    }

    func defaultResult() async -> WidgetScopeOption? {
        WidgetScopeOption(id: WidgetScope.wildcard)
    }

    private func liveBoards() async -> [String] {
        guard let client = WidgetDataClient.makeIfConfigured() else { return [] }
        var names: [String] = []
        if case .success(let running) = await client.kanbanRunning() {
            names.append(contentsOf: running.boards ?? [])
        }
        if case .success(let projects) = await client.projects() {
            names.append(contentsOf: projects.projects.compactMap { $0.board })
        }
        // First-seen order kept (the running list's order is already the
        // operator's mental model); a project board that duplicates a running
        // board folds into the one row.
        var seen = Set<String>()
        return names.filter { seen.insert($0).inserted }
    }
}

/// Projects: `GET /v1/projects` — its `{name, board}` projection (§5.1) names
/// the row and subtotals it. `repo` (an absolute local path, §7) is not even
/// decoded at this layer. On any failure: sentinel only.
struct WidgetScopeProjectQuery: EntityQuery {
    func entities(for identifiers: [WidgetScopeOption.ID]) async throws -> [WidgetScopeOption] {
        identifiers.map { WidgetScopeOption(id: $0, subtitle: "project") }
    }

    func suggestedEntities() async throws -> [WidgetScopeOption] {
        guard let client = WidgetDataClient.makeIfConfigured() else {
            return [WidgetScopeOption(id: WidgetScope.wildcard)]
        }
        guard case .success(let projects) = await client.projects() else {
            return [WidgetScopeOption(id: WidgetScope.wildcard)]
        }
        var seen = Set<String>()
        let rows = projects.projects
            .filter { seen.insert($0.name).inserted }
            .map { WidgetScopeOption(id: $0.name, subtitle: $0.board ?? "project") }
        return [WidgetScopeOption(id: WidgetScope.wildcard)] + rows
    }

    func defaultResult() async -> WidgetScopeOption? {
        WidgetScopeOption(id: WidgetScope.wildcard)
    }
}

/// Nodes: `GET /v1/cluster/monitor` labels. `MonitorNode.displayLabel` is the
/// repo's single node-identity rule (sample hostname, else the last octet —
/// never the address, §7), so the persisted scope can name a node without
/// ever holding where it lives. On any failure: sentinel only.
struct WidgetScopeNodeQuery: EntityQuery {
    func entities(for identifiers: [WidgetScopeOption.ID]) async throws -> [WidgetScopeOption] {
        identifiers.map { WidgetScopeOption(id: $0, subtitle: "node") }
    }

    func suggestedEntities() async throws -> [WidgetScopeOption] {
        guard let client = WidgetDataClient.makeIfConfigured() else {
            return [WidgetScopeOption(id: WidgetScope.wildcard)]
        }
        guard case .success(let monitor) = await client.clusterMonitor() else {
            return [WidgetScopeOption(id: WidgetScope.wildcard)]
        }
        var seen = Set<String>()
        let rows = monitor.nodes
            .map { $0.displayLabel }
            .filter { !$0.isEmpty && seen.insert($0).inserted }
            .map { WidgetScopeOption(id: $0, subtitle: "node") }
        return [WidgetScopeOption(id: WidgetScope.wildcard)] + rows
    }

    func defaultResult() async -> WidgetScopeOption? {
        WidgetScopeOption(id: WidgetScope.wildcard)
    }
}

// MARK: - The intent

/// The configuration intent every scoped widget's sheet runs, and the one the
/// widget/Live Activity config screen will drive. Exactly one of the three
/// parameters names a real thing; "All" in any picker means "everything".
///
/// The whole point is persistence: the tap does not just re-render one
/// widget's own configuration, it writes the fleet-wide scope through the App
/// Group (`WidgetScope.apply`), which is what makes one choice visible to
/// every surface that reads `AppGroupWidgetSettings.load()?.scope`.
struct WidgetScopePickerIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Widget Scope"
    static var description = IntentDescription(
        "Points the HSCC widgets at one board, project or node. Choose exactly one; choose All for everything.")

    @Parameter(title: "Board")
    var board: WidgetScopeOption?

    @Parameter(title: "Project")
    var project: WidgetScopeOption?

    @Parameter(title: "Node")
    var node: WidgetScopeOption?

    static var parameterSummary: some ParameterSummary {
        Summary("Point widgets at \(\.$board)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        // Unwrap and delegate only — the fold, the normalization and the
        // persist are the `WidgetScope` statics, which
        // `widget_settings_check.sh` extracts verbatim and EXECUTES against a
        // real named suite, so the rule that decides what the store holds has
        // one copy, proven on both sides. A `let`, not a `var`: the
        // `@Parameter` setter writes through the wrapper's own box.
        let stored = WidgetScope.buildPickerSelection(board: board?.id,
                                                      project: project?.id,
                                                      node: node?.id)
        guard WidgetScope.apply(stored) else {
            return .result(dialog: IntentDialog(
                stringLiteral: "Couldn't save the scope — the shared settings store isn't available."))
        }
        // Speak what the STORE now holds (read back through the extensions'
        // own load path), not what was requested — if the suite refused part
        // of the write, the sentence matches reality.
        let line = (AppGroupWidgetSettings.load()?.scope ?? WidgetScope()).summaryLine
        return .result(dialog: IntentDialog(stringLiteral: "Widget scope: \(line)."))
    }
}
