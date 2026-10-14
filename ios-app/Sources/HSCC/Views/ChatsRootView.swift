import SwiftUI

/// Chats — the drawer/dock's Chat destination (IOS_REDESIGN 2026-10-14 §3.2).
///
/// The dock's first icon is "talk to the REPL", but a chat is per-project:
/// the reviewer note on the shell commit asked for a real global Chat
/// destination rather than a dock icon that silently opened Projects. This is
/// it — the projects list narrowed to its one verb: every row pushes straight
/// into that project's full-bleed chat (no Overview hop), with the waiting
/// unread count on the row so the operator picks the right conversation first.
///
/// Data is the same GET /v1/projects + ProjectUnreadCenter the Projects screen
/// uses (one source of truth for what is unread), so the two lists can never
/// disagree about what is waiting.
struct ChatsRootView: View {
    let client: HSCCClient?

    @EnvironmentObject private var unread: ProjectUnreadCenter
    @EnvironmentObject private var router: DeepLinkRouter

    @State private var projects = LoadState<ProjectsResponse>.idle

    var body: some View {
        Group {
            // Below the nav bar, inside content (t_4889e978 placement rule).
            ConnectionBanner()
            if let client {
                content(client)
            } else {
                HSConnectGate(systemImage: "bubble.left.and.bubble.right",
                              verb: "to open a chat")
            }
        }
        .navigationTitle("Chats")
        .task {
            if client != nil, projects.value == nil, !projects.isLoading {
                await load()
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    Task { await load() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .disabled(projects.isLoading)
                .accessibilityLabel("Refresh chats")
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
        case .stale(let response, let ageMessage):
            list(response, staleMessage: ageMessage)
        case .loaded(let response):
            list(response, staleMessage: nil)
        }
    }

    /// One row per project; tapping pushes THAT project's chat directly on
    /// the shell's stack (spec §3.2: Chat leads the dock because the operator
    /// lives there — so the dock path is one tap shorter than via Projects).
    @ViewBuilder
    private func list(_ response: ProjectsResponse, staleMessage: String?) -> some View {
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load() }
                    }
                }
            }
            if response.projects.isEmpty {
                Section {
                    HSEmpty("No projects",
                            message: "Projects registered on the cluster appear here as chats.",
                            systemImage: "bubble.left.and.bubble.right")
                }
            }
            ForEach(response.projects) { project in
                Button {
                    router.projectsPath.append(.chat(name: project.name))
                } label: {
                    chatRow(project)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Open chat with \(project.name)")
            }
        }
        .refreshable { await load() }
    }

    @ViewBuilder
    private func chatRow(_ project: Project) -> some View {
        HStack(spacing: Theme.Spacing.md.rawValue) {
            // The one dot on each row: does a reply sit UNREAD here. Muted
            // when nothing waits; the count is spelled out in text next to
            // it, so colour is never the only channel (spec §9).
            HSStatusDot(unread.count(for: project.name) > 0 ? Theme.Semantic.warn : Theme.Semantic.neutral)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(project.name)
                    .font(.body.weight(.medium))
                    .foregroundColor(Theme.Semantic.onSurface)
                HSMetaLine([project.board,
                            project.displayTopic.isEmpty ? nil : "topic \(project.displayTopic)"])
            }
            Spacer(minLength: Theme.Spacing.sm.rawValue)
            let count = unread.count(for: project.name)
            if count > 0 {
                HSStatusChip("\(count) unread", color: Theme.Semantic.warn)
            }
            Image(systemName: "chevron.right")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .contentShape(Rectangle())
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
