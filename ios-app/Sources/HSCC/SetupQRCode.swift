import Foundation

/// The decoded content of the setup QR code printed by `hscc api status` /
/// `hscc api start`.
///
/// Payload contract (single-line UTF-8 JSON, fixed by the backend card — this
/// struct decodes EXACTLY that shape, nothing more):
///
///     {"v":1,"host":"<host>","port":<int>,"token":"<token>"}
///
/// Decoding rules (each is a HARD requirement — a bad payload is rejected in
/// full, never partially applied; the caller only ever reads a fully-validated
/// instance):
///   * `v` must be exactly 1. Anything else is rejected with an error that
///     NAMES the version the payload declared, so the operator knows the code
///     targets a different API version than this app supports.
///   * `port` is decoded as an INT, not a string (per the contract).
///   * `host`, `token` must be present and non-empty; `port` must be a valid
///     TCP port.
struct SetupQRCode: Decodable, Equatable {
    let version: Int
    let host: String
    let port: Int
    let token: String

    private enum CodingKeys: String, CodingKey {
        case version = "v"
        case host
        case port
        case token
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(Int.self, forKey: .version)
        host = try c.decode(String.self, forKey: .host)
        // INT per the contract — a quoted string here throws (rightly).
        port = try c.decode(Int.self, forKey: .port)
        token = try c.decode(String.self, forKey: .token)
    }

    /// Decode a scanned string into a validated setup code.
    ///
    /// Collects the underlying decode failure into a user-facing message so a
    /// wrong-shaped payload gives the operator a reason, not a silent nothing.
    /// - Throws: `SetupQRCodeError` (invalid shape or unsupported version).
    static func decode(_ string: String) throws -> SetupQRCode {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        let code: SetupQRCode
        do {
            code = try JSONDecoder().decode(SetupQRCode.self, from: Data(trimmed.utf8))
        } catch {
            throw SetupQRCodeError.invalidPayload(String(describing: error))
        }
        guard code.version == 1 else {
            throw SetupQRCodeError.unsupportedVersion(code.version)
        }
        // The shape decoded, but the VALUES must be usable — never apply a
        // host/port/token set that can't work. Reject as a whole.
        guard !code.host.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              (1...65535).contains(code.port),
              !code.token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw SetupQRCodeError.invalidPayload("host, port, and token must all be non-empty, and port must be a valid TCP port (1–65535)")
        }
        return code
    }
}

/// Why a scanned setup QR was rejected.
enum SetupQRCodeError: LocalizedError, Equatable {
    /// The payload was not the expected JSON shape / failed to decode.
    case invalidPayload(String)
    /// The payload declared an API version other than 1.
    case unsupportedVersion(Int)

    var errorDescription: String? {
        switch self {
        case .invalidPayload(let detail):
            return "That isn't a valid HSCC setup code — \(detail)"
        case .unsupportedVersion(let v):
            return "This code is for HSCC API version \(v); this app supports version 1. Pair with an HSCC setup that prints a v1 code."
        }
    }
}

/// The outcome of a completed QR pairing attempt, classified so the UI can tell
/// a stranger EXACTLY why a scan failed.
///
/// `SetupQRCode.decode` already rejects bad-shape and wrong-version payloads
/// before anything is applied. This type classifies the CONNECTION attempt made
/// after a valid code has been applied — turning the three real failure modes
/// from the onboarding card into distinct, actionable reasons:
///   1. **Wrong version** — the code targets an HSCC API version this app
///      doesn't speak.
///   2. **Unreachable host** — nothing answered at the scanned host:port (DNS,
///      connection refused, timeout): the host is wrong, or Tailscale isn't
///      connected on this phone.
///   3. **Rejected token** — the cluster answered but returned 401: the code's
///      token was revoked / is wrong.
/// Together with a catch-all for a cluster that answered but reported a
/// problem, so no failure is ever a silent nothing.
enum QRPairingOutcome: Equatable {
    /// Paired — the cluster answered ping ok.
    case success(service: String?, version: String?)
    /// The scanned code decodes but targets a different HSCC API version (the
    /// decode check re-states this; kept here so the connection layer never has
    /// to reach into the decode error).
    case unsupportedVersion(Int)
    /// The scanned string wasn't a valid HSCC setup code (decode failure).
    case invalidPayload(String)
    /// Couldn't reach the cluster at the scanned host:port.
    case unreachableHost
    /// Reached the cluster but it rejected the scanned token.
    case rejectedToken
    /// Reached the cluster but it reported a problem / a non-token API error.
    case other(String)

    /// A short headline for the outcome, shown as the pairing-result title.
    var title: String {
        switch self {
        case .success: return "Paired"
        case .unsupportedVersion: return "Wrong app version"
        case .invalidPayload: return "Not a setup code"
        case .unreachableHost: return "Can't reach that host"
        case .rejectedToken: return "Token rejected"
        case .other: return "Pairing failed"
        }
    }

    /// A human explanation of what went wrong and what the stranger should do.
    var message: String {
        switch self {
        case .success(let service, let version):
            if let service, let version {
                return "Connected to \(service) v\(version)."
            }
            return "Connected to your cluster."
        case .unsupportedVersion(let v):
            return "The scanned code is for HSCC API version \(v), but this app only speaks version 1. Pair with a cluster that prints a v1 setup code."
        case .invalidPayload(let detail):
            return "That isn't a valid HSCC setup code. \(detail)"
        case .unreachableHost:
            return "Nothing answered at the scanned host and port. Check that the host is right and that Tailscale is connected on this phone (and to the cluster's tailnet), then try again."
        case .rejectedToken:
            return "The cluster reached, but it rejected the token in the code. The token may be revoked or wrong — generate a fresh setup code on the cluster and scan it again."
        case .other(let message):
            return message
        }
    }
}

/// Runs the connection side of a QR pairing: ping the cluster at the given
/// host:port with the given token and classify the result into a
/// `QRPairingOutcome` so the user is told WHY a scan failed.
///
/// This is the shared step both the onboarding screen and Settings use after a
/// confirmed scan. It does NOT mutate settings — the caller applies the values
/// (the confirm-gate contract) and then calls this to learn the outcome.
enum QRPairing {
    @MainActor
    static func test(host: String, port: Int, token: String) async -> QRPairingOutcome {
        let client = HSCCClient(host: host, port: port, token: token)
        do {
            let pong = try await client.ping()
            if pong.ok {
                return .success(service: pong.service, version: pong.version)
            }
            return .other("Reached the API, but it reported not-ok.")
        } catch {
            return classify(error)
        }
    }

    /// Map a thrown `HSCCError` to the pairing outcome with an actionable
    /// reason. Pure and isolated so it is easy to reason about — everything a
    /// scan can fail with lands in exactly one bucket.
    static func classify(_ error: Error) -> QRPairingOutcome {
        guard let hscc = error as? HSCCError else {
            return .other(error.localizedDescription)
        }
        switch hscc {
        case .transport:
            return .unreachableHost
        case .api(let code, _, _) where code == "unauthorized" || code == "invalid_token":
            return .rejectedToken
        case .api(_, let message, _):
            return .other(message)
        case .invalidURL:
            return .unreachableHost
        case .decoding(let detail):
            return .other("Unexpected response from the cluster: \(detail)")
        }
    }
}
