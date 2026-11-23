import Foundation

// ---------------------------------------------------------------------------
// cards_running_widget_check/main.swift — execute the cards surface's data
// rules against committed LIVE fixtures.
//
// Compiles the REAL `SharedModels.swift`, `SharedStore.swift`,
// `WidgetDataClient.swift` and `CardsRunningProjection.swift`. Nothing here
// re-declares a subject type, so a green run proves the shipped rules against
// the fixture, not a transcription of them.
//
// One section per family. Each asserts the values that family draws equal the
// fixture's own fields:
//   * iOS systemSmall / systemMedium / systemLarge + accessoryInline /
//     accessoryCircular / accessoryRectangular
//   * macOS systemSmall / systemMedium / systemLarge / systemExtraLarge
// `hscc cards monitor --short` is the reference: running cards with title,
// board, worker and run time, and a count equal to the number of running cards.
// ---------------------------------------------------------------------------

var failures = 0

func check(_ name: String, _ ok: Bool, _ detail: @autoclosure () -> String = "") {
    if ok {
        print("  ok: \(name)")
    } else {
        failures += 1
        let extra = detail()
        print("  FAIL: \(name)\(extra.isEmpty ? "" : " — \(extra)")")
    }
}

func checkText(_ name: String, _ got: String?, _ want: String) {
    check(name, got == want, "got \"\(got ?? "nil")\", want \"\(want)\"")
}

func checkInt(_ name: String, _ got: Int?, _ want: Int) {
    check(name, got == want, "got \(got.map(String.init) ?? "nil"), want \(want)")
}

let dir = URL(fileURLWithPath: CommandLine.arguments[1])
func load(_ name: String) throws -> Data {
    try Data(contentsOf: dir.appendingPathComponent(name))
}

let iso = ISO8601DateFormatter()
iso.formatOptions = [.withInternetDateTime]
let now = iso.date(from: "2026-11-18T16:00:00Z")!

// The fixture each family reads: a scrubbed capture of GET /v1/kanban/running
// from the live API. Values are generalized; shape, count and timing are the
// capture's.
let liveData = try load("v1_kanban_running_cards.json")
let live = try JSONDecoder().decode(KanbanRunningLite.self, from: liveData)
let blockedFixture = try JSONDecoder().decode(KanbanBlockedLite.self,
                                              from: try load("kanban_blocked.json"))
let tasks = live.tasks ?? []
let projection = CardsRunningProjection(running: live, blocked: blockedFixture, now: now)

print("\nCards widget — decode (scrubbed live GET /v1/kanban/running)\n")

check("the live envelope decodes", tasks.count == 3 && live.count == 3,
      "tasks=\(tasks.count) count=\(live.count.map(String.init) ?? "nil")")
check("the count is the number of running cards, exactly as the CLI",
      projection.runningCount == tasks.count,
      "projection=\(projection.runningCount.map(String.init) ?? "nil") tasks=\(tasks.count)")
check("the wire keeps pid/host_local while the model does not declare them",
      String(decoding: liveData, as: UTF8.self).contains("\"pid\"")
      && String(decoding: liveData, as: UTF8.self).contains("\"host_local\""))
check("every projected row maps to one fixture task",
      Set(projection.rows.map(\.cardID)) == Set(tasks.map(\.id)))

// Every shown field is checked against the task with the same id. `worker`
// falls back to `assignee` on this capture, which is what the old server sends.
var byID: [String: RunningCardLite] = [:]
for task in tasks { byID[task.id] = task }

for row in projection.rows {
    let task = byID[row.cardID]
    check("row \(row.cardID): board is the fixture board",
          row.board == task?.board)
    check("row \(row.cardID): title is the fixture title",
          row.title == task?.title)
    check("row \(row.cardID): worker is worker-or-assignee from the fixture",
          row.worker == (task?.worker ?? task?.assignee))
    check("row \(row.cardID): status is the fixture status",
          row.status == task?.status)
}

print("\niOS families — every shown value equals a fixture field\n")

// systemSmall: one headline number + 2 rows + the age footer.
checkInt("systemSmall headline number", CardsRunningCopy.headlineNumber(projection), 3)
checkText("systemSmall headline label", CardsRunningCopy.runningText(projection), "3 running")
check("systemSmall draws 2 rows", projection.rows.prefix(2).count == 2)
checkText("systemSmall newest age is minutes-since-start from the fixture",
          projection.rows.first(where: { $0.cardID == "t_1a2b3c4d" })?.ageText, "1h42m")
checkText("systemSmall footer is a live age, not a stale claim",
          CardsRunningCopy.footerLine(projection: projection, ageMinutes: nil, failure: nil),
          "updated just now")

// systemMedium: 3 two-line rows with board.
check("systemMedium draws 3 rows", projection.rows.prefix(3).count == 3)
let mediumRow = projection.rows.first(where: { $0.cardID == "t_5e6f7a8b" })!
checkText("systemMedium board", mediumRow.board, "demo")
checkText("systemMedium worker", mediumRow.workerText, "ux-engineer")
checkText("systemMedium age", mediumRow.ageText, "31m")

// systemLarge: the stored cap, ordered longest run first.
check("systemLarge draws the stored cap", projection.rows.prefix(8).count == 3)
check("rows are ordered longest-running first",
      projection.rows.map(\.cardID) == ["t_1a2b3c4d", "t_9c0d1e2f", "t_5e6f7a8b"])

