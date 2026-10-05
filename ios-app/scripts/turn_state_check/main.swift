// turn_state_check/main.swift — headless proof of t_7cc2e7d5 TurnState.derive.
//
// Feeds REAL wire JSON through the REAL SessionEvent decode layer
// (ParsedPayload.parse -> .system(SystemPayload)), then through the REAL
// TurnState.derive mapping the StreamingChatStore folds over live frames.
// Asserts the handoff-pending / working / session-busy terminal states the
// app must surface.
//
// This is the same "no iOS runtime on this host" pattern as streaming_check:
// the mapping is pure Foundation, so a macOS CLI is the faithful runner.
import Foundation

func decode(_ type: String, json: String) -> SessionEvent {
    let full = "{\"seq\":1,\"type\":\"\(type)\",\"ts\":\"2026-09-01T00:00:00Z\",\"payload\":\(json)}"
    let data = full.data(using: .utf8)!
    return try! JSONDecoder().decode(SessionEvent.self, from: data)
}

func system(_ kind: String) -> SessionEvent {
    decode("system", json: "{\"kind\":\"\(kind)\"}")
}

var failures = 0
func check(_ label: String, _ cond: Bool) {
    if cond { print("PASS  \(label)") }
    else { print("FAIL  \(label)"); failures += 1 }
}

let m = "handoff_pending"

// 1. Waiting-for-approval banner: a session_busy (CLI owner holds the named
//    session) that the coordinator escalates becomes handoff_pending.
check("session_busy by itself stays idle",
      TurnState.derive(from: system("session_busy"), current: .idle) == .idle)
var s = TurnState.derive(from: system("session_busy"), current: .idle)
s = TurnState.derive(from: system(m), current: s)
check("session_busy -> handoff_pending => awaitingApproval",
      s == .awaitingApproval)

// 2. Working indicator while the reply is generated.
s = TurnState.derive(from: system("working"), current: s)
check("handoff_pending -> working => working", s == .working)

// 3. An assistant reply (a real message, not a system frame) leaves the
//    working state untouched until the turn ends.
let msg = decode("message", json: "{\"role\":\"assistant\",\"content\":\"hi\"}")
check("assistant message keeps working",
      TurnState.derive(from: msg, current: .working) == .working)

// 4. Unknown / unrelated system kinds never move the needle.
check("unrelated system kind keeps current",
      TurnState.derive(from: system("cron_fired"), current: .working) == .working)

// 5. A fresh turn starts idle again.
s = TurnState.derive(from: system("session_busy"), current: .working)
check("session_busy closes working => idle", s == .idle)

print(failures == 0 ? "ALL TURN-STATE CHECKS PASSED" : "\(failures) FAILURES")
exit(failures == 0 ? 0 : 1)
