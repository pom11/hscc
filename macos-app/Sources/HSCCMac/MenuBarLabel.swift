import SwiftUI

/// The always-mounted menu bar item: the monochrome template H plus the short
/// stat the operator picked in Settings. The image is a template asset, so the
/// system draws it correctly in both light and dark menu bars.
struct MenuBarLabel: View {
    @ObservedObject var dashboard: DashboardMonitor
    @ObservedObject var preferences: MacPreferences

    var body: some View {
        HStack(spacing: 4) {
            Image("MenuBarIcon")
                .renderingMode(.template)
                .resizable()
                .scaledToFit()
                .frame(width: 16, height: 16)
            if let text = dashboard.snapshot.menuBarText(for: preferences.menuBarStat) {
                Text(text)
                    .font(.system(size: 11, weight: .semibold))
                    .monospacedDigit()
                    .opacity(isStale ? 0.5 : 1)
                if isStale, let clock = dataClock {
                    Text("· \(clock)")
                        .font(.system(size: 9))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
        }
        .accessibilityLabel(accessibilityText)
    }

    /// Dim when the last poll failed OR the retained number is older than two
    /// cadences. The label never shows a stale fleet as if it were current
    /// (audit W6, SPEC staleness rule).
    private var isStale: Bool {
        if dashboard.snapshot.menuBarStale { return true }
        guard let at = dashboard.snapshot.menuBarDataAt else { return false }
        let cadence = TimeInterval(preferences.refreshSeconds)
        return Date().timeIntervalSince(at) > 2 * cadence
    }

    private var dataClock: String? {
        guard let at = dashboard.snapshot.menuBarDataAt else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: at)
    }

    private var accessibilityText: String {
        var parts = ["HSCC"]
        if let text = dashboard.snapshot.menuBarText(for: preferences.menuBarStat) {
            // The dense title drops the host, the sensor and the coverage; the
            // sentence puts them back for VoiceOver.
            if let full = dashboard.snapshot.menuBarAccessibilityText(for: preferences.menuBarStat) {
                parts.append(full)
            } else {
                parts.append(text)
            }
            if isStale, let clock = dataClock {
                parts.append("stale, data from \(clock)")
            }
        }
        return parts.joined(separator: ", ")
    }
}
