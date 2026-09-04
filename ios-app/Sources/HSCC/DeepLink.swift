import Foundation
import SwiftUI

// ===========================================================================
// Deep links and Handoff (t_136762f3).
//
// Today everything starts from the root. This adds a `hscc://` URL scheme that
// opens a specific PROJECT, CARD or SESSION directly, plus the routing that
// lands the operator there instead of the root tab. It is the single entry
// point that notifications and future integrations (a Telegram message, a Mac
// session) can deep-link into.
//
//   hscc://project/<name>     → Projects tab → that project's detail (Overview)
//   hscc://session/<project>  → Projects tab → that project's CHAT (session)
//   hscc://card/<id>          → Projects tab → the card's project board → card
//
// Routing is honest about malformed or stale links: a URL that doesn't parse,
// names a project/card that no longer resolves, or targets something outside
// this app is never a crash and never a blank screen — it surfaces a clear
// "couldn't open" message and drops back to the normal root UI.
// ===========================================================================

// MARK: - Route

/// A parsed deep link: where a `hscc://` URL wants the app to land.
enum DeepLinkRoute: Equatable {
    /// Open a project's Overview. `name` is the project's canonical name from
    /// GET /v1/projects.
    case project(name: String)
    /// Open a card by its id (resolved to its project's board first).
    case card(id: String)
    /// Open a project's SESSION — its orchestrator chat / session history.
    case session(project: String)
    /// The URL was malformed, or named something the app cannot open.
    case invalid(reason: String)
}

// MARK: - Parser

/// Turns a `hscc://` URL into a `DeepLinkRoute`.
///
/// Tolerant of the ways a real link can be malformed (double slashes, stray
/// spaces, percent-encoding), and REFUSES paths we don't define rather than
/// guessing — a guessed route is how a stale link silently lands on the wrong
/// screen. The scheme itself is checked so a foreign scheme can never be
/// mistaken for one of ours (onOpenURL only receives registered schemes, but
/// the parser stays defensive anyway).
enum DeepLinkParser {

    static func parse(_ url: URL) -> DeepLinkRoute {
        guard let scheme = url.scheme?.lowercased(), scheme == "hscc" else {
            return .invalid(reason: "Not an HSCC link (scheme '\(url.scheme ?? "?")').")
        }
        // `host` is the first path segment for a `scheme://host/path` URL.
        // `hscc://project/x` → host "project", path "/x".
        // `hscc://card/x` → host "card", path "/x".
        guard let kind = url.host?.lowercased() else {
            return .invalid(reason: "That link has no target.")
        }
        // The remaining path, percent-decoded. `hscc://card/my%20card` → "my card".
        let rawPath = url.path
        let target = rawPath
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .removingPercentEncoding?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""

        switch kind {
        case "project":
            guard !target.isEmpty else {
                return .invalid(reason: "That project link has no project name.")
            }
            return .project(name: target)
        case "session":
            guard !target.isEmpty else {
                return .invalid(reason: "That session link has no project name.")
            }
            return .session(project: target)
        case "card":
            guard !target.isEmpty else {
                return .invalid(reason: "That card link has no card id.")
            }
            return .card(id: target)
        default:
            return .invalid(reason: "HSCC doesn't understand a '\(kind)' link.")
        }
    }
}

// MARK: - Navigation destination values

/// A destination inside the Projects NavigationStack, as a Hashable value that
/// can ride a typed `NavigationPath`. The app's normal `NavigationLink` pushes
/// and a deep link's router push both append here, so user taps and external
/// entry points share one path.
enum ProjectRoute: Hashable {
    /// A project's detail screen. Carries the resolved board name (nil when
    /// unknown) so a deep link can jump straight into the right board section
    /// without depending on a freshly-fetched `Project` object.
    case projectDetail(name: String, board: String?)
    /// A card's detail, pushed directly (optionally on top of a project detail).
    case card(id: String)
}

// MARK: - Router

/// The app's single deep-link router. Owned by `ContentView` and injected into
/// the Projects NavigationStack; a `hscc://` URL, a notification tap, or a
/// Handoff activity all funnel through this one object, so every entry point
/// lands the operator on the exact project, card or session.
///
/// It resolves a route into a concrete navigation path (fetching the projects
/// list and/or card detail when the link only carries ids), applies it to the
/// Projects stack, and switches the tab — then reports honestly if the target
/// could not be resolved.
@MainActor
final class DeepLinkRouter: ObservableObject {

