import Foundation
import Combine

/// Shared observable tracking the outcome of the MOST RECENT real API request,
/// plus the cheap probe that RESOLVES the state when no request would ever come.
///
/// This exists to fix a stale-banner bug (t_4889e978): `ContentView` used to
/// run a ONE-SHOT `client.ping()` at launch and pin the banner to that result
/// for the whole session. A single failed probe during a cold start — before
/// Tailscale/wifi settles — left a red "Can't reach the cluster" banner on
/// screen while every subsequent real request succeeded.
///
/// So passivity became the rule: every completed request through `HSCCClient`
/// reports into this shared monitor:
///   * any completion that PROVES we reached the API (a 2xx response, or any
///     HTTP response at all, success or not) -> `.reachable` (clears the alarm);
///   * a TRANSPORT failure (URLSession threw — connection refused, DNS,
///     timeout) -> `.unreachable(...)` (sets the alarm);
///   * a request that is merely IN FLIGHT is never reported, so the banner is
///     never shown red while a request is underway.
///
/// But passivity alone caused the NEXT bug (t_e5d24c6d, operator field report
/// v2.9.0): `status` starts `.unknown` and the drawer/Home made no request of
/// their own, so the pill sat on "checking…" FOREVER with the API up. The old
/// launch ping had been deleted because a one-shot cold-start FAILURE pinned
/// the banner — that reasoning licenses no permanent failure pin, it does not
/// license an eternal `.unknown`.
///
/// THE STATE MACHINE (operator spec, 2026-10-15):
///   * Passive reporting is unchanged: every completed `HSCCClient` request
///     still reports here; `requestSucceeded()` remains the strongest signal.
///   * ACTIVE OBSERVATION: while the Sidebar header is VISIBLE and the app is
///     configured, a `.task` there runs `ConnectionProbe` — one cheap authed
///     GET `/v1/health` immediately, then every 15 s (see `ConnectionProbe`
///     below and `Sidebar.clusterHeader` for the attachment). Task
///     cancellation on disappear ends the loop: zero traffic while the
///     drawer is closed.
///   * The FIRST real result — probe or request — FLIPS `.unknown`. That is
///     the whole field bug: nothing else ever resolved it.
///   * It keeps probing at the same cadence while `.reachable` too: the dot
///     stays honest ("most recent outcome") while it is being looked at, and
///     one small GET per 15 s while a panel is open is a price worth paying
///     for that. A failed probe therefore CAN set red even after successes —
///     consistent with the passive rule (the most recent real outcome wins).
///   * A FAILED probe NEVER PINS anything — the t_4889e978 contract survives:
///     the visible loop retries on the same cadence, any later real success
///     (request path OR probe) flips back via `requestSucceeded()`, and a
///     cluster switch calls `reset()` and re-observes the new cluster.
///   * `!isConfigured` means NO network: first-run stays "not configured"
///     (the loop is gated on `settings.isConfigured`).
///
/// A screenful of freshly-loaded data can therefore never sit under a
/// "can't reach" banner — the very act of loading that data sets the status —
/// and a screen that loads no data is no longer left guessing forever.
///
/// This is a singleton because a new `HSCCClient` is built per-request and per
/// view within a session; the monitor must aggregate across all of them onto
/// one machine-readable truth the root view reads.
final class ConnectionMonitor: ObservableObject {
    /// The aggregated reachability status as of the most recent completed API
    /// request (real request or probe — both are real requests). One type,
    /// cleanly ordered so a view can switch on it.
    enum Status: Equatable {
        /// No real API request has completed yet this session (or since the
        /// last `reset()`). Keep whatever the view shows neutral; do not alarm.
        /// The visible-drawer probe exists precisely so screens that make no
        /// request of their own do not sit here forever.
        case unknown
        /// The most recent completed request proved the API is reachable.
        case reachable
        /// The most recent completed request never produced a usable HTTP
        /// answer: a transport failure, or (probe only) the server answering
        /// and REFUSING our credential. Carries a human-readable reason for
        /// the banner — the reason must name the fixable thing.
        case unreachable(String)
    }

    static let shared = ConnectionMonitor()

    /// The current status. Views bind to this and redraw when it changes.
    @Published private(set) var status: Status = .unknown

    /// The reason string for "we never got an HTTP answer". One constant so
    /// the passive path and the probe path can never drift apart (the banner
    /// text is asserted on this exact wording by connection_banner_check).
    static let transportReason = "Can't reach the cluster — is Tailscale connected?"

    /// The reason string for "the server answered but refused this token".
    /// Distinct from `transportReason` on purpose: telling someone to check
    /// Tailscale when the token is wrong is a dead end (Settings is the fix).
    static let authRefusedReason = "The cluster refused this token — check it in Settings."

    private init() {}

    /// Report a request that completed successfully (or reached the API with an
    /// HTTP response). Clears any unreachable alarm: a successful real request
    /// is the strongest possible signal the cluster is reachable.
    ///
    /// This is intentionally called for ANY HTTP response — 2xx AND non-2xx
    /// (401, 409, 5xx) — because receiving an HTTP response at all proves the
    /// API is reachable; a 409-confirm or a 401-token rejection is not a
    /// "can't reach the cluster" situation, and the banner must not lie.
    ///
    /// (The PROBE path deviates from this for 401/403 by operator ruling — a
    /// probe whose only job is to check the credential must report a refused
    /// credential honestly; see `ConnectionProbe.classify`. The no-pin
    /// guarantee is what actually matters and holds on both paths: any later
    /// success clears the alarm.)
    func requestSucceeded() {
        status = .reachable
    }

