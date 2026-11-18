import SwiftUI

/// HSCC design system — tokens, defined ONCE here and used everywhere.
///
/// Operator request 2026-11-18: the app is simple black with orange accents,
/// matching the new block-H app icon. Surfaces are pure black (#000000),
/// raised/elevated surfaces are near-black greys, text is white + muted grey,
/// and the accent is the icon's flat orange (#FF5722). Status keeps its own
/// semantic colours (ok green, warn amber, bad red), retuned to stay readable
/// on black — status must never collapse into the orange accent.
///
/// The app is DARK ONLY. `HSCCApp` pins `.preferredColorScheme(.dark)` and every
/// Info.plist sets `UIUserInterfaceStyle=Dark`; the palette has no light
/// variants to fall back to, so a trait change can never repaint a light
/// screen. Widgets and Live Activities compile this same file.
///
/// Every color at a call site must come from the semantic roles below (or the
/// raw `Palette` slots), never from an ad-hoc hex or a hardcoded `.red` /
/// `.green` / `.orange`.
enum Theme {

    // MARK: - Palette (the base tokens, in hex)

    private static func hex(_ value: UInt32) -> Color {
        Color(red: Double((value >> 16) & 0xFF) / 255.0,
              green: Double((value >> 8) & 0xFF) / 255.0,
              blue: Double(value & 0xFF) / 255.0)
    }

    enum Palette {
        /// #000000 — base surface, pure black.
        static let graphite = Theme.hex(0x000000)
        /// #111111 — raised surfaces / cards.
        static let raised = Theme.hex(0x111111)
        /// #1C1C1E — one step higher than `raised` (badges, chips, banners).
        static let elevated = Theme.hex(0x1C1C1E)
        /// #FF5722 — the operator's icon orange: primary action / selection.
        static let accent = Theme.hex(0xFF5722)
        /// #000000 — ink on the orange fill. Black on #FF5722 is 6.64:1 (AA);
        /// white on it is only 3.16:1, so black is the readable choice.
        static let accentInk = Theme.hex(0x000000)
        /// #FFFFFF — primary text (21.0:1 on black).
        static let white = Theme.hex(0xFFFFFF)
        /// #9A9A9A — secondary text (7.46:1 on black).
        static let mist = Theme.hex(0x9A9A9A)
        /// #4ADE80 — healthy, serving (12.05:1 on black).
        static let signal = Theme.hex(0x4ADE80)
        /// #FFC857 — busy, waking, warning (13.65:1 on black). Deliberately
        /// distinct from the #FF5722 accent so "warning" never reads as
        /// "primary action".
        static let thermal = Theme.hex(0xFFC857)
        /// #FF6B6B — down, fault (7.57:1 on black).
        static let halt = Theme.hex(0xFF6B6B)
        /// #2A2A2A — hairline separators (subtle on black).
        static let hairline = Theme.hex(0x2A2A2A)
    }

    // MARK: - Semantic roles (the ONLY thing views should reference)
    //
    // Plain constants: there is no light palette, so nothing has to resolve per
    // trait collection. Views call `Theme.Semantic.surface` etc.; they never
    // hardcode a palette hex.

    enum Semantic {
        /// Primary surface background (the app's canvas).
        static var surface: Color { Palette.graphite }
        /// Raised surfaces / cards sitting on `surface`.
        static var surfaceRaised: Color { Palette.raised }
        /// One step higher than `surfaceRaised` (badges, chips).
        static var surfaceElevated: Color { Palette.elevated }
        /// Primary text on a surface.
        static var onSurface: Color { Palette.white }
        /// Secondary / muted text.
        static var onSurfaceMuted: Color { Palette.mist }
        /// Healthy / serving / ok.
        static var ok: Color { Palette.signal }
        /// Busy / waking / warning.
        static var warn: Color { Palette.thermal }
        /// Down / fault.
        static var bad: Color { Palette.halt }
        /// A neutral signal for an unknown / indeterminate state.
        static var neutral: Color { Palette.mist }

        /// PRIMARY ACTION + selection + the live/connection dot. A distinct
        /// ROLE from `ok`: status-as-green and action-as-orange are tuned
        /// independently, and the orange never means "healthy".
        static var accent: Color { Palette.accent }

        /// Text/glyph drawn ON an accent-colored fill. Black on #FF5722 is
        /// 6.64:1 (passes AA); white on the same fill is 3.16:1 and fails.
        static var onAccent: Color { Palette.accentInk }

