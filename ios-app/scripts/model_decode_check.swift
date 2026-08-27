import Foundation

// Field-for-field decode check of the app's Models.swift models against the
// REAL live API JSON captured 2026-08-27. Structs mirror Models.swift /
// SharedModels.swift exactly; run as a plain macOS CLI to prove the Swift
// field names match the live JSON keys.

// ---- JSONValue (mirror of Models.swift) ----
enum JSONValue: Decodable {
    case string(String), int(Int), double(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let i = try? c.decode(Int.self) { self = .int(i) }
        else if let d = try? c.decode(Double.self) { self = .double(d) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "unknown")) }
    }
}

// ---- Ping ----
struct PingResponse: Decodable { let ok: Bool; let service: String?; let version: String?; let speak: String }

// ---- Cluster hosts (the fixed model) ----
struct ClusterHostsResponse: Decodable {
    let hosts: [JSONValue]
    let saved_clusters: [String: JSONValue]?
    let live_status: [String: JSONValue]?
    let speak: String
}

// ---- Health / Verify ----
struct HealthCheck: Decodable { let name: String; let ok: Bool; let detail: String? }
struct HealthResponse: Decodable { let ok: Bool; let checks: [HealthCheck]; let speak: String }

// ---- Autoscale ----
struct AutoscaleResponse: Decodable { let action: String; let reason: String?; let speak: String }

// ---- Fleet stats ----
struct FleetCompletions: Decodable { let total: Int; let by_profile: [String: Int]?; let by_day: [String: Int]? }
struct FleetActivity: Decodable { let tool_calls_by_profile: [String: Int]?; let top_tools: [[JSONValue]]? }
struct FleetStatsResponse: Decodable { let since_days: Int?; let completions: FleetCompletions?; let activity: FleetActivity?; let speak: String }

// ---- Fleet throughput ----
struct FleetTotals: Decodable {
    let prompt_tokens: Double?; let generation_tokens: Double?; let running: Double?; let waiting: Double?
    let nodes_ok: Int?; let nodes_total: Int?
}
struct NodeMetrics: Decodable {
    let prompt_tokens: Double?; let generation_tokens: Double?; let running: Double?; let waiting: Double?
}
struct FleetThroughputResponse: Decodable { let fleet: FleetTotals?; let by_node: [String: NodeMetrics]?; let speak: String }

// ---- Fleet streams ----
struct StreamStatus: Decodable { let ok: Bool?; let timestamp: String?; let stream: String?; let message: String? }
struct FleetStreamsResponse: Decodable { let streams: [String: StreamStatus]; let speak: String }

// ---- Cards ----
struct Card: Decodable { let id: String; let title: String?; let status: String?; let assignee: String?; let board: String? }
struct CardsResponse: Decodable { let cards: [Card]; let count: Int; let speak: String }
struct CardDetailResponse: Decodable { let id: String; let title: String?; let status: String?; let speak: String }

// ---- Projects ----
struct Project: Decodable { let name: String; let repo: String?; let board: String?; let topic: JSONValue? }
struct ProjectsResponse: Decodable { let projects: [Project]; let count: Int?; let speak: String }
struct ProjectGit: Decodable {
    let is_repo: Bool?; let branch: String?; let dirty: Bool?; let uncommitted: [String]?
    let last_activity_seconds_ago: Int?; let head: String?
}
struct ProjectDetailResponse: Decodable {
    let name: String?; let repo: String?; let board: String?; let topic: JSONValue?
    let board_counts: [String: Int]?; let git: ProjectGit?; let speak: String
}

// ---- Standup ----
struct StandupRow: Decodable {
    let rawID: String?; let title: String?; let project: String?; let kind: String?
    let status: String?; let reason: String?; let drift: String?; let age_seconds: Int?; let board: String?
    enum CodingKeys: String, CodingKey { case rawID = "id"; case title, project, kind, status, reason, drift, age_seconds, board }
}
struct StandupResponse: Decodable {
    let needs_you: [StandupRow]?; let failing: [StandupRow]?; let stale: [StandupRow]?
    let running: [StandupRow]?; let drift: [StandupRow]?; let unreadable: [StandupRow]?; let speak: String
}

