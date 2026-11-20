import Foundation

// ---------------------------------------------------------------------------
// carplay_check/main.swift — executes the CarPlay reading rules against fake
// hosts (192.0.2.x) and fixtures. Compiled by scripts/carplay_check.sh with the
// REAL CarPlayReading.swift and the REAL shared readers; nothing is
// transcribed, so a changed temperature band or row sentence fails here.
// ---------------------------------------------------------------------------

var checks = 0
var failures = 0

func check(_ condition: Bool, _ name: String) {
    checks += 1
    if condition {
        print("  ok: \(name)")
    } else {
        failures += 1
        print("FAIL: \(name)")
    }
}

func equal(_ actual: String, _ expected: String, _ name: String) {
    check(actual == expected, "\(name) — got \"\(actual)\", want \"\(expected)\"")
}

let now = Date()
let iso = ISO8601DateFormatter()

// MARK: Fake fleet (fake hosts only: 192.0.2.0/24)

func sample(_ name: String, util: String, temp: String, power: String) -> MonitorSample {
    MonitorSample(hostname: name, gpu_util_pct: util, gpu_temp_c: temp,
                  gpu_power_w: power)
}

let hosts = [
    MonitorNode(host: "192.0.2.11", error: nil,
                sample: sample("S1", util: "34", temp: "61", power: "120"),
                used_slots: 1, free_slots: 2, workloads: nil),
    MonitorNode(host: "192.0.2.12", error: nil,
                sample: sample("S2", util: "81", temp: "68", power: "240"),
                used_slots: 1, free_slots: 2, workloads: nil),
    MonitorNode(host: "192.0.2.13", error: nil,
                sample: sample("S3", util: "92", temp: "72", power: "260"),
                used_slots: 1, free_slots: 2, workloads: nil),
    MonitorNode(host: "192.0.2.14", error: nil,
                sample: sample("S4", util: "12", temp: "58", power: "80"),
                used_slots: 0, free_slots: 2, workloads: nil),
]

func serving(_ node: String, _ model: String, tps: Double) -> ServingRow {
    ServingRow(node: node, port: "8000", model: model, routes: nil,
               running: 1, waiting: 0, kv: nil, gen_tps: tps,
               prompt_tps: nil, prompt_total: nil, gen_total: nil, error: nil)
}

let servingRows = [
    serving("192.0.2.11", "hscc/orchestrator-model", tps: 41),
    serving("192.0.2.12", "qwen/qwen3.8-flash-next", tps: 88),
    serving("192.0.2.13", "qwen/qwen3.8-flash-next", tps: 102),
    serving("192.0.2.14", "qwen/qwen3.8-flash-next", tps: 0),
]

let monitor = MonitorResponse(
    success: true,
    error: nil,
    json: MonitorPayload(timestamp: now.timeIntervalSince1970,
                         hosts: hosts),
    serving: servingRows,
    speak: "")

print("cluster row")
if let cluster = CarPlayClusterReading(response: monitor, fetchedAt: now) {
    check(cluster.gauges.count == 4, "four gauges")
    equal(cluster.headline, "4/4 up", "headline")
    equal(cluster.detailLine, "4/4 up · 231 t/s", "detail")
    equal(cluster.gauges[0].detailLine, "S1 · 34% · 41 t/s", "subtitle-equivalent")
    equal(cluster.gauges[0].rowTitle, "S1 ◆", "orchestrator mark")
    equal(cluster.gauges[1].rowTitle, "S2", "no invented orchestrator mark")
    equal(cluster.gauges[2].tempText, "72°", "temp text")
    check(cluster.gauges[2].tempBand == .warn, "72 °C is warn")
    check(cluster.gauges[1].tempBand == .ok, "68 °C is ok")
    check(cluster.hottest?.label == "S3", "hottest node")
    equal(cluster.modelLine, "orchestrator-model ×1 · qwen3.8-flash-next ×3",
          "model line")
} else {
    check(false, "cluster reading built")
}

print("temperature bands")
check(CarPlayTemperature.band(69.9) == .ok, "69.9 ok")
check(CarPlayTemperature.band(70) == .warn, "70 warn")
check(CarPlayTemperature.band(74.9) == .warn, "74.9 warn")
check(CarPlayTemperature.band(75) == .bad, "75 bad")
check(CarPlayTemperature.band(nil) == .unknown, "nil unknown")

// MARK: Boards