        /// The tint wash behind banners/chips. One recipe, one place.
        static func tintOverlay(_ base: Color) -> Color {
            base.opacity(0.16)
        }
    }

    // MARK: - Spacing scale (the ONE spacing scale)
    //
    // Every gap in the app comes from this scale. There is NO other scale.
    // Before, views used ~21 distinct raw values (2…60). Collapse onto these
    // named steps so spacing is predictable and sweep-able:
    //
    //   .xxs  = 2   — inline glyph gaps / kernel of tight stacks
    //   .xs   = 4   — tight inner text stacks (title over caption)
    //   .sm   = 8   — small HStack gaps (icon — text)
    //   .md   = 12  — the default gap between related elements
    //   .lg   = 16  — between sections / cards
    //   .xl   = 24  — big separations / page padding
    //   .page = 60  — top offset for the centered "connect" state
    enum Spacing: CGFloat {
        case xxs = 2
        case xs = 4
        case sm = 8
        case md = 12
        case lg = 16
        case xl = 24
        case page = 60
    }

    // MARK: - Corner radius (the ONE corner scale)
    enum Corner: CGFloat {
        /// Cards / raised surfaces (12 pt, continuous).
        case card = 12
        /// Badges / chips / small containers (10 pt, continuous).
        case badge = 10
        /// Home dock + floating bars (IOS_REDESIGN §2.3, 24 pt, continuous).
        case dock = 24
    }

    // MARK: Elevation (IOS_REDESIGN 2026-10-14 §2.3)
    //
    // Spec'd as `hairline`, `low`, `mid`. Before this the app had NO elevation
    // token at all — raised surfaces in dark mode were readable only by luck,
    // and the first view that needed a shadow would have hardcoded one (the
    // check_theme gate rejects raw colours outside this file). Apply with
    // `.hsElevated(.low)` / `.hsElevated(.mid)`.
    enum Elevation {
        /// Separators between rows: a hairline stroke, no shadow.
        static var hairline: Color { Palette.hairline }

        /// Cards and small raised surfaces (spec `low` = 0/1/3). On black the
        /// shadow is mostly a depth cue; the `surfaceRaised` fill does the
        /// visible separation.
        static var lowShadow: Color { Palette.graphite.opacity(0.60) }
        static let lowRadius: CGFloat = 3
        static let lowY: CGFloat = 1

        /// Floating bars such as the home dock (spec `mid` = 0/6/16).
        static var midShadow: Color { Palette.graphite.opacity(0.55) }
        static let midRadius: CGFloat = 16
        static let midY: CGFloat = 6
    }
}

extension Theme {
    /// Colour ROLE for a kanban card status — the tone a leading `HSStatusDot`
    /// or chip carries.
    ///
    /// Spec §2 "Duplicated renderers" counted this switch twice (SearchView,
    /// ProjectsView); by the time this landed it had been copied a third time
    /// into CardsRootView. Four screens, one meaning: when the daemon adds a
    /// status, the dot colour for it now has one place to be added.
    ///
    /// `StreamingChatView.statusColor` is deliberately NOT folded in. It maps
    /// the same words to different roles on purpose — a blocked card is `bad`
    /// there (it is the reason the chat is stuck) and `warn` here (it is one
    /// status among many on a board), and `ready` warns there but is neutral
    /// here. Merging them would quietly change one screen's meaning to match
    /// the other's.
    static func cardStatusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return Semantic.ok
        case "review", "blocked": return Semantic.warn
        case "done", "merged", "closed": return Semantic.ok
        case "failed", "failing": return Semantic.bad
        default: return Semantic.neutral
        }
    }
}

// MARK: - Typography rules

extension Font {
    /// SYSTEM fonts only (no bundled faces).
    ///
    /// Semantic rule: anything the MACHINE produced is monospaced (SF Mono) —
    /// node IPs, task ids, ports, model names, timestamps, counts, uptimes.
    /// Anything a HUMAN wrote is not (SF Pro) — titles, prose, chat messages.
    /// This is the app's typographic personality: it makes telemetry scannable.
    ///
    /// Use `Theme.mono(...)` for machine-produced values; use the standard
    /// system fonts (`.font(.body)`, `.font(.caption)`, etc.) for human copy.
    static func hsccMono(_ size: CGFloat, weight: Font.Weight? = nil) -> Font {
        var f = Font.system(size: size, design: .monospaced)
        if let weight { f = f.weight(weight) }
        return f
    }

