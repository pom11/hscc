import Foundation
import SwiftUI

// ===========================================================================
// THEME STUB (non-model, build-only)
//
// Same role as `scripts/model_decode_check/ThemeStub.swift`: the app's
// `Theme.swift` is iOS-only (it builds colours through `UIColor`), and this
// harness runs on macOS, where there is no iOS runtime. The tokens below are
// the ones `Sources/HSCCWidgets/ProjectsWidget.swift` and
// `Sources/Shared/SharedModels.swift` reference. Nothing here is rendered —
// the harness asserts on rows, statuses and stored bytes, never on pixels — so
// satisfying the type dependency with a stub is what lets the REAL widget file
// compile and run headless. Real tokens are proven by `check_theme.sh`.
//
// This is the only non-real source in the harness; it declares no model.
// ===========================================================================
enum Theme {
    enum Semantic {
        static var surface: Color { .black }
        static var onSurface: Color { .white }
        static var onSurfaceMuted: Color { .gray }
        static var ok: Color { .green }
        static var warn: Color { .orange }
        static var bad: Color { .red }
        static var neutral: Color { .gray }
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
        static var hairline: Color { .gray }
    }
}
