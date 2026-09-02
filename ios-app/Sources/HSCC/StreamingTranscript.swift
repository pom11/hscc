import Foundation

// ===========================================================================
// Streaming transcript — the aggregation core of the LIVE chat view.
//
// The session event wire (SessionEvent.swift) is a flat, seq-ordered log of
// envelopes. History pages them out 1:1. A LIVE chat, though, wants a
// COMPOSED transcript:
//
//   * a message turn arrives as MANY `message` events (token deltas) and must
//     be shown as ONE accumulated bubble that streams token-by-token as the
//     deltas land;
//   * a tool call arrives as TWO events — `status == "start"` and
//     `status == "finish"` — that share a `call_id` and must be shown as ONE
//     collapsed chip (`read kanban — 0.4s`), expandable to its args/result;
//   * everything else (card / agent / system / error / unknown) is a
//     single-frame row.
//
// This type is the pure, side-effect-free heart of that composition. It is
// deliberately free of network and UI so it can be proven headlessly against a
// replayed wire stream (scripts/streaming_check.sh compiles THIS real source
// into a macOS CLI and drives it with committed fixtures). The SwiftUI store
// (Views/StreamingChatStore.swift) wraps it, feeds it accepted events, and
// persists the rows — exactly the split used for SessionStreamCursor.
//
// Conversation-ness: the transcript is a WINDOW. It is built FROM the session
// event log, so it reflects the whole session (the orchestrator AND the
// operator), not just the operator's own prompts — that is what makes the
// chat a window onto the project rather than a private log.
// ===========================================================================

/// Renderable metadata for a tool call (aggregates its `start` + `finish`).
struct ToolRender {
    let callID: String
    let name: String
    /// Args captured at `start`. Optional because a `finish` may arrive
    /// without a `start` in the client's window.
    let args: [String: JSONValue]?
    /// Result captured at `finish` (nil while in progress).
    let result: JSONValue?
    /// Elapsed seconds reported at `finish` (nil while in progress).
    let duration: Double?
    /// Whether the `finish` frame has landed.
    let finished: Bool
}

/// One composed row in the live chat transcript.
///
/// Pure render data: each case carries exactly what its row draws. Identity
/// lives on the wrapping `ChatRow` (a stable `id` that survives streaming),
/// not here — so these enum cases stay clean and pattern-matchable.
enum ChatItem {
    /// A message turn. `streaming == true` while deltas are still arriving;
    /// the final delta flips it false. `role` distinguishes the operator's
    /// echo ("user") from the orchestrator ("assistant").
    case message(role: String, text: String, streaming: Bool)
    /// A tool call, composed from its start+finish frames.
    case tool(ToolRender)
    /// A kanban change (tappable chip in the view).
    case card(CardPayload)
    /// A subagent spawned / finished.
    case agent(AgentPayload)
    /// An ambient session fact (cron, crash, escalation, compaction…).
    case system(SystemPayload)
    /// A named, actionable failure.
    case error(ErrorPayload)
    /// A wire type this build does not know — surfaced raw so nothing drops.
    case unknown(type: String, raw: String)
    /// A synthetic notice row the client raises (reconnected, gap detected) —
    /// never a server event, always client-local framing.
    case notice(String)
}

/// A composed chat row: the render data (`item`) plus the stable identity the
/// streaming lifetime needs. `id` is chosen once at row creation so appends to
/// that row (message deltas, tool finish) keep the same identity and SwiftUI
/// can animate them without tearing the list.
struct ChatRow: Identifiable {
    let id: String
    var item: ChatItem

    /// A short factor for debug ("assistant"), for tests and the view footer.
    var kindLabel: String {
        switch item {
        case .message(let role, _, _): return role
        case .tool: return "tool"
        case .card: return "card"
        case .agent: return "agent"
        case .system: return "system"
        case .error: return "error"
        case .unknown: return "unknown"
        case .notice: return "notice"
        }
    }
}

/// The pure aggregation core — fold decoded session events (seq-ascending)
/// into composed chat rows.
struct StreamingTranscript {
    /// Composed rows, oldest → newest.
    private(set) var rows: [ChatRow] = []

