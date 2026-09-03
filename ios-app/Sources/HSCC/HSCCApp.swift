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
    /// Requests notification authorization once at launch so the foreground
    /// NotificationCoordinator can present needs-operator banners (t_0454eb56).
    @UIApplicationDelegateAdaptor(NotificationsAppDelegate.self) private var appDelegate
    @StateObject private var settings = SettingsStore()
    @StateObject private var unread: ProjectUnreadCenter
    @StateObject private var replyWatcher: StreamReplyWatcher

    /// `replyWatcher` needs the SAME `ProjectUnreadCenter` the rest of the app
    /// sees, but a property initializer cannot reference another property —
    /// they run before `self` exists. Build both in `init()` and seed the
    /// wrappers directly, which keeps one shared unread center rather than two
    /// that silently disagree about what has been read.
    init() {
        let center = ProjectUnreadCenter()
        _unread = StateObject(wrappedValue: center)
        _replyWatcher = StateObject(wrappedValue: StreamReplyWatcher(unread: center))
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(settings)
                .environmentObject(unread)
                .environmentObject(replyWatcher)
        }
    }
}
