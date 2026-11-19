import Foundation

// ===========================================================================
// model_decode_check/main.swift — DECODE the committed live fixtures against the
// REAL model sources (the harness; see ThemeStub.swift for the one non-model
// shim, and model_decode_check.sh for the build+run wrapper).
//
// Compiled and run by scripts/model_decode_check.sh, which first compiles the
// actual model files from the repo:
//   Sources/HSCC/Models.swift
//   Sources/Shared/SharedModels.swift
//   Sources/HSCC/APIError.swift
// and NEVER redeclares a model here. The Decodable types used below therefore
// resolve to the real structs, so a decode mismatch means the real Models.swift
// no longer matches the live JSON — exactly the false-green a mirror cannot
// catch.
// ===========================================================================

final class DecodeCheck {
    let dir: String
    var failures: [(fileName: String, type: String, error: String)] = []
    var passed = 0

    /// Distinct fixture files whose bytes this run actually read, recorded in
    /// `load`. The summary names this next to `passed` so the banner's units
    /// cannot be confused: `passed` counts CHECKS (one fixture can carry several,
    /// and a `checkFails` fixture adds one by *throwing*), this set counts
    /// FIXTURES (distinct files opened). Neither number is ever typed into a
    /// string, here or in the README — a typed count rots (t_74b0c758 measured it
    /// drifting twice, once inside the commit that wrote it), so both are derived
    /// from what the run did. New fixture reads must go through `load`: a fixture
    /// read only by an inline `Data(contentsOf:)` would not be counted here.
    /// (Every committed fixture is read through `load` today — the handful of
    /// older inline reads in this file all name a fixture that `load` also opens.)
    var fixturesRead: Set<String> = []

    init(dir: String) { self.dir = dir }

    /// Load fixture bytes, and remember that this fixture was read.
    func load(_ name: String) throws -> Data {
        let data = try Data(contentsOf: URL(fileURLWithPath: dir + "/" + name))
        fixturesRead.insert(name)
        return data
    }

    /// Decode `T` from fixture `name`; record a named failure on any throw.
    /// The DecodingError's codingPath + field key are included in `error` so a
    /// failure names the fixture AND the field.
    func check<T: Decodable>(_ type: T.Type, _ name: String, _ label: String) {
        do {
            _ = try JSONDecoder().decode(type, from: load(name))
            passed += 1
            print("OK   \(name)  →  \(label)")
        } catch {
            var field = ""
            if let err = error as? DecodingError, case .keyNotFound(let key, _) = err {
                field = "field \(key.stringValue)"
            }
            let msg = error.localizedDescription
            failures.append((name, label, field.isEmpty ? msg : field + " — " + msg))
            print("FAIL \(name)  →  \(label): \(error)")
        }
    }
    /// Run a block of assertions. A throw inside is a named failure, not a crash
    /// of the whole harness, so one drifted fixture still reports the rest.
    func assertions(_ name: String, _ label: String, _ body: () throws -> Void) {
        do { try body() } catch {
            failures.append((name, label, "\(error)"))
            print("FAIL \(name)  →  \(label): \(error)")
        }
    }

    /// Assert a decode that MUST fail: the shape a contract rule forbids. This is
    /// the §6 "a missing key is a bug, zero is a value" case — a counts map that
    /// omits `done` has to throw, or a widget renders `done: 0` forever.
    func checkFails<T: Decodable>(_ type: T.Type, _ name: String, _ label: String) {
        do {
            _ = try JSONDecoder().decode(type, from: load(name))
            failures.append((name, label, "decoded — but this fixture must NOT decode"))
            print("FAIL \(name)  →  \(label): decoded when it should have thrown")
        } catch {
            passed += 1
            print("OK   \(name)  →  \(label) (rejected as required)")
        }
    }

    /// Assert what a surface READS from a payload that decoded, not only that the
    /// bytes parsed: the row cap, the age order, the degrade path.
    func expect(_ name: String, _ label: String, _ ok: Bool, _ detail: String = "") {
        if ok {
            passed += 1
            print("OK   \(name)  →  \(label)")
        } else {
            failures.append((name, label, detail.isEmpty ? "assertion failed" : detail))
            print("FAIL \(name)  →  \(label): \(detail)")
        }
    }

}

let fixtureDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/scripts/model_decode_check/fixtures"

let c = DecodeCheck(dir: fixtureDir)

// ---- Read responses with a committed fixture. Each maps a real model type to
// a real capture. Adding a fixture here extends coverage; editing a model to
// make one pass is exactly what this check is meant to validate. ----
c.check(PingResponse.self, "v1_ping.json", "PingResponse")
c.check(ClusterHostsResponse.self, "cluster_hosts.json", "ClusterHostsResponse")
c.check(HealthResponse.self, "v1_health.json", "HealthResponse")
c.check(HealthResponse.self, "v1_verify.json", "VerifyResponse (= HealthResponse)")
c.check(AutoscaleResponse.self, "v1_autoscale.json", "AutoscaleResponse")
c.check(FleetStatsResponse.self, "fleet_stats.json", "FleetStatsResponse")
c.check(FleetThroughputResponse.self, "fleet_throughput.json", "FleetThroughputResponse")
c.check(FleetStreamsResponse.self, "fleet_streams.json", "FleetStreamsResponse")
c.check(CardsResponse.self, "cards.json", "CardsResponse")
c.check(CardDetailResponse.self, "card_detail_t_049d6986.json", "CardDetailResponse")
c.check(StandupResponse.self, "v1_standup.json", "StandupResponse")
c.check(ReviewQueueResponse.self, "v1_review_queue.json", "ReviewQueueResponse")
c.check(QAQueueResponse.self, "v1_qa_queue.json", "QAQueueResponse")
c.check(ProjectsResponse.self, "v1_projects.json", "ProjectsResponse")
c.check(ProjectDetailResponse.self, "project_hscc.json", "ProjectDetailResponse")
c.check(DaemonStatusResponse.self, "daemon_status.json", "DaemonStatusResponse")
c.check(TriggersResponse.self, "v1_triggers.json", "TriggersResponse")
c.check(EscalationsResponse.self, "v1_escalate.json", "EscalationsResponse")
c.check(ProfilesResponse.self, "v1_profiles.json", "ProfilesResponse")
c.check(ProfileEditorResponse.self, "v1_profile_editor.json", "ProfileEditorResponse")
c.check(KanbanBlockedResponse.self, "kanban_blocked.json", "KanbanBlockedResponse")
c.check(KanbanStaleResponse.self, "kanban_stale.json", "KanbanStaleResponse")
c.check(TemplateListResponse.self, "template_list.json", "TemplateListResponse")
c.check(TemplateStatusResponse.self, "template_status.json", "TemplateStatusResponse")
c.check(ClusterStatusResponse.self, "v1_cluster_status.json", "ClusterStatusResponse (Shared)")
c.check(AutodownStatusResponse.self, "autodown_status.json", "AutodownStatusResponse (Shared)")
c.check(SessionsListResponse.self, "v1_sessions.json", "SessionsListResponse")
c.check(ActivityFeedResponse.self, "v1_activity_feed.json", "ActivityFeedResponse")

