import SwiftUI

/// HSCC design system — tokens, defined ONCE here and used everywhere.
///
/// The subject is a 4-node DGX Spark cluster running a dozen agent
/// orchestrators, used by ONE operator, often on the move. The palette is a
/// cool near-black graphite with soft, non-alarm semantic signals (mint =
/// healthy, amber = busy/waking, muted red = fault) — deliberately neither the
/// neon-on-black "hacker terminal" look nor cream + serif.
///
/// Every color at a call site must come from the semantic roles below (or the
/// raw `Palette` slots for the base graphite/slate), never from an ad-hoc hex
/// or a hardcoded `.red` / `.green` / `.orange`. The roles are dynamic colors:
/// they resolve to the dark-mode palette by default and shift for light mode,
/// so views adapt automatically with no per-view color-scheme plumbing.
enum Theme {

    // MARK: - Palette (the spec's six tokens, in hex)

    private static func hex(_ value: UInt32) -> Color {
        Color(red: Double((value >> 16) & 0xFF) / 255.0,
              green: Double((value >> 8) & 0xFF) / 255.0,
              blue: Double(value & 0xFF) / 255.0)
    }

    enum Palette {
        /// #16181D — base, cool near-black (faintly blue).
        static let graphite = Theme.hex(0x16181D)
        /// #232730 — raised surfaces / cards.
        static let slate = Theme.hex(0x232730)
        /// #7AE2B0 — healthy, serving (soft mint, NOT acid green).
        static let signal = Theme.hex(0x7AE2B0)
        /// #F2A65A — busy, waking, warning (amber from GPU thermal readouts).
        static let thermal = Theme.hex(0xF2A65A)
        /// #E56A6A — down, fault (muted, not alarm red).
        static let halt = Theme.hex(0xE56A6A)
        /// #A7B0C0 — secondary text.
        static let mist = Theme.hex(0xA7B0C0)
    }

    // MARK: - Semantic roles (the ONLY thing views should reference)
    //
    // Dynamic colors that resolve to the dark palette by default and shift
    // for light mode via `UIColor { traits in ... }`. Views call
    // `Theme.Semantic.surface` etc.; they never hardcode a palette hex.

    enum Semantic {
        /// Primary surface background (the app's canvas).
        static var surface: Color { dynamic { $0.userInterfaceStyle == .dark ? UIColor(Palette.graphite) : .white } }
        /// Raised surfaces / cards sitting on `surface`.
        static var surfaceRaised: Color { dynamic { _ in UIColor(Palette.slate) } }
        /// One step higher than `surfaceRaised` (badges, chips).
        static var surfaceElevated: Color { dynamic { _ in UIColor(Palette.slate).withAlphaComponent(0.6) } }
        /// Primary text on a surface.
        static var onSurface: Color { dynamic { _ in UIColor.label } }
        /// Secondary / muted text.
        static var onSurfaceMuted: Color { dynamic { $0.userInterfaceStyle == .dark ? UIColor(Palette.mist) : .secondaryLabel } }
        /// Healthy / serving / ok.
        static var ok: Color { Palette.signal }
        /// Busy / waking / warning.
        static var warn: Color { Palette.thermal }
        /// Down / fault.
        static var bad: Color { Palette.halt }
        /// A neutral signal for an unknown / indeterminate state.
        static var neutral: Color { Palette.mist }
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
    }

    /// Build a `Color` that re-resolves per trait collection (used to make the
    /// semantic roles adapt to light/dark automatically).
    private static func dynamic(_ resolve: @escaping (UITraitCollection) -> UIColor) -> Color {
        Color(UIColor { resolve($0) })
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
struct HSStatusDot: View {
    let color: Color
    init(_ color: Color) { self.color = color }

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 10, height: 10)
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

/// Shared title + content section card (the "one card style"). Use everywhere a
/// titled, rounded, raised panel wraps a switch/section of content — replacing
/// the near-identical private `sectionCard` funcs that used to be copy-pasted
/// into every view.
struct HSSectionCard<Content: View>: View {
    let title: String
    let systemImage: String
    @ViewBuilder let content: Content

    init(title: String, systemImage: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.systemImage = systemImage
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }
}

/// The shared red inline error label under a section card.
struct HSErrorLabel: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.bad)
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

// MARK: - Offline stale banner

/// A small reusable banner a view overlays when rendering stale last-known
/// data: the age ("showing state from 6m ago") + the real reason the fetch
/// failed. Tapping refetches. Defined here (with the rest of the system
/// components) because it is pure UI and lives beside the palette.
struct StaleBanner: View {
    /// The staleness age message, e.g. "showing state from 6m ago".
    let age: String
    /// The real reason the fetch failed, e.g. "Can't reach the cluster…".
    let reason: String
    /// Closure run when the operator taps retry.
    var retry: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
            Image(systemName: "clock.arrow.circlepath")
                .foregroundColor(Theme.Semantic.warn)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text("Offline — \(age.lowercased())")
                    .font(.subheadline.weight(.semibold))
                    .foregroundColor(Theme.Semantic.warn)
                if !reason.isEmpty {
                    Text(reason)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            Spacer(minLength: 0)
            Button(action: retry) {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Spacing.md.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }
}
