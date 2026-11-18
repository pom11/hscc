import SwiftUI
#if canImport(UIKit)
import UIKit
#elseif canImport(AppKit)
import AppKit
#endif

// ---------------------------------------------------------------------------
// Appearance editor (card t_444519ae, part 1)
// ---------------------------------------------------------------------------
//
// Settings -> Appearance: one GLOBAL style plus a row per widget / Live
// Activity surface. A surface either follows the global style or carries its
// own override; "Use global" stores absence (nil), which is what makes the
// inheritance real rather than a copied snapshot. Every editor shows a live
// preview in small / medium / lock-screen shapes, drawn through the same
// `WidgetStyle` bridge part 2's widget bodies use.

struct WidgetAppearanceView: View {
    @EnvironmentObject private var store: WidgetSettingsStore

#if os(macOS)
    /// Which editor the macOS master-detail pane is showing. `WidgetSurfaceKind`
    /// is Hashable (String raw value), so the selection survives a store update.
    private enum Selection: Hashable {
        case global
        case surface(WidgetSurfaceKind)
    }

    @State private var selection: Selection = .global
#endif

    var body: some View {
#if os(macOS)
        // The iOS screen is a pushed Form; a Settings window has no navigation
        // stack, so the SAME editors are laid out as a sidebar + detail pane.
        // Everything below the shell — the global binding, the per-surface
        // "Use global" toggle, the presets, controls and live preview — is the
        // iOS editor code compiled into this target unchanged.
        HSplitView {
            List(selection: $selection) {
                Section("Global Style") {
                    HStack(spacing: Theme.Spacing.md.rawValue) {
                        Image(systemName: "paintpalette")
                            .foregroundColor(Theme.Semantic.accent)
                            .frame(width: 24)
                        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                            Text("Global style")
                            Text(WidgetAppearanceView.summary(store.globalStyle))
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                    .tag(Selection.global)
                }
                Section("Per-Widget Style") {
                    ForEach(WidgetSurfaceKind.allCases, id: \.self) { kind in
                        HStack(spacing: Theme.Spacing.md.rawValue) {
                            Image(systemName: kind.displayIcon)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .frame(width: 24)
                            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                                Text(kind.displayName)
                                Text(styleLine(kind))
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
                        .tag(Selection.surface(kind))
                    }
                }
            }
            .frame(minWidth: 260, maxWidth: 320, maxHeight: .infinity)

            detail
                .frame(minWidth: 440, maxWidth: .infinity, maxHeight: .infinity)
        }
#else
        Form {
            Section {
                NavigationLink {
                    WidgetStyleEditor(
                        title: "Global Style",
                        style: Binding(
                            get: { store.globalStyle },
                            set: { store.setGlobalStyle($0) }
                        ))
                } label: {
                    HStack(spacing: Theme.Spacing.md.rawValue) {
                        Image(systemName: "paintpalette")
                            .foregroundColor(Theme.Semantic.accent)
                            .frame(width: 24)
                        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                            Text("Global style")
                                .foregroundColor(Theme.Semantic.onSurface)
                            Text(WidgetAppearanceView.summary(store.globalStyle))
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                }
            } header: {
                Text("Global Style")
            } footer: {
                Text("Every widget and Live Activity follows this unless it has its own style below. Colours accept opacity, so a transparent background is a real choice.")
            }

            Section {
                ForEach(WidgetSurfaceKind.allCases, id: \.self) { kind in
                    NavigationLink {
                        WidgetSurfaceStyleEditor(kind: kind, store: store)
                    } label: {
                        HStack(spacing: Theme.Spacing.md.rawValue) {
                            Image(systemName: kind.displayIcon)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                .frame(width: 24)
                            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                                Text(kind.displayName)
                                    .foregroundColor(Theme.Semantic.onSurface)
                                Text(styleLine(kind))
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
                    }
                }
            } header: {
                Text("Per-Widget Style")
            } footer: {
                Text("A surface with no style of its own follows the global style and keeps following it when you change the global one. Turn on Use global in a surface to drop its own style.")
            }
        }
        .navigationTitle("Appearance")
#endif
    }

#if os(macOS)
    /// The same two editors the iOS screen pushes: the global binding writes the
    /// fleet-wide style, the surface editor owns its "Use global" toggle.
    @ViewBuilder
    private var detail: some View {
        switch selection {
        case .global:
            WidgetStyleEditor(
                title: "Global Style",
                style: Binding(
                    get: { store.globalStyle },
                    set: { store.setGlobalStyle($0) }
                ))
        case .surface(let kind):
            WidgetSurfaceStyleEditor(kind: kind, store: store)
        }
    }
#endif

    private func styleLine(_ kind: WidgetSurfaceKind) -> String {
        if store.style(for: kind) == nil {
            return "Use global — \(WidgetAppearanceView.summary(store.globalStyle))"
        }
        return "Custom — \(WidgetAppearanceView.summary(store.resolvedStyle(for: kind)))"
    }

    /// One-line summary for a row: the choices that are visible at a glance.
    static func summary(_ style: WidgetStyle) -> String {
        let scale = Int((style.textScale * 100).rounded())
        return "\(style.fontDesign.displayName) · \(style.density.displayName) · \(scale)%"
    }
}

/// One surface's appearance: the "Use global" switch first, then either the
/// inherited style (preview only) or its own controls with a live preview.
struct WidgetSurfaceStyleEditor: View {
    let kind: WidgetSurfaceKind
    @ObservedObject var store: WidgetSettingsStore
    @State private var useGlobal: Bool

    init(kind: WidgetSurfaceKind, store: WidgetSettingsStore) {
        self.kind = kind
        _store = ObservedObject(wrappedValue: store)
        _useGlobal = State(initialValue: store.style(for: kind) == nil)
    }

    var body: some View {
        Form {
            Section {
                Toggle("Use global style", isOn: Binding(
                    get: { useGlobal },
                    set: { followsGlobal in
                        useGlobal = followsGlobal
                        if followsGlobal {
                            store.useGlobalStyle(for: kind)
                        } else {
                            // Materialise the style the surface is showing
                            // right now as its own, so turning the switch off
                            // never changes what the operator sees.
                            store.setStyle(store.resolvedStyle(for: kind), for: kind)
                        }
                    }
                ))
            } footer: {
                Text(useGlobal
                     ? "This surface follows the global style. Turn the switch off to give it its own colours, fonts and layout."
                     : "This surface has its own style. Turn the switch on to drop it and follow the global style again.")
            }

            Section {
                WidgetStylePreview(style: store.resolvedStyle(for: kind))
            } header: {
                Text("Live Preview")
            }

            if !useGlobal {
                WidgetStyleControls(style: Binding(
                    get: { store.style(for: kind) ?? store.globalStyle },
                    set: { store.setStyle($0, for: kind) }
                ))
            }
        }
        .navigationTitle(kind.displayName)
    }
}

/// The global style editor: the same controls and preview, writing the GLOBAL
/// entry of the shared blob.
struct WidgetStyleEditor: View {
    let title: String
    @Binding var style: WidgetStyle

    var body: some View {
        Form {
            Section {
                WidgetStylePreview(style: style)
            } header: {
                Text("Live Preview")
            }
            WidgetStyleControls(style: $style)
        }
        .navigationTitle(title)
    }
}

/// The controls shared by the global editor and every per-surface editor.
struct WidgetStyleControls: View {
    @Binding var style: WidgetStyle

    var body: some View {
        Section {
            ForEach(WidgetStyle.presets) { preset in
                Button {
                    style = preset.style
                } label: {
                    HStack(spacing: Theme.Spacing.md.rawValue) {
                        presetSwatch(preset.style)
                        Text(preset.name)
                            .foregroundColor(Theme.Semantic.onSurface)
                        Spacer(minLength: 0)
                        if style == preset.style {
                            Image(systemName: "checkmark")
                                .foregroundColor(Theme.Semantic.accent)
                        }
                    }
                }
                .buttonStyle(.plain)
            }
        } header: {
            Text("Presets")
        } footer: {
            Text("A preset fills every control below; tweak any of them afterwards. Default is the app's black and orange Theme.")
        }

        Section {
            colorRow("Background", keyPath: \.background)
            colorRow("Primary text", keyPath: \.primaryText)
            colorRow("Secondary text", keyPath: \.secondaryText)
            colorRow("Accent", keyPath: \.accent)
            colorRow("OK / serving", keyPath: \.statusOk)
            colorRow("Warn / waking", keyPath: \.statusWarn)
            colorRow("Bad / down", keyPath: \.statusBad)
        } header: {
            Text("Colours")
        } footer: {
            Text("Background colour includes its own opacity: drop alpha to zero for a transparent widget.")
        }

        Section {
            Picker("Font", selection: $style.fontDesign) {
                ForEach(WidgetStyle.FontDesign.allCases, id: \.self) { design in
                    Text(design.displayName).tag(design)
                }
            }
            Picker("Weight", selection: $style.fontWeight) {
                ForEach(WidgetStyle.FontWeight.allCases, id: \.self) { weight in
                    Text(weight.displayName).tag(weight)
                }
            }
            HStack(spacing: Theme.Spacing.md.rawValue) {
                Text("Text size")
                Slider(value: $style.textScale, in: 0.8...1.4, step: 0.05)
                Text("\(Int((style.textScale * 100).rounded()))%")
                    .font(.caption.monospacedDigit())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .frame(width: 44, alignment: .trailing)
            }
        } header: {
            Text("Typography")
        } footer: {
            Text("The size slider scales every line in the widget from 80% to 140%.")
        }

        Section {
            Picker("Density", selection: $style.density) {
                ForEach(WidgetStyle.Density.allCases, id: \.self) { density in
                    Text(density.displayName).tag(density)
                }
            }
            .pickerStyle(.segmented)
            Toggle("Show header", isOn: $style.showHeader)
            Toggle("Show icons", isOn: $style.showIcons)
        } header: {
            Text("Layout")
        } footer: {
            Text("Compact trims padding and corner radius. Turning the header or icons off hides them everywhere on the surface. Lock Screen accessory families are system-tinted: only font design, weight, size and the header/icon switches apply there — their colours stay the system's.")
        }

        Section {
            Button("Reset to default", role: .destructive) {
                style = WidgetStyle()
            }
        }
    }

    private func colorRow(_ label: String,
                          keyPath: WritableKeyPath<WidgetStyle, WidgetColor>) -> some View {
        ColorPicker(label, selection: Binding(
            get: { style[keyPath: keyPath].color },
            set: { style[keyPath: keyPath] = WidgetStyleControls.widgetColor(from: $0) }
        ), supportsOpacity: true)
    }

    private func presetSwatch(_ preset: WidgetStyle) -> some View {
        HStack(spacing: 3) {
            Circle().fill(preset.accent.color)
            Circle().fill(preset.statusOk.color)
            Circle().fill(preset.statusWarn.color)
            Circle().fill(preset.statusBad.color)
        }
        .frame(width: 46, height: 14)
        .padding(.horizontal, 4)
        .padding(.vertical, 3)
        .background(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .fill(preset.background.color)
                .overlay(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .stroke(Theme.Semantic.onSurfaceMuted.opacity(0.4), lineWidth: 0.5)
                )
        )
    }

    /// SwiftUI ColorPicker works in `Color`; the store is hex. This is the one
    /// conversion point, so every colour row round-trips identically. UIKit on
    /// iOS, AppKit on macOS (the same editor is compiled into both apps).
    private static func widgetColor(from color: Color) -> WidgetColor {
#if canImport(UIKit)
        let uiColor = UIColor(color)
        var red: CGFloat = 0
        var green: CGFloat = 0
        var blue: CGFloat = 0
        var alpha: CGFloat = 0
        if uiColor.getRed(&red, green: &green, blue: &blue, alpha: &alpha) {
            return WidgetColor(r: Double(red), g: Double(green),
                               b: Double(blue), a: Double(alpha))
        }
#else
        if let srgb = NSColor(color).usingColorSpace(.sRGB) {
            return WidgetColor(r: Double(srgb.redComponent),
                               g: Double(srgb.greenComponent),
                               b: Double(srgb.blueComponent),
                               a: Double(srgb.alphaComponent))
        }
#endif
        return .ink
    }
}

/// Small / medium / lock-screen samples drawn from the style being edited.
/// Part 2's real widget bodies apply the same `WidgetStyle` bridge, so this is
/// a faithful preview rather than a separate palette.
struct WidgetStylePreview: View {
    let style: WidgetStyle

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: Theme.Spacing.md.rawValue) {
                small
                medium
                lockScreen
            }
            .padding(.vertical, Theme.Spacing.xs.rawValue)
        }
    }

    private var small: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            header
            Text("Serving")
                .font(style.font(size: 20, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            statusRow("4 nodes up", color: style.okColor)
            statusRow("1 waking", color: style.warnColor)
            Spacer(minLength: 0)
            Text("radio · 6 cards")
                .font(style.font(size: 10))
                .foregroundColor(style.secondaryTextColor)
        }
        .frame(width: 145, height: 145, alignment: .topLeading)
        .widgetSurfaceChrome(style)
    }

    private var medium: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            header
            HStack(alignment: .top, spacing: Theme.Spacing.lg.rawValue) {
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text("FLEET")
                        .font(style.font(size: 9, weight: .semibold))
                        .foregroundColor(style.secondaryTextColor)
                    Text("Serving")
                        .font(style.font(size: 19, weight: .semibold))
                        .foregroundColor(style.primaryTextColor)
                    statusRow("4 nodes up", color: style.okColor)
                }
                VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                    Text("BOARD")
                        .font(style.font(size: 9, weight: .semibold))
                        .foregroundColor(style.secondaryTextColor)
                    Text("6 running")
                        .font(style.font(size: 15, weight: .semibold))
                        .foregroundColor(style.primaryTextColor)
                    statusRow("1 blocked", color: style.warnColor)
                }
            }
            Spacer(minLength: 0)
            Text("updated just now")
                .font(style.font(size: 9))
                .foregroundColor(style.secondaryTextColor)
        }
        .frame(width: 295, height: 145, alignment: .topLeading)
        .widgetSurfaceChrome(style)
    }

    private var lockScreen: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            if style.showHeader {
                HStack(spacing: Theme.Spacing.xs.rawValue) {
                    if style.showIcons {
                        Image(systemName: "heart.text.square")
                            .font(style.font(size: 10, weight: .semibold))
                    }
                    Text("HSCC")
                        .font(style.font(size: 10, weight: .semibold))
                    Spacer(minLength: 0)
                    Text("now")
                        .font(style.font(size: 9))
                        .foregroundColor(style.secondaryTextColor)
                }
                .foregroundColor(style.accentColor)
            }
            Text("Cluster healthy")
                .font(style.font(size: 14, weight: .semibold))
                .foregroundColor(style.primaryTextColor)
            statusRow("4 nodes up · 2 running", color: style.okColor)
            statusRow("1 blocked", color: style.badColor)
        }
        .frame(width: 295, height: 105, alignment: .topLeading)
        .widgetSurfaceChrome(style)
    }

    @ViewBuilder
    private var header: some View {
        if style.showHeader {
            HStack(spacing: Theme.Spacing.xs.rawValue) {
                if style.showIcons {
                    Image(systemName: "square.grid.2x2")
                        .font(style.font(size: 10, weight: .semibold))
                }
                Text("HSCC")
                    .font(style.font(size: 10, weight: .semibold))
                Spacer(minLength: 0)
                Circle()
                    .fill(style.accentColor)
                    .frame(width: 6, height: 6)
            }
            .foregroundColor(style.primaryTextColor)
        }
    }

    private func statusRow(_ label: String, color: Color) -> some View {
        HStack(spacing: Theme.Spacing.xs.rawValue) {
            if style.showIcons {
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
            }
            Text(label)
                .font(style.font(size: 11))
                .foregroundColor(style.primaryTextColor)
        }
    }
}
