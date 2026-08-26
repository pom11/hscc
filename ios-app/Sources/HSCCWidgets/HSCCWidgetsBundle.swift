import SwiftUI
import WidgetKit

/// HSCC Home Screen widgets — cluster state at a glance.
///
/// Hosted by the `HSCCWidgets` app-extension target. Two families:
///   * systemSmall  — state (serving / waking / down) + a compact topology glyph
///   * systemMedium — topology pairs with per-node colour + model count +
///                     idle-minutes remaining before autodown fires.
///
/// Data comes from READ-ONLY GETs against the HSCC API using the same
/// App-Group credentials as the app. State changes are minutes-scale, so the
/// timeline refreshes sparingly (well within the widget refresh budget).
@main
struct HSCCWidgetsBundle: WidgetBundle {
    var body: some Widget {
        ClusterWidget()
    }
}
