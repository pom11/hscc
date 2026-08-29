import SwiftUI

/// Projects — the PRIMARY tab (new project-centric IA).
///
/// Lists the dozen projects from GET /v1/projects. Tapping a project opens a
/// detail screen (`ProjectDetailView`) with segmented sections:
/// Overview · Chat · Board · Settings. This is the surface the operator
/// actually cares about — everything flows from a project.
///
/// Read-only: navigation + pull-to-refresh only. Follows the LoadState pattern
/// (settings-provided client, honest error handling).
struct ProjectsView: View {
    let client: HSCCClient?

    @EnvironmentObject private var unread: ProjectUnreadCenter

    @State private var projects = LoadState<ProjectsResponse>.idle
    /// Whether the cross-project search sheet is presented.
    @State private var showSearch = false

    var body: some View {
        NavigationStack {
            Group {
                if let client {
                    content(client)
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Projects")
            .task {
                // First load: fetch once the view appears (idle → loading).
                if client != nil, projects.value == nil, !projects.isLoading {
                    await load()
                }
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        showSearch = true
                    } label: {
                        Image(systemName: "magnifyingglass")
                    }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(projects.isLoading)
                }
            }
            .sheet(isPresented: $showSearch) {
                SearchView(client: client)
            }
        }
    }

    @ViewBuilder
    private func content(_ client: HSCCClient) -> some View {
        switch projects {
        case .loading, .idle:
            HSLoading("Loading…")
        case .failed(let message):
            HSError("Couldn't load projects", message: message) {
                Task { await load() }
            }
        case .stale(let response, _):
            staleContent(response, client: client)
        case .loaded(let response):
            List {
                Section {
                    Label(response.speak, systemImage: "text.bubble")
                        .font(.hsccMono(15))
                }
                if response.projects.isEmpty {
                    Section {
                        Text("No projects registered.")
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                ForEach(response.projects) { project in
                    NavigationLink {
                        ProjectDetailView(client: client, project: project)
                    } label: {
                        projectRow(project)
                    }
                }
            }
            .refreshable { await load() }
        }
    }

    /// Live-free version of the list, headed by a stale banner instead of the
    /// live `speak` line — same rows, clearly marked as last-known.
    @ViewBuilder
    private func staleContent(_ response: ProjectsResponse, client: HSCCClient) -> some View {
        List {
            Section {
                StaleBanner(age: staleMessage ?? "", reason: "Can't reach the cluster right now.") {
                    Task { await load() }
                }
            }
            if response.projects.isEmpty {
                Section {
                    Text("No projects registered.")
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            ForEach(response.projects) { project in
                NavigationLink {
                    ProjectDetailView(client: client, project: project)
                } label: {
                    projectRow(project)
                }
            }
        }
        .refreshable { await load() }
    }

    private var staleMessage: String? { projects.staleMessage }

    @ViewBuilder
    private func projectRow(_ project: Project) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(project.name)
                    .font(.body.weight(.medium))
                    .foregroundColor(Theme.Semantic.onSurface)
                unreadBadge(project.name)
            }
            HSMetaLine([project.board,
                        project.displayTopic.isEmpty ? nil : "topic \(project.displayTopic)"])
        }
    }

    /// The unread badge for a project row — the app's notification mechanism
    /// (t_267da363). Shown only when there are unread replies waiting; hidden
    /// (empty view) at zero so the list stays clean.
    @ViewBuilder
    private func unreadBadge(_ project: String) -> some View {
        let count = unread.count(for: project)
        if count > 0 {
            Text("\(count)")
                .font(.caption2.weight(.bold))
                .foregroundColor(.white)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Theme.Semantic.warn, in: Capsule())
        }
    }

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "folder", verb: "to see your projects")
    }

    private func load() async {
        guard let client else { return }
        projects = await Offline.load(projects,
                                      cacheKey: EndpointPath.projects,
                                      client: client) {
            try await client.projects()
        }
    }
}

/// One project's detail — the hub for everything about that project.
///
/// Four segmented sections behind a picker:
///   * **Overview** — board counts + git state (GET /v1/projects/{name}).
///   * **Chat**     — OrchestratorChatView fixed to THIS project's orchestrator.
///   * **Board**    — the project's kanban board (cards filtered to its board).
///   * **Settings** — project-level settings SHELL (a follow-up card fills this in).
///
/// Each section owns its own load state, so switching never cross-contaminates.
/// Follow-up cards build depth on top — the navigation shell is established here.
struct ProjectDetailView: View {
    let client: HSCCClient
    let project: Project

