import XCTest

// MARK: - Drawer navigation UI tests (t_5ed5d281)
//
// The app's FIRST XCUITest target (operator directive 2026-10-15: "real taps,
// no human"). Scope stays narrow — the full navigation graph is t_810ab152.
// These tests pin the operator field-report walk (v2.9.0, iPhone):
//
//   opening the drawer (the "HSCC" page) leaves NO way back — no close, no
//   back, swipe doesn't dismiss, and tapping a row (even the already-selected
//   Home row) does not leave the drawer.
//
// Run on a COMPACT size class (iPhone 16 sim); iPad/regular proves nothing
// here. Launches use HSCC_DEBUG_FIXTURES=1 (canned-fixture URLProtocol — the
// tests touch NO live API) plus the HSCC_DEBUG_OPEN_DRAWER=1 seam, which
// starts the app in the exact drawer-open presentation the operator got
// stuck in, so the walk starts deterministically without a human tap.
//
// Markers are AX-stable and mutually exclusive (verified against the live
// AX tree, iPhone 16 / iOS 26.3):
//   drawer open   -> staticTexts["Work"] + buttons["drawerClose"]
//   Home on screen-> buttons["Search everywhere"] + staticTexts["Not connected"]
//                    (fixture mode has no token -> the hero says exactly this)
//   Chats detail  -> buttons["Refresh chats"] (ChatsRootView's own toolbar)
// Drawer rows are tapped by their STABLE identifiers (the AppDestination
// rawValues the sidebar rows now expose: "home", "chats", ...): while the
// drawer is up the screen behind it still shows its own nav title (e.g.
// "Home"), so label queries are ambiguous by construction. The drawer row
// label "Chats" is unique anyway: Home's dock says "Chat" (no s).

final class DrawerNavigationUITests: XCTestCase {

    private func launch(openDrawer: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["HSCC_DEBUG_FIXTURES"] = "1"
        if openDrawer {
            app.launchEnvironment["HSCC_DEBUG_OPEN_DRAWER"] = "1"
        }
        app.launch()
        return app
    }

    private func assertDrawer(_ app: XCUIApplication, open: Bool,
                              file: StaticString = #filePath, line: UInt = #line) {
        let visible = app.staticTexts["Work"].waitForExistence(timeout: 5)
        XCTAssertEqual(visible, open,
                       open ? "drawer (Work/Cluster lists) never appeared on screen"
                            : "drawer is still on screen",
                       file: file, line: line)
    }

    private func assertHome(_ app: XCUIApplication,
                            file: StaticString = #filePath, line: UInt = #line) {
        // Home's unique toolbar button; its presence is the "back to Home"
        // proof that does not depend on the time-of-day greeting string.
        XCTAssertTrue(app.buttons["Search everywhere"].waitForExistence(timeout: 5),
                      "Home screen (search button + hero) is not on screen",
                      file: file, line: line)
        XCTAssertTrue(app.staticTexts["Not connected"].exists,
                      "Home hero is missing", file: file, line: line)
    }

    /// Tap a drawer row by its UNIQUE identifier (each sidebar row labels
    /// its text "drawerRow:<rawValue>"). A bare label query is ambiguous
    /// while the drawer is up — the screen behind still renders its own nav
    /// title (e.g. "Home"), which the first GREEN run proved — and a wrong
    /// pick (tapping the background title) would silently NOT leave the
    /// drawer: the bug class this suite pins.
    private func tapRow(_ app: XCUIApplication, rawValue: String, file: StaticString = #filePath, line: UInt = #line) {
        let row = app.staticTexts["drawerRow:\(rawValue)"]
        XCTAssertTrue(row.waitForExistence(timeout: 5),
                      "drawer row \(rawValue) not on screen", file: file, line: line)
        row.tap()
    }

    /// RED today (2.9.0): the drawer-open launch never reaches the drawer,
    /// and even when a user gets there by hand (toggle/swipe) no `drawerClose`
    /// exists and tapping the ALREADY-SELECTED Home row is a no-op — the
    /// operator's dead end.
    func testOpeningDrawerThenTappingHomeReturnsToHome() {
        let app = launch(openDrawer: true)
        defer { app.terminate() }

        assertDrawer(app, open: true)
        XCTAssertTrue(app.buttons["drawerClose"].exists,
                      "the drawer ships no close/dismiss affordance")

        // THE dead-end case: Home is already the current selection, the row
        // re-tap writes the SAME tag — the compact stack must still leave
        // the drawer.
        tapRow(app, rawValue: "home")

        assertDrawer(app, open: false)
        assertHome(app)
    }

    /// RED today: same-row activation is inert AND the drawer never opens
    /// from Home through any app-owned affordance (no `drawerOpen` exists).
    func testSameRowReActivationFromHomeLeavesDrawer() {
        let app = launch(openDrawer: false)
        defer { app.terminate() }
        assertHome(app)

        // Home -> drawer through the app's own top-level affordance.
        app.buttons["drawerOpen"].tap()
        assertDrawer(app, open: true)

        // Drawer -> Home row (same selection) -> drawer must be left.
        tapRow(app, rawValue: "home")
        assertDrawer(app, open: false)
        assertHome(app)

        // ...and again: opening + leaving must repeat, no edge-swipe luck.
        app.buttons["drawerOpen"].tap()
        assertDrawer(app, open: true)
        app.buttons["drawerClose"].tap()
        assertDrawer(app, open: false)
        assertHome(app)
    }

    /// RED today: a DIFFERENT row from the drawer (Chats) does not show its
    /// detail either — the AX dump (2026-10-15) showed the drawer unchanged
    /// after tapping Chats — and there is no way back to the drawer except
    /// the system toggle (which itself disappears once the drawer is open).
    func testDifferentRowFromDrawerShowsDetailAndMenuReopensDrawer() {
        let app = launch(openDrawer: true)
        defer { app.terminate() }
        assertDrawer(app, open: true)

        tapRow(app, rawValue: "chats")
        assertDrawer(app, open: false)
        XCTAssertTrue(app.buttons["Refresh chats"].waitForExistence(timeout: 5),
                      "Chats detail never appeared after tapping the drawer row")

        // Detail -> drawer again through the same affordance a user takes.
        app.buttons["drawerOpen"].tap()
        assertDrawer(app, open: true)

        // Drawer -> Home without choosing anything else.
        tapRow(app, rawValue: "home")
        assertHome(app)
    }

    /// RED today: `drawerClose` does not exist; the drawer cannot be left
    /// without choosing a row (the operator's "drawer -> back without
    /// choosing" path).
    func testCloseButtonLeavesDrawerWithoutChoosingARow() {
        let app = launch(openDrawer: true)
        defer { app.terminate() }
        assertDrawer(app, open: true)

        app.buttons["drawerClose"].tap()

        assertDrawer(app, open: false)
        assertHome(app)
    }
}
