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
    /// Display name shown in the settings rows and editor titles.
    var displayName: String {
        switch self {
        case .widgetCluster: return "Cluster Widget"
        case .widgetMonitor: return "Monitor Widget"
        case .widgetBoardStatus: return "Board Status Widget"
        case .widgetCardsRunning: return "Running Cards Widget"
        case .widgetKanbanMonitor: return "Kanban Monitor Widget"
        case .widgetProjects: return "Projects Widget"
        case .widgetNeedsMe: return "Needs Me Widget"
        case .widgetClusterGraph: return "Cluster Graph Widget"
        case .widgetMergeQueue: return "Merge Queue Widget"
        case .liveActivityWake: return "Wake Live Activity"
        case .liveActivityMonitor: return "Monitor Live Activity"
        case .liveActivityClusterHealth: return "Cluster Health Live Activity"
        case .liveActivityCards: return "Running Cards Live Activity"
        case .liveActivityBoardProgress: return "Board Progress Live Activity"
        }
    }

    /// SF Symbol for the settings row.
    var displayIcon: String {
        switch self {
        case .widgetCluster: return "square.grid.2x2"
        case .widgetMonitor: return "gauge"
        // The same glyph the widget itself draws in its header, so the row and
        // the widget are visibly the same thing.
        case .widgetBoardStatus: return "square.stack.3d.up"
        // These two have no header glyph to copy — the running-cards widget
        // draws no glyph at all outside its failure banner, and the monitor
        // panel's headline is a Text of counts. So the row glyph is chosen to
        // read as the surface AND to stay distinguishable from the rows above
        // it in the same list: a second `square.stack.3d.up` two rows under the
        // board tile is the same row twice at a glance. Both names went through
        // the same NSImage check `sf_symbol_check.sh` uses before being written
        // here — a name that resolves nowhere renders a BLANK row, and until
        // this commit the gate did not scan `displayIcon` at all.
        case .widgetCardsRunning: return "rectangle.stack.badge.play"
        case .widgetKanbanMonitor: return "list.bullet.rectangle"
        // NOT the bare `folder` the widget draws — that glyph belongs to one
        // project ROW (ProjectRowView draws it once per project), and this row
        // is the surface, not one of its rows. The gear badge says "the list you
        // control here", which is what this row is for since t_5f246177. Checked
        // through the same NSImage(systemSymbolName:) call sf_symbol_check.sh
        // uses: this name RESOLVES, and the tempting `folder.badge.list` does
        // not exist at all — the row would have rendered blank and, before #341,
        // nothing in the gate set looked at displayIcon. No neighbour row in this
        // list uses a folder glyph.
        case .widgetProjects: return "folder.badge.gearshape"
        case .widgetNeedsMe: return "bell.badge"
        // The same glyph the graph widget draws in its own header, so the settings
        // row and the widget are visibly the same thing. Checked through the
        // NSImage(systemSymbolName:) call `sf_symbol_check.sh` uses — a name that
        // resolves nowhere renders a BLANK row. Distinct from the Board Progress
        // row's `chart.bar.xaxis`: that bubble is counts over a board, this is a
        // series over time.
        case .widgetClusterGraph: return "chart.xyaxis.line"
        // The same glyph the widget itself draws in its header. Checked through
        // the NSImage(systemSymbolName:) call sf_symbol_check.sh uses — the name
        // resolves on this host and is distinct from the graph's chart glyph.
        case .widgetMergeQueue: return "arrow.triangle.merge"
        case .liveActivityWake: return "bolt"
        case .liveActivityMonitor: return "waveform.path.ecg"
        // Health, not a gauge: this surface answers "is the fleet up", which is
        // the question the monitor activity's ECG line does not ask.
        case .liveActivityClusterHealth: return "heart.text.square"
        // The same glyph the running-cards surfaces lead with (§5.4's cards
        // stack), so the settings row, the widget and the Island say the same
        // thing. It is deliberately the Board Status row's glyph too: that row
        // and this one are the two card-stack surfaces, and inventing a fourth
        // stack glyph to make the list look tidier would put a symbol on screen
        // that no rendering of either surface ever uses.
        case .liveActivityCards: return "square.stack.3d.up"
        // The progress bubble draws four counts for one board, which is the
        // Board Status widget's dataset in Live Activity form. A bar-chart
        // glyph reads as "counts over a board" and stays distinct from the
        // card-stack pair above it in the same list; checked through the same
        // NSImage(systemSymbolName:) call sf_symbol_check.sh uses.
        case .liveActivityBoardProgress: return "chart.bar.xaxis"
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