// ---- Review / QA ----
struct ReviewQueueRow: Decodable {
    let project: String?; let card_id: String?; let title: String?; let branch: String?; let age_seconds: Int?
}
struct ReviewQueueResponse: Decodable { let queue: [ReviewQueueRow]; let count: Int; let speak: String }
struct QARow: Decodable {
    let project: String?; let card_id: String?; let title: String?; let status: String?; let branch: String?
    let unverifiable: Bool?; let verify: String?; let files_changed: Int?; let verify_configured: Bool?
    let verify_run: Bool?; let verify_passed: Bool?; let created_at: Double?
}
struct ManualQARow: Decodable {
    let rawID: String?; let project: String?; let description: String?; let card_id: String?
    let added_at: String?; let checked: Bool?
    enum CodingKeys: String, CodingKey { case rawID = "id"; case project, description, card_id, added_at, checked }
}
struct QAQueueResponse: Decodable { let queue: [QARow]; let manual_qa: [ManualQARow]?; let speak: String }

// ---- Daemon / Triggers / Escalations / Profiles ----
struct DaemonStatusResponse: Decodable {
    let daemon_running: Bool?; let pid: Int?; let state: String?; let streams: [String: StreamStatus]?; let speak: String
}
struct TriggerCondition: Decodable { let metric: String?; let op: String?; let value: JSONValue? }
struct TriggerParams: Decodable { let title: String?; let body: String? }
struct TriggerRule: Decodable { let id: String; let trigger_type: String?; let condition: TriggerCondition?; let trigger_params: TriggerParams? }
struct TriggersResponse: Decodable { let rules: [TriggerRule]?; let last_run: StreamStatus?; let recent_events: [String]?; let speak: String }
struct EscalationsResponse: Decodable { let escalations: [JSONValue]?; let count: Int?; let speak: String }
struct ProfilesResponse: Decodable { let counts: [String: Int]?; let total_running: Int?; let profiles: [JSONValue]?; let speak: String }

// ---- Board hygiene ----
struct BlockedCard: Decodable {
    let board: String?; let id: String; let status: String?; let assignee: String?
    let age_days: Int?; let block_kind: String?; let why: String?; let title: String?; let comments: [String]?
}
struct KanbanBlockedResponse: Decodable { let boards: Int?; let tasks: [BlockedCard]?; let errors: [String]?; let count: Int?; let speak: String }
struct StaleCard: Decodable {
    let board: String?; let id: String; let status: String?; let assignee: String?; let age_days: Int?; let title: String?
}
struct KanbanStaleResponse: Decodable { let boards: [String]?; let tasks: [StaleCard]?; let errors: [String]?; let older_than: Int?; let count: Int?; let speak: String }

// ---- Templates ----
struct ClusterTemplate: Decodable { let name: String; let version: Int?; let description: String?; let families: [String]?; let group: String? }
struct TemplateListResponse: Decodable { let templates: [ClusterTemplate]; let count: Int?; let speak: String }
struct TemplateApplied: Decodable {
    let template: String?; let applied_at: String?; let orchestrator_node: String?; let families: [String]?; let units: JSONValue?
}
struct TemplateStatusResponse: Decodable { let applied: TemplateApplied?; let note: String?; let speak: String }
struct TemplateChange: Decodable { let file: String?; let action: String?; let summary: String?; let diff_summary: String?; let details: [String]? }
struct TemplateRouting: Decodable { let consumer: String?; let target: String?; let base_url: String?; let model: String?; let keys: [String]? }
struct TemplatePreviewResponse: Decodable {
    let template: String?; let description: String?; let changes: [TemplateChange]?
    let routing: [TemplateRouting]?; let routing_untouched: [TemplateRouting]?; let speak: String
}

