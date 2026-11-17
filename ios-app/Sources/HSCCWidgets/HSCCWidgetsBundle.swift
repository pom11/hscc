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
        BoardStatusWidget()
    }
}
