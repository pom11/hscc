//
//  DebugHarness.swift
//  HSCC
//
//  DEBUG-only capture harness for the Phase 2 screenshot set
//  (t_0d5632bc, operator brief 2026-10-15: "Don't wait on a human. Drive
//  navigation without taps").
//
//  WHY THIS EXISTS
//  ---------------
//  `ios-app/docs/design/PHASE2_PROGRESS_t_25aed701.md` proved the headless
//  capture path dead-ends on ONE modal: the first-run notification
//  authorization alert (`NotificationsAppDelegate` →
//  `NotificationCoordinator.requestAuthorization`) holds the key window, and
//  on this device `simctl privacy grant notifications` is specifically
//  refused ("Operation not permitted" — camera grant on the same device is
//  allowed), Simulator.app is not installed, and simctl has no tap/key input.
//  Rather than answer the alert, the harness makes the app not raise it and
//  drives navigation directly — no interruption to answer, so no tap needed.
//
//  WHAT IS DEBUG-ONLY (compiled out of Release; nothing here touches the
//  release path, and every hook is an early-return guard around EXISTING
//  behaviour, so shipping behaviour is byte-identical):
//
//    HSCC_DEBUG_SCREEN=<rawValue>   AppShell opens that AppDestination at
//                                   launch (unknown values fall back to the
//                                   normal default — never a crash, never a
//                                   blank screen, same rule as DeepLinkParser).
//    HSCC_DEBUG_FIXTURES=1          HSCCClient answers GETs from a canned
//                                   fixture folder instead of the network, so
//                                   the list recipe renders with REAL captured
//                                   shapes instead of "Not connected".
//
//  FIXTURE SOURCE — honesty note. Canned bodies are the repo's committed
//  model_decode_check fixtures (captured live from the daemon and kept
//  decode-current by scripts/model_decode_check). They are pushed into the
//  app container by ios-app/scripts/capture_screens.sh under the exact names
//  `fixtureFile(forPath:)` expects. Screens are therefore FIXTURE-FED
//  (real shapes, not live state); the work log says so explicitly and does
//  not present them as a connected fleet.
//
//  FIXTURE FILES are read from the app's Documents/HSCCFixtures directory —
//  NOT the app bundle — so no fixture content enters project.yml or the
//  Release bundle.
//

#if DEBUG

import Foundation

enum DebugHarness {

    /// True when this process was launched for headless capture.
    static var fixturesEnabled: Bool {
        ProcessInfo.processInfo.environment["HSCC_DEBUG_FIXTURES"] == "1"
    }

    /// The drawer destination a capture launch should open, nil for a normal
    /// launch or an unknown value (never a crash on a typo — same honesty rule
    /// as DeepLinkParser's refusal to guess).
    static func requestedScreen() -> AppDestination? {
        guard let raw = ProcessInfo.processInfo.environment["HSCC_DEBUG_SCREEN"],
              !raw.isEmpty else { return nil }
        return AppDestination(rawValue: raw)
    }

    // MARK: - Fixture-backed URLSession

