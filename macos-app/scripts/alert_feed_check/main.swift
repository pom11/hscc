import Foundation

// ===========================================================================
// alert_feed_check — the macOS alert path's decisions, EXECUTED.
//
// The card's gate is compile-only, but a menu bar app that compiles tells you
// nothing about the two properties that actually matter:
//
//   * a first launch must NOT replay the feed as notifications, and
//   * `since` must advance by `next_since`, never by `latest_id` on a full page
//     (doing that silently skips every alert the page did not include).
//
// This harness compiles the SHIPPED `AlertFeed.swift` (plus the real shared
// `WidgetDataClient` wire page it decodes) and drives the same policy the
// monitor calls. No network, no App Group, no notifications: the notification
// posting is the monitor's one untested-by-this-gate line, and the build gate
// compiles it.
//
// Usage: alert_feed_check.sh → swiftc → this binary. No arguments.
// ===========================================================================

var checks = 0
var failures = 0

func check(_ label: String, _ passed: Bool, _ detail: @autoclosure () -> String = "") {
    checks += 1
    if passed {
        print("ok   \(label)")
    } else {
        failures += 1
        let extra = detail()
        print("FAIL \(label)\(extra.isEmpty ? "" : " — \(extra)")")
    }
}

func alertJSON(_ id: Int, title: String = "Alert", level: String = "info",
               body: String? = nil, ts: String? = "2026-11-19T10:00:00Z") -> String {
    let bodyValue = body.map { "\"\($0)\"" } ?? "null"
    let tsValue = ts.map { "\"\($0)\"" } ?? "null"
    return """
    {"id": \(id), "ts": \(tsValue), "title": "\(title)", "body": \(bodyValue), \
    "level": "\(level)", "key": null, "acked": false, "acked_at": null}
    """
}

func pageJSON(_ alerts: [String], latest: Int?, next: Int?, hasMore: Bool = false) -> String {
    let latestValue = latest.map(String.init) ?? "null"
    let nextValue = next.map(String.init) ?? "null"
    return """
    {"alerts": [\(alerts.joined(separator: ","))], "count": \(alerts.count), \
    "since": null, "limit": 100, "oldest_id": null, "latest_id": \(latestValue), \
    "next_since": \(nextValue), "has_more": \(hasMore)}
    """
}

func decodePage(_ json: String) -> AlertFeedPage {
    do {
        return try JSONDecoder().decode(AlertFeedPage.self, from: Data(json.utf8))
    } catch {
        fatalError("fixture page did not decode: \(error)")
    }
}

// ---------------------------------------------------------------------------
// 1. The wire shape
// ---------------------------------------------------------------------------

let decoded = decodePage(pageJSON([
    alertJSON(4, title: "Deploy waiting", level: "action"),
    alertJSON(5, title: "Card stuck", level: "critical"),
], latest: 5, next: 5))

check("decodes the feed's public rows",
      decoded.alerts.count == 2 && decoded.alerts[0].id == 4
        && decoded.alerts[1].level == "critical")
check("acked_at null stays nil, never an empty timestamp",
      decoded.alerts[0].ackedAt == nil && decoded.alerts[0].acked == false)
check("next_since and latest_id decode",
      decoded.nextSince == 5 && decoded.latestId == 5 && !decoded.hasMore)

// ---------------------------------------------------------------------------
// 2. First launch: a baseline, not a notification replay
// ---------------------------------------------------------------------------

let firstPage = decodePage(pageJSON([
    alertJSON(4, title: "Four"),
    alertJSON(5, title: "Five"),
    alertJSON(6, title: "Six"),
], latest: 6, next: 6))
let baseline = AlertFeedPolicy.baseline(page: firstPage)

check("baseline lists the newest alerts first",
      baseline.rows.map(\.id) == [6, 5, 4], "\(baseline.rows.map(\.id))")
check("baseline starts cursor and viewed id at the store's newest id",
      baseline.knownID == 6 && baseline.viewedID == 6)
check("baseline shows no unseen badge", baseline.unseen == 0)

let emptyBaseline = AlertFeedPolicy.baseline(
    page: decodePage(pageJSON([], latest: nil, next: nil)))
check("an empty first page still starts a cursor at 0",
      emptyBaseline.knownID == 0 && emptyBaseline.rows.isEmpty)

let firstAlert = AlertFeedPolicy.advance(
    page: decodePage(pageJSON([alertJSON(1, title: "First alert")], latest: 1, next: 1)),
    from: emptyBaseline)
check("the first alert after an empty baseline DOES notify",
      firstAlert.notify.map(\.id) == [1] && firstAlert.state.unseen == 1)

// ---------------------------------------------------------------------------
// 3. A normal poll advances by next_since
// ---------------------------------------------------------------------------

let p2 = decodePage(pageJSON([alertJSON(7), alertJSON(8)], latest: 8, next: 8))
let advance = AlertFeedPolicy.advance(page: p2, from: baseline)

