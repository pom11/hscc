import Foundation
import WidgetKit

// ===========================================================================
// projects_widget_check — executed proofs for the projects widget.
//
// What runs here is the COMMITTED source:
//   Sources/HSCCWidgets/ProjectsWidget.swift   (subject — the real entry, status,
//                                               provider scope rule, projection
//                                               store, configuration intent)
//   Sources/Shared/WidgetDataClient.swift      (WidgetData.read — the ONE
//                                               stale-while-error rule it uses)
//   Sources/Shared/SharedModels.swift          (ProjectsLite / ProjectLite)
//   Sources/Shared/SharedStore.swift           (AppGroup, APIConfig)
//   scripts/projects_widget_check/ThemeStub.swift   (UI token shim, see that file)
//
// NO NETWORK is touched. `WidgetDataClient` is constructed only so
// `WidgetData.read` has a non-nil client to hand to a closure that returns a
// canned failure; the host is an unresolvable `.invalid` name and no request is
// ever built. `ProjectEntityQuery`'s own options fetch is deliberately NOT
// called for that reason — it is the one path in the file that reads the
// cluster, and it is covered by compile plus `widget_data_client_check.sh`.
//
// The SwiftUI views cannot run headless, so the dispatch (which view wins) is
// mirrored below exactly as `widget_view_dispatch_check.sh` mirrors the cluster
// widget's chain, and the script asserts by grep that all three real family
// bodies still open with the same three branches in the same order.
// ===========================================================================

var failures = 0
func check(_ label: String, _ condition: Bool) {
    print((condition ? "  ok   " : "  FAIL ") + label)
    if !condition { failures += 1 }
}

let rows = [ProjectRow(name: "hscc", board: "hscc"),
            ProjectRow(name: "radio", board: "radio"),
            ProjectRow(name: "memori", board: nil)]
let twelveMinutesAgo = Date().addingTimeInterval(-12 * 60)

// ---------------------------------------------------------------------------
// 1. The §5.1 family depths, executed on the real table.
// ---------------------------------------------------------------------------
print("\n[1] family depths (WIDGET_DATA_CONTRACT §5.1)")
check("small draws 3 rows", ProjectsEntry.rowLimit(for: .systemSmall) == 3)
check("medium draws 6 rows", ProjectsEntry.rowLimit(for: .systemMedium) == 6)
check("large draws 8 rows", ProjectsEntry.rowLimit(for: .systemLarge) == 8)
check("no family is allowed to draw more than large's 8",
      ProjectsEntry.rowLimit(for: .systemLarge) >= ProjectsEntry.rowLimit(for: .systemMedium)
      && ProjectsEntry.rowLimit(for: .systemMedium) >= ProjectsEntry.rowLimit(for: .systemSmall))
check("the placeholder is not blank", ProjectsEntry.placeholderEntry.rows.count == 3
      && ProjectsEntry.placeholderEntry.status == .live)

// ---------------------------------------------------------------------------
// 2. The status accessors must not collide: a stale entry is not a failure, and
//    a failure has no age to render.
// ---------------------------------------------------------------------------
print("\n[2] status accessors")
let stale = ProjectsStatus.stale(minutes: 12, reason: .unreachable(code: nil))
check("stale reports an age", stale.ageMinutes == 12)
check("stale is stale", stale.isStale)
check("stale is NOT a failure — it draws rows, not an error pane", stale.failure == nil)
check("stale carries the reason for the footer line",
      stale.staleReason?.shortLabel == "No connection")
let failed = ProjectsStatus.failed(.unauthorized)
check("failed has no age to render", failed.ageMinutes == nil)
check("failed reports its failure", failed.failure == .unauthorized)
check("failed has no stale reason", failed.staleReason == nil)
check("live is neither stale nor failed",
      ProjectsStatus.live.ageMinutes == nil && ProjectsStatus.live.failure == nil
      && ProjectsStatus.live.staleReason == nil)

// ---------------------------------------------------------------------------
// 3. The t_5c554c5b invariant, through the app's ONE rule (`WidgetData.read`),
//    not a re-derivation: an unconfigured app must never render stale data.
// ---------------------------------------------------------------------------
print("\n[3] unconfigured beats stale (t_5c554c5b, via WidgetData.read)")
let unconfiguredOutcome = await WidgetData.read(
    using: nil,
    lastKnown: { (value: rows, at: twelveMinutesAgo) }
) { _ in .success(rows) }
check("a nil client is .failed(.notConfigured)",
      unconfiguredOutcome.failure == .notConfigured)
