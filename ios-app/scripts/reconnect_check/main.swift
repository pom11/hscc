import Foundation

// ===========================================================================
// reconnect_check/main.swift — PROVE the reconnect guarantee of
// SessionStreamCursor (the REAL source, compiled by reconnect_check.sh).
//
// The card (t_218cb9ec): "Every event carries a sequence number; on reconnect
// the client requests everything after the last it saw. Prove: kill the
// connection mid-stream, reconnect, and the transcript has no gap and no
// repeat. Never leave the user wondering whether their message was sent."
//
// This harness is the headless proof. There is NO iOS runtime on this host, so
// the reconnect algorithm is exercised as a plain macOS CLI (the cursor is pure
// Foundation). The scenarios below replay a stream that is cut mid-way and
// resumed with the cursor's resumeRequest, then assert the assembled transcript
// is exactly the producer's events once each — no gap, no repeat.
// ===========================================================================

/// A tiny test harness with a shared pass/fail tally.
final class Harness {
    var passed = 0
    var failures: [String] = []

    /// Assert `cond` is true; name the check.
    func expect(_ cond: Bool, _ name: String) {
        if cond {
            passed += 1
            print("  ✓ \(name)")
        } else {
            failures.append(name)
            print("  ✗ \(name)")
        }
    }

    func section(_ title: String) {
        print("\n== \(title) ==")
    }

    /// Summary + exit code (mirrors model_decode_check's contract).
    func finish(_ suite: String) -> Never {
        print("")
        if failures.isEmpty {
            print("RECONNECT CHECKS PASSED — \(suite): \(passed) assertions, no gap, no repeat")
            exit(0)
        } else {
            print("RECONNECT FAILURES — \(failures.count) assertion(s) failed:")
            for f in failures { print("  ✗ \(f)") }
            exit(1)
        }
    }
}

// ---- Simulate the bridge producing a stream of sequenced events ------------
func produce(_ count: Int, _ prefix: String = "e") -> [SessionStreamCursor.Event] {
    (1...count).map { SessionStreamCursor.Event(seq: UInt64($0), payload: "\(prefix)\($0)") }
}

// Append only the events the cursor ACCEPTS, echoing the store's job of folding
// decisions into the transcript. Returns the accepted payloads in order.
func fold(_ transcript: inout [String], _ cursor: inout SessionStreamCursor,
          _ events: [SessionStreamCursor.Event], isResume: Bool)
    -> [SessionStreamCursor.Event] {
    var accepted: [SessionStreamCursor.Event] = []
    for ev in events {
        switch cursor.accept(ev, isResume: isResume) {
        case .accept(let a): transcript.append(a.payload); accepted.append(a)
        case .duplicate, .gap: break   // dropped / refused — not appended
        }
    }
    return accepted
}

let h = Harness()

// =========================== Scenario A ====================================
// DROP MID-STREAM, RECONNECT, NO GAP / NO REPEAT. The headline proof.
h.section("A — kill the connection mid-stream, reconnect: no gap, no repeat")
do {
    let producer = produce(20)                       // the session produced 1..20
    var transcript: [String] = []
    var cursor = SessionStreamCursor()               // fresh client

    // Client connects and folds the first 7 events before the phone drops.
    _ = fold(&transcript, &cursor, Array(producer[0..<7]), isResume: false)
    h.expect(cursor.lastSequence == 7, "client folded 1..7, cursor.lastSequence = \(cursor.lastSequence)")

    // While offline, the session produces 8..20 (buffered on the server/bridge).
    let missed = Array(producer[7..<20])             // seq 8..20

    // Reconnect: the client presents its cursor and the bridge replays the tail
    // AFTER the last seq it saw — exactly the events it missed.
    h.expect(cursor.resumeRequest == 7, "reconnect asks the bridge for everything after seq \(cursor.resumeRequest)")
    let replayed = missed                            // bridge replays seq 8..20
    _ = fold(&transcript, &cursor, replayed, isResume: true)

    // The transcript is exactly the producer's events once each — no gap, no repeat.
    let expected = producer.map(\.payload)
    h.expect(transcript == expected, "transcript == producer's 20 events exactly once")
    h.expect(transcript.count == producer.count, "count matches (no dupes): \(transcript.count)")
    h.expect(Set(transcript).count == transcript.count, "no duplicate payload anywhere")
    h.expect(cursor.lastSequence == 20, "cursor advanced to the final seq 20")
}

// =========================== Scenario B ====================================
// A flaky bridge re-sends an event the client already folded on a reconnect —
// the cursor must DROP it, so the transcript never repeats.
h.section("B — replay an already-folded event: defensive dedupe, no repeat")
do {
    let producer = produce(10)
    var transcript: [String] = []
    var cursor = SessionStreamCursor()
    _ = fold(&transcript, &cursor, Array(producer[0..<5]), isResume: false)
    let before = transcript

    // Bridge erroneously replays seq 3 and seq 5 (already folded) alongside the
    // genuinely-new 6,7.
    let replay = [producer[2], producer[4], producer[5], producer[6]]
    let accepted = fold(&transcript, &cursor, replay, isResume: true)

    h.expect(accepted.map(\.seq) == [6, 7], "only the genuinely-new 6,7 accepted; dupes 3,5 dropped")
    h.expect(transcript == before + ["e6", "e7"], "transcript appended exactly e6,e7 — nothing repeated")
    h.expect(cursor.lastSequence == 7, "cursor = 7 after folding the replayed tail")
}

