import Foundation
import ActivityKit

// ---------------------------------------------------------------------------
// Live Activity — HSCC Monitor.
//
// `MonitorActivityAttributes` is the ActivityKit attributes type for the NEW
// "HSCC Monitor" Live Activity: it surfaces live cluster + daemon-host metrics
// (per-node CPU/RAM/GPU, daemon-host machine data, state, timestamps) on the
// Dynamic Island / Lock Screen while it is pinned.
//
// Live Activities are PUSH-ONLY: they cannot fetch data themselves. Every
// update flows from the APP process — `MonitorActivityDriver` polls
// GET /v1/cluster/monitor + GET /v1/daemon/host at the settings cadence and
// calls update(). This file is compiled into BOTH the app (which
// starts/updates/ends via the driver) and the HSCCLiveActivityMonitor
// extension (which renders it via the ActivityConfiguration), so the two sides
// agree on the exact activity type and its content state.
// ---------------------------------------------------------------------------

struct MonitorActivityAttributes: ActivityAttributes {
    /// The state the Dynamic Island / Lock Screen renders — the flattened,
    /// compact form of the live monitor + daemon-host data the app pushed.
    ///
    /// It deliberately carries the REDUCED set of what the rendered surfaces
    /// draw (not the full raw API JSON — ActivityKit serializes this state and
    /// the Dynamic Island has tight size limits). `nodes` are per-node
    /// CPU/RAM/GPU metrics; `host` is the daemon-host machine data; both are
    /// decoded app-side from the raw responses.
    public struct ContentState: Codable, Hashable {
        /// Headline cluster state: "serving" / "waking" / "down" / "unreachable".
        public var state: String
        /// Per-node cluster monitor metrics (short `.NNN` labels, CPU/RAM/GPU).
        public var nodes: [NodeMetric]
        /// Daemon-host machine data (the machine HSCC runs on), or nil when the
        /// host section is hidden or the fetch failed.
        public var host: HostMetric?
        /// Nodes the monitor listed / that answered — the CLI's host tally.
        public var nodesUp: Int? = nil
        public var nodesTotal: Int? = nil
        /// Distinct models and the CLI total Gen t/s from the serving table.
        public var models: Int? = nil
        public var genTps: Double? = nil
        /// When the app last pushed a successful update — drives the staleDate
        /// and the honest age shown after a force-quit.
        public var updatedAt: Date

        // MARK: Cluster metrics (t_1685f884)
        //
        // The real `hscc cluster monitor` tables, projected app-side by
        // `MonitorSurfaceReading.payload` from the same read that set the fields
        // above. Every one is optional and nil-defaulted for two reasons: an
        // activity that an OLDER app pushed survives an app update (the stored
        // state must still decode, and each surface falls back to what it drew
        // before), and a section the operator switched off in Settings pushes
        // nothing rather than an empty table. The defaults are on the fields
        // because `ContentState` has no custom init — the synthesized memberwise
        // one keeps every old call site compiling, and none of these keys is
        // read back out of an `AppGroup` blob.

        /// The host table, one entry per host, CLI cells verbatim. Nil when the
        /// `nodes` section is off or the read never answered.
        public var hostRows: [MonitorHostMetric]? = nil
        /// Hosts the push cap left out, so a surface can say `+N more` instead
        /// of quietly drawing a short table.
        public var hostRowsDropped: Int? = nil
        /// The serving table's per-model `total (n)` rows.
        public var servingTotals: [MonitorServingTotalMetric]? = nil
        /// `total (n)` rows the push cap left out, so the `+N more` under a
        /// totals list is the truth about the fleet rather than about the list
        /// this region happened to be handed.
        public var servingTotalsDropped: Int? = nil
        /// The serving table's aggregate Run/Wait. nil = never told, which is
        /// NOT zero.
        public var servingRunning: Int? = nil
        public var servingWaiting: Int? = nil
        /// The one metric the island's compact and minimal surfaces lead with.
        public var primary: MonitorPrimaryMetric? = nil
        /// The metric beside it in compact (a queue, a run count, or the tally).
        public var secondary: MonitorPrimaryMetric? = nil
        /// The serving table's queue verdict (the CLI's own `_load_style`), for
        /// the aggregate run/wait line. nil = the table never answered.
        public var queueLevel: ClusterMetricLevel? = nil
    }

    /// A token identifying this monitor activity so it can be updated/ended.
    public var activityID: UUID = UUID()
}

/// One node's live monitor metrics, in the compact form the activity renders.
struct NodeMetric: Codable, Hashable {
    public var label: String        // short tail, e.g. ".244"
    public var cpuPct: Double?      // cpu_usage_pct
    public var memPct: Double?      // mem_used_pct
    public var gpuName: String?     // gpu_name
    public var gpuUtilPct: Double?  // gpu_util_pct
    /// The serving table's model for this node, CPU/GPU temperatures, and the
    /// backend's run/wait counts. All `serving[]` / `sample` fields.
    public var model: String? = nil
    public var cpuTempC: Double? = nil
    public var gpuTempC: Double? = nil
    public var running: Int? = nil
    public var waiting: Int? = nil
}

/// The daemon-host machine data the monitor activity surfaces.
struct HostMetric: Codable, Hashable {
    public var hostname: String
    public var os: String           // platform (e.g. "macOS")
    public var arch: String
    public var cpuPct: Double?      // cpu.percent
    public var memPct: Double?      // memory.percent
    public var diskPct: Double?     // disk.percent
    public var daemonRunning: Bool
    /// `daemon_uptime_seconds` — the daemon's own uptime (`hscc status`).
    public var daemonUptimeSeconds: Int? = nil
}