    /// The canned-file directory (Documents/HSCCFixtures). Created empty by
    /// the OS; missing files are answered 404, never a crash.
    static var fixturesDirectory: URL? {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)
            .first?.appendingPathComponent("HSCCFixtures")
    }

    /// GET path → fixture file name. Exact matches first; the parameterised
    /// families (`/v1/cards/<id>`, `/v1/projects/<name>`, …) are prefix
    /// fallbacks that resolve to whatever fixture the repo carries.
    ///
    /// Unmapped paths get 501 from the protocol — a screen showing its
    /// "failed to load" row is honest; a fabricated body would not be.
    static func fixtureFile(forPath path: String) -> String? {
        let exact: [String: String] = [
            "/v1/ping": "v1_ping.json",
            "/v1/health": "v1_health.json",
            "/v1/verify": "v1_verify.json",
            "/v1/cluster/status": "v1_cluster_status.json",
            "/v1/cluster/hosts": "cluster_hosts.json",
            "/v1/cluster/monitor": "v1_cluster_monitor.json",
            "/v1/fleet/stats": "fleet_stats.json",
            "/v1/fleet/throughput": "fleet_throughput.json",
            "/v1/fleet/streams": "fleet_streams.json",
            "/v1/autoscale": "v1_autoscale.json",
            "/v1/cards": "cards.json",
            "/v1/kanban/blocked": "kanban_blocked.json",
            "/v1/kanban/stale": "kanban_stale.json",
            "/v1/activity/feed": "v1_activity_feed.json",
            "/v1/autodown/status": "autodown_status.json",
            "/v1/cron/list": "cron_list.json",
            "/v1/daemon/status": "daemon_status.json",
            "/v1/daemon/history": "v1_daemon_history.json",
            "/v1/standup": "v1_standup.json",
            "/v1/review/queue": "v1_review_queue.json",
            "/v1/qa/queue": "v1_qa_queue.json",
            "/v1/projects": "v1_projects.json",
            "/v1/commands": "commands.json",
            "/v1/profiles": "v1_profiles.json",
            "/v1/profiles/list": "profiles_list.json",
            "/v1/sessions": "v1_sessions.json",
            "/v1/triggers": "v1_triggers.json",
            "/v1/escalate": "v1_escalate.json",
            "/v1/logs": "logs_daemon.json",
            "/v1/memory": "memory_list.json",
            "/v1/template/list": "template_list.json",
            "/v1/template/status": "template_status.json",
        ]
        if let hit = exact[path] { return hit }
        // Parameterised families — the detail screens under capture.
        if path.hasPrefix("/v1/cards/") { return "card_detail_t_049d6986.json" }
        if path.hasPrefix("/v1/projects/hscc") { return "project_hscc.json" }
        if path.hasPrefix("/v1/review/") { return "review_detail.json" }
        return nil
    }

    /// A URLSession whose GETs are answered from `fixturesDirectory`. Built
    /// ONLY when `fixturesEnabled`; production always uses `.shared`.
    static func fixtureSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FixtureURLProtocol.self]
        config.timeoutIntervalForRequest = 10
        return URLSession(configuration: config)
    }

    /// Fake cluster for fixture mode. `fixtures.invalid` is a reserved-TLD
    /// hostname that resolves nowhere (RFC 6761), so even a protocol bug
    /// cannot send data to a real machine; the token is a literal
    /// placeholder, never a credential. Fixed UUID keeps re-launches
    /// idempotent (saveCluster updates in place instead of appending).
    static func seedFixtureCluster(in store: SettingsStore) {
        guard let id = UUID(uuidString: "deadbeef-0000-4000-8000-0000000000d1") else { return }
        let cluster = SavedCluster(id: id, name: "Fixtures (debug)",
                                   host: "fixtures.invalid", port: 8788)
        store.saveCluster(cluster, token: "debug-fixture-token")
    }
}

/// Answers GETs from the canned folder; everything else fails fast with 501
/// ("no fixture served for this path"), which surfaces as the view's honest
/// failed-row rather than fake content.
final class FixtureURLProtocol: URLProtocol {

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url?.path ?? ""
        guard request.httpMethod == "GET" || request.httpMethod == nil else {
            finish(status: 501, body: Data("{\"speak\":\"debug fixture mode: mutations are not served\"}".utf8))
            return
        }
        guard let name = DebugHarness.fixtureFile(forPath: path),
              let dir = DebugHarness.fixturesDirectory,
              let data = try? Data(contentsOf: dir.appendingPathComponent(name)) else {
            finish(status: 501,
                   body: Data("{\"speak\":\"debug fixture mode: no fixture served for \(path)\"}".utf8))
            return
        }
        finish(status: 200, body: data)
    }

    override func stopLoading() {}

    private func finish(status: Int, body: Data) {
        let response = HTTPURLResponse(
            url: request.url ?? URL(string: "http://fixtures.invalid")!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

#endif
