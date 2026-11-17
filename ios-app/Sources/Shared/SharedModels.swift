import Foundation
import SwiftUI

// ---------------------------------------------------------------------------
// Shared cross-target glue for the HSCC app + its extensions.
//
// Compiled into ALL three bundles (app, HSCCWidgets, HSCCLiveActivity) so
// every surface reads the SAME connection settings and the SAME cluster state.
// The app Group is the seam that lets an extension see what the app stores.
//
// Keep this file self-contained (no imports beyond Foundation/SwiftUI) so any
// target can compile it without dragging in app-only code.
// ---------------------------------------------------------------------------

/// Protocol for any read response that carries the API's first-class `speak`
/// field (design §B). Declared here (not in `Models.swift`) so the extensions
/// can conform too without importing the whole app model file.
protocol Speakable {
    var speak: String { get }
}

/// One workload entry from GET /v1/cluster/status.
struct ClusterWorkload: Decodable, Identifiable {
    let name: String
    let tp: String?
    let pp: String?
    let container_id: String?

    var id: String { name }
}

/// GET /v1/cluster/status — workloads + idle/total hosts.
struct ClusterStatusResponse: Decodable, Speakable {
    let workloads: [ClusterWorkload]
    let idle_hosts: [String]
    let total_hosts: Int
    let speak: String
}

/// GET /v1/autodown/status — the autodown report (drives the widget state and
/// the Live Activity's start/end).
///
/// Verified live shape:
///   { enabled, state, idle_minutes, last_activity_iso, down_since,
///     wake_source, reason, watchdog_blocked, watchdog_intentional,
///     kanban_ok, kanban_reason, blocked_by, force_armed,
///     force_armed_overrides, active_cron_cpu_only, active_cron_model, speak }
/// Every field is optional except `speak` (which the API always synthesizes).
struct AutodownStatusResponse: Decodable, Speakable {
    let enabled: Bool?
    let state: String?
    let idle_minutes: Int?
    let last_activity_iso: String?
    let down_since: String?
    let wake_source: String?
    let reason: String?
    let watchdog_blocked: Bool?
    /// The watchdog block's `intentional` marker — a STRING ("autodown") when a
    /// teardown is in effect, absent otherwise. Not a Bool: the server passes
    /// `block.get("intentional")` through verbatim (routes_autodown.py:216).
    let watchdog_intentional: String?
    let kanban_ok: Bool?
    let kanban_reason: String?
    let blocked_by: String?
    let force_armed: Bool?
    let force_armed_overrides: [String]?
    let active_cron_cpu_only: [String]?
    let active_cron_model: [String]?
    let speak: String
}

// ---------------------------------------------------------------------------
// Kanban board work signals (cluster-wide card counts for the widget)
// ---------------------------------------------------------------------------
//
// NOTE ON NAMING: these deliberately carry the `Lite` suffix and do NOT reuse
// the app-only names (`KanbanBlockedResponse`, `KanbanStaleResponse` live in
// Sources/HSCC/Models.swift — the app target compiles BOTH Models.swift AND
// SharedModels.swift, so reusing those names is an "invalid redeclaration").
// The widget target compiles ONLY SharedModels.swift, so these are its own
// minimal decoders.

/// GET /v1/kanban/running — running tasks across ALL boards + kill detail.
///
/// This is the strongest "what is the fleet DOING" signal: it names exactly
/// what a kill would stop, each running task carrying its board, id, title,
/// assignee, and PID (real worker processes). The widget shows `count` — cards
/// actually being worked right now. Fields kept optional so a decode skips
/// gracefully if the server adds/removes keys.
struct KanbanRunningLite: Decodable {
    let boards: [String]?
    let tasks: [RunningCardLite]?
    let errors: [String]?
    let count: Int?
    let speak: String?
}

