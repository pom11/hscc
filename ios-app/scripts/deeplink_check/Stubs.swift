// deeplink_check/Stubs.swift — minimal stand-ins for the iOS-only types the
// REAL DeepLink.swift references, so the router compiles into a headless macOS
// CLI. The router (Sources/HSCC/DeepLink.swift) is compiled VERBATIM, never
// redeclared here — same rule as connection_banner_check compiling the real
// ConnectionMonitor.swift. Only the surrounding app types are stubbed.
//
// What the router actually touches from the outside world:
//   * ContentView.Tab            (real: Sources/HSCC/ContentView.swift)
//   * ProjectDetailView.Section  (real: Sources/HSCC/Views/ProjectsView.swift)
//   * HSCCClient.projects() / cardDetail(_:)  (real: Sources/HSCC/HSCCClient.swift)
//
// The real HSCCClient does network I/O + StateCache persistence — nothing a
// deterministic harness wants. The stub keeps the exact call shape the router
// relies on and lets the harness swap the responses per scenario.

import Foundation

// MARK: ContentView.Tab (real: Sources/HSCC/ContentView.swift)

enum ContentView {
    enum Tab: Hashable {
        case projects, cluster, settings
    }
}

// MARK: ProjectDetailView.Section (real: Sources/HSCC/Views/ProjectsView.swift)

enum ProjectDetailView {
    enum Section {
        case overview, chat, board, settings
    }
}

// MARK: Response model shells — the router reads only `.projects[].name` and
// `.projects[].board` off the projects response, and `.board` off cardDetail,
// so the stub types need exactly those fields and nothing more.

struct Project {
    let name: String
    let board: String?
}

struct ProjectsResponse {
    let projects: [Project]
}

struct CardDetailResponse {
    let board: String?
}

// MARK: Stub HSCCClient

/// The router's only client surface is `projects()` and `cardDetail(_:)`, both
/// `async throws`. This stub exposes exactly those two calls with swap-in
/// handlers so each test scenario scripts a deterministic resolve / missing /
/// empty outcome. The REAL HSCCClient.swift is never compiled here — it does
/// HTTP + caching the harness does not want.
final class HSCCClient {
    var projectsHandler: () async throws -> ProjectsResponse
    var cardDetailHandler: (String) async throws -> CardDetailResponse

    init(
        projects: @escaping () async throws -> ProjectsResponse = { ProjectsResponse(projects: []) },
        cardDetail: @escaping (String) async throws -> CardDetailResponse = { _ in CardDetailResponse(board: nil) }
    ) {
        self.projectsHandler = projects
        self.cardDetailHandler = cardDetail
    }

    func projects() async throws -> ProjectsResponse { try await projectsHandler() }
    func cardDetail(_ id: String) async throws -> CardDetailResponse { try await cardDetailHandler(id) }
}
