//
//  NavGraphScreenTable.swift — GENERATED. DO NOT EDIT BY HAND.
//
//  Regenerate (and commit) with:  ios-app/scripts/nav_graph_screens.py emit
//  Gate check (fails on drift / missing coverage):  scripts/nav_graph_check.sh
//
//  Source of truth: AppShell.swift (AppDestination + normalized +
//  projectRouteView), Sidebar.swift (drawer rows) and the curated
//  `expect(...)` blocks in NavGraphUITests.swift — parsed by
//  scripts/nav_graph_screens.py. Adding a drawer row without giving it
//  a walk expectation breaks the gate BEFORE the walker even runs,
//  which is the point: a new screen cannot dodge the walk.
//
//  Generated 2026-10-16 from the code as committed.

import Foundation

/// One drawer screen's walk plan. Every field is a stable AX/fixture
/// marker verified against the running app by the walker itself.
struct NavScreenExpectation {
    let raw: String               // AppDestination rawValue == `drawerRow:<raw>` id
    let appTitle: String          // AppDestination.title (drawer row label)
    let titleMarker: String       // unique control/label proving the screen landed
    let emptyMarker: String?      // honest empty-state text (zero-row fixture set)
    let errMarker: String?        // honest failed-row text (fixture failure mode)
    let ptrPath: String?          // feed path whose served-count is the PTR receipt
}

enum NavGraphScreenTable {
    /// Drawer screens in Sidebar order — the exact order the walker taps them.
    static let screens: [NavScreenExpectation] = [
        NavScreenExpectation(raw: "home", appTitle: "Home", titleMarker: "Search everywhere", emptyMarker: nil, errMarker: nil, ptrPath: nil),
        NavScreenExpectation(raw: "chats", appTitle: "Chats", titleMarker: "Refresh chats", emptyMarker: "No projects", errMarker: "Couldn't load projects", ptrPath: "/v1/projects"),
        NavScreenExpectation(raw: "projects", appTitle: "Projects", titleMarker: "Refresh projects", emptyMarker: "No projects registered.", errMarker: "Couldn't load projects", ptrPath: "/v1/projects"),
        NavScreenExpectation(raw: "cards", appTitle: "Cards", titleMarker: "Refresh cards", emptyMarker: "No open cards", errMarker: "Couldn't load cards", ptrPath: "/v1/cards"),
        NavScreenExpectation(raw: "approvals", appTitle: "Approvals", titleMarker: "Pending approvals", emptyMarker: "No pending approvals", errMarker: "Debug fixture failure", ptrPath: "/v1/kanban/blocked"),
        NavScreenExpectation(raw: "fleet", appTitle: "Fleet", titleMarker: "Health", emptyMarker: nil, errMarker: "Debug fixture failure", ptrPath: "/v1/fleet/stats"),
        NavScreenExpectation(raw: "nodesServing", appTitle: "Nodes & serving", titleMarker: "Serving units", emptyMarker: "No serving units", errMarker: "Debug fixture failure", ptrPath: "/v1/cluster/status"),
        NavScreenExpectation(raw: "opsHealth", appTitle: "Ops & health", titleMarker: "Verify", emptyMarker: nil, errMarker: "Debug fixture failure", ptrPath: "/v1/verify"),
        NavScreenExpectation(raw: "autodown", appTitle: "Autodown & self-heal", titleMarker: "Status", emptyMarker: "No automation history", errMarker: "Debug fixture failure", ptrPath: "/v1/autodown/status"),
        NavScreenExpectation(raw: "cron", appTitle: "Cron", titleMarker: "Scheduled jobs", emptyMarker: "Idle", errMarker: "Couldn't load the roster", ptrPath: "/v1/cron/list"),
        NavScreenExpectation(raw: "logs", appTitle: "Logs", titleMarker: "Log source", emptyMarker: "Clean tail", errMarker: "Debug fixture failure", ptrPath: "/v1/logs"),
        NavScreenExpectation(raw: "memory", appTitle: "Memory", titleMarker: "Memories", emptyMarker: "Nothing here", errMarker: "Debug fixture failure", ptrPath: "/v1/memory"),
        NavScreenExpectation(raw: "templates", appTitle: "Templates", titleMarker: "Applied template", emptyMarker: "No templates", errMarker: "Debug fixture failure", ptrPath: "/v1/template/list"),
        NavScreenExpectation(raw: "sessions", appTitle: "Sessions", titleMarker: "Sessions", emptyMarker: "Nothing here", errMarker: "Debug fixture failure", ptrPath: "/v1/sessions"),
        NavScreenExpectation(raw: "activity", appTitle: "Activity feed", titleMarker: "Latest", emptyMarker: "Idle", errMarker: "Couldn't load the feed", ptrPath: "/v1/activity/feed"),
        NavScreenExpectation(raw: "search", appTitle: "Search", titleMarker: "Search", emptyMarker: nil, errMarker: nil, ptrPath: nil),
        NavScreenExpectation(raw: "settings", appTitle: "Settings", titleMarker: "Settings", emptyMarker: nil, errMarker: nil, ptrPath: nil),
    ]

    /// Drawer row rawValues in Sidebar order — the walker iterates
    /// THIS (the code-derived list), never a hand-kept one.
    static var drawerRows: [String] { screens.map(\.raw) }

    /// ProjectRoute pushes registered by AppShell.projectRouteView — the
    /// detail walks must cover each name here (the walker's
    /// testProjectRouteRegistrationCoverage asserts this set verbatim).
    static let projectRoutes: [String] = [
        "board",
        "card",
        "chat",
        "projectDetail",
        "projectSettings",
    ]
}
