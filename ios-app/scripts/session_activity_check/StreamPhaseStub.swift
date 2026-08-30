import Foundation

// StreamPhaseStub — a minimal, self-contained copy of `StreamPhase` for the
// headless session_activity_check CLI.
//
// `StreamPhase` (aliased as `ConnectionPhase`) is declared inside
// StreamingChatStore.swift, which imports Combine and the network layer — not
// convenient to compile into a plain macOS CLI. `SessionActivitySummary` only
// needs the enum type and its cases, so this stub mirrors the real declaration
// exactly. It must be kept in lockstep with
// Sources/HSCC/Views/StreamingChatStore.swift lines ~41-52 (which remain the
// source of truth — production always uses the real enum).

enum StreamPhase: Equatable {
    case idle            // never attempted
    case loadingHistory  // fetching the seed page
    case connecting      // opening the WebSocket
    case connected       // live (replay tail folded, now streaming)
    case reconnecting    // socket dropped, retrying with resume
    case failed(String)  // could not connect; honest reason

    /// True when the operator is seeing LIVE updates (or the attempt to get
    /// there is in progress) — as opposed to a parked/stale transcript.
    var isLive: Bool { self == .connected || self == .reconnecting }
}

typealias ConnectionPhase = StreamPhase
