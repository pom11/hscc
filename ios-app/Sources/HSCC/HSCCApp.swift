import SwiftUI

/// HSCC — a private iOS app to manage the owner's DGX cluster + project kanban
/// over Tailscale. This app is NEVER distributed (no App Store); it is
/// sideloaded onto the owner's own device.
///
/// NOTE: This is an UNBUILT, UNTESTED skeleton (Phase B1). The first Xcode /
/// xcodegen build is expected to need small fixes. See ios-app/README.md.
@main
struct HSCCApp: App {
    @StateObject private var settings = SettingsStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(settings)
        }
    }
}
