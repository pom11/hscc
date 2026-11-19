kind: Added
task: t_1af3ef07

- **The iOS app now receives the cluster's push alerts while it is closed.**
  After notification permission is granted (alert + sound only), the app asks
  APNs for its device token and POSTs it to `/v1/devices` on **every saved
  cluster** through the normal `HSCCClient`, so each cluster's multi-address
  failover order and bearer token apply unchanged. Registration repeats on
  launch, on foreground and after the cluster list changes; the server treats
  it as an idempotent refresh, which is also how a sandbox -> production token
  corrects itself. The token stays in the App Group on the phone and is never
  logged or displayed.

- **Foreground pushes and the existing local alerts share one path.** The
  existing `UNUserNotificationCenterDelegate` already presents banners while
  the app is open and routes a tap's `hscc_url`; remote alerts use the same
  delegate. `NeedsOperatorNotifier` / `OperatorAlert` still power the local
  condition poll, so nothing regressed for events the app notices itself.

- **Settings gained an honest push status and a real Send test.** The
  Notifications section shows permission/registration state ("Push enabled —
  registered with N clusters", or the actual failure), keeps the per-class
  toggles, and its **Send test** button calls the active cluster's
  `POST /v1/devices/test` — the same push `hscc notify test` sends — after
  refreshing this phone's registration. The server's `speak` line is shown,
  so "not sent (no_config/no_devices/all_failed)" is never mistaken for
  success.

- **The app's entitlement is production for the Ad Hoc build.** `project.yml`
  writes `aps-environment=$(HSCC_APS_ENVIRONMENT)`: development for Debug,
  production for Release, matching how `release_adhoc.sh` archives with
  automatic distribution signing.

---
kind: Verified
task: t_1af3ef07

- `ios-app/scripts/build_check.sh`: all four targets compile with 0 errors and
  0 warnings (compile only, as the card requires).
- `ios-app/scripts/check_sources.sh`: the new `Notify/PushRegistrar.swift` is in
  the app target's explicit source list.
- `ios-app/scripts/notify_check.sh`: the pure `NeedsOperatorNotifier` engine
  still passes unchanged, proving the local path was kept rather than replaced.
- `hscc-api/tests/test_routes_devices.py` and
  `hscc_daemon/tests/test_notify_cli.py`: the `/v1/devices` contract and the
  `hscc notify test` argv path the new button depends on stay green.

---
kind: Docs
task: t_1af3ef07

- `ios-app/RELEASE.md` gained the one-time push setup: enable **Push
  Notifications** on the app id, let automatic signing add
  `aps-environment` to the Ad Hoc profile (regenerate it if the archive asks),
  keep Release at production, and configure the APNs key in `~/.hscc/apns.json`
  on the cluster — never in the repo. `ios-app/docs/notify-operator-plan.md`
  now says the APNs path it deferred is built and that the local decision
  engine still runs beside it, and `ios-app/README.md`'s source map names
  `PushRegistrar` next to the local notifier.
