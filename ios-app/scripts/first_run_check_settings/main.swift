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
        print("")
        if ok { print("SETTINGS-STORE CHECK PASSED"); return true }
        print("SETTINGS-STORE CHECK FAILURES"); return false
    }
}

// NOTE: kept as an explicit function + top-level call below (single-file
// harness, so top-level code is allowed).
exit(Harness.run() ? 0 : 1)
