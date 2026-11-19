import SwiftUI

/// The always-mounted menu bar item: the monochrome template H plus the short
/// stat the operator picked in Settings. The image is a template asset, so the
/// system draws it correctly in both light and dark menu bars.
///
/// Card N4 adds one signal: a small accent dot on the H while alert-feed rows
/// are unviewed. The count itself lives in the dropdown (the card's badge);
/// the dot is only "there is something to read", so the menu bar title keeps
/// its width and the operator's chosen stat is never displaced.
struct MenuBarLabel: View {
    @ObservedObject var dashboard: DashboardMonitor
    @ObservedObject var preferences: MacPreferences
    @ObservedObject var alerts: AlertFeedMonitor

    var body: some View {
        HStack(spacing: 4) {
            ZStack(alignment: .topTrailing) {
                Image("MenuBarIcon")
                    .renderingMode(.template)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 16, height: 16)
                if alerts.unseenCount > 0 {
                    Circle()
                        .fill(Theme.Semantic.accent)
                        .frame(width: 6, height: 6)
                        .offset(x: 2, y: -2)
                }
            }
            if let text = dashboard.snapshot.menuBarText(for: preferences.menuBarStat) {
                Text(text)
                    .font(.system(size: 11, weight: .semibold))
                    .monospacedDigit()
            }
        }
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        var parts = ["HSCC"]
        if alerts.unseenCount > 0 {
            parts.append(alerts.unseenCount == 1
                         ? "1 new alert"
                         : "\(alerts.unseenCount) new alerts")
        }
        if let text = dashboard.snapshot.menuBarText(for: preferences.menuBarStat) {
            // The dense title drops the host, the sensor and the coverage; the
            // sentence puts them back for VoiceOver.
            if let full = dashboard.snapshot.menuBarAccessibilityText(for: preferences.menuBarStat) {
                parts.append(full)
            } else {
                parts.append(text)
            }
        }
        return parts.joined(separator: ", ")
    }
}