check("…even when a real snapshot is sitting in the container",
      unconfiguredOutcome.ageMinutes == nil && unconfiguredOutcome.value == nil)
check("notConfigured is the one failure that never falls back",
      !WidgetDataFailure.notConfigured.showsStaleData)
let unconfiguredEntry = ProjectsEntry(date: .now, configuration: nil, rows: [],
                                      selectedName: nil, status: .failed(.notConfigured))
check("and the entry the view receives invites setup", unconfiguredEntry.isUnconfigured)

// A bad host/port is a settings problem, and it must not be dressed as staleness
// either — it is the second case `showsStaleData` refuses.
check("invalidURL never falls back either", !WidgetDataFailure.invalidURL.showsStaleData)
check("nor does a path the client refuses",
      !WidgetDataFailure.pathNotAllowed(path: "/v1/anything").showsStaleData)

// ---------------------------------------------------------------------------
// 4. stale-while-error, end to end into an entry: real rows + a real age.
// ---------------------------------------------------------------------------
print("\n[4] stale-while-error resolves into the entry")
// The client value exists only so the rule has something to hand the closure;
// the closure ignores it, so nothing here can reach the network.
let offlineClient = WidgetDataClient(config: APIConfig(host: "cluster.invalid",
                                                       port: 8788,
                                                       token: "gate-token-not-a-secret"))
let staleOutcome = await WidgetData.read(
    using: offlineClient,
    lastKnown: { (value: rows, at: twelveMinutesAgo) }
) { _ in .failure(.unreachable(code: nil)) }
check("a failed read with a snapshot is .stale, not blank", staleOutcome.isStale)
check("the age is the snapshot's age in minutes", staleOutcome.ageMinutes == 12)
check("the rows are the real stored rows", staleOutcome.value?.map(\.name) == ["hscc", "radio", "memori"])
let staleEntry = ProjectsEntry(date: .now, configuration: nil,
                               rows: staleOutcome.value ?? [],
                               selectedName: nil,
                               status: .stale(minutes: staleOutcome.ageMinutes ?? 0,
                                              reason: staleOutcome.failure ?? .unreachable(code: nil)))
check("a stale entry renders rows, not the failure pane",
      !staleEntry.isUnconfigured && staleEntry.status.failure == nil
      && staleEntry.rows.count == 3)

let noSnapshot = await WidgetData.read(
    using: offlineClient,
    lastKnown: { nil as (value: [ProjectRow], at: Date)? }
) { _ in .failure(.timeout(seconds: 8)) }
check("with no snapshot the same failure is .failed, not an empty list",
      noSnapshot.failure == .timeout(seconds: 8) && noSnapshot.value == nil)

let decodeFailure = await WidgetData.read(
    using: offlineClient,
    lastKnown: { (value: rows, at: twelveMinutesAgo) }
) { _ in .failure(.decoding(detail: "internal symbol noise")) }
check("a 2xx we cannot read keeps the last-known rows (§6 rule 1)",
      decodeFailure.isStale && decodeFailure.value?.count == 3)
check("and never renders as a zero",
      decodeFailure.value?.isEmpty == false)

// ---------------------------------------------------------------------------
// 5. The scope rule: a deleted project must not look like a live one.
// ---------------------------------------------------------------------------
print("\n[5] project scope")
check("no selection keeps the whole list", ProjectsTimelineProvider.scope(rows, to: nil).count == 3)
check("a hit filters to that row",
      ProjectsTimelineProvider.scope(rows, to: "radio").map(\.name) == ["radio"])
check("a miss yields empty — it does NOT widen back to the list",
      ProjectsTimelineProvider.scope(rows, to: "deleted-project").isEmpty)
check("the configuration folds the All sentinel to nil before any view sees it", {
    // `let`, because `IntentParameter.wrappedValue` has a nonmutating setter —
    // the value lives in the wrapper, not in the struct's storage.
    let intent = ProjectFilterIntent()
    guard intent.selectedName == nil else { return false }
    intent.project = .all
    guard intent.selectedName == nil else { return false }
    intent.project = ProjectEntity(id: "radio", name: "radio", board: "radio")
    return intent.selectedName == "radio"
}())

