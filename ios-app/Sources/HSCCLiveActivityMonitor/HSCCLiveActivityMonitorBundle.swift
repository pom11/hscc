import SwiftUI
import WidgetKit

/// The `HSCCLiveActivityMonitor` extension's entry point.
///
/// The 2026-11-20 picks left ONE Live Activity: "Cluster watch" (A2), reusing
/// the Cluster Health activity's plumbing. The wake, monitor, running-cards and
/// board-progress activity types (and their drivers, settings rows and registry
/// manifests) were removed in the same change, so this bundle now declares one
/// `ActivityConfiguration`.
@main
struct HSCCLiveActivityMonitorBundle: WidgetBundle {
    var body: some Widget {
        ClusterHealthLiveActivity()
    }
}
