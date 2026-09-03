import Foundation

// ===========================================================================
// model_decode_check/main.swift — DECODE the committed live fixtures against the
// REAL model sources (the harness; see ThemeStub.swift for the one non-model
// shim, and model_decode_check.sh for the build+run wrapper).
//
// Compiled and run by scripts/model_decode_check.sh, which first compiles the
// actual model files from the repo:
//   Sources/HSCC/Models.swift
//   Sources/Shared/SharedModels.swift
//   Sources/HSCC/APIError.swift
// and NEVER redeclares a model here. The Decodable types used below therefore
// resolve to the real structs, so a decode mismatch means the real Models.swift
// no longer matches the live JSON — exactly the false-green a mirror cannot
// catch.
// ===========================================================================

final class DecodeCheck {
    let dir: String
    var failures: [(fileName: String, type: String, error: String)] = []
    var passed = 0

    init(dir: String) { self.dir = dir }

    /// Load fixture bytes.
    func load(_ name: String) throws -> Data {
        try Data(contentsOf: URL(fileURLWithPath: dir + "/" + name))
    }

    /// Decode `T` from fixture `name`; record a named failure on any throw.
    /// The DecodingError's codingPath + field key are included in `error` so a
    /// failure names the fixture AND the field.
    func check<T: Decodable>(_ type: T.Type, _ name: String, _ label: String) {
        do {
            _ = try JSONDecoder().decode(type, from: load(name))
            passed += 1
            print("OK   \(name)  →  \(label)")
        } catch {
            var field = ""
            if let err = error as? DecodingError, case .keyNotFound(let key, _) = err {
                field = "field \(key.stringValue)"
            }
            let msg = error.localizedDescription
            failures.append((name, label, field.isEmpty ? msg : field + " — " + msg))
            print("FAIL \(name)  →  \(label): \(error)")
        }
    }
}

let fixtureDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/scripts/model_decode_check/fixtures"

let c = DecodeCheck(dir: fixtureDir)

// ---- Read responses with a committed fixture. Each maps a real model type to
// a real capture. Adding a fixture here extends coverage; editing a model to
// make one pass is exactly what this check is meant to validate. ----
c.check(PingResponse.self, "v1_ping.json", "PingResponse")
c.check(ClusterHostsResponse.self, "cluster_hosts.json", "ClusterHostsResponse")
c.check(HealthResponse.self, "v1_health.json", "HealthResponse")
c.check(HealthResponse.self, "v1_verify.json", "VerifyResponse (= HealthResponse)")
c.check(AutoscaleResponse.self, "v1_autoscale.json", "AutoscaleResponse")
c.check(FleetStatsResponse.self, "fleet_stats.json", "FleetStatsResponse")
c.check(FleetThroughputResponse.self, "fleet_throughput.json", "FleetThroughputResponse")
c.check(FleetStreamsResponse.self, "fleet_streams.json", "FleetStreamsResponse")
c.check(CardsResponse.self, "cards.json", "CardsResponse")
c.check(CardDetailResponse.self, "card_detail_t_049d6986.json", "CardDetailResponse")
c.check(StandupResponse.self, "v1_standup.json", "StandupResponse")
c.check(ReviewQueueResponse.self, "v1_review_queue.json", "ReviewQueueResponse")
c.check(QAQueueResponse.self, "v1_qa_queue.json", "QAQueueResponse")
c.check(ProjectsResponse.self, "v1_projects.json", "ProjectsResponse")
c.check(ProjectDetailResponse.self, "project_hscc.json", "ProjectDetailResponse")
c.check(DaemonStatusResponse.self, "daemon_status.json", "DaemonStatusResponse")
c.check(TriggersResponse.self, "v1_triggers.json", "TriggersResponse")
c.check(EscalationsResponse.self, "v1_escalate.json", "EscalationsResponse")
c.check(ProfilesResponse.self, "v1_profiles.json", "ProfilesResponse")
c.check(ProfileEditorResponse.self, "v1_profile_editor.json", "ProfileEditorResponse")
c.check(KanbanBlockedResponse.self, "kanban_blocked.json", "KanbanBlockedResponse")
c.check(KanbanStaleResponse.self, "kanban_stale.json", "KanbanStaleResponse")
c.check(TemplateListResponse.self, "template_list.json", "TemplateListResponse")
c.check(TemplateStatusResponse.self, "template_status.json", "TemplateStatusResponse")
c.check(TemplatePreviewResponse.self, "template_preview_hscc-live.json", "TemplatePreviewResponse")
c.check(ClusterStatusResponse.self, "v1_cluster_status.json", "ClusterStatusResponse (Shared)")
c.check(AutodownStatusResponse.self, "autodown_status.json", "AutodownStatusResponse (Shared)")
c.check(SessionsListResponse.self, "v1_sessions.json", "SessionsListResponse")
c.check(ActivityFeedResponse.self, "v1_activity_feed.json", "ActivityFeedResponse")

