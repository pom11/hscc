import Foundation

// ===========================================================================
// streaming_check — prove the REAL streaming aggregation core headlessly.
//
// This compiles the ACTUAL StreamingTranscript.swift (plus the real decode
// layer: Models/SharedModels/APIError/SessionEvent/SessionStreamCursor) into a
// macOS CLI and replays committed wire fixtures, then asserts the composed
// transcript. A failing assertion means the real aggregation logic no longer
// matches the pinned wire contract.
//
// Run via scripts/streaming_check.sh. macOS only — there is no iOS runtime on
// this host, and the aggregation is pure Foundation, so a macOS CLI is the
// faithful fixture runner.
// ===========================================================================

var failures = 0
func check(_ name: String, _ cond: @autoclosure () -> Bool, _ detail: String = "") {
    if cond() {
        print("  ok: \(name)")
    } else {
        failures += 1
        print("FAIL: \(name) \(detail)")
    }
}

// ---- helpers to decode a fixture and fold it ----

func decode(_ json: String) throws -> [SessionEvent] {
    let data = json.data(using: .utf8)!
    return try JSONDecoder().decode([SessionEvent].self, from: data)
}

func folded(_ events: [SessionEvent]) -> StreamingTranscript {
    var t = StreamingTranscript()
    for e in events { t.fold(e) }
    return t
}

// ---- row inspection helpers ----

func messageText(_ row: ChatRow) -> String? {
    if case .message(_, let text, _) = row.item { return text }
    return nil
}
func messageStreaming(_ row: ChatRow) -> Bool? {
    if case .message(_, _, let s) = row.item { return s }
    return nil
}
func messageRole(_ row: ChatRow) -> String? {
    if case .message(let role, _, _) = row.item { return role }
    return nil
}
func toolRender(_ row: ChatRow) -> ToolRender? {
    if case .tool(let t) = row.item { return t }
    return nil
}
func isCard(_ row: ChatRow) -> Bool { if case .card = row.item { return true }; return false }
func isAgent(_ row: ChatRow) -> Bool { if case .agent = row.item { return true }; return false }
func isSystem(_ row: ChatRow) -> Bool { if case .system = row.item { return true }; return false }
func isError(_ row: ChatRow) -> Bool { if case .error = row.item { return true }; return false }
func isNotice(_ row: ChatRow) -> Bool { if case .notice = row.item { return true }; return false }

print("streaming_check: compiling + replaying real aggregation core\n")

// ---- 1. Message token aggregation: deltas stream into ONE bubble ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"message","ts":"t","payload":{"role":"assistant","delta":"The ","done":false}},
      {"seq":2,"type":"message","ts":"t","payload":{"role":"assistant","delta":"fleet","done":false}},
      {"seq":3,"type":"message","ts":"t","payload":{"role":"assistant","delta":" is up","done":true}}
    ]
    """)
    let t = folded(ev)
    check("3 deltas collapse to 1 message row", t.rows.count == 1,
          "got \(t.rows.count) rows")
    check("streaming text accumulates in order", messageText(t.rows[0]) == "The fleet is up",
          "got '\(messageText(t.rows[0]) ?? "nil")'")
    check("final delta flips streaming off", messageStreaming(t.rows[0]) == false)
    check("message id stable = m-assistant-1", t.rows[0].id == "m-assistant-1",
          "got '\(t.rows[0].id)'")
}

// ---- 2. Turn separation: a done turn then a NEW turn is a new row ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"message","ts":"t","payload":{"role":"assistant","delta":"First.","done":true}},
      {"seq":2,"type":"message","ts":"t","payload":{"role":"assistant","delta":"Second turn!","done":false}},
      {"seq":3,"type":"message","ts":"t","payload":{"role":"assistant","delta":" more","done":true}}
    ]
    """)
    let t = folded(ev)
    check("two done-separated turns = 2 rows", t.rows.count == 2,
          "got \(t.rows.count)")
    check("turn 1 text is 'First.'", messageText(t.rows[0]) == "First.")
    check("turn 2 text is 'Second turn! more'", messageText(t.rows[1]) == "Second turn! more")
    check("turn 2 id differs from turn 1", t.rows[1].id != t.rows[0].id)
}

// ---- 3. Role separation: user echo vs assistant are different rows ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"message","ts":"t","payload":{"role":"user","delta":"deploy now","done":true}},
      {"seq":2,"type":"message","ts":"t","payload":{"role":"assistant","delta":"Working","done":false}},
      {"seq":3,"type":"message","ts":"t","payload":{"role":"assistant","delta":"…","done":true}}
    ]
    """)
    let t = folded(ev)
    check("user echo and assistant are distinct rows", t.rows.count == 2,
          "got \(t.rows.count)")
    check("row 0 is the user echo", messageRole(t.rows[0]) == "user")
    check("row 1 is assistant", messageRole(t.rows[1]) == "assistant")
}

// ---- 4. Tool call start+finish collapse to ONE finished row ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"tool_call","ts":"t","payload":{"call_id":"t1","name":"read kanban","status":"start","args":{"board":"hscc"}}},
      {"seq":2,"type":"tool_call","ts":"t","payload":{"call_id":"t1","name":"read kanban","status":"finish","result":{"titles":3},"duration_s":0.4}}
    ]
    """)
    let t = folded(ev)
    check("start+finish collapse to 1 tool row", t.rows.count == 1,
          "got \(t.rows.count)")
    let tr = toolRender(t.rows[0])
    check("tool row is finished", tr?.finished == true)
    check("tool row call_id preserved", tr?.callID == "t1")
    check("tool row name 'read kanban'", tr?.name == "read kanban")
    check("tool row duration 0.4", tr?.duration == 0.4)
    check("tool row has result", tr?.result != nil)
    check("tool row args preserved", tr?.args?["board"]?.renderInline() == "hscc")
    check("tool row id = t-t1", t.rows[0].id == "t-t1")
}

