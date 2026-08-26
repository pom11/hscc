import Foundation

// ---------------------------------------------------------------------------
// Codable models matching the ACTUAL /v1 response shapes.
//
// Read these from the real HSCC API implementation on feat/hscc-api:
//   hscc-api/routes_cluster.py  (cluster + fleet reads)
//   hscc-api/routes_project.py  (project / kanban reads)
//   hscc-api/api_server.py      (/v1/ping + the error contract)
// and docs/DESIGN-api.md §A (the contract) + §B (the `speak` field).
//
// Every READ response carries a first-class, top-level `speak` string. The
// client exposes it via `Speakable` so B5 (Siri App Intents) can read the
// server-derived one-liner instead of re-deriving prose on device.
// ---------------------------------------------------------------------------

/// Protocol for any read response that carries the API's first-class `speak`
/// field (design §B). B5 consumes this to speak summaries aloud.
protocol Speakable {
    var speak: String { get }
}

// MARK: - Shared error shape (design §C)

/// The unified error object inside `{ "error": { ... } }`.
struct APIErrorBody: Decodable {
    let code: String
    let message: String
    let speak: String?
}

struct APIErrorEnvelope: Decodable {
    let error: APIErrorBody
}

// MARK: - Liveness

/// GET /v1/ping — the API's own liveness probe.
struct PingResponse: Decodable, Speakable {
    let ok: Bool
    let service: String?
    let version: String?
    let speak: String
}

// MARK: - Cluster

/// GET /v1/cluster/status — one workload entry.
struct ClusterWorkload: Decodable, Identifiable {
    let name: String
    let tp: String?
    let pp: String?
    let container_id: String?

    var id: String { name }
}

/// GET /v1/cluster/status.
struct ClusterStatusResponse: Decodable, Speakable {
    let workloads: [ClusterWorkload]
    let idle_hosts: [String]
    let total_hosts: Int
    let speak: String
}

/// GET /v1/cluster/hosts.
///
/// `saved_clusters` and `live_status` are raw `run_cmd`-shaped dicts from the
/// cluster engine (keys like `success`/`returncode`/`output` with MIXED value
/// types — bool, int, string), so they are decoded as untyped `[String:
/// JSONValue]`. They are not rendered in the current UI, but declaring them as
/// `[String: String]` (as this was) makes the WHOLE response throw on decode
/// and blanks the Hosts section for no visible reason. (Verified against the
/// live /v1/cluster/hosts response.)
struct ClusterHostsResponse: Decodable, Speakable {
    let hosts: [String]
    let saved_clusters: [String: JSONValue]?
    let live_status: [String: JSONValue]?
    let speak: String
}

/// GET /v1/health — one fleet check entry.
struct HealthCheck: Decodable, Identifiable {
    let name: String
    let ok: Bool
    let detail: String?

    var id: String { name }
}

/// GET /v1/health.
struct HealthResponse: Decodable, Speakable {
    let ok: Bool
    let checks: [HealthCheck]
    let speak: String
}

/// GET /v1/autoscale — a decision, never an action.
struct AutoscaleResponse: Decodable, Speakable {
    let action: String
    let reason: String?
    let target: Int?
    let speak: String
}

// MARK: - Fleet stats

/// GET /v1/fleet/stats — fleet completions & tool activity over the last N days.
struct FleetStatsResponse: Decodable, Speakable {
    let since_days: Int?
    let completions: FleetCompletions?
    let activity: FleetActivity?
    let speak: String
}

/// The `completions` bucket of /v1/fleet/stats.
struct FleetCompletions: Decodable {
    let total: Int
    let by_profile: [String: Int]?
    let by_day: [String: Int]?
}

/// The `activity` bucket of /v1/fleet/stats.
struct FleetActivity: Decodable {
    let tool_calls_by_profile: [String: Int]?
    /// Pairs of [toolName, count] as returned by the API.
    let top_tools: [[JSONValue]]?
}

// MARK: - Fleet throughput

