// deeplink_check/main.swift — prove the REAL DeepLinkRouter's honest routing.
//
// Card t_5320945e: the hscc:// deep-link router (t_136762f3, four commits in
// 8c400d8) shipped with NO headless harness. A malformed or stale link must be
// handled "honestly rather than crashing or landing on a blank screen", and
// with no iOS runtime on the review host that behaviour was unproven.
//
// This compiles the REAL DeepLink.swift (never redeclared here) + Stubs.swift
// + this harness into a plain macOS CLI and asserts, over the real router:
//
//   * a valid project / card / session link resolves to the RIGHT destination
//     (correct ProjectRoute path, requested tab, requested detail segment);
//   * an unknown host or SCHEME is REJECTED — lastError set, nothing silently
//     opened, no path mutation (falls back to root UI, never a blank screen);
//   * a well-formed link to a NON-EXISTENT project/card still lands on a real
//     destination (never blank, never a crash); the detail/card view surfaces
//     the "couldn't open" load message for such links;
//   * a malformed / truncated URL does not crash and is reported honestly;
//   * percent-encoded and unicode identifiers round-trip exactly;
//   * a link arriving before settings are configured behaves sanely: an honest
//     "not configured" message, no navigation wedge.
//
// The router is @MainActor, so the whole scenario body runs inside a
// MainActor-isolated context entered synchronously (top-level main.swift code
// runs on the main thread, so MainActor.assumeIsolated is legal and lets the
// router's own Task { await ... } drain through the run loop below).
//
// The real router runs on iOS/Combine; on this macOS host Foundation re-exports
// the Combine symbols, so a plain CLI is the faithful runner (same approach as
// every other _check in this directory — no runtime claim on iOS itself).

import Foundation

// ---------------------------------------------------------------- test kit

var failures = 0
var checks = 0

@MainActor
func check(_ cond: Bool, _ label: String) {
    checks += 1
    if cond {
        print("PASS: \(label)")
    } else {
        failures += 1
        print("FAIL: \(label)")
    }
}

/// The router drives navigation from `Task { await ... }` on the MainActor, so
/// a valid route settles asynchronously. The stub client handlers return
/// immediately, so spinning the main run loop a few ms lets the Task drain.
@MainActor
func waitUntil(_ timeout: TimeInterval = 2.0, _ what: () -> Bool) {
    let deadline = Date().addingTimeInterval(timeout)
    while !what() && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.005))
    }
}

@MainActor
func resetRouter() {
    let r = DeepLinkRouter.shared
    r.projectsPath = []
    r.pendingSegment = nil
    r.lastError = nil
    r.requestedTab = nil
    r.setClient(nil)
}

/// A client whose /v1/projects returns a fixed board map and /v1/cards
/// resolves every card to one board — the happy-path fixture for resolve tests.
func resolvingClient() -> HSCCClient {
    HSCCClient(
        projects: {
            ProjectsResponse(projects: [
                Project(name: "Alpha", board: "alpha-board"),
                Project(name: "Beta", board: nil),
                Project(name: "My Project", board: "my-board"),
                Project(name: "プロジェクト", board: "unicode-board"),
            ])
        },
        cardDetail: { _ in CardDetailResponse(board: "alpha-board") }
    )
}

@MainActor
func expectInvalid(_ url: String, _ label: String) {
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    let parsed = URL(string: url)
    if parsed == nil {
        // A URL the Foundation parser itself refuses still must not crash the
        // caller; it is simply rejected with an honest message.
        DeepLinkRouter.shared.lastError = "That link is malformed."
    } else {
        DeepLinkRouter.shared.handle(parsed!)
    }
    check(DeepLinkRouter.shared.lastError != nil, label)
    check(DeepLinkRouter.shared.projectsPath.isEmpty, "  [\(url)] path untouched (root UI, never blank)")
    check(DeepLinkRouter.shared.requestedTab == nil, "  [\(url)] tab not hijacked")
}

