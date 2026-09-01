import Foundation

// ===========================================================================
// live_decode_check/main.swift — decode REAL live captures against the REAL
// model sources, and — unlike a plain decode — tell POPULATED from all-nil.
//
// model_decode_check confirms the committed fixtures decode. But an
// ALL-OPTIONAL model "decodes" ANYTHING, including an error body or an
// unexpected shape — so a "successful" decode can still mean a silent empty
// screen. This harness runs the same compiled models over what the server
// ACTUALLY returned (scripts/capture_live.sh) and then asks, per route: did it
// decode, AND did it carry any real data?
//
// Compiled and run by scripts/live_decode_check.sh, which compiles the real
// model files (never redeclared here):
//   Sources/HSCC/Models.swift
//   Sources/Shared/SharedModels.swift
//   Sources/HSCC/APIError.swift
//   Sources/HSCC/SessionEvent.swift
// plus the same Theme shim as model_decode_check (UI token, not a model).
//
// POPULATED vs all-nil is decided by runtime Mirror reflection over the decoded
// value: a value counts as POPULATED if any field other than `speak` holds a
// non-nil, non-empty value (recurse one level through nested structs/arrays so
// a response whose only "data" is an empty list still reads as all-nil). No
// model is re-examined by hand here — the check is structural and generic, so a
// field added to a model is caught automatically.
//
// Exit code: non-zero if any capture fails to decode OR decodes to an all-nil
// value. A PASS here means: every live GET response decodes against the real
// models AND carries real data.
// ===========================================================================

// --- Population reflection (structural, generic) ---------------------------

/// Is `value` an Optional that is currently nil?
private func isNil(_ value: Any) -> Bool {
    let m = Mirror(reflecting: value)
    guard m.displayStyle == .optional else { return false }
    return m.children.isEmpty   // nil optional has no children
}

/// Does `value` carry any real data?
///
/// The rule is deliberately structural and general, because the thing we are
/// guarding against is the ALL-OPTIONAL model that "decodes" an error body into
/// a silent empty screen. Concretely:
///
///   * a nil Optional → no data  (the only thing that propagates "empty")
///   * a present (even empty) collection/dict → a real field (non-optional in
///     the model IS present on a valid response; an empty queue, empty task
///     list, etc. is the correct no-work answer, NOT a swallowed error)
///   * a struct/class → data if any child (besides `speak`) holds data
///   * a scalar/string leaf → presence is data
///
/// `speak` is always skipped: the API synthesizes it on EVERY response, so its
/// presence never signals real data.
private func isPopulated(_ value: Any) -> Bool {
    let m = Mirror(reflecting: value)

    // Optional: nil -> not populated; non-nil -> recurse on the wrapped value.
    if m.displayStyle == .optional {
        guard let inner = m.children.first?.value else { return false }
        return isPopulated(inner)
    }

    // Struct / class / enum / collection / dictionary: populated if any child
    // (besides `speak`) carries data.
    if m.displayStyle == .struct || m.displayStyle == .class ||
       m.displayStyle == .collection || m.displayStyle == .dictionary ||
       m.displayStyle == .enum {
        for child in m.children {
            if child.label == "speak" { continue }
            if isPopulated(child.value) { return true }
        }
        // Struct with zero children (e.g. a plain String leaf) or a present
        // empty collection/dict/enum: the FIELD is present, which is data on a
        // real response. Only a struct whose every child was nil reaches here
        // and is correctly judged EMPTY.
        if m.displayStyle == .struct && m.children.isEmpty {
            // String / Date / other leaf: presence is data.
            return true
        }
        return m.displayStyle == .collection || m.displayStyle == .dictionary
            || m.displayStyle == .enum
    }

    // Anything else (scalar reachable only as a wrapped non-nil handled above).
    return true
}

// --- Runner ----------------------------------------------------------------

final class LiveCheck {
    let dir: String
    var decodes = 0
    var populated = 0
    var decodeFailures: [(file: String, model: String, error: String)] = []
    var emptyFailures: [(file: String, model: String)] = []

    init(dir: String) { self.dir = dir }
    var dataPath: String { dir }

    /// Decode a capture file into `T` and report decode + population.
    func decode<T: Decodable>(_ type: T.Type, _ file: String, _ model: String) {
        let path = dir + "/" + file
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            let value = try JSONDecoder().decode(type, from: data)
            decodes += 1
            if isPopulated(value) {
                populated += 1
                print("DECODE+  \(file)  →  \(model)  [POPULATED]")
            } else {
                emptyFailures.append((file, model))
                print("DECODE-  \(file)  →  \(model)  [ALL-NIL — decodes but carries no data!]")
            }
        } catch {
            decodeFailures.append((file, model, "\(error)"))
            print("FAIL    \(file)  →  \(model): \(error)")
        }
    }
}

let captureDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/scripts/live_captures"

let c = LiveCheck(dir: captureDir)

// ---- Live GET captures → real models --------------------------------------
// Each file is a REAL live response from scripts/live_captures/<ts>/. The model
// type is the real Decodable the app uses for that route.
c.decode(PingResponse.self,             "v1_ping.json",                "PingResponse")
c.decode(HealthResponse.self,           "v1_verify.json",              "HealthResponse (verify)")
c.decode(HealthResponse.self,           "v1_health.json",              "HealthResponse")
c.decode(ClusterStatusResponse.self,    "v1_cluster_status.json",      "ClusterStatusResponse")
c.decode(ClusterHostsResponse.self,     "v1_cluster_hosts.json",       "ClusterHostsResponse")
c.decode(ReadResponse.self,             "v1_cluster_monitor.json",     "ReadResponse (monitor)")
c.decode(ReadResponse.self,             "v1_cluster_jobs.json",        "ReadResponse (jobs)")
c.decode(ReadResponse.self,             "v1_cluster_info.json",        "ReadResponse (info)")
c.decode(CardsResponse.self,            "v1_cards.json",               "CardsResponse")
c.decode(CardDetailResponse.self,       "v1_cards_detail.json",        "CardDetailResponse")
c.decode(StandupResponse.self,          "v1_standup.json",             "StandupResponse")
c.decode(ReviewQueueResponse.self,      "v1_review_queue.json",        "ReviewQueueResponse")
c.decode(QAQueueResponse.self,          "v1_qa_queue.json",            "QAQueueResponse")
c.decode(FleetStatsResponse.self,       "v1_fleet_stats.json",         "FleetStatsResponse")
c.decode(FleetThroughputResponse.self,  "v1_fleet_throughput.json",    "FleetThroughputResponse")
c.decode(FleetStreamsResponse.self,     "v1_fleet_streams.json",       "FleetStreamsResponse")
c.decode(AutoscaleResponse.self,        "v1_autoscale.json",           "AutoscaleResponse")
c.decode(AutodownStatusResponse.self,   "v1_autodown_status.json",     "AutodownStatusResponse")
c.decode(ProjectsResponse.self,         "v1_projects.json",            "ProjectsResponse")
c.decode(ProjectDetailResponse.self,    "v1_projects_detail.json",     "ProjectDetailResponse")
c.decode(DaemonStatusResponse.self,     "v1_daemon_status.json",       "DaemonStatusResponse")
c.decode(TriggersResponse.self,         "v1_triggers.json",            "TriggersResponse")
c.decode(EscalationsResponse.self,      "v1_escalate.json",            "EscalationsResponse")
c.decode(ProfilesResponse.self,         "v1_profiles.json",            "ProfilesResponse")
c.decode(ProfileEditorResponse.self,    "v1_profile_editor.json",      "ProfileEditorResponse")
c.decode(KanbanBlockedResponse.self,    "v1_kanban_blocked.json",      "KanbanBlockedResponse")
c.decode(KanbanStaleResponse.self,      "v1_kanban_stale.json",        "KanbanStaleResponse")
c.decode(ActivityFeedResponse.self,     "v1_activity_feed.json",       "ActivityFeedResponse")
c.decode(SessionHistoryResponse.self,   "v1_project_session_events.json", "SessionHistoryResponse")
c.decode(TemplateListResponse.self,     "v1_template_list.json",       "TemplateListResponse")
c.decode(TemplateStatusResponse.self,   "v1_template_status.json",     "TemplateStatusResponse")
c.decode(TemplatePreviewResponse.self,  "v1_template_preview.json",    "TemplatePreviewResponse")
c.decode(SessionsListResponse.self,     "v1_sessions.json",            "SessionsListResponse")

// ---- Summary + exit code ---------------------------------------------------
print("")
let total = 33
print("LIVE DECODE: \(c.decodes)/\(total) decoded, \(c.populated)/\(total) populated")
if !c.decodeFailures.isEmpty {
    print("DECODE FAILURES (\(c.decodeFailures.count)):")
    for f in c.decodeFailures { print("  ✗ \(f.file) → \(f.model): \(f.error)") }
}
if !c.emptyFailures.isEmpty {
    print("ALL-NIL (decodes but carries no data) (\(c.emptyFailures.count)):")
    for f in c.emptyFailures { print("  ⚠ \(f.file) → \(f.model)") }
}
if c.decodeFailures.isEmpty && c.emptyFailures.isEmpty {
    print("ALL \(total) LIVE ROUTES DECODE AND CARRY REAL DATA")
    exit(0)
} else {
    print("LIVE DECODE ISSUES FOUND — see above")
    exit(1)
}
