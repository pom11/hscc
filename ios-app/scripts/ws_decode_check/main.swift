// ws_decode_check — decode one captured-frames fixture (JSON Lines, one
// frame envelope per line) through the SHIPPED decoder and assert shape.
//
// This exists because the stuck-banner bug (iOS-chat-e2e 2026-10-13) was a
// decode-mismatch class failure the synthetic-fixture tests could not see:
// the server's hello envelope is {"seq":N,"type":"hello",...} while the
// client's isHelloFrame probed the field "kind". Feeding VERBATIM frames
// captured from the live API through the exact production decoder
// (JSONDecoder().decode(SessionEvent.self), the same call StreamingChatStore
// makes) is the only check that catches this class of bug.
//
// Usage: ws_decode_check <fixture.jsonl>
import Foundation

var failures: [String] = []
func check(_ cond: Bool, _ what: String) {
    if !cond { failures.append(what) }
}

let args = CommandLine.arguments
guard args.count == 2 else {
    FileHandle.standardError.write(Data("usage: ws_decode_check <fixture.jsonl>\n".utf8))
    exit(2)
}
guard let raw = try? String(contentsOfFile: args[1], encoding: .utf8) else {
    FileHandle.standardError.write(Data("cannot read \(args[1])\n".utf8))
    exit(2)
}

var frames: [String] = []
for line in raw.split(separator: "\n") where !line.trimmingCharacters(in: .whitespaces).isEmpty {
    frames.append(String(line))
}
check(!frames.isEmpty, "fixture has no frames")

var sawHello = false
var sawUserEcho = 0
var sawAssistant = 0
var sawWorking = 0
var sawToolCallStart = 0
var sawToolCallFinish = 0
var sawReasoning = 0
var seqs: [Int] = []
var helloCount = 0

let decoder = JSONDecoder()

for (idx, line) in frames.enumerated() {
    let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
    let typeField = obj.flatMap { $0["type"] as? String }
    // The detection predicate used by StreamingChatStore must agree with the
    // wire shape: hello iff type == "hello" (never a "kind" field).
    check(isHelloEnvelope(line) == (typeField == "hello"),
          "\(idx): isHelloEnvelope disagrees with the type field")
    if typeField == "hello" {
        sawHello = true
        helloCount += 1
        continue   // hello is control framing consumed before the event path
    }
    guard let event = try? decoder.decode(SessionEvent.self, from: Data(line.utf8)) else {
        failures.append("\(idx): LIVE FRAME FAILED TO DECODE: \(String(line.prefix(140)))")
        continue
    }
    seqs.append(event.seq)
    switch event.payload {
    case .message(let p):
        if p.role == "user" { sawUserEcho += 1 }
        if p.role == "assistant" { sawAssistant += 1 }
    case .system(let p):
        if p.kind == "working" { sawWorking += 1 }
        if p.kind == "reasoning" { sawReasoning += 1 }
    case .toolCall(let p):
        if p.status == "start" { sawToolCallStart += 1 }
        if p.status == "finish" { sawToolCallFinish += 1 }
    case .unknown(let t, _):
        failures.append("\(idx): known wire type decoded as .unknown (\(t)): \(String(line.prefix(120)))")
    default:
        break
    }
}

check(sawHello, "no hello envelope in fixture")
check(helloCount == 1, "expected exactly one hello, got \(helloCount)")
check(sawUserEcho >= 1, "no user echo decoded")
check(sawAssistant >= 1, "no assistant reply decoded")

// The rich fixture (a full tool turn) additionally proves working +
// tool_call + reasoning decode; the plain replay fixture may legitimately
// contain none of those.
if args[1].contains("turn_roundtrip") {
    check(sawWorking >= 1, "no working frame decoded")
    check(sawToolCallStart >= 1, "tool_call start not decoded")
    check(sawToolCallFinish >= 1, "tool_call finish not decoded")
    check(sawReasoning >= 1, "reasoning frame not decoded")
}

// Replay must be in seq order (store emits monotonic seqs; the client
// relies on contiguity for its gap check).
for i in 1..<seqs.count {
    check(seqs[i] > seqs[i-1], "seq order broken at \(i): \(seqs[i-1]) -> \(seqs[i])")
}

let name = (args[1] as NSString).lastPathComponent
if failures.isEmpty {
    print("ok \(name): \(frames.count) frames — hello, \(sawUserEcho) user, \(sawAssistant) assistant, \(sawWorking) working, \(sawToolCallStart) tool_start, \(sawToolCallFinish) tool_finish, \(sawReasoning) reasoning")
    exit(0)
} else {
    print("FAIL \(name):")
    for f in failures { print("  - \(f)") }
    exit(1)
}
