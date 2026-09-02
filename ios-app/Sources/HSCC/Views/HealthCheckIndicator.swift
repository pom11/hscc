import SwiftUI

/// Rendering helpers for `HealthCheck.ok` (the server's documented tri-state:
/// `true` = pass, `false` = hard fail, `nil` = could not be verified).
///
/// A `nil` check must NOT be conflated with a red fail — the server explicitly
/// reserves `ok: null` for "not a pass, not a hard fail" (hscc_daemon/verify.py).
/// Showing unverified as neutral means a genuinely-unavailable check renders
/// distinctly from both green (pass) and red (fail), and never trains the
/// operator to ignore reds.
enum HealthCheckIndicator {
    static func icon(_ ok: Bool?) -> String {
        switch ok {
        case true:  return "checkmark.circle.fill"
        case false: return "xmark.circle.fill"
        case nil:   return "questionmark.circle.fill"
        }
    }

    static func tint(_ ok: Bool?) -> Color {
        switch ok {
        case true:  return Theme.Semantic.ok
        case false: return Theme.Semantic.bad
        case nil:   return Theme.Semantic.neutral
        }
    }
}
