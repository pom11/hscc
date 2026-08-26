import Foundation

/// B5 — builds an `HSCCClient` for App Intent execution from the stored
/// settings.
///
/// App Intents run in their own extension/process context, so there is no
/// SwiftUI `SettingsStore` instance to read from. This helper reads the SAME
/// backing stores the settings UI uses — host/port from `UserDefaults` under
/// the exact keys `SettingsStore` writes, and the token from the Keychain via
/// `KeychainStore` — so a voice intent always sees what the app's Settings
/// screen last saved.
///
/// No networking is defined here: it only constructs the existing
/// `HSCCClient`, keeping a single networking/decoding path in the app.
enum IntentClient {
    /// Must match `SettingsStore`'s keys exactly, or a voice intent would read
    /// stale/empty settings.
    private static let hostKey = "hscc.host"
    private static let portKey = "hscc.port"

    /// A configured client, or `nil` when the app isn't set up yet (no host +
    /// token). Callers speak a clear "not configured" message in that case and
    /// fail the intent — never a crash or a silent no-op.
    static func make() -> HSCCClient? {
        let defaults = UserDefaults.standard
        let host = defaults.string(forKey: hostKey) ?? ""
        let portString = defaults.string(forKey: portKey) ?? "8788"
        guard !host.trimmingCharacters(in: .whitespaces).isEmpty,
              let token = KeychainStore.readToken(), !token.isEmpty,
              let port = Int(portString) else {
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