    init(rows: [ChatRow] = []) {
        self.rows = rows
    }

    /// Fold one decoded event into the transcript (mutating).
    ///
    /// Callers feed events in seq order — either a replayed history page or
    /// the live/resumed WebSocket stream — AFTER their own seq-gap guard
    /// (SessionStreamCursor) has decided the event is new and contiguous.
    mutating func fold(_ event: SessionEvent) {
        switch event.payload {
        case .hello(let p):
            // Control handshake, not a chat row. The store reads `next_seq`
            // separately for its resume cursor; nothing to render here.
            _ = p
        case .message(let m):
            foldMessage(m, seq: event.seq)
        case .toolCall(let t):
            foldTool(t)
        case .card(let p):
            push(ChatItem.card(p), anchorSeq: event.seq)
        case .agent(let p):
            push(ChatItem.agent(p), anchorSeq: event.seq)
        case .system(let p):
            push(ChatItem.system(p), anchorSeq: event.seq)
        case .error(let p):
            push(ChatItem.error(p), anchorSeq: event.seq)
        case .unknown(let type, let raw):
            push(ChatItem.unknown(type: type, raw: raw), anchorSeq: event.seq)
        }
    }

    /// Append a client-local notice row (reconnected, gap detected, etc.).
    mutating func addNotice(_ text: String) {
        _noticeCounter -= 1
        push(ChatItem.notice(text), anchorSeq: _noticeCounter)
    }

    private var _noticeCounter: Int = 0

    /// Rows shown before the server confirmed them, keyed by their text. The
    /// server echoes every operator message back as a `user` frame, so without
    /// this the optimistic row and the echo would render as TWO identical
    /// bubbles. Instead the echo ADOPTS the pending row (below).
    private var _pendingLocalUserText: [String] = []

    /// Show the operator's own message immediately, before the server echo.
    /// Anchored on a negative counter like notices so its id cannot collide
    /// with a real seq; `foldMessage` re-anchors it when the echo arrives.
    mutating func addLocalUserMessage(_ text: String) {
        _noticeCounter -= 1
        _pendingLocalUserText.append(text)
        push(ChatItem.message(role: "user", text: text, streaming: false),
             anchorSeq: _noticeCounter)
    }

    /// Append a single-frame row with its anchor seq as its identity.
    private mutating func push(_ item: ChatItem, anchorSeq: Int) {
        let id = stableID(for: item, anchorSeq: anchorSeq)
        rows.append(ChatRow(id: id, item: item))
    }

    /// The stable id for a freshly-created row. Messages and tools carry ids
    /// that later frames will REUSE (message deltas stream onto the first
    /// delta's id-seed; tool finish reuses the call_id id), so this must be
    /// deterministic given the item + anchor.
    private func stableID(for item: ChatItem, anchorSeq: Int) -> String {
        switch item {
        case .message(let role, _, _):
            return "m-\(role)-\(anchorSeq)"
        case .tool(let t):
            return "t-\(t.callID)"
        case .card(let p):
            return "c-\(p.id)-\(anchorSeq)"
        case .agent(let p):
            return "a-\(p.role)-\(anchorSeq)"
        case .system(let p):
            return "s-\(p.kind)-\(anchorSeq)"
        case .error(let p):
            return "e-\(p.code)-\(anchorSeq)"
        case .unknown(let type, _):
            return "u-\(type)-\(anchorSeq)"
        case .notice:
            return "n-\(anchorSeq)"
        }
    }

    // MARK: - Message aggregation