let dir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/hscc_live"
func load(_ name: String) -> Data { try! Data(contentsOf: URL(fileURLWithPath: dir + "/" + name)) }
func jd<T: Decodable>(_ t: T.Type, _ name: String) throws -> T { try JSONDecoder().decode(t, from: load(name)) }
func jrp<T: Decodable>(_ t: T.Type, _ name: String) { _ = try! jd(t, name); print("OK  \(name)  →  \(T.self)") }
func tryDecode(_ name: String, _ label: String, _ block: () throws -> Void) {
    do { try block(); print("OK  \(name)  →  \(label)") }
    catch { print("FAIL \(name)  →  \(label): \(error)") }
}

tryDecode("v1_ping.json", "PingResponse") { _ = try jd(PingResponse.self, "v1_ping.json") }
tryDecode("cluster_hosts.json", "ClusterHostsResponse") {
    let r = try jd(ClusterHostsResponse.self, "cluster_hosts.json")
    print("     hosts decoded \(r.hosts.count) entries")
}
tryDecode("v1_health.json", "HealthResponse") { _ = try jd(HealthResponse.self, "v1_health.json") }
tryDecode("v1_autoscale.json", "AutoscaleResponse") { _ = try jd(AutoscaleResponse.self, "v1_autoscale.json") }
tryDecode("fleet_stats.json", "FleetStatsResponse") { _ = try jd(FleetStatsResponse.self, "fleet_stats.json") }
tryDecode("fleet_throughput.json", "FleetThroughputResponse") { _ = try jd(FleetThroughputResponse.self, "fleet_throughput.json") }
tryDecode("fleet_streams.json", "FleetStreamsResponse") { _ = try jd(FleetStreamsResponse.self, "fleet_streams.json") }
tryDecode("cards.json", "CardsResponse") { _ = try jd(CardsResponse.self, "cards.json") }
tryDecode("card_detail_t_049d6986.json", "CardDetailResponse") { _ = try jd(CardDetailResponse.self, "card_detail_t_049d6986.json") }
tryDecode("projects.json", "ProjectsResponse") { _ = try jd(ProjectsResponse.self, "projects.json") }
tryDecode("project_hscc.json", "ProjectDetailResponse") { _ = try jd(ProjectDetailResponse.self, "project_hscc.json") }
tryDecode("v1_standup.json", "StandupResponse") { _ = try jd(StandupResponse.self, "v1_standup.json") }
tryDecode("v1_review_queue.json", "ReviewQueueResponse") { _ = try jd(ReviewQueueResponse.self, "v1_review_queue.json") }
tryDecode("v1_qa_queue.json", "QAQueueResponse") { _ = try jd(QAQueueResponse.self, "v1_qa_queue.json") }
tryDecode("daemon_status.json", "DaemonStatusResponse") { _ = try jd(DaemonStatusResponse.self, "daemon_status.json") }
tryDecode("v1_triggers.json", "TriggersResponse") { _ = try jd(TriggersResponse.self, "v1_triggers.json") }
tryDecode("v1_escalate.json", "EscalationsResponse") { _ = try jd(EscalationsResponse.self, "v1_escalate.json") }
tryDecode("v1_profiles.json", "ProfilesResponse") { _ = try jd(ProfilesResponse.self, "v1_profiles.json") }
tryDecode("kanban_blocked.json", "KanbanBlockedResponse") { _ = try jd(KanbanBlockedResponse.self, "kanban_blocked.json") }
tryDecode("kanban_stale.json", "KanbanStaleResponse") { _ = try jd(KanbanStaleResponse.self, "kanban_stale.json") }
tryDecode("template_list.json", "TemplateListResponse") { _ = try jd(TemplateListResponse.self, "template_list.json") }
tryDecode("template_status.json", "TemplateStatusResponse") { _ = try jd(TemplateStatusResponse.self, "template_status.json") }
tryDecode("template_preview_hscc-live.json", "TemplatePreviewResponse") { _ = try jd(TemplatePreviewResponse.self, "template_preview_hscc-live.json") }

print("\nALL DECODE CHECKS COMPLETE")
