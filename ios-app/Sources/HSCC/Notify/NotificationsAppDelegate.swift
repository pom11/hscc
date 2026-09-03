import UIKit

// ===========================================================================
// NotificationsAppDelegate — one-time notification authorization at launch.
//
// Phase 1+2 of docs/notify-operator-plan.md. This lightweight
// `UIApplicationDelegate` is attached to `HSCCApp` via
// `@UIApplicationDelegateAdaptor`. Its sole Phase-1/2 job is to request
// notification authorization once, so the foreground `NotificationCoordinator`
// can later present local banners.
//
// Phase 3 (BGAppRefreshTask background refresh) is a SEPARATE follow-up that
// will extend this delegate to register the background-task identifier and
// route `application(_:handleEventsForBackgroundURLSession:)`
// / BGTaskScheduler callbacks to `BackgroundRefresh`. It is deliberately kept
// out of this card (needs a real device to even run once).
// ===========================================================================

final class NotificationsAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        Task { @MainActor in
            await NotificationCoordinator.shared.requestAuthorization()
        }
        return true
    }
}
