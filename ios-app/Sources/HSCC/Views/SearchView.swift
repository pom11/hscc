import SwiftUI

/// Cross-project search (offline-friendly).
///
/// One field to find anything across the fleet's dozen projects, instead of
/// hunting through each board. Searches over two sources drawn from
/// last-known-cached reads so it works even when the cluster is unreachable:
///
///   * **Projects** — matched on name, repo, and board.
///   * **Cards**     — matched on title, id, and status (across every board).
///
/// Results are grouped by kind and tap through to the existing detail screens
/// (`ProjectDetailView`, `CardDetailView`). An empty query shows the likely /
/// recent items (all known projects + a hint to type); no results says so
/// plainly and suggests what is searchable.
///
/// The whole screen rides the same `Offline.load` path as every other read
/// surface, so when the cluster is down it shows last-known data marked stale
/// rather than a blank "no data" lie.
struct SearchView: View {
    let client: HSCCClient?

    @State private var projects = LoadState<ProjectsResponse>.idle
    @State private var cards = LoadState<CardsResponse>.idle
    @State private var query = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Search")
                .navigationBarTitleDisplayMode(.inline)
                .navigationBarItems(leading: closeButton)
                .searchable(text: $query,
                            placement: .navigationBarDrawer(displayMode: .always),
                            prompt: "Projects, cards, boards…")
                .task { await load() }
        }
    }

    private var closeButton: some View {
        Button("Done") { dismiss() }
    }

    @ViewBuilder
    private var content: some View {
        if client == nil {
            notConfiguredView
        } else {
            results
        }
    }

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "magnifyingglass", verb: "to search across projects and boards")
    }

    // MARK: - Results

    @ViewBuilder
    private var results: some View {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)

        switch (projects, cards) {
        case (.failed(let m), _), (_, .failed(let m)):
            HSError("Couldn't search", message: m) {
                Task { await load() }
            }
        default:
            if trimmed.isEmpty {
                emptyQueryView
            } else if !hasLoaded {
                // The search sources are still resolving — don't claim
                // "No results" before we actually know. On a slow cluster they
                // land out-of-order; a premature no-results is the same "no
                // data" lie this screen exists to avoid (the two Offline.load
                // calls in `load()` run concurrently and set state in flight).
                searchingView
            } else {
                let (ps, cs) = matched(trimmed)
                if ps.isEmpty && cs.isEmpty {
                    noResultsView
                } else {
                    resultsList(projects: ps, cards: cs)
                }
            }
        }
    }

    /// True once both sources hold a value (live or cached), so `matched()`
    /// can be trusted to mean "really no results" rather than "still loading".
    private var hasLoaded: Bool {
        projects.value != nil && cards.value != nil
    }

    /// Shown while an in-flight query is still loading its two sources.
    @ViewBuilder
    private var searchingView: some View {
        List {
            if let stale = staleBanner() {
                Section { stale }
            }
            HStack(spacing: 10) {
                ProgressView()
                Text("Searching…")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Matched result rows for the current query.
    private func matched(_ q: String) -> ([Project], [Card]) {
        let needle = q.lowercased()
        let projectHits = (projects.value?.projects ?? []).filter { project in
            [project.name, project.repo ?? "", project.board ?? "", project.displayTopic]
                .joined(separator: " ")
                .lowercased()
                .contains(needle)
        }
        let cardHits = (cards.value?.cards ?? []).filter { card in
            [card.displayTitle, card.id, card.displayStatus, card.board ?? ""]
                .joined(separator: " ")
                .lowercased()
                .contains(needle)
        }
        return (projectHits, Array(cardHits.prefix(50)))
    }

    /// The slice of the screen rich with likely targets: all known projects,
    /// plus a hint. Shows regardless of the current load freshness — if it's
    /// cached and stale, the banner says so.
    @ViewBuilder
    private var emptyQueryView: some View {
        List {
            if let stale = staleBanner() {
                Section { stale }
            }
            Section {
                Label("Type to search across projects and cards.",
                      systemImage: "magnifyingglass")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            let ps = projects.value?.projects ?? []
            if !ps.isEmpty {
                Section("Likely items") {
                    ForEach(ps) { project in
                        NavigationLink {
                            ProjectDetailView(client: client!, project: project)
                        } label: {
                            projectRow(project)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var noResultsView: some View {
        List {
            if let stale = staleBanner() {
                Section { stale }
            }
            HSEmpty("No results for “\(query)”",
                     message: "Search project names, repos, boards, or card titles, ids, and statuses.",
                     systemImage: "magnifyingglass")
        }
    }

    @ViewBuilder
    private func resultsList(projects ps: [Project], cards cs: [Card]) -> some View {
        List {
            if let stale = staleBanner() {
                Section { stale }
            }
            if !ps.isEmpty {
                Section("Projects — \(ps.count)") {
                    ForEach(ps) { project in
                        NavigationLink {
                            ProjectDetailView(client: client!, project: project)
                        } label: {
                            projectRow(project)
                        }
                    }
                }
            }
            if !cs.isEmpty {
                Section("Cards — \(cs.count)") {
                    ForEach(cs) { card in
                        NavigationLink {
                            CardDetailView(cardID: card.id)
                        } label: {
                            cardRow(card)
                        }
                    }
                }
            }
        }
    }

    /// A stale banner when either source is showing cached last-known data.
    /// Nil when both sources are live (or never fetched).
    private func staleBanner() -> StaleBanner? {
        let msg = projects.staleMessage ?? cards.staleMessage
        guard let msg else { return nil }
        return StaleBanner(age: msg, reason: "Can't reach the cluster right now.") {
            Task { await load() }
        }
    }

    // MARK: - Rows

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
                if let repo = project.repo, !repo.isEmpty {
                    Text(repo).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .lineLimit(1).truncationMode(.middle)
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
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(card.displayTitle)
                    .foregroundColor(Theme.Semantic.onSurface)
                HSMetaLine([card.board, card.status])
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

    // MARK: - Load

    private func load() async {
        guard let client else { return }
        async let p: Void = loadProjects(client)
        async let c: Void = loadCards(client)
        _ = await (p, c)
    }

    private func loadProjects(_ client: HSCCClient) async {
        projects = await Offline.load(projects,
                                      cacheKey: EndpointPath.projects,
                                      client: client) {
            try await client.projects()
        }
    }

    private func loadCards(_ client: HSCCClient) async {
        cards = await Offline.load(cards,
                                   cacheKey: EndpointPath.cards,
                                   client: client) {
            try await client.cards()
        }
    }
}