    /// Dynamic-Type-scaling monospaced font (IOS_REDESIGN 2026-10-14 §2.2).
    ///
    /// `hsccMono(_:)` takes a FIXED point size, so machine-produced values
    /// (tool args, JSON, ids, tails) could not scale for a large-text user —
    /// and several call sites clamped that unscaled text with `lineLimit(1)`.
    /// Prefer this where the text must stay readable at accessibility sizes;
    /// keep the fixed-size form only where the glyph grid must not move (e.g.
    /// aligned hex dumps).
    static func hsccMono(_ style: Font.TextStyle, weight: Font.Weight? = nil) -> Font {
        var f = Font.system(style, design: .monospaced)
        if let weight { f = f.weight(weight) }
        return f
    }
}

// MARK: - Standard full-pane states (loading / error / empty)
//
// The app's ONE loading/empty/error component set. Every view that renders a
// full-pane async state uses these instead of inlining `ProgressView(...)` and
// `ContentUnavailableView { ... }` variations — so the chrome, typography and
// retry affordance are identical everywhere and a future design change lands
// in ONE place.

/// Centered full-pane ProgressView. Use for a pane with no partial content.
/// Prefer the app's `hsccMono`-free path; the label is human copy.
struct HSLoading: View {
    let label: String?
    init(_ label: String? = nil) { self.label = label }

    var body: some View {
        VStack(spacing: Theme.Spacing.sm.rawValue) {
            ProgressView()
            if let label {
                Text(label)
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Centered full-pane error state with a retry action. Mirrors the system
/// `ContentUnavailableView` look but standardizes title/description/action
/// across the app, and always uses the semantic `bad` role.
struct HSError: View {
    let title: String
    let message: String
    let retry: () -> Void

    init(_ title: String, message: String, retry: @escaping () -> Void) {
        self.title = title
        self.message = message
        self.retry = retry
    }

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: "exclamationmark.triangle")
                .foregroundColor(Theme.Semantic.bad)
        } description: {
            Text(message)
        } actions: {
            Button("Try again", action: retry)
        }
    }
}

/// Centered full-pane empty state (no content yet, nothing failed).
struct HSEmpty: View {
    let title: String
    let message: String
    let systemImage: String

    init(_ title: String, message: String, systemImage: String = "tray") {
        self.title = title
        self.message = message
        self.systemImage = systemImage
    }

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: systemImage)
                .foregroundColor(Theme.Semantic.neutral)
        } description: {
            Text(message)
        }
    }
}

// MARK: - Shared row styles (the ONE row style)
//
// List rows across the app follow ONE recipe: a leading status (dot or icon),
// a title, and a muted caption line. Use these instead of re-inventing an
// HStack(:firstTextBaseline) per view.

/// A compact status dot (e.g. the 10pt circle ahead of a card title).
///
/// Decorative: it is a colour-only glyph. Everywhere it is used the state it
/// hints at is ALSO spelled out as text in the same row (e.g. the meta line),
/// so it carries no meaning on its own. It is hidden from VoiceOver so it
/// never surfaces as an unlabeled element that polls the reading order — the
/// coloured dot is pure embellishment, the text conveys the state.
struct HSStatusDot: View {
    let color: Color
    init(_ color: Color) { self.color = color }

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 10, height: 10)
            .accessibilityHidden(true)
    }
}

/// The standard two-line list row: leading icon + bold-ish title over a muted
/// caption line, aligned to the first text baseline. This is the app's shared
/// "status row" — use it for cards, blocked/stale items, streams, checks.
struct HSStatusRow: View {
    let title: String
    let caption: String
    let icon: String?          // leading SF Symbol (nil → no leading icon)
    let iconColor: Color
    let statusIcon: String?    // trailing status glyph (nil → hidden)