/// GET /v1/fleet/throughput — vLLM token throughput + per-node queue depth.
struct FleetThroughputResponse: Decodable, Speakable {
    let fleet: FleetTotals?
    let by_node: [String: NodeMetrics]?
    let speak: String
}

/// The aggregate `fleet` bucket of /v1/fleet/throughput.
struct FleetTotals: Decodable {
    let prompt_tokens: Double?
    let generation_tokens: Double?
    let running: Double?
    let waiting: Double?
    let nodes_ok: Int?
    let nodes_total: Int?
}

/// Per-endpoint metrics inside /v1/fleet/throughput's `by_node` map.
struct NodeMetrics: Decodable {
    let prompt_tokens: Double?
    let generation_tokens: Double?
    let running: Double?
    let waiting: Double?
}

// MARK: - Fleet streams

/// GET /v1/fleet/streams — daemon stream health (stream name -> status dict).
/// Each status dict is written by the daemon via `write_state`, so it carries a
/// stable `ok` flag plus whatever check-specific keys that stream emits; extra
/// keys are ignored on decode.
struct FleetStreamsResponse: Decodable, Speakable {
    let streams: [String: StreamStatus]
    let speak: String
}

/// One daemon stream's status entry.
struct StreamStatus: Decodable {
    let ok: Bool?
    let timestamp: String?
    let stream: String?
    let message: String?
}

// MARK: - Projects / kanban

/// A single flightdeck card. This is the full flightdeck card dict; only the
/// fields the UI currently needs are declared as strong types. Unknown fields
/// are ignored (the API ignores unknown JSON fields for forward compat, and
/// the client tolerates a growing card schema by reading only known keys).
struct Card: Decodable, Identifiable {
    let id: String
    let title: String?
    let status: String?
    let assignee: String?
    let board: String?

    var displayTitle: String { title ?? "(untitled)" }
    var displayStatus: String { status ?? "unknown" }
}

/// GET /v1/cards.
struct CardsResponse: Decodable, Speakable {
    let cards: [Card]
    let count: Int
    let speak: String
}

/// GET /v1/cards/{card_id}.
struct CardDetailResponse: Decodable, Speakable {
    let id: String
    let title: String?
    let status: String?
    let speak: String
}

/// ONE standup digest row. Sections vary in shape (standup.py:960 `_render_row`):
///
/// * Card sections (needs_you / stale / running) carry ``id`` + ``title``;
///   needs_you/failing add ``project``; stale adds ``kind`` (starved | stale)
///   and ``age_seconds``.
/// * Project sections (failing / drift / unreadable) carry ``project`` and a
///   status-ish key (``status`` | ``drift`` | ``reason``).
///
/// We read only the safe, common keys and let the rest be ignored, so one
/// generic row serves every section without guessing at a unifying contract.
struct StandupRow: Decodable, Identifiable {
    /// The card id from the API (a project-style row may have none).
    let rawID: String?
    let title: String?
    let project: String?
    let kind: String?
    let status: String?
    let reason: String?
    let drift: String?
    let age_seconds: Int?
    let board: String?

    enum CodingKeys: String, CodingKey {
        case rawID = "id"
        case title, project, kind, status, reason, drift, age_seconds, board
    }

    /// Row identity for List iteration. Cards have a real id; project rows
    /// fall back to their project name so the list still works.
    var id: String { rawID ?? project ?? title ?? UUID().uuidString }

    var displayTitle: String { title ?? project ?? "(untitled)" }
    var displayKind: String { kind ?? status ?? drift ?? reason ?? "" }
}

/// GET /v1/standup — the daily digest (what needs attention).
struct StandupResponse: Decodable, Speakable {
    let needs_you: [StandupRow]?
    let failing: [StandupRow]?
    let stale: [StandupRow]?
    let running: [StandupRow]?
    let drift: [StandupRow]?
    let unreadable: [StandupRow]?
    let speak: String
}

/// GET /v1/review/queue — one row awaiting review (newest first).
/// ``age_seconds`` may be null when the card has no created_at (sorted last).
struct ReviewQueueRow: Decodable, Identifiable {
    let project: String?
    let card_id: String?
    let title: String?
    let branch: String?
    let age_seconds: Int?

