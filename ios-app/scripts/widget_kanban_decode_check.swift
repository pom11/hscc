import Foundation

// Mirror of the Lite decoders added to SharedModels.swift for the widget, to
// prove the decode + queueDepth derivation logic against synthetic JSON shaped
// exactly like the documented HSCC API routes (routes_kanban.py).

struct KanbanRunningLite: Decodable {
    let boards: [String]?
    let tasks: [RunningCardLite]?
    let errors: [String]?
    let count: Int?
    let speak: String?
}
struct RunningCardLite: Decodable {
    let board: String?; let id: String; let title: String?; let assignee: String?
    let status: String?; let pid: Int?; let host_local: Bool?; let started_at: String?
}
struct KanbanBlockedLite: Decodable {
    let boards: Int?; let tasks: [BlockedCardLite]?; let errors: [String]?
    let count: Int?; let speak: String?
}
struct BlockedCardLite: Decodable {
    let board: String?; let id: String; let status: String?; let assignee: String?
    let age_days: Int?; let block_kind: String?; let title: String?
}
struct KanbanStaleLite: Decodable {
    let boards: [String]?; let tasks: [StaleCardLite]?; let errors: [String]?
    let older_than: Int?; let count: Int?; let speak: String?
}
struct StaleCardLite: Decodable {
    let board: String?; let id: String; let status: String?; let assignee: String?
    let age_days: Int?; let title: String?
}

// Mirror of ClusterWidget.queueDepth(stale:)
func queueDepth(stale: KanbanStaleLite?) -> Int? {
    guard let tasks = stale?.tasks else { return nil }
    return tasks.reduce(0) { acc, card in
        guard let s = card.status?.lowercased() else { return acc }
        return (s == "ready" || s == "todo") ? acc + 1 : acc
    }
}

let decoder = JSONDecoder()

let runningJSON = """
{"boards":["hscc"],"tasks":[{"board":"hscc","id":"t_123","title":"build widget","assignee":"ios-engineer","status":"running","pid":4112,"host_local":true,"started_at":"2026-09-04T00:10:00Z"}],"errors":[],"count":1}
"""
let blockedJSON = """
{"boards":2,"tasks":[{"board":"hscc","id":"t_9","status":"blocked","assignee":"planner","age_days":3,"block_kind":"needs_input","title":"stuck thing"}],"errors":[],"count":1}
"""
let staleJSON = """
{"boards":["hscc"],"tasks":[
  {"board":"hscc","id":"t_1","status":"running","assignee":"ios","age_days":0,"title":"a"},
  {"board":"hscc","id":"t_2","status":"ready","assignee":"planner","age_days":0,"title":"b"},
  {"board":"hscc","id":"t_3","status":"todo","assignee":"qa","age_days":1,"title":"c"},
  {"board":"hscc","id":"t_4","status":"ready","assignee":"dev","age_days":2,"title":"d"},
  {"board":"hscc","id":"t_5","status":"in_progress","assignee":"dev","age_days":0,"title":"e"},
  {"board":"hscc","id":"t_6","status":"blocked","assignee":"planner","age_days":1,"title":"f"}
],"errors":[],"older_than":0,"count":6}
"""

let running = try! decoder.decode(KanbanRunningLite.self, from: Data(runningJSON.utf8))
let blocked = try! decoder.decode(KanbanBlockedLite.self, from: Data(blockedJSON.utf8))
let stale = try! decoder.decode(KanbanStaleLite.self, from: Data(staleJSON.utf8))

print("running count = \(running.count ?? -1) (expect 1)")
print("blocked count = \(blocked.count ?? -1) (expect 1)")
let qd = queueDepth(stale: stale) ?? -1
print("queueDepth = \(qd) (expect 3 — t_2 ready + t_3 todo + t_4 ready; running/in_progress/blocked excluded)")

// nil-tasks must yield nil (widget omits the metric), not 0
let emptyStale = try! decoder.decode(KanbanStaleLite.self, from: Data(#"{"tasks":null}"#.utf8))
print("queueDepth(nil tasks) = \(String(describing: queueDepth(stale: emptyStale))) (expect nil)")

guard running.count == 1, blocked.count == 1, qd == 3, queueDepth(stale: emptyStale) == nil else {
    print("FAIL: unexpected decode/derivation result")
    exit(1)
}
print("✅ Kanban Lite models + queueDepth derivation decode correctly.")
