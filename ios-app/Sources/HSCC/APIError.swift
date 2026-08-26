import Foundation

/// A typed error surfaced by the HSCC API client.
///
/// This mirrors the API's unified error contract (docs/DESIGN-api.md §C): every
/// error response is a JSON object with shape
///   { "error": { "code": "...", "message": "...", "speak": "..." } }
/// plus a corresponding HTTP status code. The client decodes that shape and
/// folds it into a `HSCCError` so callers can match on `code` or read a
/// human-readable `message`.
enum HSCCError: Error, Equatable {
    /// The request reached the server but it returned a structured API error.
    /// `code` is the machine-readable slug (e.g. "unauthorized"), `message` is
    /// the human sentence, `status` is the HTTP status code.
    case api(code: String, message: String, status: Int)

    /// The response was not valid JSON / did not match an expected shape.
    case decoding(String)

    /// No endpoint reached: DNS failure, connection refused, timeout, etc.
    /// The `underlying` error is attached for diagnostics but never contains
    /// the token.
    case transport(underlying: Error?)

    /// A URL could not be constructed from the configured host + port.
    case invalidURL

    var localizedDescription: String {
        switch self {
        case .api(let code, let message, _):
            if code == "unauthorized" {
                return "Not authorized — check your token."
            }
            return message
        case .decoding(let detail):
            return "Unexpected response from the cluster: \(detail)"
        case .transport:
            return "Can't reach the cluster — is Tailscale connected?"
        case .invalidURL:
            return "The host or port is invalid."
        }
    }

    /// Equatable is written by hand because `.transport` carries an
    /// `Error?`, and `Error` is not `Equatable` — so the compiler cannot
    /// synthesise conformance. Two transport failures are treated as equal
    /// when their underlying descriptions match (nil == nil), which is what
    /// callers actually care about: "same kind of unreachable", not object
    /// identity.
    static func == (lhs: HSCCError, rhs: HSCCError) -> Bool {
        switch (lhs, rhs) {
        case let (.api(lc, lm, ls), .api(rc, rm, rs)):
            return lc == rc && lm == rm && ls == rs
        case let (.decoding(l), .decoding(r)):
            return l == r
        case let (.transport(l), .transport(r)):
            return String(describing: l) == String(describing: r)
        case (.invalidURL, .invalidURL):
            return true
        default:
            return false
        }
    }
}