    var id: String { card_id ?? title ?? project ?? UUID().uuidString }
    var displayTitle: String { title ?? card_id ?? "(untitled)" }
}

/// GET /v1/review/queue.
struct ReviewQueueResponse: Decodable, Speakable {
    let queue: [ReviewQueueRow]
    let count: Int
    let speak: String
}

/// GET /v1/review/{card_id} — DRY-RUN review facts. Read-only by construction:
/// the endpoint never merges or closes. ``conflicts`` is null when merge status
/// is unknown (0 = clean). ``landed`` reflects whether the branch is already an
/// ancestor of the base. ``verify`` is the card's VERIFY: line (may be empty);
/// ``verify_present`` tells whether one was found.
struct ReviewDetailResponse: Decodable, Speakable {
    let id: String?
    let title: String?
    let board: String?
    let project: String?
    let repo: String?
    let branch: String?
    let base: String?
    let subject: String?
    let files_changed: Int?
    let insertions: Int?
    let deletions: Int?
    let conflicts: Int?
    let landed: Bool?
    let verify_present: Bool?
    let verify: String?
    let dependents: [String]?
    let speak: String

    var displayTitle: String { title ?? subject ?? id ?? "(untitled)" }

    /// A human, server-derived conflict verdict already computed by the API.
    var mergeClause: String {
        if let landed, landed { return "Already merged into \(base ?? "main")." }
        if let conflicts {
            return conflicts == 0 ? "Merges cleanly into \(base ?? "main")."
                                  : "\(conflicts) conflict\(conflicts == 1 ? "" : "s") to resolve."
        }
        return "Merge status unknown."
    }
}

/// GET /v1/qa/queue — one pre-merge QA row.
struct QARow: Decodable, Identifiable {
    let project: String?
    let card_id: String?
    let title: String?
    let status: String?
    let branch: String?
    let unverifiable: Bool?
    let verify: String?
    let files_changed: Int?
    let verify_configured: Bool?
    let verify_run: Bool?
    let verify_passed: Bool?
    let created_at: Double?

    var id: String { card_id ?? title ?? project ?? UUID().uuidString }
    var displayTitle: String { title ?? card_id ?? "(untitled)" }
}

/// GET /v1/qa/queue — one manual-QA store entry.
///
/// `added_at` is the store's ISO-8601 STRING (e.g. "2026-08-15T23:52:51") as
/// written by `qa_manual` on the server — NOT a Unix epoch. It is decoded as a
/// String and surfaced verbatim; the client never parses it to a Date for the
/// current UI. (Verified against the live /v1/qa/queue response.)
struct ManualQARow: Decodable, Identifiable {
    /// The API `id` field (may be absent for some entries).
    let rawID: String?
    let project: String?
    let description: String?
    let card_id: String?
    let added_at: String?
    let checked: Bool?

    enum CodingKeys: String, CodingKey {
        case rawID = "id"
        case project, description, card_id, added_at, checked
    }

    var id: String { rawID ?? card_id ?? description ?? UUID().uuidString }
    var displayDescription: String { description ?? card_id ?? "(untitled)" }
}

/// GET /v1/qa/queue.
struct QAQueueResponse: Decodable, Speakable {
    let queue: [QARow]
    let manual_qa: [ManualQARow]?
    let speak: String
}

// MARK: - Orchestrator chat (C5)

/// POST /v1/orchestrator/chat — the orchestrator's reply.
///
/// Matches the ACTUAL 200 response shape in
/// `hscc-api/routes_orchestrator.py`:
///   { "reply": "<text>", "profile": "<P>-orch", "session": "<P>", "speak": "..." }
/// `reply` is the full orchestrator reply the transcript shows; `speak` is the
/// short server-derived one-liner (B5 may read it aloud). A non-2xx makes the
/// client throw, so this is only ever decoded from a 2xx success — a failed or
/// timed-out chat is never rendered as a reply.
struct OrchestratorChatResponse: Decodable, Speakable {
    let reply: String
    let profile: String?
    let session: String?
    let speak: String
}

