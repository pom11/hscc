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
        /// When the app last pushed a successful update — drives the staleDate
        /// and the honest age shown after a force-quit.
        public var updatedAt: Date
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
}