    /// Fold a `message` delta. Repeated deltas of the same role accumulate
    /// into ONE streaming bubble until `done` flips it final — that is what
    /// makes the live stream render token-by-token instead of one row per
    /// frame. A `done` delta of the same role closes the bubble so the next
    /// turn starts fresh.
    private mutating func foldMessage(_ m: MessagePayload, seq: Int) {
        // The server echoes the operator's own message back. If we already
        // showed it optimistically, ADOPT that row (re-anchor its id to the
        // real seq) rather than pushing a duplicate bubble.
        if m.role == "user", m.done,
           let pendingIdx = _pendingLocalUserText.firstIndex(of: m.delta),
           let rowIdx = rows.lastIndex(where: {
               if case .message(let r, let t, _) = $0.item { return r == "user" && t == m.delta }
               return false
           }) {
            _pendingLocalUserText.remove(at: pendingIdx)
            rows[rowIdx] = ChatRow(
                id: stableID(for: rows[rowIdx].item, anchorSeq: seq),
                item: rows[rowIdx].item)
            return
        }
        // Is the LAST row an in-progress message from the same role? Stream
        // onto it; otherwise this delta starts a new turn.
        if let idx = rows.indices.last,
           case .message(let role, var text, let streaming) = rows[idx].item,
           role == m.role, streaming {
            text += m.delta
            rows[idx].item = .message(role: m.role, text: text, streaming: !m.done)
            return
        }
        // New turn: anchor to this first delta's seq so the id stays stable as
        // later deltas stream onto the row.
        push(ChatItem.message(role: m.role, text: m.delta, streaming: !m.done),
             anchorSeq: seq)
    }

    // MARK: - Tool aggregation

    /// Fold a tool_call frame. `start` creates a row; `finish` finds that row
    /// by `call_id` and fills in its result — so one tool renders as ONE chip,
    /// not two rows. A `finish` whose `start` fell outside the client's window
    /// still lands as its own lone chip rather than being dropped.
    private mutating func foldTool(_ t: ToolCallPayload) {
        if t.isStart {
            push(ChatItem.tool(ToolRender(callID: t.call_id,
                                          name: t.name,
                                          args: t.args,
                                          result: nil,
                                          duration: nil,
                                          finished: false)),
                 anchorSeq: 0)   // identity is call_id, seq irrelevant
            return
        }
        // `finish`: update the matching in-progress row (same call_id).
        if let idx = rows.indices.last(where: {
            if case .tool(let t0) = rows[$0].item, t0.callID == t.call_id { return true }
            return false
        }) {
            if case .tool(let existing) = rows[idx].item {
                rows[idx].item = .tool(ToolRender(callID: existing.callID,
                                                  name: t.name.isEmpty ? existing.name : t.name,
                                                  args: existing.args ?? t.args,
                                                  result: t.result ?? existing.result,
                                                  duration: t.duration_s ?? existing.duration,
                                                  finished: true))
            }
        } else {
            // A lone `finish` (start predates our window): still surface it.
            push(ChatItem.tool(ToolRender(callID: t.call_id,
                                          name: t.name,
                                          args: t.args,
                                          result: t.result,
                                          duration: t.duration_s,
                                          finished: true)),
                 anchorSeq: 0)
        }
    }
}

// MARK: - Printable rendering for JSONValue (tool args / results / details)

extension JSONValue {
    /// Render a JSON value as one scannable line (for chip captions and the
    /// expanded panel header).
    func renderInline() -> String {
        switch self {
        case .string(let s): return s
        case .int(let i): return String(i)
        case .double(let d): return String(d)
        case .bool(let b): return String(b)
        case .null: return "null"
        case .object(let o):
            let pairs = o.keys.sorted().map { key in "\"\(key)\": \(o[key]?.renderInline() ?? "null")" }
            return "{ " + pairs.joined(separator: ", ") + " }"
        case .array(let a):
            return "[ " + a.map { $0.renderInline() }.joined(separator: ", ") + " ]"
        }
    }
}

extension Dictionary where Key == String, Value == JSONValue {
    /// Render an args (or result) dict into a label: single-line for the
    /// collapsed chip, or pretty multi-line JSON when the chip is expanded.
    func renderArgs(pretty: Bool = false) -> String {
        let separator = pretty ? "\n" : "  "
        let pairs = keys.sorted().map { key -> String in
            let v = self[key]?.renderInline() ?? "null"
            return "\(key): \(v)"
        }
        return pairs.joined(separator: separator)
    }
}
