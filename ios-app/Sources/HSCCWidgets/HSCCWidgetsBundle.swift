import SwiftUI
import WidgetKit

/// HSCC Home Screen widgets — cluster state at a glance.
///
/// Hosted by the `HSCCWidgets` app-extension target. Three families:
///   * systemSmall  — state (serving / waking / down) + a compact topology glyph
///   * systemMedium — topology pairs with per-node colour + model count +
///                     idle-minutes remaining before autodown fires.
///   * systemMedium/systemLarge — HSCC Monitor: per-node `hscc cluster monitor`
///                     metrics (CPU/RAM/GPU) + the daemon-host machine data,
///                     refreshed on the operator's configured cadence.
///   * small/medium/large + the three accessory families — Running Cards: the
///                     cards being worked right now, each with its worker and
///                     its run time (`hscc cards monitor --live`, narrowed).
///   * accessory + small/medium/large — Kanban Monitor: `hscc kanban monitor
///                     --live`'s running / ready / blocked, from the kanban trio,
///                     with a visible error pane when a board read fails.
///   * small/medium/large — Projects: the projects row of WIDGET_DATA_CONTRACT
///                     §3, one `GET /v1/projects` read, each row with its board,
///                     configured through the widget's own project picker.
///   * systemSmall/Medium/Large + the three accessory families — Board Status:
///                     todo/ready/running/blocked/done for ONE board, picked in
///                     the widget's own configuration sheet (t_6d8731ab).
///
/// Data comes from READ-ONLY GETs against the HSCC API using the same
/// App-Group credentials as the app. State changes are minutes-scale, so the
/// timeline refreshes sparingly (well within the widget refresh budget).
@main
struct HSCCWidgetsBundle: WidgetBundle {
    var body: some Widget {
        ClusterWidget()
        MonitorWidget()
        CardsRunningWidget()
        KanbanMonitorWidget()
        ProjectsWidget()
        BoardStatusWidget()
    }
}
