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
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "clock.arrow.circlepath")
                .foregroundColor(Theme.Semantic.warn)
            VStack(alignment: .leading, spacing: 2) {
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
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }
}
