import Foundation
import SwiftUI

// ---------------------------------------------------------------------------
// SharedModels.swift — the wire payloads every HSCC surface reads.
//
// The 2026-11-17 pivot made this a widget + Live Activity client: the app keeps
// connection settings and one config screen, and every number on a surface comes
// from one of the five datasets of `docs/WIDGET_DATA_CONTRACT.md` §3. This file
// is that vocabulary, ordered by dataset, each type anchored to the route and the
// contract section that sized it.
//
//   * Nothing here is persisted. `SharedStore.swift` owns the App Group identifier
//     and every value written to the container; this file names neither the group
//     nor `UserDefaults` nor the keychain. The monitor node type is `Codable` to
//     DECODE only: §7 (a node's LAN `host` must never reach the container) is why
//     `MonitorSnapshotStore` persists `MonitorNodeEntry` — its projection, declared
//     in `SharedStore.swift` — and `project(_:)` there is the only path from this
//     file's wire type to storage. `displayLabel` is the node's only renderable
//     form. `MonitorDaemonHost` IS still encoded into the snapshot: it holds no
//     address (its `hostname` is the daemon host's own name, §5.5) and
//     `model_decode_check.sh` asserts that, key by key.
//   * Nothing here acts: no request builder, no mutation type. `ExtensionClient`
//     is GET-only by construction.
//   * Redaction is STRUCTURAL, not a scrub (§7): a value that must never reach a
//     view or the container is declared nowhere, so it is never decoded —
//     `projects[].repo`, `cards[].body`/`workspace_path`/`branch`,
//     `blocked.tasks[].why`/`.comments`, the kanban `boards[]`/`errors[]`,
//     monitor's `output` table string.
//
// Compiled into all five targets through `project.yml`, so extensions read exactly
// what the app decodes. Foundation + SwiftUI only: no extension may drag in
// app-only code.
//
// NAMING — the `Lite` suffix. The console's `Sources/HSCC/Models.swift` compiles
// into the app target ALONGSIDE this file, so a name it owns (`ProjectsResponse`,
// `KanbanStaleResponse`, `ClusterMonitorResponse`) is an invalid redeclaration
// here: a widget-local decoder for a route the console also reads takes `Lite`
// (`KanbanRunningLite` set the precedent); a payload only the extensions read
// keeps its contract name (`MobileSummaryResponse`). `model_decode_check.sh`
// compiles BOTH files against the fixtures, so a collision is a gate failure,
// never a runtime surprise.
// ---------------------------------------------------------------------------

/// Protocol for any read response that carries the API's first-class `speak`
/// field (design §B). Declared here (not in `Models.swift`) so the extensions
/// can conform too without importing the whole app model file.
protocol Speakable {
    var speak: String { get }
}


// ---------------------------------------------------------------------------
// Dataset 1 — projects · GET /v1/projects · §3 row 1, §5.1 (`reuse`)
// ---------------------------------------------------------------------------

/// GET /v1/projects — 1,854 B for 18 projects (§4.1); a surface draws at most 8
/// rows of `{name, board}` (715 B), so those are the only keys declared. `repo`
/// (an absolute path on the operator's machine — 58% of the array's bytes) and
/// `topic` ride on the wire and are ignored by `Decodable`, so they can never be
/// rendered or reach the container. `model_decode_check` proves it against the
/// real `v1_projects.json`, which carries `repo` on every row.
///
/// NAMING: `ProjectsResponse`/`Project` belong to `Sources/HSCC/Models.swift` —
/// see the `Lite` naming rule in the header.
struct ProjectsLite: Decodable {
    let projects: [ProjectLite]
    let count: Int?
    let speak: String?

    /// Rows in server order, capped to the family (§5.1: small 3, medium 6, large 8).
    func rows(limit: Int) -> [ProjectLite] { Array(projects.prefix(limit)) }
}

/// One project — the two keys every §5.1 family can render. `board` is optional
/// because the console's `Project` treats it that way: no board renders as the
/// name alone instead of failing the decode and blanking the surface.
struct ProjectLite: Decodable, Identifiable {
    let name: String
    let board: String?

    var id: String { name }
}


// ---------------------------------------------------------------------------
// Dataset 2 — board status counts · GET /v1/mobile/summary · §3 row 2, §5.2, §6
// ---------------------------------------------------------------------------
//
// The one dataset no existing route can serve: the open four cost 81,855 B from
// `/v1/cards`, `done` is reachable only via `?include_archived=1` (8.6 MB), and
// `/v1/cards?status=done` answers with ZERO cards — so a widget that inferred
// counts from a list would render `done: 0` forever. `GET /v1/mobile/summary`
// (§6, `hscc-api/routes_mobile.py`) counts them server-side: all boards, one
// request, inside the 4 KB Live-Activity ceiling.

