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

/// One project — the registry row plus the board truth the Projects widget
/// draws. `board` and every status field are optional because an older server
/// (the live 2.15.11 today) sends only `{name, board, repo, topic}`; a missing
/// count renders as "—", never as a fabricated zero.
struct ProjectLite: Decodable, Identifiable {
    let name: String
    let board: String?
    /// `hscc project list`'s health token: ok | partial | absent.
    let health: String?
    /// The project board's status histogram, the same projection
    /// `/v1/mobile/summary` serves. nil when the board read failed.
    let counts: ProjectCounts?
    /// Newest parseable card timestamp on the board, epoch seconds.
    let lastActivity: Int?
    /// A live orchestrator turn, nil when the orchestrator is idle.
    let orchestrator: ProjectOrchestrator?

    enum CodingKeys: String, CodingKey {
        case name, board, health, counts, orchestrator
        case lastActivity = "last_activity"
    }

    init(name: String, board: String?, health: String? = nil,
         counts: ProjectCounts? = nil, lastActivity: Int? = nil,
         orchestrator: ProjectOrchestrator? = nil) {
        self.name = name
        self.board = board
        self.health = health
        self.counts = counts
        self.lastActivity = lastActivity
        self.orchestrator = orchestrator
    }

    var id: String { name }
}

/// A project board's status counts. Every field is optional so a partial
/// payload cannot blank the row; the view omits what the server did not send.
struct ProjectCounts: Codable, Equatable, Hashable, Sendable {
    let todo: Int?
    let ready: Int?
    let running: Int?
    let blocked: Int?
    let done: Int?
    let archived: Int?
    let total: Int?

