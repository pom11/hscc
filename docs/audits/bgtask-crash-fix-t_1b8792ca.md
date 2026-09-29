# BGTaskScheduler crash fix — t_1b8792ca — work notes

STATUS: landed
BRANCH: wt/t_1b8792ca
ASSIGNEE: ios-engineer
LANDED ON: origin/main @ af0ff97 (pushed 2026-09-29)

## Task
Fix a CRITICAL iOS launch crash on the Monitor Live Activity background-refresh
path:

```
*** Assertion failure in -[BGTaskScheduler _handleSubmissionWithoutRegistrationForTaskRequest:error:], BGTaskScheduler.m:350
*** Terminating app due to uncaught exception 'NSInternalInconsistencyException',
    reason: 'No launch handler registered for task with identifier com.hscc.ios.monitor.backgroundRefresh'
```

## Root cause
In `ios-app/Sources/HSCC/Notify/NotificationsAppDelegate.swift`, the ONLY call to
`MonitorActivityDriver.registerBackgroundTasks()` was wrapped inside an ASYNC
`Task { @MainActor in ... }` in `didFinishLaunching`. BGTaskScheduler requires
`register(forTaskWithIdentifier:)` (the launch handler) to run BEFORE the first
`BGTaskScheduler.shared.submit()` for that identifier, or submit throws the fatal
`NSInternalInconsistencyException` at BGTaskScheduler.m:350.

On the first fast launch the SwiftUI Scene's `.onChange(of: scenePhase)`
(HSCCApp.swift:60) fires `.active` -> `MonitorActivityDriver.sceneDidBecomeActive()`
-> `scheduleBackgroundRefresh()` -> `BGTaskScheduler.shared.submit()`
(MonitorActivityDriver.swift:174). The scene activates before the deferred
async Task in didFinishLaunching reliably reaches `registerBackgroundTasks()` —
submit raced ahead of registration -> crash.

## The fix
1. `NotificationsAppDelegate` is now `@MainActor`. In
   `application(_:didFinishLaunchingWithOptions:)` it calls
   `MonitorActivityDriver.registerBackgroundTasks()` SYNCHRONOUSLY, directly in
   didFinishLaunching, BEFORE the async `Task` and BEFORE `return true`. The
   launch handler is therefore installed before the scene can submit. The async
   `Task` keeps only the notification-auth request + orphan sweep +
   foreground-poll kick (none need to precede registration).
2. `MonitorActivityDriver` hardened so submit can NEVER precede registration
   regardless of call site: a `private static registered` flag, set `true`
   synchronously as soon as `register(...)` returns in
   `registerBackgroundTasks()`; `scheduleBackgroundRefresh()` gates on it
   (`guard Self.registered else { return }`), so an un-registered submit is a
   no-op instead of a crash.
3. `registerBackgroundTasks()` handler body unchanged (runs work in a detached
   `Task { @MainActor in }`: refreshOnce -> scheduleBackgroundRefresh ->
   setTaskCompleted) — only the registration call itself must be synchronous.
4. App-Group defaults CFPrefs warning (ByHost/kCFPreferencesAnyUser) is
   non-fatal log noise. Verified `AppGroupWidgetSettings.load()` already uses
   the standard `UserDefaults(suiteName: AppGroup.suiteName)` app-group pattern
   (SharedModels.swift:472), so no wrong-domain bug exists there — no change
   warranted.

## Verification (executed)
- build_check.sh: all 5 targets (HSCC, HSCCWidgets, HSCCLiveActivity,
  HSCCLiveActivitySession, HSCCLiveActivityMonitor) 0 errors / 0 warnings.
  Fix compiles clean in the app target.
- DEVICE-TARGET BUILD: `xcodebuild Release generic/platform=iOS
  CODE_SIGNING_ALLOWED=NO` -> **BUILD SUCCEEDED**. HSCC.app + all 4 extensions
  (incl. HSCCLiveActivityMonitor.appex) compile+link+embed+validate for a real
  iOS device target (Release-iphoneos DerivedData).
- Ordering verified by code review (device-only): `didFinishLaunching` returns
  BEFORE the scene is created, so registration always precedes the first
  scene-activated submit; the `registered` guard makes submit a no-op until
  registration completes. `scheduleBackgroundRefresh()` is the only submit path
  and it cannot submit un-registered.

## Why no unit test (device-only)
The ordering contract lives on `BGTaskScheduler.shared.submit()`, an iOS-only
API (BackgroundTasks framework). It cannot run on a macOS host, so a headless
test cannot observe a submit or its absence. The testable invariant is enforced
structurally: didFinishLaunching -> synchronous register() sets `registered`
before the scene can activate, and the single submit path early-returns before
touching BGTaskScheduler when not registered. Documented as device-only per the
card's allowance.

## Honest limits
- No physical device attached (headless Mac; revoked signing cert per
  t_b6a8c450). Device compile+link proven via the generic/platform=iOS build;
  a signed on-device launch is not possible in this environment. BGAppRefresh
  background execution is OS-scheduled and can't be exercised headlessly.

## Files changed
- ios-app/Sources/HSCC/Notify/NotificationsAppDelegate.swift
- ios-app/Sources/HSCC/MonitorActivityDriver.swift
