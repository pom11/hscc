import Foundation

// ===========================================================================
// model_decode_check.swift — DECODE the committed live fixtures against the
// REAL model sources (this is the harness; see main.swift for the driver and
// ThemeStub.swift for the one non-model shim).
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
c.check(KanbanBlockedResponse.self, "kanban_blocked.json", "KanbanBlockedResponse")
c.check(KanbanStaleResponse.self, "kanban_stale.json", "KanbanStaleResponse")
c.check(TemplateListResponse.self, "template_list.json", "TemplateListResponse")
c.check(TemplateStatusResponse.self, "template_status.json", "TemplateStatusResponse")
c.check(TemplatePreviewResponse.self, "template_preview_hscc-live.json", "TemplatePreviewResponse")
c.check(ClusterStatusResponse.self, "v1_cluster_status.json", "ClusterStatusResponse (Shared)")
c.check(AutodownStatusResponse.self, "autodown_status.json", "AutodownStatusResponse (Shared)")

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