    /// Report a TRANSPORT failure — the request never received an HTTP
    /// response (connection refused, DNS failure, timeout). This is the only
    /// condition the passive request path may raise the "can't reach the
    /// cluster" alarm from.
    func requestFailed() {
        status = .unreachable(Self.transportReason)
    }

    /// Report that the server ANSWERED and refused our credential (the probe's
    /// honest third failure mode — see `ConnectionProbe.classify`). A refused
    /// token is a real "this connection does not work" state, but the reason
    /// names the token, not the network. Never a pin: `requestSucceeded()` —
    /// from a repaired token or any other real success — clears it.
    func authRefused() {
        status = .unreachable(Self.authRefusedReason)
    }

    /// Reset to the idle/unknown state. Used when settings change so the banner
    /// does not keep showing the outcome of a request made against a different
    /// cluster. The drawer's probe loop restarts on the same
    /// `connectionIdentity` change, so the new cluster gets observed too.
    func reset() {
        status = .unknown
    }
}

// MARK: - The cheap reachability probe

/// The active half of the monitor: one cheap authed GET that resolves
/// `.unknown` (and keeps the dot honest) while the drawer header is visible.
///
/// t_e5d24c6d: the pill read "checking…" forever because NOTHING probed while
/// status was `.unknown` and the drawer makes no request of its own. This is
/// not the old launch ping: it is not one-shot (it retries while visible), it
/// does not run at cold start with no surface looking at it, and a failed
/// result never pins the banner — every contract above still holds.
///
/// The endpoint is `/v1/health` — the lightest confirmed authed route
/// (`hscc-api/routes_cluster.py handle_health`). Never invent another.
///
/// Everything here is `static` and the probe call is INJECTED, so the whole
/// decision tree compiles and runs under the hermetic macOS gate
/// (scripts/connection_probe_check.sh) — no socket, no fixtures at runtime,
/// no iOS UI. The app-side loop lives in `Sidebar.clusterHeader` (`.task`),
/// because only a view knows when the header is on screen.
enum ConnectionProbe {
    /// The probed path. Pinned by the gate: a probe that drifts to a heavier
    /// or invented endpoint defeats its own purpose.
    static let probePath = "/v1/health"

    /// What one probe proved. `Equatable` so the gate can assert it directly.
    enum Report: Equatable {
        /// An HTTP response arrived (any status) — the cluster is reachable.
        case reachable
        /// The server answered and refused the credential (401/403 or the
        /// unified `unauthorized` slug). Honest three-state: surfaced as
        /// `.unreachable`, with `authRefusedReason` naming the token.
        case authRefused
        /// No HTTP response at all — transport failure (or no request could
        /// even be built). Surfaced as `.unreachable`.
        case transportFailure
    }

    /// Classify one probe outcome WITHOUT applying it (pure, gate-testable).
    ///
    /// `status` is the HTTP status code, `nil` when no HTTP response was ever
    /// received. The monitor's long-standing rule: ANY HTTP response proves
    /// reachability (a 500 or 404 is the server talking). The one exception is
    /// operator-ruled (2026-10-15): a probe whose job is checking the
    /// connection must report a REFUSED credential as not-working, so 401/403 —
    /// and the API's `unauthorized` machine slug even under a different status —
    /// classify as refusal rather than reachability.
    static func classify(status: Int?, code: String?) -> Report {
        guard let status else {
            // Never got an HTTP answer at all — nothing was proven reachable.
            return .transportFailure
        }
        if status == 401 || status == 403 || code == "unauthorized" {
            return .authRefused
        }
        return .reachable
    }

    /// Apply a classification to the monitor — the single funnel both the app
    /// loop and the gate use, so the gate can never drift from app behaviour.
    static func apply(_ report: Report, to monitor: ConnectionMonitor) {
        switch report {
        case .reachable:        monitor.requestSucceeded()
        case .authRefused:      monitor.authRefused()
        case .transportFailure: monitor.requestFailed()
        }
    }

    /// Run ONE probe step with an injected call (in the app: `client.health()`;
    /// in the gate: a closure throwing the exact `HSCCError`s the client
    /// throws), classify HONESTLY, apply it to `monitor`, and return the report
    /// so the caller can pace itself (back off on refusal; the visible loop
    /// retries on cadence regardless — no pin, ever).
    @discardableResult
    static func runReachabilityProbe(on monitor: ConnectionMonitor,
                                     probe: () async throws -> Void) async -> Report {
        let report: Report
        do {
            try await probe()
            report = .reachable
        } catch let error as HSCCError {
            switch error {
            case .api(let code, _, let status):
                report = classify(status: status, code: code)
            case .decoding:
                // A 2xx whose body we can't read STILL proved an HTTP answer
                // arrived — reachable per the any-response contract. (The
                // app/cluster version mismatch is the view's problem to show.)
                report = .reachable
            case .transport, .invalidURL:
                // Nothing ever answered (or nothing could be asked).
                report = .transportFailure
            }
        } catch {
            // Unknown error kind: no HTTP answer was proven — be conservative.
            report = .transportFailure
        }
        apply(report, to: monitor)
        return report
    }
}