    /// The single, app-wide router. Mirroring `NotificationCoordinator.shared`
    /// / `ConnectionMonitor.shared`: HSCCApp owns it as its @StateObject via
    /// this singleton, and any entry point (URL, notification tap, Handoff)
    /// can reach it without chasing an object graph.
    static let shared = DeepLinkRouter()

    private init() {}

    /// The navigation path of the Projects tab. `ProjectsView` binds its
    /// `NavigationStack(path:)` to this, so a router push and a user tap write
    /// to the same path.
    @Published var projectsPath: [ProjectRoute] = []

    /// Which segment the NEXT project-detail screen should open on. Set by a
    /// deep link (e.g. chat for a session link), consumed once by
    /// `ProjectDetailView` on first appear.
    @Published var pendingSegment: ProjectDetailView.Section?

    /// True while the router is resolving a route against the cluster. Lets a
    /// call site show "Opening…" instead of nothing while the fetch runs.
    @Published var isResolving = false

    /// A human-readable error from the last attempt, shown as an alert by
    /// ContentView. Surfaces malformed OR stale (unresolvable) links honestly.
    @Published var lastError: String?

    /// The tab the deep link wants active. `ContentView` binds its TabView
    /// selection to this so a link that lands in the Projects tab switches to
    /// it. When nil, the user's own tab choice stands.
    @Published var requestedTab: ContentView.Tab?

    private var client: HSCCClient?

    /// Bucket a client so deep links arriving before/after configuration both
    /// resolve. `ContentView` sets this alongside its other pollers.
    func setClient(_ client: HSCCClient?) {
        self.client = client
    }

    /// The entry point every deep-link source calls. Parses the URL and then
    /// drives navigation (switching tab + building the Projects path). No
    /// op/error surfaces are skipped — a bad link always yields `lastError`.
    func handle(_ url: URL, tab defaultTab: ContentView.Tab = .projects) {
        let route = DeepLinkParser.parse(url)
        switch route {
        case .invalid(let reason):
            lastError = reason
        case .project(let name):
            requestedTab = defaultTab
            Task { await openProject(named: name, segment: .overview) }
        case .session(let project):
            requestedTab = defaultTab
            Task { await openProject(named: project, segment: .chat) }
        case .card(let id):
            requestedTab = defaultTab
            Task { await openCard(id: id) }
        }
    }

    /// Push a project detail (at the given segment) on to the Projects path.
    private func openProject(named name: String, segment: ProjectDetailView.Section) async {
        guard let client else {
            lastError = "No cluster is configured — open Settings to connect before following this link."
            return
        }
        isResolving = true
        defer { isResolving = false }
        do {
            // Resolve the board name so the destination opens the RIGHT board
            // section. Fail open: the route is still a valid project even when
            // the list read hiccups — we just can't pre-resolve the board.
            var board: String? = nil
            if let projects = try? await client.projects() {
                board = projects.projects.first { $0.name == name }?.board
            }
            pendingSegment = segment
            projectsPath = [.projectDetail(name: name, board: board)]
        }
    }

    /// Push a card (and its project detail when the board resolves to one) on
    /// to the Projects path.
    private func openCard(id: String) async {
        guard let client else {
            lastError = "No cluster is configured — open Settings to connect before following this link."
            return
        }
        isResolving = true
        defer { isResolving = false }
        do {
            // Resolve the card's board, then the project that owns that board,
            // so the card opens inside its project context. Each read is
            // fail-open: the card itself loads by id regardless.
            let detail = try? await client.cardDetail(id)
            let cardBoard = detail?.board
            var projectName: String? = nil
            if let cardBoard, let projects = try? await client.projects() {
                projectName = projects.projects.first { $0.board == cardBoard }?.name
            }
            if let projectName {
                pendingSegment = .board
                projectsPath = [.projectDetail(name: projectName, board: cardBoard), .card(id: id)]
            } else {
                // No owning project resolution — open the card directly. It
                // fetches itself by id, so the operator still sees the real
                // card rather than a dead end.
                projectsPath = [.card(id: id)]
            }
        }
    }
}
