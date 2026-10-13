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

// ===========================================================================
// Server-restart rebase (iOS-chat-e2e 2026-10-13)
// ===========================================================================

/// Pure decision: when a `hello` announces `serverNextSeq` but the client
/// cursor already sits at `clientLastSeq`, what should the cursor become?
///
/// The server's event store is IN-MEMORY (session_event.get_store): an API
/// restart rebuilds it from history and seq restarts from 1. A client whose
/// persisted/live cursor is at, say, 500 would then classify EVERY later
/// frame as `.duplicate` (seq <= lastSequence) — the stream would look
/// Connected while silently dropping everything (the "no replies ever come"
/// failure class, second variant). There is no way to align the old rows
/// with the new numbering, so the honest move is to accept everything the
/// new server emits from here on (cursor 0) and tell the operator the
/// window may show repeats. Returning nil means "no rebase needed".
///
/// Headlessly proven in scripts/reconnect_check.sh (compiles this file).
func cursorRebaseAfterHello(serverNextSeq: Int, clientLastSeq: Int) -> Int? {
    guard serverNextSeq >= 0, clientLastSeq > 0, serverNextSeq <= clientLastSeq else {
        return nil
    }
    // next_seq is the boundary where live frames BEGIN (first frame has
    // seq == next_seq... or > next_seq for a trimmed store). Rebasing to 0
    // accepts every frame the new numbering emits, including the boundary.
    return 0
}
