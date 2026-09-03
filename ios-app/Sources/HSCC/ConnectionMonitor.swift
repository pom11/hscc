import Foundation
import Combine

/// Shared observable tracking the outcome of the MOST RECENT real API request.
///
/// This exists to fix a stale-banner bug (t_4889e978): `ContentView` used to
/// run a ONE-SHOT `client.ping()` at launch and pin the banner to that result
/// for the whole session. A single failed probe during a cold start — before
/// Tailscale/wifi settles — left a red "Can't reach the cluster" banner on
/// screen while every subsequent real request succeeded.
///
/// Instead of a launch probe, every completed request through `HSCCClient`
/// reports into this shared monitor:
///   * any completion that PROVES we reached the API (a 2xx response, or any
///     HTTP response at all, success or not) -> `.reachable` (clears the alarm);
///   * a TRANSPORT failure (URLSession threw — connection refused, DNS,
///     timeout) -> `.unreachable(...)` (sets the alarm);
///   * a request that is merely IN FLIGHT is never reported, so the banner is
///     never shown red while a request is underway.
///
/// A screenful of freshly-loaded data can therefore never sit under a
/// "can't reach" banner — the very act of loading that data sets the status.
///
/// This is a singleton because a new `HSCCClient` is built per-request and per
/// view within a session; the monitor must aggregate across all of them onto
/// one machine-readable truth the root view reads.
final class ConnectionMonitor: ObservableObject {
    /// The aggregated reachability status as of the most recent completed API
    /// request. One type, cleanly ordered so a view can switch on it.
    enum Status: Equatable {
        /// No real API request has completed yet this session. Keep whatever
        /// the view shows neutral; do not alarm.
        case unknown
        /// The most recent completed request proved the API is reachable.
        case reachable
        /// The most recent completed request failed at the transport layer —
        /// we never got an HTTP response, so the cluster genuinely appears
        /// unreachable. Carries a human-readable reason for the banner.
        case unreachable(String)
    }

    static let shared = ConnectionMonitor()

    /// The current status. Views bind to this and redraw when it changes.
    @Published private(set) var status: Status = .unknown

    private init() {}

    /// Report a request that completed successfully (or reached the API with an
    /// HTTP response). Clears any unreachable alarm: a successful real request
    /// is the strongest possible signal the cluster is reachable.
    ///
    /// This is intentionally called for ANY HTTP response — 2xx AND non-2xx
    /// (401, 409, 5xx) — because receiving an HTTP response at all proves the
    /// API is reachable; a 409-confirm or a 401-token rejection is not a
    /// "can't reach the cluster" situation, and the banner must not lie.
    func requestSucceeded() {
        status = .reachable
    }

    /// Report a TRANSPORT failure — the request never received an HTTP
    /// response (connection refused, DNS failure, timeout). This is the only
    /// condition that may raise the "can't reach the cluster" alarm.
    func requestFailed() {
        status = .unreachable("Can't reach the cluster — is Tailscale connected?")
    }

    /// Reset to the idle/unknown state. Used when settings change so the banner
    /// does not keep showing the outcome of a request made against a different
    /// cluster.
    func reset() {
        status = .unknown
    }
}
