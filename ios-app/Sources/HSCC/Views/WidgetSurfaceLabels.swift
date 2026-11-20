import Foundation

// ---------------------------------------------------------------------------
// Shared display metadata for surfaces + sections
// ---------------------------------------------------------------------------
//
// UI-only presentation (name, SF Symbol icon, human section labels) for the
// settings screens. Kept separate from the data model in SharedModels.swift so
// the shared model carries NO UI concerns, while the apps (which render the
// editor) have one set of display facts both settings rows and the generic
// `WidgetSurfaceEditor` reference — no duplicated switch statements. Compiled
// into the iOS app and the macOS app, so the same surface names appear on both.

extension WidgetSurfaceKind {
    // displayName / displayIcon are GENERATED from ios-app/widgets/*.json into
    // WidgetSurfaceLabels.generated.swift. This file keeps only the shared
    // section-label vocabulary, which is not per-widget registration.

    /// The Lock Screen families this surface ships (L2 Gauges, 2026-11-20).
    /// Live Activities have no widget families, so their row omits the line.
    var lockScreenFamilies: String? {
        switch self {
        case .widgetCluster, .widgetKanbanMonitor, .widgetCardsRunning,
             .widgetNeedsMe, .widgetMergeQueue:
            return "Lock Screen: circular · rectangular · inline"
        case .liveActivityWake, .liveActivityMonitor, .liveActivityClusterHealth,
             .liveActivityCards, .liveActivityBoardProgress:
            return nil
        }
    }

    /// Human label for a canonical section identifier (\"nodes\" → \"Node metrics\").
    static func displaySectionLabel(_ id: String) -> String {
        switch id {
        case "state": return "State"
        case "topology": return "Topology"
        case "models": return "Models"
        case "board": return "Board work"
        case "nodes": return "Node metrics"
        case "host": return "Host machine"
        case "timing": return "Timing"
        default: return id
        }
    }
}
