import UIKit

// ===========================================================================
// LaunchAppDelegate — the app's one `UIApplicationDelegate` launch seam.
//
// It exists for the one surviving Live Activity: its BGAppRefreshTask
// identifier must be registered SYNCHRONOUSLY in `didFinishLaunching`, before
// the first scene can become active and submit a request. It also runs the
// idempotent orphan sweep and adopts an operator-started watch.
//
// It deliberately has no notification job: the apps carry no notifications
// (operator decision 2026-11-19), so there is no authorization request and no
// APNs device-token callback here.
// ===========================================================================

/// `@MainActor` because UIKit calls `didFinishLaunching` on the main thread and
/// the launch hooks it drives (`ClusterHealthActivityDriver`'s static members)
/// are MainActor-isolated. Making the whole delegate MainActor lets us call
/// `registerBackgroundTasks()` SYNCHRONOUSLY in `didFinishLaunching`.
@MainActor
final class LaunchAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Register the BGTaskScheduler background-refresh launch handler
        // SYNCHRONOUSLY, before the scene can become active and submit a
        // BGAppRefreshTaskRequest. BGTaskScheduler requires the launch handler
        // (`register(forTaskWithIdentifier:)`) to be registered BEFORE the first
        // `submit()` for that identifier — otherwise submit throws the fatal
        // `NSInternalInconsistencyException` at BGTaskScheduler.m:350 ("No launch
        // handler registered") and terminates the app at launch.
        ClusterHealthActivityDriver.registerBackgroundTasks()

        // The async block holds the launch sweep and the adopt-existing hook.
        // Both are idempotent and safe on every launch.
        Task { @MainActor in
            // Cluster watch (A2): end a bubble no live feeder can still own,
            // then adopt one the operator started before this process existed.
            // The driver never starts an activity from this hook.
            ClusterHealthActivitySweep.sweepLeftoverClusterHealth()
            ClusterHealthActivityDriver.shared.sceneDidBecomeActive()
        }
        return true
    }
}
