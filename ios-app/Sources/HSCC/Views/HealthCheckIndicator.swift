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

/// The one render of a `checks` array (spec §5: "the byte-twin
/// `verifyBody`/`healthBody` collapse to one `HealthCheckList` used by Fleet
/// and Ops"). Emits one row per check — icon + name over the detail line — so
/// the two screens cannot drift apart again.
///
/// Inside a `List`/`Form` this drops rows straight into the enclosing
/// container, so each check is a real list row with List's separators.
struct HealthCheckList: View {
    let checks: [HealthCheck]
    /// Shown when the server reported no checks at all. Each caller names the
    /// surface in its own words (the verify suite vs the health endpoint).
    let emptyMessage: String

    init(_ checks: [HealthCheck], emptyMessage: String = "No checks reported.") {
        self.checks = checks
        self.emptyMessage = emptyMessage
    }

    var body: some View {
        if checks.isEmpty {
            HSEmpty("No checks", message: emptyMessage, systemImage: "checkmark.seal")
        } else {
            ForEach(checks) { check in
                HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: HealthCheckIndicator.icon(check.ok))
                        .foregroundColor(HealthCheckIndicator.tint(check.ok))
                    VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                        Text(check.name)
                            .font(.body)
                        if let detail = check.detail, !detail.isEmpty {
                            Text(detail)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                    Spacer(minLength: 0)
                }
                // One VoiceOver element per check (spec §9).
                .accessibilityElement(children: .combine)
            }
        }
    }
}
