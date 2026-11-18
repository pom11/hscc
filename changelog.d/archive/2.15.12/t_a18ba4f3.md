kind: Added
task: t_a18ba4f3

- **`ios-app/Sources/Shared/SharedStore.swift` — the App Group container and
  the typed stores every target shares.** The pivot's single shared surface,
  compiled into all five targets (app, `HSCCWidgets`, the three Live Activity
  extensions): the App Group identifier (`group.com.hscc.ios`), the connection
  settings (`SavedCluster`, the `AppGroup` key set, `APIConfig`, the shared
  keychain reference) and the widget / Live Activity configuration registry
  (`WidgetSurfaceKind`, `AppGroupWidgetSettings`), plus the last-known snapshot
  stores and `SharedStore` — the one place the suite is opened, so the
  no-fallback rule cannot be re-invented per call site. No credential-bearing
  value is ever written here: the bearer token stays in the app's own keychain
  and is only ever *referenced* (`keychainService` + `keychainAccount`),
  resolved per request. The five rules that keep a broken App Group from
  failing silently are now written down once, in the file header.
---
kind: Changed
task: t_a18ba4f3

- **`ios-app/Sources/Shared/SharedModels.swift` is now wire payloads only**
  (829 → 412 lines). Everything that is *stored* moved to `SharedStore.swift`;
  everything that is *decoded* stayed, so the file no longer references the App
  Group at all and the next slice (narrowing it to the five widget payloads)
  has nothing shared left to break.
- **`SettingsStore` reads container availability through
  `SharedStore.containerAvailable`** instead of repeating
  `containerURL(forSecurityApplicationGroupIdentifier:)` — one copy of the
  canonical check, same behaviour.
- **Five path-anchored harnesses re-pointed at the moved code**:
  `shared_store_check.sh`, `notify_check.sh`, `widget_settings_check.sh`,
  `snapshot_store_check.sh` and `first_run_check_settings.sh` (it compiles
  `SettingsStore`, so it now compiles the new file too). The App-Group asserts
  in `shared_store_check.sh` are directory-scoped over `Sources/Shared/`
  rather than pinned to one file — a file-pinned grep stops covering its own
  rule the moment a type moves, which is exactly what happened here.
---
kind: Verified
task: t_a18ba4f3

- `ios-app/scripts/build_check.sh`: HSCC **89** / HSCCWidgets **9** /
  HSCCLiveActivity **5** / HSCCLiveActivitySession **5** /
  HSCCLiveActivityMonitor **5** files, **0 error(s), 0 warning(s)** — every
  target compiles and links the new file against the iOS 26 SDK with its real
  per-target set (each count is +1 over the parent card's, which is the point).
- `check_sources.sh`: 97 Swift files, all listed in `project.yml` (was 96).
  `check_theme.sh`: CLEAN. `actool_icon_check.sh`: PASSED.
  `model_decode_check.sh`: ALL DECODE CHECKS PASSED — 62/62 fixtures.
- The five re-pointed harnesses all pass by execution, not by grep:
  `shared_store_check.sh`, `notify_check.sh`, `widget_settings_check.sh`,
  `snapshot_store_check.sh`, `first_run_check_settings.sh`.
- Whole `scripts/*.sh` suite re-run: the only reds are
  `no_op_handler_check.sh` (a `TODO` inside a comment in `Theme.swift`) and
  `offline_queue_check.sh` (a `ChatEntry` marker whose subject file is already
  gone). Both reproduce identically on the base commit — pre-existing, not
  this card's, and both named for the next worker to fix.
