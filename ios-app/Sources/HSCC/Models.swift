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
//
// NOTE: The `Speakable` protocol and the shared endpoint models that the
// widget / Live Activity extensions also need (`AutodownStatusResponse`,
// `ClusterStatusResponse`, `ClusterWorkload`, `TopologyPair`, `TopologyNode`)
// live in Sources/Shared/ so all targets compile ONE definition instead of
// duplicating. This file owns app-only + extension-specific surface models.
// ---------------------------------------------------------------------------

// MARK: - Shared error shape (design §C)

/// The unified error object inside `{ \"error\": { ... } }`.
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

// MARK: - Slash commands

/// One slash command from GET /v1/commands — the server-driven catalog the
/// chat palette lists. NOT a hardcoded Swift array (that would rot the moment
/// a command is added/removed server-side): it is fetched live from the
/// endpoint, which itself sources from the authoritative `hscc-commands`
/// plugin `register()`.
struct SlashCommand: Decodable, Hashable, Identifiable {
    let name: String
    let description: String
    let takesArgs: Bool

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, description
        case takesArgs = "takes_args"
    }
}

/// GET /v1/commands — the slash commands available to the chat client.
struct CommandsResponse: Decodable, Speakable {
    let commands: [SlashCommand]
    let speak: String
}

// MARK: - Cluster

/// `hosts` is an array of node dicts `{ id, name, ip, role, ssh_user }` (every
/// field may be null — e.g. the NAS host has no id/name/ssh_user), so it is
/// decoded as untyped `[JSONValue]`. `saved_clusters` and `live_status` are
/// raw `run_cmd`-shaped dicts from the cluster engine (keys like
/// `success`/`returncode`/`output` with MIXED value types — bool, int, string),
/// also decoded as untyped `[String: JSONValue]`. None are rendered in the
/// current UI (the topology strip derives from `/v1/cluster/status`), but a
/// wrongly-typed field here makes the WHOLE response throw on decode and blanks
/// the Hosts load state for no visible reason. (Verified against the live
/// /v1/cluster/hosts response 2026-08-27.)
struct ClusterHostsResponse: Decodable, Speakable {
    let hosts: [JSONValue]
    let saved_clusters: [String: JSONValue]?
    let live_status: [String: JSONValue]?
    let speak: String
}

/// GET /v1/health — one fleet check entry.
///
/// `ok` is the server's documented TRI-STATE (`bool | None`): `true` = pass,
/// `false` = hard fail, `nil` = could not be verified (not a pass, not a fail).
/// It must be Optional or a single unverified check (e.g. `api_routes` racing
/// a repo-tree absence) fails the decode of the WHOLE health/verify response.
struct HealthCheck: Decodable, Identifiable {
    let name: String
    let ok: Bool?
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
    /// Full card description. The API returns it (live-checked 2026-09: the
    /// detail endpoint carries `body`, `assignee`, `board` beyond the four
    /// fields the detail view originally declared). OPTIONAL so old captures
    /// still decode; the view drops the row when it is absent.
    let body: String?
    let assignee: String?
    let board: String?
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

// MARK: - Orchestrator chat (C5, job-based)

/// POST /v1/orchestrator/chat — the immediate 202 response that STARTS a job.
///
/// Matching `hscc-api/routes_orchestrator.py`: the POST validates + resolves
/// the project synchronously, spawns a background thread that actually invokes
/// the orchestrator, and returns **202 Accepted** with a `job_id` — NOT the
/// reply. This kills the 90 s dead wait on the phone: the call returns in
/// milliseconds, and the answer is collected by polling
/// `GET /v1/orchestrator/chat/{id}` (see `OrchestratorChatJobStatus`).
///
/// A non-2xx (409/400/502/503/504) still makes the client throw — it never
/// yields a job object. The orchestrator is messaged asynchronously after this
/// returns, so a killed/backgrounded app can still pick the answer up later by
/// job_id.
struct OrchestratorChatJobResponse: Decodable, Speakable {
    let jobID: String
    let project: String?
    let status: String
    let elapsed: Double
    let profile: String?
    let session: String?
    let speak: String

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case project, status, elapsed, profile, session, speak
    }
}

