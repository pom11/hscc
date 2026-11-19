import AppIntents
import Foundation
import SwiftUI
import WidgetKit

// ---------------------------------------------------------------------------
// The history graph widget: its configuration, its one read, its timeline.
//
// CONFIGURATION (the card's three knobs):
//   * Node       — one node, or unset for every node (which a single line draws
//                  as the fleet's min/max envelope, and a panel chart draws as one
//                  panel per node).
//   * Metric set — temps / utilisation / power / memory / serving, or unset for all
//                  of them (`ClusterHistoryMetricGroup`, the Shared layer's own
//                  vocabulary — this widget declares no second metric enum).
//   * Window     — 15 m (the DEFAULT, per the operator's mid-run amendment: a
//                  graph earns its keep at the 2-second tier, which is what the
//                  Monitor Live Activity and the menu bar already refresh at) or
//                  1 h / 6 h / 24 h / 7 d.
//
// ONE READ: `WidgetDataClient.clusterHistory` → `GET /v1/cluster/history`, the
// canonical metrics-history route (DECISION-t_447cc7fc). Not two reads, and never
// the retired `/v1/metrics/history`. The metric list is one comma list on one
// request, because the route answers one row per (identity, timestamp) with every
// requested column, and a widget that asked per metric would spend a budget it has
// no right to spend.
//
// The cadence and the pause come from `WidgetSurfaceKind.widgetClusterGraph` — the
// shared registry, exactly like every other surface — so the operator's Settings
// row controls this widget and nothing here hard-codes a refresh interval.
// ---------------------------------------------------------------------------

// MARK: - Pickers

/// The window picker. Backed by `ClusterGraphWindow`, so the widget's own options
/// and the Shared layer's window rules cannot drift apart: the raw values ARE the
/// route's `window=` values.
enum ClusterGraphWindowOption: String, AppEnum {
    case fifteenMinutes = "15m"
    case oneHour = "1h"
    case sixHours = "6h"
    case twentyFourHours = "24h"
    case sevenDays = "7d"

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Window")
    static var caseDisplayRepresentations: [ClusterGraphWindowOption: DisplayRepresentation] = [
        .fifteenMinutes: "15 minutes (2 s)",
        .oneHour: "1 hour",
        .sixHours: "6 hours",
        .twentyFourHours: "24 hours",
        .sevenDays: "7 days"
    ]

    var window: ClusterGraphWindow { ClusterGraphWindow(rawValue: rawValue) ?? .fifteenMinutes }
}

/// The metric-set picker: the Shared layer's five groups, plus the "unset" case
/// the `@Parameter` optionality already provides (nil = every set).
enum ClusterMetricSetOption: String, AppEnum {
    case temps
    case utilisation
    case power
    case memory
    case serving

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Metric set")
    static var caseDisplayRepresentations: [ClusterMetricSetOption: DisplayRepresentation] = [
        .temps: "Temperatures",
        .utilisation: "Utilisation",
        .power: "Power",
        .memory: "Memory",
        .serving: "Serving"
    ]

    /// The Shared layer's group, or nil when the name matches none. A nil here is
    /// "every set", which is the same meaning the unset parameter has — so a
    /// renamed group degrades to the wider answer rather than to an empty chart.
    var group: ClusterHistoryMetricGroup? {
        ClusterHistoryMetricGroup.allCases.first { $0.rawValue == rawValue }
    }
}

/// One node option. Identity is the recorder's own host identity, because that is
/// what the route's `node=` filter keys on; the label is the app's ONE short-tail
/// rule, so a Home Screen configuration sheet never prints an address (§7).
struct ClusterGraphNodeEntity: AppEntity {
    let id: String

    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Node")
    static var defaultQuery = ClusterGraphNodeQuery()

    var displayRepresentation: DisplayRepresentation {
        DisplayRepresentation(title: "\(MonitorNode.shortTail(id))")
    }
}

/// The node list. Live first, stored identities second, empty last — never an
/// error, because a throwing options provider gives the configuration sheet a
/// spinner and the operator needs the truth ("no nodes seen yet"), not a hang.
struct ClusterGraphNodeQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [ClusterGraphNodeEntity] {
        identifiers.map { ClusterGraphNodeEntity(id: $0) }
    }

    /// `throws` because `EntityQuery` requires it, and nothing here throws: an
    /// options provider that throws gives the configuration sheet a spinner, and
    /// the operator needs "no nodes seen yet" rather than a hang.
    func suggestedEntities() async throws -> [ClusterGraphNodeEntity] {
        await ClusterGraphNodeQuery.hostIdentities().map { ClusterGraphNodeEntity(id: $0) }
    }

    func allEntities() async throws -> [ClusterGraphNodeEntity] {
        try await suggestedEntities()
    }

    /// Live host identities when the fleet answers, the last ones this device saw
    /// when it does not. Kept `static` so the provider's save path and the query's
    /// read path are the same code, and so the configuration sheet and the timeline
    /// cannot disagree about which nodes exist.
    static func hostIdentities() async -> [String] {
        guard let client = WidgetDataClient.makeIfConfigured() else {
            return ClusterGraphSnapshotStore.loadHosts()
        }
        switch await client.clusterMonitor() {
        case .success(let payload):
            let hosts = payload.nodes.map { $0.host }.filter { !$0.isEmpty }.sorted()
            if hosts.isEmpty { return ClusterGraphSnapshotStore.loadHosts() }
            ClusterGraphSnapshotStore.saveHosts(hosts)
            return hosts
        case .failure:
            return ClusterGraphSnapshotStore.loadHosts()
        }
    }
}

