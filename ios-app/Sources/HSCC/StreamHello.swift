import Foundation

// ===========================================================================
// StreamHello — pure detection of the server's opening `hello` envelope.
//
// Extracted from StreamingChatStore for the same reason SessionStreamCursor
// and StreamConnectionError exist as separate files: it is the decision that
// flips the UI to "Live", and it must be provable headlessly against REAL
// captured frames (scripts/ws_decode_check.sh), not eyeballed in a view.
//
// THE bug this shape prevents (iOS-chat-e2e 2026-10-13): the store used to
// match `kind == "hello"`, but the server's hello is
// `{"seq":N,"type":"hello","ts":...,"payload":{"next_seq":N}}` — no `kind`
// field on any SERVER→client frame (client→server send/stop frames carry
// `kind`, which is what the confusion was). The check never matched: the
// banner sat on "Connecting to the live stream…" while frames streamed in,
// and the hello fell through to the seq-cursor where its seq (= boundary)
// looked like a gap on a fresh/trimmed store → reconnect churn. Accepting
// BOTH tag fields makes the check true under either envelope convention.
// ===========================================================================

/// True when `text` is a JSON object announcing itself as the `hello`
/// envelope, under either the `type` (real wire) or `kind` (legacy/hypothetical)
/// field convention. Cheap by design: runs on every inbound frame.
func isHelloEnvelope(_ text: String) -> Bool {
    guard text.first == "{",
          let data = text.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return false
    }
    let tag = (obj["type"] as? String) ?? (obj["kind"] as? String)
    return tag == "hello"
}

/// The `next_seq` a hello announces (the boundary where live frames begin),
/// when present and numeric. Callers use it for diagnostics/resume sanity
/// checks; absence is not an error.
func helloNextSeq(_ text: String) -> Int? {
    guard text.first == "{",
          let data = text.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          ((obj["type"] as? String) ?? (obj["kind"] as? String)) == "hello",
          let payload = obj["payload"] as? [String: Any] else {
        return nil
    }
    return payload["next_seq"] as? Int
}