/// GET /v1/orchestrator/chat/{id} — a polled job's current state.
///
/// `status` is `queued` / `running` / `done`, or a terminal failure state
/// (`timeout` / `unavailable` / `error`). `reply` + `speak` are present only
/// when `status == "done"`; `error` is present only on a terminal failure and
/// carries the unified `{ code, message, speak }` shape. `elapsed` is the
/// honest server-side wall-clock from submission.
struct OrchestratorChatJobStatus: Decodable {
    let jobID: String
    let project: String?
    let status: String
    let elapsed: Double
    let reply: String?
    let profile: String?
    let session: String?
    let speak: String?
    let error: ChatJobError?

    enum CodingKeys: String, CodingKey {
        case jobID = "job_id"
        case project, status, elapsed, reply, profile, session, speak, error
    }

    var isTerminal: Bool { status == "done" || status == "timeout" || status == "unavailable" || status == "error" }
}

/// The unified `{ code, message, speak }` error carried on a failed chat job.
struct ChatJobError: Decodable {
    let code: String
    let message: String
    let speak: String?
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

// MARK: - Autodown (attachment models; the shared `/v1/autodown/status`
// `AutodownStatusResponse` lives in Sources/Shared/SharedModels.swift)

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
    /// Commits on the local branch not on its tracking upstream (to push).
    let ahead: Int?
    /// Commits on the upstream not on the local branch (to pull).
    let behind: Int?
}

/// GET /v1/projects/{name} — the chat session's health bucket (routes_project
/// surfaces routes_orchestrator._session_health for the `<name>-orch` profile /
/// `<name>` session). Mirrors the backend keys so the operator sees the bloat
/// signals approaching the context ceiling before it wedges.
///
/// `compaction_at_risk` is the alert: positive evidence compaction is not
/// firing (a compression_failure_error / fallback streak / ineffective count),
/// NOT raw input_tokens — that column is a cumulative counter never reset by
/// compaction. `threshold_tokens` is the ensured compaction cap.
struct ProjectSessionHealth: Decodable {
    let profile: String?
    let session: String?
    let messages: Int?
    let input_tokens: Int?
    let compression_failure_error: String?
    let compression_fallback_streak: Int?
    let compression_ineffective_count: Int?
    let context_window: Int?
    let threshold_tokens: Int?
    let compaction_at_risk: Bool?
    let bloated: Bool?
    let reason: String?
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
    let session_health: ProjectSessionHealth?
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

/// GET /v1/profiles/list — one entry in the full profile list.
///
/// Verified live shape (routes_profiles.py `_profile_info_to_dict`):
/// `{ name, is_default, gateway_running, model, provider, skill_count,
///   description, description_auto, distribution_name, distribution_version }`.
/// Only `name` is required for the memory picker; the rest are kept for
/// display. Never any secrets.
struct ProfileSummary: Decodable, Identifiable, Equatable, Hashable {
    let name: String
    let is_default: Bool?
    let gateway_running: Bool?
    let model: String?
    let provider: String?
    let skill_count: Int?
    let description: String?

    var id: String { name }
}

/// GET /v1/profiles/list — every profile the API can see.
///
/// This is the REAL full profile roster (`hermes_cli.profiles.list_profiles()`),
/// NOT the running-task subset that `/v1/profiles` (ProfilesResponse) serves.
/// The memory picker consumes this so the operator can select any profile by
/// name without knowing the slug by heart. Shape:
/// `{ profiles: [{name, ...}], count, speak }`.
struct ProfileListResponse: Decodable, Speakable {
    let profiles: [ProfileSummary]?
    let count: Int?
    let speak: String
}

// MARK: - Profile editor (per-project profile read / edit)

/// GET/POST /v1/profile/editor/{profile} — a profile's editable surface.
///
/// The per-project editor targets the orchestrator's `<project>-orch` profile
/// (the project's bot). Shape: `{ profile, model, provider, toolsets,
/// preload_skills, description, compression {threshold, threshold_tokens},
/// toolsets_all, skills_all, speak }` plus `updated` on a successful POST.
struct ProfileCompression: Decodable, Equatable {
    let threshold: Double?
    let threshold_tokens: Int?
}

struct ProfileEditorResponse: Decodable, Speakable {
    let profile: String?
    let model: String?
    let provider: String?
    let toolsets: [String]?
    let preload_skills: [String]?
    let description: String?
    let compression: ProfileCompression?
    let toolsets_all: [String]?
    let skills_all: [String]?
    let updated: [String]?
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

