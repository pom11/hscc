import AppKit
import SwiftUI

/// Menu-bar-only entry point. LSUIElement in Info.plist removes the Dock icon;
/// the delegate repeats it as an activation-policy nudge so a Debug launch is
/// never a regular Dock app either.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}

@main
@MainActor
struct HSCCMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var settings = SettingsStore()
    @StateObject private var preferences = MacPreferences()
    @StateObject private var dashboard = DashboardMonitor()
    /// The shared widget/Live Activity registry and Appearance store. Same
    /// App Group blob the widget extension reads; writes reload timelines.
    @StateObject private var widgetSettings = WidgetSettingsStore()
    /// Sparkle 2 (t_2a1e5913): daily automatic checks by default, the menu
    /// item/Settings check, and the "update available" badge in the dropdown.
    @StateObject private var updates = SparkleUpdater()

    var body: some Scene {
        MenuBarExtra {
            DashboardView(dashboard: dashboard)
                .environmentObject(settings)
                .environmentObject(preferences)
                .environmentObject(updates)
        } label: {
            MenuBarLabel(dashboard: dashboard, preferences: preferences)
        }
        .menuBarExtraStyle(.window)

        Settings {
            MacSettingsView()
                .environmentObject(settings)
                .environmentObject(preferences)
                .environmentObject(widgetSettings)
                .environmentObject(dashboard)
                .environmentObject(updates)
        }
    }
}
