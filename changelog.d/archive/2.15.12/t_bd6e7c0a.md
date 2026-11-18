kind: Removed
task: t_bd6e7c0a

- **The live streaming chat is gone: `StreamingChatView.swift` (1094 lines),
  `StreamingChatStore.swift` (561) and `StreamingTranscript.swift` (326),
  together with the checks that exercised only them** — `streaming_check`,
  `turn_state_check`, `voice_draft_check`, `reply_watcher_check`,
  `slash_palette_check` / `slash_preview_check` and `session_activity_check`.
  First cut of the iOS pivot to a widget + Live Activity client.
  `ChatItem` / `ChatRow` / `ToolRender`, the `JSONValue`
  `renderInline()` / `renderArgs()` helpers, and `StreamPhase` with its
  `ConnectionPhase` alias were **moved into `SessionActivitySummary.swift`
  byte-for-byte** rather than deleted with the files: they are the input
  contract of `SessionActivitySummary.make(rows:phase:)` and
  `SessionActivityDriver.reflect(rows:phase:)`, which belong to the Live
  Activity path the pivot keeps, so the summary and driver derive exactly what
  they derived before. `TurnState` and `StreamSettings` went with the store —
  they drove only the chat view's own working/approval banners.
---
kind: Changed
task: t_bd6e7c0a

- **`AppShell.projectRouteView` maps the `.chat` route to
  `ProjectDetailView`.** `StreamingChatView` no longer exists, so an
  `hscc://session/<project>` deep link or an in-app Chat tap now lands on the
  project Overview instead of routing to a deleted view — the convention this
  repo already follows for a retired screen (`.clusterHub`, `.selfHeal`,
  `.boardHygiene`, `.fleetControl`). The Chat segment and its row retire with
  the two-screen collapse.
---
kind: Verified
task: t_bd6e7c0a

- `ios-app/scripts/build_check.sh` — full compile+link of every target with its
  real per-target file set against the iOS 26 SDK: HSCC 88 → **85** files,
  0 errors, 0 warnings; HSCCWidgets 8, HSCCLiveActivity 4,
  HSCCLiveActivitySession 4, HSCCLiveActivityMonitor 4, each 0 errors /
  0 warnings. `check_sources.sh`: 93 Swift files on disk, all listed in
  `project.yml`. `check_theme.sh`, `actool_icon_check.sh` and
  `model_decode_check.sh` clean. Compile gate only — never built or run on a
  device.
