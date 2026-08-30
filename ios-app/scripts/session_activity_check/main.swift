import Foundation

// ===========================================================================
// session_activity_check — prove the session Live Activity DERIVATION headlessly.
//
// This compiles the ACTUAL SessionActivitySummary.swift (plus the real decode +
// aggregation layer: Models/SharedModels/APIError/SessionEvent/SessionStreamCursor/
// StreamingTranscript) into a macOS CLI and replays committed session_event wire
// fixtures, then asserts the derived Live Activity phase/headline/detail.
//
// The rows handed to `SessionActivitySummary.make` are the PRODUCT of decoding
// real session_event JSON and folding it through the real StreamingTranscript —
// so the lock screen summary is proven to be a faithful mirror of the wire, not
// an invented shape. This is the "decode against session_event.py rather than
// inventing shapes" guarantee applied to the Live Activity.
//
// Run via scripts/session_activity_check.sh. macOS only — no iOS runtime here.
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

// ---- decode + fold helpers (real wire -> real rows) ----

func decodeEvents(_ json: String) throws -> [SessionEvent] {
    let data = json.data(using: .utf8)!
    return try JSONDecoder().decode([SessionEvent].self, from: data)
}

func folded(_ events: [SessionEvent]) -> [ChatRow] {
    var t = StreamingTranscript()
    for e in events { t.fold(e) }
    return t.rows
}

// Each fixture is an ARRAY of session events (the /streams/events shape), so
// they decode straight into [SessionEvent].

// A full assistant turn that streams then settles.
let turnFixture = """
[
  {"seq":1,"type":"message","ts":"2026-08-29T09:01:00Z","payload":{"role":"user","delta":"What is the cluster status?","done":true}},
  {"seq":2,"type":"tool_call","ts":"2026-08-29T09:01:03Z","payload":{"call_id":"c1","name":"cluster_status","status":"start","args":{"project":"hscc"}}},
  {"seq":3,"type":"message","ts":"2026-08-29T09:01:05Z","payload":{"role":"assistant","delta":"The cluster","done":false}},
  {"seq":4,"type":"message","ts":"2026-08-29T09:01:06Z","payload":{"role":"assistant","delta":" is healthy.","done":true}}
]
"""

// A card + agent + system + error tail (distinct single-frame rows).
let tailFixture = """
[
  {"seq":11,"type":"card","ts":"2026-08-29T09:05:00Z","payload":{"board":"hscc","id":"t_1","title":"Implement pager","status":"running"}},
  {"seq":12,"type":"agent","ts":"2026-08-29T09:05:02Z","payload":{"role":"ios-engineer","action":"spawned","task":"implement pager"}},
  {"seq":13,"type":"system","ts":"2026-08-29T09:06:00Z","payload":{"kind":"cron","details":{"job":"daily-briefing"}}},
  {"seq":14,"type":"error","ts":"2026-08-29T09:07:00Z","payload":{"code":"E_BACKEND","message":"Daemon parse failed"}}
]
"""

// A live stream with an in-flight tool call as the last row.
let inflightToolFixture = """
[
  {"seq":21,"type":"tool_call","ts":"2026-08-29T09:10:00Z","payload":{"call_id":"c2","name":"read_kanban","status":"start","args":{"board":"hscc"}}}
]
"""

// ===========================================================================
// checks
// ===========================================================================

print("— helper: a settled user message folds and derives as 'done'/'said' —")
do {
    let rows = try folded(decodeEvents(turnFixture))
    // Last row is the settled assistant message.
    guard let s = SessionActivitySummary.make(rows: rows, phase: .connected) else {
        check("summary not nil", false, "expected a summary for folded rows"); throw NSError(domain: "x", code: 1)
    }
    check("phase is 'done'", s.phase == "done", "got \(s.phase)")
    check("headline is 'reply ready'", s.headline == "reply ready", "got \(s.headline)")
    check("detail carries the tail", s.detail == "The cluster is healthy.", "got \(s.detail)")
} catch { check("turn scenario decoded+folded", false, "\(error)") }