/// One running card from /v1/kanban/running. The widget only needs `count`
/// from the envelope, but the per-card shape is decoded so the field set stays
/// anchored to the real API (routes_kanban.py handle_kanban_running):
/// `{ board, id, title, assignee, status, pid, host_local, started_at }`.
struct RunningCardLite: Decodable {
    let board: String?
    let id: String
    let title: String?
    let assignee: String?
    let status: String?
    let pid: Int?
    let host_local: Bool?
    let started_at: String?
}

/// GET /v1/kanban/blocked — blocked cards across ALL boards + why.
///
/// Blocked = work stuck waiting (a failure / attention signal) — the widget's
/// "failure indicator". `count` is the number of blocked cards. Shape from
/// routes_kanban.py: `{ boards:Int, tasks, errors, count }`.
struct KanbanBlockedLite: Decodable {
    let boards: Int?
    let tasks: [BlockedCardLite]?
    let errors: [String]?
    let count: Int?
    let speak: String?
}

/// One blocked card. Only `count` drives the widget, but the per-card shape is
/// decoded to stay anchored to the API: `{ board, id, status, assignee,
/// age_days, block_kind, why, title, comments }`.
struct BlockedCardLite: Decodable {
    let board: String?
    let id: String
    let status: String?
    let assignee: String?
    let age_days: Int?
    let block_kind: String?
    let title: String?
}

/// GET /v1/kanban/stale?older_than=0 — every non-terminal card across ALL
/// boards, with per-card `status`. The widget derives BOARD QUEUE DEPTH from
/// this: cards whose status is `ready`/`todo` are sitting in the queue waiting
/// to be picked up. Shape from autodown.list_stale_tasks (autodown.py:439):
/// `{ board, id, status, assignee, age_days, title }`, envelope
/// `{ boards:[String], tasks, errors, older_than, count }`.
struct KanbanStaleLite: Decodable {
    let boards: [String]?
    let tasks: [StaleCardLite]?
    let errors: [String]?
    let older_than: Int?
    let count: Int?
    let speak: String?
}

/// One non-terminal card from /v1/kanban/stale. `status` is what we actually
/// read — the widget counts `ready`/`todo` as queued work.
struct StaleCardLite: Decodable {
    let board: String?
    let id: String
    let status: String?
    let assignee: String?
    let age_days: Int?
    let title: String?
}

// ---------------------------------------------------------------------------
// Monitor widget models (t_6d545375): /v1/cluster/monitor + /v1/daemon/host
// ---------------------------------------------------------------------------
//
// NAMING NOTE: the app's own decoder for /v1/cluster/monitor
// (`ClusterMonitorResponse` / `NodeSample`) lives in Sources/HSCC/Models.swift
// — the app target compiles BOTH Models.swift AND this file, so reusing those
// names here is an invalid redeclaration. Following the `Lite` precedent above
// (kanban decoders), this file carries the widget target's OWN minimal
// decoders under the `Monitor` prefix. The app currently renders no monitor
// UI, so the two never both decode the same payload in the same target.

/// GET /v1/cluster/monitor — the `hscc cluster monitor` snapshot.
///
/// `handle_cluster_monitor` wraps the sparkrun payload:
///   { "success": true, "output": "<string>", "json": { "timestamp": Double,
///     "hosts": [ { "host": "<ip>", "error": null, "sample": { ... },
///                  "used_slots": 1, "free_slots": 0 } ] }, "speak": "..." }
/// When the cluster is unreachable the route degrades to `{ "speak": "fleet
/// monitor unavailable" }` — no `json` — so `json` is optional here. Every
/// numeric field inside `sample` arrives as a STRING, often `""` when a metric
/// is unreadable on that node; parsed defensively via `MonitorSample`'s
/// computed `Double?` accessors (a bad/empty string yields nil, never a
/// decode failure that blanks the whole route).
struct MonitorResponse: Codable, Speakable {
    /// The nested per-node payload. Absent when the route degraded.
    let json: MonitorPayload?
    let speak: String

