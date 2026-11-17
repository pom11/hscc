// main.swift — the executed half of scripts/board_status_widget_check.sh.
//
// It links against the REAL committed sources (BoardStatusReading.swift,
// SharedModels.swift, SharedStore.swift, WidgetDataClient.swift) and drives
// BoardStatusProjection / BoardStatusReading / BoardStatusSnapshotStore with
// fixture payloads. NOTHING here redeclares a subject type: a re-declaration is
// the orphan-mirror shape scripts/ has had to delete twice, and the .sh fails
// the gate if this file declares any of them.
//
// Every assertion is one of the widget's honesty rules from
// ios-app/docs/WIDGET_DATA_CONTRACT.md, and the section names say which. No
// network, no simulator, no WidgetKit: the view half is checked structurally by
// the script that runs this binary.
import Foundation

let argv = CommandLine.arguments
guard argv.count >= 3 else {
    FileHandle.standardError.write(Data("usage: board_status_widget_check <own fixtures> <shared fixtures>\n".utf8))
    exit(2)
}
let ownDir = argv[1]       // this harness's fixtures
let sharedDir = argv[2]    // scripts/model_decode_check/fixtures (the committed wire captures)

var checks = 0
var failures = 0

func section(_ title: String) { print("\n" + title) }

func check(_ label: String, _ ok: Bool, _ got: String = "") {
    checks += 1
    if ok {
        print("  ok   " + label)
    } else {
        failures += 1
        print("  FAIL " + label + (got.isEmpty ? "" : "  [got: " + got + "]"))
    }
}

func readFixture(_ dir: String, _ name: String) -> Data {
    let path = dir + "/" + name
    guard let data = FileManager.default.contents(atPath: path) else {
        print("FATAL: no fixture at " + path)
        exit(2)
    }
    return data
}

func mustDecode<T: Decodable>(_ type: T.Type, _ dir: String, _ name: String) -> T {
    do {
        return try JSONDecoder().decode(type, from: readFixture(dir, name))
    } catch {
        print("FATAL: " + name + " does not decode as " + String(describing: type) + ": " + String(describing: error))
        exit(2)
    }
}

/// Unwrap a projection or die: a section that cannot get its input is a broken
/// harness, not a finding, and must not report a passing count.
func need(_ result: Result<BoardStatusProjection, WidgetDataFailure>, _ what: String) -> BoardStatusProjection {
    switch result {
    case .success(let p): return p
    case .failure(let f):
        print("FATAL: " + what + " was expected to produce numbers but the read refused: " + String(describing: f))
        exit(2)
    }
}

func failureOf(_ result: Result<BoardStatusProjection, WidgetDataFailure>) -> WidgetDataFailure? {
    if case .failure(let f) = result { return f }
    return nil
}

func text(_ value: Int?) -> String { value.map { String($0) } ?? "nil" }

// ---------------------------------------------------------------------------
// Fixtures. Section 0 asserts the FIXTURES say what the assertions below claim
// they say — an assertion about a payload that does not actually contain the
// shape is a green light on an empty intersection.
// ---------------------------------------------------------------------------

let staleFixture = mustDecode(KanbanStaleLite.self, ownDir, "stale_with_blocked_rows.json")
let staleRows = staleFixture.tasks ?? []
let blockedFixture = mustDecode(KanbanBlockedLite.self, ownDir, "blocked_two_boards.json")
let blockedRows = blockedFixture.tasks ?? []
let blocked18 = mustDecode(KanbanBlockedLite.self, ownDir, "blocked_measured_18.json")
let staleTruncated = mustDecode(KanbanStaleLite.self, ownDir, "stale_truncated.json")
let staleNoTasks = mustDecode(KanbanStaleLite.self, ownDir, "stale_no_tasks.json")
let blockedTruncated = mustDecode(KanbanBlockedLite.self, ownDir, "blocked_truncated.json")
let blockedNoTasks = mustDecode(KanbanBlockedLite.self, ownDir, "blocked_no_tasks.json")

let staleOK: Result<KanbanStaleLite, WidgetDataFailure> = .success(staleFixture)
let blockedOK: Result<KanbanBlockedLite, WidgetDataFailure> = .success(blockedFixture)
let blocked18OK: Result<KanbanBlockedLite, WidgetDataFailure> = .success(blocked18)
let blockedDown: Result<KanbanBlockedLite, WidgetDataFailure> = .failure(.http(status: 503, code: nil))