    enum Section: String, CaseIterable, Identifiable {
        case overview, chat, board, settings
        var id: String { rawValue }
        var label: String {
            switch self {
            case .overview: return "Overview"
            case .chat: return "Chat"
            case .board: return "Board"
            case .settings: return "Settings"
            }
        }
    }

    @State private var selected: Section = .overview

    var body: some View {
        VStack(spacing: 0) {
            Picker("Section", selection: $selected) {
                ForEach(Section.allCases) { section in
                    Text(section.label).tag(section)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)
            .padding(.vertical, 8)

            switch selected {
            case .overview: ProjectOverviewView(client: client, name: project.name)
            case .chat:     OrchestratorChatView(project: project.name)
            case .board:    ProjectBoardView(client: client, board: project.board)
            case .settings: ProjectSettingsView(client: client, name: project.name)
            }
        }
        .navigationTitle(project.name)
        .navigationBarTitleDisplayMode(.inline)
    }
}

/// Overview section — board counts + git state (GET /v1/projects/{name}).
struct ProjectOverviewView: View {
    let client: HSCCClient
    let name: String

    @State private var detail = LoadState<ProjectDetailResponse>.idle

    var body: some View {
        Group {
            switch detail {
            case .loading, .idle:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load project", message: message) {
                    Task { await load() }
                }
            case .stale(let state, let ageMessage):
                content(state, staleMessage: ageMessage)
            case .loaded(let state):
                content(state, staleMessage: nil)
            }
        }
        .task {
            if detail.value == nil, !detail.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ state: ProjectDetailResponse, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            Section {
                Label(state.speak, systemImage: "text.bubble")
                    .font(.hsccMono(15))
            }

            Section("Project") {
                if let topic = state.displayTopic, !topic.isEmpty {
                    LabeledContent("Topic") { Text(topic).font(.hsccMono(15)) }
                }
                if let board = state.board, !board.isEmpty {
                    LabeledContent("Board") { Text(board).font(.hsccMono(15)) }
                }
                if let repo = state.repo, !repo.isEmpty {
                    LabeledContent("Repo") { Text(repo).lineLimit(1).truncationMode(.middle).font(.hsccMono(15)) }
                }
                if let counts = state.board_counts {
                    let sorted = counts
                        .filter { $0.key != "total" }
                        .sorted { $0.value > $1.value }
                    if !sorted.isEmpty {
                        let openTotal = counts["total"] ?? sorted.map(\.value).reduce(0, +)
                        LabeledContent("Open cards") { Text("\(openTotal)").font(.hsccMono(15)) }
                        ForEach(sorted, id: \.key) { status, count in
                            LabeledContent(status.capitalized) { Text("\(count)").font(.hsccMono(15)) }
                        }
                    }
                }
            }

            if let git = state.git, git.is_repo == true {
                Section("Git") {
                    if let branch = git.branch, !branch.isEmpty {
                        LabeledContent("Branch") { Text(branch).font(.hsccMono(15)) }
                    }
                    LabeledContent("Dirty") { Text(git.dirty == true ? "Yes" : "No") }
                    if let age = git.last_activity_seconds_ago {
                        LabeledContent("Last commit") { Text(timeAgo(age)) }
                    }
                    if let head = git.head, !head.isEmpty {
                        LabeledContent("Head") { Text(head.prefix(8)).font(.hsccMono(15)) }
                    }
                    if let uncommitted = git.uncommitted, !uncommitted.isEmpty {
                        LabeledContent("Uncommitted") {
                            Text("\(uncommitted.count) file\(uncommitted.count == 1 ? "" : "s")").font(.hsccMono(15))
                        }
                    }
                }
            } else {
                Section("Git") {
                    Text("Not a git repo.")
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
        .refreshable { await load() }
    }

    private func timeAgo(_ seconds: Int) -> String {
        let interval = TimeInterval(max(0, seconds))
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: Date(timeIntervalSinceNow: -interval),
                                        relativeTo: Date())
    }

    private func load() async {
        detail = await Offline.load(detail,
                                    cacheKey: "/v1/projects/\(name)",
                                    client: client) {
            try await client.projectDetail(name)
        }
    }
}

/// Board section — THIS project's kanban board (read-only).
///
/// Fetches GET /v1/cards and filters to the project's board, so the operator
/// sees exactly this project's cards in one place. Also brings in the board's
/// blocked (/v1/kanban/blocked) and stale (/v1/kanban/stale) reads, filtered
/// to this project, under a "Needs attention" heading — the states the operator
/// actively cares about.
struct ProjectBoardView: View {
    let client: HSCCClient
    let board: String?

