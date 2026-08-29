import Foundation

// ===========================================================================
// Session event wire models — the per-project chat event log contract.
//
// Mirrors hscc-api/session_event.py (THE locked wire contract, committed on
// dev by t_47f51a71). Every event on the wire is an ENVELOPE:
//
//     {"seq": 42, "type": "message", "ts": "2026-08-29T00:00:00Z",
//      "payload": {...}}
//
//   * `seq`  — monotonically increasing per project; contiguous 1..N. THE
//     paging/reconnect cursor. History and the live WebSocket share one seq
//     space, so a client can page history down to `seq` and continue from
//     `seq+1` with no gap and no duplicate.
//   * `type` — one of the TYPE_* cases (hello/message/tool_call/card/agent/
//     system/error). Determines the payload shape.
//   * `ts`   — ISO-8601 UTC timestamp the event was produced.
//   * `payload` — type-specific shape (one struct per type below).
//
// This file is the SHARED decode layer: the history pager (t_2776ea3c) and the
// future typed-event streaming view (t_1ff4dcbd) both consume these same
// types, so the wire contract lives in exactly one place.
// ===========================================================================

/// GET /v1/projects/{name}/session/events — one page of the project's log.
///
/// Verified shape: `{ project, events: [envelope], next_before, oldest_seq,
/// next_seq, speak }`. ``events`` is seq-ASCENDING within a page. ``next_before``
/// is the cursor for the next OLDER page (nil when this page already reaches
/// the oldest retained frame). ``oldest_seq``/``next_seq`` are the retained
/// low and high water marks the pager uses for its footer + gap awareness.
struct SessionHistoryResponse: Decodable, Speakable {
    let project: String
    let events: [SessionEvent]
    let next_before: Int?
    let oldest_seq: Int
    let next_seq: Int
    let speak: String
}

/// One complete frame: envelope + decoded payload.
///
/// ``payload`` is decoded into the concrete type matching ``type`` (see
/// ``ParsedPayload``). Unknown/forward-compatible event types decode as
/// ``.unknown`` (carrying the raw JSON) so an older app never blanks history
/// when the backend grows a new type.
struct SessionEvent: Decodable, Identifiable {
    let seq: Int
    let type: String
    let ts: String          // ISO-8601 UTC
    let payload: ParsedPayload

    var id: Int { seq }

    private enum CodingKeys: String, CodingKey {
        case seq, type, ts, payload
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = try c.decode(Int.self, forKey: .seq)
        type = try c.decode(String.self, forKey: .type)
        ts = try c.decodeIfPresent(String.self, forKey: .ts) ?? ""
        // Decode payload into its concrete type; fall back to raw JSON on an
        // unknown/mismatched type so one bad frame never breaks history decode.
        let container = try c.nestedContainer(keyedBy: DynamicKey.self, forKey: .payload)
        let data = try c.superDecoder(forKey: .payload)
        payload = ParsedPayload.parse(type: type, container: container, data: data)
    }

    struct DynamicKey: CodingKey {
        var stringValue: String
        var intValue: Int?
        init?(stringValue: String) { self.stringValue = stringValue; intValue = nil }
        init?(intValue: Int) { self.intValue = intValue; stringValue = "\(intValue)" }
    }
}

/// The decoded payload of a session event — one case per wire `type`.
enum ParsedPayload {
    case hello(HelloPayload)
    case message(MessagePayload)
    case toolCall(ToolCallPayload)
    case card(CardPayload)
    case agent(AgentPayload)
    case system(SystemPayload)
    case error(ErrorPayload)
    /// A type this build does not know (newer backend). Carries the raw text
    /// so the timeline can still surface it instead of dropping it.
    case unknown(type: String, raw: String)