section("0. the fixtures carry the shapes the assertions depend on")
check("stale fixture: rows == its own count (so the read is accepted, not refused)",
      staleRows.count == staleFixture.count, "\(staleRows.count) rows vs count \(text(staleFixture.count))")
check("stale fixture: it really does contain rows with status blocked",
      staleRows.filter { $0.status == "blocked" }.count == 3,
      String(staleRows.filter { $0.status == "blocked" }.count))
check("stale fixture: two of those blocked rows are on the board under test",
      staleRows.filter { $0.status == "blocked" && $0.board == "hscc" }.count == 2)
check("stale fixture: statuses outside the five are present (review, missing)",
      staleRows.filter { $0.status == "review" || $0.status == nil }.count == 2)
check("stale fixture: a dialect spelling of running (claimed) is present",
      staleRows.contains { $0.status == "claimed" })
check("blocked fixture: rows == its own count", blockedRows.count == blockedFixture.count)
check("blocked fixture: 3 of its 5 rows are on the board under test",
      blockedRows.filter { $0.board == "hscc" }.count == 3)
check("blocked fixture: its board is the SAME board the stale rows are on",
      Set(blockedRows.map { $0.board }).isSubset(of: Set(staleRows.map { $0.board })))
check("truncated stale fixture: fewer rows than its count",
      (staleTruncated.tasks ?? []).count == 3 && staleTruncated.count == 15)
check("truncated blocked fixture: 3 rows against its own count of 18",
      (blockedTruncated.tasks ?? []).count == 3 && blockedTruncated.count == 18)
check("measured-18 fixture: 18 rows and count 18 (the live daemon's shape)",
      (blocked18.tasks ?? []).count == 18 && blocked18.count == 18)
check("no-tasks fixtures: count present, tasks absent",
      staleNoTasks.tasks == nil && staleNoTasks.count == 0 && blockedNoTasks.tasks == nil && blockedNoTasks.count == 4)

// ---------------------------------------------------------------------------
// §5.2 + §6 rule 1 — the interim path, which is the path that runs on every
// cluster today (mobile/summary answers 404 until PR #310 deploys).
// ---------------------------------------------------------------------------

section("1. the interim never invents done, and never counts done from nothing (§5.2)")
let interimNoBlocked = need(BoardStatusReading.interim(stale: staleOK, blocked: blockedDown, boardSlug: "hscc"),
                            "interim with a failed blocked read")
check("interim source is openOnly", interimNoBlocked.source == .openOnly)
check("openOnly implies done == nil", interimNoBlocked.done == nil, text(interimNoBlocked.done))
check("the unknown done is reported as unknown, not as zero", interimNoBlocked.value(for: "done") == nil)
let allNil = BoardStatusProjection(boardSlug: "hscc", todo: nil, ready: nil, running: nil,
                                   blocked: nil, done: nil, source: .openOnly)
check("a projection with no numbers says it has no numbers", !allNil.hasNumbers)
check("and the interim cannot be built into one that has a done",
      need(BoardStatusReading.interim(stale: staleOK, blocked: blockedOK, boardSlug: "hscc"), "interim").done == nil)

section("2. blocked NEVER comes from the kanban/stale histogram (§6 rule 1, measured 2026-11-17)")
check("the stale list's blocked rows would have produced 2 for this board",
      staleRows.filter { $0.status == "blocked" && $0.board == "hscc" }.count == 2)
check("with kanban/blocked down, blocked is nil — NOT the 2 the stale list would give",
      interimNoBlocked.blocked == nil, text(interimNoBlocked.blocked))
check("the other four numbers are still the stale histogram (todo)",
      interimNoBlocked.todo == 2, text(interimNoBlocked.todo))
check("...ready", interimNoBlocked.ready == 1, text(interimNoBlocked.ready))
check("...running folds the claimed dialect and ignores review/nil",
      interimNoBlocked.running == 2, text(interimNoBlocked.running))
check("blockedUnknown names the gap so the footer can say it out loud",
      interimNoBlocked.blockedUnknown)
