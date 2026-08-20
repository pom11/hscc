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
struct ClusterHostsResponse: Decodable, Speakable {
    let hosts: [String]
    let saved_clusters: [String: String]?
    let live_status: [String: String]?
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

/// GET /v1/standup — the daily digest.
struct StandupResponse: Decodable, Speakable {
    let needs_you: [Card]?
    let running: [Card]?
    let failing: [Card]?
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
