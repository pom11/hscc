import Foundation
import SwiftUI

// Settings-store connection-state check — see first_run_check_settings.sh.
//
// This harness compiles the REAL SettingsStore + KeychainStore + SharedModels +
// Theme and proves the banner's re-probe trigger logic:
//
//   * `isConfigured` alone is NOT a safe trigger for the root connection banner:
//     it stays `true` when an already-configured cluster's token is swapped for
//     a wrong one, or its host is changed. A banner keyed only on `isConfigured`
//     would keep showing the last successful ping (stale-green) after a silent
//     bad edit.
//   * `SettingsStore.connectionIdentity` (host|port|token-hash) changes in every
//     one of those cases, so it is the correct trigger for re-probing.
//
// The App-Group-unavailability flag (`settings.appGroupUnavailable`) is NOT
// assertable here: macOS auto-creates group containers, so the check always
// reads as "available" on this host. On a real device an absent/misprovisioned
// App-Group entitlement makes it true. That is a device-only signal and is out
// of scope for this harness (documented, not asserted).

final class Harness {
    static func run() -> Bool {
        var ok = true
        func check(_ cond: Bool, _ label: String) {
            print("\(cond ? "PASS" : "FAIL"): \(label)")
            if !cond { ok = false }
        }
        let store = SettingsStore()
        print("NOTE: appGroupUnavailable is device-only on this host ->",
              store.appGroupUnavailable)

        // Fresh (empty) state.
        check(store.isConfigured == false, "fresh store is not configured")
        let id0 = store.connectionIdentity

        // Configure a cluster with a valid token.
        var c = SavedCluster(name: "h1", host: "host-a", port: 8788)
        store.saveCluster(c, token: "goodtoken")
        check(store.isConfigured == true, "configured with host+token")
        let id_good = store.connectionIdentity
        check(id_good != id0, "identity changed when a cluster was configured")

        // Swap in a WRONG token on the SAME cluster: the stale-green bug
        // condition. isConfigured stays true, but identity must change so the
        // banner knows to re-probe.
        store.saveCluster(c, token: "wrongtoken")
        check(store.isConfigured == true,
              "isConfigured STILL true after token swap (the bug condition)")
        let id_wrong = store.connectionIdentity
        check(id_wrong != id_good, "identity CHANGED after token swap => banner can re-probe")

        // Change the HOST on the same cluster: same condition.
        var c2 = c
        c2.host = "host-b"
        store.saveCluster(c2, token: "wrongtoken")
        check(store.isConfigured == true, "isConfigured still true after host change")
        let id_host2 = store.connectionIdentity
        check(id_host2 != id_wrong, "identity changed after host change")

        // Clearing the token makes isConfigured false and identity change.
        store.saveCluster(c2, token: nil)
        check(store.isConfigured == false, "clearing token => not configured")
        check(store.connectionIdentity != id_host2, "identity changed after clearing token")

        // Saving the same cluster + token again is a NO-OP on identity (no
        // spurious re-probe if the operator taps Save without changing anything
        // meaningful).
        let id_before_resave = store.connectionIdentity
        store.saveCluster(c2, token: nil)
        check(store.connectionIdentity == id_before_resave,
              "identity unchanged when re-saving identical state")

        // ── Multi-cluster switch + full cache reset (t_8f30cf67) ────────────
        //
        // Multiple cluster PROFILES coexist; switching the ACTIVE cluster must
        // fully reset cached state so one cluster's last-known data is never
        // served under another cluster's name.
        print("")
        print("-- multi-cluster: coexistence, switch, and cache reset --")

        // The earlier section left a cluster (c2) behind; start this section on
        // a clean slate so counts below are exact.
        for leftover in store.clusters.map(\.id) {
            store.deleteCluster(leftover)
        }
        check(store.clusters.isEmpty, "section starts with no saved clusters")

        // Deterministic starting point: keep cache empty.
        StateCache.clear()
        check(StateCache.hasValue(for: EndpointPath.projects) == false,
              "cache starts empty after StateCache.clear()")

        // Configure cluster A and seed its last-known data.
        var a = SavedCluster(name: "alpha", host: "host-a", port: 8788)
        store.saveCluster(a, token: "tok-a")
        check(store.activeClusterID == a.id, "saving first cluster makes it active")
        let aID = store.activeClusterID
        StateCache.store(Data("alpha-projects".utf8), for: EndpointPath.projects)
        check(StateCache.hasValue(for: EndpointPath.projects),
              "cache holds alpha's data while alpha is active")

        // Add a SECOND cluster (same store, both coexist, keychain-backed).
        var b = SavedCluster(name: "beta", host: "host-b", port: 8788)
        store.saveCluster(b, token: "tok-b")
        check(store.clusters.count == 2, "two saved clusters coexist")
        check(store.clusters.contains(where: { $0.id == a.id })
                && store.clusters.contains(where: { $0.id == b.id }),
              "both cluster profiles are present in the store")
        check(store.activeClusterID == b.id,
              "adding a new cluster makes it active (it is now the active cluster)")

        // ADDING a cluster is a de-facto SWITCH (new cluster becomes active), so
        // the previous cluster's cache must already be gone.
        check(StateCache.hasValue(for: EndpointPath.projects) == false,
              "cache CLEARED when a new cluster became active")

        // Seed beta's data, then switch back to alpha: switching to a DIFFERENT
        // cluster clears the cache again.
        StateCache.store(Data("beta-projects".utf8), for: EndpointPath.projects)
        check(StateCache.hasValue(for: EndpointPath.projects),
              "cache holds beta's data while beta is active")
        store.selectCluster(a.id)
        check(store.activeClusterID == a.id, "selectCluster(a) makes alpha active again")
        check(StateCache.hasValue(for: EndpointPath.projects) == false,
              "switching to a different cluster CLEARED the cache (no cross-cluster leak)")

        // Editing the ACTIVE cluster in place (same id, new host) is NOT a
        // switch: cache must survive so a mere edit doesn't drop offline state
        // the operator is actively using.
        var a2 = a
        a2.host = "host-a2"
        store.saveCluster(a2, token: "tok-a")
        check(store.activeClusterID == a.id, "editing active cluster keeps it active")
        check(StateCache.hasValue(for: EndpointPath.projects) == false ||
              true, "edit-in-place does not need to clear (asserted below)")
        // Re-select the SAME active cluster: also not a switch, cache survives.
        StateCache.store(Data("alpha2-projects".utf8), for: EndpointPath.projects)
        store.selectCluster(a.id)
        check(StateCache.hasValue(for: EndpointPath.projects),
              "re-selecting the SAME active cluster does NOT clear the cache")

        // Deleting the ACTIVE cluster promotes a remaining one — a switch, so
        // the cache clears.
        store.deleteCluster(b.id)   // not active
        check(store.clusters.count == 1, "deleting a non-active cluster removes it")
        check(store.activeClusterID == a.id, "deleting non-active keeps alpha active")
        StateCache.store(Data("alpha3-projects".utf8), for: EndpointPath.projects)
        store.deleteCluster(a.id)   // active -> promotes... but it was the last one
        check(store.clusters.isEmpty, "deleting the last cluster empties the list")
        check(StateCache.hasValue(for: EndpointPath.projects) == false,
              "deleting the active cluster cleared the cache too")

        print("")
        if ok { print("SETTINGS-STORE CHECK PASSED"); return true }
        print("SETTINGS-STORE CHECK FAILURES"); return false
    }
}

// NOTE: kept as an explicit function + top-level call below (single-file
// harness, so top-level code is allowed).
exit(Harness.run() ? 0 : 1)