let interimBoard = need(BoardStatusReading.interim(stale: staleOK, blocked: blockedOK, boardSlug: "hscc"),
                        "interim with the blocked route answering")
check("with kanban/blocked answering, blocked is THAT route's board-scoped count",
      interimBoard.blocked == 3, text(interimBoard.blocked))
check("adding the stale list's 2 blocked rows would have made it 5 — it is still 3",
      interimBoard.blocked != 5, text(interimBoard.blocked))
check("the todo/ready/running numbers did not change because blocked came from elsewhere",
      interimBoard.todo == 2 && interimBoard.ready == 1 && interimBoard.running == 2)
let interimAll = need(BoardStatusReading.interim(stale: staleOK, blocked: blockedOK, boardSlug: nil),
                      "interim for every board together")
check("with no board picked, blocked is the route's own count (5), not the stale list's 3",
      interimAll.blocked == 5, text(interimAll.blocked))
check("and the open numbers are scoped to no board: todo is 3 across both boards",
      interimAll.todo == 3, text(interimAll.todo))
let interim18 = need(BoardStatusReading.interim(stale: staleOK, blocked: blocked18OK, boardSlug: nil),
                     "interim with the measured 18-card blocked payload")
check("the measured fleet-wide 18 reaches the tile (the stale list knows only 3)",
      interim18.blocked == 18, text(interim18.blocked))
check("and a named board still gets only its own 10 of those 18",
      need(BoardStatusReading.interim(stale: staleOK, blocked: blocked18OK, boardSlug: "hscc"), "18 board").blocked == 10)

section("3. a truncated list refuses the read instead of under-counting")
check("isTruncated is false when the envelope sends no count",
      !BoardStatusReading.isTruncated(returned: 3, total: nil))
check("isTruncated is false when rows == count",
      !BoardStatusReading.isTruncated(returned: 3, total: 3))
check("isTruncated is true when rows < count",
      BoardStatusReading.isTruncated(returned: 3, total: 15))
let truncatedRead = BoardStatusReading.interim(stale: .success(staleTruncated), blocked: blockedOK, boardSlug: "hscc")
check("a stale list shorter than its own count refuses the whole read",
      failureOf(truncatedRead) == .decoding(detail: "kanban/stale sent fewer rows than its count"),
      String(describing: truncatedRead))
check("the refusal is a decoding failure, so the surface keeps its last-known snapshot",
      failureOf(truncatedRead)?.showsStaleData == true)
check("the refusal names the route so the footer can say which read failed",
      failureOf(truncatedRead)?.shortLabel == "Can't read data")
let noTasksRead = BoardStatusReading.interim(stale: .success(staleNoTasks), blocked: blockedOK, boardSlug: "hscc")
check("a 2xx with no tasks array is not 'the board is empty' — it refuses too",
      failureOf(noTasksRead) == .decoding(detail: "kanban/stale carried no tasks array"),
      String(describing: noTasksRead))
check("and it is a refusal, not a zero: todo is not 0, there is no projection at all",
      failureOf(noTasksRead) != nil)
let staleDownRead = BoardStatusReading.interim(stale: .failure(.unauthorized), blocked: blockedOK, boardSlug: "hscc")
check("the queue read's own failure is the failure the surface renders",
      failureOf(staleDownRead) == .unauthorized, String(describing: staleDownRead))

section("4. a failed blocked read nils ONLY blocked")
check("todo/ready/running stay live while blocked is nil",
      interimNoBlocked.todo != nil && interimNoBlocked.ready != nil
      && interimNoBlocked.running != nil && interimNoBlocked.blocked == nil)
let blockedTruncatedRead = need(BoardStatusReading.interim(stale: staleOK, blocked: .success(blockedTruncated),
                                                           boardSlug: "hscc"), "blocked truncated")
check("a TRUNCATED blocked payload is dropped rather than counted (nil, not 3)",
      blockedTruncatedRead.blocked == nil, text(blockedTruncatedRead.blocked))
check("and the queue numbers survive it — the read still renders",
      blockedTruncatedRead.todo == 2 && blockedTruncatedRead.source == .openOnly)
check("the blocked tile says it is unknown so the footer can name it",
      blockedTruncatedRead.blockedUnknown)
