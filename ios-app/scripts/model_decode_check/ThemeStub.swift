import Foundation
import SwiftUI

// ===========================================================================
// THEME STUB (non-model, build-only)
//
// This is the ONLY thing in the checker that is not a real model source. The
// app's Theme.swift is iOS-only (imports UIKit), but SharedModels.swift
// references Theme.Semantic.* in the `color` computed properties of
// ClusterState and TopologyNode.NodeState. Those colors are never rendered in
// a headless decode CLI, so we satisfy the dependency with a minimal enum.
//
// Theme is a UI design token, NOT an API response model — providing it here
// does not redeclare any model. Every actual model struct lives in the real
// source files compiled in by model_decode_check.sh:
//   Sources/HSCC/Models.swift
//   Sources/Shared/SharedModels.swift
//   Sources/HSCC/APIError.swift
// ===========================================================================
enum Theme {
    enum Semantic {
        static var ok: Color { .green }
        static var warn: Color { .orange }
        static var bad: Color { .red }
        static var neutral: Color { .gray }
    }
}
