import Foundation
import SwiftUI

// ===========================================================================
// THEME STUB (non-model, build-only)
//
// Same role as `scripts/model_decode_check/ThemeStub.swift`: the app's
// `Theme.swift` is iOS-only (it ships inside the app targets), and this harness
// runs on macOS. The tokens below are the ones the widget/shared sources
// compiled here reference. Nothing here is rendered — the harness asserts on
// rows, statuses and stored bytes, never on pixels — so satisfying the type
// dependency with a stub is what lets the REAL widget file compile and run
// headless. Real tokens are proven by `check_theme.sh`.
//
// Values mirror the real black/orange dark-only palette (operator request
// 2026-11-18) so a stub-using check can never drift to the retired light theme.
// This is the only non-real source in the harness; it declares no model.
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

    /// Raw values matter: the widget uses `.rawValue`, so the scale must be the
    /// same CGFloat-backed enum shape the real one has.
    enum Spacing: CGFloat {
        case xxs = 2
        case xs = 4
        case sm = 8
        case md = 12
        case lg = 16
        case xl = 24
    }

    enum Elevation {
        static var hairline: Color { Palette.hairline }
    }
}
