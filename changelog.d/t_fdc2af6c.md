kind: Added
task: t_fdc2af6c

- **CarPlay scene: design C "Rows with gauges".** One `CPListTemplate` titled
  "HSCC": four node temperature gauges, top-three board stacked bars, the
  longest running card, the unacknowledged count (never "All clear" after a
  failed fetch) and the merge queue's four-stage track, each opening a pushed
  read-only detail list. The scene reuses the widget readers, refreshes every
  30 s while connected, and says `as of` / `stale` / `offline` from the data's
  own clock.
- **The CarPlay entitlement is simulator-only.** `com.apple.developer.carplay-driving-task`
  lives only in `ios-app/Sources/HSCC.simulator.entitlements`, selected by
  `CODE_SIGN_ENTITLEMENTS[sdk=iphonesimulator*]`; device and release signing
  keep the existing entitlement files byte for byte (pinned by
  `ios-app/scripts/carplay_check.sh`).

---
kind: Verified

- `ios-app/scripts/carplay_check.sh`: 59 executed reading checks (temperature
  bands, the five row sentences, queue stages, the 30 s stale rule, no
  "All clear" after a failed fetch), the iOS-SDK compile leg clean, and the
  three release entitlement files' SHA-256 pins unchanged.
- `ios-app/scripts/build_check.sh`: all four targets compile clean;
  `check_sources.sh` and `widget_registry.py --check` green; `xcodegen` emits
  the CarPlay scene role and keeps the device `CODE_SIGN_ENTITLEMENTS`.
- No CLI verb changed; no server route added. The CarPlay scene was never
  built for a device, signed, installed or launched.