// ---- Session history paging (t_2776ea3c) ----
// Decode the session-event history page against the REAL wire model and assert
// the pager's contract: every event type decodes to its right case, seq is
// ascending oldest→newest, and the `next_before` paging cursor is surfaced.
c.check(SessionHistoryResponse.self, "v1_session_events.json", "SessionHistoryResponse")
do {
    let page = try JSONDecoder().decode(SessionHistoryResponse.self,
                                         from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/v1_session_events.json")))
    var ok = page.project == "hscc"
    ok = ok && page.next_before == 40
    ok = ok && page.oldest_seq == 1 && page.next_seq == 51
    ok = ok && !page.speak.isEmpty
    ok = ok && page.events.count == 10
    // seq strictly ascending oldest→newest (the pager relies on this order).
    for i in 1..<page.events.count where page.events[i].seq <= page.events[i-1].seq {
        ok = false
    }
    // every event type decodes to the expected case.
    let types: [ParsedPayload] = page.events.map(\.payload)
    guard types.count == 10 else { throw NSError(domain: "decodecheck", code: 1, userInfo: [NSLocalizedDescriptionKey: "expected 10 payloads"]) }
    if case .hello = types[0] {} else { ok = false }
    if case .message = types[1] {} else { ok = false }
    if case .toolCall = types[2] {} else { ok = false }
    if case .toolCall = types[3] {} else { ok = false }
    if case .message = types[4] {} else { ok = false }
    if case .card = types[5] {} else { ok = false }
    if case .agent = types[6] {} else { ok = false }
    if case .system = types[7] {} else { ok = false }
    if case .error = types[8] {} else { ok = false }
    if case .message = types[9] {} else { ok = false }
    // tool_call finish carried its optional duration_s as absent — decode lenient.
    if case .toolCall(let tc) = types[3], tc.duration_s != nil { ok = false }

    if ok {
        c.passed += 1
        print("OK   v1_session_events.json  →  history paging contract (10 events, seq 41–50, next_before 40, all 7 types)")
    } else {
        c.failures.append(("v1_session_events.json", "history paging contract", "assertion failed"))
        print("FAIL v1_session_events.json → history paging contract")
    }
} catch {
    c.failures.append(("v1_session_events.json", "history paging contract", "\(error)"))
    print("FAIL v1_session_events.json → history paging contract: \(error)")
}
// ---- Mutation POST responses (previously uncovered; each shape derived from
// the actual hscc-api handler so the fixture mirrors what the real endpoint
// returns on a 2xx clean success). ----
c.check(ReviewDetailResponse.self, "review_detail.json", "ReviewDetailResponse")
c.check(DispatchCardResponse.self, "dispatch_card.json", "DispatchCardResponse")
c.check(MergeCardResponse.self, "merge_card.json", "MergeCardResponse")
c.check(TemplateApplyResponse.self, "template_apply.json", "TemplateApplyResponse")
c.check(StopClusterResponse.self, "stop_cluster.json", "StopClusterResponse")
c.check(RecoverCardResponse.self, "recover_card.json", "RecoverCardResponse")
c.check(SessionMutationResponse.self, "session_retire.json", "SessionMutationResponse (retire)")
c.check(MemoryListResponse.self, "memory_list.json", "MemoryListResponse")
c.check(MemoryMutationResponse.self, "memory_delete.json", "MemoryMutationResponse (delete)")
c.check(AutodownEnableResponse.self, "autodown_enable.json", "AutodownEnableResponse")
c.check(AutodownDisableResponse.self, "autodown_disable.json", "AutodownDisableResponse")
c.check(AutodownWakeResponse.self, "autodown_wake.json", "AutodownWakeResponse")
c.check(AutodownCancelResponse.self, "autodown_cancel.json", "AutodownCancelResponse")
c.check(ClusterUpResponse.self, "cluster_up.json", "ClusterUpResponse")
c.check(ClusterDownResponse.self, "cluster_down.json", "ClusterDownResponse")
c.check(OrchestratorChatJobResponse.self, "orchestrator_chat.json", "OrchestratorChatJobResponse")
c.check(OrchestratorChatJobStatus.self, "orchestrator_chat_status.json", "OrchestratorChatJobStatus")

// ---- Approvals classification (t_9a5cfc3b) — the SAME `isPendingApproval`
// logic the on-screen inbox, the badge poller, and the Siri intent use, asserted
// against the REAL committed kanban_blocked.json fixture. Both cards in that
// capture are `block_kind == needs_input`, so both are pending approvals. This
// guards the classification from drifting on decode types we don't redeclare. ----
do {
    let blocked = try JSONDecoder().decode(
        KanbanBlockedResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/kanban_blocked.json")))
    let cards = blocked.tasks ?? []
    let pending = cards.filter(\.isPendingApproval)
    if cards.count == 2 && pending.count == 2 {
        c.passed += 1
        print("OK   kanban_blocked.json  →  approvals classification: 2/2 pending")
    } else {
        c.failures.append(("kanban_blocked.json",
                           "isPendingApproval classification",
                           "expected 2/2 pending, got \(cards.count) cards / \(pending.count) pending"))
        print("FAIL kanban_blocked.json → isPendingApproval classification (\(pending.count)/\(cards.count))")
    }
} catch {
    c.failures.append(("kanban_blocked.json", "approvals classification", "\(error)"))
    print("FAIL kanban_blocked.json → approvals classification: \(error)")
}

// ---- Summary + exit code. A single failure is a non-zero exit — a guard that
// exits 0 here has proven, right now, that every committed fixture decodes
// against the real models. ----
print("")
if c.failures.isEmpty {
    print("ALL DECODE CHECKS PASSED — \(c.passed)/\(c.passed) fixtures decoded against the real models")
    exit(0)
} else {
    print("DECODE FAILURES — \(c.failures.count) fixture(s) did not decode against the real models:")
    for f in c.failures {
        print("  ✗ \(f.fileName) → \(f.type): \(f.error)")
    }
    exit(1)
}
