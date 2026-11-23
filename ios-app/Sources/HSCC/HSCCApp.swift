import SwiftUI
import WidgetKit

/// HSCC — a private iOS app to manage the owner's DGX cluster + project kanban
/// over Tailscale. This app is NEVER distributed (no App Store); it is
/// sideloaded onto the owner's own device.
///
/// NOTE: This is an UNBUILT, UNTESTED skeleton (Phase B1). The first Xcode /
/// xcodegen build is expected to need small fixes. See ios-app/README.md.
///
/// B5 adds the Siri App Intents surface (in-car, hands-free). Declaring a type
/// that conforms to `AppShortcutsProvider` (see Intents/AppShortcuts.swift) is
/// all that is needed — the system discovers it at build time. There is NO
/// scene modifier to attach it with; an earlier `.appShortcuts(...)` call here
/// did not compile.
@main
struct HSCCApp: App {
    /// The Live Activities' background-refresh registration and launch hooks.
    /// Notifications are deliberately absent (operator decision 2026-11-19).
    @UIApplicationDelegateAdaptor(LaunchAppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var settings = SettingsStore()
    /// In-app update check (t_2a1e5913). App target only: the extensions get
    /// neither this object nor UpdateCheck.swift/UpdateChecker.swift.
    @StateObject private var updates = UpdateChecker()
    /// Shared Widgets & Live Activities settings (t_dea36c48) — exposed to the
    /// Settings UI as an EnvironmentObject so it can read/write the App-Group
    /// store that every widget/Live Activity extension reads read-only.
    @StateObject private var widgetSettings = WidgetSettingsStore()
    /// Deep-link router (t_136762f3): the single entry point for `hscc://`
    /// URLs and Handoff, injected everywhere navigation happens so every
    /// external link can land the operator on the exact project, card or
    /// session.
    @StateObject private var router = DeepLinkRouter.shared

    init() {
        // t_0f3f70e6 (iOS pivot): the fixture/offline capture harness
        // (DebugHarness.swift) is retired with the console — its only
        // consumers were the XCUITest nav lane and the screenshot capture
        // scripts. There is no debug launch state left to branch on.
        //
        // The `unread` / `replyWatcher` pair this used to build in init() (they
        // had to share ONE ProjectUnreadCenter) went with the chat family in
        // t_9d5a30c9 — CONSOLE_RETIREMENT §5 S2.
        _settings = StateObject(wrappedValue: SettingsStore())
    }

    var body: some Scene {
        WindowGroup {
            // The two-screen root (t_9664a1c8, pivot slice S1): connection
            // settings + widget/Live Activity configuration. No drawer, no dock,
            // no detail stack — every data surface the console rendered is now a
            // widget or a Live Activity.
            ConfigView()
                .environmentObject(settings)
                .environmentObject(widgetSettings)
                .environmentObject(router)
                .environmentObject(updates)
                // Dark-only (operator request 2026-11-18): the black/orange
                // palette has no light variants, so pin every scene to dark
                // even if the device is set to light. The Info.plist
                // `UIUserInterfaceStyle=Dark` key backs this up at launch.
                .preferredColorScheme(.dark)
        }
        // Cluster watch (A2), the one Live Activity. Scene phase resumes or
        // stops its foreground loop; the one-time hooks (task registration +
        // orphan sweep) run in `LaunchAppDelegate.didFinishLaunching`. An
        // `.active` transition only ADOPTS an operator-started watch — it never
        // starts one.
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                // In-app update check (t_2a1e5913): launch + every foreground
                // entry, throttled to one attempt per six hours by
                // UpdatePreferences. Never touches the cluster connection.
                Task { await updates.checkIfDue() }
                // A foreground entry is new data's best chance to reach a
                // widget. WidgetKit coalesces scheduled reloads, so ask for a
                // rebuild now rather than waiting out the timeline.
                WidgetCenter.shared.reloadAllTimelines()
                ClusterHealthActivityDriver.shared.sceneDidBecomeActive()
            case .background, .inactive:
                ClusterHealthActivityDriver.shared.sceneDidEnterBackground()
            @unknown default:
                break
            }
        }
    }
}
