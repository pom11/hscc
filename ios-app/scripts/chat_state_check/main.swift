// chat_state_check/main.swift — PROVE ChatStore's honest terminal-state machine
// (card t_c0953d4c). Compiled by scripts/chat_state_check.sh together with the
// REAL ChatStore.swift and REAL ChatEntry (sliced from OrchestratorChatView.swift);
// nothing here redeclares app logic. Uses a unique project per group so
// UserDefaults persistence is isolated across scenarios.
import Foundation

@MainActor
final class Harness {
    static func run() -> Bool {
        var ok = true
        func check(_ cond: Bool, _ label: String) {
            print("\(cond ? "PASS" : "FAIL"): \(label)")
            if !cond { ok = false }
        }

        // ---- 1. Delivery failure -> UNSENT, never discarded ----
        let a = ChatStore(project: "proj_unsent_test")
        a.beginSend(prompt: "deploy the widget")
        check(a.isSending, "1a send begins in-flight (isSending)")
        a.markUnsent(reason: "Can't reach the cluster — is Tailscale connected?")
        check(!a.isSending, "1b markUnsent ends in-flight")
        if case .unsent(let prompt, let reason)? = a.transcript.last {
            check(prompt == "deploy the widget", "1c UNSENT keeps the exact prompt")
            check(reason == "Can't reach the cluster — is Tailscale connected?", "1d reason preserved")
        } else {
            print("FAIL: last entry is not .unsent: \(String(describing: a.transcript.last))")
            ok = false
        }

        // ---- 2. Retry re-sends the failed prompt as a fresh turn ----
        a.retry(prompt: "deploy the widget")
        check(a.isSending, "2a retry starts in-flight")
        check(a.transcript.count >= 2, "2b transcript grew (historical UNSENT kept + new prompt)")
        if case .prompt(let p)? = a.transcript.last {
            check(p == "deploy the widget", "2c retry appended a fresh .prompt")
        } else {
            print("FAIL: retry last is not .prompt"); ok = false
        }

        // ---- 3. reachabilityLost -> honest terminal, job survives ----
        let b = ChatStore(project: "proj_reach_test")
        b.beginSend(prompt: "ping")
        b.startPolling(jobID: "job-reach-1")
        check(b.resumedJobID == "job-reach-1", "3a job persisted in-flight")
        b.reachabilityLost()
        check(!b.isSending, "3b reachabilityLost ends in-flight")
        if case .failure(let note)? = b.transcript.last {
            check(note.contains("Couldn't reach the cluster"), "3c honest terminal note appended")
        } else { print("FAIL: last not .failure"); ok = false }
        check(b.resumedJobID == "job-reach-1", "3d job SURVIVES reachabilityLost for later resume")
        let before3 = b.transcript.count
        b.reachabilityLost()
        check(b.transcript.count == before3, "3e reachabilityLost idempotent (no double append)")

        // ---- 4. abandonWaiting -> honest terminal, job cleared ----
        let c = ChatStore(project: "proj_abandon_test")
        c.beginSend(prompt: "ping")
        c.startPolling(jobID: "job-abandon-1")
        c.abandonWaiting()
        check(!c.isSending, "4a abandonWaiting ends in-flight")
        if case .failure(let note)? = c.transcript.last {
            check(note.contains("Stopped waiting"), "4b honest 'stopped waiting' note appended")
        } else { print("FAIL: last not .failure"); ok = false }
        check(c.resumedJobID == nil, "4c abandonWaiting clears job (no stale resume loop)")
        let before4 = c.transcript.count
        c.abandonWaiting()
        check(c.transcript.count == before4, "4d abandonWaiting idempotent")

        // ---- 5. failSend unchanged for a job that WAS created ----
        let d = ChatStore(project: "proj_fail_test")
        d.beginSend(prompt: "x")
        d.startPolling(jobID: "job-fail-1")
        d.failSend(message: "orchestrator_timeout")
        check(!d.isSending, "5a failSend ends in-flight")
        if case .failure(let msg)? = d.transcript.last {
            check(msg == "orchestrator_timeout", "5b failSend appends failure")
        }
        check(d.resumedJobID == nil, "5c failSend clears job")

        // ---- 6. Codable round-trip + backward compatibility ----
        let cases: [ChatEntry] = [
            .prompt("p"), .reply("r"), .failure("f"),
            .unsent(prompt: "hi", reason: "transport"),
            .unsent(prompt: "old style", reason: nil),
        ]
        for c in cases {
            guard let data = try? JSONEncoder().encode(c),
                  let back = try? JSONDecoder().decode(ChatEntry.self, from: data) else {
                print("FAIL: encode/decode \(c)"); ok = false; continue
            }
            if back != c { print("FAIL: round-trip mismatch \(c) -> \(back)"); ok = false }
        }
        // Backward-compat: pre-reason unsent and the original cases decode.
        for j in [#"{"kind":"unsent","text":"old"}"#, #"{"kind":"prompt","text":"p"}"#,
                  #"{"kind":"reply","text":"r"}"#, #"{"kind":"failure","text":"f"}"#] {
            if (try? JSONDecoder().decode(ChatEntry.self, from: Data(j.utf8))) == nil {
                print("FAIL: legacy decode \(j)"); ok = false; continue
            }
        }
        let u = ChatEntry.unsent(prompt: "p", reason: "rn")
        if u.text != "p" || u.unsentReason != "rn" { print("FAIL: accessors"); ok = false }

        return ok
    }
}

// Top-level code runs on the main thread, which is the main actor — safe to
// assume isolation rather than awaiting a MainActor-static from sync context.
let result = MainActor.assumeIsolated { Harness.run() }
print(result ? "ALL CHAT STATE MACHINE TESTS PASS" : "SOME FAILED")
exit(result ? 0 : 1)
