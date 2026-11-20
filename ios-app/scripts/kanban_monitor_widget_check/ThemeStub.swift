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

// MARK: - WidgetStyle SwiftUI bridge (mirrors Sources/HSCC/Views/Theme.swift)

extension WidgetColor {
    var color: Color { Color(red: red, green: green, blue: blue, opacity: alpha) }
}

extension WidgetStyle {
    var backgroundColor: Color { background.color }
    var primaryTextColor: Color { primaryText.color }
    var secondaryTextColor: Color { secondaryText.color }
    var accentColor: Color { accent.color }
    var okColor: Color { statusOk.color }
    var warnColor: Color { statusWarn.color }
    var badColor: Color { statusBad.color }
    var raisedColor: Color { primaryTextColor.opacity(0.07) }
    var hairlineColor: Color { secondaryTextColor.opacity(0.25) }

    var swiftUIFontDesign: Font.Design {
        switch fontDesign {
        case .standard: return .default
        case .rounded: return .rounded
        case .monospaced: return .monospaced
        case .serif: return .serif
        }
    }
    var swiftUIFontWeight: Font.Weight {
        switch fontWeight {
        case .regular: return .regular
        case .medium: return .medium
        case .semibold: return .semibold
        case .bold: return .bold
        }
    }
    func scaled(_ size: CGFloat) -> CGFloat { max(1, size * CGFloat(textScale)) }
    func font(size: CGFloat, weight: Font.Weight? = nil) -> Font {
        Font.system(size: scaled(size), weight: weight ?? swiftUIFontWeight, design: swiftUIFontDesign)
    }
    func font(_ textStyle: Font.TextStyle, weight: Font.Weight? = nil) -> Font {
        let size: CGFloat
        switch textStyle {
        case .largeTitle: size = 34
        case .title: size = 28
        case .title2: size = 22
        case .title3: size = 20
        case .headline: size = 17
        case .body: size = 17
        case .callout: size = 16
        case .subheadline: size = 15
        case .footnote: size = 13
        case .caption: size = 12
        case .caption2: size = 11
        @unknown default: size = 17
        }
        return font(size: size, weight: weight)
    }
    func spacing(_ value: CGFloat) -> CGFloat { density == .compact ? max(1, value * 0.75) : value }
    func corner(_ value: CGFloat) -> CGFloat { density == .compact ? max(4, value * 0.8) : value }
    func cardStatusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return okColor
        case "review", "blocked": return warnColor
        case "done", "merged", "closed": return okColor
        case "failed", "failing": return badColor
        default: return secondaryTextColor
        }
    }
}

private struct WidgetStyleEnvironmentKey: EnvironmentKey {
    static let defaultValue = WidgetStyle()
}
extension EnvironmentValues {
    var widgetStyle: WidgetStyle {
        get { self[WidgetStyleEnvironmentKey.self] }
        set { self[WidgetStyleEnvironmentKey.self] = newValue }
    }
}
extension View {
    func widgetStyle(_ style: WidgetStyle) -> some View { environment(\.widgetStyle, style) }
    func widgetIcon(_ style: WidgetStyle) -> some View { opacity(style.showIcons ? 1 : 0) }
    func widgetHeader(_ style: WidgetStyle) -> some View { opacity(style.showHeader ? 1 : 0) }
}
struct WidgetLabelStyle: LabelStyle {
    @Environment(\.widgetStyle) private var style
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            if style.showIcons { configuration.icon }
            configuration.title
        }
    }
}
extension LabelStyle where Self == WidgetLabelStyle {
    static var widget: WidgetLabelStyle { WidgetLabelStyle() }
}