    @State private var cards = LoadState<CardsResponse>.idle
    @State private var blocked = LoadState<KanbanBlockedResponse>.idle
    @State private var stale = LoadState<KanbanStaleResponse>.idle

    var body: some View {
        Group {
            switch cards {
            case .loading, .idle:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load the board", message: message) {
                    Task { await load() }
                }
            case .stale(let response, let ageMessage):
                content(response, staleMessage: ageMessage)
            case .loaded(let response):
                content(response, staleMessage: nil)
            }
        }
        .task {
            if cards.value == nil, !cards.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ response: CardsResponse, staleMessage: String?) -> some View {
        // Filter to this project's board. Every card carries a `board`; cards
        // with no board match are excluded (the board section is project-scoped).
        let filtered = response.cards.filter { card in
            guard let board else { return card.board == nil || card.board?.isEmpty == true }
            return card.board == board
        }
        // Blocked / stale scoped to THIS project's board.
        let projectBlocked = blocked.value?.tasks?.filter { $0.board == board } ?? []
        let projectStale = stale.value?.tasks?.filter { $0.board == board } ?? []

        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            if filtered.isEmpty && projectBlocked.isEmpty && projectStale.isEmpty {
                Section {
                    HSEmpty("No cards on \(board ?? "this") board",
                             message: "Nothing is open here yet. This section fills in as the project's board develops.",
                             systemImage: "square.grid.2x2")
                }
            } else {
                // Active cards first — the primary surface.
                Section("Open cards") {
                    if filtered.isEmpty {
                        Text("No open cards on this board.")
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    } else {
                        ForEach(filtered) { card in
                            NavigationLink {
                                CardDetailView(cardID: card.id)
                            } label: {
                                cardRow(card)
                            }
                        }
                    }
                }

                // Blocked — things stalled that the operator cares about.
                if !projectBlocked.isEmpty {
                    Section("Blocked — \(projectBlocked.count)") {
                        ForEach(projectBlocked, id: \.id) { card in
                            buttonRow(icon: "hand.raised.fill",
                                      color: Theme.Semantic.warn,
                                      title: card.displayTitle,
                                      subtitle: subtitle(blockCard: card))
                        }
                    }
                }

                // Stale — non-terminal cards sitting too long.
                if !projectStale.isEmpty {
                    Section("Stale — \(projectStale.count)") {
                        ForEach(projectStale, id: \.id) { card in
                            buttonRow(icon: "clock.fill",
                                      color: Theme.Semantic.warn,
                                      title: card.displayTitle,
                                      subtitle: subtitle(staleCard: card))
                        }
                    }
                }
            }
        }
        .refreshable { await load() }
    }

    private func subtitle(blockCard card: BlockedCard) -> String {
        var parts: [String] = []
        if let why = card.why, !why.isEmpty { parts.append(why) }
        if card.comments?.isEmpty == false { parts.append("\(card.comments?.count ?? 0) comments") }
        parts.append(ageString(card.age_days, noun: "blocked"))
        return parts.joined(separator: " · ")
    }

    private func subtitle(staleCard card: StaleCard) -> String {
        ageString(card.age_days, noun: "stale")
    }

    private func ageString(_ days: Int?, noun: String) -> String {
        guard let days else { return "age unknown" }
        return days == 0 ? "\(noun) today" : "\(days)d \(noun)"
    }

    @ViewBuilder
    private func buttonRow(icon: String, color: Color, title: String, subtitle: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon)
                .foregroundColor(color)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .foregroundColor(Theme.Semantic.onSurface)
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
    }