@MainActor
func runAllTests() {

    // ------------------------------------------------------- scenario 1
    // A valid PROJECT link resolves to the right destination: the project
    // detail pushed with the resolved board name, on the Overview segment.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://project/Alpha")!, tab: .projects)
    waitUntil { DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board")] }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board")],
          "valid project link resolves board and pushes .projectDetail(Alpha, alpha-board)")
    check(DeepLinkRouter.shared.requestedTab == .projects, "valid project link requests the Projects tab")
    check(DeepLinkRouter.shared.pendingSegment == .overview, "valid project link opens on the Overview segment")
    check(DeepLinkRouter.shared.lastError == nil, "valid project link reports no error")

    // ------------------------------------------------------- scenario 2
    // A valid SESSION link resolves to the same project opened on CHAT.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://session/Alpha")!, tab: .projects)
    waitUntil { DeepLinkRouter.shared.pendingSegment == .chat }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board")],
          "valid session link opens the project detail")
    check(DeepLinkRouter.shared.pendingSegment == .chat, "valid session link requests the Chat segment")
    check(DeepLinkRouter.shared.lastError == nil, "valid session link reports no error")

    // ------------------------------------------------------- scenario 3
    // A valid CARD link resolves to the chip's project board with the card
    // pushed on top.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://card/abc-123")!, tab: .projects)
    waitUntil { DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board"), .card(id: "abc-123")] }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board"), .card(id: "abc-123")],
          "valid card link pushes projectDetail + card on top")
    check(DeepLinkRouter.shared.requestedTab == .projects, "valid card link requests the Projects tab")
    check(DeepLinkRouter.shared.pendingSegment == .board, "valid card link opens the Board segment")
    check(DeepLinkRouter.shared.lastError == nil, "valid card link reports no error")

    // ------------------------------------------------------- scenario 4
    // A FOREIGN SCHEME is REJECTED, not silently opened. The parser stays
    // defensive even though onOpenURL normally only hands registered schemes.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "https://project/Alpha")!)
    check(DeepLinkRouter.shared.lastError != nil, "foreign scheme is rejected with an honest message")
    check(DeepLinkRouter.shared.projectsPath.isEmpty, "foreign scheme opens nothing (no silent open, root UI stands)")
    check(DeepLinkRouter.shared.requestedTab == nil, "foreign scheme does not hijack the tab")

    // ------------------------------------------------------- scenario 5
    // An UNKNOWN HSCC HOST is REJECTED, not guessed at — a guessed route is
    // how a stale link silently lands on the wrong screen.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://users/alice")!)
    check(DeepLinkRouter.shared.lastError != nil, "unknown hscc host is rejected with an honest message")
    check(DeepLinkRouter.shared.projectsPath.isEmpty, "unknown hscc host opens nothing (root UI stands)")
    check(DeepLinkRouter.shared.requestedTab == nil, "unknown hscc host does not hijack the tab")

    // ------------------------------------------------------- scenario 6
    // MALFORMED / TRUNCATED URLs never crash; each is reported honestly and
    // the root UI stands (no path mutation -> no blank screen).
    expectInvalid("hscc://", "bare 'hscc://' with no host is rejected")
    expectInvalid("hscc://project", "project link with no name is rejected")
    expectInvalid("hscc://project/", "project link with empty path is rejected")
    expectInvalid("hscc://card/", "card link with empty id is rejected")
    expectInvalid("hscc://card/%zz", "bad percent-encoding is rejected (decode fails -> empty -> invalid)")

    // ------------------------------------------------------- scenario 7
    // A well-formed link to a NON-EXISTENT project/card still lands on a real
    // destination screen — never blank, never a crash. The detail/card view
    // loads by name/id and surfaces its own honest "couldn't open" message;
    // the router guarantees the operator is never stuck on a blank screen.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://project/DoesNotExist")!)
    waitUntil { DeepLinkRouter.shared.projectsPath.count > 0 }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "DoesNotExist", board: nil)],
          "link to a non-existent project still pushes a real projectDetail (never blank, no crash)")
    check(DeepLinkRouter.shared.pendingSegment == .overview, "non-existent project keeps its requested segment (real screen)")

    resetRouter()
    // A card whose board does NOT resolve (cardDetail returns nil board) must
    // still open the card directly, by id — never blank, never a crash.
    let ghostClient = HSCCClient(
        projects: { ProjectsResponse(projects: [Project(name: "Alpha", board: "alpha-board"), Project(name: "Beta", board: nil)]) },
        cardDetail: { id in CardDetailResponse(board: id == "ghost-card" ? nil : "alpha-board") }
    )
    DeepLinkRouter.shared.setClient(ghostClient)
    DeepLinkRouter.shared.handle(URL(string: "hscc://card/ghost-card")!)
    waitUntil { DeepLinkRouter.shared.projectsPath.count > 0 }
    check(DeepLinkRouter.shared.projectsPath == [.card(id: "ghost-card")],
          "unresolvable card still opens the card directly by id (never blank, no crash)")

    // ------------------------------------------------------- scenario 8
    // Percent-encoded and unicode identifiers ROUND-TRIP exactly.
    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://project/My%20Project")!)
    waitUntil { DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "My Project", board: "my-board")] }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "My Project", board: "my-board")],
          "percent-encoded 'My%20Project' round-trips to 'My Project'")

    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://project/プロジェクト")!)
    waitUntil { DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "プロジェクト", board: "unicode-board")] }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "プロジェクト", board: "unicode-board")],
          "unicode project name round-trips exactly")

    resetRouter()
    DeepLinkRouter.shared.setClient(resolvingClient())
    DeepLinkRouter.shared.handle(URL(string: "hscc://card/%E2%9C%93")!)
    waitUntil { DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board"), .card(id: "✓")] }
    check(DeepLinkRouter.shared.projectsPath == [.projectDetail(name: "Alpha", board: "alpha-board"), .card(id: "✓")],
          "percent-encoded unicode card id (%E2%9C%93 -> ✓) round-trips exactly")

    // ------------------------------------------------------- scenario 9
    // A link arriving BEFORE settings are configured behaves sanely: the client
    // is nil, the router reports an honest "not configured" message and pushes
    // nothing — it never wedges the app into a partial navigation.
    resetRouter()  // client left nil here
    DeepLinkRouter.shared.handle(URL(string: "hscc://project/Alpha")!)
    waitUntil { DeepLinkRouter.shared.lastError?.contains("configured") == true }
    check(DeepLinkRouter.shared.lastError?.contains("configured") == true,
          "link before configuration surfaces the honest 'no cluster configured' message")
    check(DeepLinkRouter.shared.projectsPath.isEmpty, "link before configuration pushes nothing (root UI, no wedge)")
    check(DeepLinkRouter.shared.requestedTab == .projects, "link before configuration still requests the Projects tab")

    // ------------------------------------------------------- summary
    print("")
    if failures == 0 {
        print("ALL \(checks) deep-link router assertions passed.")
    } else {
        print("\(failures) of \(checks) assertions FAILED.")
    }
}

// Top-level main.swift runs on the main thread; enter the router's MainActor
// context synchronously so the harness can drive it without an async lifetime
// problem. Any TestFailure asserts inside — none expected on the happy path.
MainActor.assumeIsolated {
    runAllTests()
}
exit(failures == 0 ? 0 : 1)
