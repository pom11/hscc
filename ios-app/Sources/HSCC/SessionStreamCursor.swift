import Foundation

/// Client-side reconnect cursor for the streaming orchestrator session (t_218cb9ec).
///
/// The chat pipeline is a WINDOW onto a project's Hermes session. The bridge
/// (`hscc-api`, t_47f51a71) relays session events to the app and tags every
/// event with a monotonically increasing `seq`. Phone networks drop constantly,
/// so the client must be able to resume a dropped stream with NO GAP and NO
/// REPEAT: the operator must never wonder whether their message was sent, and
/// must never see the same event twice.
///
/// This type is the pure, side-effect-free heart of that guarantee. It is
/// deliberately free of network and UI so it can be proven headlessly (the
/// `scripts/reconnect_check.sh` harness compiles this real source into a macOS
/// CLI and replays a dropped stream). The actual app store folds `accept`'s
/// result into the transcript and persists `lastSequence` in the SAME write as
/// the transcript (see `commit` docs), which is what makes the guarantee
/// survive a crash mid-apply as well as a plain network drop.
///
/// ## The wire contract (the seam the bridge owns)
///
/// The bridge assigns each relayed event a `seq` and, when a client presents a
/// `since`/`after` cursor on (re)connect, replays every event with
/// `seq > cursor` in order. Because `seq` is a monotonic sequence and the
/// bridge never drops a number, the disjoint tail the client already folded is
/// `<= lastSequence` and the replayed tail is `> lastSequence` — the two
/// partitions abut with no overlap (no repeat) and no hole (no gap).
///
/// The bridge card (t_47f51a71) owns the SERVER half of this contract: emitting
/// `seq`, honouring `since`, and buffering events for a reconnecting client.
/// This type owns the CLIENT half: remembering the last seq seen, asking for
/// everything after it, and defensively refusing to double-append a duplicate.
public struct SessionStreamCursor: Equatable {
    /// The highest sequence number already folded into the transcript.
    /// Codable so a store can persist it across app relaunch — the cursor would
    /// be useless if a relaunch forgot how far it had got (it would re-replay
    /// old events as if new, creating duplicates).
    public private(set) var lastSequence: UInt64

    /// Monotonic sequence of a relayed event (the bridge's `seq` field).
    public struct Event: Equatable {
        public let seq: UInt64
        public let payload: String
        public init(seq: UInt64, payload: String) {
            self.seq = seq
            self.payload = payload
        }
    }

    /// The outcome of feeding one event to the cursor.
    public enum Decision: Equatable {
        /// The event is exactly the next one expected — it must be appended to
        /// the transcript and `lastSequence` advanced. The payload is echoed so
        /// the store does not need to keep a second reference.
        case accept(Event)
        /// A duplicate (`seq <= lastSequence`) — already folded on a previous
        /// connection. DROP it; the transcript already holds it. Guarding
        /// against this is what makes the stream idempotent under retry.
        case duplicate(Event)
        /// A NON-contiguous jump (`seq > lastSequence + 1`) arrived on a FRESH
        /// (non-resume) stream. This means real events were lost and the client
        /// did not ask for them — the transcript should surface an honest
        /// "some messages may be missing" rather than silently skipping.
        /// The event is refused so the caller keeps the transcript contiguous
        /// and can re-establish with `resumeRequest` to fill the hole.
        case gap(missingFrom: UInt64, missingThrough: UInt64)
    }

    /// A fresh stream (no history). The first accepted event may be any seq —
    /// a `0` cursor means "everything from the beginning", so an out-of-order
    /// first event is a jump, not a gap against nothing.
    public init() {
        self.lastSequence = 0
    }

    /// A resumed stream starting from `lastSequence` (e.g. restored from a
    /// persisted cursor after relaunch).
    public init(lastSequence: UInt64) {
        self.lastSequence = lastSequence
    }

    /// What the client sends on (RE)connect so the bridge replays everything
    /// after the last seq it has folded. `after:` here is the cursor value; the
    /// bridge returns every event with `seq > after`.
    public var resumeRequest: UInt64 { lastSequence }

    /// Feed one relayed event through the cursor.
    ///
    /// - Parameter event: the bridge-relayed event.
    /// - Parameter isResume: true when this connection began by presenting
    ///   `resumeRequest` (a replay tail). During a resume the stream is
    ///   expected to start at `lastSequence + 1` if anything was missed, or at
    ///   the next live event if nothing was — but a replay of `seq > after`
    ///   never emits the already-folded head, so non-contiguity against
    ///   `lastSequence` is NORMAL and must not be flagged as a gap. Only a
    ///   FRESH stream (`isResume == false`) flags jumps, because only then is a
    ///   jump the client's own fault (it did not ask for the missing tail).
    public mutating func accept(_ event: Event, isResume: Bool) -> Decision {
        if event.seq <= lastSequence {
            return .duplicate(event)
        }
        if !isResume && event.seq > lastSequence + 1 {
            // Jumped ahead on a fresh stream. The head 1...lastSequence was not
            // replayed because we never asked; events lastSequence+1 ... seq-1
            // were dropped somewhere. This is the one place a caller can
            // silently hide a gap — refuse and let the caller resume instead.
            return .gap(missingFrom: lastSequence + 1, missingThrough: event.seq - 1)
        }
        // Contiguous and new: fold it in. Advance BEFORE returning so the
        // accepted event is never re-requested (never repeated).
        lastSequence = event.seq
        return .accept(event)
    }
}
