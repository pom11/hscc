import Foundation
import Combine

/// Holds the user-configured connection settings: host + port in `UserDefaults`
/// and the bearer token in the Keychain (never in UserDefaults or source).
///
/// This is an `ObservableObject` so the root view and Settings can react to
/// changes as the user edits the fields. It is the single source of truth for
/// host/port/token; `HSCCClient` builds each request from these values.
final class SettingsStore: ObservableObject {
    /// Published copy of the host the user is editing / has saved.
    @Published var host: String {
        didSet { UserDefaults.standard.set(host, forKey: Self.hostKey) }
    }

    /// Published copy of the port as a string (kept in the text field as text;
    /// converted to an Int when building the URL).
    @Published var port: String {
        didSet { UserDefaults.standard.set(port, forKey: Self.portKey) }
    }

    /// The current token, backed by the Keychain. Exposed for the Settings UI
    /// to edit and for the client to read.
    @Published private(set) var hasToken: Bool

    private static let hostKey = "hscc.host"
    private static let portKey = "hscc.port"

    init() {
        let defaults = UserDefaults.standard
        // Host starts EMPTY — the owner enters their tailnet host/IP at runtime
        // (never a hardcoded default / no tailnet IP committed).
        self.host = defaults.string(forKey: Self.hostKey) ?? ""
        // Port defaults to the HSCC API default (8787) but stays editable.
        self.port = defaults.string(forKey: Self.portKey) ?? "8787"
        self.hasToken = (KeychainStore.readToken() != nil)
    }

    var token: String? {
        KeychainStore.readToken()
    }

    /// Save a token to the Keychain and update the published flag.
    func saveToken(_ token: String?) {
        KeychainStore.saveToken(token)
        hasToken = (KeychainStore.readToken() != nil)
    }

    /// True when a host and a token are both set — the precondition for a
    /// useful connection.
    var isConfigured: Bool {
        !host.trimmingCharacters(in: .whitespaces).isEmpty && hasToken
    }

    /// Re-read the Keychain token presence (e.g. after the test-connection flow).
    func refreshTokenPresence() {
        hasToken = (KeychainStore.readToken() != nil)
    }
}