/// The widget's configuration: node, metric set, window.
struct ClusterGraphConfigurationIntent: WidgetConfigurationIntent {
    static var title: LocalizedStringResource = "Cluster Graph"
    static var description = IntentDescription(
        "Pick the node, the metric set and the window. Leave the node unset for every node and the metric set unset for all of them.")

    @Parameter(title: "Node",
               description: "One node, or leave unset for every node.")
    var node: ClusterGraphNodeEntity?

    @Parameter(title: "Metric set",
               description: "Temps, utilisation, power, memory or serving — leave unset for all of them.")
    var metricSet: ClusterMetricSetOption?

    /// The default is 15 minutes on purpose: at the 2-second tier a home-screen
    /// graph shows a thermal excursion while it is happening, which is the only
    /// reason a chart beats a number. The longer windows stay selectable, and the
    /// widget always prints the resolution the ROUTE served, so a sparse 7-day
    /// chart can never be mistaken for live 2-second data. (`default:` is the
    /// `@Parameter` form for a non-optional option: a `= value` on the property is
    /// an "extra argument 'wrappedValue'" error, which is what this line was.)
    @Parameter(title: "Window",
               description: "15 minutes (the 2-second ring) up to 7 days.",
               default: .fifteenMinutes)
    var window: ClusterGraphWindowOption

    /// The node to draw, nil for every node. The sentinel is folded away here so no
    /// view downstream knows a picker sentinel exists.
    var pickedNode: String? {
        guard let node, !node.id.isEmpty else { return nil }
        return node.id
    }

    var pickedSet: ClusterHistoryMetricGroup? { metricSet?.group }

    var pickedWindow: ClusterGraphWindow { window.window }
}

// MARK: - Timeline provider

struct ClusterGraphTimelineProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> ClusterGraphEntry { .placeholder }

    func snapshot(for configuration: ClusterGraphConfigurationIntent,
                  in context: Context) async -> ClusterGraphEntry {
        // The gallery preview goes through the same registry read the timeline
        // does, so a paused widget previews as paused instead of showing a live
        // chart of a surface the operator turned off.
        await entry(for: configuration, family: context.family, surface: Self.surface())
    }

    func timeline(for configuration: ClusterGraphConfigurationIntent,
                  in context: Context) async -> Timeline<ClusterGraphEntry> {
        let surface = Self.surface()
        let entry = await entry(for: configuration, family: context.family, surface: surface)
        guard let next = ClusterGraphTimelineProvider.nextRefreshDate(
            refreshMinutes: surface?.refreshMinutes ?? Self.defaultRefreshMinutes) else {
            return Timeline(entries: [entry], policy: .atEnd)
        }
        return Timeline(entries: [entry], policy: .after(next))
    }

    /// The operator's registry entry for this surface. nil only when the store has
    /// never been written, which is the "on, 5 minutes" default the registry
    /// documents rather than a state this file invents.
    static func surface() -> WidgetSurfaceSettings? {
        AppGroupWidgetSettings.load()?.setting(for: .widgetClusterGraph)
    }

    /// The cadence when the operator has never touched the settings screen.
    static let defaultRefreshMinutes = 5

    // GATE-SLICE-BEGIN: cluster_graph_cadence
    /// The ONE cadence decision, kept free of any state so a gate can execute it:
    /// nil means Off — the timeline gets `policy: .atEnd` and the OS wakes the
    /// widget when it likes — and a date means `.after(date)`. Written inline, the
    /// Off branch (the one that becomes a refresh loop if it is wrong) would be the
    /// only branch in this file no gate ever runs.
    static func nextRefreshDate(refreshMinutes: Int, from now: Date = .now) -> Date? {
        guard refreshMinutes > 0 else { return nil }
        return Calendar.current.date(byAdding: .minute, value: refreshMinutes, to: now)
            ?? now.addingTimeInterval(TimeInterval(refreshMinutes * 60))
    }
    // GATE-SLICE-END: cluster_graph_cadence

    // MARK: Fetch

    private func entry(for configuration: ClusterGraphConfigurationIntent,
                       family: WidgetFamily,
                       surface: WidgetSurfaceSettings?) async -> ClusterGraphEntry {
        let window = configuration.pickedWindow
        let node = configuration.pickedNode
        let set = configuration.pickedSet

        // Gate order matters and is the shipped rule everywhere: setup invite, then
        // pause, then the read. An unconfigured app must never read as "paused",
        // and a paused surface must never draw a stale number.
        guard WidgetDataClient.makeIfConfigured() != nil else {
            return ClusterGraphEntry(date: .now, family: family, node: node, set: set,
                                     window: window, metrics: [], payload: nil,
                                     status: .unconfigured)
        }
        guard surface?.isActive ?? true else {
            return ClusterGraphEntry(date: .now, family: family, node: node, set: set,
                                     window: window, metrics: [], payload: nil,
                                     status: .paused)
        }

        // Which metrics this size draws, decided against the window's EXPECTED tier
        // (the request can only ask what that tier stores — the three `*_max`
        // peaks exist only after a roll-up, and asking for them at the 15-minute
        // tier fails the request rather than the chart).
        let density = family.clusterGraphDensity ?? .sparkline
        let requestList = ClusterGraphRule.requestMetrics(density: density, set: set,
                                                          window: window)
        let nodeParam = node ?? "all"

        let outcome = await WidgetData.read(
            using: WidgetDataClient.makeIfConfigured(),
            lastKnown: { ClusterGraphSnapshotStore.lastKnown(window: window.requestValue,
                                                             node: nodeParam) }
        ) { client in
            switch await client.clusterHistory(window: window.requestValue,
                                               node: nodeParam,
                                               metrics: requestList.map { $0.rawValue }) {
            case .success(let response):
                return .success(ClusterGraphPayload.project(from: response,
                                                             metrics: requestList))
            case .failure(let failure):
                return .failure(failure)
            }
        }

        let payload: ClusterGraphPayload
        let status: ClusterGraphStatus
        switch outcome {
        case .live(let value, let fetchedAt):
            payload = value
            status = .live
            // Only a live read writes the last-known payload, and it is written for
            // the window and node it was fetched with — a stale chart for a window
            // the operator did not pick is a chart with a lying x axis.
            ClusterGraphSnapshotStore.save(value, window: window.requestValue,
                                           node: nodeParam, at: fetchedAt)
        case .stale(let value, let minutes, let reason):
            payload = value
            status = .stale(minutes: minutes, reason: reason)
        case .failed(let failure):
            payload = ClusterGraphPayload(series: [], note: nil)
            status = .failed(failure)
        }

        // The draw list is filtered against the tier the answer ACTUALLY served:
        // the route clamps and reports, and a peak column it did not fill must not
        // occupy a panel slot that reads as "this node has no peak".
        let served = payload.series.first?.tier ?? window.expectedTier
        let drawList = ClusterGraphRule.requestable(requestList, tier: served)

        return ClusterGraphEntry(date: .now, family: family, node: node, set: set,
                                 window: window, metrics: drawList, payload: payload,
                                 status: status)
    }
}