check("new alerts notify in ascending order",
      advance.notify.map(\.id) == [7, 8], "\(advance.notify.map(\.id))")
check("rows merge newest-first and stay at the display limit",
      advance.state.rows.map(\.id) == [8, 7, 6, 5, 4], "\(advance.state.rows.map(\.id))")
check("the cursor follows next_since",
      advance.state.knownID == 8)
check("every new alert counts as unseen",
      advance.state.unseen == 2 && advance.state.viewedID == 6)

let shortPage = decodePage(pageJSON([alertJSON(9), alertJSON(10)],
                                    latest: 12, next: 10, hasMore: true))
let fromShortPage = AlertFeedPolicy.advance(page: shortPage, from: advance.state)
check("a full page advances by next_since, NOT the store's latest_id",
      fromShortPage.state.knownID == 10,
      "known \(String(describing: fromShortPage.state.knownID)) would skip 11 and 12")
check("alerts beyond a full page are not counted as delivered",
      fromShortPage.notify.map(\.id) == [9, 10])

// ---------------------------------------------------------------------------
// 4. A burst is coalesced, never a wall of banners
// ---------------------------------------------------------------------------

let burstJSON = (21...28).map { alertJSON($0, title: "Burst \($0)") }
let burst = AlertFeedPolicy.advance(
    page: decodePage(pageJSON(burstJSON, latest: 28, next: 28)),
    from: AlertFeedState(rows: [], knownID: 20, viewedID: 20, unseen: 0))

check("a burst caps banners at 5 and counts the rest",
      burst.notify.map(\.id) == [24, 25, 26, 27, 28] && burst.suppressed == 3,
      "\(burst.notify.map(\.id)) suppressed \(burst.suppressed)")
check("the dropdown still keeps only 6 rows",
      burst.state.rows.count == 6 && burst.state.rows.first?.id == 28)
check("every burst alert counts toward the badge", burst.state.unseen == 8)

// ---------------------------------------------------------------------------
// 5. A cleared feed re-baselines instead of going silent
// ---------------------------------------------------------------------------

let cleared = decodePage(pageJSON([alertJSON(3, title: "After clear")],
                                  latest: 3, next: 3))
check("a feed whose newest id is below our cursor is a reset",
      AlertFeedPolicy.isFeedReset(page: cleared, knownID: 9))
check("an ordinary page is not a reset",
      !AlertFeedPolicy.isFeedReset(page: cleared, knownID: 3))

let rebased = AlertFeedPolicy.baseline(page: cleared)
check("a reset re-baselines without notifying",
      rebased.knownID == 3 && rebased.rows.map(\.id) == [3] && rebased.unseen == 0)

// ---------------------------------------------------------------------------
// 6. Small honesty rules the view depends on
// ---------------------------------------------------------------------------

let duplicatePage = decodePage(pageJSON([alertJSON(7), alertJSON(7)],
                                        latest: 7, next: 7))
let duplicateAdvance = AlertFeedPolicy.advance(page: duplicatePage, from: baseline)
check("a repeated row cannot draw twice",
      duplicateAdvance.state.rows.filter { $0.id == 7 }.count == 1)

check("levels are a closed set with an info fallback",
      AlertFeedLevel.parse("critical") == .critical
        && AlertFeedLevel.parse("ACTION") == .action
        && AlertFeedLevel.parse("invented") == .info
        && AlertFeedLevel.parse(nil) == .info)
check("each level names a distinct symbol",
      Set(AlertFeedLevel.allCases.map(\.symbolName)).count == 3)

let blank = decodePage(pageJSON([alertJSON(30, title: "")], latest: 30, next: 30))
let blankRow = AlertFeedPolicy.baseline(page: blank).rows[0]
check("an empty title never renders as a blank row",
      blankRow.titleText == "(untitled alert)")
check("a valid timestamp yields a relative age",
      !blankRow.ageText(now: Date(timeIntervalSince1970: 1_800_000_000)).isEmpty)
let undated = decodePage(pageJSON([alertJSON(31, ts: nil)], latest: 31, next: 31))
check("an unparseable/absent timestamp renders no age, not a wrong one",
      AlertFeedPolicy.baseline(page: undated).rows[0].ageText().isEmpty)

// ---------------------------------------------------------------------------
// 7. The persisted state round-trips
// ---------------------------------------------------------------------------

do {
    let data = try JSONEncoder().encode(advance.state)
    let restored = try JSONDecoder().decode(AlertFeedState.self, from: data)
    check("the persisted projection round-trips", restored == advance.state)
} catch {
    check("the persisted projection round-trips", false, "\(error)")
}

print("")
if failures > 0 {
    print("ALERT FEED CHECK FAILED — \(failures)/\(checks) checks failed")
    exit(1)
}
print("ALERT FEED CHECK PASSED — \(checks) executed checks")