    /// Decode the envelope's payload container into the case matching `type`.
    /// Never throws for a known-shape mismatch — it degrades to `.unknown`.
    static func parse(type: String,
                      container: KeyedDecodingContainer<SessionEvent.DynamicKey>,
                      data: Decoder) -> ParsedPayload {
        do {
            switch type {
            case "hello":    return .hello(try HelloPayload(from: data))
            case "message":  return .message(try MessagePayload(from: data))
            case "tool_call": return .toolCall(try ToolCallPayload(from: data))
            case "card":     return .card(try CardPayload(from: data))
            case "agent":    return .agent(try AgentPayload(from: data))
            case "system":   return .system(try SystemPayload(from: data))
            case "error":    return .error(try ErrorPayload(from: data))
            default:
                // Unknown type — keep the raw object for a degraded render.
                var raw = ""
                if let data = try? JSONSerialization.data(withJSONObject: rawObject(container)) {
                    raw = String(data: data, encoding: .utf8) ?? ""
                }
                return .unknown(type: type, raw: raw)
            }
        } catch {
            // A known type that failed to decode its payload — surface as raw
            // text rather than dropping the whole frame.
            var raw = ""
            if let data = try? JSONSerialization.data(withJSONObject: rawObject(container)) {
                raw = String(data: data, encoding: .utf8) ?? ""
            }
            return .unknown(type: type, raw: raw)
        }
    }

    private static func rawObject(_ container: KeyedDecodingContainer<SessionEvent.DynamicKey>) -> [String: Any] {
        var out: [String: Any] = [:]
        for key in container.allKeys {
            if let v = try? container.decode(JSONValue.self, forKey: key) {
                out[key.stringValue] = anyValue(v)
            }
        }
        return out
    }

    private static func anyValue(_ value: JSONValue) -> Any {
        switch value {
        case .string(let s): return s
        case .int(let i): return i
        case .double(let d): return d
        case .bool(let b): return b
        case .null: return NSNull()
        case .array(let a): return a.map(anyValue)
        case .object(let o): return o.mapValues(anyValue)
        }
    }
}

// ---------------------------------------------------------------------------
// Payloads — one struct per wire `type`. Field names mirror the API's JSON
// exactly (snake_case source keys stay literal; no mapping hacks).
// ---------------------------------------------------------------------------

/// `hello` — stream-open handshake: the high-water seq the client continues
/// from. Trivial in history (a stamped frame) but the contract pins it.
struct HelloPayload: Decodable {
    let next_seq: Int
}

/// `message` — a token delta on the orchestrator session.
///
/// Streams as an ordered sequence of `delta` fragments; the final fragment of
/// a turn carries `done = true`. `role` is "user" (echo) or "assistant".
/// In the HISTORY store the bridge stows complete frames, so a stored message
/// is typically a whole turn (`done == true`), but the model supports deltas.
struct MessagePayload: Decodable {
    let role: String
    let delta: String
    let done: Bool
}

/// `tool_call` — a tool invocation started or finished. Two frames share a
/// `call_id`: `status == "start"` (name + args, no result) and `status ==
/// "finish"` (name + result + elapsed). `args`/`result`/`duration_s` are
/// emitted only when present, so they're all optional here. `result` is
/// arbitrary JSON (== `Any`), carried as `JSONValue` for printable rendering.
struct ToolCallPayload: Decodable {
    let call_id: String
    let name: String
    let status: String               // "start" | "finish"
    let args: [String: JSONValue]?
    let result: JSONValue?
    let duration_s: Double?

    var isStart: Bool { status == "start" }
    var isFinish: Bool { status == "finish" }
}

/// `card` — a kanban card changed state (moved, created, blocked, closed).
struct CardPayload: Decodable {
    let board: String
    let id: String
    let title: String
    let status: String
}

/// `agent` — a subagent/orchestrator spawned or finished.
struct AgentPayload: Decodable {
    let role: String                 // profile name, e.g. "researcher-a"
    let action: String               // "spawned" | "finished"
    let task: String?

    var isSpawned: Bool { action == "spawned" }
    var isFinished: Bool { action == "finished" }
}

/// `system` — an ambient session fact the operator decided was worth surfacing
/// (cron firing, worker crash, escalation, compaction, session rotated).
struct SystemPayload: Decodable {
    let kind: String
    let details: [String: JSONValue]?
}

/// `error` — a named, actionable failure.
struct ErrorPayload: Decodable {
    let code: String
    let message: String
}