    /// Computed passthrough so the widget reads the node list directly.
    var nodes: [MonitorNode] { json?.hosts ?? [] }
}

/// The nested `json` payload of /v1/cluster/monitor.
struct MonitorPayload: Codable {
    let timestamp: Double?
    let hosts: [MonitorNode]
}

/// One node's entry. `error` is non-null when the node could not be sampled;
/// `sample` is then absent.
struct MonitorNode: Codable, Identifiable {
    /// The node's address as reported by the API (e.g. a LAN IP).
    let host: String
    let error: String?
    let sample: MonitorSample?
    let used_slots: Int?
    let free_slots: Int?

    var id: String { host }
}

/// The per-node `sample` dict — all numeric metrics are STRINGS (may be empty
/// `""` when a metric is unreadable). Parsed via the computed `Double?`
/// accessors.
struct MonitorSample: Codable {
    let hostname: String?
    let uptime_sec: String?
    let cpu_usage_pct: String?
    let mem_used_pct: String?
    let mem_total_mb: String?
    let mem_used_mb: String?
    let gpu_name: String?
    let gpu_util_pct: String?
    let gpu_mem_used_pct: String?

    // MARK: Parsed numeric accessors (nil when unreadable/empty)

    var uptimeSeconds: Double? { Self.num(uptime_sec) }
    var cpuUsagePct: Double? { Self.num(cpu_usage_pct) }
    var memUsedPct: Double? { Self.num(mem_used_pct) }
    var memTotalMB: Double? { Self.num(mem_total_mb) }
    var memUsedMB: Double? { Self.num(mem_used_mb) }
    var gpuUtilPct: Double? { Self.num(gpu_util_pct) }
    var gpuMemUsedPct: Double? { Self.num(gpu_mem_used_pct) }

    /// A human uptime ("2d 11h") from `uptime_sec`, or nil when unreadable.
    var uptimeText: String? {
        guard let s = uptimeSeconds, s >= 0 else { return nil }
        let total = Int(s)
        let days = total / 86400
        let hours = (total % 86400) / 3600
        if days > 0 { return "\(days)d \(hours)h" }
        let mins = (total % 3600) / 60
        if hours > 0 { return "\(hours)h \(mins)m" }
        return "\(mins)m"
    }

    /// Parse a numeric STRING ('2.84', '77.0', '0') to Double, or nil.
    private static func num(_ raw: String?) -> Double? {
        guard let raw, !raw.isEmpty else { return nil }
        return Double(raw)
    }
}

/// GET /v1/daemon/host — the machine HSCC runs on (the operator's Mac when the
/// Mac is remote from the DGX cluster). The API server runs on the daemon host,
/// so this is `platform`/`psutil`-local to it (see hscc-api/routes_ops.py
/// `_backing_daemon_host`).
///
/// Verified live shape (200, macOS arm64):
///   { "hostname": String, "platform": "macOS", "arch": "arm64",
///     "uptime_seconds": Int, "cpu": { "count", "percent", "load_avg": [] },
///     "memory": { "total_gb", "used_gb", "percent" },
///     "disk": { "total_gb", "used_gb", "percent" },
///     "processes": Int, "daemon_running": Bool,
///     "daemon_uptime_seconds": Int, "speak": "..." }
///
/// NAMING: the sibling monitor Live Activity card (t_03d318cd) declared a
/// `DaemonHostResponse` in Sources/HSCC/ (app target) which is ALSO compiled
/// into this file, so the exact name collides. The widget's own wire struct
/// uses the `Monitor` prefix (widget-local, per the Lite precedent) to live
/// beside it without a duplicate-redeclaration.
struct MonitorDaemonHost: Codable, Speakable {
    let hostname: String?
    let platform: String?
    let arch: String?
    let uptime_seconds: Int?
    let cpu: MonitorDaemonCPU?
    let memory: MonitorDaemonMemory?
    let disk: MonitorDaemonDisk?
    let processes: Int?
    let daemon_running: Bool?
    let daemon_uptime_seconds: Int?
    let speak: String

