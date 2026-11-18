import Foundation
import WidgetKit

// ===========================================================================
// kanban_monitor_widget_check — executed proofs for the KANBAN MONITOR widget.
//
// Compiles the REAL Sources/HSCCWidgets/KanbanMonitorWidget.swift with the real
// SharedModels / SharedStore / WidgetDataClient and drives the board-table
// projection against a scrubbed live capture of the compact summary route.
// Nothing is redeclared and nothing touches the network.
//
// The capture is the CLI's own board table: each fixture count below is the
// value `hscc kanban monitor` printed on 2026-11-18 (Done 24h included), folded
// into the `/v1/mobile/summary` shape by `kanban_live.board_status_counts`.
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
// 1. The capture really carries the values the assertions claim.
// ---------------------------------------------------------------------------
print("\n[1] the live capture carries the CLI board-table values")
let byBoard = Dictionary(uniqueKeysWithValues: rows.map { ($0.board, $0) })
check("five active boards decoded", rows.count == 5, "\(rows.count)")
check("the CLI's order: most running first, then board name",
      rows.map(\.board) == ["hscc", "ecofire-flows", "ecofire-app", "gateclerk", "headless-display"],
      rows.map(\.board).joined(separator: ","))
check("hscc: 2 running / 0 ready / 0 todo / 5 blocked / 73 done 24h",
      byBoard["hscc"]?.running == 2 && byBoard["hscc"]?.ready == 0
      && byBoard["hscc"]?.todo == 0 && byBoard["hscc"]?.blocked == 5
      && byBoard["hscc"]?.done == 73)
check("ecofire-flows: 1 running / 2 todo / 1 blocked / 6 done 24h",
      byBoard["ecofire-flows"]?.running == 1 && byBoard["ecofire-flows"]?.todo == 2
      && byBoard["ecofire-flows"]?.blocked == 1 && byBoard["ecofire-flows"]?.done == 6)
check("headless-display: 10 blocked and no done today",
      byBoard["headless-display"]?.blocked == 10 && byBoard["headless-display"]?.done == 0)
check("an idle board would be skipped exactly as boards_table skips it",
      !KanbanBoardRow(board: "idle", running: 0, ready: 0, todo: 0,
                      blocked: 0, done: 0).hasActivity)
if case .success(let table) = KanbanBoardRow.readResult(from: summary) {
    check("the envelope's registered-board count survives", table.boardCount == 5)
    check("the envelope's unreadable count is nil when every board read", table.unreadableBoards == nil)
} else {
    check("the live capture is accepted", false, "read refused")
}

// ---------------------------------------------------------------------------
// 2. The entry's aggregates are the CLI's own totals, built only from rows.
// ---------------------------------------------------------------------------
print("\n[2] the entry aggregates the rows it draws")
let live = KanbanMonitorEntry(date: .now, configured: true, rows: rows,
                              boardCount: rows.count, unreadableBoards: nil,
                              failure: nil, partialReason: nil, ageMinutes: nil,
                              refreshMinutes: 5, paused: false)
check("running total is the CLI title's n_run (2+1=3)", live.runningTotal == 3, "\(live.runningTotal)")
check("blocked total is the sum of the Blocked cells (5+1+1+2+10=19)", live.blockedTotal == 19)
check("done-today total is the sum of the Done 24h cells (73+6=79)", live.doneTotal == 79)
check("the small family's hero is running", live.headlineNumber == 3 && live.headlineLabel == "running")
check("the inline line points at the busiest board's exact cells",
      live.inlineLine == "hscc 2 run · 5 blocked", live.inlineLine)
check("the rectangular line names the busiest board, not an aggregate",
      live.rectangularLine == "hscc 2 run · 5 blocked", live.rectangularLine)
check("family depths: small 2, medium 5, large 8",
      KanbanMonitorEntry.rowLimit(for: .systemSmall) == 2
      && KanbanMonitorEntry.rowLimit(for: .systemMedium) == 5
      && KanbanMonitorEntry.rowLimit(for: .systemLarge) == 8)
check("nothing open is an honest empty, not a failure",
      KanbanMonitorEntry(date: .now, configured: true, rows: [], boardCount: 0,
                         unreadableBoards: nil, failure: nil, partialReason: nil,
                         ageMinutes: nil, refreshMinutes: 5, paused: false).inlineLine == "No open cards")

// ---------------------------------------------------------------------------
// 3. Stale and failed states carry their age and reason.
// ---------------------------------------------------------------------------
print("\n[3] age and reason are visible, never implied")
let stale = KanbanMonitorEntry(date: .now, configured: true, rows: rows,
                               boardCount: rows.count, unreadableBoards: nil,
                               failure: nil, partialReason: .unreachable(code: nil),
                               ageMinutes: 12, refreshMinutes: 5, paused: false)
check("a kept snapshot says updated + age + reason",
      stale.updatedLine == "updated 12m ago · No connection", stale.updatedLine)
let failed = KanbanMonitorEntry(date: .now, configured: true, rows: [], boardCount: nil,
                                unreadableBoards: nil, failure: .unauthorized,
                                partialReason: nil, ageMinutes: nil,
                                refreshMinutes: 5, paused: false)
check("a failed read names the reason and draws no count",
      failed.inlineLine == "Token rejected" && failed.headlineNumber == nil)
check("paused draws no data and says so",
      KanbanMonitorEntry.paused(refreshMinutes: 0).inlineLine == "Paused"
      && KanbanMonitorEntry.paused(refreshMinutes: 0).rows.isEmpty)
let unconfigured = KanbanMonitorEntry.unconfigured
check("unconfigured invites setup before anything else",
      unconfigured.inlineLine == "Set up in HSCC" && unconfigured.gateLine.contains("Set up"))

// ---------------------------------------------------------------------------
// 4. Payload refusals: degraded and truncated data never become a table.
// ---------------------------------------------------------------------------
print("\n[4] a payload the app cannot trust is refused")
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
// 5. §7 storage: board slugs + integers + the fetch date, nothing else.
// ---------------------------------------------------------------------------
print("\n[5] the App Group snapshot is the projection, nothing else (§7)")
let suite = SharedStore.suite
check("the shared suite is reachable", suite != nil)
if let suite {
    let prior = suite.data(forKey: KanbanMonitorSnapshotStore.storageKey)
    let stamp = Date(timeIntervalSince1970: 1_700_000_000)
    KanbanMonitorSnapshotStore.save(rows, at: stamp)
    let loaded = KanbanMonitorSnapshotStore.lastKnown()
    check("the board rows round-trip", loaded?.value.rows == rows)
    check("and the fetch date travels with them", loaded?.at == stamp)
    if let data = suite.data(forKey: KanbanMonitorSnapshotStore.storageKey),
       let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        let top = Set(object.keys)
        check("the stored top level is rows + date", top == ["rows", "date"], top.sorted().joined(separator: ","))
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
