import SwiftUI

/// HSCC — a private iOS app to manage the owner's DGX cluster + project kanban
/// over Tailscale. This app is NEVER distributed (no App Store); it is
/// sideloaded onto the owner's own device.
///
/// NOTE: This is an UNBUILT, UNTESTED skeleton (Phase B1). The first Xcode /
/// xcodegen build is expected to need small fixes. See ios-app/README.md.
///
/// B5 adds the Siri App Intents surface (in-car, hands-free). The
/// `.appShortcuts(...)` scene registers `AppShortcuts` so the voice shortcuts
/// ("cluster status", "review queue", "dispatch a card") become available to
/// Siri / the Shortcuts app.
@main
struct HSCCApp: App {
    @StateObject private var settings = SettingsStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(settings)
        }
        // B5 — expose the voice shortcuts to Siri for the in-car path.
        .appShortcuts(AppShortcuts())
    }
}
