import Foundation
import WidgetKit

// ===========================================================================
// kanban_monitor_widget_check — executed proofs for the Boards widget (B3).
//
// Compiles the REAL Sources/HSCCWidgets/KanbanMonitorWidget.swift with the real
// SharedModels / SharedStore / WidgetDataClient and drives the B3 projection
// against a scrubbed live capture of the compact summary route. Nothing is
// redeclared and nothing touches the network.
//
// The capture is the CLI's own board table; the fixture adds the server's
// `max_in_progress` cap so the worker-slot strip is proven to come from the
// wire, not from a literal in the widget.
// ===========================================================================

var checks = 0
var failures = 0

func check(_ label: String, _ condition: Bool, _ got: String = "") {
    checks += 1
    if condition {
        print("  ok   " + label)
    } else {
        failures += 1
        print("  FAIL " + label + (got.isEmpty ? "" : "  [got: " + got + "]"))
    }
}

let fixtureDirs = CommandLine.arguments.dropFirst()
func fixture(_ name: String) -> Data {
    for dir in fixtureDirs {
        let path = dir + "/" + name
        if let data = FileManager.default.contents(atPath: path) { return data }
    }
    print("FATAL: no fixture named " + name + " in " + fixtureDirs.joined(separator: ", "))
    exit(2)
}

func decode<T: Decodable>(_ type: T.Type, _ name: String) -> T {
    do {
        return try JSONDecoder().decode(type, from: fixture(name))
    } catch {
        print("FATAL: \(name) does not decode as \(type): \(error)")
        exit(2)
    }
}

let summary = decode(MobileSummaryResponse.self, "v1_mobile_summary_live_2026-11-18.json")
let rows = KanbanBoardRow.rows(from: summary)

// ---------------------------------------------------------------------------
// 1. The capture really carries the values the B3 bars draw.
// ---------------------------------------------------------------------------
print("\n[1] the live capture carries the B3 deck values")
let byBoard = Dictionary(uniqueKeysWithValues: rows.map { ($0.board, $0) })
check("five active boards decoded", rows.count == 5, "\(rows.count)")
check("the B3 order: most open cards first, then board name",
      rows.map(\.board) == ["headless-display", "hscc", "ecofire-flows",
                            "gateclerk", "ecofire-app"],
      rows.map(\.board).joined(separator: ","))
check("hscc: 2 running / 0 todo / 5 blocked / 73 done = 7 open",
      byBoard["hscc"]?.running == 2 && byBoard["hscc"]?.todo == 0
      && byBoard["hscc"]?.blocked == 5 && byBoard["hscc"]?.done == 73
      && byBoard["hscc"]?.openCards == 7)
check("headless-display: 10 open / 10 blocked / no done today",
      byBoard["headless-display"]?.openCards == 10
      && byBoard["headless-display"]?.blocked == 10
      && byBoard["headless-display"]?.done == 0)
check("ecofire-flows: 4 open / 6 done today",
      byBoard["ecofire-flows"]?.openCards == 4
      && byBoard["ecofire-flows"]?.done == 6)
check("an idle board would be skipped exactly as boards_table skips it",
      !KanbanBoardRow(board: "idle", running: 0, ready: 0, todo: 0,
                      blocked: 0, done: 0).hasActivity)
if case .success(let table) = KanbanBoardRow.readResult(from: summary) {
    check("the envelope's registered-board count survives", table.boardCount == 5)
    check("the envelope's unreadable count is nil when every board read", table.unreadableBoards == nil)
    check("the server's worker-slot cap is carried, not invented",
          table.maxInProgress == 6, "\(String(describing: table.maxInProgress))")
    check("the summary's generated_at is the data's clock",
          table.generatedAtRaw == "2026-11-18T15:32:05Z")
} else {
    check("the live capture is accepted", false, "read refused")
}

// ---------------------------------------------------------------------------
// 2. The entry's B3 aggregates are the fixture's own fields.
// ---------------------------------------------------------------------------
print("\n[2] the entry aggregates the rows it draws")
let live = KanbanMonitorEntry(date: .now, configured: true, rows: rows,
                              boardCount: rows.count, unreadableBoards: nil,
                              maxInProgress: 6,
                              generatedAtRaw: "2026-11-18T15:32:05Z",
                              failure: nil, partialReason: nil, ageMinutes: nil,
                              refreshMinutes: 15, paused: false)
check("running total is the sum of Running cells (2+1=3)", live.runningTotal == 3, "\(live.runningTotal)")
check("blocked total is the sum of Blocked cells (5+1+1+2+10=19)", live.blockedTotal == 19)
check("done-today total is the sum of Done 24h cells (73+6=79)", live.doneTotal == 79)
check("the bar denominator is the largest open count", live.maxOpenCards == 10, "\(live.maxOpenCards)")
check("small draws the top four by open cards",
      live.topBoards.map(\.board) == ["headless-display", "hscc",
                                      "ecofire-flows", "gateclerk"])
check("the inline accessory says running + blocked",
      live.inlineLine == "3 running · 19 blocked", live.inlineLine)
check("the rectangular accessory says done + blocked",
      live.rectangularLine == "79 done today · 19 blocked", live.rectangularLine)
check("family depths: small 4, medium 4, large 10",
      KanbanMonitorEntry.rowLimit(for: .systemSmall) == 4
      && KanbanMonitorEntry.rowLimit(for: .systemMedium) == 4
      && KanbanMonitorEntry.rowLimit(for: .systemLarge) == 10)
check("nothing open is an honest empty, not a failure",
      KanbanMonitorEntry(date: .now, configured: true, rows: [], boardCount: 0,
                         unreadableBoards: nil, failure: nil, partialReason: nil,
                         ageMinutes: nil, refreshMinutes: 15, paused: false).inlineLine
      == "0 running · 0 blocked")

