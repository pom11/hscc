import SwiftUI
import WidgetKit

/// The `HSCCLiveActivityMonitor` extension's entry point.
///
/// One app extension hosts ALL THREE of HSCC's monitor-family Live Activities —
/// each with its own `ActivityConfiguration`, all declared here. This is why
/// the extension's `@main` is a `WidgetBundle` and not a single `Widget`: an
/// extension can carry several Live Activity types (iOS 17+), so the split
/// into a target per activity that this repo used for wake / session / monitor
/// is no longer needed for anything added here. The activities stay
/// independent types — a `WidgetBundle` shares only the process, never the
/// attributes or the content state — and each answers its own question: the
/// monitor one how hard the fleet is working, the cards one what it is running,
/// the cluster-health one whether it is up at all.
@main
struct HSCCLiveActivityMonitorBundle: WidgetBundle {
    var body: some Widget {
        HSCCLiveActivityMonitor()
        CardsRunningLiveActivity()
        ClusterHealthLiveActivity()
        BoardProgressLiveActivity()
    }
}
