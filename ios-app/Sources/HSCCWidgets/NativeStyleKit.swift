import SwiftUI

// ---------------------------------------------------------------------------
// Native style kit — the shared look for the apps 1.4 widgets.
//
// The operator picked "Native" in the 2026-11-20 widget redesign: system
// colours, SF Pro Rounded for numbers and headlines, colour only for state.
// This file is the one place those tokens and the four shared components live,
// so a later widget card composes the kit instead of re-typing a font or a
// threshold. It intentionally paints no background: every widget root uses
// `containerBackground(.fill.tertiary, for: .widget)`, which is the system
// default for both platforms.
//
// State bands are the contract's, not a hardware spec:
//   temperature < 70 ok (green), 70-74 warn (orange), >= 75 bad (red).
// ---------------------------------------------------------------------------

enum NativeStyle {
    static let labelSize: CGFloat = 11
    static let heroSize: CGFloat = 34
    static let subSize: CGFloat = 13
    static let smallSize: CGFloat = 11

    static func label() -> Font { .system(size: labelSize, weight: .semibold) }
    static func hero(_ size: CGFloat = heroSize) -> Font {
        .system(size: size, weight: .bold, design: .rounded)
    }
    static func sub() -> Font { .system(size: subSize, weight: .semibold) }
    static func small() -> Font { .system(size: smallSize) }
    static func number(_ size: CGFloat = smallSize, weight: Font.Weight = .semibold) -> Font {
        .system(size: size, weight: weight).monospacedDigit()
    }

    static let accent = Color.blue
    static let ok = Color.green
    static let warn = Color.orange
    static let bad = Color.red

    static func temperature(_ celsius: Double?) -> Color {
        guard let celsius else { return .secondary }
        if celsius >= 75 { return bad }
        if celsius >= 70 { return warn }
        return ok
    }

    static func utilisation(_ percent: Double?) -> Color {
        guard let percent, percent > 85 else { return accent }
        return warn
    }

    static func hhmm(_ date: Date?) -> String {
        guard let date else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }
}

/// The data's own clock: HH:mm of the sample, never of the render.
struct AsOfStamp: View {
    let date: Date?
    var prefix: String? = nil

    var body: some View {
        Text(text)
            .font(NativeStyle.small())
            .monospacedDigit()
            .foregroundStyle(.secondary)
            .lineLimit(1)
    }

    private var text: String {
        let clock = NativeStyle.hhmm(date)
        guard let prefix, !prefix.isEmpty else { return clock }
        return "\(prefix) · \(clock)"
    }
}

/// A 6 pt capsule meter. `value` is a 0…1 fraction; nil draws the empty track.
struct MeterBar: View {
    let value: Double?
    var tint: Color = NativeStyle.accent
    var height: CGFloat = 6

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                if let value {
                    Capsule()
                        .fill(tint)
                        .frame(width: geometry.size.width * min(max(value, 0), 1))
                }
            }
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}

/// A 12 pt rounded content tile on the system quaternary fill.
struct Tile<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(10)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

/// The data's own age, shared by every Native widget. Stale is measured against
/// the surface's refresh interval: older than 2x means dim; a failed fetch means
/// offline. `label` names the clock of the data, never the render.
struct StaleState: Equatable {
    let dataAt: Date?
    let fetchedAt: Date
    let refreshMinutes: Int
    let failed: Bool

    var referenceDate: Date { dataAt ?? fetchedAt }
    var ageMinutes: Int {
        max(0, Int(Date().timeIntervalSince(referenceDate) / 60))
    }
    var isOffline: Bool { failed }
    var isStale: Bool {
        !failed && refreshMinutes > 0 && ageMinutes > 2 * refreshMinutes
    }
    var isDimmed: Bool { isOffline || isStale }
    var label: String? {
        let clock = Self.hhmm(referenceDate)
        if isOffline { return "offline · \(clock)" }
        if isStale { return "stale · \(clock)" }
        return nil
    }

    private static func hhmm(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }
}

struct StaleModifier: ViewModifier {
    let state: StaleState?

    func body(content: Content) -> some View {
        content
            .opacity(state?.isDimmed == true ? 0.5 : 1)
            .overlay(alignment: .bottomTrailing) {
                if let label = state?.label {
                    Text(label)
                        .font(NativeStyle.small())
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .padding(.top, 2)
                }
            }
    }
}

extension View {
    func stale(_ state: StaleState?) -> some View {
        modifier(StaleModifier(state: state))
    }
}