    var cpuPercent: Double? { cpu?.percent }
    var cpuCount: Int? { cpu?.count }
    var memUsedGB: Double? { memory?.used_gb }
    var memTotalGB: Double? { memory?.total_gb }
    var memPercent: Double? { memory?.percent }
    var diskUsedGB: Double? { disk?.used_gb }
    var diskTotalGB: Double? { disk?.total_gb }
    var diskPercent: Double? { disk?.percent }

    /// A human uptime ("2d 11h") from `uptime_seconds`, or nil.
    var uptimeText: String? {
        guard let s = uptime_seconds, s >= 0 else { return nil }
        let d = s / 86400
        let h = (s % 86400) / 3600
        if d > 0 { return "\(d)d \(h)h" }
        let m = (s % 3600) / 60
        if h > 0 { return "\(h)h \(m)m" }
        return "\(m)m"
    }
}

struct MonitorDaemonCPU: Codable {
    let count: Int?
    let percent: Double?
    let load_avg: [Double]?
}

struct MonitorDaemonMemory: Codable {
    let total_gb: Double?
    let used_gb: Double?
    let percent: Double?
}

struct MonitorDaemonDisk: Codable {
    let total_gb: Double?
    let used_gb: Double?
    let percent: Double?
}


// ---------------------------------------------------------------------------
// Cluster state
// ---------------------------------------------------------------------------

/// The top-level cluster state the widget/Live Activity surface.
/// `unreachable` is a FIRST-CLASS state (never an error/local fallback): the
/// cluster can't be reached, and we say so alongside the last-known state.
enum ClusterState: String {
    case serving      // autodown state == up
    case waking       // autodown state == waking
    case down         // autodown state == down
    case unreachable  // the API can't be reached
    case unknown      // no signal at all yet

    /// Sentence-case, active copy for this state.
    var label: String {
        switch self {
        case .serving: return "Serving"
        case .waking: return "Waking"
        case .down: return "Down"
        case .unreachable: return "Can't reach the cluster"
        case .unknown: return "Unknown"
        }
    }

    var color: Color {
        switch self {
        case .serving: return Theme.Semantic.ok
        case .waking: return Theme.Semantic.warn
        case .down: return Theme.Semantic.bad
        case .unreachable: return Theme.Semantic.neutral
        case .unknown: return Theme.Semantic.neutral
        }
    }
}

// ---------------------------------------------------------------------------
// Topology models (moved here so the app widget + Live Activity can reuse the
// app's canonical topology without re-declaring it).
// ---------------------------------------------------------------------------

/// One serving pair in the topology strip — two nodes + a role label.
struct TopologyPair: Identifiable {
    /// A stable identity for the pair (the joining node labels).
    var id: String { "\(nodes[0].label)-\(nodes[1].label)" }
    let nodes: [TopologyNode]       // exactly 2
    let role: String                // e.g. "orchestrator" / "worker"
}

/// One node in the topology strip: its displayed label (the short ip tail) and
/// its live state.
struct TopologyNode: Identifiable {
    let label: String               // e.g. ".244"
    let state: NodeState

    var id: String { label }

    /// The live serving state of a node, driving its dot colour.
    enum NodeState: String {
        /// Serving / healthy.
        case up
        /// Busy / waking / in transition.
        case busy
        /// Warning / degraded.
        case warn
        /// Down / faulted.
        case down
        /// No live signal yet.
        case unknown

        var color: Color {
            switch self {
            case .up: return Theme.Semantic.ok
            case .busy: return Theme.Semantic.warn
            case .warn: return Theme.Semantic.warn
            case .down: return Theme.Semantic.bad
            case .unknown: return Theme.Semantic.neutral
            }
        }
    }
}