    /// Whether this blocked card is a PENDING APPROVAL — i.e. genuinely waiting
    /// on a human decision (approvals inbox, t_9a5cfc3b).
    ///
    /// The `kanban_block` tool's kinds:
    ///   * `needs_input`, `capability` (and missing/unclassified) → a human must
    ///     decide → APPROVAL.
    ///   * `dependency`, `transient` → auto-resume; no human in the loop → NOT.
    var isPendingApproval: Bool {
        switch block_kind {
        case "dependency", "transient":
            return false
        default:
            return true
        }
    }
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

/// GET /v1/kanban/stale — the envelope (shares the blocked envelope shape,
/// EXCEPT `boards` is an array of board name STRINGS here, while /v1/kanban/blocked
/// reports a board COUNT int — verified live 2026-08-27).
struct KanbanStaleResponse: Decodable, Speakable {
    let boards: [String]?
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

// MARK: - Sessions manager (list / retire / compact a profile's sessions)

/// One session row from GET /v1/sessions?profile=<name> (routes_sessions.py).
///
/// Verified shape (tests/ + routes_sessions._row_to_session): every field is
/// present on a well-formed row. Token totals mirror the `sessions` columns
/// SessionDB maintains; ``total_tokens`` is the sum across all streams.
/// ``bloated`` is decided on POSITIVE compaction-failure evidence (never on
/// size alone — ``input_tokens`` is cumulative), and ``compaction_headroom``
/// = ``context_window`` − ``threshold_tokens``, the guaranteed room the early
/// compaction cap leaves for the compress call.
struct SessionItem: Decodable, Identifiable {
    let id: String
    let title: String?
    let source: String?
    let model: String?
    let message_count: Int?
    let tool_call_count: Int?
    let input_tokens: Int?
    let output_tokens: Int?
    let cache_read_tokens: Int?
    let cache_write_tokens: Int?
    let reasoning_tokens: Int?
    let total_tokens: Int?
    // The wire sends these as epoch REALs (float seconds), not ISO strings —
    // SessionDB stores them as `started_at REAL` / `ended_at REAL`. The app is
    // honest about the shape even though no surface formats them yet.
    let started_at: Double?
    let ended_at: Double?
    let archived: Bool?
    let pinned: Bool?
    let compression_failure_error: String?
    let compression_fallback_streak: Int?
    let compression_ineffective_count: Int?
    let context_window: Int?
    let threshold_tokens: Int?
    let compaction_headroom: Int?
    let bloated: Bool?
    let reason: String?

    var displayTitle: String { title ?? id }
    var isBloated: Bool { bloated ?? false }

    /// Human token count for the list row (e.g. "12.5k").
    var tokenSummary: String {
        formatCount(total_tokens ?? input_tokens ?? 0)
    }

