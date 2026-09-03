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
        case .decoding:
            // We deliberately do NOT interpolate the raw `detail` here. The
            // detail is often a `String(describing:)` of Swift's `DecodingError`
            // (e.g. `keyNotFound(CodingKeys(...))`) or an internal field name
            // (`missing speak field`) — internal symbols a stranger can't act
            // on. A decode failure of a 2xx body almost always means the app and
            // cluster disagree on the API schema; say that and what to do.
            return "The cluster returned something this app can't read — likely an app/cluster version mismatch. Update the app (or the cluster) so they match."
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

/// Turn any thrown `Error` into an operator-actionable message.
///
/// This is the single shared "what went wrong + what to do" translator for the
/// app's error surfaces. `HSCCError` keeps its own specific `localizedDescription`
/// (that path already says what happened and what to do next). A NON-`HSCCError`
/// is an unexpected internal failure that escaped the typed client path — a
/// coding error, a data race, a decode outside the API layer. The operator still
/// needs an honest, actionable message, never a vague dead-end like the old
/// "Something went wrong." So every view that used to copy-paste
/// `(error as? HSCCError)?.localizedDescription ?? "Something went wrong."`
/// now calls this one function.
func operatorErrorMessage(_ error: Error?) -> String {
    if let e = error as? HSCCError { return e.localizedDescription }
    return "Something unexpected went wrong on this screen. Try again — if it keeps failing, restart the app."
}