let blockedNoRows = need(BoardStatusReading.interim(stale: staleOK, blocked: .success(blockedNoTasks), boardSlug: nil),
                         "blocked with count but no rows")
check("a server count with no rows is still the server's own number for all boards",
      blockedNoRows.blocked == 4, text(blockedNoRows.blocked))
check("and nil for a named board, because the split cannot be scoped without rows",
      need(BoardStatusReading.interim(stale: staleOK, blocked: .success(blockedNoTasks), boardSlug: "hscc"),
           "blocked no rows named board").blocked == nil)

section("5. openCount is a sum or it is nothing")
let full = BoardStatusProjection(boardSlug: "hscc", todo: 17, ready: 2, running: 3, blocked: 0,
                                 done: 750, source: .compact)
check("openCount sums the four open operands", full.openCount == 22, text(full.openCount))
check("done is not one of the operands", full.openCount == 22 && full.done == 750)
for missing in ["todo", "ready", "running", "blocked"] {
    var p = full
    switch missing {
    case "todo": p.todo = nil
    case "ready": p.ready = nil
    case "running": p.running = nil
    default: p.blocked = nil
    }
    check("openCount is nil when " + missing + " is missing (no partial sum)", p.openCount == nil, text(p.openCount))
    if missing == "blocked" {
        // The ring's own scalar is gone AND the fallback sum needs blocked, so
        // there is nothing honest to draw.
        check("with blocked unknown the ring has nothing to draw either",
              BoardStatusReading.circularNumber(p) == nil, text(BoardStatusReading.circularNumber(p)))
    } else {
        // blocked IS known to be zero here, so the ring legitimately shows a zero
        // — a fact from the route, not a missing value dressed up as one.
        check("an unknown OPEN operand does not stop the ring saying blocked is 0",
              BoardStatusReading.circularNumber(p) == 0 && !BoardStatusReading.circularIsUrgent(p),
              text(BoardStatusReading.circularNumber(p)))
    }
}
let interimOpen = need(BoardStatusReading.interim(stale: staleOK, blocked: blockedOK, boardSlug: "hscc"), "openCount interim")
check("the interim's open total is the four it knows (2+1+2+3)",
      interimOpen.openCount == 8, text(interimOpen.openCount))
check("a blocked read failure takes the open total with it rather than faking one",
      interimNoBlocked.openCount == nil, text(interimNoBlocked.openCount))

section("6. the five tiles and the projection cannot drift apart")
check("the tile list is exactly the five statuses in board order",
      BoardStatusReading.statuses.map { $0.key } == ["todo", "ready", "running", "blocked", "done"],
      BoardStatusReading.statuses.map { $0.key }.joined(separator: ","))
check("and the copy the families draw",
      BoardStatusReading.statuses.map { $0.label } == ["todo", "ready", "run", "blocked", "done"])
let expected: [String: Int] = ["todo": 17, "ready": 2, "running": 3, "blocked": 0, "done": 750]
for item in BoardStatusReading.statuses {
    check("value(for: " + item.key + ") reads its own field",
          full.value(for: item.key) == expected[item.key], text(full.value(for: item.key)))
}
check("a key the projection does not have is nil, never 0", full.value(for: "archived") == nil)

section("7. an absent status is not renderable as zero (§6 rule 1)")
check("numberText(nil) is an em dash", BoardStatusReading.numberText(nil) == "—")
check("numberText(0) is a zero", BoardStatusReading.numberText(0) == "0")
check("the dash and the zero are different strings",
      BoardStatusReading.numberText(nil) != BoardStatusReading.numberText(0))
check("a nil operand never appears in the short line at all",
      !BoardStatusReading.countsShortLine(interimNoBlocked).contains("blocked"),
      BoardStatusReading.countsShortLine(interimNoBlocked))
check("a nonzero blocked outruns everything else",
      BoardStatusReading.countsShortLine(interimBoard) == "3 blocked · 2 run",
      BoardStatusReading.countsShortLine(interimBoard))
check("with nothing blocked the run line leads",
      BoardStatusReading.countsShortLine(full) == "3 run · 17 todo",
      BoardStatusReading.countsShortLine(full))
check("the line never carries more than two operands",
      !BoardStatusReading.countsShortLine(full).contains("ready"))
