import Foundation

/// B5 — builds an `HSCCClient` for App Intent execution from the stored
/// settings.
///
/// App Intents run in their own extension/process context, so there is no
/// SwiftUI `SettingsStore` instance to read from. This helper reads the SAME
/// backing stores the settings UI uses — host/port from the App-Group
/// `UserDefaults` suite under the exact keys `SettingsStore` writes, and the
/// token from the Keychain via `KeychainStore` — so a voice intent always
/// sees what the app's Settings screen last saved.
///
/// No networking is defined here: it only constructs the existing
/// `HSCCClient`, keeping a single networking/decoding path in the app.
enum IntentClient {
    /// A configured client, or `nil` when the app isn't set up yet (no host +
    /// token). Callers speak a clear "not configured" message in that case and
    /// fail the intent — never a crash or a silent no-op.
    ///
    /// Reads from the APP-GROUP suite (`group.com.hscc.ios`), NOT
    /// `UserDefaults.standard` — `SettingsStore` writes host/port to that suite
    /// (exactly like the extensions' `APIConfig.load()`). Reading `.standard`
    /// here used to return an empty host, so every voice intent falsely
    /// reported "not configured" even when the app was set up.
    static func make() -> HSCCClient? {
        let defaults = UserDefaults(suiteName: AppGroup.suiteName)
        guard let host = defaults?.string(forKey: AppGroup.hostKey),
              !host.trimmingCharacters(in: .whitespaces).isEmpty,
              let port = Int(defaults?.string(forKey: AppGroup.portKey) ?? "8788"),
              let token = KeychainStore.readToken(), !token.isEmpty else {
            return nil
        }
        return HSCCClient(host: host, port: port, token: token)
    }
}

/// The spoken message used whenever the app's connection settings aren't set
/// up yet. Shared by every intent so the wording stays consistent.
enum IntentSettingsMessage {
    /// `HSCC isn't configured yet — open the app and add your host and token.`
    static let notConfigured =
        "HSCC isn't configured yet — open the app and add your host and token."
}
