import Foundation
import Combine

/// Holds the operator's SAVED CLUSTERS: a named list of host/port/token sets,
/// exactly one of which is active (the cluster the app currently connects to).
///
/// The list itself lives in the shared App-Group `UserDefaults` (as JSON), the
/// active-cluster id in the same suite, and each cluster's token in the
/// Keychain (never UserDefaults/source) — see `KeychainStore`.
///
/// Why multiple clusters: an operator may keep a production cluster and a test
/// cluster, or want to demo the app against a colleague's cluster without
/// destroying their own settings. The active cluster's host/port/token are
/// mirrored into the legacy `hscc.host`/`hscc.port` keys and the legacy
/// `api-token` Keychain item so the widget / Live Activity / Siri intents
/// (which read those single keys) keep working against the ACTIVE cluster with
/// no changes to their code.
///
/// This is an `ObservableObject` so the root view and Settings can react to
/// changes as the operator edits. It is the single source of truth for the
/// cluster list; `HSCCClient` builds each request from the active cluster.
final class SettingsStore: ObservableObject {
    // MARK: - State

    /// Every saved cluster, in the order the operator sees them.
    @Published private(set) var clusters: [SavedCluster]
    /// Which cluster is currently active (the one the app connects to).
    @Published private(set) var activeClusterID: UUID?
    /// Non-nil when the most recent `saveCluster` could not write its token to
    /// the Keychain (e.g. the device blocked the write). Surfaced in Settings
    /// so a saved host/port is never mistaken for a saved CONNECTION — the
    /// token is what makes it usable. Cleared on the next successful write.
    @Published private(set) var tokenSaveFailure: String?
    /// True when the shared App-Group `UserDefaults` suite could not be
    /// opened, so `saveCluster` fell back to the app's own `.standard` defaults.
    /// The main app still reads that fallback (so it looks configured and
    /// works), but the widget / Live Activity / Siri intents read the shared
    /// suite directly with NO fallback (see `APIConfig`) and would see
    /// nothing. Surfaced so a broken App Group never silently strands the
    /// extended surfaces while the app reports success.
    @Published private(set) var appGroupUnavailable: Bool

    /// The shared suite the extensions read from too.
    ///
    /// STATIC on purpose: `init()` reads it to seed the stored properties, and a
    /// computed *instance* property would touch `self` before `clusters` are
    /// initialized — which Swift rejects. It depends on no instance state, so
    /// static is also the honest signature.
    private static var suite: UserDefaults {
        UserDefaults(suiteName: AppGroup.suiteName) ?? .standard
    }

    // MARK: - Lifecycle

    init() {
        let defaults = Self.suite

        // Detect a silent App-Group fallback BEFORE seeding state, so the flag
        // is set on the very first launch even when nothing is saved yet. A
        // missing suite means the app is about to write to its own `.standard`
        // defaults while the extensions read the (absent) shared suite.
        // `containerURL(forSecurityApplicationGroupIdentifier:)` is the
        // canonical check: it returns nil on a REAL device when the App-Group
        // entitlement is absent / the container isn't provisioned. (Note: on
        // macOS it always returns a URL even for an unregistered group, so this
        // flag cannot be exercised headlessly here — it is a device-only signal;
        // the headless harness instead proves the identity/save logic.)
        appGroupUnavailable = FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: AppGroup.suiteName) == nil

        // Decode the saved cluster list, tolerating a corrupt/missing value.
        if let raw = defaults.data(forKey: AppGroup.clustersKey),
           let decoded = try? JSONDecoder().decode([SavedCluster].self, from: raw) {
            self.clusters = decoded
        } else {
            self.clusters = []
        }

        // Resolve the active cluster: the saved id if it still exists,
        // otherwise the first cluster.
        if let idStr = defaults.string(forKey: AppGroup.activeClusterIDKey),
           let id = UUID(uuidString: idStr),
           clusters.contains(where: { $0.id == id }) {
            self.activeClusterID = id
        } else {
            self.activeClusterID = clusters.first?.id
        }

        // MIGRATION: fresh install of the multi-cluster build, but a legacy
        // single-cluster config exists. Import it so the operator's existing
        // settings are never lost.
        if clusters.isEmpty {
            let legacyHost = defaults.string(forKey: AppGroup.hostKey) ?? ""
            if !legacyHost.trimmingCharacters(in: .whitespaces).isEmpty {
                let legacyPort = Int(defaults.string(forKey: AppGroup.portKey) ?? "8788") ?? 8788
                let legacyToken = KeychainStore.readToken()
                var imported = SavedCluster(name: legacyHost, host: legacyHost, port: legacyPort)
                if legacyToken != nil {
                    imported.lastTestSuccess = true
                    imported.lastConnected = Date()
                }
                clusters = [imported]
                activeClusterID = imported.id
                // Copy the legacy token into the new cluster's own account.
                if let t = legacyToken { KeychainStore.saveToken(t, forCluster: imported.id) }
                persistClusters()
            }
        }