let summaries = MobileSummaryResponse(
    generated_at: iso.string(from: now),
    boards: [
        MobileSummaryBoard(board: "hscc",
                           counts: BoardStatusCounts(todo: 2, ready: 0, running: 2,
                                                     blocked: 7, done: 60,
                                                     archived: 0, other: nil),
                           total: 71, error: nil),
        MobileSummaryBoard(board: "flows",
                           counts: BoardStatusCounts(todo: 0, ready: 0, running: 0,
                                                     blocked: 17, done: 17,
                                                     archived: 0, other: nil),
                           total: 34, error: nil),
        MobileSummaryBoard(board: "display",
                           counts: BoardStatusCounts(todo: 0, ready: 0, running: 0,
                                                     blocked: 10, done: 12,
                                                     archived: 0, other: nil),
                           total: 22, error: nil),
        MobileSummaryBoard(board: "gateclerk",
                           counts: BoardStatusCounts(todo: 0, ready: 0, running: 0,
                                                     blocked: 2, done: 0,
                                                     archived: 0, other: nil),
                           total: 2, error: nil),
    ],
    board_count: 4, total_boards: 4, truncated: false, omitted_boards: 0,
    unreadable_boards: 0, max_in_progress: 6, speak: "")

print("boards row")
if let boards = CarPlayBoardsReading(summary: summaries) {
    equal(boards.detailLine, "2 running · 36 blocked · 89 done 24h", "detail")
    check(boards.topBoards.map(\.board) == ["flows", "hscc", "display"],
          "top three by open count")
    check(boards.topBoards[1].segments.map(\.count) == [2, 2, 7],
          "stacked segments running/todo/blocked")
    check(boards.maxOpen == 17, "bar scale is the largest open count")
} else {
    check(false, "boards reading built")
}

// MARK: Running

let runningPayload = KanbanRunningLite(
    tasks: [
        RunningCardLite(board: "hscc", id: "t_fdc2af6c",
                       title: "/v1/health serves a cached verify snapshot",
                       worker: "ios-engineer", assignee: nil, status: "running",
                       run_s: 1_920, hb_s: 120, started_at: nil),
        RunningCardLite(board: "hscc", id: "t_other",
                       title: "Second card", worker: "reviewer",
                       assignee: nil, status: "running",
                       run_s: 600, hb_s: 600, started_at: nil),
    ],
    count: 2, speak: "", boards: nil)

let runningProjection = CardsRunningProjection(running: runningPayload,
                                               blocked: nil, now: now)

print("running row")
let running = CarPlayRunningReading(projection: runningProjection, fetchedAt: now)
equal(running.imageText, "32m", "longest elapsed")
equal(running.detailLine, "/v1/health serves a cached verify snapshot",
      "detail is the longest title")
equal(running.longest?.cardID ?? "", "t_fdc2af6c", "longest card first")
check(CarPlayDuration.elapsed(3_900) == "1h 05m", "1h 05m")
check(CarPlayDuration.elapsed(45) == "45s", "45s")
check(CarPlayDuration.heartbeat(120) == .fresh(seconds: 120), "2m beat is fresh")
check(CarPlayDuration.heartbeat(600).isLate, "10m beat is late")
equal(CarPlayDuration.heartbeatText(.late(seconds: 600)), "beat 10m",
      "late heartbeat copy")
equal(CarPlayRunningReading(projection: CardsRunningProjection(
    rows: [], runningCount: 0, blockedCount: 0, rowsAreComplete: true),
    fetchedAt: now).detailLine,
    "Nothing running", "calm empty state")

// MARK: Needs me

print("needs row")
let clear = CarPlayNeedsReading(feed: .empty, failed: false, fetchedAt: now)
check(clear.isClear, "clear only after a successful fetch")
equal(clear.detailLine, "All clear", "clear copy")
equal(clear.imageText, "✓", "green check")

let failed = CarPlayNeedsReading(feed: .empty, failed: true, fetchedAt: now)
check(!failed.isClear, "a failed fetch is not clear")
equal(failed.detailLine, "offline", "failed detail")
equal(failed.imageText, "—", "failed image")

let alerts = AlertsLite(
    alerts: [
        AlertLite(id: 2, ts: iso.string(from: now.addingTimeInterval(-120)),
                  title: "Approve the 2.17 release notes before the tag.",
                  body: "the tag is waiting", level: "warn", key: "flows",
                  acked: false, ackedAt: nil),
        AlertLite(id: 1, ts: iso.string(from: now.addingTimeInterval(-600)),
                  title: "A worker stopped", body: nil, level: "info",
                  key: "hscc", acked: false, ackedAt: nil),
    ],
    count: 2, latestID: 2, hasMore: false, nextSince: 2, speak: "")

let open = CarPlayNeedsReading(feed: NeedsMeFeed.from(alerts), failed: false,
                               fetchedAt: now)
equal(open.imageText, "2", "open count")
equal(open.countText, "2 need you", "count copy")
equal(open.detailLine, "Approve the 2.17 release notes before the tag.",
      "top alert title")
check(!open.isClear, "open items are not clear")
check(CarPlayFreshness.label(dataAt: open.dataAt, fetchedAt: now,
                             failed: false, now: now) == nil,
      "an old alert fetched now is fresh data")

// MARK: Merge queue

