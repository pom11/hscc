kind: Added
task: t_c55be8e1

- **The macOS menu bar app now surfaces the operator alert feed.** While it
  runs it polls `GET /v1/alerts?since=<id>` every 5 s through the existing
  GET-only `WidgetDataClient` (a new `alertFeed` read on its closed allowlist)
  and posts one local `UNUserNotificationCenter` banner per new alert. The
  dropdown gained an **Alerts** section with the newest six rows, a `N new`
  badge, and a small dot beside the menu bar H; the count stays visible while
  the dropdown is open and clears when it closes (viewing is local — no `ack`
  is sent to the server).

- **Polling, not APNs, for the Mac.** Receiving remote pushes in a Developer
  ID + notarized build needs `aps-environment` and its provisioning story;
  the feed is already authenticated and cheap, and the card's preferred path
  was the poll. So the Mac registers no device token and the entitlement is
  unchanged. A first launch (or a feed cleared under the stored cursor) is a
  baseline and notifies nothing, so reopening the app never replays history.
  A burst is coalesced to five banners plus one "earlier alerts" line, and
  cursors/unseen counts are stored per saved cluster.

---
kind: Verified
task: t_c55be8e1

- `macos-app/scripts/build_check.sh`: HSCCMac and HSCCMacWidgets compile with
  0 errors and 0 warnings (compile only).
- New `macos-app/scripts/alert_feed_check.sh` (also wired into the
  `app-build-checks` workflow): compiles and RUNS the shipped `AlertFeed`
  policy over fixtures — first-launch baseline, `next_since` vs `latest_id`
  paging, burst coalescing, feed-reset re-baseline, duplicate rows, level
  fallback, persisted-state round-trip — then greps the 5 s cadence, the
  `since` read, the absence of APNs registration/`aps-environment`, and the
  dropdown/H wiring.
- `ios-app/scripts/widget_data_client_check.sh`: the allowlist check now
  expects `/v1/alerts` deliberately, and the client's GET-only / no-write
  structural legs stay green.
- `hscc_daemon/tests/test_alerts.py` and
  `hscc-api/tests/test_routes_alerts.py`: the feed contract the Mac consumes
  (exclusive `since`, `next_since`, `latest_id`, `acked_at` null) passes.