// MARK: - Generic / untyped bucket

/// A catch-all for read responses we haven't given a dedicated strong type
/// yet (cluster/monitor, fleet/stats, review/queue, etc.). Since the API
/// ignores unknown fields and every read carries `speak`, this lets the client
/// fetch and surface a speech summary today and add typed models in B3/B4 when
/// the feature views are built.
struct ReadResponse: Decodable, Speakable {
    let speak: String
    // Plus whatever the endpoint returned; consumers look up keys directly.
    let payload: [String: JSONValue]

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: DynamicCodingKey.self)
        var payload: [String: JSONValue] = [:]
        for key in container.allKeys {
            let value = try container.decode(JSONValue.self, forKey: key)
            payload[key.stringValue] = value
        }
        guard let s = payload["speak"]?.string else {
            throw HSCCError.decoding("missing speak field")
        }
        self.speak = s
        self.payload = payload
    }
}

/// A tiny JSON value enum so untyped read payloads can be inspected without
/// pulling in a third-party JSON library.
enum JSONValue: Decodable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    var string: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let i = try? c.decode(Int.self) { self = .int(i) }
        else if let d = try? c.decode(Double.self) { self = .double(d) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "unknown JSON value")) }
    }
}

// MARK: - Dynamic coding key (for the untyped bucket)

private struct DynamicCodingKey: CodingKey {
    var stringValue: String
    var intValue: Int?

    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { self.intValue = intValue; self.stringValue = String(intValue) }
}

// ---------------------------------------------------------------------------
// B4 — mutating (confirm-gated) response models.
//
// These match the ACTUAL response shapes in the mutating API:
//   hscc-api/routes_actions.py  (POST /v1/cards, /v1/review/{id}/merge,
//                                /v1/template/apply, /v1/cluster/stop)
//
// Every one of these endpoints requires `"confirm": true` in the request body
// and returns 409 without it. The client ALWAYS sends `confirm: true`; the
// view is responsible for gating the call behind an explicit confirm UI.
// ---------------------------------------------------------------------------

/// POST /v1/cards — the created card's id + a human message.
struct DispatchCardResponse: Decodable {
    let id: String?
    let message: String?
}

/// POST /v1/review/{card_id}/merge — merge + close result.
///
/// `merged` reflects whether the branch actually landed; `card_closed` whether
/// the card was archived. On a failed merge the API returns a NON-2xx (502) and
/// the client throws — this struct is only ever decoded from a 2xx, so a
/// failure can never be rendered as a merged success. `warning` is set when the
/// merge landed but the card could not be archived (still a 2xx — callers show
/// the warning rather than claiming the card is closed).
struct MergeCardResponse: Decodable {
    let message: String?
    let merged: Bool?
    let card_closed: Bool?
    let warning: String?
}

/// POST /v1/template/apply — template application result.
///
/// The API returns `{ success: true, ... }` on a clean apply and a non-2xx (or
/// `success: false`) for a blocked/partial apply, which the client throws. This
/// struct is only decoded from a 2xx success. `message` is the human summary.
struct TemplateApplyResponse: Decodable {
    let success: Bool?
    let message: String?
}

/// POST /v1/cluster/stop — workload stop result.
///
/// The API returns `{ message, container_id, success: true, ... }` on a clean
/// stop and a non-2xx for a failure, which the client throws. This struct is
/// only decoded from a 2xx success.
struct StopClusterResponse: Decodable {
    let message: String?
    let container_id: String?
    let success: Bool?
}

// ---------------------------------------------------------------------------
// C6 — autodown / projects / ops / board hygiene / fleet control models.
//
// These match the ACTUAL response shapes verified against the live API
// (100.64.0.1:8788) and the source in hscc-api/routes_autodown.py,
// routes_ops.py, routes_kanban.py, routes_project.py, routes_template.py, and
// hscc-cluster/hscc.py. Every server-optional field is Optional in Swift so a
// single absent key can never blank the whole screen.
// ---------------------------------------------------------------------------

// MARK: - Autodown