// ---------------------------------------------------------------------------
// 6. §7 — the stored projection. The wire payload carries `repo` (an absolute
//    path on the operator's machine) and `speak` on every row; the bytes the
//    widget writes must contain neither.
// ---------------------------------------------------------------------------
print("\n[6] the App Group projection (§7)")
let wireBody = """
{"projects":[{"name":"hscc","board":"hscc","repo":"/Users/someone/dev/hscc","topic":3},
             {"name":"radio","board":null,"repo":"/Users/someone/dev/Radio","topic":7}],
 "count":2,"speak":"2 projects registered"}
"""
let payload = try JSONDecoder().decode(ProjectsLite.self, from: Data(wireBody.utf8))
check("the real payload shape decodes with repo present on the wire",
      payload.projects.count == 2 && payload.count == 2)
let projected = ProjectsSnapshotStore.project(from: payload)
check("the projection keeps name + board only",
      projected.map { "\($0.name):\($0.board ?? "–")" } == ["hscc:hscc", "radio:–"])
let blob = String(data: try JSONEncoder().encode(
    ProjectsSnapshotStore.Snapshot(date: twelveMinutesAgo, rows: projected)), encoding: .utf8)!
check("the stored bytes carry no repo key", !blob.contains("repo"))
check("the stored bytes carry no absolute local path", !blob.contains("/Users/"))
check("the stored bytes carry no server prose", !blob.contains("speak") && !blob.contains("registered"))
check("the stored bytes do carry the date the age is rendered from", blob.contains("\"date\""))
let decodedBack = try JSONDecoder().decode(ProjectsSnapshotStore.Snapshot.self, from: Data(blob.utf8))
check("and it round-trips", decodedBack.rows == projected && decodedBack.date == twelveMinutesAgo)

let many = ProjectsLite(projects: (0..<60).map { ProjectLite(name: "p\($0)", board: "b") },
                        count: 60, speak: nil)
check("the store is bounded, not open-ended",
      ProjectsSnapshotStore.project(from: many).count == ProjectsSnapshotStore.rowLimit
      && ProjectsSnapshotStore.rowLimit <= 50)
check("rows(limit:) still caps at the payload size (§5.1)", payload.rows(limit: 8).count == 2)

// ---------------------------------------------------------------------------
// 7. The dispatch chain, mirrored from the three family bodies (which the
//    script proves by grep are still in that order in the real source).
// ---------------------------------------------------------------------------
print("\n[7] view dispatch (mirrored chain; order proven by grep in the script)")
func dispatch(_ entry: ProjectsEntry) -> String {
    if entry.isUnconfigured { return "setup" }
    if let failure = entry.status.failure { return "failure:\(failure.shortLabel)" }
    if entry.rows.isEmpty { return "empty:\(entry.selectedName ?? "all")" }
    return "rows:\(min(entry.rows.count, ProjectsEntry.rowLimit(for: .systemLarge)))"
}
check("unconfigured wins over everything",
      dispatch(ProjectsEntry(date: .now, configuration: nil, rows: rows,
                             selectedName: nil, status: .failed(.notConfigured))) == "setup")
check("a token problem says so",
      dispatch(ProjectsEntry(date: .now, configuration: nil, rows: [],
                             selectedName: nil, status: .failed(.unauthorized))) == "failure:Token rejected")
check("an honest empty names the scope",
      dispatch(ProjectsEntry(date: .now, configuration: nil, rows: [],
                             selectedName: "radio", status: .live)) == "empty:radio")
check("a selected project that vanished is the empty branch",
      dispatch(ProjectsEntry(date: .now, configuration: nil,
                             rows: ProjectsTimelineProvider.scope(rows, to: "gone"),
                             selectedName: "gone", status: .live)) == "empty:gone")
check("a stale entry draws its rows",
      dispatch(ProjectsEntry(date: .now, configuration: nil, rows: rows,
                             selectedName: nil, status: stale)) == "rows:3")
check("the large family crops 60 rows to 8",
      dispatch(ProjectsEntry(date: .now, configuration: nil,
                             rows: (0..<60).map { ProjectRow(name: "p\($0)", board: "b") },
                             selectedName: nil, status: .live)) == "rows:8")

// ---------------------------------------------------------------------------
print("")
if failures == 0 {
    print("PROJECTS WIDGET CHECK PASSED — family depths, status accessors, unconfigured-beats-stale, scope, §7 projection bytes")
    exit(0)
} else {
    print("PROJECTS WIDGET CHECK FAILED — \(failures) assertion(s)")
    exit(1)
}