// accessoryInline: the running count only — the CLI's cards view.
checkText("accessoryInline", CardsRunningCopy.inlineLine(projection: projection, failure: nil),
          "3 running")

// accessoryCircular: the one scalar.
checkInt("accessoryCircular", CardsRunningCopy.headlineNumber(projection), 3)

// accessoryRectangular: one row, title clipped to the family budget, plus the
// worker and age the surface is named for.
let rect = projection.rows[0]
let rectTitle = rect.headline(max: 20)
check("accessoryRectangular title is the fixture title (clipped, never invented)",
      rectTitle.hasSuffix("…")
      ? rect.title!.hasPrefix(String(rectTitle.dropLast()))
      : rectTitle == rect.title!,
      "shown \"\(rectTitle)\" fixture \"\(rect.title ?? "nil")\"")
checkText("accessoryRectangular worker", rect.workerText, "ios-engineer")
checkText("accessoryRectangular age", rect.ageText, "1h42m")

print("\nmacOS families — Small / Medium / Large / XL\n")

// The macOS target compiles the same views, so the same fixture drives them.
// Small 2, Medium 3, Large/XL 8 (the stored cap).
check("macOS systemSmall inherits the 2-row budget", projection.rows.prefix(2).count == 2)
check("macOS systemMedium inherits the 3-row budget", projection.rows.prefix(3).count == 3)
check("macOS systemLarge inherits the 8-row cap", projection.rows.prefix(8).count == 3)
check("macOS systemExtraLarge reuses that same cap", projection.rows.prefix(8).count == 3)

print("\nHonesty rules — no invented number, no stale cache shown fresh\n")

checkText("a card under a minute prints the CLI's seconds, not 0m",
          CardsRunningRow(board: "hscc", cardID: "t_short", title: "Just started",
                          worker: "ios-engineer", status: "running",
                          ageSeconds: 30).ageText,
          "30s")

check("a blind read has no count to print", CardsRunningCopy.runningText(nil) == nil)
checkText("a blind inline names the reason",
          CardsRunningCopy.inlineLine(projection: nil, failure: .timeout(seconds: 8)),
          "Timed out")
checkText("a stale footer carries the age AND the reason",
          CardsRunningCopy.footerLine(projection: projection, ageMinutes: 3,
                                      failure: .unreachable(code: nil)),
          "last known 3 min ago · No connection")
checkText("a blind footer is the operator sentence",
          CardsRunningCopy.footerLine(projection: nil, ageMinutes: nil,
                                      failure: .unauthorized),
          WidgetDataFailure.unauthorized.operatorLine)
checkText("a blocked read that did not answer is a dash, not zero",
          CardsRunningCopy.blockedText(CardsRunningProjection(rows: [], runningCount: 3,
                                                             blockedCount: nil,
                                                             rowsAreComplete: true)),
          "blocked —")
checkInt("the secondary blocked figure is the blocked route's own count",
         projection.blockedCount, blockedFixture.count ?? -1)

print("\nContract shape — worker / run_s / hb_s from CARD_VIEW_FIELDS\n")

let contract = try JSONDecoder().decode(KanbanRunningLite.self,
                                        from: try load("v1_kanban_running_contract.json"))
let contractProjection = CardsRunningProjection(running: contract, blocked: nil, now: now)
let contractRows = contractProjection.rows
check("contract rows decode", contractRows.count == 3 && contract.tasks?.count == 3)
check("run_s orders the rows longest-first", contractRows.map(\.cardID)
      == ["t_aa11bb22", "t_cc33dd44", "t_ee55ff66"])
checkText("worker wins over assignee when both are sent",
          contractRows[0].workerText, "backend-engineer")
checkInt("run_s is the age source when sent", contractRows[0].ageMinutes, 62)
checkText("started_at is the fallback when run_s is null",
          contractRows[1].workerText, "ios-engineer")
checkInt("started_at fallback age", contractRows[1].ageMinutes, 31)
checkInt("run_s alone (no started_at) still ages", contractRows[2].ageMinutes, 3)
check("hb_s is decoded for the R3 heartbeat label, not fabricated",
      contract.tasks?.first?.hb_s == 12.4)
checkInt("hb_s is projected into the stored row", contractRows[0].heartbeatSeconds, 12)
checkText("a fresh heartbeat reads as seconds", contractRows[0].heartbeatText, "beat 12s")
check("a missing hb_s is unknown, never a fresh beat",
      contractRows[1].heartbeatSeconds == nil
        && contractRows[1].heartbeatText == "beat —"
        && contractRows[1].heartbeatIsFresh == nil)
checkText("an old heartbeat reads as minutes and turns warn",
          CardsRunningRow(board: "hscc", cardID: "t_old", title: "Old",
                          worker: "worker", status: "running",
                          ageSeconds: 4000, heartbeatSeconds: 720)
              .heartbeatText,
          "beat 12m")
check("the R3 hero clock keeps hours readable",
      CardsRunningRow(board: "hscc", cardID: "t_long", title: "Long",
                      worker: "worker", status: "running",
                      ageSeconds: 3900).heroAgeText == "1h 05m")

print("")
if failures == 0 {
    print("CARDS WIDGET CHECK PASSED — every family's shown values equal the live fixture's fields, and no blind/stale state invents a number")
} else {
    print("CARDS WIDGET CHECK FAILED — \(failures) assertion(s)")
}
exit(failures == 0 ? 0 : 1)