        // Publish the active cluster to the legacy keys so the extensions and
        // intents see it from the very first launch.
        publishActiveCluster()
    }

    // MARK: - Active-cluster access

    /// The currently active cluster, or nil when none is configured.
    var activeCluster: SavedCluster? {
        guard let id = activeClusterID else { return nil }
        return clusters.first(where: { $0.id == id })
    }

    // Active-cluster proxy accessors. Kept with the original signatures so the
    // existing views/intents build unchanged — they read the ACTIVE cluster.

    var host: String { activeCluster?.host ?? "" }

    var port: String { activeCluster.map { String($0.port) } ?? "" }

    var token: String? {
        guard let c = activeCluster else { return nil }
        return KeychainStore.readToken(forCluster: c.id)
    }

    var hasToken: Bool { token != nil }

    /// A value that changes whenever the ACTIVE cluster's host, port, or token
    /// change. Used by the root connection banner to know when to re-probe.
    ///
    /// `isConfigured` alone is not enough: it stays `true` when an
    /// already-configured cluster's token is swapped for a wrong one (or its
    /// host changed to an unreachable address), so a banner keyed only on
    /// `isConfigured` would keep showing the last successful ping — a stale
    /// green check while the stored config is unusable. A fingerprint of
    /// the actual host/port/token catches every edit. The token is reduced to a
    /// presence/hash marker here, never the raw secret.
    var connectionIdentity: String {
        "\(host)|\(port)|\((token ?? "<none>").hashValue)"
    }

    /// True when the app is configured to be useful against the active cluster
    /// (a host and a token are both set).
    var isConfigured: Bool {
        activeCluster != nil && hasToken
    }

    /// Re-read the Keychain token presence so SwiftUI re-evaluates `hasToken` /
    /// `isConfigured` after a write from elsewhere. `hasToken` is computed, so
    /// this just nudges the object to re-publish.
    func refreshTokenPresence() {
        objectWillChange.send()
    }

    // MARK: - Mutation

    /// Upsert a cluster and store its token.
    ///
    /// A brand-new cluster (id not already saved) becomes the active cluster —
    /// connecting to it immediately after adding is the expected flow. Editing
    /// an existing cluster keeps its place and, if it was active, re-publishes
    /// the (possibly changed) host/port/token to the legacy keys.
    ///
    /// - Parameters:
    ///   - cluster: the full cluster to save (id stable across edits).
    ///   - token: the token to store for this cluster. nil/empty deletes it.
    func saveCluster(_ cluster: SavedCluster, token: String?) {
        KeychainStore.saveToken(token, forCluster: cluster.id)
        // The Keychain write reports a failure through `lastError` (it cannot
        // throw). Surface it here so the operator is told when a saved host/port
        // did NOT come with a usable token — otherwise the UI would look saved
        // while the app reads back as "not configured".
        if let token, !token.isEmpty, let status = KeychainStore.lastError {
            tokenSaveFailure = "The token could not be saved (Keychain OSStatus \(status)). Host and port were saved, but the token was not — the connection will not work."
        } else {
            tokenSaveFailure = nil
        }
        if let idx = clusters.firstIndex(where: { $0.id == cluster.id }) {
            clusters[idx] = cluster
        } else {
            clusters.append(cluster)
            activeClusterID = cluster.id
        }
        persistClusters()
        publishActiveCluster()
    }

    /// Make a saved cluster the active one and publish it to the legacy keys.
    func selectCluster(_ id: UUID) {
        guard clusters.contains(where: { $0.id == id }) else { return }
        if activeClusterID == id {
            publishActiveCluster()
            return
        }
        activeClusterID = id
        persistClusters()
        publishActiveCluster()
    }

    /// Delete a saved cluster and its Keychain token. If it was active, the
    /// first remaining cluster (if any) becomes active.
    func deleteCluster(_ id: UUID) {
        guard clusters.contains(where: { $0.id == id }) else { return }
        KeychainStore.deleteToken(forCluster: id)
        clusters.removeAll { $0.id == id }
        if activeClusterID == id {
            activeClusterID = clusters.first?.id
        }
        persistClusters()
        publishActiveCluster()
    }

    /// Record the outcome of a connection test / use for a cluster — drives the
    /// health dot and the "last connected" timestamp. No effect on the legacy
    /// keys (health/last are not part of them).
    func recordTestResult(for id: UUID, success: Bool, connectedAt: Date = Date()) {
        guard let idx = clusters.firstIndex(where: { $0.id == id }) else { return }
        var c = clusters[idx]
        c.lastTestSuccess = success
        if success { c.lastConnected = connectedAt }
        clusters[idx] = c
        persistClusters()
    }

    // MARK: - Persistence

    private func persistClusters() {
        let defaults = Self.suite
        if let data = try? JSONEncoder().encode(clusters) {
            defaults.set(data, forKey: AppGroup.clustersKey)
        }
        if let id = activeClusterID {
            defaults.set(id.uuidString, forKey: AppGroup.activeClusterIDKey)
        } else {
            defaults.removeObject(forKey: AppGroup.activeClusterIDKey)
        }
    }

    /// Mirror the active cluster's host/port/token into the LEGACY single-value
    /// keys that the extensions / intents / `APIConfig` read. When there is no
    /// active cluster, clear the legacy host/port so nothing keeps pointing at
    /// a dead cluster.
    private func publishActiveCluster() {
        let d = Self.suite
        guard let c = activeCluster else {
            d.removeObject(forKey: AppGroup.hostKey)
            d.removeObject(forKey: AppGroup.portKey)
            return
        }
        d.set(c.host, forKey: AppGroup.hostKey)
        d.set(String(c.port), forKey: AppGroup.portKey)
        // Mirror the active cluster's token into the legacy item the
        // extensions/intents read.
        if let t = KeychainStore.readToken(forCluster: c.id) {
            KeychainStore.saveToken(t)
        }
    }
}