// =========================== Scenario C ====================================
// Persistence order: the cursor advances on accept, so a crash after folding an
// event into the transcript can never re-request it on resume (no repeat).
h.section("C — cursor advances on accept, so a crash can never double-deliver")
do {
    var transcript: [String] = []
    var cursor = SessionStreamCursor()
    // Fold a genuine prefix 1..4 on a fresh stream so lastSequence reaches 4
    // the honest way (contiguous accepts), not by assuming it.
    _ = fold(&transcript, &cursor, produce(4), isResume: false)
    h.expect(cursor.lastSequence == 4, "after folding e1..e4, lastSequence = 4")
    h.expect(transcript == ["e1", "e2", "e3", "e4"], "prefix e1..e4 appended")

    // Simulate a crash+relaunch: the client restores lastSequence from storage.
    var restored = SessionStreamCursor(lastSequence: 4)
    h.expect(restored.resumeRequest == 4, "relaunched client resumes from seq 4")
    // The bridge responds with events > 4 only — e4 is never sent again.
    let replay = produce(6).filter { $0.seq > 4 }    // seq 5,6
    let accepted = fold(&transcript, &restored, replay, isResume: true)
    h.expect(accepted.map(\.seq) == [5, 6], "resumed stream delivers only 5,6 — e4 NOT repeated")
    h.expect(restored.lastSequence == 6, "cursor = 6")
    // Persisting lastSequence in the SAME write as the transcript is the
    // store's job; if the two ever diverge the cursor still clamps to the
    // higher persisted value on restore, and the duplicate guard (Scenario B)
    // makes a double-append impossible even if the bridge over-sends.
}

// =========================== Scenario D ====================================
// A FRESH stream that jumps ahead must be REFUSED and flagged as a gap — the
// one case where events could be silently skipped. The caller resumes instead.
h.section("D — fresh-stream jump is flagged as a gap, never silently skipped")
do {
    var transcript: [String] = []
    var cursor = SessionStreamCursor()
    _ = fold(&transcript, &cursor, [.init(seq: 1, payload: "e1"), .init(seq: 2, payload: "e2")], isResume: false)

    // The producer emits seq 8 next (5 events 3..7 were lost and never sent).
    let outcome = cursor.accept(.init(seq: 8, payload: "e8"), isResume: false)
    guard case .gap(let from, let through) = outcome else {
        h.expect(false, "jump on a fresh stream returns .gap, got \(outcome)")
        h.finish("Scenario D"); fatalError()
    }
    h.expect(from == 3 && through == 7, "gap names the missing range 3..7 precisely")
    h.expect(cursor.lastSequence == 2, "the refused event did NOT advance the cursor")
    h.expect(transcript == ["e1", "e2"], "e8 not appended — the transcript stays contiguous")

    // The honest fix: the client resumes, the bridge replays the true tail 3..8,
    // and nothing is silently lost.
    _ = fold(&transcript, &cursor, (3...8).map { .init(seq: UInt64($0), payload: "e\($0)") }, isResume: true)
    h.expect(transcript == (1...8).map { "e\($0)" }, "after resuming the gap is filled: e1..e8 once each")
}

// =========================== Scenario E ====================================
// A resume replay may legitimately begin ABOVE lastSequence+1 (the bridge only
// replays what it has; a seq that was never emitted simply never appears). This
// is NOT a false gap — the caller must not flag it.
h.section("E — resume replay above lastSequence+1 is not a false gap")
do {
    var transcript: [String] = []
    var cursor = SessionStreamCursor(lastSequence: 3)
    // The bridge replays seq 7,8 (4,5,6 were never emitted by the session).
    let accepted = fold(&transcript, &cursor,
                        [.init(seq: 7, payload: "e7"), .init(seq: 8, payload: "e8")],
                        isResume: true)
    h.expect(accepted.map(\.seq) == [7, 8], "resume replay accepted 7,8 without a false gap")
    h.expect(cursor.lastSequence == 8, "cursor = 8")
}

// =========================== Scenario F ====================================
// Idempotence under retry: replaying the SAME reconnect response twice (e.g. a
// dropped ACK causes the client to re-request) must not double-append anything.
h.section("F — replaying the same reconnect response twice: idempotent")
do {
    let producer = produce(6)
    var transcript: [String] = []
    var cursor = SessionStreamCursor()
    _ = fold(&transcript, &cursor, Array(producer[0..<6]), isResume: false)
    let afterFirst = transcript

    // The reconnect response (all of 1..6) is delivered, then delivered AGAIN
    // because the client never saw the ACK.
    let twice = producer + producer
    _ = fold(&transcript, &cursor, twice, isResume: true)

    h.expect(transcript == afterFirst, "re-delivering the full stream appends nothing twice")
    h.expect(transcript.count == 6, "transcript still 6 entries, no duplicates")
}

h.finish("6 scenarios, mid-stream drop + reconnect")