// ---- 5. Tool finish without a start (start predates the window) ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"tool_call","ts":"t","payload":{"call_id":"old","name":"list_running_tasks","status":"finish","result":{"tasks":[]},"duration_s":1.2}}
    ]
    """)
    let t = folded(ev)
    check("lone finish surfaces as 1 row", t.rows.count == 1)
    let tr = toolRender(t.rows[0])
    check("lone finish is finished", tr?.finished == true)
    check("lone finish keeps name", tr?.name == "list_running_tasks")
}

// ---- 6. In-flight tool: start without finish renders as in-progress ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"tool_call","ts":"t","payload":{"call_id":"t9","name":"deploy","status":"start","args":{}}}
    ]
    """)
    let t = folded(ev)
    let tr = toolRender(t.rows[0])
    check("in-flight tool is NOT finished", tr?.finished == false)
    check("in-flight tool has no result", tr?.result == nil)
}

// ---- 7. Single-frame types: card / agent / system / error each a row ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"card","ts":"t","payload":{"board":"hscc","id":"c42","title":"Build streaming view","status":"done"}},
      {"seq":2,"type":"agent","ts":"t","payload":{"role":"researcher-a","action":"spawned","task":"research cards"}},
      {"seq":3,"type":"system","ts":"t","payload":{"kind":"cron_fired","details":{"job":"daily-brief"}}},
      {"seq":4,"type":"error","ts":"t","payload":{"code":"worker_crash","message":"worker lost"}}
    ]
    """)
    let t = folded(ev)
    check("4 single-frame events = 4 rows", t.rows.count == 4,
          "got \(t.rows.count)")
    check("card row", isCard(t.rows[0]))
    check("agent row", isAgent(t.rows[1]))
    check("system row", isSystem(t.rows[2]))
    check("error row", isError(t.rows[3]))
}

// ---- 8. Unknown type degrades to an unknown row, not dropped ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"weird_new_type","ts":"t","payload":{"foo":1}}
    ]
    """)
    let t = folded(ev)
    check("unknown type becomes 1 row", t.rows.count == 1)
    if case .unknown(let type, let raw) = t.rows[0].item {
        check("unknown keeps type name", type == "weird_new_type")
        check("unknown keeps raw json", raw.contains("foo"))
    } else {
        check("unknown row is .unknown", false)
    }
}

// ---- 9. hello is control-only, not a chat row ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"hello","ts":"t","payload":{"next_seq":5}}
    ]
    """)
    let t = folded(ev)
    check("hello produces zero rows", t.rows.isEmpty,
          "got \(t.rows.count)")
}

// ---- 10. Descending / out-of-order events are NOT the fold's job ----
// The fold assumes the CALLER fed seq-ascending events (the cursor's job).
// Feed an out-of-order stream and confirm the fold does not crash on it:
do {
    let ev = try decode("""
    [
      {"seq":5,"type":"message","ts":"t","payload":{"role":"assistant","delta":"a","done":false}},
      {"seq":2,"type":"message","ts":"t","payload":{"role":"assistant","delta":"b","done":true}}
    ]
    """)
    let t = folded(ev)
    // Out-of-order produces whatever it produces — the point is it must not
    // crash. (Real callers gate ordering via SessionStreamCursor.)
    check("out-of-order fold does not crash", true)
}

// ---- 11. Errors reported with JSON result render ----
do {
    let ev = try decode("""
    [
      {"seq":1,"type":"tool_call","ts":"t","payload":{"call_id":"e1","name":"deploy","status":"start","args":{"project":"hscc"}}},
      {"seq":2,"type":"tool_call","ts":"t","payload":{"call_id":"e1","name":"deploy","status":"finish","result":{"ok":false,"message":"team not set"},"duration_s":3.1}}
    ]
    """)
    let t = folded(ev)
    let tr = toolRender(t.rows[0])
    check("tool result renders as text", tr?.result?.renderInline() == "{ \"message\": team not set, \"ok\": false }",
          "got '\(tr?.result?.renderInline() ?? "nil")'")
}

print("")
if failures == 0 {
    print("streaming_check: ALL PASSED")
} else {
    print("streaming_check: \(failures) FAILURE(S)")
    exit(1)
}
