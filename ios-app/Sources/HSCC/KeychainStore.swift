import Foundation
import Security

/// Stores the API bearer token in the iOS Keychain.
///
/// The token is a secret and MUST never live in UserDefaults, a plist, or
/// source code. This is the single place the app reads/writes it. The value is
/// held in an `kSecClassGenericPassword` item scoped to this app's access
/// group, tagged with a purpose constant so the item is easy to delete.
enum KeychainStore {
    /// Purpose string identifying this app's token item in the Keychain.
    private static let service = AppGroup.keychainService
    private static let account = AppGroup.keychainAccount
    /// The shared App-Group access group so the widget/Live Activity extensions
    /// can read the SAME token item (`$(AppIdentifierPrefix)` = team prefix
    /// resolved at build time from the entitlements file).
    private static let accessGroup = KeychainConstants.keychainAccessGroup

    /// Last non-success OSStatus from a write, for diagnosing a failed save.
    /// nil once a write succeeds.
    private(set) static var lastError: OSStatus?

    /// Read the stored token, or nil when none is saved.
    static func readToken() -> String? {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        // Apply the shared access group when it is non-empty (always is via
        // KeychainConstants), so app + extensions read the same item.
        let ag = accessGroup.trimmingCharacters(in: .whitespaces)
        if !ag.isEmpty {
            query[kSecAttrAccessGroup as String] = ag
        }
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    /// Persist (create or update) the token in the Keychain.
    ///
    /// - Parameter token: the token to store. Pass nil/empty to delete it.
    static func saveToken(_ token: String?) {
        if let token, !token.isEmpty {
            let data = Data(token.utf8)
            var query: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: account,
                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            ]
            let ag = accessGroup.trimmingCharacters(in: .whitespaces)
            if !ag.isEmpty {
                query[kSecAttrAccessGroup as String] = ag
            }
            let attributes: [String: Any] = [
                kSecValueData as String: data,
                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            ]
            let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
            if status == errSecItemNotFound {
                var addQuery = query
                addQuery[kSecValueData as String] = data
                let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
                lastError = (addStatus == errSecSuccess) ? nil : addStatus
            }
            // Surface a real failure instead of swallowing it. A silent
            // Keychain write is what made a filled-in form still report
            // "Set a host, port, and token first".
            if status == errSecSuccess { lastError = nil }
            else if status != errSecItemNotFound { lastError = status }
        } else {
            deleteToken()
        }
    }

    /// Delete any stored token.
    static func deleteToken() {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let ag = accessGroup.trimmingCharacters(in: .whitespaces)
        if !ag.isEmpty {
            query[kSecAttrAccessGroup as String] = ag
        }
        SecItemDelete(query as CFDictionary)
    }
}