/// GET /v1/mobile/summary — shape anchored to `routes_mobile.py::_assemble`.
/// On the degrade path (§6 rule 7) the route sends `generated_at` + `speak` and
/// NO `boards` key, because empty counts would read as "every board is quiet".
/// So `boards` is optional, `degraded` names the case, and a degraded response
/// falls through to the last-known snapshot with its age — never to zeros.
struct MobileSummaryResponse: Decodable, Speakable {
    /// When the server built the payload — the age a stale tile shows.
    let generated_at: String
    /// Absent when degraded: nil is NOT "there are no boards".
    let boards: [MobileSummaryBoard]?
    let board_count: Int?
    let total_boards: Int?
    /// Set when the 4 KB budget trimmed the tail — announced, never silent.
    let truncated: Bool?
    let omitted_boards: Int?
    /// Boards the server could not read, as a COUNT only: an unusable slug is
    /// never echoed back (§6 rule 4).
    let unreadable_boards: Int?
    let speak: String

    var degraded: Bool { boards == nil }

    /// Tiles to draw. Empty only when degraded or the host truly has no boards,
    /// so check `degraded` before concluding anything from an empty list.
    var entries: [MobileSummaryBoard] { boards ?? [] }

    /// nil means this payload did not carry the board (e.g. omitted for size),
    /// which is not the same as the board being empty.
    func board(_ slug: String) -> MobileSummaryBoard? { entries.first { $0.board == slug } }
}

/// One board's tile. `counts`+`total` are present exactly when `error` is not:
/// with `?boards=` one unreadable board degrades on its own, and that error must
/// never be rendered as a zero.
struct MobileSummaryBoard: Decodable, Identifiable {
    let board: String
    let counts: BoardStatusCounts?
    let total: Int?
    let error: String?

    var id: String { board }

    /// Open cards = `total` minus the terminal statuses — the server's own
    /// `_open_total`, so a status folded into `other` still counts as open.
    var openTotal: Int? {
        guard let total = total, let counts = counts else { return nil }
        return total - counts.done - counts.archived
    }
}

/// The five statuses a tile draws, plus `archived` — where `done` actually lives,
/// since `list_cards` excludes closed cards unless archived (§4.2).
///
/// EVERY key is a REQUIRED Int: §6 rule 1, "a missing key is a bug, zero is a
/// value". If the server ever omits `done` this decode THROWS and the surface
/// keeps its last-known snapshot, instead of quietly rendering `done: 0` forever.
/// `model_decode_check` asserts exactly that with a fixture missing `done`.
struct BoardStatusCounts: Decodable {
    let todo: Int
    let ready: Int
    let running: Int
    let blocked: Int
    let done: Int
    let archived: Int
    /// Cards whose status is outside the six; the server folds them so `total`
    /// equals the sum of what it reports. Absent means none, not unknown.
    let other: Int?

    /// The operands of the `accessoryInline` line ("17 todo · 6 run").
    var openCount: Int { todo + ready + running + blocked }
    /// §5.2's colour rule for the circular/rectangular families.
    var hasBlocked: Bool { blocked > 0 }
}


// ---------------------------------------------------------------------------
// Datasets 3 + 4 — the kanban trio · GET /v1/kanban/{running,blocked,stale}
// · §3 rows 3-4, §5.3 + §5.4 (both `narrow client-side`)
// ---------------------------------------------------------------------------
//
// The cheapest per-row source in the API (11,745 B over three reads, vs 81,855 B
// for `/v1/cards`), and the ONLY sanctioned card-list route: §8.3 — "any card
// that wants a card LIST uses the kanban trio, not `/v1/cards`".
// `stale?older_than=0` already carries every non-terminal card with
// title/status/board/age_days and no `body` — dataset 4's row projection,
// ~15x cheaper per row because the projection happens server-side.
//
// Dropped structurally (declared nowhere, so never decoded — §7's redaction is
// not a scrub): `boards[]` (264 B of board names, duplicated in two envelopes),
// `errors[]` (a server error line is not widget copy), `older_than` (echoes our
// own query param), and running's `pid`/`host_local` (they existed to show what
// a kill would stop; every surface here is read-only). Two facts §4.3 measured:
// `?board=` is IGNORED by `/v1/kanban/running`, so board-scope client-side and
// do not add the param expecting a saving; and `blocked.tasks[].why`/`.comments`
// stay undecoded — a large surface gets `block_kind`, not the block prose.

/// GET /v1/kanban/running — cards being worked right now, fleet-wide.
/// Optional fields everywhere but the row `id`, so a server-side key change
/// degrades one row instead of blanking the surface.
struct KanbanRunningLite: Decodable {
    let tasks: [RunningCardLite]?
    let count: Int?
    let speak: String?
    /// The registered board names (measured: a bare list of 23 strings, 264 B —
    /// §4.3). Decoded because the board/project pickers need the list and this
    /// is the cheapest sanctioned source for it; optional so a payload without
    /// the key still decodes.
    let boards: [String]?

    func rows(limit: Int) -> [RunningCardLite] { Array((tasks ?? []).prefix(limit)) }
}

/// One running task (`handle_kanban_running`'s keys, minus `pid`/`host_local`).
struct RunningCardLite: Decodable, Identifiable {
    let board: String?
    let id: String
    let title: String?
    let assignee: String?
    let status: String?
    let started_at: String?
}

