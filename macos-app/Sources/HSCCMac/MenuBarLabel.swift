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
            }
        }
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        var parts = ["HSCC"]
        if let text = dashboard.snapshot.menuBarText(for: preferences.menuBarStat) {
            parts.append(text)
        }
        return parts.joined(separator: ", ")
    }
}
