import Foundation
import SwiftUI

// ===========================================================================
// THEME STUB (non-model, build-only)
//
// This is the ONLY thing in the checker that is not a real model source. The
// app's Theme.swift is iOS-only, but SharedModels.swift references
// Theme.Semantic.* in the `color` computed properties of ClusterState and
// TopologyNode.NodeState. Those colors are never rendered in a headless decode
// CLI, so we satisfy the dependency with a minimal enum.
//
// Theme is a UI design token, NOT an API response model — providing it here
// does not redeclare any model. Every actual model struct lives in the real
// source files compiled in by model_decode_check.sh:
//   Sources/HSCC/Models.swift
//   Sources/Shared/SharedModels.swift
//   Sources/HSCC/APIError.swift
//
// Values mirror the real black/orange dark-only palette (operator request
// 2026-11-18) so a stub-using check can never drift to the retired light theme.
// Every token is declared, not just the four the current call sites use, so
// adding a token to a shared model does not break these harnesses.
// ===========================================================================
private func hsccThemeColor(_ value: UInt32) -> Color {
    Color(red: Double((value >> 16) & 0xFF) / 255.0,
          green: Double((value >> 8) & 0xFF) / 255.0,
          blue: Double(value & 0xFF) / 255.0)
}

enum Theme {
    enum Palette {
        static let graphite = hsccThemeColor(0x000000)
        static let raised = hsccThemeColor(0x111111)
        static let elevated = hsccThemeColor(0x1C1C1E)
        static let accent = hsccThemeColor(0xFF5722)
        static let accentInk = hsccThemeColor(0x000000)
        static let white = hsccThemeColor(0xFFFFFF)
        static let mist = hsccThemeColor(0x9A9A9A)
        static let signal = hsccThemeColor(0x4ADE80)
        static let thermal = hsccThemeColor(0xFFC857)
        static let halt = hsccThemeColor(0xFF6B6B)
        static let hairline = hsccThemeColor(0x2A2A2A)
    }

    enum Semantic {
        static var surface: Color { Palette.graphite }
        static var surfaceRaised: Color { Palette.raised }
        static var surfaceElevated: Color { Palette.elevated }
        static var onSurface: Color { Palette.white }
        static var onSurfaceMuted: Color { Palette.mist }
        static var ok: Color { Palette.signal }
        static var warn: Color { Palette.thermal }
        static var bad: Color { Palette.halt }
        static var neutral: Color { Palette.mist }
        static var accent: Color { Palette.accent }
        static var onAccent: Color { Palette.accentInk }
        static func tintOverlay(_ base: Color) -> Color { base.opacity(0.16) }
    }
}