    /// The three numbers the Projects widget promises (CARD A3). Missing keys
    /// are omitted, not printed as zero.
    var statusLine: String? {
        var parts: [String] = []
        if let running { parts.append("\(running) run") }
        if let blocked { parts.append("\(blocked) blocked") }
        if let ready { parts.append("\(ready) ready") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}

/// One live orchestrator turn from `hscc kanban monitor`'s own row shape.
struct ProjectOrchestrator: Codable, Equatable, Hashable, Sendable {
    let profile: String?
    let session: String?
    let state: String?
    let turnSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case profile, session, state
        case turnSeconds = "turn_s"
    }

    /// The state the CLI shows, or the profile when the store had no prose.
    var display: String? {
        if let state, !state.isEmpty { return state }
        if let profile, !profile.isEmpty { return profile }
        return nil
    }
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
///
/// `worker` / `run_s` / `hb_s` are the three fields `kanban_live.card_view()`
/// sends from `CARD_VIEW_FIELDS` and `hscc cards monitor` draws:
///
///   * `worker` — `collect()`'s `r["profile"] or r["assignee"]`, the profile
///     actually running the card (the CLI panel header's `worker` column).
///     `assignee` is the card's assignment and is only the fallback for an
///     older server, exactly the way `kanban_live.py` builds it.
///   * `run_s` — seconds since the card started, the CLI's `run` column.
///   * `hb_s` — seconds since the worker's last heartbeat, the CLI's `heartbeat`
///     column. Decoded for completeness; the widget shows run time only.
///
/// All three are optional because the live 2.15.11 server predates the route
/// fix and sends only `assignee` + `started_at`; a missing field is "the server
/// did not send it", never zero.
struct RunningCardLite: Decodable, Identifiable {
    let board: String?
    let id: String
    let title: String?
    let worker: String?
    let assignee: String?
    let status: String?
    let run_s: Double?
    let hb_s: Double?
    let started_at: String?

    init(board: String?, id: String, title: String?, worker: String? = nil,
         assignee: String?, status: String?, run_s: Double? = nil,
         hb_s: Double? = nil, started_at: String?) {
        self.board = board
        self.id = id
        self.title = title
        self.worker = worker
        self.assignee = assignee
        self.status = status
        self.run_s = run_s
        self.hb_s = hb_s
        self.started_at = started_at
    }
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
    /// The CLI's serving table: one row per (backend, model). Additive on the
    /// route; absent only from an older server, which is not the same as "zero
    /// models" (see `ServingSummary`).
    let serving: [ServingRow]?
    /// Why the serving poll failed, when it did. Nil on a clean poll.
    let serving_error: String?
    /// Why the proxy route map could not be refreshed. Nil on a clean poll.
    let serving_proxy_error: String?
    let speak: String

    /// Computed passthrough so the widget reads the node list directly.
    var nodes: [MonitorNode] { json?.hosts ?? [] }
}

/// One row of `hscc cluster monitor`'s serving table: the model a backend is
/// running plus the load and token rates the CLI prints beside it.
struct ServingRow: Codable, Identifiable {
    let node: String?
    let port: String?
    let model: String?
    let routes: [String]?
    let running: Int?
    let waiting: Int?
    let kv: Double?
    let gen_tps: Double?
    let prompt_tps: Double?
    let prompt_total: Double?
    let gen_total: Double?
    let error: String?

    var id: String { "\(node ?? "?")-\(port ?? "?")-\(model ?? "?")" }

    /// The CLI prints "down" when the backend could not be scraped. A row with
    /// a model but an error is not a served model.
    var isDown: Bool { error != nil || model == nil || model == "-" }

    /// `org/model` -> `model`, the same shortening the CLI table applies.
    var shortModel: String? {
        guard let model, !model.isEmpty, model != "-" else { return nil }
        return model.components(separatedBy: "/").last ?? model
    }
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
    /// What this host is serving (t_56b910c8). NO default here on purpose: a
    /// stored property with an initial value is silently NOT decoded (the
    /// compiler warns "immutable property will not be decoded"), which would
    /// have made every model count read `nil` while still compiling. Call sites
    /// that build placeholder nodes pass it explicitly.
    let workloads: [MonitorWorkload]?

    var id: String { host }

    /// Whether to count this host as UP. The honest reading of the wire: the
    /// route lists every host it tried to sample and puts the failure in
    /// `error`, so a host with no error answered — a missing `sample` is a
    /// metric gap on a live host, not a host that is down.
    var isUp: Bool { error == nil }

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

/// One workload as `/v1/cluster/monitor` reports it under each host.
///
/// ONLY `cluster_id` is declared. The sibling keys in the same object are
/// `recipe` (an absolute path on the operator's machine), `containers[].name`
/// (a workload name) and `image` — three §7 redaction items — and a key that is
/// not declared is never decoded, so they can be neither rendered nor persisted
/// (structural redaction, not a scrub). Counting DISTINCT ids is how "models
/// served" is answered: a tensor-parallel model occupies both hosts of a pair
/// under one id (measured in the committed 4-node fixture: 4 hosts, 2 ids).
struct MonitorWorkload: Codable {
    /// The contract's id for one sparkrun workload (`workloads[].workload_id`).
    let workload_id: String?
    /// Legacy spelling in captures written before the route renamed the key.
    /// Decoded only so an older fixture still counts its job; both name the
    /// same sparkrun workload.
    let cluster_id: String?

    var id: String? { workload_id ?? cluster_id }
}

/// The per-node `sample` dict — all numeric metrics are STRINGS (may be empty
/// `""` when a metric is unreadable). Parsed via the computed `Double?`
/// accessors.
///
/// EVERY key the route sends on a live host is declared here except ONE:
/// `sparkrun_job_names` (a sparkrun container name — §7's redaction list, and
/// no surface reads it). Measured against the live `/v1/cluster/monitor` on
/// API 2.15.13: 35 keys per sample, 34 declared. The
/// monitor view-model (`ClusterMonitorViewModel.swift`) prints the CLI's host
/// table, and a column whose key is undeclared renders as `-` forever while
/// every gate stays green — the one failure mode this struct cannot tolerate,
/// so the field list is exhaustive rather than "what a surface happens to read".
/// `scripts/cluster_monitor_cli_parity.py` regenerates the wire keys from a
/// live capture and `model_decode_check` fails if a key arrives undeclared.
struct MonitorSample: Codable {
    let timestamp: String?
    let hostname: String?
    let uptime_sec: String?
    let cpu_name: String?
    let cpu_usage_pct: String?
    let cpu_load_1m: String?
    let cpu_load_5m: String?
    let cpu_load_15m: String?
    let cpu_freq_mhz: String?
    let cpu_temp_c: String?
    let mem_total_mb: String?
    let mem_used_mb: String?
    let mem_available_mb: String?
    let mem_bufcache_mb: String?
    let mem_used_pct: String?
    let swap_total_mb: String?
    let swap_used_mb: String?
    let gpu_name: String?
    let gpu_count: String?
    let gpu_util_pct: String?
    let gpu_mem_used_mb: String?
    let gpu_mem_total_mb: String?
    let gpu_mem_used_pct: String?
    let gpu_temp_c: String?
    let gpu_power_w: String?
    let gpu_power_limit_w: String?
    let gpu_clock_mhz: String?
    let gpu_mem_clock_mhz: String?
    let gpu_encoder_pct: String?
    let gpu_decoder_pct: String?
    let gpu_fan_pct: String?
    let net_recv_bytes_s: String?
    let net_send_bytes_s: String?
    /// The CLI's `Jobs` column. Declared WITHOUT a default for the same reason
    /// as `MonitorNode.workloads`: a stored property with an initial value is
    /// silently NOT decoded — it just compiles and reads nil, which here would
    /// have made the Jobs column a permanent `-`.
    let sparkrun_jobs: String?

    /// Explicit memberwise init, every parameter defaulted to nil, because the
    /// struct went from 12 keys to 34 and the synthesized one now demands all of
    /// them. The 12 keys a caller could already pass keep their RELATIVE order
    /// (the new keys append), so the two placeholder sites —
    /// `ClusterWidgetMetrics.sampleFleet` and `cluster_widget_check/main.swift`'s
    /// `sample()` — compile without being touched.
    ///
    /// The defaults are on the INIT PARAMETERS, not on the stored properties.
    /// That distinction is the whole §7 trap: a property written
    /// `let gpu_temp_c: String? = nil` is dropped from the synthesized
    /// `CodingKeys` and is then silently never decoded, so the column prints `-`
    /// forever with every gate green. A defaulted init parameter decodes
    /// normally — the key is still in `CodingKeys` — and only says "a hand-built
    /// sample need not mention it".
    init(hostname: String? = nil,
         uptime_sec: String? = nil,
         cpu_usage_pct: String? = nil,
         mem_used_pct: String? = nil,
         mem_total_mb: String? = nil,
         mem_used_mb: String? = nil,
         gpu_name: String? = nil,
         gpu_util_pct: String? = nil,
         gpu_mem_used_pct: String? = nil,
         cpu_temp_c: String? = nil,
         gpu_temp_c: String? = nil,
         cpu_load_1m: String? = nil,
         timestamp: String? = nil,
         cpu_name: String? = nil,
         cpu_load_5m: String? = nil,
         cpu_load_15m: String? = nil,
         cpu_freq_mhz: String? = nil,
         mem_available_mb: String? = nil,
         mem_bufcache_mb: String? = nil,
         swap_total_mb: String? = nil,
         swap_used_mb: String? = nil,
         gpu_count: String? = nil,
         gpu_mem_used_mb: String? = nil,
         gpu_mem_total_mb: String? = nil,
         gpu_power_w: String? = nil,
         gpu_power_limit_w: String? = nil,
         gpu_clock_mhz: String? = nil,
         gpu_mem_clock_mhz: String? = nil,
         gpu_encoder_pct: String? = nil,
         gpu_decoder_pct: String? = nil,
         gpu_fan_pct: String? = nil,
         net_recv_bytes_s: String? = nil,
         net_send_bytes_s: String? = nil,
         sparkrun_jobs: String? = nil) {
        self.timestamp = timestamp
        self.hostname = hostname
        self.uptime_sec = uptime_sec
        self.cpu_name = cpu_name
        self.cpu_usage_pct = cpu_usage_pct
        self.cpu_load_1m = cpu_load_1m
        self.cpu_load_5m = cpu_load_5m
        self.cpu_load_15m = cpu_load_15m
        self.cpu_freq_mhz = cpu_freq_mhz
        self.cpu_temp_c = cpu_temp_c
        self.mem_total_mb = mem_total_mb
        self.mem_used_mb = mem_used_mb
        self.mem_available_mb = mem_available_mb
        self.mem_bufcache_mb = mem_bufcache_mb
        self.mem_used_pct = mem_used_pct
        self.swap_total_mb = swap_total_mb
        self.swap_used_mb = swap_used_mb
        self.gpu_name = gpu_name
        self.gpu_count = gpu_count
        self.gpu_util_pct = gpu_util_pct
        self.gpu_mem_used_mb = gpu_mem_used_mb
        self.gpu_mem_total_mb = gpu_mem_total_mb
        self.gpu_mem_used_pct = gpu_mem_used_pct
        self.gpu_temp_c = gpu_temp_c
        self.gpu_power_w = gpu_power_w
        self.gpu_power_limit_w = gpu_power_limit_w
        self.gpu_clock_mhz = gpu_clock_mhz
        self.gpu_mem_clock_mhz = gpu_mem_clock_mhz
        self.gpu_encoder_pct = gpu_encoder_pct
        self.gpu_decoder_pct = gpu_decoder_pct
        self.gpu_fan_pct = gpu_fan_pct
        self.net_recv_bytes_s = net_recv_bytes_s
        self.net_send_bytes_s = net_send_bytes_s
        self.sparkrun_jobs = sparkrun_jobs
    }

    // MARK: Parsed numeric accessors (nil when unreadable/empty)

    var uptimeSeconds: Double? { Self.num(uptime_sec) }
    var cpuUsagePct: Double? { Self.num(cpu_usage_pct) }
    var cpuLoad1m: Double? { Self.num(cpu_load_1m) }
    var cpuLoad5m: Double? { Self.num(cpu_load_5m) }
    var cpuLoad15m: Double? { Self.num(cpu_load_15m) }
    var cpuFreqMHz: Double? { Self.num(cpu_freq_mhz) }
    var cpuTempC: Double? { Self.num(cpu_temp_c) }
    var memTotalMB: Double? { Self.num(mem_total_mb) }
    var memUsedMB: Double? { Self.num(mem_used_mb) }
    var memAvailableMB: Double? { Self.num(mem_available_mb) }
    var memBufcacheMB: Double? { Self.num(mem_bufcache_mb) }
    var memUsedPct: Double? { Self.num(mem_used_pct) }
    var swapTotalMB: Double? { Self.num(swap_total_mb) }
    var swapUsedMB: Double? { Self.num(swap_used_mb) }
    var gpuCount: Int? { Self.num(gpu_count).map { Int($0) } }
    var gpuUtilPct: Double? { Self.num(gpu_util_pct) }
    var gpuMemUsedMB: Double? { Self.num(gpu_mem_used_mb) }
    var gpuMemTotalMB: Double? { Self.num(gpu_mem_total_mb) }
    var gpuMemUsedPct: Double? { Self.num(gpu_mem_used_pct) }
    var gpuTempC: Double? { Self.num(gpu_temp_c) }
    var gpuPowerW: Double? { Self.num(gpu_power_w) }
    var gpuPowerLimitW: Double? { Self.num(gpu_power_limit_w) }
    var gpuClockMHz: Double? { Self.num(gpu_clock_mhz) }
    var gpuMemClockMHz: Double? { Self.num(gpu_mem_clock_mhz) }
    var gpuEncoderPct: Double? { Self.num(gpu_encoder_pct) }
    var gpuDecoderPct: Double? { Self.num(gpu_decoder_pct) }
    var gpuFanPct: Double? { Self.num(gpu_fan_pct) }
    var netRecvBytesS: Double? { Self.num(net_recv_bytes_s) }
    var netSendBytesS: Double? { Self.num(net_send_bytes_s) }
    /// `Jobs` in the CLI's host table: an integer count on the wire.
    var sparkrunJobCount: Int? { Self.num(sparkrun_jobs).map { Int($0) } }
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

    /// The daemon process's own uptime (`daemon_uptime_seconds`), separate from
    /// the machine's uptime. Used by the monitor widget's health line.
    var daemonUptimeText: String? {
        guard let s = daemon_uptime_seconds, s >= 0 else { return nil }
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
    /// The route's specific failure when the backing command failed. Absent on
    /// a healthy answer, so `total_hosts: 0` from a degraded route is never
    /// mistaken for a genuinely empty cluster.
    let error: String?
    let speak: String
}

/// One stored point from GET /v1/cluster/history — a node row or an engine row,
/// which share this shape because the server forwards the recorder's own rows
/// with their key names (`hosts[]` carry `host`, `engines[]` carry
/// `node`/`port`/`model`).
///
/// Deliberately sparse: the recorder carries every field it samples
/// (`HOST_KEYS` plus the three `*_max` peaks), and a model that declared all of
/// them would fail to decode the first time the recorder adds one. A `nil` here
/// is the recorder's own "not reported", and it must stay nil — the server
/// forwards its nulls untouched precisely because a 0.0 would draw a node that
/// never answered as an idle one.
struct ClusterHistoryPoint: Decodable {
    let ts: Int
    let n: Int?
    let host: String?
    let node: String?
    let port: String?
    let model: String?
    let error: String?
    let cpu_usage_pct: Double?
    let cpu_temp_c: Double?
    let gpu_util_pct: Double?
    let gpu_temp_c: Double?
    let gpu_power_w: Double?
    let gpu_power_w_max: Double?
    let gpu_temp_c_max: Double?
    let cpu_temp_c_max: Double?
    let mem_used_pct: Double?
    /// VRAM as the recorder actually stores it: a megabyte figure. There is no
    /// `gpu_mem_total_mb` column, so no percentage of VRAM exists to draw — a
    /// surface that shows "VRAM" shows MB, never a share of a total the route
    /// never sends (`ClusterHistoryMetric.vramUsedMb` is the one place that
    /// spelling is allowed, and `cluster_history_chart_check.sh` holds it).
    let gpu_mem_used_mb: Double?
    let run: Int?
    let wait: Int?
    let kv: Double?
    let gen_tps: Double?
    /// Prompt-side throughput. The recorder keeps `run`/`wait`/`kv`/`gen_tps`
    /// AND `prompt_tps` (its `ENGINE_FIELDS`), and the store's columns are
    /// sqlite `REAL`, so this route answers `2.0` for a count of 2 — the integer
    /// fields above only decode because JSONDecoder converts an integral number
    /// losslessly, which the committed `v1_cluster_history_6h.json` now pins.
    let prompt_tps: Double?
}

/// GET /v1/cluster/history — the recorder's stored series over three tiers.
///
/// `tier`/`resolution_s`/`resolution`/`tier_summary` are the part a surface must
/// not guess: the same `hosts` array means 2-second points at `tier 0`, 1-minute
/// averages at `tier 1` and 5-minute averages at `tier 2`, and `max_fields`
/// names the peak columns this answer carries (empty at tier 0, which stores no
/// peak). `hosts_included`/`engines_included` distinguish "this `metrics=`
/// filter excluded that series" from "the fleet had nothing to serve".
struct ClusterHistoryResponse: Decodable, Speakable {
    let window: String
    let window_s: Int?
    let tier: Int
    let resolution_s: Int
    let resolution: String
    let tier_summary: String?
    let max_fields: [String]?
    let since: Int
    let until: Int
    let node: String?
    let hosts_included: Bool?
    let engines_included: Bool?
    let hosts: [ClusterHistoryPoint]
    let engines: [ClusterHistoryPoint]
    /// Why the answer is empty when it is (fresh install, or — for `15m` — that
    /// the 2 s ring lives in the daemon process and this server cannot see it).
    let note: String?
    let error: String?
    let speak: String
    let store: ClusterHistoryStore?

    /// Whether the 2 s ring was readable from the serving process — answered at
    /// tier 0 only. `false` there means it is not, which a chart must render as a
    /// gap with a reason, not as a quiet fleet; `nil` means the served tier never
    /// read the ring, so it reports nothing either way (the reason for an empty
    /// answer is `note`). Not a non-optional: `null` is a real value on the wire.
    var tier0Resident: Bool? { store?.tier0_resident }
}

/// The `store` object of GET /v1/cluster/history. `path` is deliberately NOT
/// declared: it is a filesystem path on the operator's machine, and §7 keeps
/// server-side paths out of the App Group container — an undeclared key is never
/// decoded, so it can neither be stored nor rendered.
struct ClusterHistoryStore: Decodable {
    let exists: Bool?
    let db_bytes: Int?
    let tier0_resident: Bool?
    let error: String?
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

// ---------------------------------------------------------------------------
// GET /v1/metrics/history · t_5ae8cfcf · the three-tier history read
// ---------------------------------------------------------------------------
//
// The route is a pass-through of the daemon's recorder
// (`hscc_daemon/metrics_history.py`), so these field names ARE sparkrun's
// `MonitorSample` names — spelled exactly as the server sends them, snake_case
// and all, because the rename a decoder "cleans up" is how a chart silently
// reads nil for a real number. Two rules the shapes below encode:
//
//   * every metric is Optional and stays Optional. The server writes `null` for
//     "this node did not report it" and NEVER 0, so a `Double` (non-optional)
//     here would turn a node that never answered into an idle one on the chart.
//   * `*_max` are separate columns from their averages, not decorations: a
//     1-minute mean is exactly what hides a throttle spike. A temperature or
//     power chart that draws `gpu_temp_c` without `gpu_temp_c_max` draws the
//     wrong fact, and the peak column exists only on the rolled tiers.
//
// Tier 0 (the 2 s ring) has no `*_max` and carries `res: "2s"`; rows from the
// store (tiers 1 and 2) have both peaks and no `res`. Both are one struct, all
// optional, so one decoder serves every tier.

/// One node row of `GET /v1/metrics/history` — one bucket, one host.
struct MetricsHostRow: Decodable {
    let tier: Int?
    /// The bucket start (epoch seconds), inclusive — the store's own key.
    let ts: Int?
    /// `res` is the tier-0 resolution label ("2s"); absent on stored rows.
    let res: String?
    let host: String?
    /// Samples that went into the bucket (1 on tier 0, up to 30 on tier 1).
    /// `nil`/0 means "nothing sampled", which is not the same as a value.
    let n: Int?
    let error: String?

    // sparkrun's MonitorSample names, verbatim.
    let cpu_usage_pct: Double?
    let cpu_temp_c: Double?
    let gpu_util_pct: Double?
    let gpu_temp_c: Double?
    let gpu_power_w: Double?
    let mem_used_pct: Double?
    let swap_used_mb: Double?
    let swap_total_mb: Double?
    let swap_used_pct: Double?
    let gpu_mem_used_mb: Double?
    let gpu_clock_mhz: Double?
    let net_recv_bytes_s: Double?
    let net_send_bytes_s: Double?
    let sparkrun_jobs: Double?

    // The peaks, stored beside the averages (tiers 1 and 2 only).
    let cpu_temp_c_max: Double?
    let gpu_temp_c_max: Double?
    let gpu_power_w_max: Double?
}

/// One engine row — one bucket, one backend. Identity is `(node, port)`:
/// `model` is an attribute of the port, so a model swapped on that port is the
/// same series, not a new one.
struct MetricsEngineRow: Decodable {
    let tier: Int?
    let ts: Int?
    let res: String?
    let node: String?
    let port: String?
    let model: String?
    let n: Int?
    let error: String?
    let run: Double?
    let wait: Double?
    let kv: Double?
    let gen_tps: Double?
    let prompt_tps: Double?

    /// The identity the store keys on, as one string for a legend line.
    var engineKey: String { "\(node ?? "?"):\(port ?? "?")" }
}

/// Tier 0's depth + whether the serving process could actually read the ring.
///
/// `readable_here` is the field a chart must branch on: tier 0 lives in the
/// daemon's memory and the API is a different process, so `points` can be a real
/// count while `hosts` is legitimately empty. Drawing that as "nothing happened"
/// is the misreading this exists to prevent.
struct MetricsHistoryTier0: Decodable {
    let points: Int?
    let capacity: Int?
    let readable_here: Bool?
    let source: String?
    let reason: String?
    let sampler_error: String?
    let store_error: String?
    let stamp_age_s: Double?
}

struct MetricsHistoryTierMeta: Decodable {
    let tier: Int?
    /// Seconds per bucket: 2 / 60 / 300.
    let resolution_s: Int?
    /// The tier's own retention in seconds: 900 / 86400 / 604800.
    let window_s: Int?
    let capacity: Int?
}

/// The field vocabulary the server publishes so no client hardcodes a column
/// list. `host_peaks` is the list a temperature or power chart must draw
/// alongside its averages.
struct MetricsHistoryFields: Decodable {
    let host: [String]?
    let host_peaks: [String]?
    let engine: [String]?
    let engine_key: [String]?
    let engine_identity: String?
}

struct MetricsHistoryCounts: Decodable {
    let host_rows: Int?
    let engine_rows: Int?
}

/// The recorder's `stats()`: the store's size against its stated bound.
struct MetricsHistoryStats: Decodable {
    let path: String?
    let db_bytes: Int?
    let bound_bytes: Int?
    let tier1_rows: Int?
    let tier2_rows: Int?
    let exists: Bool?
    let error: String?
}

/// `GET /v1/metrics/history?tier=&host=&node=&since=&until=`.
///
/// Verified route shape (routes_metrics.py, t_5ae8cfcf):
///   { tier, tier_meta{tier,resolution_s,window_s,capacity}, since, until,
///     host, node, source, hosts[], engines[], counts{host_rows,engine_rows},
///     fields{host[],host_peaks[],engine[],engine_key[],engine_identity},
///     tier0{points,capacity,readable_here,source,reason,sampler_error,
///           store_error,stamp_age_s}, stats{…}, error, speak }
/// Only `tier`, `hosts`, `engines` and `speak` are guaranteed present; every
/// metric inside a row is optional by the rule above.
struct MetricsHistoryResponse: Decodable, Speakable {
    let tier: Int
    let tier_meta: MetricsHistoryTierMeta?
    let since: Int?
    let until: Int?
    let host: String?
    let node: String?
    /// Where the rows came from: `sqlite_store`, `sqlite_store_absent`,
    /// `recorder_ring`, `recorder_ring_absent`.
    let source: String?
    let hosts: [MetricsHostRow]
    let engines: [MetricsEngineRow]
    let counts: MetricsHistoryCounts?
    let fields: MetricsHistoryFields?
    let tier0: MetricsHistoryTier0?
    let stats: MetricsHistoryStats?
    /// The route's own problem, if it had one. Absent on a clean read; a
    /// degraded read still answers 200 with empty rows, so this — not the
    /// status code — is what tells a surface the history is unavailable.
    let error: String?
    let speak: String

    /// True only when rows actually came from the store. An empty `hosts` with
    /// this false is "no history read yet", not "the fleet was idle".
    var hasStoredRows: Bool {
        source == "sqlite_store" && !hosts.isEmpty
    }

    /// The live 2 s window can be drawn ONLY when the serving process held the
    /// ring when it answered.
    var hasLiveTier0: Bool {
        tier0?.readable_here == true && (tier0?.points ?? 0) > 0
    }
}

