import SwiftUI

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
        // HSCC Monitor Live Activity (t_03d318cd). Scene phase drives the
        // foreground poll loop (active → poll; background → stop the tight loop
        // + schedule a BGAppRefresh). The one-time launch hooks (task
        // registration + orphan sweep) run in `LaunchAppDelegate`
        // `didFinishLaunching` — a Scene has no `.task`, and the delegate is
        // the correct launch seam.
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                // In-app update check (t_2a1e5913): launch + every foreground
                // entry, throttled to one attempt per six hours by
                // UpdatePreferences. Never touches the cluster connection.
                Task { await updates.checkIfDue() }
                // Re-hydrate leftover orphans + resume the foreground poll loop.
                MonitorActivityDriver.sweepLeftoverMonitors()
                MonitorActivityDriver.shared.sceneDidBecomeActive()
                // Cluster-health (t_f2867fb3): resume the poll loop. Deliberately
                // NO sweep call here — this surface's sweep is age-based (six
                // hours of silence) rather than settings-based, and running it on
                // every foreground entry would END a bubble the driver still
                // holds a live handle to, after which every push would land on
                // an activity that no longer exists. The sweep runs at launch,
                // which is when a dead handle is actually a dead handle.
                ClusterHealthActivityDriver.shared.sceneDidBecomeActive()
                // The running-cards activity (t_71deac83): same seam, its own
                // driver, so the two Live Activities never share a cadence, a
                // toggle or a background task. Same rule as above about the
                // sweep — `CardsRunningActivitySweep` runs at launch and not
                // here, because the driver may be holding a handle the sweep
                // would end from underneath it.
                CardsRunningActivityDriver.shared.sceneDidBecomeActive()
                // The per-board progress activity (t_451cbe25) is
                // operator-started, so there is nothing to sweep: adopting a
                // bubble left by a previous process and resuming its poll is
                // all this hook does. No activity up → no reads.
                BoardProgressActivityDriver.shared.sceneDidBecomeActive()
            case .background, .inactive:
                MonitorActivityDriver.shared.sceneDidEnterBackground()
                ClusterHealthActivityDriver.shared.sceneDidEnterBackground()
                CardsRunningActivityDriver.shared.sceneDidEnterBackground()
                BoardProgressActivityDriver.shared.sceneDidEnterBackground()
            @unknown default:
                break
            }
        }
    }
}
