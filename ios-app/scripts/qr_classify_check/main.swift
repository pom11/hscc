// main.swift — PROVE QRPairing.classify maps each HSCCError to the right,
// actionable QRPairingOutcome (t_cf296e48).
//
// Compiled by scripts/qr_classify_check.sh with the REAL APIError.swift
// (HSCCError) and the REAL QRPairingOutcome + QRPairing.classify sliced out of
// SetupQRCode.swift. Nothing here redeclares them. The `test` method is
// excluded because it drags in HSCCClient/URLSession; classify is pure and is
// the logic the Settings connect step and onboarding path now depend on.
import Foundation

final class Harness {
    static func run() -> Bool {
        var ok = true
        func check(_ cond: Bool, _ label: String) {
            print("\(cond ? "PASS" : "FAIL"): \(label)")
            if !cond { ok = false }
        }

        // ---- 1. transport error -> unreachableHost ----
        let transport: HSCCError = .transport(underlying: nil)
        let out1 = QRPairing.classify(transport)
        check(out1 == .unreachableHost, "1a transport -> unreachableHost")
        check(out1.title == "Can't reach that host", "1b unreachableHost has the headline title")

        // ---- 2. invalidURL -> unreachableHost ----
        let out2 = QRPairing.classify(HSCCError.invalidURL)
        check(out2 == .unreachableHost, "2a invalidURL -> unreachableHost")

        // ---- 3. unauthorized api error -> rejectedToken ----
        let unauthorized = HSCCError.api(code: "unauthorized", message: "nope", status: 401)
        check(QRPairing.classify(unauthorized) == .rejectedToken, "3a unauthorized -> rejectedToken")

        // ---- 4. invalid_token api error -> rejectedToken (same bucket) ----
        let invalidToken = HSCCError.api(code: "invalid_token", message: "bad", status: 401)
        check(QRPairing.classify(invalidToken) == .rejectedToken, "4a invalid_token -> rejectedToken")

        // ---- 5. non-token api error -> .other(message) ----
        let otherApi = HSCCError.api(code: "server_error", message: "boom", status: 500)
        let out5 = QRPairing.classify(otherApi)
        if case .other(let m) = out5 {
            check(m == "boom", "5a non-token api -> .other(raw message)")
        } else {
            check(false, "5a non-token api -> .other(raw message)")
        }
        check(out5.title == "Pairing failed", "5b other outcome headline is 'Pairing failed'")

        // ---- 6. decoding error -> .other(actionable, non-leaky) ----
        // Regression guard (t_b89d0b9a): a `.decoding` failure used to surface as
        // "Unexpected response from the cluster: <raw DecodingError>", leaking
        // internal symbols. It must now read as an actionable version-mismatch
        // hint and must NOT interpolate the raw detail.
        let dec = HSCCError.decoding("garbage-symbol-dump")
        let out6 = QRPairing.classify(dec)
        if case .other(let m) = out6 {
            check(m.contains("version mismatch"), "6a decoding -> .other(version-mismatch guidance)")
            check(!m.contains("garbage-symbol-dump") && !m.hasPrefix("Unexpected response from the cluster:"),
                  "6b decoding message does not leak raw detail")
        } else {
            check(false, "6a decoding -> .other(version-mismatch guidance)")
        }

        // ---- 7. non-HSCCError -> .other(localizedDescription) ----
        struct Plain: LocalizedError { var errorDescription: String? { "plain failure" } }
        let out7 = QRPairing.classify(Plain())
        if case .other(let m) = out7 {
            check(m == "plain failure", "7a non-HSCCError -> .other(localizedDescription)")
        } else {
            check(false, "7a non-HSCCError -> .other(localizedDescription)")
        }

        // ---- 8. success outcome message is the exact connect string ----
        let s = QRPairingOutcome.success(service: "HSCC", version: "3.2.1")
        check(s.title == "Paired", "8a success title is 'Paired'")
        check(s.message == "Connected to HSCC v3.2.1.", "8b success message matches Settings connect text")

        // ---- 9. The actionable guidance is present in failure messages ----
        let rejected = QRPairingOutcome.rejectedToken
        check(rejected.message.contains("generate a fresh setup code"), "9a rejectedToken message gives actionable guidance")
        let unreachable = QRPairingOutcome.unreachableHost
        check(unreachable.message.contains("Tailscale"), "9b unreachableHost message names Tailscale")

        // Summary line.
        print(ok ? "qr_classify_check: ALL PASS" : "qr_classify_check: FAILURES")
        return ok
    }
}

exit(Harness.run() ? 0 : 1)