/// GET /v1/kanban/blocked — work stuck waiting: the widget's failure signal.
struct KanbanBlockedLite: Decodable {
    let tasks: [BlockedCardLite]?
    let count: Int?
    let speak: String?

    /// At most `limit` rows, stuck longest first (§5.3's row order).
    func rows(limit: Int) -> [BlockedCardLite] {
        Array((tasks ?? []).sorted { ($0.age_days ?? 0) > ($1.age_days ?? 0) }.prefix(limit))
    }
}

/// One blocked card. `block_kind` is the reason's category (`needs_input`,
/// `dependency`, …); `why` and `comments` are deliberately not decoded — card
/// prose may quote anything, and §7 keeps it out of an extension's hands.
struct BlockedCardLite: Decodable, Identifiable {
    let board: String?
    let id: String
    let status: String?
    let assignee: String?
    let age_days: Int?
    let block_kind: String?
    let title: String?
}

/// GET /v1/kanban/stale?older_than=0 — every non-terminal card, fleet-wide.
/// The queue-depth tile counts `ready`/`todo` rows here; the cards widget draws
/// the same rows as its list (§5.4).
struct KanbanStaleLite: Decodable {
    let tasks: [StaleCardLite]?
    let count: Int?
    let speak: String?

    /// At most `limit` rows, oldest first — the sort §5.3 binds on row lists.
    func rows(limit: Int) -> [StaleCardLite] {
        Array((tasks ?? []).sorted { ($0.age_days ?? 0) > ($1.age_days ?? 0) }.prefix(limit))
    }
}

/// One non-terminal card; `status` is what the queue-depth count keys on.
struct StaleCardLite: Decodable, Identifiable {
    let board: String?
    let id: String
    let status: String?
    let assignee: String?
    let age_days: Int?
    let title: String?
}


// ---------------------------------------------------------------------------
// Dataset 5 — cluster monitor · GET /v1/cluster/monitor + GET /v1/daemon/host
// · §3 row 5, §5.5 (`narrow client-side`)
// ---------------------------------------------------------------------------
//
// Up-state costs 10,747 B for 4 nodes and 54% of it is `output`, the ANSI table
// string a view cannot use — so `output` is simply not declared, and the rows a
// surface keeps come to 225 B + the 439 B daemon-host sidecar. When the fleet is
// DOWN the route degrades to `{"speak": ...}` with no `json` key (38 B): that is
// why `json` is optional and the surface falls back to its last-known snapshot
// with an age, as `MonitorWidget` already does.

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

    /// The node's display label: the sample hostname when the API carried one,
    /// otherwise the last octet of `host` — never the whole address (§7). This is
    /// the single rule every surface uses; `MonitorViews` used to carry its own
    /// copy of it. A configured label beats it.
    var displayLabel: String {
        if let hostname = sample?.hostname, !hostname.isEmpty { return hostname }
        return Self.shortTail(host)
    }

    /// Last octet of the first dotted-quad, as ".NN". Same derivation as
    /// `shortTail` in scripts/widget_datapath_live/template.swift, so widget,
    /// Live Activity and live probe can never disagree on a node's identity.
    static func shortTail(_ line: String) -> String {
        let ips = line.split(whereSeparator: { !$0.isNumber && $0 != "." })
            .map(String.init).filter { $0.split(separator: ".").count == 4 }
        guard let ip = ips.first else { return line.isEmpty ? "?" : line }
        let tail = ip.split(separator: ".").last.map(String.init) ?? ip
        return "." + tail
    }
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
// Shared view state, not wire payloads: the cluster state a surface names, its
// colour, and the node strip. Here rather than in the app target so the
// extensions render the same state without re-declaring it. `TopologyNode.label`
// is already §7's redacted identity (a ".244" tail, never an address), which is
// what lets `SnapshotStore` persist the strip as it stands.
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


// ---------------------------------------------------------------------------
// Dataset 5's chrome — GET /v1/cluster/status + GET /v1/autodown/status: the
// state that starts and ends the Live Activities, and the "N hosts up, M models"
// line the cluster widget and the Siri intent read. §5.5 sizes a surface's need
// at ~40 B of the 926 B the route costs (`state`, `enabled`, `idle_minutes`,
// `speak`).
// ---------------------------------------------------------------------------

// RETIRE (console retirement D2 — Fleet, Nodes & serving): the full
// `ClusterWorkload` shape (`tp`/`pp`/`container_id`) is read only by FleetView,
// NodesServingView, FleetServing and ServingPlacement, all on the retirement
// list; the widget uses `workloads.count` alone for its "models" metric. These
// fields leave in the commit that takes those screens with `fleet_models_check.sh`
// and `serving_placement_check.sh`.

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

// RETIRE (console retirement D3 — Autodown): `blocked_by`, `reason`,
// `kanban_ok`/`kanban_reason`, `watchdog_*`, `force_armed*`, `active_cron*`,
// `last_activity_iso`, `wake_source`, `down_since` are read only by AutodownView.
// §7 keeps `blocked_by` (398 B of internal prose) out of shared storage and out of
// every widget. These leave with that screen and its fixtures/decode blocks.

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