/// GET /v1/autodown/status — the autodown report.
///
/// Verified live shape:
///   { enabled, state, idle_minutes, last_activity_iso, down_since,
///     wake_source, reason, watchdog_blocked, watchdog_intentional,
///     kanban_ok, kanban_reason, blocked_by, force_armed,
///     force_armed_overrides, active_cron_cpu_only, active_cron_model, speak }
/// Every field is optional except `speak` (which the API always synthesizes).
struct AutodownStatusResponse: Decodable, Speakable {
    let enabled: Bool?
    let state: String?
    let idle_minutes: Int?
    let last_activity_iso: String?
    let down_since: String?
    let wake_source: String?
    let reason: String?
    let watchdog_blocked: Bool?
    /// The watchdog block's `intentional` marker — a STRING ("autodown") when a
    /// teardown is in effect, absent otherwise. Not a Bool: the server passes
    /// `block.get("intentional")` through verbatim (routes_autodown.py:216).
    let watchdog_intentional: String?
    let kanban_ok: Bool?
    let kanban_reason: String?
    let blocked_by: String?
    let force_armed: Bool?
    let force_armed_overrides: [String]?
    let active_cron_cpu_only: [String]?
    let active_cron_model: [String]?
    let speak: String
}

/// POST /v1/autodown/enable (routes_autodown.py handle_autodown_enable).
struct AutodownEnableResponse: Decodable {
    let enabled: Bool?
    let idle_minutes: Int?
    let state: String?
    let last_activity_iso: String?
    let force_armed: Bool?
    let force_armed_overrides: [String]?
    let message: String?
}

/// POST /v1/autodown/disable (routes_autodown.py handle_autodown_disable).
struct AutodownDisableResponse: Decodable {
    let enabled: Bool?
    let state: String?
    let message: String?
}

/// POST /v1/autodown/wake (routes_autodown.py handle_autodown_wake).
///
/// The API returns promptly with `state: waking` and runs autoup() on a
/// background thread (it can block ~9 minutes). The client surfaces this as a
/// "waking" state and the view polls /v1/autodown/status rather than blocking.
struct AutodownWakeResponse: Decodable {
    let result: String?
    let state: String?
    let wake_source: String?
    let message: String?
    let speak: String?
}

/// POST /v1/autodown/cancel (routes_autodown.py handle_autodown_cancel).
struct AutodownCancelResponse: Decodable {
    let cancel_requested: Bool?
    let message: String?
}

// MARK: - Projects

/// GET /v1/projects — one registry row.
///
/// Verified live shape: `{ name, repo, board, topic }`. `topic` is an integer
/// topic id on most rows but the literal string "unknown" when a project has
/// none (routes_project.handle_projects), so it is modeled as `JSONValue?`
/// and rendered via `displayTopic` — a typed `Int?` would blow up the whole
/// list decode on the first topicless row.
struct Project: Decodable, Identifiable {
    let name: String
    let repo: String?
    let board: String?
    let topic: JSONValue?

    var id: String { name }
    var displayTopic: String {
        switch topic {
        case .int(let n): return "\(n)"
        case .string(let s): return s
        default: return "—"
        }
    }
}

/// GET /v1/projects — the list envelope.
struct ProjectsResponse: Decodable, Speakable {
    let projects: [Project]
    let count: Int?
    let speak: String
}

/// GET /v1/projects/{name} — git state bucket.
struct ProjectGit: Decodable {
    let is_repo: Bool?
    let branch: String?
    let dirty: Bool?
    let uncommitted: [String]?
    let last_activity_seconds_ago: Int?
    let head: String?
}

/// GET /v1/projects/{name} — per-project detail.
///
/// `board_counts` maps status -> count (plus a `total` entry). `topic` mirrors
/// the list row (Int id or "unknown"). All fields optional except `speak`.
struct ProjectDetailResponse: Decodable, Speakable {
    let name: String?
    let repo: String?
    let board: String?
    let topic: JSONValue?
    let board_counts: [String: Int]?
    let git: ProjectGit?
    let speak: String

