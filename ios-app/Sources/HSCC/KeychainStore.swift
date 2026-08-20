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
    private static let service = "com.hscc.ios"
    private static let account = "api-token"
    private static let accessGroup = ""

    /// Read the stored token, or nil when none is saved.
    static func readToken() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
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
            let query: [String: Any] = [
                kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: service,
                kSecAttrAccount as String: account,
                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            ]
            let attributes: [String: Any] = [
                kSecValueData as String: data,
                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            ]
            let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
            if status == errSecItemNotFound {
                var addQuery = query
                addQuery[kSecValueData as String] = data
                SecItemAdd(addQuery as CFDictionary, nil)
            }
            // Ignore the update status: either the item updated or we added it.
        } else {
            deleteToken()
        }
    }

    /// Delete any stored token.
    static func deleteToken() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
