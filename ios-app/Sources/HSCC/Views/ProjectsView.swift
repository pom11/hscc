import SwiftUI

/// Projects — the registry list (GET /v1/projects) with tap-through to a
/// per-project detail (GET /v1/projects/{name}).
///
/// Read-only: navigation + pull-to-refresh only. Follows the CardsView pattern
/// (settings-provided client, LoadState-style error handling).
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
                            .foregroundColor(.secondary)
                    }
                }
                ForEach(response.projects) { project in
                    NavigationLink {
                        ProjectDetailView(client: client, name: project.name)
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
            HStack(spacing: 6) {
                if let board = project.board, !board.isEmpty {
                    Text(board).font(.caption).foregroundColor(.secondary)
                }
                if !project.displayTopic.isEmpty {
                    Text("topic \(project.displayTopic)").font(.caption).foregroundColor(.secondary)
                }
            }
        }
    }

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "folder")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to see your projects.")
                .font(.subheadline)
                .foregroundColor(.secondary)
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

/// Projects — one project's detail (GET /v1/projects/{name}).
///
/// Shows board counts and git state. Read-only.
struct ProjectDetailView: View {
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
        .navigationTitle(detail.value?.name ?? name)
        .navigationBarTitleDisplayMode(.inline)
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
                    LabeledContent("Board") { Text(board) }
                }
                if let repo = state.repo, !repo.isEmpty {
                    LabeledContent("Repo") { Text(repo).lineLimit(1).truncationMode(.middle) }
                }
                if let counts = state.board_counts {
                    let sorted = counts
                        .filter { $0.key != "total" }
                        .sorted { $0.value > $1.value }
                    if !sorted.isEmpty {
                        let openTotal = counts["total"] ?? sorted.map(\.value).reduce(0, +)
                        LabeledContent("Open cards") { Text("\(openTotal)") }
                        ForEach(sorted, id: \.key) { status, count in
                            LabeledContent(status.capitalized) { Text("\(count)") }
                        }
                    }
                }
            }

            if let git = state.git, git.is_repo == true {
                Section("Git") {
                    if let branch = git.branch, !branch.isEmpty {
                        LabeledContent("Branch") { Text(branch) }
                    }
                    LabeledContent("Dirty") { Text(git.dirty == true ? "Yes" : "No") }
                    if let age = git.last_activity_seconds_ago {
                        LabeledContent("Last commit") { Text(timeAgo(age)) }
                    }
                    if let head = git.head, !head.isEmpty {
                        LabeledContent("Head") { Text(head.prefix(8)) }
                    }
                    if let uncommitted = git.uncommitted, !uncommitted.isEmpty {
                        LabeledContent("Uncommitted") {
                            Text("\(uncommitted.count) file\(uncommitted.count == 1 ? "" : "s")")
                        }
                    }
                }
            } else {
                Section("Git") {
                    Text("Not a git repo.")
                        .foregroundColor(.secondary)
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
