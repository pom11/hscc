import UIKit

// ===========================================================================
// NotificationsAppDelegate — one-time notification authorization at launch.
//
// Phase 1+2 of docs/notify-operator-plan.md. This lightweight
// `UIApplicationDelegate` is attached to `HSCCApp` via
// `@UIApplicationDelegateAdaptor`. Its Phase-1/2 job is to request
// notification authorization once, so the foreground `NotificationCoordinator`
// can later present local banners.
//
// Phase 3 (BGAppRefreshTask background refresh) registers the monitor Live
// Activity's background-refresh identifier here and routes the one-time
// launch hooks (task registration + orphan sweep) to `MonitorActivityDriver`.
// ===========================================================================

/// `@MainActor` because UIKit calls `didFinishLaunching` on the main thread and
/// the launch hooks it drives (`MonitorActivityDriver`'s static members) are
/// MainActor-isolated. Making the whole delegate MainActor lets us call
/// `registerBackgroundTasks()` SYNCHRONOUSLY in `didFinishLaunching`.
@MainActor
final class NotificationsAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Register the BGTaskScheduler background-refresh launch handler
        // SYNCHRONOUSLY, before the scene can become active and submit a
        // BGAppRefreshTaskRequest. BGTaskScheduler requires the launch handler
        // (`register(forTaskWithIdentifier:)`) to be registered BEFORE the first
        // `submit()` for that identifier — otherwise submit throws the fatal
        // `NSInternalInconsistencyException` at BGTaskScheduler.m:350 ("No launch
        // handler registered") and terminates the app at launch.
        //
        // The scene fires `.active` → `MonitorActivityDriver.sceneDidBecomeActive()`
        // → `scheduleBackgroundRefresh()` → `submit()` on the first fast launch,
        // so this registration MUST precede that submit (see MonitorActivityDriver
        // for the `registered`-gated hardening that makes submit safe regardless
        // of call order).
        MonitorActivityDriver.registerBackgroundTasks()

        // The async block holds everything that must not block launch AND does
        // not need to precede registration: the notification-auth request and
        // the monitor orphan-sweep + foreground-poll kick. All idempotent —
        // safe on every launch.
        Task { @MainActor in
            await NotificationCoordinator.shared.requestAuthorization()
            // HSCC Monitor Live Activity (t_03d318cd): run the orphan sweep and
            // kick off the foreground poll loop at launch. All idempotent —
            // safe on every launch. (A Scene has no `.task` mutation seam, so
            // the delegate's didFinishLaunching is where these one-time hooks
            // live.)
            MonitorActivityDriver.sweepLeftoverMonitors()
            MonitorActivityDriver.shared.sceneDidBecomeActive()
        }
        return true
    }
}