    init(_ title: String,
         caption: String = "",
         icon: String? = nil,
         iconColor: Color = Theme.Semantic.neutral,
         statusIcon: String? = nil) {
        self.title = title
        self.caption = caption
        self.icon = icon
        self.iconColor = iconColor
        self.statusIcon = statusIcon
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
            if let icon {
                Image(systemName: icon)
                    .foregroundColor(iconColor)
                    .frame(width: 22)
            }
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(title)
                    .foregroundColor(Theme.Semantic.onSurface)
                if !caption.isEmpty {
                    Text(caption)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            Spacer(minLength: 0)
            if let statusIcon {
                Image(systemName: statusIcon)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

/// A small colored label/chip used to flag a row's state (e.g. the amber
/// "hand.raised" block kind, the red error list, a status name).
struct HSStatusChip: View {
    let text: String
    let systemImage: String?
    let color: Color

    init(_ text: String, systemImage: String? = nil, color: Color = Theme.Semantic.neutral) {
        self.text = text
        self.systemImage = systemImage
        self.color = color
    }

    var body: some View {
        HStack(spacing: Theme.Spacing.xxs.rawValue) {
            if let systemImage {
                Image(systemName: systemImage)
            }
            Text(text)
        }
        .font(.caption)
        .foregroundColor(color)
    }
}

/// The shared "not configured" gate every tab shows before the operator has
/// entered host/port/token. One component instead of the old four near-copies
/// (Projects, Cluster, Templates, BoardHygiene), each with inconsistent
/// colors/wording.
struct HSConnectGate: View {
    let systemImage: String
    let verb: String            // "to see your projects" / "to manage boards"

    init(systemImage: String = "folder", verb: String) {
        self.systemImage = systemImage
        self.verb = verb
    }

    var body: some View {
        VStack(spacing: Theme.Spacing.md.rawValue) {
            Image(systemName: systemImage)
                .font(.system(size: 44))
                .foregroundColor(Theme.Semantic.neutral)
            Text("Connect to your cluster")
                .font(.headline)
                .foregroundColor(Theme.Semantic.onSurface)
            Text("Set the host, port, and token in Settings \(verb).")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, Theme.Spacing.page.rawValue)
        .padding(.horizontal)
    }
}

// HSSectionCard (the titled rounded `surfaceRaised` box) was DELETED here on
// t_f64706b4. Phase 2's list-recipe conversions (spec §5) retired its last call
// sites — Fleet `45645201`, Autodown `79be2dd5`, Serving Control `644286f6`,
// Cron, Ops, Approvals/BoardHygiene and Nodes & serving — and no screen builds a
// ScrollView of boxes any more, so the component had zero users left. It is gone
// deliberately: the recipe dialect (`List` + `.listStyle(.plain)` + `Section` +
// `HSEmpty`/`HSError`) is the only panel style in the app. Do not resurrect it;
// the prose mentions of it in the views' doc comments and
// docs/design/PHASE2_PROGRESS_t_25aed701.md are the record of what it used to
// be, not a TODO.

/// The shared red inline error label under a section card.
///
/// When a `retry` closure is provided (most `.failed` cases are retryable), a
/// "Try again" button is rendered beneath the message so the error state has an
/// unmistakable next action and not just a passive text line. Backward
/// compatible: callers that omit `retry` get exactly the old bare label.
struct HSErrorLabel: View {
    let message: String
    var retry: (() -> Void)?

    init(message: String, retry: (() -> Void)? = nil) {
        self.message = message
        self.retry = retry
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            Label(message, systemImage: "exclamationmark.triangle.fill")
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.bad)
            if let retry {
                Button("Try again", action: retry)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.bad)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The shared muted inline empty label ("nothing here").
struct HSEmptyLabel: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "tray")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
    }
}

/// The shared secondary-metadata line under a row title (board · assignee ·
/// age…). Nils are dropped, so separators only appear between present parts.
struct HSMetaLine: View {
    let parts: [String]

    init(_ parts: [String?]) { self.parts = parts.compactMap { $0 } }
    init(_ parts: String...) { self.parts = parts }
    init<S: Sequence>(_ parts: S) where S.Element == String { self.parts = Array(parts) }

    var body: some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            ForEach(Array(parts.enumerated()), id: \.offset) { index, part in
                if index > 0 { Rectangle().fill(Theme.Semantic.onSurfaceMuted.opacity(0.4))
                        .frame(width: 1, height: 8) }
                Text(part)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }
}

// MARK: - Banners (the ONE in-content banner recipe)

/// The app's one in-content banner: a leading glyph (or spinner), a bold title
/// line, an optional muted reason line, and an optional trailing icon action,
/// on the elevated surface fill.
///
/// Spec §1.3 counted this recipe written twice — `StaleBanner` here and the
/// Autodown screen's private `wakingBanner` (that screen retired with the
/// console, t_0f3f70e6) — same shape, different meaning. They
/// could NOT simply share `StaleBanner`'s copy: it prints "Offline — …", and a
/// fleet that is *coming up* is not a fleet that is *unreachable*. Printing
/// "Offline — waking the fleet…" is precisely the word/state disagreement spec
/// §9 bans, so the shapes collapse and the copy stays with the caller. Each
/// state still says its own truth; the geometry can no longer drift.
struct HSBanner: View {
    /// The glyph/tone role. `warn` = something needs attention (offline data,
    /// a blocked pipeline); `neutral` = an in-progress state that is not a
    /// fault (a wake in flight).
    enum Tone {
        case warn, neutral

        var glyphColor: Color {
            switch self {
            case .warn: return Theme.Semantic.warn
            case .neutral: return Theme.Semantic.onSurfaceMuted
            }
        }
        var titleColor: Color {
            switch self {
            case .warn: return Theme.Semantic.warn
            case .neutral: return Theme.Semantic.onSurface
            }
        }
    }

    /// What leads the banner: a status glyph, or a spinner when the state is
    /// literally "in progress" (the wake poll) — a spinner is information, not
    /// decoration, so it is part of the recipe rather than a call-site hack.
    enum Leading {
        case symbol(String)
        case spinner
    }

    let leading: Leading
    var tone: Tone = .neutral
    let title: String
    var reason: String = ""
    /// Trailing icon-button action (e.g. retry). Ignored unless `action` is set.
    var actionSystemImage: String = "arrow.clockwise"
    var actionLabel: String = "Retry loading"
    var action: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
            switch leading {
            case .symbol(let name):
                Image(systemName: name)
                    .foregroundColor(tone.glyphColor)
            case .spinner:
                ProgressView()
            }
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(tone.titleColor)
                if !reason.isEmpty {
                    Text(reason)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            Spacer(minLength: 0)
            if let action {
                Button(action: action) {
                    Image(systemName: actionSystemImage)
                }
                .buttonStyle(.borderless)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .accessibilityLabel(actionLabel)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Spacing.md.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }
}

// MARK: - Widget appearance bridge (card t_444519ae)
//
// The stored model is Foundation-only (`WidgetStyle` in SharedStore.swift) so
// the App Group blob is UI-free. This is the ONE SwiftUI bridge part 2's
// widget bodies and the Settings preview share: resolve a style, hand it down
// through the environment, then read colours/fonts from it. No widget body
// changes in part 1 beyond the preview this file powers.

extension WidgetColor {
    /// The SwiftUI colour for this stored RGBA value.
    var color: Color {
        Color(red: red, green: green, blue: blue, opacity: alpha)
    }
}

extension WidgetStyle {
    var backgroundColor: Color { background.color }
    var primaryTextColor: Color { primaryText.color }
    var secondaryTextColor: Color { secondaryText.color }
    var accentColor: Color { accent.color }
    var okColor: Color { statusOk.color }
    var warnColor: Color { statusWarn.color }
    var badColor: Color { statusBad.color }

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

    /// Apply the operator's text-size scale to a point size. One call site per
    /// text role, so every surface scales by the same amount.
    func scaled(_ size: CGFloat) -> CGFloat {
        max(1, size * CGFloat(textScale))
    }

    /// A font in the operator's chosen design/weight, scaled by `textScale`.
    func font(size: CGFloat, weight: Font.Weight? = nil) -> Font {
        Font.system(size: scaled(size),
                    weight: weight ?? swiftUIFontWeight,
                    design: swiftUIFontDesign)
    }

    /// Density as real geometry: compact trims the padding and the corner.
    var contentPadding: CGFloat { density == .compact ? 8 : 12 }
    var cornerRadius: CGFloat { density == .compact ? 10 : 14 }

    /// A raised surface tint derived from the operator's own text colour, so a
    /// custom palette never leaves a hard-coded grey card behind.
    var raisedColor: Color { primaryTextColor.opacity(0.07) }
    /// Hairline separators, derived the same way.
    var hairlineColor: Color { secondaryTextColor.opacity(0.25) }

    /// Density applied to a spacing token. Compact trims every gap by a
    /// quarter; regular leaves the design system untouched.
    func spacing(_ value: CGFloat) -> CGFloat {
        density == .compact ? max(1, value * 0.75) : value
    }

    /// Density applied to a corner token. Compact badges tighten, never invert.
    func corner(_ value: CGFloat) -> CGFloat {
        density == .compact ? max(4, value * 0.8) : value
    }

    /// A font in the operator's design/weight at a Dynamic-Type text style's
    /// base point size, scaled by `textScale`. The text-style overload keeps
    /// every call site naming a role rather than a magic number.
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

    /// The kanban card-status tone, resolved from the operator's status
    /// colours. One rule, shared by every widget/activity that draws a dot.
    func cardStatusColor(_ status: String) -> Color {
        switch status {
        case "running", "claimed", "in_progress": return okColor
        case "review", "blocked": return warnColor
        case "done", "merged", "closed": return okColor
        case "failed", "failing": return badColor
        default: return secondaryTextColor
        }
    }

    /// The cluster headline state, resolved from the style's status roles.
    func clusterStateColor(_ state: ClusterState) -> Color {
        switch state {
        case .serving: return okColor
        case .waking: return warnColor
        case .down: return badColor
        case .unreachable, .unknown: return secondaryTextColor
        }
    }

    /// A topology node dot, resolved from the style's status roles.
    func nodeStateColor(_ state: TopologyNode.NodeState) -> Color {
        switch state {
        case .up: return okColor
        case .busy, .warn: return warnColor
        case .down: return badColor
        case .unknown: return secondaryTextColor
        }
    }
}

private struct WidgetStyleEnvironmentKey: EnvironmentKey {
    static let defaultValue = WidgetStyle()
}

extension EnvironmentValues {
    /// The resolved appearance for a widget surface. Part 2 applies
    /// `.widgetStyle(WidgetStyle.resolved(for: kind))` at the surface root and
    /// its body reads this instead of hardcoded Theme colours.
    var widgetStyle: WidgetStyle {
        get { self[WidgetStyleEnvironmentKey.self] }
        set { self[WidgetStyleEnvironmentKey.self] = newValue }
    }
}

extension View {
    /// Hand the resolved appearance to a widget / Live Activity body.
    func widgetStyle(_ style: WidgetStyle) -> some View {
        environment(\.widgetStyle, style)
    }

    /// The shared widget chrome: resolved background, corner and padding. Part
    /// 2's bodies apply this once at the root; the preview uses the same
    /// modifier so what the operator sees is what ships.
    func widgetSurfaceChrome(_ style: WidgetStyle) -> some View {
        self
            .padding(style.contentPadding)
            .background(
                RoundedRectangle(cornerRadius: style.cornerRadius, style: .continuous)
                    .fill(style.backgroundColor)
            )
            .foregroundColor(style.primaryTextColor)
    }

    /// The operator's "show icons" switch, applied at every glyph. Kept as a
    /// modifier (not a body branch) so a row's layout does not jump when icons
    /// are hidden — the glyph simply stops being drawn.
    func widgetIcon(_ style: WidgetStyle) -> some View {
        opacity(style.showIcons ? 1 : 0)
    }

    /// The operator's "show header" switch, applied at each family's header.
    func widgetHeader(_ style: WidgetStyle) -> some View {
        opacity(style.showHeader ? 1 : 0)
    }
}

/// A Label whose icon obeys the widget style's "show icons" switch. Used by
/// the families that draw a Label rather than an Image, so one setting owns
/// every glyph on the surface.
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

/// A small reusable banner a view overlays when rendering stale last-known
/// data: the age ("showing state from 6m ago") + the real reason the fetch
/// failed. Tapping refetches. This is `HSBanner`'s offline shape — one
/// façade so the 20+ call sites keep their exact wording while the geometry
/// lives in one place.
struct StaleBanner: View {
    /// The staleness age message, e.g. "showing state from 6m ago".
    let age: String
    /// The real reason the fetch failed, e.g. "Can't reach the cluster…".
    let reason: String
    /// Closure run when the operator taps retry.
    var retry: () -> Void

    var body: some View {
        HSBanner(leading: .symbol("clock.arrow.circlepath"),
                 tone: .warn,
                 title: "Offline — \(age.lowercased())",
                 reason: reason,
                 actionSystemImage: "arrow.clockwise",
                 actionLabel: "Retry loading",
                 action: retry)
    }
}