    @ViewBuilder
    private func cardRow(_ card: Card) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle()
                .fill(statusColor(card.displayStatus))
                .frame(width: 10, height: 10)
            VStack(alignment: .leading, spacing: 3) {
                Text(card.displayTitle)
                    .foregroundColor(Theme.Semantic.onSurface)
                HSMetaLine([card.assignee, card.id])
            }
        }
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return Theme.Semantic.ok
        case "review", "blocked": return Theme.Semantic.warn
        case "done", "merged", "closed": return Theme.Semantic.ok
        case "failed", "failing": return Theme.Semantic.bad
        default: return Theme.Semantic.neutral
        }
    }

    private func load() async {
        cards = .loading
        // Load the three reads concurrently so the board fills in fast; each
        // writes to its own LoadState so one failing read never blanks the rest.
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.loadCards() }
            group.addTask { await self.loadBlockedIfPresent() }
            group.addTask { await self.loadStaleIfPresent() }
        }
    }

    private func loadCards() async {
        cards = await Offline.load(cards,
                                   cacheKey: EndpointPath.cards,
                                   client: client) {
            try await client.cards()
        }
    }

    /// Load blocked/stale best-effort: if their reads fail we still show the
    /// active cards (the board section must not blank because a hygiene read
    /// hiccuped).
    private func loadBlockedIfPresent() async {
        do {
            blocked = .loaded(try await client.kanbanBlocked())
        } catch {
            blocked = .failed((error as? HSCCError)?.localizedDescription ?? "Something went wrong.")
        }
    }

    private func loadStaleIfPresent() async {
        do {
            stale = .loaded(try await client.kanbanStale(olderThan: 0))
        } catch {
            stale = .failed((error as? HSCCError)?.localizedDescription ?? "Something went wrong.")
        }
    }
}

/// Settings section — what the operator can actually see/change for a project.
///
/// The project registry is basically read-only for the operator: repo path,
/// board name, session topic, and the orchestrator profile/session live on the
/// server (provisioned / driven by the project hub), not in the app. There is
/// nothing here the operator can edit from the phone, so everything is
/// presented read-only — honest about what's configurable vs. what is not. No
/// fake editable controls.
///
/// Shows repo path, board name, topic, and the orchestrator's project session.
struct ProjectSettingsView: View {
    let client: HSCCClient
    let name: String

    @State private var detail = LoadState<ProjectDetailResponse>.idle

    var body: some View {
        Group {
            switch detail {
            case .loading, .idle:
                HSLoading("Loading…")
            case .failed(let message):
                HSError("Couldn't load project settings", message: message) {
                    Task { await load() }
                }
            case .stale(let state, let ageMessage):
                content(state, staleMessage: ageMessage)
            case .loaded(let state):
                content(state, staleMessage: nil)
            }
        }
        .task {
            if detail.value == nil, !detail.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ state: ProjectDetailResponse, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            Section {
                Text("These project settings live on the cluster, driven by the project hub. They are read-only from this app.")
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }

            Section("Repository") {
                row("Path", value: state.repo)
                if let git = state.git, git.is_repo == true, let branch = git.branch, !branch.isEmpty {
                    row("Branch", value: branch)
                }
            }

            Section("Board") {
                row("Name", value: state.board)
                if let counts = state.board_counts, counts["total"] != nil {
                    row("Open cards", value: "\(counts["total"] ?? 0)")
                }
            }

            Section("Orchestrator") {
                // The orchestrator's per-project profile + session follow the
                // project naming convention: `<project>-orch` / session `<project>`.
                row("Profile", value: "\(name)-orch")
                row("Session", value: name)
                if let topic = state.displayTopic, !topic.isEmpty {
                    row("Topic", value: topic)
                }
            }

            Section {
                Text("The orchestrator session persists context across messages — this conversation is continuous, not one-off. To restart it, recreate the session on the cluster.")
                    .font(.footnote)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    @ViewBuilder
    private func row(_ label: String, value: String?) -> some View {
        if let value, !value.isEmpty {
            LabeledContent(label) {
                Text(value).font(.hsccMono(15)).textSelection(.enabled)
            }
        }
    }

    private func load() async {
        detail = await Offline.load(detail,
                                    cacheKey: "/v1/projects/\(name)",
                                    client: client) {
            try await client.projectDetail(name)
        }
    }
}
