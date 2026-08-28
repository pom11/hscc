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
              !code.token.isEmpty else {
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