    /// Rendered topic: an int id as-is, the string "unknown" when a project has
    /// none. Mirrors `Project.displayTopic`.
    var displayTopic: String? {
        guard let topic else { return nil }
        switch topic {
        case .int(let n): return "\(n)"
        case .string(let s): return s
        default: return nil
        }
    }
}

// MARK: - Ops / health (verify, daemon, triggers, escalate, profiles)

/// GET /v1/verify — reuses `HealthResponse` (identical shape: ok + checks of
/// {name, ok, detail} + speak). See `HealthResponse` above.
typealias VerifyResponse = HealthResponse

/// GET /v1/daemon/status — daemon PID + every health stream.
///
/// Stream entries carry per-stream extra keys beyond the common {ok,
/// timestamp, stream, message}; those are ignored on decode by `StreamStatus`.
struct DaemonStatusResponse: Decodable, Speakable {
    let daemon_running: Bool?
    let pid: Int?
    let state: String?
    let streams: [String: StreamStatus]?
    let speak: String
}

/// GET /v1/triggers — one trigger rule.
struct TriggerRule: Decodable, Identifiable {
    let id: String
    let trigger_type: String?
    let condition: TriggerCondition?
    let trigger_params: TriggerParams?
}

/// The `condition` bucket of a /v1/triggers rule.
struct TriggerCondition: Decodable {
    let metric: String?
    let op: String?
    let value: JSONValue?
}

/// The `trigger_params` bucket of a /v1/triggers rule.
struct TriggerParams: Decodable {
    let title: String?
    let body: String?
}

/// GET /v1/triggers — rules + last run + recent events.
///
/// `last_run` is a stream-status-shaped dict (timestamp/stream/ok/message plus
/// rules_evaluated/events_checked/actions_fired) — decoded via `StreamStatus`,
/// extra keys ignored. `recent_events` is a list of serialized JSON strings.
struct TriggersResponse: Decodable, Speakable {
    let rules: [TriggerRule]?
    let last_run: StreamStatus?
    let recent_events: [String]?
    let speak: String
}

/// GET /v1/escalate — pending escalations.
///
/// Verified live shape: `{ escalations: [], count: 0, speak }`. Escalation
/// entries vary; they're kept as untyped `JSONValue` so a populated list can
/// never break the decode.
struct EscalationsResponse: Decodable, Speakable {
    let escalations: [JSONValue]?
    let count: Int?
    let speak: String
}

/// GET /v1/profiles — running kanban task counts per profile.
///
/// Verified live shape: `{ counts: {}, total_running: 0, profiles: [], speak }`.
/// `counts` maps profile -> running count; `profiles` entries are kept untyped.
struct ProfilesResponse: Decodable, Speakable {
    let counts: [String: Int]?
    let total_running: Int?
    let profiles: [JSONValue]?
    let speak: String
}

// MARK: - Board hygiene (blocked / recover / stale)

/// GET /v1/kanban/blocked — one blocked card.
///
/// Verified live shape: `{ board, id, status, assignee, age_days, block_kind,
/// why, title, comments }` (routes_kanban.py + kanban_blocked module).
struct BlockedCard: Decodable, Identifiable {
    let board: String?
    let id: String
    let status: String?
    let assignee: String?
    let age_days: Int?
    let block_kind: String?
    let why: String?
    let title: String?
    let comments: [String]?

    var displayTitle: String { title ?? id }
}

/// GET /v1/kanban/blocked — the envelope.
struct KanbanBlockedResponse: Decodable, Speakable {
    let boards: Int?
    let tasks: [BlockedCard]?
    let errors: [String]?
    let count: Int?
    let speak: String
}

/// POST /v1/kanban/blocked/{id}/recover (routes_kanban.py handle_kanban_recover).
struct RecoverCardResponse: Decodable {
    let id: String?
    let board: String?
    let reason: String?
    let message: String?
    let speak: String?
}

/// GET /v1/kanban/stale — one stale card.
///
/// Shape from autodown.list_stale_tasks (autodown.py:439): `{ board, id,
/// status, assignee, age_days, title }`. Same envelope as blocked.
struct StaleCard: Decodable, Identifiable {
    let board: String?
    let id: String
    let status: String?
    let assignee: String?
    let age_days: Int?
    let title: String?