print("— helper: a MID-STREAM assistant message (streaming=true last) is a live 'replying…' —")
do {
    let rows = try folded(decodeEvents(
        "[{\"seq\":1,\"type\":\"message\",\"ts\":\"2026-08-29T09:01:00Z\",\"payload\":{\"role\":\"assistant\",\"delta\":\"portion two\",\"done\":false}}]"
    ))
    guard let s = SessionActivitySummary.make(rows: rows, phase: .connected) else {
        check("summary not nil", false, "expected streaming summary"); throw NSError(domain: "x", code: 1)
    }
    check("phase is 'streaming'", s.phase == "streaming", "got \(s.phase)")
    check("headline is 'replying…'", s.headline == "replying…", "got \(s.headline)")
} catch { check("streaming scenario decoded+folded", false, "\(error)") }

print("— helper: in-flight tool call is a live 'tool' —")
do {
    let rows = try folded(decodeEvents(inflightToolFixture))
    guard let s = SessionActivitySummary.make(rows: rows, phase: .connected) else {
        check("summary not nil", false, "expected tool summary"); throw NSError(domain: "x", code: 1)
    }
    check("phase is 'tool'", s.phase == "tool", "got \(s.phase)")
    check("headline names the tool", s.headline == "tool: read_kanban", "got \(s.headline)")
    check("detail is 'running…'", s.detail == "running…", "got \(s.detail)")
} catch { check("inflight-tool scenario decoded+folded", false, "\(error)") }

print("— helper: card/agent/system/error rows derive to the right phase —")
do {
    let rows = try folded(decodeEvents(tailFixture))
    // Rows: card, agent, system, error. The LAST row is the error.
    guard let s = SessionActivitySummary.make(rows: rows, phase: .connected) else {
        check("summary not nil", false, "expected error summary"); throw NSError(domain: "x", code: 1)
    }
    check("error last -> phase 'error'", s.phase == "error", "got \(s.phase)")
    check("error headline", s.headline == "error", "got \(s.headline)")
    check("error detail is the message", s.detail == "Daemon parse failed", "got \(s.detail)")

    // Subset: card first is done/card.
    let cardRows = Array(rows.prefix(1))
    if let c = SessionActivitySummary.make(rows: cardRows, phase: .connected) {
        check("card -> phase 'done'", c.phase == "done", "got \(c.phase)")
        check("card headline", c.headline == "card running", "got \(c.headline)")
        check("card detail is the title", c.detail == "Implement pager", "got \(c.detail)")
    } else { check("card summary not nil", false) }
} catch { check("tail scenario decoded+folded", false, "\(error)") }

print("— helper: no rows + not live -> nil (nothing honest to mirror) —")
if let s = SessionActivitySummary.make(rows: [], phase: .idle) {
    check("nil when idle+empty", false, "expected nil, got \(s)")
} else { check("nil when idle+empty", true) }

print("— helper: no rows but LIVE -> honest 'listening…' idle —")
if let s = SessionActivitySummary.make(rows: [], phase: .connected) {
    check("listening phase", s.phase == "idle", "got \(s.phase)")
    check("listening headline", s.headline == "listening…", "got \(s.headline)")
} else { check("listening summary", false, "expected a summary while live") }

print("— helper: activityCount round-trips through every derivation —")
if let s = SessionActivitySummary.make(rows: try folded(decodeEvents(turnFixture)), phase: .connected, activityCount: 7) {
    check("activityCount carried", s.activityCount == 7, "got \(s.activityCount)")
} else { check("activityCount summary", false) }

// ===========================================================================
print("")
if failures == 0 {
    print("session_activity_check: all checks passed (\(failures) failures)")
    exit(0)
} else {
    print("session_activity_check: \(failures) failure(s)")
    exit(1)
}