check("a zero blocked is not urgent and is not the headline",
      !BoardStatusReading.circularIsUrgent(full) && BoardStatusReading.circularNumber(full) == 0)
check("one blocked card makes the ring urgent — the number that means 'waiting on you'",
      BoardStatusReading.circularIsUrgent(interimBoard) && BoardStatusReading.circularNumber(interimBoard) == 3)
check("with no numbers at all the ring draws nothing rather than 0",
      BoardStatusReading.circularNumber(allNil) == nil && !BoardStatusReading.circularIsUrgent(allNil))
// The compact path's half of the same rule, against the committed wire capture:
// BoardStatusCounts declares every status a REQUIRED Int precisely so a payload
// missing done throws instead of drawing 750 -> 0 forever.
let missingDone = readFixture(sharedDir, "v1_mobile_summary_missing_done.json")
let decodedMissingDone = try? JSONDecoder().decode(MobileSummaryResponse.self, from: missingDone)
check("a summary payload that omits done fails the decode (it cannot become 0)",
      decodedMissingDone == nil, String(describing: decodedMissingDone?.board_count))
let summary = mustDecode(MobileSummaryResponse.self, sharedDir, "v1_mobile_summary.json")
let compactOrNil = BoardStatusProjection(summary: summary, boardSlug: "hscc")
check("the compact fixture yields a projection at all", compactOrNil != nil)
check("and its source says where the numbers came from", compactOrNil?.source == .compact)
let compact = compactOrNil ?? allNil
check("the compact fixture's real done reaches the tile", compact.done == 750, text(compact.done))
check("and every number is known, so no tile is drawn from a guess",
      compact.todo == 17 && compact.ready == 2 && compact.running == 3 && compact.blocked == 0)
check("a real done is not the interim's dash", BoardStatusReading.numberText(compact.done) == "750")
let degraded = mustDecode(MobileSummaryResponse.self, sharedDir, "v1_mobile_summary_degraded.json")
check("a degraded §6 response yields NO projection (empty counts would read as 'quiet')",
      BoardStatusProjection(summary: degraded, boardSlug: "hscc") == nil)
let boardError = mustDecode(MobileSummaryResponse.self, sharedDir, "v1_mobile_summary_board_error.json")
check("a board carrying its own error yields no projection for that board (§6 rule 4)",
      BoardStatusProjection(summary: boardError, boardSlug: "headless-display") == nil)
check("and the healthy board in the same payload is still served",
      BoardStatusProjection(summary: boardError, boardSlug: "hscc")?.todo == 17)
check("the all-boards sum skips an errored tile rather than counting it as nothing",
      BoardStatusProjection(summary: boardError, boardSlug: nil)?.openCount == 22,
      text(BoardStatusProjection(summary: boardError, boardSlug: nil)?.openCount))
check("a board the payload did not carry is not an empty board",
      BoardStatusProjection(summary: summary, boardSlug: "nope-not-here") == nil)

// ---------------------------------------------------------------------------
// §7 — the last-known snapshot. This drives the REAL App Group suite, so it
// captures both keys first and restores them, and asserts the restore. It runs
// last: nothing after it can abort the process and leave operator state behind.
// ---------------------------------------------------------------------------

