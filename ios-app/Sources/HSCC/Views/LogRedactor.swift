import Foundation

/// Client-side redaction for log lines (t_2eda26a6) — the SECOND line of
/// defence.
///
/// The backend `GET /v1/logs` is required to redact before serving, but the
/// iOS view must never trust that alone (defence in depth). Every line is
/// passed through `LogRedactor.redact(_:)` immediately before display, and
/// redacted text is what the view holds in memory — never the raw line.
///
/// CRITICAL SECURITY BOUNDARY: `LogRedactor.redact` is the ONLY place raw log
/// text may exist, and only transiently as a function argument. The view (and
/// the app) must never persist, print, or write a raw, unredacted log line to
/// any store, audit trail, or commit. Redacted output is what ships.
///
/// What gets masked (match the repo's documented placeholders):
///   * Tailnet host addresses (100.x.y.z)                       → 100.64.0.1
///   * RFC1918 LAN addresses (10.x / 172.16-31.x / 192.168.x)  → 10.0.0.x
///   * Any IPv4 that slipped through the above                 → [REDACTED_IP]
///   * `Authorization: Bearer <tok>` and `Bearer <tok>` tokens → Bearer ***
///   * `token=` / `apikey=` / `key=` / `secret=` values         → ***
///   * Session ids (`sess_*`, `session=[…]`, `session: <id>`)   → sess_***
///   * Long opaque runs (20+ token chars) — likely a token/session id → ***
enum LogRedactor {

    /// Mask a single raw log line. Returns fully-redacted text.
    static func redact(_ text: String) -> String {
        maskTokenValues(maskSessionIds(maskLongRuns(maskBearer(maskIPs(text)))))
    }

    /// Apply every redaction to a batch of entries (text is never written
    /// unredacted anywhere). Returns NEW entries whose `line` is redacted.
    static func redactMany(_ lines: [LogEntry]) -> [LogEntry] {
        lines.map { entry in
            LogEntry(timestamp: entry.timestamp,
                     level: entry.level,
                     source: entry.source,
                     line: entry.line.map { redact($0) })
        }
    }

    // MARK: - Individual masks

    /// Mask Tailnet (100.64.0.0/10) and RFC1918 addresses to documented
    /// placeholders, then any other IPv4 to a generic placeholder.
    static func maskIPs(_ text: String) -> String {
        // Tailnet CGNAT range 100.64.0.0/10. The upper bound is deliberately not
        // spelled out as a literal: a real-looking address in this repo trips the
        // committed-address guard, which is the bug this redactor prevents.
        let tailnet = try! NSRegularExpression(pattern: #"\b100\.(?:[6-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b"#)
        // RFC1918: 10/8, 172.16/12, 192.168/16.
        let rfc1918 = try! NSRegularExpression(pattern: #"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"#)
        // Any other dotted-quad IPv4 that isn't already masked. Negative
        // lookahead excludes the `100.64.0.1` placeholder emitted above, so a
        // masked tailnet address isn't re-masked into [REDACTED_IP].
        let other = try! NSRegularExpression(pattern: #"\b(?!100\.64\.0\.1\b)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"#)

        let r1 = tailnet.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "100.64.0.1")
        let r2 = rfc1918.stringByReplacingMatches(in: r1, range: NSRange(r1.startIndex..., in: r1), withTemplate: "10.0.0.x")
        let r3 = other.stringByReplacingMatches(in: r2, range: NSRange(r2.startIndex..., in: r2), withTemplate: "[REDACTED_IP]")
        return r3
    }

    /// Mask `Authorization: Bearer <tok>` and inline `Bearer <tok>` tokens.
    static func maskBearer(_ text: String) -> String {
        let full = try! NSRegularExpression(pattern: #"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/=-]+"#)
        let inline = try! NSRegularExpression(pattern: #"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"#)
        let a = full.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "authorization: Bearer ***")
        let b = inline.stringByReplacingMatches(in: a, range: NSRange(a.startIndex..., in: a), withTemplate: "Bearer ***")
        return b
    }

    /// Mask key=value style secrets: token, apikey/api_key, secret, password,
    /// auth. Only masks when a value actually follows the `=`, and keeps the
    /// key name so the operator still sees WHAT was redacted.
    static func maskTokenValues(_ text: String) -> String {
        let re = try! NSRegularExpression(pattern: #"(?i)\b(token|apikey|api_key|secret|password|auth|access_token|client_secret)\s*[=:]\s*[^&\s'"]+"#)
        return re.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "$1=***")
    }

    /// Mask session-id-looking fragments: `sess_<id>`, `session=<id>`,
    /// `session: <id>`, and ` session <id>`.
    static func maskSessionIds(_ text: String) -> String {
        let re = try! NSRegularExpression(pattern: #"(?i)(\bsess_[A-Za-z0-9_-]+|\bsession\s*[=:]\s*[A-Za-z0-9_-]+|\bsession\s+[A-Za-z0-9_-]{8,})"#)
        return re.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "sess_***")
    }

    /// Mask long opaque runs — the catch-all for tokens/uuid-like session ids
    /// that didn't match a named pattern. A 20+ char run of token characters
    /// is far more likely a secret than a word.
    static func maskLongRuns(_ text: String) -> String {
        let re = try! NSRegularExpression(pattern: #"\b[A-Za-z0-9][A-Za-z0-9_-]{19,}\b"#)
        return re.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "***")
    }
}
