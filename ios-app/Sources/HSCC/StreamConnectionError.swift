import Foundation

// ===========================================================================
// StreamConnectionError — classify a WebSocket connection error so the store
// can tell a PERMANENT server rejection (e.g. a rotated/rejected token that
// returns HTTP 401 before the WS upgrade) from a TRANSIENT network failure
// (connection refused, DNS, timeout, mid-stream drop). Audit t_ec570637.
//
// WHY this exists: the app runs over a Tailscale tailnet from a phone, so the
// connection WILL drop, sleep and change networks. A dropped socket is
// retryable — reconnect with backoff. But a server that RESPONDS to the WS
// upgrade with a non-101 (most often a rejected `Authorization: Bearer`
// token) will NEVER accept a retry: reconnecting hammers the server and leaves
// the operator watching a transcript that is silently not live. The client
// must distinguish the two and tell the operator about the permanent one.
//
// HOW the classification works (proven by scripts/reconnect_check.sh and a
// live URLSession experiment on this host): a WebSocket upgrade that the
// server rejects surfaces to `URLSessionWebSocketTask.receive` as
// `NSURLErrorDomain` `NSURLErrorBadServerResponse` (-1011) — the server sent a
// valid HTTP response that was not a 101 upgrade. A transport-level failure
// (unreachable/refused = -1004, timeout = -1001, host = -1003, mid-stream
// drop) is any AF net error. Everything in `NSURLErrorDomain` that is NOT
// `.badServerResponse` is transient and retryable.
//
// Pure Foundation — no network, no UI — so reconnect_check.sh compiles THIS
// real source into a macOS CLI and asserts the classification (the same
// "compile the real source headless" pattern as SessionStreamCursor).
// ===========================================================================

/// The two failure classes a WS connection can hit (an analogue of the REST
/// `HSCCError.transport` vs `HSCCError.api` distinction, for the socket path).
enum StreamErrorKind: Equatable {
    /// The server RESPONDED but rejected the upgrade with a non-101 HTTP
    /// response — most often a rotated/rejected token returning 401 before the
    /// WS handshake. Reconnect can never succeed; surface it and stop.
    case rejected
    /// The transport failed (connection refused, DNS, timeout, mid-stream
    /// drop). The server may be unreachable or a socket was torn down — a
    /// retry with backoff can legitimately succeed later. Retry.
    case transient
}

/// Classify a `URLSessionWebSocketTask` connection/receive error.
///
/// - Returns: `.rejected` when the server answered the upgrade with an
///   unacceptable HTTP response (NSURLErrorBadServerResponse, -1011) — the
///   server is UP but refused the connection, so retrying is futile and the
///   operator should be told (usually: the token rotated). `.transient`
///   otherwise (network unreachable, dropped socket, timeout).
func classifyStreamError(_ error: Error) -> StreamErrorKind {
    let ns = error as NSError
    // NSURLErrorBadServerResponse (-1011): the server sent an HTTP response
    // for the WS upgrade that was NOT a 101 Switching Protocols. For an HSCC
    // upgrade this is the auth-rejection path (the API answers a rejected
    // `Authorization` with HTTP 401 before the handshake). The server is
    // reachable and clearly refuses this connection — retries will not help.
    if ns.domain == NSURLErrorDomain, ns.code == NSURLErrorBadServerResponse {
        return .rejected
    }
    // Anything else in NSURLErrorDomain is a transport-level failure
    // (refused/timeout/DNS/drop) — transient and worth retrying.
    return .transient
}
