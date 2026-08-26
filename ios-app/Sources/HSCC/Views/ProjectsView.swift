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

    @State private var projects = LoadState<ProjectsResponse>.idle

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
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(projects.isLoading)
                }
            }
        }
    }

    @ViewBuilder
    private func content(_ client: HSCCClient) -> some View {
        switch projects {
        case .loading:
            ProgressView("Loading…")
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't load projects", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Try again") { Task { await load() } }
            }
        case .loaded(let response):
            List {
                Section {
                    Label(response.speak, systemImage: "text.bubble")
                        .font(.subheadline)
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
        case .idle:
            ProgressView("Loading…")
                .task { await load() }
        }
    }

    @ViewBuilder
    private func projectRow(_ project: Project) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(project.name)
                .font(.body.weight(.medium))
                .foregroundColor(Theme.Semantic.onSurface)
            HStack(spacing: 6) {
                if let board = project.board, !board.isEmpty {
                    Text(board).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                if !project.displayTopic.isEmpty {
                    Text("topic \(project.displayTopic)").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
        }
    }

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "folder")
                .font(.system(size: 44))
                .foregroundColor(Theme.Semantic.neutral)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to see your projects.")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    private func load() async {
        guard let client else { return }
        projects = .loading
        do {
            projects = .loaded(try await client.projects())
        } catch {
            projects = .failed((error as? HSCCError)?.localizedDescription ?? "Something went wrong.")
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
            case .settings: ProjectSettingsView(name: project.name)
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
                ProgressView("Loading…")
            case .failed(let message):
                ContentUnavailableView {
                    Label("Couldn't load project", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }
            case .loaded(let state):
                content(state)
            }
        }
        .task {
            if detail.value == nil, !detail.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ state: ProjectDetailResponse) -> some View {
        List {
            Section {
                Label(state.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }

            Section("Project") {
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
        detail = .loading
        do {
            detail = .loaded(try await client.projectDetail(name))
        } catch {
            detail = .failed((error as? HSCCError)?.localizedDescription ?? "Something went wrong.")
        }
    }
}

/// Board section — THIS project's kanban board (read-only).
///
/// Fetches GET /v1/cards and filters to the project's board, so the operator
/// sees exactly the cards for this project in one place. A follow-up card
/// builds deeper project depth on top of this.
struct ProjectBoardView: View {
    let client: HSCCClient
    let board: String?

    @State private var cards = LoadState<CardsResponse>.idle

    var body: some View {
        Group {
            switch cards {
            case .loading, .idle:
                ProgressView("Loading…")
            case .failed(let message):
                ContentUnavailableView {
                    Label("Couldn't load the board", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(message)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }
            case .loaded(let response):
                content(response)
            }
        }
        .task {
            if cards.value == nil, !cards.isLoading { await load() }
        }
    }

    @ViewBuilder
    private func content(_ response: CardsResponse) -> some View {
        // Filter to this project's board. Every card carries a `board`; cards
        // with no board match are excluded (the board section is project-scoped).
        let filtered = response.cards.filter { card in
            guard let board else { return card.board == nil || card.board?.isEmpty == true }
            return card.board == board
        }
        List {
            if filtered.isEmpty {
                Section {
                    ContentUnavailableView {
                        Label("No cards on \(board ?? "this") board", systemImage: "square.grid.2x2")
                    } description: {
                        Text("Nothing is open here yet. This section fills in as the project's board develops.")
                    }
                }
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
        .refreshable { await load() }
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
                HStack(spacing: 6) {
                    if let assignee = card.assignee, !assignee.isEmpty {
                        Text(assignee).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    Text(card.id).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
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
        do {
            cards = .loaded(try await client.cards())
        } catch {
            cards = .failed((error as? HSCCError)?.localizedDescription ?? "Something went wrong.")
        }
    }
}

/// Settings section shell — project-level settings.
///
/// The navigation shell is established here; a follow-up card fills in the
/// actual project-level configuration. Keep a clean seam so depth can land
/// without restructuring this screen.
struct ProjectSettingsView: View {
    let name: String

    var body: some View {
        List {
            Section {
                Label("Project settings", systemImage: "gearshape.2")
                    .font(.headline)
                Text("Configuration for \(name) lives here — project-scoped orchestration, notification, and board options. Depth lands in a follow-up card; the shell is ready.")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}