    private func formatCount(_ n: Int) -> String {
        if n >= 1000 { return String(format: "%.1fk", Double(n) / 1000) }
        return "\(n)"
    }
}

/// GET /v1/sessions?profile=<name> — the envelope.
///
/// Verified shape: `{ profile, sessions: [], count, bloated_count, speak }`.
/// ``sessions`` is kept optional so a partial body never blanks the screen.
struct SessionsListResponse: Decodable, Speakable {
    let profile: String?
    let sessions: [SessionItem]?
    let count: Int?
    let bloated_count: Int?
    let speak: String
}

/// POST /v1/sessions/{id}/retire and /v1/sessions/{id}/compact — shared result.
///
/// Retire: `{ session_id, previous_title?, retired_title?, message, speak }`.
/// Compact: `{ session_id, title?, message, speak }`. All optional so either
/// shape decodes cleanly.
struct SessionMutationResponse: Decodable, Speakable {
    let session_id: String?
    let previous_title: String?
    let retired_title: String?
    let title: String?
    let message: String?
    let speak: String
}

// MARK: - Live agent activity feed (flight recorder)

/// One row of the live agent activity feed (GET /v1/activity/feed).
///
/// Two kinds, both in one timeline:
///   * ``kind == "running"`` — a worker is on a card (emitted per running
///     card even if that profile has no tool call in the window, so "who is
///     running what" is always visible);
///   * ``kind == "tool_call"`` — a specific tool the profile just called,
///     tied to its card.
/// ``tool`` is the tool name (dotted namespaces reduced to the head, e.g.
/// ``build_server.run`` → ``build_server``). The ``at`` field is a UTC ISO
/// timestamp used for the newest-first sort. ``profile`` +
/// ``card_id`` + ``session_id`` give the operator everything needed to
/// TAP-TO-TRACE back to the source session or card.
struct ActivityEntry: Decodable, Identifiable {
    let kind: String?
    let profile: String?
    let board: String?
    let card_id: String?
    let card_title: String?
    let pid: Int?
    let host_local: Bool?
    let started_at: String?
    let at: String?
    let tool: String?
    let session_id: String?

    /// Identifiable — the feed has no server-supplied id, so synthesize a
    /// reasonably stable one from the fields that define a row.
    var id: String {
        "\(profile ?? "")|\(kind ?? "")|\(at ?? "")|\(tool ?? "")|\(session_id ?? "")"
    }

    var isRunning: Bool { kind == "running" }

    /// The kind label for the list row's leading glyph / badge.
    var kindLabel: String { isRunning ? "Running" : "Tool" }
}

/// GET /v1/activity/feed — the envelope.
///
/// Verified shape: `{ entries: [], count, running_count, profiles, speak }`.
/// ``entries`` is kept optional so a partial body never blanks the screen.
struct ActivityFeedResponse: Decodable, Speakable {
    let entries: [ActivityEntry]?
    let count: Int?
    let running_count: Int?
    let profiles: [String]?
    let speak: String
}

/// One memory card from GET /v1/memory (the memory viewer, t_e8ffd787).
///
/// Mirrors the API card shape: ``node_id`` is the stable graph id
/// (``memory:<memory|profile>:<index>``) the operator passes back to
/// correct/delete; ``body`` is the FULL entry text (not truncated — the viewer
/// shows everything the agent remembers).
struct MemoryItem: Decodable, Identifiable {
    let id: String
    let node_id: String?
    let source: String?
    let kind: String?
    let timestamp: Int?
    let title: String?
    let body: String?

    var nodeID: String { node_id ?? id }
    var sourceLabel: String { source == "profile" ? "Profile" : "Notes" }
}

/// GET /v1/memory?profile=<name> — the envelope.
///
/// Verified shape: `{ profile, memories: [], count, memory_count,
/// profile_count, speak }`. ``memories`` is kept optional so a partial body
/// never blanks the screen.
struct MemoryListResponse: Decodable, Speakable {
    let profile: String?
    let memories: [MemoryItem]?
    let count: Int?
    let memory_count: Int?
    let profile_count: Int?
    let speak: String
}

/// POST /v1/memory/{node_id}/delete and /v1/memory/{node_id}/edit — shared result.
///
/// Delete: `{ node_id, kind, title?, message, speak }`.
/// Edit: `{ node_id, kind, previous_title?, message, speak }`.
struct MemoryMutationResponse: Decodable, Speakable {
    let node_id: String?
    let kind: String?
    let title: String?
    let previous_title: String?
    let message: String?
    let speak: String
}