section("8. the snapshot belongs to its board, and stores nothing else (§7)")
let suite = SharedStore.suite
check("the shared App Group suite is reachable from a macOS harness", suite != nil)
let priorSnapshot = suite?.data(forKey: BoardStatusSnapshotStore.snapshotKey)
let priorBoards = suite?.stringArray(forKey: BoardStatusSnapshotStore.boardsKey)
if let suite {
    let stamp = Date(timeIntervalSince1970: 1_700_000_000)
    BoardStatusSnapshotStore.save(interimBoard, at: stamp)
    let loaded = BoardStatusSnapshotStore.load(boardSlug: "hscc")
    check("the projection saved for a board reads back for that board", loaded?.value == interimBoard)
    check("and its age travels with it", loaded?.at == stamp)
    check("a foreign slug gets NOTHING — another board is not last-known data",
          BoardStatusSnapshotStore.load(boardSlug: "radio") == nil)
    check("a nil slug (all boards) cannot read a named board's snapshot",
          BoardStatusSnapshotStore.load(boardSlug: nil) == nil)
    let storedBytes = suite.data(forKey: BoardStatusSnapshotStore.snapshotKey)
    check("the store wrote a real blob", storedBytes != nil)
    if let storedBytes, let raw = try? JSONSerialization.jsonObject(with: storedBytes),
       let object = raw as? [String: Any] {
        let keys = Set(object.keys)
        check("the stored top level is the projection plus its date, nothing else",
              keys == ["projection", "at"], keys.sorted().joined(separator: ","))
        let projectionKeys = Set(((object["projection"] as? [String: Any]) ?? [:]).keys)
        let allowed: Set<String> = ["boardSlug", "todo", "ready", "running", "blocked", "done", "source"]
        check("the projection keys are the slug, the five numbers and the source (§7)",
              projectionKeys.isSubset(of: allowed), projectionKeys.sorted().joined(separator: ","))
        check("an unknown done is absent from the blob, not stored as 0",
              !projectionKeys.contains("done"), projectionKeys.sorted().joined(separator: ","))
        let flattened = (object["projection"] as? [String: Any])?.values.map { String(describing: $0) } ?? []
        let leaked = flattened.first { $0.contains("http") || $0.contains("/") || $0.contains("token") }
        check("no address, path or token can hide in the blob", leaked == nil, leaked ?? "")
    }
    // A projection with no numbers must not replace the good one it would erase.
    let emptyForBoard = BoardStatusProjection(boardSlug: "hscc", todo: nil, ready: nil, running: nil,
                                              blocked: nil, done: nil, source: .openOnly)
    BoardStatusSnapshotStore.save(emptyForBoard, at: Date())
    check("a numbers-less projection is refused, so it cannot clobber the last-known numbers",
          BoardStatusSnapshotStore.load(boardSlug: "hscc")?.value == interimBoard)
    check("and a numbers-less snapshot is never readable even if one existed",
          BoardStatusSnapshotStore.load(boardSlug: "empty-board") == nil)
    let otherEmpty = BoardStatusProjection(boardSlug: "empty-board", todo: nil, ready: nil, running: nil,
                                           blocked: nil, done: nil, source: .openOnly)
    BoardStatusSnapshotStore.save(otherEmpty, at: Date())
    check("saving numbers-less for another board writes nothing either",
          BoardStatusSnapshotStore.load(boardSlug: "empty-board") == nil)
    // The picker fallback list, the store's second shape.
    BoardStatusSnapshotStore.saveBoardSlugs(["hscc", "radio"])
    check("the picker's offline slugs round-trip",
          BoardStatusSnapshotStore.loadBoardSlugs() == ["hscc", "radio"],
          BoardStatusSnapshotStore.loadBoardSlugs().joined(separator: ","))
    BoardStatusSnapshotStore.saveBoardSlugs([])
    check("an empty slug list never replaces what the picker already knows",
          BoardStatusSnapshotStore.loadBoardSlugs() == ["hscc", "radio"])

    // Restore whatever the operator had, then prove the restore — a gate that
    // leaves state behind is not a read-only gate.
    if let priorSnapshot { suite.set(priorSnapshot, forKey: BoardStatusSnapshotStore.snapshotKey) }
    else { suite.removeObject(forKey: BoardStatusSnapshotStore.snapshotKey) }
    if let priorBoards { suite.set(priorBoards, forKey: BoardStatusSnapshotStore.boardsKey) }
    else { suite.removeObject(forKey: BoardStatusSnapshotStore.boardsKey) }
    suite.synchronize()
    let fresh = UserDefaults(suiteName: AppGroup.suiteName)
    check("the operator's snapshot key is back exactly as it was",
          fresh?.data(forKey: BoardStatusSnapshotStore.snapshotKey) == priorSnapshot)
    check("the operator's slug list is back exactly as it was",
          fresh?.stringArray(forKey: BoardStatusSnapshotStore.boardsKey) == priorBoards)
    check("and a foreign board still reads nothing after the restore",
          BoardStatusSnapshotStore.load(boardSlug: "radio") == nil)
}

print("\n" + String(checks) + " assertions, " + String(failures) + " failure(s)")
exit(failures == 0 ? 0 : 1)
