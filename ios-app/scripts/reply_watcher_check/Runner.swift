// reply_watcher_check/main.swift — PROVE the StreamReplyWatcher watermark + the
// ProjectUnreadCenter interaction (card t_c9cc4ef9 — "I have to switch tabs
// to see it").
//
// Compiled by scripts/reply_watcher_check.sh together with the REAL
// StreamReplyWatcher.swift, ProjectUnreadCenter.swift, SessionEvent.swift and
// the real decode-layer deps, plus the stub HSCCClient defined HERE. Events are
// built by DECODING the same wire JSON the app decodes, so the harness drives
// real `SessionEvent` values (never a redeclaration).
import Foundation

// ---------------------------------------------------------------------------
// Stub HSCCClient — satisfies the watcher's exact API surface with canned data.
// The test appends new events to `projectTails` between polls to simulate the
// orchestrator finishing a reply while the operator is elsewhere.
// ---------------------------------------------------------------------------
final class HSCCClient {
    var projectTails: [String: [SessionEvent]] = [:]

    func projects() async throws -> ProjectsResponse {
        let projects = projectTails.keys.sorted().map { Project(name: $0, repo: nil, board: nil, topic: nil) }
        return ProjectsResponse(projects: projects, count: projects.count, speak: "")
    }

    func sessionEvents(project: String,
                      before: Int? = nil,
                      limit: Int = 200) async throws -> SessionHistoryResponse {
        var tail = projectTails[project] ?? []
        tail.sort { $0.seq < $1.seq }
        let events: [SessionEvent] = Array(tail.suffix(limit))
        return SessionHistoryResponse(project: project, events: events,
                                   next_before: nil, oldest_seq: events.first?.seq ?? 0,
                                   next_seq: (events.last?.seq ?? 0) + 1, speak: "")
    }
}

// Decode a real SessionEvent from its wire JSON (the same shape the app and
// model_decode_check prove). Fatal-if-bad is acceptable: this is a harness.
func event(_ json: String) -> SessionEvent {
    try! JSONDecoder().decode(SessionEvent.self, from: Data(json.utf8))
}
func assistantReply(seq: Int, done: Bool) -> SessionEvent {
    event(#"{"seq":\#(seq),"type":"message","ts":"","payload":{"role":"assistant","delta":"reply \#(seq)","done":\#(done)}}"#)
}
func userEvent(seq: Int) -> SessionEvent {
    event(#"{"seq":\#(seq),"type":"message","ts":"","payload":{"role":"user","delta":"hi","done":true}}"#)
}
func toolEvent(seq: Int) -> SessionEvent {
    event(#"{"seq":\#(seq),"type":"tool_call","ts":"","payload":{"call_id":"c\#(seq)","name":"read","status":"finish"}}"#)
}

@main
struct Main {
    @MainActor
    static func main() async {
        var ok = true
        func check(_ cond: Bool, _ label: String) {
            print("\(cond ? "PASS" : "FAIL"): \(label)")
            if !cond { ok = false }
        }

        // Clean slate — the unread center persists ALL projects in one
        // UserDefaults blob; wipe it for a deterministic run.
        UserDefaults.standard.removeObject(forKey: ProjectUnreadCenter.storageKey)

        let unread = ProjectUnreadCenter()
        let watcher = StreamReplyWatcher(unread: unread)
        let client = HSCCClient()
        let P = "project_a"

        // --- 1: historical replies are BASELINE, never a badge ---
        client.projectTails[P] = [ userEvent(seq: 1), assistantReply(seq: 2, done: true), assistantReply(seq: 3, done: true) ]
        check(unread.count(for: P) == 0, "1a: fresh app has no badge before first observation")
        await watcher.refresh(project: P, client: client)
        check(unread.count(for: P) == 0, "1b: first poll does NOT badge pre-existing history (baseline, not prior art)")

        // --- 2: a reply that lands while away IS badged, idempotently ---
        client.projectTails[P]!.append(assistantReply(seq: 4, done: true))
        await watcher.refresh(project: P, client: client)
    check(unread.count(for: P) == 1, "2a: new reply (seq 4) while away is counted")
        await watcher.refresh(project: P, client: client)
        check(unread.count(for: P) == 1, "2b: re-poll of the same reply does not double-count")

        // --- 3: user echoes and tool calls never badge ---
        client.projectTails[P]!.append(userEvent(seq: 5))
        client.projectTails[P]!.append(toolEvent(seq: 6))
        await watcher.refresh(project: P, client: client)
        check(unread.count(for: P) == 1, "3: user echo + tool call do not increment the reply badge")

        // --- 4: replies the operator READ live are never re-badged ---
        let P2 = "project_b"
        client.projectTails[P2] = [ userEvent(seq: 1), assistantReply(seq: 2, done: false), assistantReply(seq: 3, done: true) ]
        await watcher.refresh(project: P2, client: client)          // establish baseline (max seq 3)
        watcher.noteSeen(project: P2, seq: 5)     // chat folded live up to seq 5
        await watcher.refresh(project: P2, client: client)
        check(unread.count(for: P2) == 0, "4: replies already seen live (noteSeen) are never re-badged")

        // --- 5: a reply landing AFTER the operator stopped reading IS badged ---
        let P3 = "project_c"
        client.projectTails[P3] = [ userEvent(seq: 1), assistantReply(seq: 2, done: true) ]
        await watcher.refresh(project: P3, client: client)          // baseline seq 2 (seen in chat)
        client.projectTails[P3]!.append(assistantReply(seq: 6, done: true))   // new reply
        await watcher.refresh(project: P3, client: client)
        check(unread.count(for: P3) == 1, "5: reply landing AFTER operator stopped reading is badged")

        // --- 6: reading suppression — a reply while reading that chat is not badged ---
        let P4 = "project_d"
        client.projectTails[P4] = [ userEvent(seq: 1), assistantReply(seq: 2, done: true) ]
        await watcher.refresh(project: P4, client: client)          // baseline seq 2
        unread.setReading(P4)                      // operator is reading P4's chat
        client.projectTails[P4]!.append(assistantReply(seq: 7, done: true))
        await watcher.refresh(project: P4, client: client)
        check(unread.count(for: P4) == 0, "6: a reply landing while actively reading the chat is suppressed (no badge)")
        unread.setReading(nil)

        print(ok ? "\nALL PASS" : "\nSOME FAILED")
        exit(ok ? 0 : 1)
    }
}