    var displayTitle: String { title ?? id }
}

/// GET /v1/kanban/stale — the envelope (shares the blocked envelope shape).
struct KanbanStaleResponse: Decodable, Speakable {
    let boards: Int?
    let tasks: [StaleCard]?
    let errors: [String]?
    let older_than: Int?
    let count: Int?
    let speak: String
}

// MARK: - Fleet control (cluster up/down + templates)

/// GET /v1/template/list — one template row.
///
/// Verified live shape: `{ name, version, description, families, group }`.
struct ClusterTemplate: Decodable, Identifiable {
    let name: String
    let version: Int?
    let description: String?
    let families: [String]?
    let group: String?

    var id: String { name }
}

/// GET /v1/template/list — the envelope.
struct TemplateListResponse: Decodable, Speakable {
    let templates: [ClusterTemplate]
    let count: Int?
    let speak: String
}

/// GET /v1/template/status — the currently-applied template.
///
/// Verified live shape: `{ applied: { template, applied_at,
/// orchestrator_node, families, units }, note, speak }`. `units` may be an
/// int or a `{total, per_family}` dict depending on whether the applied apply
/// recorded units — so `applied.units` is `JSONValue?` to be safe.
struct TemplateApplied: Decodable {
    let template: String?
    let applied_at: String?
    let orchestrator_node: String?
    let families: [String]?
    let units: JSONValue?
}

/// GET /v1/template/status.
struct TemplateStatusResponse: Decodable, Speakable {
    let applied: TemplateApplied?
    let note: String?
    let speak: String
}

/// GET /v1/template/preview/{name} — one file change the apply would make.
///
/// Verified live shape: `{ file, action, summary, diff_summary?, details? }`.
/// `action` is a short verb ("write" / "update" / "create" / "provision");
/// `summary` is the one-line human description; `details` optional bullet list.
struct TemplateChange: Decodable, Identifiable {
    let file: String?
    let action: String?
    let summary: String?
    let diff_summary: String?
    let details: [String]?

    var id: String { [file ?? "", summary ?? ""].joined(separator: "-") }
}

/// GET /v1/template/preview/{name} — one workload the apply would route.
///
/// Verified live shape: `{ consumer, target, base_url, model, keys }`. A
/// `consumer` (delegation, compaction, …) routes to a `target` unit with a
/// `model`; `keys` are the config paths it would set.
struct TemplateRouting: Decodable, Identifiable {
    let consumer: String?
    let target: String?
    let base_url: String?
    let model: String?
    let keys: [String]?

    var id: String { [consumer ?? "", target ?? "", model ?? ""].joined(separator: "-") }
}

/// GET /v1/template/preview/{name} — dry-run of applying a template.
///
/// Verified live shapes: a FULL preview `{ template, description, changes[],
/// routing[], routing_untouched[], speak }`, OR a minimal `{ speak }` when the
/// template has no preview available yet. Every field is optional so a partial
/// body never blanks the screen — the view falls back to `speak` and notes
/// which details are missing.
struct TemplatePreviewResponse: Decodable, Speakable {
    let template: String?
    let description: String?
    let changes: [TemplateChange]?
    let routing: [TemplateRouting]?
    let routing_untouched: [TemplateRouting]?
    let speak: String
}

/// POST /v1/cluster/up — fleet-up result (routes_ops.py + hscc.py:301).
///
/// `plan` is an array of `{kind, nodes, port, unit_id, cmd, keepalive}` dicts
/// and `issued` an array of per-command run results — both kept untyped so a
/// shape drift never blanks the confirmation "what will happen" labeling.
struct ClusterUpResponse: Decodable {
    let success: Bool?
    let dry_run: Bool?
    let units: Int?
    let plan: [JSONValue]?
    let issued: [JSONValue]?
    let message: String?
    let speak: String?
}

/// POST /v1/cluster/down — fleet-down result (routes_ops.py + hscc.py:285).
struct ClusterDownResponse: Decodable {
    let message: String?
    let speak: String?
}
