import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// Widget rendering-mode adaptation (card t_1143c19b, part 2)
//
// The same widget roots ship on the iOS Home Screen and the macOS desktop /
// Notification Center. WidgetKit renders a widget in one of three modes:
//
//   * fullColor — the operator's WidgetStyle is drawn as saved;
//   * accented  — macOS desktop (and the iOS tinted Home Screen) composits the
//                 widget over the wallpaper and renders it monochrome, tinted
//                 by the system;
//   * vibrant   — the dimmed/translucent desktop treatment.
//
// A custom black background or a fixed white/black text colour is wrong in the
// last two: the system supplies a material whose lightness follows the desktop,
// so a hard-coded palette can disappear. The adapters below keep the SIZE,
// FONT, DENSITY, header and icon choices from the operator's style, but swap
// the colour roles for the system's primary/secondary equivalents (approximated
// as pure ink/paper picked by `colorScheme`, because the stored model is
// Foundation-only) and stop painting the custom background. Full-colour mode is
// untouched, so the style the operator previewed is exactly what ships there.
//
// ONE implementation for both platforms: the widgets stay reused by path, and
// this file is the only place a rendering mode is named.
// ---------------------------------------------------------------------------

/// The widget root's background. Painted only in full-colour rendering; in
/// accented/vibrant mode the system's wallpaper material is the background, so
/// drawing an opaque fill there would undo the effect the operator chose in
/// System Settings.
struct WidgetSurfaceBackground: View {
    @Environment(\.widgetRenderingMode) private var renderingMode
    let style: WidgetStyle

    var body: some View {
        if renderingMode == .fullColor {
            style.backgroundColor
        } else {
            Color.clear
        }
    }
}

extension WidgetStyle {
    /// The palette used when WidgetKit ignores custom colours. Fonts, weights,
    /// text scale, density and the header/icon switches survive untouched: on
    /// macOS those are still the operator's choice, only the colours become the
    /// system's so the tile reads on a light or dark wallpaper.
    func systemTinted(for colorScheme: ColorScheme) -> WidgetStyle {
        let primary = colorScheme == .dark
            ? WidgetColor(r: 1, g: 1, b: 1)
            : WidgetColor(r: 0, g: 0, b: 0)
        let secondary = colorScheme == .dark
            ? WidgetColor(r: 0.78, g: 0.78, b: 0.78)
            : WidgetColor(r: 0.24, g: 0.24, b: 0.24)
        var tinted = self
        tinted.background = .transparent
        tinted.primaryText = primary
        tinted.secondaryText = secondary
        // Accented/vibrant rendering collapses status hues to one system tint.
        // Say so in the palette rather than leaving three colours that the
        // renderer will silently flatten into each other.
        tinted.accent = primary
        tinted.statusOk = primary
        tinted.statusWarn = primary
        tinted.statusBad = primary
        return tinted
    }
}

/// Hands every descendant the palette appropriate for the CURRENT rendering
/// mode. Applied INSIDE the widget's content (instead of the plain
/// `.widgetStyle(style)` the widgets used before), so WidgetKit can switch
/// modes without a timeline reload and every colour read still goes through
/// one environment key.
private struct WidgetRenderingAdaptation: ViewModifier {
    @Environment(\.widgetRenderingMode) private var renderingMode
    @Environment(\.colorScheme) private var colorScheme
    let style: WidgetStyle

    func body(content: Content) -> some View {
        content.widgetStyle(renderingMode == .fullColor
                            ? style
                            : style.systemTinted(for: colorScheme))
    }
}

extension View {
    /// Adapt a widget root to WidgetKit's rendering mode. The root still paints
    /// `WidgetSurfaceBackground(style:)` inside `containerBackground`, so the
    /// full-colour path is unchanged and the system-tinted path drops the fill.
    func widgetRenderingAdaptation(style: WidgetStyle) -> some View {
        modifier(WidgetRenderingAdaptation(style: style))
    }
}
