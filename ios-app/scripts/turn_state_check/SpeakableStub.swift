// turn_state_check/SpeakableStub.swift — the one symbol SessionEvent.swift's
// SessionHistoryResponse conformance needs that we don't otherwise compile
// here (the real one lives in Sources/Shared/SharedModels.swift and drags in
// a large, view-independent model surface we don't need to prove the
// turn-state mapping). Mirrors streaming_check's ThemeStub pattern.
import Foundation

/// Minimal stand-in: the app's responses carry a first-class `speak` field.
/// We only need the protocol name to exist so SessionEvent.swift compiles.
protocol Speakable {
    var speak: String { get }
}