// ---------------------------------------------------------------------------
// 3. One-board scope uses that board's row and running titles.
// ---------------------------------------------------------------------------
print("\n[3] one-board scope draws one bar and its running titles")
let scoped = KanbanMonitorEntry(date: .now, configured: true, rows: rows,
                                boardCount: rows.count, unreadableBoards: nil,
                                maxInProgress: 6, scopeBoard: "hscc",
                                runningTitles: ["Wire the boards widget"],
                                failure: nil, partialReason: nil, ageMinutes: nil,
                                refreshMinutes: 15, paused: false)
check("the scoped row is the board the intent chose",
      scoped.scopedRow?.board == "hscc" && scoped.scopedRow?.openCards == 7)
check("running titles are the scoped board's rows",
      scoped.runningTitles == ["Wire the boards widget"])

// ---------------------------------------------------------------------------
// 4. Stale and failed states carry their age and reason.
// ---------------------------------------------------------------------------
print("\n[4] age and reason are visible, never implied")
let stale = KanbanMonitorEntry(date: .now, configured: true, rows: rows,
                               boardCount: rows.count, unreadableBoards: nil,
                               maxInProgress: 6,
                               failure: nil, partialReason: .unreachable(code: nil),
                               ageMinutes: 40, refreshMinutes: 15, paused: false)
check("a kept snapshot is dimmed and says offline",
      stale.staleState?.isDimmed == true
      && (stale.staleState?.label?.contains("offline") ?? false))
let failed = KanbanMonitorEntry(date: .now, configured: true, rows: [], boardCount: nil,
                                unreadableBoards: nil, failure: .unauthorized,
                                partialReason: nil, ageMinutes: nil,
                                refreshMinutes: 15, paused: false)
check("a failed read names the reason and draws no count",
      failed.inlineLine == "Token rejected" && failed.runningTotal == 0)
check("paused draws no data and says so",
      KanbanMonitorEntry.paused(refreshMinutes: 0).inlineLine == "Paused"
      && KanbanMonitorEntry.paused(refreshMinutes: 0).rows.isEmpty)
let unconfigured = KanbanMonitorEntry.unconfigured
check("unconfigured invites setup before anything else",
      unconfigured.inlineLine == "Set up in HSCC" && unconfigured.gateLine.contains("Set up"))

// ---------------------------------------------------------------------------
// 5. Payload refusals: degraded and truncated data never become a table.
// ---------------------------------------------------------------------------
print("\n[5] a payload the app cannot trust is refused")
let degraded = decode(MobileSummaryResponse.self, "v1_mobile_summary_degraded.json")
if case .failure(let f) = KanbanBoardRow.readResult(from: degraded) {
    check("degraded summary is refused", f == .decoding(detail: "mobile/summary carried no boards"))
} else {
    check("degraded summary is refused", false, "returned rows")
}
let truncated = decode(MobileSummaryResponse.self, "v1_mobile_summary_truncated.json")
if case .failure(let f) = KanbanBoardRow.readResult(from: truncated) {
    check("truncated summary is refused rather than under-counted",
          f == .decoding(detail: "mobile/summary was truncated"))
} else {
    check("truncated summary is refused rather than under-counted", false, "returned rows")
}
let boardError = decode(MobileSummaryResponse.self, "v1_mobile_summary_board_error.json")
if case .success(let table) = KanbanBoardRow.readResult(from: boardError) {
    check("a board-level error drops only that board",
          !table.rows.contains { $0.board == "headless-display" }
          && table.rows.contains { $0.board == "hscc" })
} else {
    check("a board-level error drops only that board", false, "read refused")
}

// ---------------------------------------------------------------------------
// 6. §7 storage: board slugs + integers + the fetch date, nothing else.
// ---------------------------------------------------------------------------
print("\n[6] the App Group snapshot is the projection, nothing else (§7)")
let suite = SharedStore.suite
check("the shared suite is reachable", suite != nil)
if let suite {
    let prior = suite.data(forKey: KanbanMonitorSnapshotStore.storageKey)
    let stamp = Date(timeIntervalSince1970: 1_700_000_000)
    KanbanMonitorSnapshotStore.save(rows, maxInProgress: 6, at: stamp)
    let loaded = KanbanMonitorSnapshotStore.lastKnown()
    check("the board rows round-trip", loaded?.value.rows == rows)
    check("the cap travels with the rows", loaded?.value.maxInProgress == 6)
    check("and the fetch date travels with them", loaded?.at == stamp)
    if let data = suite.data(forKey: KanbanMonitorSnapshotStore.storageKey),
       let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        let top = Set(object.keys)
        check("the stored top level is rows + date + cap",
              top == ["rows", "date", "maxInProgress"],
              top.sorted().joined(separator: ","))
        if let storedRows = object["rows"] as? [[String: Any]],
           let first = storedRows.first {
            let keys = Set(first.keys)
            check("a row stores only board + the five counts",
                  keys == ["board", "running", "ready", "todo", "blocked", "done"],
                  keys.sorted().joined(separator: ","))
        }
    }
    if let prior { suite.set(prior, forKey: KanbanMonitorSnapshotStore.storageKey) }
    else { suite.removeObject(forKey: KanbanMonitorSnapshotStore.storageKey) }
    suite.synchronize()
    check("the operator's snapshot is restored exactly",
          UserDefaults(suiteName: AppGroup.suiteName)?.data(forKey: KanbanMonitorSnapshotStore.storageKey) == prior)
}

print("\n\(checks) assertions, \(failures) failure(s)")
exit(failures == 0 ? 0 : 1)