// ---- Phase 4 (t_ca133e7d) — previously-uncovered wire families, captured
// LIVE from the running API 2026-09-27 and sanitized value-only. These close
// the three remaining decode gaps so no Codable response model is left without
// a contract-pinning fixture.
c.check(CommandsResponse.self, "commands.json", "CommandsResponse")
c.check(ProfileListResponse.self, "profiles_list.json", "ProfileListResponse")
c.check(LogsResponse.self, "logs_daemon.json", "LogsResponse (bare [LogEntry])")

// ---- Phase 3 cron roster (t_a2c8e456) — decode the schedule-jobs roster
// against the REAL wire model and assert the contract the view relies on:
// jobs decode (with every nullable field), the `speak` summary surfaces, and
// the roster maps active/paused correctly via `isActive`. ---- 
c.check(CronListResponse.self, "cron_list.json", "CronListResponse")
do {
    let roster = try JSONDecoder().decode(
        CronListResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/cron_list.json")))
    // The REAL capture is 11 jobs, 2 of them enabled/active (hscc-dep-watcher
    // + hscc-escalate-watcher), all last_status ok with null last_error.
    var ok = roster.jobs?.count == 11
    ok = ok && roster.speak == "11 scheduled jobs (2 active, 9 paused)."
    // active vs paused classification matches the enabled/state contract.
    ok = ok && (roster.jobs ?? []).filter(\.isActive).count == 2
    ok = ok && (roster.jobs ?? []).first(where: { $0.name == "hscc-dep-watcher" })?.isActive == true
    ok = ok && (roster.jobs ?? []).first(where: { $0.name == "sparkrun-issue-176-monitor" })?.isActive == false
    // every nullable field is allowed to be absent/nil without a decode throw.
    ok = ok && (roster.jobs ?? []).allSatisfy { $0.last_error == nil }
    if ok {
        c.passed += 1
        print("OK   cron_list.json  →  cron roster contract (11 jobs, 2 active, speak decode)")
    } else {
        c.failures.append(("cron_list.json", "cron roster contract", "assertion failed"))
        print("FAIL cron_list.json → cron roster contract")
    }
} catch {
    c.failures.append(("cron_list.json", "cron roster contract", "\(error)"))
    print("FAIL cron_list.json → cron roster contract: \(error)")
}

// ---- Watchdog & self-heal history (t_b5ce7935) — decode the timeline against
// the REAL wire model and assert the enum/kinds contract the view relies on:
// known kinds decode to their case, an unknown future kind decodes to nil
// (the view renders it via the generic "Event" label), and the outcome field
// (a raw String that feeds HistoryOutcome(rawValue:)) stays available. ----
c.check(HistoryResponse.self, "v1_daemon_history.json", "HistoryResponse")
do {
    let resp = try JSONDecoder().decode(
        HistoryResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/v1_daemon_history.json")))
    var ok = resp.count == 7 && resp.events.count == 7
    ok = ok && resp.windowBytes == 33554432 && resp.exhausted == false
    // kinds decode to the right case.
    ok = ok && resp.events[0].kind == .block && resp.events[1].kind == .restart
    ok = ok && resp.events[3].kind == .autodown && resp.events[6].kind == .recovery
    // outcome is the raw string the display enum consumes.
    ok = ok && resp.events[1].outcome == "success" && resp.events[2].outcome == "failed"
    // outcome resolution helper: no `.autodown` case exists in HistoryOutcome,
    // so raw "autodown" falls through to the .info display fallback.
    ok = ok && resp.events[0].resolvedOutcome == .info
    // unknown/arbitrary outcome string degrades to .info.
    ok = ok && HistoryOutcome(rawValue: "nonsense") == nil
    if ok {
        c.passed += 1
        print("OK   v1_daemon_history.json  →  history contract (7 events, kinds+outcomes decode, lenient unknowns)")
    } else {
        c.failures.append(("v1_daemon_history.json", "history contract", "assertion failed"))
        print("FAIL v1_daemon_history.json → history contract")
    }
} catch {
    c.failures.append(("v1_daemon_history.json", "history contract", "\(error)"))
    print("FAIL v1_daemon_history.json → history contract: \(error)")
}

// ---- Mutation POST responses (previously uncovered; each shape derived from
// the actual hscc-api handler so the fixture mirrors what the real endpoint
// returns on a 2xx clean success). ----
c.check(ReviewDetailResponse.self, "review_detail.json", "ReviewDetailResponse")
c.check(DispatchCardResponse.self, "dispatch_card.json", "DispatchCardResponse")
c.check(MergeCardResponse.self, "merge_card.json", "MergeCardResponse")
c.check(StopClusterResponse.self, "stop_cluster.json", "StopClusterResponse")
c.check(RecoverCardResponse.self, "recover_card.json", "RecoverCardResponse")
c.check(SessionMutationResponse.self, "session_retire.json", "SessionMutationResponse (retire)")
c.check(ClusterUpResponse.self, "cluster_up.json", "ClusterUpResponse")
c.check(ClusterDownResponse.self, "cluster_down.json", "ClusterDownResponse")
c.check(OrchestratorChatJobResponse.self, "orchestrator_chat.json", "OrchestratorChatJobResponse")
c.check(OrchestratorChatJobStatus.self, "orchestrator_chat_status.json", "OrchestratorChatJobStatus")

// ---- Phase 4 (t_ca133e7d) — contract assertions for the newly-pinned
// families. Each guards a decode/classification invariant the views rely on,
// not just bare shape.
do {
    // Commands: every slash command carries name/takes_args, and the palette
    // needs both a no-args command and an args-taking command to render.
    let cmds = try JSONDecoder().decode(
        CommandsResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/commands.json")))
    var ok = !cmds.commands.isEmpty
    ok = ok && cmds.speak == "12 slash commands available."
    ok = ok && cmds.commands.contains { $0.name == "cluster" && $0.takesArgs == false }
    ok = ok && cmds.commands.contains { $0.name == "cluster-restart" && $0.takesArgs == true }
    // every command must carry its description (the palette shows it under the name).
    ok = ok && cmds.commands.allSatisfy { !($0.description.isEmpty) }
    if ok {
        c.passed += 1
        print("OK   commands.json  →  slash-command catalog (name/description/takes_args decode, args + no-args present)")
    } else {
        c.failures.append(("commands.json", "commands contract", "assertion failed"))
        print("FAIL commands.json → commands contract")
    }
} catch {
    c.failures.append(("commands.json", "commands contract", "\(error)"))
    print("FAIL commands.json → commands contract: \(error)")
}

do {
    // Profile roster: the full list shape tolerates the extra distribution_*
    // keys the live wire carries (Decodable ignores unknowns), the required
    // `name` decodes, and count/speak agree — so the memory picker never blanks.
    let roster = try JSONDecoder().decode(
        ProfileListResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/profiles_list.json")))
    var ok = (roster.profiles ?? []).count == 4
    ok = ok && roster.count == 4
    ok = ok && !roster.speak.isEmpty
    ok = ok && (roster.profiles ?? []).contains { $0.name == "default" && $0.is_default == true }
    // the model ignores the extra keys but must still present every declared field.
    ok = ok && (roster.profiles ?? []).allSatisfy { !($0.name.isEmpty) }
    ok = ok && (roster.profiles ?? []).allSatisfy { $0.skill_count != nil }
    if ok {
        c.passed += 1
        print("OK   profiles_list.json  →  profile roster (name/is_default/skill_count decode, extra distribution_* keys tolerated)")
    } else {
        c.failures.append(("profiles_list.json", "profile roster contract", "assertion failed"))
        print("FAIL profiles_list.json → profile roster contract")
    }
} catch {
    c.failures.append(("profiles_list.json", "profile roster contract", "\(error)"))
    print("FAIL profiles_list.json → profile roster contract: \(error)")
}

do {
    // Logs: the bare array decodes each row with all four keys; INFO and ERROR
    // levels both present; source is the selector bucket the view filters by.
    let logs = try JSONDecoder().decode(
        LogsResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/logs_daemon.json")))
    var ok = logs.count == 6
    ok = ok && logs.allSatisfy { $0.timestamp != nil && $0.level != nil && $0.source != nil && $0.line != nil }
    ok = ok && logs.contains { $0.level == "ERROR" }
    ok = ok && logs.contains { $0.level == "INFO" }
    ok = ok && logs.allSatisfy { $0.source == "daemon" }
    if ok {
        c.passed += 1
        print("OK   logs_daemon.json  →  log tail (bare [LogEntry], 4 keys/row, INFO+ERROR, source filter preserved)")
    } else {
        c.failures.append(("logs_daemon.json", "logs contract", "assertion failed"))
        print("FAIL logs_daemon.json → logs contract")
    }
} catch {
    c.failures.append(("logs_daemon.json", "logs contract", "\(error)"))
    print("FAIL logs_daemon.json → logs contract: \(error)")
}

// ---- Approvals classification (t_9a5cfc3b) — the SAME `isPendingApproval`
// logic the on-screen inbox, the badge poller, and the Siri intent use, asserted
// against the REAL committed kanban_blocked.json fixture. Both cards in that
// capture are `block_kind == needs_input`, so both are pending approvals. This
// guards the classification from drifting on decode types we don't redeclare. ----
do {
    let blocked = try JSONDecoder().decode(
        KanbanBlockedResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/kanban_blocked.json")))
    let cards = blocked.tasks ?? []
    let pending = cards.filter(\.isPendingApproval)
    if cards.count == 2 && pending.count == 2 {
        c.passed += 1
        print("OK   kanban_blocked.json  →  approvals classification: 2/2 pending")
    } else {
        c.failures.append(("kanban_blocked.json",
                           "isPendingApproval classification",
                           "expected 2/2 pending, got \(cards.count) cards / \(pending.count) pending"))
        print("FAIL kanban_blocked.json → isPendingApproval classification (\(pending.count)/\(cards.count))")
    }
} catch {
    c.failures.append(("kanban_blocked.json", "approvals classification", "\(error)"))
    print("FAIL kanban_blocked.json → approvals classification: \(error)")
}

// ---- Cluster monitor (per-node fleet telemetry, t_a1aaba69) ----
// The /v1/cluster/monitor route wraps the sparkrun payload under `json`
// (`cmd_monitor()['json']`), so ClusterMonitorResponse must decode that nested
// shape, not a flat one. This catches a regression to a flat model (which
// decodes `hosts` to empty and shows zero node cards). Assert both the healthy
// and the degraded `{"speak": "..."}` wire bodies.
do {
    let healthy = try JSONDecoder().decode(
        ClusterMonitorResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/v1_cluster_monitor.json")))
    var ok = healthy.hosts.count == 4
    ok = ok && healthy.hosts.allSatisfy { $0.sample != nil }
    ok = ok && healthy.timestamp != nil && !healthy.speak.isEmpty
    // every per-node metric the view reads parses from a bare STRING field.
    ok = ok && healthy.hosts.allSatisfy { $0.sample?.gpuUtilPct != nil }
    ok = ok && healthy.hosts.allSatisfy { $0.sample?.cpuUsagePct != nil }
    ok = ok && healthy.hosts.allSatisfy { $0.sample?.memUsedPct != nil }
    ok = ok && healthy.hosts.allSatisfy { $0.sample?.gpuTempC != nil }
    ok = ok && healthy.hosts.allSatisfy { $0.sample?.cpuTempC != nil }
    ok = ok && healthy.hosts.allSatisfy { $0.sample?.uptimeText != nil }
    if ok {
        c.passed += 1
        print("OK   v1_cluster_monitor.json  →  ClusterMonitorResponse nested json: 4 nodes, GPU/CPU/RAM/temp all parse")
    } else {
        c.failures.append(("v1_cluster_monitor.json", "ClusterMonitorResponse nested json", "assertion failed"))
        print("FAIL v1_cluster_monitor.json → ClusterMonitorResponse nested json")
    }
    let degraded = try JSONDecoder().decode(
        ClusterMonitorResponse.self,
        from: try Data(contentsOf: URL(fileURLWithPath: fixtureDir + "/v1_cluster_monitor_unavailable.json")))
    if degraded.hosts.isEmpty && degraded.speak == "fleet monitor unavailable" {
        c.passed += 1
        print("OK   v1_cluster_monitor_unavailable.json  →  degraded {speak} body decodes, empty hosts")
    } else {
        c.failures.append(("v1_cluster_monitor_unavailable.json", "degraded decode", "expected empty hosts + unavailable speak"))
        print("FAIL v1_cluster_monitor_unavailable.json → degraded decode")
    }
} catch {
    c.failures.append(("v1_cluster_monitor.json", "ClusterMonitorResponse decode", "\(error)"))
    print("FAIL v1_cluster_monitor.json → ClusterMonitorResponse decode: \(error)")
}

// ---- Widget / Live Activity payloads (t_595a5fee) -------------------------
// The five datasets of docs/WIDGET_DATA_CONTRACT.md §3 are what the pivot's
// surfaces render, and every type below is the REAL one from
// Sources/Shared/SharedModels.swift. This card deleted the two mirror harnesses
// (scripts/shared_model_check.swift, scripts/widget_kanban_decode_check.swift)
// that re-declared these structs: a mirror that drifts is exactly the false
// green this harness exists to prevent, and the real types decode here anyway.

// Dataset 1 — projects (`reuse`). v1_projects.json is the live capture and it
// DOES carry `repo` (an absolute path on the operator's machine) and `topic`;
// ProjectsLite declares neither, so a clean decode plus `carriesRepo` proves the
// §7 redaction is structural: the value arrives, is ignored, and can never be
// rendered or written into the App Group container.
c.assertions("v1_projects.json", "ProjectsLite (widget projects)") {
    let raw = try c.load("v1_projects.json")
    let p = try JSONDecoder().decode(ProjectsLite.self, from: raw)
    let text = String(decoding: raw, as: UTF8.self)
    c.expect("v1_projects.json", "ProjectsLite: \(p.projects.count) projects + count agrees",
             !p.projects.isEmpty && p.count == p.projects.count,
             "count \(String(describing: p.count)) vs \(p.projects.count) rows")
    c.expect("v1_projects.json", "ProjectsLite: `repo`/`topic` on the wire, NOT in the type",
             text.contains("\"repo\"") && text.contains("\"topic\""),
             "fixture lost the fields the redaction proves")
    c.expect("v1_projects.json", "rows(limit:) caps to what a surface draws (§5.1)",
             p.rows(limit: 3).count == min(3, p.projects.count)
             && p.rows(limit: 99).count == p.projects.count,
             "row cap is not what §5.1 sizes")
    c.expect("v1_projects.json", "every row has a name (its identity)",
             p.projects.allSatisfy { !$0.name.isEmpty }, "empty name")
}

// Dataset 2 — board status counts (`GET /v1/mobile/summary`, §6). The route is
// not deployed yet (PR #310 open; a live GET 404s — verified 2026-11-17), so
// these fixtures are authored from routes_mobile.py::_assemble and its degrade
// paths; provenance is recorded in fixtures/README.md.
c.assertions("v1_mobile_summary.json", "MobileSummaryResponse (§6 counts)") {
    let ok = try JSONDecoder().decode(MobileSummaryResponse.self,
                                      from: try c.load("v1_mobile_summary.json"))
    c.expect("v1_mobile_summary.json", "decodes, not degraded, speak present",
             !ok.degraded && ok.entries.count == 3 && !ok.speak.isEmpty, "expected 3 tiles")
    c.expect("v1_mobile_summary.json", "every tile carries all six counts",
             ok.entries.allSatisfy { $0.counts != nil && $0.error == nil },
             "a tile without counts is a bug, not a quiet board")
    c.expect("v1_mobile_summary.json", "openTotal = total − done − archived (§6)",
             ok.board("hscc")?.openTotal == 22
             && ok.entries.reduce(0) { $0 + ($1.openTotal ?? 0) } == 37,
             "open math drifted from the server's _open_total")
    c.expect("v1_mobile_summary.json", "openCount + hasBlocked drive the tile",
             ok.board("headless-display")?.counts?.openCount == 10
             && ok.board("headless-display")?.counts?.hasBlocked == true
             && ok.board("hscc")?.counts?.hasBlocked == false,
             "the blocked-is-red rule is wrong")
    c.expect("v1_mobile_summary.json", "an absent board reads nil, never a zero tile",
             ok.board("does-not-exist") == nil, "the lookup invented a tile")

    // §6 rule 7: the degrade path sends no `boards` key at all.
    let deg = try JSONDecoder().decode(MobileSummaryResponse.self,
                                       from: try c.load("v1_mobile_summary_degraded.json"))
    c.expect("v1_mobile_summary_degraded.json", "degraded: zero tiles, no counts, speak only",
             deg.degraded && deg.entries.isEmpty && deg.board_count == nil
             && deg.speak == "Board counts unavailable.", "degrade body changed shape")

    // One unreadable board must stay an error, not become a zero.
    let partial = try JSONDecoder().decode(MobileSummaryResponse.self,
                                           from: try c.load("v1_mobile_summary_board_error.json"))
    c.expect("v1_mobile_summary_board_error.json", "per-board error stays an error",
             partial.entries.count == 2
             && partial.board("headless-display")?.counts == nil
             && partial.board("headless-display")?.error != nil
             && partial.board("headless-display")?.openTotal == nil
             && partial.board("hscc")?.counts?.done == 750,
             "an unreadable board was rendered as data")

    // The 4 KB Live-Activity budget trims the tail and announces it.
    let trunc = try JSONDecoder().decode(MobileSummaryResponse.self,
                                         from: try c.load("v1_mobile_summary_truncated.json"))
    c.expect("v1_mobile_summary_truncated.json", "truncation is announced, never silent",
             trunc.truncated == true && trunc.omitted_boards == 22
             && trunc.unreadable_boards == 1 && trunc.entries.count == 1
             && trunc.total_boards == 23, "a trimmed payload no longer reports the trim")

    // §6 rule 1 — the case that shipped the original lie: counts without `done`.
    c.checkFails(MobileSummaryResponse.self, "v1_mobile_summary_missing_done.json",
                 "BoardStatusCounts requires all six statuses")
}

// Datasets 3 + 4 — the kanban trio. v1_kanban_running.json is a LIVE capture that
// still carries `boards`, `errors`, `pid` and `host_local`; the Lite types declare
// none of them — §5.3's narrowing plus the read-only pivot (no surface may see
// which process a kill would stop).
c.assertions("v1_kanban_running.json", "the kanban trio (Lite decoders)") {
    let raw = try c.load("v1_kanban_running.json")
    let r = try JSONDecoder().decode(KanbanRunningLite.self, from: raw)
    let text = String(decoding: raw, as: UTF8.self)
    c.expect("v1_kanban_running.json", "KanbanRunningLite decodes the live envelope",
             (r.tasks?.count ?? -1) == r.count && (r.speak ?? "").isEmpty == false,
             "count/tasks disagree")
    c.expect("v1_kanban_running.json",
             "`boards`/`errors`/`pid`/`host_local` on the wire, not in the type",
             text.contains("\"boards\"") && text.contains("\"errors\"")
             && text.contains("\"pid\"") && text.contains("\"host_local\""),
             "fixture lost the fields the narrowing proves")
    c.expect("v1_kanban_running.json", "rows(limit:) caps the surface",
             r.rows(limit: 2).count == min(2, r.tasks?.count ?? 0), "row cap wrong")

    let b = try JSONDecoder().decode(KanbanBlockedLite.self, from: try c.load("kanban_blocked.json"))
    let ages = b.rows(limit: 3).map { $0.age_days ?? 0 }
    c.expect("kanban_blocked.json", "KanbanBlockedLite: rows capped, stuck-longest-first (§5.3)",
             !ages.isEmpty && ages == ages.sorted(by: >) && ages.count <= 3,
             "the row order drifted: \(ages)")
    c.expect("kanban_blocked.json", "block prose (`why`/`comments`) is on the wire, not in the type",
             String(decoding: try c.load("kanban_blocked.json"), as: UTF8.self)
                .contains("\"why\""), "fixture no longer carries the prose to ignore")

    let s = try JSONDecoder().decode(KanbanStaleLite.self, from: try c.load("kanban_stale.json"))
    c.expect("kanban_stale.json", "KanbanStaleLite: envelope agrees, rows capped at 8",
             (s.tasks?.count ?? -1) == s.count && s.rows(limit: 8).count <= 8,
             "envelope or row cap wrong")

    // The committed `kanban_stale.json` is a 2-row capture whose rows are both
    // `running`, so it cannot exercise the two things a queue-depth tile depends
    // on. `kanban_stale_queued.json` is shape-faithful from
    // `autodown.list_stale_tasks` (§4.3's keys) and carries the mixed spread:
    // queue depth = the ready+todo rows, and rows(limit:) must order oldest first.
    let q = try JSONDecoder().decode(KanbanStaleLite.self,
                                     from: try c.load("kanban_stale_queued.json"))
    let queued = (q.tasks ?? []).filter { $0.status == "ready" || $0.status == "todo" }
    c.expect("kanban_stale_queued.json", "queue depth = ready+todo rows (§5.3's small family)",
             queued.count == 4 && q.count == q.tasks?.count,
             "got \(queued.count) queued of \(String(describing: q.count))")
    let qAges = q.rows(limit: 3).map { $0.age_days ?? -1 }
    c.expect("kanban_stale_queued.json", "rows(limit:) returns the oldest 3 first",
             qAges == [12, 7, 5], "ages came back \(qAges)")
}

// The cards surface's own pair (B2). `v1_kanban_running_cards.json` is a
// scrubbed live capture; `v1_kanban_running_contract.json` is the fixed route's
// CARD_VIEW_FIELDS shape (worker + run_s + hb_s) that the 2.15.11 server does
// not send yet. Both decode through the same REAL `RunningCardLite`.
c.assertions("v1_kanban_running_cards.json", "the running-cards surface (live + contract shapes)") {
    let live = try JSONDecoder().decode(KanbanRunningLite.self,
                                        from: try c.load("v1_kanban_running_cards.json"))
    c.expect("v1_kanban_running_cards.json", "the scrubbed live capture decodes",
             live.count == 3 && live.tasks?.count == 3,
             "count/tasks disagree")
    c.expect("v1_kanban_running_cards.json", "the old server's rows use assignee + started_at",
             live.tasks?.allSatisfy { $0.worker == nil && $0.started_at != nil } == true,
             "the live shape drifted")

    let contract = try JSONDecoder().decode(KanbanRunningLite.self,
                                            from: try c.load("v1_kanban_running_contract.json"))
    let first = contract.tasks?.first
    c.expect("v1_kanban_running_contract.json",
             "worker / run_s / hb_s decode from CARD_VIEW_FIELDS",
             first?.worker == "backend-engineer" && first?.run_s == 3720
             && first?.hb_s == 12.4 && contract.count == 3,
             "a CARD_VIEW_FIELDS key is not decoded")
    c.expect("v1_kanban_running_contract.json",
             "an absent clock is nil, never a zero",
             contract.tasks?.last?.run_s != nil
             && contract.tasks?.last?.started_at == nil,
             "the run_s-only row lost its shape")
}

// Dataset 5 — cluster monitor + daemon host. `output` (54% of the body) is not
// declared, and `displayLabel` is §7's redacted node identity: asserted here so no
// surface can quietly render a LAN address.
c.assertions("v1_cluster_monitor.json", "cluster monitor + daemon host (widget/LA)") {
    let m = try JSONDecoder().decode(MonitorResponse.self, from: try c.load("v1_cluster_monitor.json"))
    c.expect("v1_cluster_monitor.json", "MonitorResponse: 4 nodes, every metric parses",
             m.nodes.count == 4 && m.nodes.allSatisfy { $0.sample != nil }
             && m.nodes.allSatisfy { $0.sample?.cpuUsagePct != nil && $0.sample?.memUsedPct != nil }
             && m.nodes.allSatisfy { $0.sample?.gpuUtilPct != nil },
             "node metrics stopped parsing")
    c.expect("v1_cluster_monitor.json", "displayLabel never leaks the node's LAN address (§7)",
             m.nodes.allSatisfy { node in
                 !node.displayLabel.contains(node.host)
                 && (node.displayLabel == node.sample?.hostname
                     || (node.displayLabel.hasPrefix(".")
                         && node.displayLabel.dropFirst().allSatisfy { ch in ch.isNumber }))
             }, "a surface could render a full address")
    c.expect("v1_cluster_monitor.json", "shortTail reduces an address to its last octet (§7)",
             m.nodes.allSatisfy { MonitorNode.shortTail($0.host)
                 == "." + ($0.host.split(separator: ".").last.map(String.init) ?? "") }
             && MonitorNode.shortTail("192.0.2.244 up 3d") == ".244",
             "the shared tail rule is not a tail")
    let down = try JSONDecoder().decode(MonitorResponse.self,
                                        from: try c.load("v1_cluster_monitor_unavailable.json"))
    c.expect("v1_cluster_monitor_unavailable.json", "fleet-down degrade: no json key, no nodes",
             down.nodes.isEmpty && down.speak == "fleet monitor unavailable",
             "degrade shape changed")

    let h = try JSONDecoder().decode(MonitorDaemonHost.self, from: try c.load("v1_daemon_host.json"))
    c.expect("v1_daemon_host.json", "daemon host (live capture): cpu/mem/disk + uptime parse",
             h.cpuPercent != nil && h.memPercent != nil && h.diskPercent != nil
             && h.uptimeText != nil && h.cpuCount == 14 && !h.speak.isEmpty,
             "the daemon-host sidecar stopped decoding")
}

// ---- SavedCluster: old blobs keep working, new blobs keep the order ----
//
// The multi-address model added `fallbackHosts` AFTER operators already had
// saved clusters on disk. A blob written before that key existed must decode
// with [] (no migration prompt, no lost cluster), while a multi-address blob
// must preserve primary → fallbacks and put the last-working address first at
// request time.
let failoverDefaults = UserDefaults(suiteName: AppGroup.suiteName)
failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
failoverDefaults?.synchronize()

c.assertions("saved_cluster_legacy.json", "old saved data decodes with no fallbacks") {
    let legacy = try JSONDecoder().decode(SavedCluster.self,
                                          from: try c.load("saved_cluster_legacy.json"))
    c.expect("saved_cluster_legacy.json", "legacy blob has an empty fallback list",
             legacy.fallbackHosts.isEmpty,
             "missing fallbackHosts decoded as \(legacy.fallbackHosts)")
    c.expect("saved_cluster_legacy.json", "legacy blob still exposes its one primary address",
             legacy.hosts == ["host-a.example"],
             "hosts = \(legacy.hosts)")
    let roundTripped = try JSONDecoder().decode(
        SavedCluster.self, from: try JSONEncoder().encode(legacy))
    c.expect("saved_cluster_legacy.json", "a re-save of a legacy cluster invents no address",
             roundTripped.fallbackHosts.isEmpty && roundTripped.host == "host-a.example",
             "round-trip = \(roundTripped.host) + \(roundTripped.fallbackHosts)")
}

c.assertions("saved_cluster_multi_address.json", "order is primary -> fallbacks, last-working first") {
    let multi = try JSONDecoder().decode(SavedCluster.self,
                                         from: try c.load("saved_cluster_multi_address.json"))
    c.expect("saved_cluster_multi_address.json", "saved order is primary, then fallbacks in operator order",
             multi.hosts == ["host-a.example", "host-b.example", "host-c.example"],
             "hosts = \(multi.hosts)")
    c.expect("saved_cluster_multi_address.json", "with no remembered host the try order is the saved order",
             HostFailover.orderedHosts(multi.hosts) == multi.hosts,
             "try order = \(HostFailover.orderedHosts(multi.hosts))")
    HostFailover.remember("host-b.example")
    c.expect("saved_cluster_multi_address.json", "the last-working address is tried first, then the rest in order",
             HostFailover.orderedHosts(multi.hosts)
                == ["host-b.example", "host-a.example", "host-c.example"],
             "try order = \(HostFailover.orderedHosts(multi.hosts))")
    failoverDefaults?.removeObject(forKey: AppGroup.lastWorkingHostKey)
failoverDefaults?.synchronize()
}

// ---- §7 storage shape: what the App Group may actually hold (t_438aa6ce) ----
//
// `MonitorSnapshotStore` writes the last-known monitor snapshot into
// `group.com.hscc.ios`, which every extension can read and which outlives the
// app process. So the shape §7 binds is the STORED JSON, not the decoded
// response — and this is the one place that checks it. `SharedStore.swift` is in
// `model_decode_check.sh`'s real-source list, so `MonitorNodeEntry`,
// `MonitorSnapshotStore.project` and `Snapshot` here are the production types,
// not mirrors.
//
// The control that gives the address scan teeth: the live capture's
// `hosts[].host` values ARE dotted quads (192.0.2.1 / 192.0.2.2), so persisting
// `MonitorNode` instead of the projection fails this check rather than passing
// it vacuously.

/// A JSON value, declared by the VALIDATOR (not a model — nothing in
/// `Sources/` reads it) so the stored bytes can be walked key by key.
private enum StoredJSON: Decodable {
    case null, bool(Bool), number(Double), string(String)
    case array([StoredJSON]), object([String: StoredJSON])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let d = try? c.decode(Double.self) { self = .number(d); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([StoredJSON].self) { self = .array(a); return }
        if let o = try? c.decode([String: StoredJSON].self) { self = .object(o); return }
        throw DecodingError.dataCorruptedError(in: c, debugDescription: "unsupported JSON value")
    }

    /// Every string the stored bytes could be hiding an address in: each key
    /// name and each string value, each with its JSON path so a failure says
    /// WHERE the address was found.
    func addressCandidates(_ path: String = "$") -> [(at: String, text: String)] {
        switch self {
        case .null, .bool, .number:
            return []
        case .string(let s):
            let key = path.split(separator: ".").last.map(String.init) ?? path
            return [(path, key), (path, s)]
        case .array(let items):
            return items.enumerated().flatMap { addressCandidates(at: "\(path)[\($0.offset)]", from: $0.element) }
        case .object(let pairs):
            return pairs.sorted { $0.key < $1.key }.flatMap { k, v in
                [(path, k)] + addressCandidates(at: "\(path).\(k)", from: v)
            }
        }
    }

    private func addressCandidates(at path: String, from v: StoredJSON) -> [(at: String, text: String)] {
        v.addressCandidates(path)
    }

    /// Every object in the document with its JSON path and key set, so a check
    /// can name the exact field list a stored object is allowed to have.
    func objects(_ path: String = "$") -> [(path: String, keys: Set<String>)] {
        var out: [(path: String, keys: Set<String>)] = []
        switch self {
        case .object(let pairs):
            out.append((path, Set(pairs.keys)))
            for (k, v) in pairs.sorted(by: { $0.key < $1.key }) {
                out.append(contentsOf: v.objects("\(path).\(k)"))
            }
        case .array(let items):
            for (i, item) in items.enumerated() { out.append(contentsOf: item.objects("\(path)[\(i)]")) }
        default:
            break
        }
        return out
    }
}

/// Four dot-separated groups of 1–3 digits — a dotted-quad IPv4 address, e.g.
/// `100.64.0.1` (the docs/tests placeholder) or the LAN address §7 forbids.
private let dottedQuadRE = try! NSRegularExpression(pattern: #"\d{1,3}(?:\.\d{1,3}){3}"#)

/// Internal (not `private`) because `monitor_surfaces.swift` scans the Live
/// Activity's pushed state with the SAME validator, and two regexes that are
/// supposed to mean the same thing is how one of them stops matching.
func holdsDottedQuad(_ s: String) -> Bool {
    dottedQuadRE.firstMatch(in: s, range: NSRange(location: 0, length: s.utf16.count)) != nil
}

c.assertions("§7_monitor_snapshot", "the App Group holds the projection, never MonitorNode") {
    let monitor = try JSONDecoder().decode(MonitorResponse.self,
                                           from: try c.load("v1_cluster_monitor.json"))
    let host = try JSONDecoder().decode(MonitorDaemonHost.self,
                                        from: try c.load("v1_daemon_host.json"))

    // (0) The control: the fixture's wire nodes really do carry addresses.
    c.expect("§7_monitor_snapshot", "control — the fixture's MonitorNode.host is a dotted-quad",
             !monitor.nodes.isEmpty && monitor.nodes.allSatisfy { holdsDottedQuad($0.host) },
             "fixture hosts stopped being addresses, so the scan below proves nothing")

    // (1) project() — the only wire→storage path — drops the address and keeps
    //     the six §3 row-5 fields per node.
    let entries = MonitorSnapshotStore.project(monitor.nodes)
    let zipped = zip(entries, monitor.nodes)
    c.expect("§7_monitor_snapshot", "project() yields one entry per node, labelled by displayLabel and never by host",
             entries.count == monitor.nodes.count
             && zipped.allSatisfy { e, n in
                 e.label == n.displayLabel && e.label != n.host && !holdsDottedQuad(e.label)
             }, "a projected label stopped being displayLabel, or an address reached a label")
    c.expect("§7_monitor_snapshot", "project() carries the six §3 fields (cpu/mem/gpu util + used/free slots) unchanged",
             zipped.allSatisfy { e, n in
                 e.cpuUsagePct == n.sample?.cpuUsagePct && e.memUsedPct == n.sample?.memUsedPct
                 && e.gpuUtilPct == n.sample?.gpuUtilPct
                 && e.usedSlots == n.used_slots && e.freeSlots == n.free_slots
             }, "the projection lost or renamed a field the widget/contract needs")

    // (2) Round-trip through the REAL store encode/decode (same JSONEncoder /
    //     JSONDecoder + Snapshot type the App Group sees).
    let snap = MonitorSnapshotStore.Snapshot(date: 1_788_468_629.041858,
                                             nodes: entries, daemonHost: host)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let storedData = try encoder.encode(snap)
    let revived = try JSONDecoder().decode(MonitorSnapshotStore.Snapshot.self, from: storedData)
    c.expect("§7_monitor_snapshot", "MonitorNodeEntry round-trips through the real Snapshot encode/decode",
             revived.nodes == entries && revived.date == snap.date
             && revived.daemonHost?.hostname == host.hostname
             && revived.daemonHost?.cpuPercent == host.cpuPercent,
             "the stored form no longer survives a write+read")

    // (3) The stored bytes: no key and no string value may hold a dotted-quad.
    let stored = try JSONDecoder().decode(StoredJSON.self, from: storedData)
    let hits = stored.addressCandidates().filter { holdsDottedQuad($0.text) }
    c.expect("§7_monitor_snapshot", "no key or value in the stored snapshot JSON holds a dotted-quad (§7)",
             hits.isEmpty,
             "address-shaped value at \(hits.prefix(3).map { "\($0.at)=\($0.text)" }.joined(separator: ", "))")

    // (4) Structural: a stored node object's keys are EXACTLY the six
    //     projection fields. `host` / `sparkrun_job_names` are not merely unset
    //     — they are undeclared, so no future field can smuggle an address in.
    let nodeKeys = stored.objects().filter { $0.path.hasPrefix("$.nodes[") }.flatMap { $0.keys }
    let projectionKeys: Set<String> = ["label", "cpuUsagePct", "memUsedPct",
                                       "gpuUtilPct", "usedSlots", "freeSlots"]
    c.expect("§7_monitor_snapshot", "a stored node object declares no key outside the six projection fields (§7)",
             Set(nodeKeys).isSubset(of: projectionKeys),
             "stored node keys grew: \(Set(nodeKeys).subtracting(projectionKeys).sorted())")
    let firstNodeKeys = stored.objects().first { $0.path == "$.nodes[0]" }?.keys
    c.expect("§7_monitor_snapshot", "the first stored node object holds all six projection fields (Codable names intact)",
             firstNodeKeys == projectionKeys,
             "stored node keys: \(firstNodeKeys.map { $0.sorted().joined(separator: ",") } ?? "<no node object>")")

    // (5) daemonHost STAYS in the snapshot (t_595a5fee's decision) because it
    //     carries no address: its stored keys are exactly §5.5's machine fields,
    //     and `hostname` is the daemon host's own name, not where it lives.
    let daemonHostKeys = stored.objects().first { $0.path == "$.daemonHost" }?.keys ?? []
    c.expect("§7_monitor_snapshot", "the stored daemon-host object holds exactly §5.5's machine fields — no address field",
             daemonHostKeys == ["hostname", "platform", "arch", "uptime_seconds", "cpu", "memory",
                                "disk", "processes", "daemon_running", "daemon_uptime_seconds", "speak"],
             "daemon-host keys changed: \(daemonHostKeys.sorted())")
    c.expect("§7_monitor_snapshot", "the daemon host's hostname is a name, not an address (§5.5/§7)",
             host.hostname.map { !holdsDottedQuad($0) } ?? false,
             "hostname held an address")
}

// The old stored shape (§7's violation): the bytes `MonitorSnapshotStore` wrote
// before t_438aa6ce persisted the projection — a node object with `host`,
// `error` and the full `sample`, `sparkrun_job_names` included. It MUST fail to
// decode now: `MonitorNodeEntry` has no such fields, so a build that upgrades in
// place refuses the legacy blob instead of reading an address back out of the
// container. That is the migration behaviour, asserted here rather than argued.
c.checkFails(MonitorSnapshotStore.Snapshot.self, "v1_monitor_snapshot_legacy.json",
             "legacy snapshot blob (nodes carried `host` + raw sample) is rejected outright")

// ---- Cluster-monitor CLI parity (t_61ed3f5e). The checks themselves are in
// monitor_parity.swift next to this file — Swift forbids top-level statements
// outside main.swift, so only the calls are here. They compare the shared
// view-model's formatted cells against goldens that were NOT transcribed:
// scripts/cluster_monitor_cli_parity.py generated them by running
// hscc_daemon/cluster_live.py's own host_table() / serving_table() / render()
// over the same fixture bytes. Read that file's docstring before trusting a
// green run of these.
monitorParity(c, fixture: "v1_cluster_monitor_live.json",
              golden: "v1_cluster_monitor_live.cli.json",
              text: "v1_cluster_monitor_live.cli.txt")
monitorParity(c, fixture: "v1_cluster_monitor_edge.json",
              golden: "v1_cluster_monitor_edge.cli.json",
              text: "v1_cluster_monitor_edge.cli.txt")
monitorSampleDecodeShape(c)

// ---- The systemSmall grid (t_9e110d88). The checks are in
// cluster_small_grid.swift next to this file. They EXECUTE
// Sources/HSCCWidgets/ClusterSmallGrid.swift — the tile the small widget draws —
// against the fixtures above and, where a golden exists, the CLI's own cells.
clusterSmallGridChecks(c)
// ---- The large / extra-large table layout (t_9e64d9d9). The checks are in
// monitor_tables.swift; only the calls are here. Parity above owns the CELLS;
// these own the SELECTION (which columns a width draws), the CLIP, and the six
// fields only the two biggest monitor surfaces print (VRAM, GPU clock, net,
// uptime, fan, load) — none of which the CLI's host table has a column for, so
// they are pinned by rule and by fixture rather than by a CLI golden.
monitorRateRules(c)
monitorDetailRules(c)
monitorPlans(c)
monitorTablesFromFixtures(c)

// ---- Cluster history (t_b6512c2e) ---------------------------------------
// The four `v1_cluster_history_*.json` fixtures are NOT hand-typed:
// scripts/make_cluster_history_fixture.py fills a real store through the
// recorder's own write path (`MetricsRecorder.tick`, which rolls tiers 1 and 2
// with its own `rollup`) and then calls
// `routes_cluster.handle_cluster_history`, so these are the handler's bytes.
// scripts/cluster_history_metric_vocabulary.py re-checks every key in them
// against `metrics_history.HOST_COLUMNS` / `ENGINE_KEYS`, and
// scripts/cluster_history_chart_check.sh runs the behavioural half (the
// pixel-width reduction that must not move a min or a max, and the tiers that
// must never be mixed). What is asserted HERE is only the decode shape the
// chart reads off the payload — including the two keys the model gained here
// (`gpu_mem_used_mb`, `prompt_tps`), which the route has always sent.
c.check(ClusterHistoryResponse.self, "v1_cluster_history_6h.json",
        "ClusterHistoryResponse (window=6h -> tier 1, 1-minute averages)")
c.check(ClusterHistoryResponse.self, "v1_cluster_history_7d.json",
        "ClusterHistoryResponse (window=7d -> tier 2, 5-minute averages)")
c.check(ClusterHistoryResponse.self, "v1_cluster_history_6h_engine_only.json",
        "ClusterHistoryResponse (a metrics= projection that excludes the host series)")
c.check(ClusterHistoryResponse.self, "v1_cluster_history_15m.json",
        "ClusterHistoryResponse (window=15m, tier 0, ring not resident here)")
c.check(ClusterHistoryResponse.self, "v1_cluster_history_tier_axis.json",
        "ClusterHistoryResponse (tier axis: tier 1 over an explicit since/until)")

c.assertions("v1_cluster_history_6h.json", "the history decode shape the chart reads") {
    let payload = try JSONDecoder().decode(
        ClusterHistoryResponse.self, from: try c.load("v1_cluster_history_6h.json"))
    var ok = payload.tier == 1 && payload.resolution_s == 60 && payload.resolution == "60s"
    ok = ok && payload.max_fields == ["cpu_temp_c_max", "gpu_temp_c_max", "gpu_power_w_max"]
    // The store's columns are sqlite REAL, so a count of 2 arrives as 2.0; the
    // model's Int? only holds because JSONDecoder converts an integral number
    // losslessly. Pinned here so a stricter decoder strategy fails LOUDLY.
    ok = ok && payload.engines.first?.run == 2 && payload.engines.first?.wait == 1
    // nulls stay null: 192.0.2.11 never reported its GPU in the fixture's ticks
    ok = ok && payload.hosts.contains { $0.host == "192.0.2.11" && $0.gpu_temp_c == nil
        && $0.gpu_power_w == nil }
    // the two keys this card added to the model, and the kv fraction as stored
    ok = ok && payload.hosts.first?.gpu_mem_used_mb == 12000
        && payload.engines.first?.prompt_tps == 200
        && payload.engines.first?.kv == 0.125
    let storeKeys = Mirror(reflecting: payload.store!).children.compactMap { $0.label }
    ok = ok && !storeKeys.contains("path")
    // The two keys t_b611b56e added to the model: which axis answered, and where
    // the rows came from, as a code.
    ok = ok && payload.axis == "window" && payload.source == "sqlite_store"
    // `tier0_resident` is a measurement of the 2 s ring taken inside the route's
    // tier-0 branch alone (hscc-api/routes_cluster.py, PR #417), so a tier-1
    // answer is null: a tier the route never read cannot report residency. A
    // `true` committed here would be a hand-edit rather than a regeneration.
    ok = ok && payload.tier == 1 && payload.tier0Resident == nil
    c.expect("v1_cluster_history_6h.json",
             "tier + resolution stated, peaks named, nulls kept, VRAM MB and prompt tok/s decode, no server path, tier-1 ring unmeasured",
             ok, "tier=\(payload.tier) res=\(payload.resolution) max=\(payload.max_fields ?? []) resident=\(String(describing: payload.tier0Resident))")
}

c.assertions("v1_cluster_history_7d.json", "the 7d tier is stated, not guessed") {
    let payload = try JSONDecoder().decode(
        ClusterHistoryResponse.self, from: try c.load("v1_cluster_history_7d.json"))
    c.expect("v1_cluster_history_7d.json",
             "tier 2 at 300 s, with the same three peak columns available and no tier-0 residency claim",
             payload.tier == 2 && payload.resolution_s == 300 && payload.resolution == "300s"
                && payload.max_fields == ["cpu_temp_c_max", "gpu_temp_c_max", "gpu_power_w_max"]
                && !payload.hosts.isEmpty && !payload.engines.isEmpty
                && payload.tier0Resident == nil,
             "tier=\(payload.tier) res=\(payload.resolution) hosts=\(payload.hosts.count) resident=\(String(describing: payload.tier0Resident))")
}

c.assertions("v1_cluster_history_6h_engine_only.json", "an excluded series is reported as excluded") {
    let payload = try JSONDecoder().decode(
        ClusterHistoryResponse.self, from: try c.load("v1_cluster_history_6h_engine_only.json"))
    c.expect("v1_cluster_history_6h_engine_only.json",
             "hosts_included=false with empty hosts, while the engine series still carries data, and no tier-0 residency claim",
             payload.hosts_included == false && payload.hosts.isEmpty
                && payload.engines_included == true && !payload.engines.isEmpty
                && payload.engines.first?.kv == nil
                && payload.tier == 1 && payload.tier0Resident == nil,
             "hosts_included=\(String(describing: payload.hosts_included)) engines=\(payload.engines.count) resident=\(String(describing: payload.tier0Resident))")
}

c.assertions("v1_cluster_history_15m.json", "an empty tier 0 explains itself") {
    let payload = try JSONDecoder().decode(
        ClusterHistoryResponse.self, from: try c.load("v1_cluster_history_15m.json"))
    c.expect("v1_cluster_history_15m.json",
             "tier 0, ring not resident, no peak columns, and a note that points at window=1h",
             payload.tier == 0 && payload.resolution_s == 2 && payload.tier0Resident == false
                && (payload.max_fields ?? []).isEmpty && payload.hosts.isEmpty
                && (payload.note ?? "").contains("window=1h"),
             "tier=\(payload.tier) resident=\(String(describing: payload.tier0Resident)) note=\(payload.note ?? "")")
}

c.assertions("v1_cluster_history_tier_axis.json",
             "the tier axis is stated, and so is the span it served") {
    let payload = try JSONDecoder().decode(
        ClusterHistoryResponse.self, from: try c.load("v1_cluster_history_tier_axis.json"))
    // The axis must be READ off the payload, not inferred: on this axis `window`
    // is a derived span label, and a surface that parsed it as a preset would
    // plot a 1-hour chart from a 10-minute answer. `since`/`until` are the pair
    // the server actually served (it clamps to the tier's retention), so the
    // chart draws those and not the ones it asked for.
    let span = payload.window_s ?? -1
    c.expect("v1_cluster_history_tier_axis.json",
             "axis=tier, source stated as a code, window a derived span label, and since/until the served pair",
             payload.axis == "tier" && payload.source == "sqlite_store"
                && payload.tier == 1 && payload.resolution_s == 60
                && payload.window == "\(span)s"
                && !["15m", "1h", "6h", "24h", "7d"].contains(payload.window)
                && payload.until == payload.since + span && span > 0
                && !payload.hosts.isEmpty && !payload.engines.isEmpty,
             "axis=\(String(describing: payload.axis)) source=\(String(describing: payload.source)) window=\(payload.window) since=\(payload.since) until=\(payload.until)")
}
// ---- Monitor Live Activity surfaces (t_1685f884) — same arrangement: the
// checks are in monitor_surfaces.swift, the calls are here. They exercise
// `MonitorSurfaceReading`, the code the Lock Screen, compact, expanded and
// minimal surfaces actually call, against the same goldens — so a surface that
// picks the wrong column, invents a fleet average, colours an absent metric, or
// truncates silently fails here rather than on a lock screen.
monitorActivitySurfaces(c)
monitorActivityEdgeSurfaces(c)
monitorActivityGlanceLadder(c)

// ---- Per-widget decode blocks (one file per widget).
checkWidgetDecodeFixtures(c)

// ---- Summary + exit code. A single failure is a non-zero exit — a guard that
// exits 0 here has proven, right now, that every committed fixture decodes
// against the real models. The exit code is the guard; the words below only
// report, and they name their UNITS: `passed` is CHECKS, `fixturesRead` is
// FIXTURES (fewer, because one fixture can carry several checks and a negative
// fixture contributes a check while being one file). Both are read off this run,
// never typed — the README defers to this line as the authority for how many are
// green, so a wrong unit here would ship a wrong unit into the docs.
print("")
if c.failures.isEmpty {
    print("ALL DECODE CHECKS PASSED — \(c.passed)/\(c.passed) checks (over \(c.fixturesRead.count) committed fixtures) decoded against the real models")
    exit(0)
} else {
    print("DECODE FAILURES — \(c.failures.count) check(s) did not pass against the real models:")
    for f in c.failures {
        print("  ✗ \(f.fileName) → \(f.type): \(f.error)")
    }
    exit(1)
}