// MARK: - Widget

/// The Cluster Graph widget: the recorder's stored series, drawn as a chart that is
/// dense at every size it offers.
struct ClusterGraphWidget: Widget {
    let kind = "ClusterGraphWidget"

    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .widgetClusterGraph)
        return AppIntentConfiguration(kind: kind,
                                      intent: ClusterGraphConfigurationIntent.self,
                                      provider: ClusterGraphTimelineProvider()) { entry in
            ClusterGraphWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    WidgetSurfaceBackground(style: style)
                }
                .widgetRenderingAdaptation(style: style)
        }
        .configurationDisplayName("Cluster Graph")
        .description("Temperature, utilisation, power, memory and serving history per node — dense at every size.")
        // ONE list, read on both platforms. Home Screen sizes only: no accessory
        // family is declared, `widget_view_dispatch` gates this surface as
        // home-screen-only (which asserts the INVERSE — no accessory in the list and
        // no Lock Screen label rule in this file), and a chart of a series in a Lock
        // Screen complication is either a picture with no numbers or a number with no
        // chart.
        //
        // There is deliberately no `#if os(iOS)` around this line, and the reason is
        // not taste. WidgetKit marks the four `accessory*` cases
        // `@available(macOS, unavailable)`, which is exactly why a widget naming one
        // needs a guard — this widget names none, and `cluster_graph_check.sh` §2
        // fails the surface if one ever appears. The four sizes it does name are all
        // valid on the desktop: Apple's widget HIG lists system extra large as
        // supported on the Mac desktop and in Notification Center, and names iPhone as
        // the platform that does not support it, and `systemExtraLarge` carries only
        // tvOS/watchOS unavailability in the macOS `WidgetKit.swiftinterface`. The
        // desktop target also already compiles this list's own definition,
        // `GraphWidgetMetrics.swift`, which names `.systemExtraLarge` unguarded — so
        // the guard here protected nothing. What it did protect was a second,
        // desktop-only list of three sizes, which left the desktop compiling, shipping
        // and gating a small-multiples layout, a density mapping and a dispatch arm
        // for a size it never offered (t_4311500c).
        //
        // A per-platform asymmetry is allowed again only if it is WRITTEN: an arm that
        // omits a family from `clusterGraphFamilies` has to name it in a
        // `// families-omit: .systemX — reason` marker on that arm, or §1 of that gate
        // reddens it by name. An unwritten asymmetry is what this line used to be.
        .supportedFamilies(WidgetFamily.clusterGraphFamilies)
    }
}
