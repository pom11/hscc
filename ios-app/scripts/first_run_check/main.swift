// main.swift — PROVE the QR-scan -> settings path (t_e118313c).
//
// Compiled by scripts/first_run_check.sh together with the REAL
// Sources/HSCC/SetupQRCode.swift; nothing here redeclares the parser. This
// asserts the decode/validate contract the first-run flow depends on, with
// extra attention to the whitespace-token dead-end found in the audit: a
// whitespace-only token must be REJECTED up front (never partially applied),
// so the operator can never be left "configured but 401ing" with no message.
import Foundation

final class Harness {
    static func run() -> Bool {
        var ok = true
        func check(_ cond: Bool, _ label: String) {
            print("\(cond ? "PASS" : "FAIL"): \(label)")
            if !cond { ok = false }
        }

        // ---- 1. Canonical payload decodes (the happy path) ----
        do {
            let code = try SetupQRCode.decode(#"{"v":1,"host":"dgx-tailscale","port":8788,"token":"Tok123"}"#)
            check(code.host == "dgx-tailscale", "1a canonical payload decodes (host)")
            check(code.port == 8788, "1b canonical payload decodes (port)")
            check(code.token == "Tok123", "1c canonical payload decodes (token)")
        } catch {
            print("FAIL: canonical payload threw: \(error)"); ok = false
        }

        // ---- 2. Whitespace-only token is REJECTED (the audit's dead-end) ----
        // Before the fix this passed decode (".isEmpty" doesn't reject "   ")
        // and the app became "configured" while every request 401'd silently.
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":8788,"token":"   "}"#)
            print("FAIL: whitespace-only token decoded (should be rejected)")
            ok = false
        } catch let e as SetupQRCodeError {
            check(true, "2a whitespace-only token rejected (\(e))")
        } catch {
            print("FAIL: whitespace-only token rejected with wrong error: \(error)"); ok = false
        }

        // ---- 3. Empty token is rejected ----
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":8788,"token":""}"#)
            print("FAIL: empty token decoded (should be rejected)")
            ok = false
        } catch {
            check(true, "3a empty token rejected")
        }

        // ---- 4. Token with SURROUNDING whitespace decodes (trim happens at
        // the SettingsView.save choke point before it reaches the Keychain) ----
        do {
            let code = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":8788,"token":"  Tok123  "}"#)
            check(code.token == "  Tok123  ", "4a surrounding-space token decodes (raw; trimmed on save)")
        } catch {
            print("FAIL: surrounding-space token unexpectedly rejected: \(error)"); ok = false
        }

        // ---- 5. Whitespace-only HOST is rejected (parity with token) ----
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"   ","port":8788,"token":"Tok123"}"#)
            print("FAIL: whitespace-only host decoded (should be rejected)")
            ok = false
        } catch {
            check(true, "5a whitespace-only host rejected")
        }

        // ---- 6. Invalid port values are rejected ----
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":0,"token":"Tok123"}"#)
            print("FAIL: port 0 decoded (should be rejected)")
            ok = false
        } catch {
            check(true, "6a port 0 rejected (outside 1-65535)")
        }
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":70000,"token":"Tok123"}"#)
            print("FAIL: port 70000 decoded (should be rejected)")
            ok = false
        } catch {
            check(true, "6b port 70000 rejected (outside 1-65535)")
        }

        // ---- 7. Wrong-schema / truncated / wrong-version payloads fail with
        // an explanatory error, never a silent nothing ----
        // Truncated JSON.
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":878"#)
            print("FAIL: truncated payload decoded")
            ok = false
        } catch {
            check(true, "7a truncated payload rejected")
        }
        // Missing token field.
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":8788}"#)
            print("FAIL: missing-token payload decoded")
            ok = false
        } catch {
            check(true, "7b missing token field rejected")
        }
        // port as a string (wrong schema — contract says int).
        do {
            _ = try SetupQRCode.decode(#"{"v":1,"host":"dgx","port":"8788","token":"Tok123"}"#)
            print("FAIL: string port decoded (contract says int)")
            ok = false
        } catch {
            check(true, "7c string port rejected (wrong schema)")
        }
        // Wrong version is named.
        do {
            _ = try SetupQRCode.decode(#"{"v":2,"host":"dgx","port":8788,"token":"Tok123"}"#)
            print("FAIL: wrong-version payload decoded")
            ok = false
        } catch let e as SetupQRCodeError {
            check(true, "7d wrong version rejected (named: \(e))")
        } catch {
            print("FAIL: wrong version rejected with wrong error"); ok = false
        }
        // Non-JSON garbage.
        do {
            _ = try SetupQRCode.decode("scan-this-plain-string")
            print("FAIL: non-JSON garbage decoded")
            ok = false
        } catch {
            check(true, "7e non-JSON garbage rejected")
        }

        print("")
        if ok {
            print("ALL FIRST-RUN CHECKS PASSED")
            exit(0)
        } else {
            print("FIRST-RUN CHECK FAILURES")
            exit(1)
        }
    }
}

exit(Harness.run() ? 0 : 1)