print("merge queue row")
let gating = MergeQueueLite(
    ok: true, generatedAt: iso.string(from: now),
    rows: [
        MergeQueueRowLite(project: "hscc", position: 1, pr: 485, card: "t_x",
                          branch: "wt/x", state: "gating", ageS: 90,
                          lastResult: nil, detail: nil, isLast: false),
        MergeQueueRowLite(project: "hscc", position: 2, pr: 486, card: "t_y",
                          branch: "wt/y", state: "queued", ageS: 30,
                          lastResult: nil, detail: nil, isLast: false),
        MergeQueueRowLite(project: "hscc", position: 3, pr: 487, card: "t_z",
                          branch: "wt/z", state: "queued", ageS: 20,
                          lastResult: nil, detail: nil, isLast: false),
    ],
    count: 3, queued: 2,
    projects: ["hscc": MergeQueueProjectLite(enabled: true, paused: false,
                                             queue_len: 3, repo: "hscc")],
    speak: "")

let queue = CarPlayQueueReading(feed: MergeQueueFeed.from(gating))
equal(queue.detailLine, "#485 in gates · 2 waiting", "queue detail")
check(queue.stageProgress == 1, "gating is stage 1")
equal(queue.stageLabels.joined(separator: "/"), "rebase/gates/push/merge",
      "stage vocabulary")

let off = MergeQueueLite(
    ok: true, generatedAt: iso.string(from: now), rows: [], count: 0, queued: 0,
    projects: ["hscc": MergeQueueProjectLite(enabled: false, paused: false,
                                             queue_len: 0, repo: "hscc")],
    speak: "")
equal(CarPlayQueueReading(feed: MergeQueueFeed.from(off)).detailLine,
      "Merge queue off for hscc", "off is not an empty queue")

let empty = MergeQueueLite(
    ok: true, generatedAt: iso.string(from: now), rows: [], count: 0, queued: 0,
    projects: ["hscc": MergeQueueProjectLite(enabled: true, paused: false,
                                             queue_len: 0, repo: "hscc")],
    speak: "")
equal(CarPlayQueueReading(feed: MergeQueueFeed.from(empty)).detailLine,
      "Merge queue empty", "enabled empty queue")

let unavailable = MergeQueueLite(
    ok: false, generatedAt: nil, rows: [], count: nil, queued: nil,
    projects: nil, speak: "")
equal(CarPlayQueueReading(feed: MergeQueueFeed.from(unavailable)).detailLine,
      "offline", "ok:false is offline, not empty")

// MARK: Freshness and the never-fresh rule

print("freshness")
check(CarPlayFreshness.label(dataAt: now.addingTimeInterval(-30),
                             fetchedAt: now, failed: false, now: now) == nil,
      "30 s old is fresh")
equal(CarPlayFreshness.label(dataAt: now.addingTimeInterval(-90),
                             fetchedAt: now, failed: false, now: now) ?? "",
      "stale · " + CarPlayFreshness.hhmm(now.addingTimeInterval(-90)),
      "stale after 2x the refresh")
equal(CarPlayFreshness.label(dataAt: now.addingTimeInterval(-90),
                             fetchedAt: now, failed: true, now: now) ?? "",
      "offline · " + CarPlayFreshness.hhmm(now.addingTimeInterval(-90)),
      "failure says offline")
equal(CarPlayFreshness.headerLabel(dataAt: now, fetchedAt: now,
                                  failed: false, now: now),
      "as of " + CarPlayFreshness.hhmm(now), "header is the data's clock")

var state = CarPlayState()
state.applyNeeds(clear, failed: false, now: now)
equal(state.needsDetail, "All clear", "clear state")
state.applyNeeds(nil, failed: true, now: now)
check(!state.needsDetail.contains("All clear"),
      "never All clear after a failed fetch")
check(state.needsDetail.contains("offline"), "failed Needs says offline")
equal(state.needsEmptyText, "offline", "empty detail row is offline, not clear")

let staleMonitor = MonitorResponse(
    success: true, error: nil,
    json: MonitorPayload(timestamp: nil, hosts: hosts),
    serving: servingRows, speak: "")
let staleCluster = CarPlayClusterReading(response: staleMonitor,
                                         fetchedAt: now.addingTimeInterval(-120))
state.applyCluster(staleCluster, failed: false, now: now)
check(state.headerLabel.hasPrefix("stale · "), "header goes stale")
state.applyCluster(nil, failed: true, now: now)
check(state.headerLabel.hasPrefix("offline · "), "header goes offline")
check(state.clusterDimmed, "offline cluster is dimmed")
state.applyCluster(staleCluster, failed: false, now: now)
check(state.clusterDimmed, "stale cluster is dimmed")
state.applyCluster(CarPlayClusterReading(response: monitor, fetchedAt: now),
                   failed: false, now: now)
check(!state.clusterDimmed, "fresh cluster is not dimmed")

print("")
if failures == 0 {
    print("CARPLAY READING: \(checks) checks passed")
    exit(0)
} else {
    print("CARPLAY READING: \(failures) of \(checks) checks FAILED")
    exit(1)
}
